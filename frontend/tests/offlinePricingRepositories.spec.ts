// @vitest-environment jsdom

import "fake-indexeddb/auto";

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { db } from "../src/offline/db";
import {
	currencyRateRepository,
	itemPriceRepository,
	pricingRuleRepository,
} from "../src/offline/repositories";

async function clearOfflineDb() {
	if (!db.isOpen()) {
		await db.open();
	}
	for (const table of db.tables) {
		await table.clear();
	}
}

describe("offline pricing repositories", () => {
	beforeEach(clearOfflineDb);
	afterEach(clearOfflineDb);

	it("stores distinct UOM, currency, and customer Item Prices", async () => {
		await itemPriceRepository.upsertMany([
			{
				name: "IP-NOS",
				price_list: "Retail",
				item_code: "ITEM-001",
				uom: "Nos",
				currency: "PKR",
				customer: null,
				price_list_rate: 100,
				valid_from: null,
				valid_upto: null,
				modified: "2026-06-01T10:00:00",
			},
			{
				name: "IP-BOX-CUSTOMER",
				price_list: "Retail",
				item_code: "ITEM-001",
				uom: "Box",
				currency: "USD",
				customer: "CUST-001",
				price_list_rate: 15,
				valid_from: "2026-01-01",
				valid_upto: "2026-12-31",
				modified: "2026-06-01T10:01:00",
			},
		]);

		const rows = await itemPriceRepository.findForItemAndUom(
			"Retail",
			"ITEM-001",
			"Box",
		);

		expect(rows).toEqual([
			expect.objectContaining({
				name: "IP-BOX-CUSTOMER",
				currency: "USD",
				customer: "CUST-001",
				valid_upto: "2026-12-31",
			}),
		]);
	});

	it("resolves the active exact-UOM price with customer precedence", async () => {
		await itemPriceRepository.upsertMany([
			{
				name: "IP-DOZEN-GENERAL",
				price_list: "Retail",
				item_code: "ITEM-002",
				uom: "Dozen",
				currency: "PKR",
				customer: null,
				selling: 1,
				price_list_rate: 960,
			},
			{
				name: "IP-DOZEN-CUSTOMER",
				price_list: "Retail",
				item_code: "ITEM-002",
				uom: "Dozen",
				currency: "PKR",
				customer: "CUST-001",
				selling: 1,
				price_list_rate: 900,
				valid_from: "2026-01-01",
				valid_upto: "2026-12-31",
			},
			{
				name: "IP-DOZEN-EXPIRED",
				price_list: "Retail",
				item_code: "ITEM-002",
				uom: "Dozen",
				currency: "PKR",
				customer: "CUST-001",
				selling: 1,
				price_list_rate: 800,
				valid_upto: "2025-12-31",
			},
		]);

		const row = await itemPriceRepository.findApplicableForItemAndUom({
			priceList: "Retail",
			itemCode: "ITEM-002",
			uom: "Dozen",
			customer: "CUST-001",
			currency: "PKR",
			date: "2026-09-04",
		});

		expect(row).toEqual(
			expect.objectContaining({
				name: "IP-DOZEN-CUSTOMER",
				price_list_rate: 900,
			}),
		);
	});

	it("deletes Item Prices by ERPNext document name", async () => {
		await itemPriceRepository.upsertMany([
			{
				name: "IP-DELETE",
				price_list: "Retail",
				item_code: "ITEM-001",
				uom: "Nos",
				currency: "PKR",
				customer: null,
				price_list_rate: 100,
			},
		]);

		await itemPriceRepository.deleteByNames(["IP-DELETE"]);

		expect(
			await itemPriceRepository.findForItem("Retail", "ITEM-001"),
		).toEqual([]);
	});

	it("purges Item Prices from price lists that leave the selling scope", async () => {
		await itemPriceRepository.upsertMany([
			{
				name: "IP-RETAIL",
				price_list: "Retail",
				item_code: "ITEM-001",
				uom: "Nos",
				currency: "PKR",
				price_list_rate: 100,
			},
			{
				name: "IP-OLD-LIST",
				price_list: "Old Retail",
				item_code: "ITEM-001",
				uom: "Nos",
				currency: "PKR",
				price_list_rate: 90,
			},
		]);

		await itemPriceRepository.deleteOutsidePriceLists(["Retail"]);

		expect(
			await itemPriceRepository.findForItem("Old Retail", "ITEM-001"),
		).toEqual([]);
		expect(
			await itemPriceRepository.findForItem("Retail", "ITEM-001"),
		).toHaveLength(1);
	});

	it("atomically replaces stale Pricing Rule targets", async () => {
		await pricingRuleRepository.replaceRuleTargets([
			{
				key: "RULE-1::item_group::Old Group",
				rule_name: "RULE-1",
				target_type: "item_group",
				target_value: "Old Group",
				name: "RULE-1",
			},
		]);
		await pricingRuleRepository.replaceRuleTargets([
			{
				key: "RULE-1::item_group::New Group",
				rule_name: "RULE-1",
				target_type: "item_group",
				target_value: "New Group",
				name: "RULE-1",
			},
		]);

		expect(
			await pricingRuleRepository.findByTarget(
				"item_group",
				"Old Group",
			),
		).toEqual([]);
		expect(
			await pricingRuleRepository.findByTarget(
				"item_group",
				"New Group",
			),
		).toEqual([
			expect.objectContaining({
				rule_name: "RULE-1",
				target_value: "New Group",
			}),
		]);
	});

	it("resolves the latest stored Currency Exchange rate on or before a date", async () => {
		await currencyRateRepository.upsertMany([
			{
				name: "FX-OLD",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "PKR",
				exchange_rate: 275,
				date: "2026-05-01",
			},
			{
				name: "FX-NEW",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "PKR",
				exchange_rate: 280,
				date: "2026-06-01",
			},
		]);

		const rate = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "PKR",
			date: "2026-05-15",
		});

		expect(rate).toEqual(
			expect.objectContaining({
				name: "FX-OLD",
				exchange_rate: 275,
			}),
		);
	});

	it("filters Currency Exchange rows by purpose and breaks same-day ties by specificity then name desc", async () => {
		await currencyRateRepository.upsertMany([
			{
				name: "FX-001",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4000,
				date: "2026-05-10",
				for_buying: 1,
				for_selling: 1,
			},
			{
				name: "FX-002",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2026-05-10",
				for_buying: 1,
				for_selling: 0,
			},
			{
				name: "FX-003",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4080,
				date: "2026-05-10",
				for_buying: 1,
				for_selling: 0,
			},
			{
				name: "FX-004",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3700,
				date: "2026-05-15",
				for_buying: 0,
				for_selling: 1,
			},
		]);

		const buyingRate = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-05-20",
			purpose: "for_buying",
		});
		expect(buyingRate?.name).toBe("FX-003");
		expect(buyingRate?.exchange_rate).toBe(4080);

		const sellingRate = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-05-20",
			purpose: "for_selling",
		});
		expect(sellingRate?.name).toBe("FX-004");
		expect(sellingRate?.exchange_rate).toBe(3700);
	});

	it("enforces strict Accounts Settings stale_days cutoff when allow_stale is 0", async () => {
		await currencyRateRepository.upsertMany([
			{
				name: "FX-STALE-BOUNDARY",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3900,
				date: "2026-10-29",
				for_buying: 1,
				for_selling: 0,
			},
			{
				name: "FX-JAN-2025",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3800,
				date: "2025-01-15",
				for_buying: 1,
				for_selling: 0,
			},
		]);

		// With allow_stale=0 and stale_days=30 on 2026-11-28, checkpoint is 2026-10-29:
		// ERPNext setup/utils.py:87 requires strict date > checkpoint_date -> both 2026-10-29 and 2025-01-15 are rejected.
		const rejected = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-11-28",
			purpose: "for_buying",
			allowStale: 0,
			staleDays: 30,
		});
		expect(rejected).toBeNull();

		// A rate dated 2026-10-30 (> 2026-10-29) IS accepted.
		await currencyRateRepository.upsertMany([
			{
				name: "FX-FRESH",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2026-10-30",
				for_buying: 1,
				for_selling: 0,
			},
		]);
		const accepted = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-11-28",
			purpose: "for_buying",
			allowStale: 0,
			staleDays: 30,
		});
		expect(accepted?.name).toBe("FX-FRESH");
		expect(accepted?.exchange_rate).toBe(4050);

		// With allow_stale=1, stale rates remain eligible.
		const permissive = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-10-29",
			purpose: "for_buying",
			allowStale: 1,
			staleDays: 30,
		});
		expect(permissive?.name).toBe("FX-STALE-BOUNDARY");
	});

	it("strictly requires matching purpose flags for explicit purpose lookups and rejects legacy unqualified rows", async () => {
		await currencyRateRepository.upsertMany([
			{
				name: "FX-BUY-VERIFIED",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2026-05-10",
				for_buying: 1,
				for_selling: 0,
			},
			{
				name: "FX-LEGACY-NEWER",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3700,
				date: "2026-05-15",
			},
		]);

		// Newer unqualified legacy row MUST NOT beat verified for_buying=1 row.
		const buying = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-05-20",
			purpose: "for_buying",
		});
		expect(buying?.name).toBe("FX-BUY-VERIFIED");
		expect(buying?.exchange_rate).toBe(4050);

		// Explicit for_selling with only a legacy unqualified row + buying-only row MUST fail closed (null).
		const selling = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-05-20",
			purpose: "for_selling",
		});
		expect(selling).toBeNull();

		// When only unqualified legacy rows exist, explicit for_buying MUST return null,
		// while unqualified lookup (no purpose) still returns the legacy row.
		await currencyRateRepository.deleteByNames(["FX-BUY-VERIFIED"]);
		expect(
			await currencyRateRepository.findLatestOnOrBefore({
				profileName: "POS-1",
				company: "Test Co",
				fromCurrency: "USD",
				toCurrency: "UGX",
				date: "2026-05-20",
				purpose: "for_buying",
			}),
		).toBeNull();
		expect(
			(
				await currencyRateRepository.findLatestOnOrBefore({
					profileName: "POS-1",
					company: "Test Co",
					fromCurrency: "USD",
					toCurrency: "UGX",
					date: "2026-05-20",
				})
			)?.name,
		).toBe("FX-LEGACY-NEWER");
	});

	it("supports ERPNext inverse Currency Exchange fallback with opposite purpose and stale-policy enforcement", async () => {
		// Only reverse UGX -> USD exists with for_selling=1 (opposite of for_buying).
		await currencyRateRepository.upsertMany([
			{
				name: "FX-REV-SELL",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "UGX",
				to_currency: "USD",
				exchange_rate: 1 / 4050,
				date: "2026-11-20",
				for_buying: 0,
				for_selling: 1,
			},
			{
				name: "FX-REV-WRONG",
				profile_name: "POS-1",
				company: "Test Co",
				from_currency: "UGX",
				to_currency: "USD",
				exchange_rate: 1 / 3700,
				date: "2026-11-25",
				for_buying: 1,
				for_selling: 0,
			},
		]);

		const buyingInverse = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-11-28",
			purpose: "for_buying",
			allowStale: 0,
			staleDays: 30,
		});
		expect(buyingInverse).not.toBeNull();
		expect(buyingInverse?.exchange_rate).toBeCloseTo(4050, 2);

		// Stale inverse (Nov 20 on Dec 20 with allowStale=0, staleDays=30 -> boundary 2026-11-20 rejected).
		const staleInverse = await currencyRateRepository.findLatestOnOrBefore({
			profileName: "POS-1",
			company: "Test Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-12-20",
			purpose: "for_buying",
			allowStale: 0,
			staleDays: 30,
		});
		expect(staleInverse).toBeNull();
	});
});
