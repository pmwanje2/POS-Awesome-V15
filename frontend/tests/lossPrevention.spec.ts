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

	it("excludes free promotional items from below-cost risk", () => {
		expect(
			getItemLossRisk({
				item_code: "PROMO",
				qty: 1,
				rate: 0,
				is_free_item: 1,
				valuation_rate: 4500,
			}),
		).toBeNull();
	});
});
