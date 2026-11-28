import * as offlineModule from "../../offline/index";
import {
	isCurrencyFlagEnabled,
	isCurrencyRateDateWithinStalePolicy,
	oppositeCurrencyPurpose,
	type CurrencyRateStalePolicy,
} from "../../offline/currencyRatePolicy";
import { currencyRateRepository } from "../../offline/repositories";

declare const frappe: any;

const isOfflineMode = (): boolean =>
	typeof (offlineModule as any)?.isOffline === "function" &&
	Boolean((offlineModule as any).isOffline());

const getCurrentExchangeSettings =
	async (): Promise<CurrencyRateStalePolicy | null> => {
		if (
			typeof currencyRateRepository?.getExchangeSettings !== "function"
		) {
			return null;
		}
		try {
			return await currencyRateRepository.getExchangeSettings();
		} catch {
			return null;
		}
	};

const buildPolicyKey = (
	settings: CurrencyRateStalePolicy | null | undefined,
): string => {
	if (!settings) {
		return "default";
	}
	const allowStale =
		settings.allowStale === undefined || settings.allowStale === null
			? 1
			: isCurrencyFlagEnabled(settings.allowStale)
				? 1
				: 0;
	const staleDays =
		settings.staleDays !== undefined &&
		settings.staleDays !== null &&
		Number.isFinite(Number(settings.staleDays)) &&
		Number(settings.staleDays) >= 0
			? Math.trunc(Number(settings.staleDays))
			: 1;
	return `${allowStale}:${staleDays}`;
};

export type ExchangeRateSource =
	| "same_currency"
	| "manual"
	| "currency_exchange"
	| "offline_cache"
	| "derived_inverse";

export type ExchangeRateResult = {
	found: boolean;
	fromCurrency: string;
	toCurrency: string;
	rate: number | null;
	effectiveDate: string;
	rateDate?: string | null;
	source?: ExchangeRateSource;
	error?: string;
};

export type ResolveExchangeRateArgs = {
	profileName?: string;
	company?: string;
	fromCurrency: string;
	toCurrency: string;
	effectiveDate: string;
	purpose?: "for_buying" | "for_selling" | null;
	allowManualRate?: boolean;
	manualRate?: number | string | null;
	allowExternal?: boolean;
	forceRefresh?: boolean;
};

type CachedResolvedRate = {
	result: ExchangeRateResult;
	policyKey: string;
	resolvedOffline: boolean;
};

const resolvedRates = new Map<string, CachedResolvedRate>();
const pendingRates = new Map<string, Promise<ExchangeRateResult>>();

const positiveFinite = (value: unknown): number | null => {
	const rate = Number(value);
	return Number.isFinite(rate) && rate > 0 ? rate : null;
};

const cacheKey = (args: ResolveExchangeRateArgs) =>
	[
		args.profileName || "",
		args.company || "",
		args.fromCurrency,
		args.toCurrency,
		args.effectiveDate,
		args.purpose || "neutral",
		args.allowManualRate && positiveFinite(args.manualRate)
			? `manual:${positiveFinite(args.manualRate)}`
			: "automatic",
	].join("|");

const unavailable = (
	args: ResolveExchangeRateArgs,
	error?: string,
): ExchangeRateResult => ({
	found: false,
	fromCurrency: args.fromCurrency,
	toCurrency: args.toCurrency,
	rate: null,
	effectiveDate: args.effectiveDate,
	error,
});

const resolveUncached = async (
	args: ResolveExchangeRateArgs,
	exchangeSettings: CurrencyRateStalePolicy | null,
): Promise<{ result: ExchangeRateResult; resolvedOffline: boolean }> => {
	const manualRate = positiveFinite(args.manualRate);
	if (args.allowManualRate && manualRate) {
		return {
			result: {
				found: true,
				fromCurrency: args.fromCurrency,
				toCurrency: args.toCurrency,
				rate: manualRate,
				effectiveDate: args.effectiveDate,
				rateDate: args.effectiveDate,
				source: "manual",
			},
			resolvedOffline: false,
		};
	}

	const reversePurpose = oppositeCurrencyPurpose(args.purpose);

	// ERPNext setup/utils.py:124-134 precedence: exhaust ALL direct sources first
	// (warm direct -> persistent direct -> online direct when online), and ONLY consult
	// inverse sources after no direct rate is found.
	const directWarmCached = offlineModule.getCachedExchangeRate({
		profileName: args.profileName,
		company: args.company,
		fromCurrency: args.fromCurrency,
		toCurrency: args.toCurrency,
		rateDate: args.effectiveDate,
		...(args.purpose ? { purpose: args.purpose } : {}),
	});
	const directWarmMatchesPurpose =
		!args.purpose || directWarmCached?.purpose === args.purpose;
	const directWarmWithinPolicy = isCurrencyRateDateWithinStalePolicy(
		directWarmCached?.date,
		args.effectiveDate,
		exchangeSettings,
	);
	const directWarmRate =
		directWarmMatchesPurpose && directWarmWithinPolicy
			? positiveFinite(directWarmCached?.exchange_rate)
			: null;
	if (directWarmRate) {
		return {
			result: {
				found: true,
				fromCurrency: args.fromCurrency,
				toCurrency: args.toCurrency,
				rate: directWarmRate,
				effectiveDate: args.effectiveDate,
				rateDate: directWarmCached?.date || args.effectiveDate,
				source: "offline_cache",
			},
			resolvedOffline: isOfflineMode(),
		};
	}

	let deferredPersistentInverse: any = null;
	const hasSplitRepoLookup =
		typeof (currencyRateRepository as any)?.findDirectLatestOnOrBefore ===
		"function";
	if (args.profileName) {
		try {
			const lookup = {
				profileName: args.profileName,
				company: args.company || "",
				fromCurrency: args.fromCurrency,
				toCurrency: args.toCurrency,
				date: args.effectiveDate,
				...(args.purpose ? { purpose: args.purpose } : {}),
			};
			const directRecord = hasSplitRepoLookup
				? await (
						currencyRateRepository as any
					).findDirectLatestOnOrBefore(lookup)
				: await currencyRateRepository.findLatestOnOrBefore(lookup);
			if (directRecord?._inverted) {
				deferredPersistentInverse = directRecord;
			} else {
				const directRate = positiveFinite(directRecord?.exchange_rate);
				if (directRate) {
					return {
						result: {
							found: true,
							fromCurrency: args.fromCurrency,
							toCurrency: args.toCurrency,
							rate: directRate,
							effectiveDate: args.effectiveDate,
							rateDate: directRecord?.date,
							source: "offline_cache",
						},
						resolvedOffline: isOfflineMode(),
					};
				}
			}
		} catch (error) {
			console.warn("Unable to read cached exchange rate", error);
		}
	}

	let onlineError: string | undefined;
	let onlineAttempted = false;
	let onlineTransportFailed = false;
	if (!isOfflineMode()) {
		onlineAttempted = true;
		try {
			const response = await frappe.call({
				method: "posawesome.posawesome.api.invoices.resolve_exchange_rate",
				args: {
					from_currency: args.fromCurrency,
					to_currency: args.toCurrency,
					transaction_date: args.effectiveDate,
					purpose: args.purpose || null,
					allow_external: args.allowExternal === false ? 0 : 1,
				},
			});
			const payload = response?.message || {};
			const rate = positiveFinite(payload.exchange_rate);
			if (payload.found && rate) {
				const isDerivedInverse =
					payload.is_inverse === true ||
					payload.source === "derived_inverse";
				const result: ExchangeRateResult = {
					found: true,
					fromCurrency: args.fromCurrency,
					toCurrency: args.toCurrency,
					rate,
					effectiveDate: args.effectiveDate,
					rateDate: payload.date || args.effectiveDate,
					source: isDerivedInverse
						? "derived_inverse"
						: payload.source || "currency_exchange",
				};
				if (!isDerivedInverse) {
					offlineModule.saveExchangeRateCache({
						profileName: args.profileName,
						company: args.company,
						fromCurrency: args.fromCurrency,
						toCurrency: args.toCurrency,
						rateDate: args.effectiveDate,
						date: result.rateDate || args.effectiveDate,
						...(args.purpose ? { purpose: args.purpose } : {}),
						exchange_rate: rate,
					});
				}
				return { result, resolvedOffline: false };
			}
			onlineError = payload.error || "rate_unavailable";
		} catch (error: any) {
			onlineTransportFailed = true;
			onlineError = error?.message || "rate_unavailable";
		}
	}

	const resolvedOffline =
		!onlineAttempted || onlineTransportFailed || isOfflineMode();

	// No direct rate found — fall back to inverse sources with opposite purpose.
	const inMemoryInverse = resolvedRates.get(
		cacheKey({
			...args,
			fromCurrency: args.toCurrency,
			toCurrency: args.fromCurrency,
			purpose: reversePurpose,
		}),
	)?.result;
	if (
		inMemoryInverse?.found &&
		inMemoryInverse.rate &&
		isCurrencyRateDateWithinStalePolicy(
			inMemoryInverse.rateDate,
			args.effectiveDate,
			exchangeSettings,
		)
	) {
		return {
			result: {
				found: true,
				fromCurrency: args.fromCurrency,
				toCurrency: args.toCurrency,
				rate: 1 / inMemoryInverse.rate,
				effectiveDate: args.effectiveDate,
				rateDate: inMemoryInverse.rateDate,
				source: "derived_inverse",
			},
			resolvedOffline,
		};
	}

	const reverseWarmCached = offlineModule.getCachedExchangeRate({
		profileName: args.profileName,
		company: args.company,
		fromCurrency: args.toCurrency,
		toCurrency: args.fromCurrency,
		rateDate: args.effectiveDate,
		...(reversePurpose ? { purpose: reversePurpose } : {}),
	});
	const reverseWarmMatchesPurpose =
		!reversePurpose || reverseWarmCached?.purpose === reversePurpose;
	const reverseWarmWithinPolicy = isCurrencyRateDateWithinStalePolicy(
		reverseWarmCached?.date,
		args.effectiveDate,
		exchangeSettings,
	);
	const reverseWarmRate =
		reverseWarmMatchesPurpose && reverseWarmWithinPolicy
			? positiveFinite(reverseWarmCached?.exchange_rate)
			: null;
	if (reverseWarmRate) {
		return {
			result: {
				found: true,
				fromCurrency: args.fromCurrency,
				toCurrency: args.toCurrency,
				rate: 1 / reverseWarmRate,
				effectiveDate: args.effectiveDate,
				rateDate: reverseWarmCached?.date || args.effectiveDate,
				source: "derived_inverse",
			},
			resolvedOffline,
		};
	}

	if (args.profileName) {
		try {
			const inverseRecord =
				deferredPersistentInverse ??
				(typeof (currencyRateRepository as any)
					?.findInverseLatestOnOrBefore === "function"
					? await (
							currencyRateRepository as any
						).findInverseLatestOnOrBefore({
							profileName: args.profileName,
							company: args.company || "",
							fromCurrency: args.fromCurrency,
							toCurrency: args.toCurrency,
							date: args.effectiveDate,
							...(args.purpose ? { purpose: args.purpose } : {}),
						})
					: null);
			const inverseRate = positiveFinite(inverseRecord?.exchange_rate);
			if (inverseRate) {
				return {
					result: {
						found: true,
						fromCurrency: args.fromCurrency,
						toCurrency: args.toCurrency,
						rate: inverseRate,
						effectiveDate: args.effectiveDate,
						rateDate: inverseRecord?.date,
						source: "derived_inverse",
					},
					resolvedOffline,
				};
			}
		} catch (error) {
			console.warn("Unable to read cached inverse exchange rate", error);
		}
	}

	return {
		result: unavailable(args, onlineError || "rate_unavailable"),
		resolvedOffline,
	};
};

export async function resolveExchangeRate(
	args: ResolveExchangeRateArgs,
): Promise<ExchangeRateResult> {
	if (!args.fromCurrency || !args.toCurrency || !args.effectiveDate) {
		return unavailable(args, "invalid_currency_context");
	}
	if (args.fromCurrency === args.toCurrency) {
		return {
			found: true,
			fromCurrency: args.fromCurrency,
			toCurrency: args.toCurrency,
			rate: 1,
			effectiveDate: args.effectiveDate,
			rateDate: args.effectiveDate,
			source: "same_currency",
		};
	}

	const key = cacheKey(args);
	if (!args.forceRefresh && pendingRates.has(key)) {
		return pendingRates.get(key)!;
	}

	const existing = !args.forceRefresh ? resolvedRates.get(key) : undefined;
	if (
		existing &&
		(existing.result.source === "manual" ||
			existing.result.source === "same_currency")
	) {
		return existing.result;
	}

	const pending = (async () => {
		const exchangeSettings = await getCurrentExchangeSettings();
		const policyKey = buildPolicyKey(exchangeSettings);
		if (existing) {
			// Never short-circuit cached derived_inverse entries (so newly available direct
			// rates always win) and never serve a cached offline miss once connectivity returns.
			if (
				existing.result.found &&
				existing.result.source !== "derived_inverse" &&
				isCurrencyRateDateWithinStalePolicy(
					existing.result.rateDate,
					args.effectiveDate,
					exchangeSettings,
				)
			) {
				return existing.result;
			}
			if (
				!existing.result.found &&
				!existing.resolvedOffline &&
				!isOfflineMode() &&
				existing.policyKey === policyKey
			) {
				return existing.result;
			}
			resolvedRates.delete(key);
		}
		const { result, resolvedOffline } = await resolveUncached(
			args,
			exchangeSettings,
		);
		resolvedRates.set(key, { result, policyKey, resolvedOffline });
		return result;
	})().finally(() => {
		pendingRates.delete(key);
	});
	pendingRates.set(key, pending);
	return pending;
}

export function convertCurrencyAmount(
	amount: unknown,
	rate: unknown,
): number | null {
	const numericAmount = Number(amount);
	const numericRate = positiveFinite(rate);
	if (!Number.isFinite(numericAmount) || !numericRate) {
		return null;
	}
	return numericAmount * numericRate;
}

export function clearExchangeRateResolverCache() {
	resolvedRates.clear();
	pendingRates.clear();
}
