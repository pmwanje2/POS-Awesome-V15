import {
	itemPriceRepository,
	type OfflineItemPriceRecord,
} from "../../repositories";
import { setProfileBuyingPriceList } from "../../cache";
import { traceStartupEvent } from "../../../utils/startupTrace";
import {
	buildResourceSyncResult,
	persistResourceSyncState,
	type ResourceSyncResult,
	type SyncResponse,
	type SyncScopedProfile,
} from "./common";

type ItemPricesFetcher = (_args: {
	posProfile: SyncScopedProfile;
	watermark?: string | null;
	offset?: number;
	schemaVersion?: string | null;
}) => Promise<SyncResponse>;

type ItemPricesSyncArgs = {
	posProfile: SyncScopedProfile;
	watermark?: string | null;
	schemaVersion?: string | null;
	fetcher: ItemPricesFetcher;
};

function itemPriceNames(response: SyncResponse) {
	return (response?.deleted || [])
		.map((entry) => String(entry?.key || ""))
		.filter((key) => key.startsWith("item_price::"))
		.map((key) => key.slice("item_price::".length))
		.filter(Boolean);
}

export async function syncItemPricesResource(
	args: ItemPricesSyncArgs,
): Promise<ResourceSyncResult> {
	if (!args.watermark) {
		await itemPriceRepository.clear();
	}

	let offset = 0;
	let finalResponse: SyncResponse = {};
	let scopeApplied = false;
	let page = 0;
	while (true) {
		const response = await args.fetcher({
			posProfile: args.posProfile,
			watermark: args.watermark || null,
			offset,
			schemaVersion: args.schemaVersion,
		});
		finalResponse = response;
		page += 1;
		const changesCount = Array.isArray(response?.changes)
			? response.changes.length
			: 0;
		const pageDiagnostics = {
			page,
			offset,
			nextOffset: response?.next_offset ?? null,
			hasMore: Boolean(response?.has_more),
			changesCount,
			deletedCount: Array.isArray(response?.deleted)
				? response.deleted.length
				: 0,
			schemaVersion: response?.schema_version || null,
		};
		traceStartupEvent(
			"offline_sync.item_prices_page",
			"ok",
			pageDiagnostics,
		);
		if (!scopeApplied && Array.isArray(response?.scope?.price_lists)) {
			await itemPriceRepository.deleteOutsidePriceLists(
				response.scope.price_lists,
			);
			scopeApplied = true;
		}
		if (response?.scope?.buying_price_list) {
			const buyingNotUomDependent =
				response.scope.buying_price_not_uom_dependent;
			if (buyingNotUomDependent !== undefined) {
				await setProfileBuyingPriceList(
					args.posProfile,
					String(response.scope.buying_price_list),
					buyingNotUomDependent,
				);
			} else {
				await setProfileBuyingPriceList(
					args.posProfile,
					String(response.scope.buying_price_list),
				);
			}
		}

		if (response?.full_resync_required) {
			await itemPriceRepository.clear();
			await persistResourceSyncState({
				resourceId: "item_prices",
				status: "limited",
				posProfile: args.posProfile,
				response,
				watermark: args.watermark,
			});
			return buildResourceSyncResult(
				"item_prices",
				"limited",
				response,
				args.watermark,
			);
		}

		const rows = (response?.changes || [])
			.map((entry) => entry?.data)
			.filter(
				(row): row is OfflineItemPriceRecord =>
					!!row?.name && !!row?.price_list && !!row?.item_code,
			);
		await itemPriceRepository.upsertMany(rows);
		await itemPriceRepository.deleteByNames(itemPriceNames(response));

		if (!response?.has_more) {
			break;
		}
		const reportedNextOffset = Number(response?.next_offset);
		let nextOffset = reportedNextOffset;
		if (!Number.isFinite(nextOffset) || nextOffset <= offset) {
			const inferredNextOffset = offset + changesCount;
			traceStartupEvent("offline_sync.item_prices_pagination", "error", {
				...pageDiagnostics,
				reportedNextOffset: response?.next_offset ?? null,
				inferredNextOffset,
			});
			console.error(
				"Item Price sync received an invalid pagination cursor",
				{
					...pageDiagnostics,
					reportedNextOffset: response?.next_offset ?? null,
					inferredNextOffset,
				},
			);
			if (changesCount <= 0 || inferredNextOffset <= offset) {
				throw new Error(
					`Item Price sync returned an invalid next_offset at page ${page} (offset=${offset}, next_offset=${String(response?.next_offset)}, has_more=${String(response?.has_more)}, changes=${changesCount})`,
				);
			}
			nextOffset = inferredNextOffset;
			traceStartupEvent("offline_sync.item_prices_pagination", "info", {
				...pageDiagnostics,
				recoveredNextOffset: nextOffset,
			});
		}
		offset = nextOffset;
	}

	await persistResourceSyncState({
		resourceId: "item_prices",
		status: "fresh",
		posProfile: args.posProfile,
		response: finalResponse,
		watermark: args.watermark,
	});
	return buildResourceSyncResult(
		"item_prices",
		"fresh",
		finalResponse,
		args.watermark,
	);
}
