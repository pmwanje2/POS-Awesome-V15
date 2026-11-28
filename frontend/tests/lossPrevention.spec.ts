import { describe, expect, it } from "vitest";

import {
	findLossRiskItems,
	getEffectiveSellingRate,
	getItemLossRisk,
	resolveSaleFloorPolicy,
} from "../src/posapp/utils/lossPrevention";

describe("loss prevention", () => {
	it("flags sale rows priced below trade price", () => {
		const risk = getItemLossRisk({
			item_code: "02017",
			item_name: "ARINAC FORT",
			qty: 1,
			rate: 10,
			trade_price: 12.75,
		});

		expect(risk?.belowCost).toBe(true);
		expect(risk?.costField).toBe("trade_price");
		expect(risk?.sellingRate).toBe(10);
		expect(risk?.costRate).toBe(12.75);
	});

	it("allows rows at or above buying price", () => {
		expect(
			getItemLossRisk({
				item_code: "02017",
				qty: 1,
				rate: 12.75,
				trade_price: 12.75,
			}),
		).toBeNull();
	});

	it("uses amount divided by quantity when rate is unavailable", () => {
		expect(
			getEffectiveSellingRate({
				qty: 2,
				amount: 18,
				trade_price: 10,
			}),
		).toBe(9);
	});

	it("collects only below-cost rows", () => {
		const risks = findLossRiskItems([
			{ item_code: "LOW", qty: 1, rate: 5, buying_rate: 6 },
			{ item_code: "OK", qty: 1, rate: 7, buying_rate: 6 },
		]);

		expect(risks).toHaveLength(1);
		expect(risks[0].itemCode).toBe("LOW");
	});

	it("converts a stock-UOM buying floor for the selected UOM", () => {
		const risk = getItemLossRisk({
			item_code: "BOXED",
			qty: 1,
			rate: 100,
			conversion_rate: 1,
			uom: "Box",
			stock_uom: "Nos",
			conversion_factor: 10,
			_buying_prices_by_uom: {
				Nos: { base_price_list_rate: 12.75 },
			},
		});

		expect(risk?.costRate).toBe(127.5);
		expect(risk?.costField).toBe("_buying_prices_by_uom");
	});

	it("prefers an exact selected-UOM buying price", () => {
		const risk = getItemLossRisk({
			item_code: "BOXED",
			qty: 1,
			rate: 115,
			conversion_rate: 1,
			uom: "Box",
			stock_uom: "Nos",
			conversion_factor: 10,
			_buying_prices_by_uom: {
				Nos: { base_price_list_rate: 12.75 },
				Box: { base_price_list_rate: 120 },
			},
		});

		expect(risk?.costRate).toBe(120);
	});

	it("compares company-currency buying floor in the invoice currency", () => {
		const risk = getItemLossRisk({
			item_code: "USD-ITEM",
			qty: 1,
			rate: 3.5,
			conversion_rate: 280,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			_buying_prices_by_uom: {
				Nos: { base_price_list_rate: 1000 },
			},
		});

		expect(risk?.costRate).toBeCloseTo(1000 / 280);
	});

	it("preserves hard-block defaults when new profile fields are absent", () => {
		expect(resolveSaleFloorPolicy({})).toEqual({
			enabled: true,
			action: "Block",
			minimumMarginPercentage: 0,
			missingCostAction: "Allow",
		});
	});

	it("applies configured minimum margin to the loss floor", () => {
		const risk = getItemLossRisk(
			{ item_code: "MARGIN", qty: 1, rate: 104, trade_price: 100 },
			{ minimumMarginPercentage: 5 },
		);

		expect(risk?.costRate).toBe(105);
		expect(risk?.costLabel).toBe("Minimum Rate");
	});

	it("applies invoice-level discount before checking the sale floor", () => {
		const risk = getItemLossRisk(
			{ item_code: "DISCOUNTED", qty: 1, rate: 105, trade_price: 100 },
			{ invoiceDiscountPercentage: 10 },
		);

		expect(risk?.sellingRate).toBeCloseTo(94.5);
		expect(risk?.costRate).toBe(100);
	});

	it("selects the highest cost floor across buying price, warehouse valuation and last purchase rate", () => {
		const risk = getItemLossRisk({
			item_code: "QB-524",
			item_name: "RONG PLASTIC TOOTHPICKS WOODEN MED",
			qty: 1,
			rate: 2000,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			conversion_rate: 1,
			buying_rate: 1500,
			valuation_rate: 4500,
			last_purchase_rate: 3000,
		});

		expect(risk?.belowCost).toBe(true);
		expect(risk?.costField).toBe("valuation_rate");
		expect(risk?.costLabel).toBe("Warehouse Valuation");
		expect(risk?.costRate).toBe(4500);
	});

	it("scales warehouse valuation_rate by conversion_factor and company currency", () => {
		const risk = getItemLossRisk({
			item_code: "BOXED",
			qty: 1,
			rate: 35,
			uom: "Box",
			stock_uom: "Nos",
			conversion_factor: 10,
			conversion_rate: 250,
			valuation_rate: 1000,
		});

		expect(risk?.belowCost).toBe(true);
		expect(risk?.costField).toBe("valuation_rate");
		expect(risk?.costRate).toBeCloseTo(40);
	});

	it("excludes ERPNext pricing rule free items from below-cost risk", () => {
		expect(
			getItemLossRisk({
				item_code: "PROMO",
				qty: 1,
				rate: 0,
				is_free_item: 1,
				pricing_rules: "PR-FREE",
				valuation_rate: 4500,
			}),
		).toBeNull();
	});

	it("excludes the till's own pricing rule free lines, which the server re-derives", () => {
		expect(
			getItemLossRisk({
				item_code: "PROMO",
				qty: 1,
				rate: 0,
				is_free_item: 1,
				source_rule: "PR-FREE",
				auto_free_source: "PR-FREE::TRIG::PROMO",
				valuation_rate: 4500,
			}),
		).toBeNull();
	});

	it("excludes every pricing rule free line the server strips and re-derives", () => {
		const markers = [
			{ is_free_item: 1, auto_free_source: "PR-FREE::TRIG::PROMO" },
			{ auto_free_source: "PR-FREE::TRIG::PROMO" },
			{ is_free_item: 1, source_rule: "PR-FREE" },
			{ is_free_item: 1, pricing_rule: "PR-FREE" },
			{ is_free_item: 1, pricing_rules: ["PR-FREE"] },
		];
		for (const marker of markers) {
			expect(
				getItemLossRisk({
					item_code: "PROMO",
					qty: 1,
					rate: 0,
					valuation_rate: 4500,
					...marker,
				}),
			).toBeNull();
		}
	});

	it("still evaluates paid rows that carry a pricing rule", () => {
		const risk = getItemLossRisk({
			item_code: "DISC",
			qty: 1,
			rate: 9,
			trade_price: 10,
			pricing_rules: "PR-DISC",
			source_rule: "PR-DISC",
		});

		expect(risk?.belowCost).toBe(true);
	});

	it("evaluates POS Offer give-aways for below-cost risk", () => {
		const risk = getItemLossRisk({
			item_code: "GIFT",
			qty: 1,
			rate: 0,
			is_free_item: 1,
			posa_is_offer: 1,
			valuation_rate: 4500,
		});

		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBe(4500);
	});

	it("evaluates free rows without a pricing rule for below-cost risk", () => {
		for (const pricingRules of [undefined, "", []]) {
			const risk = getItemLossRisk({
				item_code: "FORGED",
				qty: 1,
				rate: 0,
				is_free_item: 1,
				pricing_rules: pricingRules,
				valuation_rate: 4500,
			});

			expect(risk?.belowCost).toBe(true);
		}
	});

	it("allows a valid USD invoice sale when raw UGX buying fields accompany _buying_prices_by_uom", () => {
		expect(
			getItemLossRisk({
				item_code: "MC-UGX-BUY-USD-SELL",
				qty: 1,
				rate: 15,
				currency: "USD",
				conversion_rate: 3700,
				uom: "Nos",
				stock_uom: "Nos",
				conversion_factor: 1,
				buying_price_currency: "UGX",
				trade_price: 37000,
				buying_rate: 37000,
				_buying_prices_by_uom: {
					Nos: {
						price_list_rate: 37000,
						base_price_list_rate: 37000,
						currency: "UGX",
					},
				},
			}),
		).toBeNull();
	});

	it("blocks a below-cost USD invoice sale at the normalized USD floor instead of raw UGX buying rate", () => {
		const risk = getItemLossRisk({
			item_code: "MC-UGX-BUY-USD-SELL",
			qty: 1,
			rate: 8,
			currency: "USD",
			conversion_rate: 3700,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_price_currency: "UGX",
			trade_price: 37000,
			buying_rate: 37000,
			_buying_prices_by_uom: {
				Nos: {
					price_list_rate: 37000,
					base_price_list_rate: 37000,
					currency: "UGX",
				},
			},
		});

		expect(risk?.belowCost).toBe(true);
		expect(risk?.costField).toBe("_buying_prices_by_uom");
		expect(risk?.costRate).toBeCloseTo(10);
	});

	it("handles USD buying list with UGX invoice for both valid and below-cost sales", () => {
		const baseItem = {
			item_code: "MC-USD-BUY-UGX-SELL",
			qty: 1,
			currency: "UGX",
			conversion_rate: 1,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_price_currency: "USD",
			trade_price: 10,
			buying_rate: 10,
			_buying_prices_by_uom: {
				Nos: {
					price_list_rate: 10,
					base_price_list_rate: 37000,
					currency: "USD",
				},
			},
		};

		expect(getItemLossRisk({ ...baseItem, rate: 40000 })).toBeNull();

		const risk = getItemLossRisk({ ...baseItem, rate: 30000 });
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costField).toBe("_buying_prices_by_uom");
		expect(risk?.costRate).toBeCloseTo(37000);
	});

	it("allows a valid USD invoice sale when company currency is USD and buying list is UGX", () => {
		expect(
			getItemLossRisk({
				item_code: "MC-USD-CO-UGX-BUY",
				qty: 1,
				rate: 15,
				currency: "USD",
				conversion_rate: 1,
				uom: "Nos",
				stock_uom: "Nos",
				conversion_factor: 1,
				buying_price_currency: "UGX",
				trade_price: 37000,
				buying_rate: 37000,
				_buying_prices_by_uom: {
					Nos: {
						price_list_rate: 37000,
						base_price_list_rate: 10,
						currency: "UGX",
					},
				},
			}),
		).toBeNull();
	});

	it("normalizes mismatched-currency buying fields against company currency when _buying_prices_by_uom is absent", () => {
		const baseItem = {
			item_code: "MC-NO-TABLE",
			qty: 1,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_price_currency: "UGX",
			trade_price: 37000,
			buying_rate: 37000,
		};

		expect(getItemLossRisk({ ...baseItem, rate: 15 })).toBeNull();

		const risk = getItemLossRisk({ ...baseItem, rate: 8 });
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBeCloseTo(10);
	});

	it("skips unconvertible foreign-currency buying fields instead of falsely blocking valid multicurrency sales", () => {
		expect(
			getItemLossRisk({
				item_code: "MC-UNCONVERTIBLE",
				qty: 1,
				rate: 15,
				currency: "USD",
				conversion_rate: 3700,
				uom: "Nos",
				stock_uom: "Nos",
				conversion_factor: 1,
				buying_price_currency: "UGX",
				trade_price: 37000,
				buying_rate: 37000,
			}),
		).toBeNull();
	});
});
