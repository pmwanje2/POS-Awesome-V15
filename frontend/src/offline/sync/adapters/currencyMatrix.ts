import {
	saveCurrencyOptionsCache,
	saveExchangeRateCache,
} from "../../cache";
import {
	currencyRateRepository,
	type OfflineCurrencyRateRecord,
} from "../../repositories";
import {
	buildResourceSyncResult,
	buildScopeSignature,
	persistResourceSyncState,
	refreshSnapshotFromSync,
	type ResourceSyncResult,
	type SyncResponse,
	type SyncScopedProfile,
} from "./common";
import * as syncStateModule from "../syncState";

type CurrencyPair = {
	from_currency: string;
	to_currency: string;
};

type CurrencyMatrixFetcher = (_args: {
	posProfile: SyncScopedProfile;
	currencyPairs?: CurrencyPair[];
	backfillPairs?: CurrencyPair[];
	watermark?: string | null;
	offset?: number;
	schemaVersion?: string | null;
}) => Promise<SyncResponse>;

type CurrencyMatrixSyncArgs = {
	posProfile: SyncScopedProfile;
	currencyPairs?: CurrencyPair[];
	backfillPairs?: CurrencyPair[];
	syncedCurrencyPairs?: string[] | null;
	watermark?: string | null;
	schemaVersion?: string | null;
	fetcher: CurrencyMatrixFetcher;
};

function toCanonicalPairKey(
	fromCurrency: unknown,
	toCurrency: unknown,
): string | null {
	const from = String(fromCurrency || "").trim();
	const to = String(toCurrency || "").trim();
	if (!from || !to || from === to) {
		return null;
	}
	return `${from}::${to}`;
}

export async function syncCurrencyMatrixResource(
	args: CurrencyMatrixSyncArgs,
): Promise<ResourceSyncResult> {
	const scopeSignature = buildScopeSignature(args.posProfile);
	let existingState: any = null;
	try {
		const getState = (syncStateModule as any)?.getSyncResourceState;
		if (typeof getState === "function") {
			existingState = await getState("currency_matrix");
		}
	} catch {
		// Best-effort read of prior sync state.
	}
	const scopeChanged = Boolean(
		existingState?.scopeSignature &&
			existingState.scopeSignature !== scopeSignature,
	);
	const effectiveWatermark = scopeChanged ? null : args.watermark || null;
	if (!effectiveWatermark) {
		await currencyRateRepository.clear();
	}

	let currencyOptionsCount: number | undefined;
	let exchangeRateCount = 0;
	let offset = 0;
	let finalResponse: SyncResponse = {};
	let priorSyncedPairs: string[] = [];
	if (effectiveWatermark && !scopeChanged) {
		if (Array.isArray(args.syncedCurrencyPairs)) {
			priorSyncedPairs = args.syncedCurrencyPairs.filter(Boolean);
		} else if (Array.isArray(existingState?.syncedCurrencyPairs)) {
			priorSyncedPairs = existingState.syncedCurrencyPairs.filter(Boolean);
		}
	}
	const acknowledgedPairs = new Set<string>(priorSyncedPairs);
	let hasServerSyncedPairs = false;

	while (true) {
		const response = await args.fetcher({
			posProfile: args.posProfile,
			currencyPairs: args.currencyPairs || [],
			...(effectiveWatermark && args.backfillPairs?.length
				? { backfillPairs: args.backfillPairs }
				: {}),
			watermark: effectiveWatermark,
			offset,
			schemaVersion: args.schemaVersion,
		});
		finalResponse = response;

		if (response?.full_resync_required) {
			refreshSnapshotFromSync({
				posProfile: args.posProfile,
				cacheState: {
					currencyOptionsCount: 0,
					exchangeRateCount: 0,
				},
			});
			await persistResourceSyncState({
				resourceId: "currency_matrix",
				status: "limited",
				posProfile: args.posProfile,
				response,
				watermark: effectiveWatermark,
				syncedCurrencyPairs: [],
			});
			return buildResourceSyncResult(
				"currency_matrix",
				"limited",
				response,
				effectiveWatermark,
				{
					scopeSignature,
					syncedCurrencyPairs: [],
				},
			);
		}

		if (Array.isArray(response?.synced_pairs)) {
			hasServerSyncedPairs = true;
			for (const pair of response.synced_pairs) {
				const key = toCanonicalPairKey(
					pair?.from_currency,
					pair?.to_currency,
				);
				if (key) acknowledgedPairs.add(key);
			}
		}

		for (const change of response?.changes || []) {
			if (
				change?.key === "currency_options" &&
				Array.isArray(change.data)
			) {
				saveCurrencyOptionsCache(args.posProfile.name, change.data);
				currencyOptionsCount = change.data.length;
				continue;
			}

			if (!String(change?.key || "").startsWith("exchange_rate::")) {
				if (String(change?.key || "").startsWith("currency_rate::")) {
					const row = change?.data || {};
					await currencyRateRepository.upsertMany([
						{
							...row,
							profile_name: args.posProfile.name,
							company: args.posProfile.company || "",
						} as OfflineCurrencyRateRecord,
					]);
				}
				continue;
			}

			const rate = change?.data || {};
			saveExchangeRateCache({
				profileName: args.posProfile.name,
				company: args.posProfile.company || undefined,
				fromCurrency: rate.from_currency,
				toCurrency: rate.to_currency,
				date: rate.date,
				...(rate.purpose ? { purpose: rate.purpose } : {}),
				exchange_rate: rate.exchange_rate,
			});
			exchangeRateCount += 1;
		}
		const deletedRateNames = (response?.deleted || [])
			.map((entry) => String(entry?.key || ""))
			.filter((key) => key.startsWith("currency_rate::"))
			.map((key) => key.slice("currency_rate::".length))
			.filter(Boolean);
		await currencyRateRepository.deleteByNames(deletedRateNames);
		if (
			response?.currency_settings &&
			typeof currencyRateRepository.saveExchangeSettings === "function"
		) {
			await currencyRateRepository.saveExchangeSettings(
				response.currency_settings,
			);
		}

		if (!response?.has_more) {
			break;
		}
		const nextOffset = Number(response?.next_offset);
		if (!Number.isFinite(nextOffset) || nextOffset <= offset) {
			throw new Error("Currency matrix sync returned an invalid next_offset");
		}
		offset = nextOffset;
	}

	if (
		typeof currencyOptionsCount !== "undefined" ||
		exchangeRateCount > 0
	) {
		refreshSnapshotFromSync({
			posProfile: args.posProfile,
			cacheState: {
				...(typeof currencyOptionsCount !== "undefined"
					? { currencyOptionsCount }
					: {}),
				...(exchangeRateCount > 0 ? { exchangeRateCount } : {}),
			},
		});
	}

	if (!hasServerSyncedPairs) {
		// Fallback for servers that do not return `synced_pairs`: only treat explicitly
		// full-synced or explicitly backfilled pairs as covered (never infer coverage from
		// opportunistic incremental rows).
		const fallbackCoveredPairs = effectiveWatermark
			? args.backfillPairs || []
			: [...(args.currencyPairs || []), ...(args.backfillPairs || [])];
		for (const pair of fallbackCoveredPairs) {
			const key = toCanonicalPairKey(
				pair?.from_currency,
				pair?.to_currency,
			);
			if (key) acknowledgedPairs.add(key);
		}
	}

	const persistedPairs = Array.from(acknowledgedPairs).sort();

	await persistResourceSyncState({
		resourceId: "currency_matrix",
		status: "fresh",
		posProfile: args.posProfile,
		response: finalResponse,
		watermark: effectiveWatermark,
		syncedCurrencyPairs: persistedPairs,
	});
	return buildResourceSyncResult(
		"currency_matrix",
		"fresh",
		finalResponse,
		effectiveWatermark,
		{
			scopeSignature,
			syncedCurrencyPairs: persistedPairs,
		},
	);
}
