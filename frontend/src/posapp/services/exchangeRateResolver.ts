import {
	getCachedExchangeRate,
	saveExchangeRateCache,
} from "../../offline/index";
import {
	isCurrencyRateDateWithinStalePolicy,
	oppositeCurrencyPurpose,
} from "../../offline/currencyRatePolicy";
import { currencyRateRepository } from "../../offline/repositories";

declare const frappe: any;

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

const resolvedRates = new Map<string, ExchangeRateResult>();
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
): Promise<ExchangeRateResult> => {
	const manualRate = positiveFinite(args.manualRate);
	if (args.allowManualRate && manualRate) {
		return {
			found: true,
			fromCurrency: args.fromCurrency,
			toCurrency: args.toCurrency,
			rate: manualRate,
			effectiveDate: args.effectiveDate,
			rateDate: args.effectiveDate,
			source: "manual",
		};
	}

	const exchangeSettings =
		typeof currencyRateRepository?.getExchangeSettings === "function"
			? await currencyRateRepository.getExchangeSettings()
			: null;
	const reversePurpose = oppositeCurrencyPurpose(args.purpose);

	const inverse = resolvedRates.get(
		cacheKey({
			...args,
			fromCurrency: args.toCurrency,
			toCurrency: args.fromCurrency,
			purpose: reversePurpose,
		}),
	);
	if (
		inverse?.found &&
		inverse.rate &&
		isCurrencyRateDateWithinStalePolicy(
			inverse.rateDate,
			args.effectiveDate,
			exchangeSettings,
		)
	) {
		return {
			found: true,
			fromCurrency: args.fromCurrency,
			toCurrency: args.toCurrency,
			rate: 1 / inverse.rate,
			effectiveDate: args.effectiveDate,
			rateDate: inverse.rateDate,
			source: "derived_inverse",
		};
	}

	const legacyCached = getCachedExchangeRate({
		profileName: args.profileName,
		company: args.company,
		fromCurrency: args.fromCurrency,
		toCurrency: args.toCurrency,
		rateDate: args.effectiveDate,
		...(args.purpose ? { purpose: args.purpose } : {}),
	});
	// Never let an unqualified/mismatched or stale warm-cache entry override
	// an explicit purpose lookup or Accounts Settings stale_days policy.
	const legacyMatchesPurpose =
		!args.purpose || legacyCached?.purpose === args.purpose;
	const legacyWithinStalePolicy = isCurrencyRateDateWithinStalePolicy(
		legacyCached?.date,
		args.effectiveDate,
		exchangeSettings,
	);
	const legacyRate =
		legacyMatchesPurpose && legacyWithinStalePolicy
			? positiveFinite(legacyCached?.exchange_rate)
			: null;
	if (legacyRate) {
		return {
			found: true,
			fromCurrency: args.fromCurrency,
			toCurrency: args.toCurrency,
			rate: legacyRate,
			effectiveDate: args.effectiveDate,
			rateDate: legacyCached?.date || args.effectiveDate,
			source: "offline_cache",
		};
	}

	const reverseWarmCached = getCachedExchangeRate({
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
			found: true,
			fromCurrency: args.fromCurrency,
			toCurrency: args.toCurrency,
			rate: 1 / reverseWarmRate,
			effectiveDate: args.effectiveDate,
			rateDate: reverseWarmCached?.date || args.effectiveDate,
			source: "derived_inverse",
		};
	}

	if (args.profileName) {
		try {
			const cachedRecord = await currencyRateRepository.findLatestOnOrBefore({
				profileName: args.profileName,
				company: args.company || "",
				fromCurrency: args.fromCurrency,
				toCurrency: args.toCurrency,
				date: args.effectiveDate,
				...(args.purpose ? { purpose: args.purpose } : {}),
			});
			const cachedRate = positiveFinite(cachedRecord?.exchange_rate);
			if (cachedRate) {
				return {
					found: true,
					fromCurrency: args.fromCurrency,
					toCurrency: args.toCurrency,
					rate: cachedRate,
					effectiveDate: args.effectiveDate,
					rateDate: cachedRecord?.date,
					source: cachedRecord?._inverted
						? "derived_inverse"
						: "offline_cache",
				};
			}
		} catch (error) {
			console.warn("Unable to read cached exchange rate", error);
		}
	}

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
		if (!payload.found || !rate) {
			return unavailable(args, payload.error || "rate_unavailable");
		}

		const result: ExchangeRateResult = {
			found: true,
			fromCurrency: args.fromCurrency,
			toCurrency: args.toCurrency,
			rate,
			effectiveDate: args.effectiveDate,
			rateDate: payload.date || args.effectiveDate,
			source: payload.source || "currency_exchange",
		};
		saveExchangeRateCache({
			profileName: args.profileName,
			company: args.company,
			fromCurrency: args.fromCurrency,
			toCurrency: args.toCurrency,
			rateDate: args.effectiveDate,
			date: result.rateDate || args.effectiveDate,
			...(args.purpose ? { purpose: args.purpose } : {}),
			exchange_rate: rate,
		});
		return result;
	} catch (error: any) {
		return unavailable(args, error?.message || "rate_unavailable");
	}
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
	if (!args.forceRefresh && resolvedRates.has(key)) {
		return resolvedRates.get(key)!;
	}
	if (!args.forceRefresh && pendingRates.has(key)) {
		return pendingRates.get(key)!;
	}

	const pending = resolveUncached(args).then((result) => {
		resolvedRates.set(key, result);
		pendingRates.delete(key);
		return result;
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
