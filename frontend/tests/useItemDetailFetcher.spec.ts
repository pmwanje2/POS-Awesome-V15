import { beforeEach, describe, expect, it, vi } from "vitest";

const { getCachedItemDetails, saveItemDetailsCache, updateLocalStockCache } =
	vi.hoisted(() => ({
		getCachedItemDetails: vi.fn(),
		saveItemDetailsCache: vi.fn(),
		updateLocalStockCache: vi.fn(),
	}));

vi.mock("../src/offline/index", () => ({
	getCachedItemDetails,
	saveItemDetailsCache,
	saveItemsBulk: vi.fn(async () => {}),
	updateLocalStockCache,
	saveItemUOMs: vi.fn(),
	getItemUOMs: vi.fn(() => []),
	getLocalStock: vi.fn(() => null),
	getLocalStockCache: vi.fn(() => ({})),
	isStockCacheReady: vi.fn(() => false),
	initializeStockCache: vi.fn(async () => {}),
	isOffline: vi.fn(() => false),
}));

vi.mock("../src/posapp/utils/perf.js", () => ({
	scheduleFrame: vi.fn(async () => {}),
}));

import { useItemDetailFetcher } from "../src/posapp/composables/pos/items/useItemDetailFetcher";

describe("useItemDetailFetcher", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		getCachedItemDetails.mockResolvedValue({
			cached: [],
			missing: ["ITEM-1"],
		});
		(globalThis as any).frappe = {
			call: vi.fn(async () => ({
				message: [
					{
						item_code: "ITEM-1",
						actual_qty: 0,
						has_batch_no: 1,
						has_serial_no: 1,
						batch_no_data: [],
						serial_no_data: [],
					},
				],
			})),
		};
	});

	it("replaces stale batch and serial lists when server returns empty arrays", async () => {
		const fetcher = useItemDetailFetcher();
		fetcher.registerContext({
			pos_profile: { name: "POS-TEST" },
			active_price_list: "Standard Selling",
			itemAvailability: null,
			applyCurrencyConversionToItem: vi.fn(),
			items: [],
			displayedItems: [],
			usesLimitSearch: false,
			storageAvailable: false,
		});

		const item: any = {
			item_code: "ITEM-1",
			has_batch_no: 1,
			has_serial_no: 1,
			batch_no_data: [{ batch_no: "B-EXPIRED", batch_qty: 7, is_expired: true }],
			serial_no_data: [{ serial_no: "SER-OLD" }],
		};

		await fetcher.update_items_details([item], { forceRefresh: true });

		expect(item.batch_no_data).toEqual([]);
		expect(item.serial_no_data).toEqual([]);
		expect(item.actual_qty).toBe(0);
		expect(updateLocalStockCache).toHaveBeenCalledTimes(1);
		expect(saveItemDetailsCache).toHaveBeenCalledTimes(1);
	});

	it("resolves context getters dynamically for POS profile", async () => {
		const fetcher = useItemDetailFetcher();
		let currentProfile: any = null;

		fetcher.registerContext({
			get pos_profile() {
				return currentProfile;
			},
			active_price_list: "Standard Selling",
			itemAvailability: null,
			applyCurrencyConversionToItem: vi.fn(),
			items: [],
			displayedItems: [],
			usesLimitSearch: false,
			storageAvailable: false,
		});

		currentProfile = { name: "POS-TEST" };

		const details = await fetcher.fetchItemDetails([{ item_code: "ITEM-1" }]);

		expect((globalThis as any).frappe.call).toHaveBeenCalledTimes(1);
		expect(details).toHaveLength(1);
	});

	it("re-applies cart reservations after cached visible stock refreshes", async () => {
		getCachedItemDetails.mockResolvedValue({
			cached: [{ item_code: "ITEM-1", actual_qty: 10 }],
			missing: [],
		});
		const item = { item_code: "ITEM-1", actual_qty: 8 };
		const itemAvailability = {
			captureBaseAvailability: vi.fn(),
			applyReservationToItem: vi.fn((target: any) => {
				target.actual_qty = 8;
			}),
		};
		const fetcher = useItemDetailFetcher();
		fetcher.registerContext({
			pos_profile: { name: "POS-TEST" },
			active_price_list: "Standard Selling",
			itemAvailability,
			displayedItems: [item],
			usesLimitSearch: false,
			storageAvailable: false,
		});

		await fetcher.refreshPricesForVisibleItems();

		expect(itemAvailability.captureBaseAvailability).toHaveBeenCalledWith(
			item,
			10,
		);
		expect(itemAvailability.applyReservationToItem).toHaveBeenCalledWith(
			item,
		);
		expect(item.actual_qty).toBe(8);
	});

	it("hydrates batch and serial options from cached item details", async () => {
		getCachedItemDetails.mockResolvedValue({
			cached: [
				{
					item_code: "ITEM-1",
					actual_qty: 4,
					has_batch_no: 1,
					has_serial_no: 1,
					batch_no_data: [
						{ batch_no: "B-CACHED", batch_qty: 4 },
					],
					serial_no_data: [
						{ serial_no: "S-CACHED", batch_no: "B-CACHED" },
					],
					item_uoms: [{ uom: "Nos", conversion_factor: 1 }],
				},
			],
			missing: [],
		});
		const fetcher = useItemDetailFetcher();
		fetcher.registerContext({
			pos_profile: { name: "POS-TEST" },
			active_price_list: "Standard Selling",
			itemAvailability: null,
			applyCurrencyConversionToItem: vi.fn(),
			usesLimitSearch: false,
			storageAvailable: false,
		});
		const item: any = { item_code: "ITEM-1", stock_uom: "Nos" };

		await fetcher.update_items_details([item]);

		expect(item.batch_no_data).toEqual([
			{ batch_no: "B-CACHED", batch_qty: 4 },
		]);
		expect(item.serial_no_data).toEqual([
			{ serial_no: "S-CACHED", batch_no: "B-CACHED" },
		]);
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();
	});

	it("hydrates reconstructed buying-floor fields on cached update_items_details and refreshPricesForVisibleItems and clears stale floors on unresolved FX", async () => {
		const cachedDetail = {
			item_code: "ITEM-FLOOR-1",
			actual_qty: 6,
			rate: 10.5,
			price_list_rate: 10.5,
			currency: "USD",
			price_list_currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			plc_conversion_rate: 3700,
			buying_price_list: "USD Buying",
			buying_price_currency: "USD",
			buying_rate_currency: "USD",
			buying_exchange_rate: 4050,
			buying_price_not_uom_dependent: 1,
			base_buying_rate: 40500,
			trade_price: 10.9459459459,
			buying_rate: 10.9459459459,
			_source_buying_currency: "USD",
			_source_buying_price: 10,
			_company_buying_price: 40500,
			valuation_rate: 39000,
			warehouse_valuation_rate: 39500,
			_buying_prices_by_uom: {
				Nos: {
					price_list_rate: 10,
					base_price_list_rate: 40500,
					currency: "USD",
					price_list: "USD Buying",
					price_not_uom_dependent: 1,
				},
			},
			item_uoms: [{ uom: "Nos", conversion_factor: 1 }],
		};
		getCachedItemDetails.mockResolvedValueOnce({
			cached: [cachedDetail],
			missing: [],
		});

		const fetcher = useItemDetailFetcher();
		fetcher.registerContext({
			pos_profile: { name: "POS-USD", currency: "USD" },
			active_price_list: "USD Selling",
			itemAvailability: null,
			applyCurrencyConversionToItem: vi.fn(),
			items: [],
			displayedItems: [],
			usesLimitSearch: false,
			storageAvailable: false,
		});

		const cartItem: any = { item_code: "ITEM-FLOOR-1", stock_uom: "Nos" };
		await fetcher.update_items_details([cartItem]);

		expect(cartItem._buying_prices_by_uom).toEqual(
			cachedDetail._buying_prices_by_uom,
		);
		expect(cartItem.base_buying_rate).toBe(40500);
		expect(cartItem.trade_price).toBeCloseTo(10.9459459459);
		expect(cartItem.buying_rate).toBeCloseTo(10.9459459459);
		expect(cartItem.buying_price_list).toBe("USD Buying");
		expect(cartItem.buying_price_currency).toBe("USD");
		expect(cartItem.buying_rate_currency).toBe("USD");
		expect(cartItem.buying_exchange_rate).toBe(4050);
		expect(cartItem.buying_price_not_uom_dependent).toBe(1);
		expect(cartItem._source_buying_currency).toBe("USD");
		expect(cartItem._source_buying_price).toBe(10);
		expect(cartItem._company_buying_price).toBe(40500);
		expect(cartItem.company_currency).toBe("UGX");
		expect(cartItem.valuation_rate).toBe(39000);
		expect(cartItem.warehouse_valuation_rate).toBe(39500);

		// Subsequent offline refresh with unresolved FX must clear stale _company_buying_price
		// and propagate unresolved_fx + buying_exchange_rate: 0 + base_buying_rate: null.
		getCachedItemDetails.mockResolvedValueOnce({
			cached: [
				{
					item_code: "ITEM-FLOOR-1",
					actual_qty: 6,
					rate: 10.5,
					price_list_rate: 10.5,
					currency: "USD",
					company_currency: "UGX",
					buying_price_list: "USD Buying",
					buying_price_currency: "USD",
					buying_exchange_rate: 0,
					base_buying_rate: null,
					_buying_prices_by_uom: {
						Nos: {
							price_list_rate: 10,
							base_price_list_rate: null,
							currency: "USD",
							price_list: "USD Buying",
							price_not_uom_dependent: 1,
							unresolved_fx: true,
						},
					},
					item_uoms: [{ uom: "Nos", conversion_factor: 1 }],
				},
			],
			missing: [],
		});
		await fetcher.update_items_details([cartItem]);
		expect(cartItem._buying_prices_by_uom.Nos.unresolved_fx).toBe(true);
		expect(cartItem.buying_exchange_rate).toBe(0);
		expect(cartItem.base_buying_rate).toBeNull();
		expect(cartItem._company_buying_price).toBeUndefined();

		// refreshPricesForVisibleItems must also copy buying-floor fields on cached refresh.
		const visibleItem: any = { item_code: "ITEM-FLOOR-1", stock_uom: "Nos" };
		getCachedItemDetails.mockResolvedValueOnce({
			cached: [cachedDetail],
			missing: [],
		});
		fetcher.registerContext({
			pos_profile: { name: "POS-USD", currency: "USD" },
			active_price_list: "USD Selling",
			itemAvailability: null,
			displayedItems: [visibleItem],
			usesLimitSearch: false,
			storageAvailable: false,
		});
		await fetcher.refreshPricesForVisibleItems();
		expect(visibleItem._buying_prices_by_uom).toEqual(
			cachedDetail._buying_prices_by_uom,
		);
		expect(visibleItem.base_buying_rate).toBe(40500);
		expect(visibleItem.company_currency).toBe("UGX");
	});

	it("hydrates buying-floor fields from server fetch in update_items_details and refreshPricesForVisibleItems", async () => {
		const serverRow = {
			item_code: "ITEM-1",
			actual_qty: 5,
			rate: 10.5,
			price_list_rate: 10.5,
			currency: "USD",
			price_list_currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			plc_conversion_rate: 3700,
			buying_price_list: "USD Buying",
			buying_price_currency: "USD",
			buying_rate_currency: null,
			buying_exchange_rate: null,
			base_buying_rate: null,
			trade_price: null,
			buying_rate: null,
			_buying_prices_by_uom: {
				Nos: {
					price_list_rate: 10,
					base_price_list_rate: null,
					currency: "USD",
					price_list: "USD Buying",
					price_not_uom_dependent: 0,
				},
			},
			item_uoms: [{ uom: "Nos", conversion_factor: 1 }],
		};
		(globalThis as any).frappe.call.mockResolvedValue({
			message: [serverRow],
		});

		const fetcher = useItemDetailFetcher();
		const item: any = {
			item_code: "ITEM-1",
			stock_uom: "Nos",
			_company_buying_price: 40500,
			base_buying_rate: 40500,
		};
		fetcher.registerContext({
			pos_profile: { name: "POS-USD", currency: "USD" },
			active_price_list: "USD Selling",
			itemAvailability: null,
			applyCurrencyConversionToItem: vi.fn(),
			items: [item],
			displayedItems: [item],
			usesLimitSearch: false,
			storageAvailable: false,
		});

		await fetcher.update_items_details([item], { forceRefresh: true });
		expect(item._buying_prices_by_uom).toEqual(serverRow._buying_prices_by_uom);
		expect(item.buying_exchange_rate).toBeNull();
		expect(item.base_buying_rate).toBeNull();
		expect(item.buying_price_currency).toBe("USD");
		expect(item.company_currency).toBe("UGX");
		expect(item._company_buying_price).toBeUndefined();

		const visibleItem: any = { item_code: "ITEM-1", stock_uom: "Nos" };
		fetcher.registerContext({
			pos_profile: { name: "POS-USD", currency: "USD" },
			active_price_list: "USD Selling",
			itemAvailability: null,
			displayedItems: [visibleItem],
			usesLimitSearch: false,
			storageAvailable: false,
		});
		await fetcher.refreshPricesForVisibleItems();
		expect(visibleItem._buying_prices_by_uom).toEqual(
			serverRow._buying_prices_by_uom,
		);
		expect(visibleItem.buying_exchange_rate).toBeNull();
		expect(visibleItem.company_currency).toBe("UGX");
	});
});
