// @vitest-environment jsdom

import "fake-indexeddb/auto";

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { db } from "../src/offline/db";
import {
	getCachedExchangeRate,
	getCachedItemDetails,
	saveExchangeRateCache,
	saveItemDetailsCache,
	setProfileBuyingPriceList,
} from "../src/offline/cache";
import {
	getItemCostFloor,
	getItemLossRisk,
} from "../src/posapp/utils/lossPrevention";
import { useItemDetailFetcher } from "../src/posapp/composables/pos/items/useItemDetailFetcher";
import { useItemCurrency } from "../src/posapp/composables/pos/items/useItemCurrency";
import { syncCurrencyMatrixResource } from "../src/offline/sync/adapters/currencyMatrix";

async function clearOfflineDb() {
	if (!db.isOpen()) {
		await db.open();
	}
	for (const table of db.tables) {
		await table.clear();
	}
}

describe("offline buying floor cache reconstruction", () => {
	beforeEach(clearOfflineDb);
	afterEach(clearOfflineDb);

	it("uses for_buying FX (and not newer selling-only FX) when reconstructing offline buying floors", async () => {
		const profile = {
			name: "POS-UGX-USD",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("items").put({
			item_code: "ITEM-FX-CACHE",
			item_name: "Offline FX Item",
			stock_uom: "Nos",
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-BUY-1",
				price_list: "USD Buying",
				item_code: "ITEM-FX-CACHE",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		// Older buying rate (4,050) vs newer selling-only rate (3,700):
		// offline buying floor MUST stay UGX 40,500 so USD 10.50 (UGX 38,850) blocks.
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-BUY-OLD",
				profile_name: "POS-UGX-USD",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2025-04-01",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-04-01T10:00:00",
			},
			{
				name: "FX-SELL-NEW",
				profile_name: "POS-UGX-USD",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3700,
				date: "2025-04-10",
				for_buying: 0,
				for_selling: 1,
				modified: "2025-04-10T10:00:00",
			},
		]);

		saveItemDetailsCache("POS-UGX-USD", "USD Selling", [
			{
				item_code: "ITEM-FX-CACHE",
				stock_uom: "Nos",
				rate: 10.5,
				price_list_rate: 10.5,
				company_currency: "UGX",
				buying_price_list: "USD Buying",
				buying_price_currency: "USD",
				buying_exchange_rate: 4050,
				base_buying_rate: 40500,
				_buying_prices_by_uom: {
					Nos: {
						price_list_rate: 10,
						base_price_list_rate: 40500,
						currency: "USD",
						price_list: "USD Buying",
					},
				},
			},
		]);

		const { cached } = await getCachedItemDetails(
			"POS-UGX-USD",
			"USD Selling",
			["ITEM-FX-CACHE"],
		);
		expect(cached).toHaveLength(1);
		const item = {
			...cached[0],
			qty: 1,
			rate: 10.5,
			uom: "Nos",
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
		};
		expect(
			item._buying_prices_by_uom.Nos.base_price_list_rate,
		).toBeCloseTo(40500);
		const risk = getItemLossRisk(item, { currencyPrecision: 2 });
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBeCloseTo((10 * 4050) / 3700, 4);
	});

	it("preserves price_not_uom_dependent when reconstructing _buying_prices_by_uom from offline cache", async () => {
		const profile = {
			name: "POS-PNUD",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "Flat USD Buying",
		};
		await setProfileBuyingPriceList(profile, "Flat USD Buying");
		await db.table("items").put({
			item_code: "ITEM-PNUD-BOX",
			item_name: "Flat Buying Box",
			stock_uom: "Each",
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-PNUD-1",
				price_list: "Flat USD Buying",
				item_code: "ITEM-PNUD-BOX",
				uom: "Each",
				currency: "USD",
				price_list_rate: 10,
				price_not_uom_dependent: 1,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-PNUD-BUY",
				profile_name: "POS-PNUD",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2025-04-01",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-04-01T10:00:00",
			},
		]);

		saveItemDetailsCache("POS-PNUD", "USD Selling", [
			{
				item_code: "ITEM-PNUD-BOX",
				stock_uom: "Each",
				rate: 12,
				price_list_rate: 12,
				company_currency: "UGX",
				buying_price_list: "Flat USD Buying",
				buying_price_currency: "USD",
				buying_exchange_rate: 4050,
				base_buying_rate: 40500,
				_buying_prices_by_uom: {
					Each: {
						price_list_rate: 10,
						base_price_list_rate: 40500,
						currency: "USD",
						price_list: "Flat USD Buying",
						price_not_uom_dependent: 1,
					},
				},
			},
		]);

		const { cached } = await getCachedItemDetails("POS-PNUD", "USD Selling", [
			"ITEM-PNUD-BOX",
		]);
		expect(cached).toHaveLength(1);
		expect(
			cached[0]._buying_prices_by_uom.Each.price_not_uom_dependent,
		).toBe(1);

		const boxItem = {
			...cached[0],
			qty: 1,
			rate: 12,
			uom: "Box",
			stock_uom: "Each",
			conversion_factor: 10,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
		};
		const floor = getItemCostFloor(boxItem);
		expect(floor).not.toBeNull();
		expect(floor?.baseCurrency).toBe(true);
		// Must stay UGX 40,500 (not scaled 10x to UGX 405,000).
		expect(floor?.rate).toBeCloseTo(40500);
	});

	it("preserves name desc tie-breaking on qualified for_buying rates and cached price_not_uom_dependent when offline rows omit newer fields", async () => {
		const profile = {
			name: "POS-LEGACY",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "Legacy Flat Buying",
		};
		await setProfileBuyingPriceList(profile, "Legacy Flat Buying");
		await db.table("items").put({
			item_code: "ITEM-LEGACY",
			item_name: "Legacy Item",
			stock_uom: "Each",
		});
		// Legacy Item Price row without price_not_uom_dependent field.
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-LEGACY-1",
				price_list: "Legacy Flat Buying",
				item_code: "ITEM-LEGACY",
				uom: "Each",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		// Same-day qualified for_buying rows tie-break by name desc (ERPNext setup/utils.py:77).
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-LEG-001",
				profile_name: "POS-LEGACY",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3900,
				date: "2025-04-01",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-04-01T11:00:00",
			},
			{
				name: "FX-LEG-002",
				profile_name: "POS-LEGACY",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2025-04-01",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-04-01T10:00:00",
			},
		]);

		saveItemDetailsCache("POS-LEGACY", "USD Selling", [
			{
				item_code: "ITEM-LEGACY",
				stock_uom: "Each",
				rate: 12,
				price_list_rate: 12,
				company_currency: "UGX",
				buying_price_list: "Legacy Flat Buying",
				buying_price_currency: "USD",
				buying_exchange_rate: 4050,
				base_buying_rate: 40500,
				_buying_prices_by_uom: {
					Each: {
						price_list_rate: 10,
						base_price_list_rate: 40500,
						currency: "USD",
						price_list: "Legacy Flat Buying",
						price_not_uom_dependent: 1,
					},
				},
			},
		]);

		const { cached } = await getCachedItemDetails("POS-LEGACY", "USD Selling", [
			"ITEM-LEGACY",
		]);
		expect(cached).toHaveLength(1);
		// Picks FX-LEG-002 (4050) by name desc on same date despite FX-LEG-001 having newer modified.
		expect(
			cached[0]._buying_prices_by_uom.Each.base_price_list_rate,
		).toBeCloseTo(40500);
		// Preserves price_not_uom_dependent: 1 from cached item when offline row omits it.
		expect(
			cached[0]._buying_prices_by_uom.Each.price_not_uom_dependent,
		).toBe(1);
	});

	it("preserves verified cached base floor when fixed foreign profile lacks company_currency instead of defaulting to profile.currency", async () => {
		// USD fixed-currency POS Profile in UGX company, but company_currency is absent on both profile and cached item.
		const profile = {
			name: "POS-USD-NO-CC",
			company: "Test Co",
			currency: "USD",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("items").put({
			item_code: "ITEM-NO-CC",
			item_name: "USD No Company Currency Item",
			stock_uom: "Nos",
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-NO-CC-1",
				price_list: "USD Buying",
				item_code: "ITEM-NO-CC",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);

		saveItemDetailsCache("POS-USD-NO-CC", "USD Selling", [
			{
				item_code: "ITEM-NO-CC",
				stock_uom: "Nos",
				rate: 10.5,
				price_list_rate: 10.5,
				buying_price_list: "USD Buying",
				buying_price_currency: "USD",
				buying_exchange_rate: 4050,
				base_buying_rate: 40500,
				_buying_prices_by_uom: {
					Nos: {
						price_list_rate: 10,
						base_price_list_rate: 40500,
						currency: "USD",
						price_list: "USD Buying",
					},
				},
			},
		]);

		const { cached } = await getCachedItemDetails(
			"POS-USD-NO-CC",
			"USD Selling",
			["ITEM-NO-CC"],
		);
		expect(cached).toHaveLength(1);
		// Must preserve UGX 40,500 base floor (never overwrite with 10 by treating USD as company_currency).
		expect(
			cached[0]._buying_prices_by_uom.Nos.base_price_list_rate,
		).toBeCloseTo(40500);
	});

	it("fails closed when offline for_buying FX is missing instead of falling through to warehouse valuation", async () => {
		const profile = {
			name: "POS-MISSING-BUY-FX",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("items").put({
			item_code: "ITEM-NO-BUY-FX",
			item_name: "Missing Buying FX Item",
			stock_uom: "Nos",
			valuation_rate: 100,
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-NO-BUY-FX",
				price_list: "USD Buying",
				item_code: "ITEM-NO-BUY-FX",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		// Only a selling-only FX record exists — no valid for_buying FX!
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-SELL-ONLY",
				profile_name: "POS-MISSING-BUY-FX",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3700,
				date: "2025-04-10",
				for_buying: 0,
				for_selling: 1,
				modified: "2025-04-10T10:00:00",
			},
		]);

		saveItemDetailsCache("POS-MISSING-BUY-FX", "USD Selling", [
			{
				item_code: "ITEM-NO-BUY-FX",
				stock_uom: "Nos",
				rate: 10.5,
				price_list_rate: 10.5,
				company_currency: "UGX",
			},
		]);

		const { cached } = await getCachedItemDetails(
			"POS-MISSING-BUY-FX",
			"USD Selling",
			["ITEM-NO-BUY-FX"],
		);
		expect(cached).toHaveLength(1);
		const item = {
			...cached[0],
			qty: 1,
			rate: 10.5,
			uom: "Nos",
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			valuation_rate: 100,
		};
		// Must return null (triggering missing-cost policy), NEVER fall through to UGX 100 valuation_rate.
		expect(getItemCostFloor(item)).toBeNull();
	});

	it("applies updated Price List price_not_uom_dependent=0 over stale cached Item Price row flag=1", async () => {
		const profile = {
			name: "POS-PNUD-FLIP",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "Flipped Buying",
		};
		// Delta sync updates Price List flag from 1 -> 0 while unchanged Item Price row retains stale 1.
		await setProfileBuyingPriceList(profile, "Flipped Buying", 0);
		await db.table("items").put({
			item_code: "ITEM-PNUD-FLIP",
			item_name: "Flipped UOM Flag Item",
			stock_uom: "Each",
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-PNUD-STALE",
				price_list: "Flipped Buying",
				item_code: "ITEM-PNUD-FLIP",
				uom: "Each",
				currency: "USD",
				price_list_rate: 10,
				price_not_uom_dependent: 1,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-PNUD-FLIP",
				profile_name: "POS-PNUD-FLIP",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2025-04-01",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-04-01T10:00:00",
			},
		]);

		saveItemDetailsCache("POS-PNUD-FLIP", "USD Selling", [
			{
				item_code: "ITEM-PNUD-FLIP",
				stock_uom: "Each",
				rate: 120,
				price_list_rate: 120,
				company_currency: "UGX",
			},
		]);

		const { cached } = await getCachedItemDetails(
			"POS-PNUD-FLIP",
			"USD Selling",
			["ITEM-PNUD-FLIP"],
		);
		expect(cached).toHaveLength(1);
		expect(
			cached[0]._buying_prices_by_uom.Each.price_not_uom_dependent,
		).toBe(0);

		const boxFloor = getItemCostFloor({
			...cached[0],
			qty: 1,
			rate: 50,
			uom: "Box",
			stock_uom: "Each",
			conversion_factor: 10,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
		});
		expect(boxFloor).not.toBeNull();
		// Box (10 Each) must scale 10x to UGX 405,000 (not stay UGX 40,500).
		expect(boxFloor?.rate).toBeCloseTo(405000);
	});

	it("rejects stale offline buying FX when Accounts Settings disables allow_stale", async () => {
		const profile = {
			name: "POS-STALE-FX",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("settings").put({
			key: "currency_exchange_settings",
			value: { allow_stale: 0, stale_days: 30 },
		});
		await db.table("items").put({
			item_code: "ITEM-STALE-FX",
			item_name: "Stale FX Item",
			stock_uom: "Nos",
			valuation_rate: 100,
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-STALE-FX",
				price_list: "USD Buying",
				item_code: "ITEM-STALE-FX",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2024-01-01",
				modified: "2025-01-15T10:00:00",
			},
		]);
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-JAN-2025",
				profile_name: "POS-STALE-FX",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2025-01-15",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-01-15T10:00:00",
			},
		]);

		saveItemDetailsCache("POS-STALE-FX", "USD Selling", [
			{
				item_code: "ITEM-STALE-FX",
				stock_uom: "Nos",
				rate: 12,
				price_list_rate: 12,
				company_currency: "UGX",
			},
		]);

		const { cached } = await getCachedItemDetails(
			"POS-STALE-FX",
			"USD Selling",
			["ITEM-STALE-FX"],
		);
		expect(cached).toHaveLength(1);
		// Jan 2025 buying rate is stale under allow_stale=0, stale_days=30 -> must fail closed (null floor).
		expect(
			getItemCostFloor({
				...cached[0],
				qty: 1,
				rate: 12,
				uom: "Nos",
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 100,
			}),
		).toBeNull();
	});

	it("keys warm exchange rate cache by purpose and does not return unqualified legacy entry for explicit purpose", () => {
		saveExchangeRateCache({
			profileName: "POS-FX-KEY",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-08-20",
			exchange_rate: 3700,
		});
		// Unqualified lookup still works for backwards compatibility.
		expect(
			getCachedExchangeRate({
				profileName: "POS-FX-KEY",
				company: "Test Co",
				fromCurrency: "USD",
				toCurrency: "UGX",
				rateDate: "2026-08-20",
			})?.exchange_rate,
		).toBe(3700);
		// Explicit for_buying lookup MUST NOT return the unqualified 3,700 entry.
		expect(
			getCachedExchangeRate({
				profileName: "POS-FX-KEY",
				company: "Test Co",
				fromCurrency: "USD",
				toCurrency: "UGX",
				rateDate: "2026-08-20",
				purpose: "for_buying",
			}),
		).toBeNull();

		saveExchangeRateCache({
			profileName: "POS-FX-KEY",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-08-20",
			purpose: "for_buying",
			exchange_rate: 4050,
		});
		expect(
			getCachedExchangeRate({
				profileName: "POS-FX-KEY",
				company: "Test Co",
				fromCurrency: "USD",
				toCurrency: "UGX",
				rateDate: "2026-08-20",
				purpose: "for_buying",
			})?.exchange_rate,
		).toBe(4050);
	});

	it("propagates reconstructed offline buying floors end-to-end through update_items_details and useItemCurrency into getItemLossRisk", async () => {
		const profile = {
			name: "POS-E2E-OFFLINE",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("items").put({
			item_code: "ITEM-E2E-FLOOR",
			item_name: "Offline E2E Floor Item",
			stock_uom: "Nos",
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-E2E-BUY",
				price_list: "USD Buying",
				item_code: "ITEM-E2E-FLOOR",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-E2E-BUY",
				profile_name: "POS-E2E-OFFLINE",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2025-04-01",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-04-01T10:00:00",
			},
		]);
		saveItemDetailsCache("POS-E2E-OFFLINE", "USD Selling", [
			{
				item_code: "ITEM-E2E-FLOOR",
				stock_uom: "Nos",
				actual_qty: 10,
				rate: 10.5,
				price_list_rate: 10.5,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				item_uoms: [{ uom: "Nos", conversion_factor: 1 }],
			},
		]);

		const { applyCurrencyConversionToItem } = useItemCurrency();
		const fetcher = useItemDetailFetcher();
		fetcher.registerContext({
			pos_profile: profile,
			active_price_list: "USD Selling",
			itemAvailability: null,
			applyCurrencyConversionToItem: (it: any) =>
				applyCurrencyConversionToItem(it, {
					pos_profile: profile,
					price_list_currency: it?.original_currency || "USD",
					selected_currency: "USD",
					exchange_rate: 1,
					conversion_rate: 3700,
					currency_precision: 2,
				}),
			items: [],
			displayedItems: [],
			usesLimitSearch: false,
			storageAvailable: false,
		});

		const item: any = {
			item_code: "ITEM-E2E-FLOOR",
			stock_uom: "Nos",
			uom: "Nos",
			qty: 1,
			rate: 10.5,
			currency: "USD",
		};
		await fetcher.update_items_details([item]);

		expect(item.company_currency).toBe("UGX");
		expect(item.conversion_rate).toBe(3700);
		expect(item._buying_prices_by_uom?.Nos?.base_price_list_rate).toBeCloseTo(
			40500,
		);
		const risk = getItemLossRisk(item, { currencyPrecision: 2 });
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBeCloseTo((10 * 4050) / 3700, 4);
	});

	it("rejects legacy unqualified currency_rate_records in offline buying-floor reconstruction", async () => {
		const profile = {
			name: "POS-LEGACY-REJECT",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("items").put({
			item_code: "ITEM-LEGACY-REJECT",
			item_name: "Legacy Unqualified FX Item",
			stock_uom: "Nos",
			valuation_rate: 100,
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-LEGACY-REJECT",
				price_list: "USD Buying",
				item_code: "ITEM-LEGACY-REJECT",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		// Newer unqualified legacy row (3,700) must NEVER override an older verified for_buying=1 row (4,050).
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-BUY-VERIFIED",
				profile_name: "POS-LEGACY-REJECT",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2025-04-01",
				for_buying: 1,
				for_selling: 0,
				modified: "2025-04-01T10:00:00",
			},
			{
				name: "FX-LEGACY-UNQUALIFIED",
				profile_name: "POS-LEGACY-REJECT",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3700,
				date: "2025-04-10",
				modified: "2025-04-10T10:00:00",
			},
		]);
		saveItemDetailsCache("POS-LEGACY-REJECT", "USD Selling", [
			{
				item_code: "ITEM-LEGACY-REJECT",
				stock_uom: "Nos",
				rate: 10.5,
				price_list_rate: 10.5,
				company_currency: "UGX",
			},
		]);

		const withVerified = await getCachedItemDetails(
			"POS-LEGACY-REJECT",
			"USD Selling",
			["ITEM-LEGACY-REJECT"],
		);
		expect(
			withVerified.cached[0]._buying_prices_by_uom.Nos.base_price_list_rate,
		).toBeCloseTo(40500);

		// When ONLY the unqualified legacy row exists, reconstruction must fail closed (unresolved_fx).
		await db.table("currency_rate_records").delete("FX-BUY-VERIFIED");
		const onlyLegacy = await getCachedItemDetails(
			"POS-LEGACY-REJECT",
			"USD Selling",
			["ITEM-LEGACY-REJECT"],
		);
		expect(
			onlyLegacy.cached[0]._buying_prices_by_uom.Nos.unresolved_fx,
		).toBe(true);
		expect(
			getItemCostFloor({
				...onlyLegacy.cached[0],
				qty: 1,
				rate: 10.5,
				uom: "Nos",
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 100,
			}),
		).toBeNull();
	});

	it("preserves verified server-computed base buying floor on a fresh till before currency_rate_records are synced, but fails closed if price changes", async () => {
		const profile = {
			name: "POS-FRESH-TILL",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("items").put({
			item_code: "ITEM-FRESH-FLOOR",
			item_name: "Fresh Till Floor Item",
			stock_uom: "Nos",
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-FRESH-1",
				price_list: "USD Buying",
				item_code: "ITEM-FRESH-FLOOR",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		// Note: currency_rate_records is intentionally EMPTY (fresh till before currency sync).
		saveItemDetailsCache("POS-FRESH-TILL", "USD Selling", [
			{
				item_code: "ITEM-FRESH-FLOOR",
				stock_uom: "Nos",
				rate: 10.5,
				price_list_rate: 10.5,
				company_currency: "UGX",
				buying_price_list: "USD Buying",
				buying_price_currency: "USD",
				buying_exchange_rate: 4050,
				base_buying_rate: 40500,
				_buying_prices_by_uom: {
					Nos: {
						price_list_rate: 10,
						base_price_list_rate: 40500,
						currency: "USD",
						price_list: "USD Buying",
						price_not_uom_dependent: 0,
					},
				},
			},
		]);

		const fresh = await getCachedItemDetails(
			"POS-FRESH-TILL",
			"USD Selling",
			["ITEM-FRESH-FLOOR"],
		);
		expect(fresh.cached).toHaveLength(1);
		expect(
			fresh.cached[0]._buying_prices_by_uom.Nos.base_price_list_rate,
		).toBeCloseTo(40500);
		expect(
			fresh.cached[0]._buying_prices_by_uom.Nos.unresolved_fx,
		).toBeUndefined();
		expect(fresh.cached[0].buying_exchange_rate).toBe(4050);

		// If offline item_price_records changes the buying price (10 -> 12) while currency rates are absent,
		// fail closed rather than reusing the stale 40,500 server floor for the new USD 12 price.
		await db.table("item_price_records").put({
			name: "IP-FRESH-1",
			price_list: "USD Buying",
			item_code: "ITEM-FRESH-FLOOR",
			uom: "Nos",
			currency: "USD",
			price_list_rate: 12,
			buying: 1,
			selling: 0,
			valid_from: "2025-01-01",
			modified: "2025-04-02T10:00:00",
		});
		const changedPrice = await getCachedItemDetails(
			"POS-FRESH-TILL",
			"USD Selling",
			["ITEM-FRESH-FLOOR"],
		);
		expect(
			changedPrice.cached[0]._buying_prices_by_uom.Nos.unresolved_fx,
		).toBe(true);
	});

	it("resolves offline buying floors via ERPNext inverse FX fallback using opposite purpose (for_selling) and rejects wrong-purpose or stale inverse", async () => {
		const profile = {
			name: "POS-INVERSE-FX",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("settings").put({
			key: "currency_exchange_settings",
			value: { allow_stale: 0, stale_days: 30 },
		});
		await db.table("items").put({
			item_code: "ITEM-INV-FX",
			item_name: "Inverse FX Item",
			stock_uom: "Nos",
			valuation_rate: 100,
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-INV-1",
				price_list: "USD Buying",
				item_code: "ITEM-INV-FX",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		const today = new Date().toISOString().slice(0, 10);
		// No direct USD->UGX row; only reverse UGX->USD with for_selling=1 (ERPNext setup/utils.py:128-134).
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-UGX-USD-SELL",
				profile_name: "POS-INVERSE-FX",
				company: "Test Co",
				from_currency: "UGX",
				to_currency: "USD",
				exchange_rate: 1 / 4050,
				date: today,
				for_buying: 0,
				for_selling: 1,
				modified: `${today}T10:00:00`,
			},
		]);
		saveItemDetailsCache("POS-INVERSE-FX", "USD Selling", [
			{
				item_code: "ITEM-INV-FX",
				stock_uom: "Nos",
				rate: 10.5,
				price_list_rate: 10.5,
				company_currency: "UGX",
			},
		]);

		const resolved = await getCachedItemDetails(
			"POS-INVERSE-FX",
			"USD Selling",
			["ITEM-INV-FX"],
		);
		expect(
			resolved.cached[0]._buying_prices_by_uom.Nos.base_price_list_rate,
		).toBeCloseTo(40500, 2);

		// Reverse UGX->USD with wrong purpose (for_buying=1, for_selling=0) must fail closed.
		await db.table("currency_rate_records").clear();
		await db.table("currency_rate_records").put({
			name: "FX-UGX-USD-WRONG",
			profile_name: "POS-INVERSE-FX",
			company: "Test Co",
			from_currency: "UGX",
			to_currency: "USD",
			exchange_rate: 1 / 4050,
			date: today,
			for_buying: 1,
			for_selling: 0,
			modified: `${today}T10:00:00`,
		});
		const wrongPurpose = await getCachedItemDetails(
			"POS-INVERSE-FX",
			"USD Selling",
			["ITEM-INV-FX"],
		);
		expect(
			wrongPurpose.cached[0]._buying_prices_by_uom.Nos.unresolved_fx,
		).toBe(true);
		expect(
			getItemCostFloor({
				...wrongPurpose.cached[0],
				qty: 1,
				rate: 10.5,
				uom: "Nos",
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 100,
			}),
		).toBeNull();

		// Stale reverse UGX->USD (for_selling=1, Jan 2025 under allow_stale=0, stale_days=30) must fail closed.
		await db.table("currency_rate_records").clear();
		await db.table("currency_rate_records").put({
			name: "FX-UGX-USD-STALE",
			profile_name: "POS-INVERSE-FX",
			company: "Test Co",
			from_currency: "UGX",
			to_currency: "USD",
			exchange_rate: 1 / 4050,
			date: "2025-01-15",
			for_buying: 0,
			for_selling: 1,
			modified: "2025-01-15T10:00:00",
		});
		const staleInverse = await getCachedItemDetails(
			"POS-INVERSE-FX",
			"USD Selling",
			["ITEM-INV-FX"],
		);
		expect(
			staleInverse.cached[0]._buying_prices_by_uom.Nos.unresolved_fx,
		).toBe(true);
	});

	it("emits unresolved_fx and clears stale buying floor after currency sync acknowledges deletion of the last FX rate", async () => {
		const profile = {
			name: "POS-DELETED-FX",
			company: "Test Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			buying_price_list: "USD Buying",
		};
		await setProfileBuyingPriceList(profile, "USD Buying");
		await db.table("items").put({
			item_code: "ITEM-DEL-FX",
			item_name: "Deleted FX Item",
			stock_uom: "Nos",
			valuation_rate: 100,
		});
		await db.table("item_price_records").bulkPut([
			{
				name: "IP-DEL-1",
				price_list: "USD Buying",
				item_code: "ITEM-DEL-FX",
				uom: "Nos",
				currency: "USD",
				price_list_rate: 10,
				buying: 1,
				selling: 0,
				valid_from: "2025-01-01",
				modified: "2025-04-01T10:00:00",
			},
		]);
		saveItemDetailsCache("POS-DELETED-FX", "USD Selling", [
			{
				item_code: "ITEM-DEL-FX",
				stock_uom: "Nos",
				rate: 10.5,
				price_list_rate: 10.5,
				company_currency: "UGX",
				buying_price_list: "USD Buying",
				buying_price_currency: "USD",
				buying_exchange_rate: 4050,
				base_buying_rate: 40500,
				trade_price: 40500 / 3700,
				buying_rate: 40500 / 3700,
				_company_buying_price: 40500,
				_buying_prices_by_uom: {
					Nos: {
						price_list_rate: 10,
						base_price_list_rate: 40500,
						currency: "USD",
						price_list: "USD Buying",
						price_not_uom_dependent: 0,
					},
				},
			},
		]);

		// Step 1: Initial currency_matrix sync populates the USD->UGX buying rate.
		await syncCurrencyMatrixResource({
			posProfile: profile,
			currencyPairs: [
				{ from_currency: "USD", to_currency: "UGX" },
				{ from_currency: "UGX", to_currency: "USD" },
			],
			watermark: null,
			fetcher: async () => ({
				schema_version: "2026-04-09",
				next_watermark: "2026-04-09T10:00:00",
				changes: [
					{
						key: "currency_rate::FX-USD-UGX-LAST",
						modified: "2026-04-09T10:00:00",
						data: {
							name: "FX-USD-UGX-LAST",
							from_currency: "USD",
							to_currency: "UGX",
							exchange_rate: 4050,
							date: "2026-04-09",
							for_buying: 1,
							for_selling: 0,
							modified: "2026-04-09T10:00:00",
						},
					},
				],
				deleted: [],
				has_more: false,
			}),
		});

		// Step 2: Delta sync acknowledges deletion of that last USD->UGX buying rate -> 0 rate records remain.
		await syncCurrencyMatrixResource({
			posProfile: profile,
			currencyPairs: [
				{ from_currency: "USD", to_currency: "UGX" },
				{ from_currency: "UGX", to_currency: "USD" },
			],
			watermark: "2026-04-09T10:00:00",
			fetcher: async () => ({
				schema_version: "2026-04-09",
				next_watermark: "2026-04-09T10:05:00",
				changes: [],
				deleted: [{ key: "currency_rate::FX-USD-UGX-LAST" }],
				has_more: false,
			}),
		});

		const remainingRates = await db.table("currency_rate_records").toArray();
		expect(remainingRates).toHaveLength(0);

		// Step 3: getCachedItemDetails must NOT fall back to the stale 40,500 server floor;
		// it must mark unresolved_fx: true and clear stale buying floor fields.
		const afterDelete = await getCachedItemDetails(
			"POS-DELETED-FX",
			"USD Selling",
			["ITEM-DEL-FX"],
		);
		expect(afterDelete.cached).toHaveLength(1);
		expect(
			afterDelete.cached[0]._buying_prices_by_uom.Nos.unresolved_fx,
		).toBe(true);
		expect(
			afterDelete.cached[0]._buying_prices_by_uom.Nos.base_price_list_rate,
		).toBeNull();
		expect(afterDelete.cached[0].base_buying_rate).toBeNull();
		expect(afterDelete.cached[0].buying_exchange_rate).toBe(0);
		expect(afterDelete.cached[0]._company_buying_price).toBeUndefined();
		expect(
			getItemCostFloor({
				...afterDelete.cached[0],
				qty: 1,
				rate: 10.5,
				uom: "Nos",
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 100,
			}),
		).toBeNull();
	});
});
