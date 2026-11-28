import {
	syncBootstrapConfigResource,
	syncCurrencyMatrixResource,
	syncCustomersResource,
	syncItemPricesResource,
	syncItemsResource,
	syncPaymentMethodCurrenciesResource,
	syncPriceListMetaResource,
	syncPricingRulesResource,
	syncStockResource,
} from "./adapters";
import { syncInvoiceOutboxResource } from "../invoiceOutbox";
import { db } from "../db";
import { getCachedPriceListMeta, getOpeningStorage } from "../cache";
import type { SyncScopedProfile } from "./adapters/common";
import type {
	SyncResourceDefinition,
	SyncResourceId,
	SyncResourceState,
} from "./types";

const SUPPORTED_OFFLINE_SYNC_RESOURCE_IDS = new Set<SyncResourceId>([
	"bootstrap_config",
	"price_list_meta",
	"currency_matrix",
	"payment_method_currencies",
	"items",
	"item_prices",
	"pricing_rules",
	"stock",
	"customers",
	"invoice_outbox",
]);

type SupportedSyncProfile = SyncScopedProfile & {
	currency?: string | null;
	company_currency?: string | null;
	selling_price_list?: string | null;
	buying_price_list?: string | null;
	posa_allow_multi_currency?: boolean;
	payments?: any[];
};

async function collectCurrencyMatrixPairs(
	posProfile: SupportedSyncProfile,
): Promise<Array<{ from_currency: string; to_currency: string }>> {
	let storedProfile: any = null;
	try {
		if (posProfile?.name) {
			if (!db.isOpen()) {
				await db.open();
			}
			storedProfile = await db.table("pos_profiles").get(posProfile.name);
		}
	} catch {
		storedProfile = null;
	}

	const openingCompanyCurrency = String(
		getOpeningStorage()?.company?.default_currency || "",
	).trim();
	const companyCurrency = String(
		posProfile.company_currency ||
			storedProfile?.company_currency ||
			openingCompanyCurrency ||
			"",
	).trim();
	if (!companyCurrency) {
		return [];
	}

	const foreignCurrencies = new Set<string>();
	const addCurrency = (value: unknown) => {
		const normalized = String(value || "").trim();
		if (normalized && normalized !== companyCurrency) {
			foreignCurrencies.add(normalized);
		}
	};

	addCurrency(posProfile.currency);
	addCurrency(storedProfile?.currency);
	const priceListMeta = posProfile?.name
		? getCachedPriceListMeta(posProfile.name)
		: null;
	addCurrency(priceListMeta?.price_list_currency);

	const buyingPriceList = String(
		posProfile.buying_price_list || storedProfile?.buying_price_list || "",
	).trim();
	if (buyingPriceList) {
		try {
			const buyingRows = await db
				.table("item_price_records")
				.where("price_list")
				.equals(buyingPriceList)
				.toArray();
			for (const row of buyingRows) {
				addCurrency(row?.currency);
			}
		} catch {
			// Best-effort discovery from local buying price cache.
		}
	}

	const pairs: Array<{ from_currency: string; to_currency: string }> = [];
	for (const foreignCurrency of foreignCurrencies) {
		pairs.push({
			from_currency: foreignCurrency,
			to_currency: companyCurrency,
		});
		pairs.push({
			from_currency: companyCurrency,
			to_currency: foreignCurrency,
		});
	}
	return pairs;
}

type CallOfflineSyncMethod = (
	_method: string,
	_args?: Record<string, any>,
) => Promise<any>;

type RunSupportedOfflineSyncResourceArgs = {
	resource: SyncResourceDefinition;
	posProfile: SupportedSyncProfile;
	schemaVersion?: string | null;
	getPersistedState: (
		_resourceId: SyncResourceId,
	) => Promise<SyncResourceState | null>;
	getRuntimeState?: (_resourceId: SyncResourceId) => SyncResourceState | null;
	callOfflineSyncMethod: CallOfflineSyncMethod;
};

function getPersistedWatermark(state: SyncResourceState | null | undefined) {
	return state?.watermark || null;
}

export function isSupportedOfflineSyncResourceId(
	resourceId: SyncResourceId | null | undefined,
) {
	return !!(
		resourceId && SUPPORTED_OFFLINE_SYNC_RESOURCE_IDS.has(resourceId)
	);
}

export function filterSupportedOfflineSyncResources(
	resources: SyncResourceDefinition[] = [],
) {
	return (resources || []).filter((resource) =>
		isSupportedOfflineSyncResourceId(resource?.id),
	);
}

export function filterSupportedOfflineSyncStates(
	states: SyncResourceState[] = [],
) {
	return (states || []).filter((state) =>
		isSupportedOfflineSyncResourceId(state?.resourceId),
	);
}

export function buildOfflineSyncProfile(
	profile: any,
): SupportedSyncProfile | null {
	if (!profile?.name) {
		return null;
	}

	return {
		name: profile.name,
		company: profile.company || null,
		warehouse: profile.warehouse || null,
		modified: profile.modified || null,
		currency: profile.currency || null,
		selling_price_list: profile.selling_price_list || null,
		posa_allow_multi_currency: !!profile.posa_allow_multi_currency,
		payments: Array.isArray(profile.payments) ? profile.payments : [],
	};
}

async function runSupportedOfflineSyncResourceOnce({
	resource,
	posProfile,
	getPersistedState,
	callOfflineSyncMethod,
}: RunSupportedOfflineSyncResourceArgs) {
	const persistedState = await getPersistedState(resource.id);
	const sharedArgs = {
		posProfile,
		watermark: getPersistedWatermark(persistedState),
		// Resource endpoints evolve independently. Persisted server state is the
		// protocol authority; a global frontend date cannot represent them all.
		schemaVersion: persistedState?.schemaVersion || null,
	};

	switch (resource.id) {
		case "bootstrap_config":
			return syncBootstrapConfigResource({
				...sharedArgs,
				fetcher: ({ posProfile, watermark, schemaVersion }) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.bootstrap.sync_bootstrap_config",
						{
							pos_profile: posProfile,
							watermark,
							schema_version: schemaVersion,
						},
					),
			});
		case "price_list_meta":
			return syncPriceListMetaResource({
				...sharedArgs,
				fetcher: ({ posProfile, watermark, schemaVersion }) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.bootstrap.sync_bootstrap_config",
						{
							pos_profile: posProfile,
							watermark,
							schema_version: schemaVersion,
						},
					),
			});
		case "currency_matrix": {
			const discoveredCurrencyPairs =
				await collectCurrencyMatrixPairs(posProfile);
			return syncCurrencyMatrixResource({
				...sharedArgs,
				...(discoveredCurrencyPairs.length
					? { currencyPairs: discoveredCurrencyPairs }
					: {}),
				fetcher: ({
					posProfile,
					currencyPairs = [],
					watermark,
					offset,
					schemaVersion,
				}) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.currencies.sync_currency_scope",
						{
							pos_profile: posProfile,
							watermark,
							currency_pairs: currencyPairs,
							offset: offset || 0,
							schema_version: schemaVersion,
						},
					),
			});
		}
		case "payment_method_currencies":
			return syncPaymentMethodCurrenciesResource({
				...sharedArgs,
				fetcher: ({ posProfile, watermark, schemaVersion }) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.payment_methods.sync_payment_method_currencies",
						{
							pos_profile: posProfile,
							watermark,
							schema_version: schemaVersion,
						},
					),
			});
		case "items":
			return syncItemsResource({
				...sharedArgs,
				priceList: posProfile.selling_price_list || null,
				fetcher: ({
					posProfile,
					priceList,
					customer,
					watermark,
					startAfter,
					limit,
					schemaVersion,
				}) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.items.sync_items",
						{
							pos_profile: posProfile,
							price_list: priceList,
							customer: customer || null,
							watermark,
							start_after: startAfter || null,
							limit: limit || null,
							schema_version: schemaVersion,
						},
					),
			});
		case "item_prices":
			return syncItemPricesResource({
				...sharedArgs,
				fetcher: ({ posProfile, watermark, offset, schemaVersion }) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.item_prices.sync_item_prices",
						{
							pos_profile: posProfile,
							watermark,
							offset: offset || 0,
							schema_version: schemaVersion,
						},
					),
			});
		case "pricing_rules":
			return syncPricingRulesResource({
				...sharedArgs,
				fetcher: ({ posProfile, watermark, offset, schemaVersion }) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.pricing_rules.sync_pricing_rules",
						{
							pos_profile: posProfile,
							watermark,
							offset: offset || 0,
							schema_version: schemaVersion,
						},
					),
			});
		case "stock":
			return syncStockResource({
				...sharedArgs,
				fetcher: ({ posProfile, watermark, schemaVersion }) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.stock.sync_stock",
						{
							pos_profile: posProfile,
							watermark,
							schema_version: schemaVersion,
						},
					),
			});
		case "customers":
			return syncCustomersResource({
				...sharedArgs,
				fetcher: ({
					posProfile,
					watermark,
					startAfter,
					limit,
					schemaVersion,
				}) =>
					callOfflineSyncMethod(
						"posawesome.posawesome.api.offline_sync.customers.sync_customers",
						{
							pos_profile: posProfile,
							watermark,
							start_after: startAfter || null,
							limit: limit || null,
							schema_version: schemaVersion,
						},
					),
			});
		case "invoice_outbox":
			return syncInvoiceOutboxResource(callOfflineSyncMethod);
		default:
			return {
				status: "idle",
			};
	}
}

export async function runSupportedOfflineSyncResource(
	args: RunSupportedOfflineSyncResourceArgs,
) {
	const persistedState = await args.getPersistedState(args.resource.id);
	const firstResult = await runSupportedOfflineSyncResourceOnce({
		...args,
		getPersistedState: async () => persistedState,
	});
	const firstResponse =
		"response" in firstResult ? firstResult.response : null;

	if (
		!firstResponse?.full_resync_required ||
		args.resource.fullResyncSupported === false
	) {
		return firstResult;
	}

	// The first pass invalidated the incompatible local representation. Finish
	// its clean rebuild now so resource health does not remain "limited" until
	// a later timer tick.
	return runSupportedOfflineSyncResourceOnce({
		...args,
		getPersistedState: async () =>
			({
				...(persistedState || {}),
				resourceId: args.resource.id,
				watermark: null,
				schemaVersion: firstResponse.schema_version || null,
			}) as SyncResourceState,
	});
}
