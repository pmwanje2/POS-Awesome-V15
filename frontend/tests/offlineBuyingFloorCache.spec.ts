// @vitest-environment jsdom

import "fake-indexeddb/auto";

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { db } from "../src/offline/db";
import {
	getCachedItemDetails,
	saveItemDetailsCache,
	setProfileBuyingPriceList,
} from "../src/offline/cache";
import {
	getItemCostFloor,
	getItemLossRisk,
} from "../src/posapp/utils/lossPrevention";

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

	it("preserves legacy currency rate compatibility and cached price_not_uom_dependent when offline rows omit newer fields", async () => {
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
		// Same-day legacy Currency Exchange rows without for_buying/for_selling flags:
		// must remain usable and tie-break by name desc (ERPNext setup/utils.py:77).
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-LEG-001",
				profile_name: "POS-LEGACY",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3900,
				date: "2025-04-01",
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
});
