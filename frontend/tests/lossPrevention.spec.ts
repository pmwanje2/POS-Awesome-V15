import { ref } from "vue";
import { describe, expect, it, vi } from "vitest";

import { useItemCurrency } from "../src/posapp/composables/pos/items/useItemCurrency";
import { registerItemsSelectorEvents } from "../src/posapp/composables/pos/items/useItemsSelectorEvents";
import {
	computeRowTaxParams,
	findLossRiskItems,
	getEffectiveSellingRate,
	getItemCostFloor,
	getItemLossRisk,
	resolveInvoiceDiscountPercentage,
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

	it("preserves below-cost detection across UGX -> USD -> UGX currency switches without _buying_prices_by_uom", () => {
		const { applyCurrencyConversionToItem } = useItemCurrency();
		const item: Record<string, unknown> = {
			item_code: "SWITCH-UGX-USD-UGX",
			qty: 1,
			rate: 55500,
			price_list_rate: 55500,
			currency: "UGX",
			company_currency: "UGX",
			conversion_rate: 1,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_price_currency: "UGX",
			buying_rate_currency: "UGX",
			trade_price: 37000,
			buying_rate: 37000,
		};

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "UGX",
			selected_currency: "USD",
			exchange_rate: 1 / 3700,
			conversion_rate: 3700,
			currency_precision: 2,
		});
		expect(getItemLossRisk(item)).toBeNull();

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "UGX",
			selected_currency: "UGX",
			exchange_rate: 1,
			conversion_rate: 1,
			currency_precision: 2,
		});
		item.rate = 30000;

		const risk = getItemLossRisk(item);
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBeCloseTo(37000);
	});

	it("preserves below-cost detection when a USD-normalized scalar item switches back to UGX without _buying_prices_by_uom", () => {
		const { applyCurrencyConversionToItem } = useItemCurrency();
		const item: Record<string, unknown> = {
			item_code: "SWITCH-USD-TO-UGX",
			qty: 1,
			rate: 15,
			price_list_rate: 15,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			plc_conversion_rate: 3700,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_price_currency: "UGX",
			buying_rate_currency: "USD",
			trade_price: 10,
			buying_rate: 10,
		};

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "USD",
			selected_currency: "UGX",
			exchange_rate: 3700,
			conversion_rate: 1,
			currency_precision: 2,
		});
		item.rate = 30000;

		const risk = getItemLossRisk(item);
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBeCloseTo(37000);
	});

	it("does not treat a stale USD-tagged scalar buying price as UGX after switching invoice currency to UGX", () => {
		const item = {
			item_code: "STALE-USD-TAG",
			qty: 1,
			rate: 30000,
			currency: "UGX",
			company_currency: "UGX",
			conversion_rate: 1,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_price_currency: "UGX",
			buying_rate_currency: "USD",
			trade_price: 10,
			buying_rate: 10,
		};

		expect(getItemCostFloor(item)).toBeNull();
	});

	it("does not fall back to 1:1 cross-currency comparison when invoice FX is missing, 0, or NaN (with _buying_prices_by_uom)", () => {
		for (const conversionRate of [undefined, 0, Number.NaN]) {
			const item = {
				item_code: "FX-MISSING-WITH-TABLE",
				qty: 1,
				rate: 15,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: conversionRate,
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
			};

			expect(getItemCostFloor(item)).toBeNull();
			expect(getItemLossRisk(item)).toBeNull();
		}
	});

	it("does not fall back to 1:1 cross-currency comparison when invoice FX is missing, 0, or NaN (without _buying_prices_by_uom)", () => {
		for (const conversionRate of [undefined, 0, Number.NaN]) {
			const item = {
				item_code: "FX-MISSING-NO-TABLE",
				qty: 1,
				rate: 15,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: conversionRate,
				uom: "Nos",
				stock_uom: "Nos",
				conversion_factor: 1,
				buying_price_currency: "UGX",
				trade_price: 37000,
				buying_rate: 37000,
				valuation_rate: 37000,
			};

			expect(getItemCostFloor(item)).toBeNull();
			expect(getItemLossRisk(item)).toBeNull();
		}
	});

	it("matches ERPNext Grand Total discount distribution on taxed items", () => {
		const exclusiveTax = [
			{
				charge_type: "On Net Total",
				rate: 10,
				included_in_print_rate: 0,
			},
		];
		expect(
			getItemLossRisk(
				{
					item_code: "GT-TAX-OK",
					qty: 1,
					rate: 100,
					net_rate: 90,
					trade_price: 89.5,
				},
				{
					invoiceDiscountPercentage: 11,
					applyDiscountOn: "Grand Total",
					taxes: exclusiveTax,
				},
			),
		).toBeNull();

		const below = getItemLossRisk(
			{
				item_code: "GT-TAX-LOW",
				qty: 1,
				rate: 100,
				net_rate: 90,
				trade_price: 90.5,
			},
			{
				invoiceDiscountPercentage: 11,
				applyDiscountOn: "Grand Total",
				taxes: exclusiveTax,
			},
		);
		expect(below?.belowCost).toBe(true);
		expect(below?.sellingRate).toBeCloseTo(90);
	});

	it("does not poison _company_buying_price with 1:1 when price-list FX is missing in useItemCurrency", () => {
		const { applyCurrencyConversionToItem } = useItemCurrency();
		const item: Record<string, unknown> = {
			item_code: "USD-PL-NO-FX",
			qty: 1,
			rate: 15,
			price_list_rate: 15,
			currency: "USD",
			buying_rate_currency: "USD",
			trade_price: 10,
			buying_rate: 10,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
		};

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "USD",
			selected_currency: "USD",
			exchange_rate: 1,
			conversion_rate: 0,
			currency_precision: 2,
		});

		expect(item._company_buying_price).toBeUndefined();
		expect(item.base_buying_rate).toBeUndefined();

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "USD",
			selected_currency: "USD",
			exchange_rate: 1,
			conversion_rate: 3700,
			currency_precision: 2,
		});

		expect(item._company_buying_price).toBeCloseTo(37000);
		expect(item.base_buying_rate).toBeCloseTo(37000);
	});

	it("returns null floor when company-currency valuation cannot be converted even if lower invoice-currency trade_price exists", () => {
		const item = {
			item_code: "HIDDEN-VALUATION",
			qty: 1,
			rate: 15,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 0,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_rate_currency: "USD",
			trade_price: 9,
			valuation_rate: 74000,
		};

		expect(getItemCostFloor(item)).toBeNull();
		expect(getItemLossRisk(item)).toBeNull();
	});

	it("accepts valid positive FX rates below 0.0001 such as USD/VND 0.00004", () => {
		const item = {
			item_code: "VND-FX-SMALL",
			qty: 1,
			rate: 20000,
			currency: "VND",
			company_currency: "USD",
			conversion_rate: 0.00004,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			valuation_rate: 1,
		};

		const floor = getItemCostFloor(item);
		expect(floor).not.toBeNull();
		expect(floor?.baseCurrency).toBe(true);
		expect(floor?.rate).toBeCloseTo(1);

		const risk = getItemLossRisk(item);
		expect(risk?.belowCost).toBe(true);
		expect(risk?.sellingRate).toBeCloseTo(20000);
		expect(risk?.costRate).toBeCloseTo(25000);
	});

	it("clears selectedConversionRate on zero or omitted update_currency so useItemCurrency does not cache 1:1 buying cost", () => {
		const handlers = new Map<string, (..._args: any[]) => void>();
		const eventBus = {
			on: (event: string, handler: (..._args: any[]) => void) => {
				handlers.set(event, handler);
			},
			off: (event: string) => {
				handlers.delete(event);
			},
		};
		const selectedCurrency = ref("UGX");
		const selectedExchangeRate = ref(1);
		const selectedConversionRate = ref(1);
		const selectedSupplier = ref<string | null>(null);

		const cleanup = registerItemsSelectorEvents({
			eventBus,
			selectedCurrency,
			selectedExchangeRate,
			selectedConversionRate,
			selectedSupplier,
			syncSelectorPriceList: vi.fn(),
			scheduleLastBuyingRateRefresh: vi.fn(),
			requestItemSearchFocus: vi.fn(),
			handleCartQuantitiesUpdated: vi.fn(),
			handleRemoteStockAdjustment: vi.fn(),
		});

		const emitCurrency = handlers.get("update_currency")!;
		emitCurrency({
			currency: "USD",
			exchange_rate: 1,
			conversion_rate: 0,
		});
		expect(selectedCurrency.value).toBe("USD");
		expect(selectedConversionRate.value).toBe(0);

		const { applyCurrencyConversionToItem } = useItemCurrency();
		const item: Record<string, unknown> = {
			item_code: "USD-SWITCH-ZERO-FX",
			qty: 1,
			rate: 15,
			price_list_rate: 15,
			currency: "USD",
			buying_rate_currency: "USD",
			trade_price: 10,
			buying_rate: 10,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
		};

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "USD",
			selected_currency: selectedCurrency.value,
			exchange_rate: selectedExchangeRate.value,
			conversion_rate: selectedConversionRate.value,
			currency_precision: 2,
		});

		expect(item._company_buying_price).toBeUndefined();
		expect(item.base_buying_rate).toBeUndefined();

		emitCurrency({
			currency: "USD",
			exchange_rate: 1,
			conversion_rate: 3700,
		});
		expect(selectedConversionRate.value).toBe(3700);

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "USD",
			selected_currency: selectedCurrency.value,
			exchange_rate: selectedExchangeRate.value,
			conversion_rate: selectedConversionRate.value,
			currency_precision: 2,
		});

		expect(item._company_buying_price).toBeCloseTo(37000);
		expect(item.base_buying_rate).toBeCloseTo(37000);

		// Omitting conversion_rate on a currency switch also clears the stale rate.
		emitCurrency({ currency: "EUR" });
		expect(selectedCurrency.value).toBe("EUR");
		expect(selectedConversionRate.value).toBe(0);
		expect(selectedExchangeRate.value).toBe(0);

		cleanup();
	});

	it("honors reconciled native net_rate on middle line where net_rate equals gross rate", () => {
		const items = [
			{
				item_code: "LINE-1",
				qty: 1,
				rate: 1.0,
				net_rate: 0.99,
				net_amount: 0.99,
				trade_price: 1.0,
			},
			{
				item_code: "LINE-2",
				qty: 1,
				rate: 1.0,
				net_rate: 1.0,
				net_amount: 1.0,
				trade_price: 1.0,
			},
			{
				item_code: "LINE-3",
				qty: 1,
				rate: 1.0,
				net_rate: 0.99,
				net_amount: 0.99,
				trade_price: 1.0,
			},
		];
		const doc = {
			apply_discount_on: "Net Total",
			discount_amount: 0.02,
			items,
		};
		const invoiceDiscountPercentage = resolveInvoiceDiscountPercentage(
			doc,
			items,
		);
		const risks = findLossRiskItems(items, {
			invoiceDiscountPercentage,
			applyDiscountOn: "Net Total",
		});
		expect(risks.map((risk) => risk.itemCode)).toEqual(["LINE-1", "LINE-3"]);

		// Stale un-discounted net_rates must still be rejected.
		const staleItems = items.map((row) => ({
			...row,
			net_rate: 1.0,
			net_amount: 1.0,
		}));
		const staleRisks = findLossRiskItems(staleItems, {
			invoiceDiscountPercentage,
			applyDiscountOn: "Net Total",
		});
		expect(staleRisks.map((risk) => risk.itemCode)).toEqual([
			"LINE-1",
			"LINE-2",
			"LINE-3",
		]);
	});

	it("does not dilute Grand Total discount by On Item Quantity tax via grand_total fallback when net_rate is absent", () => {
		const taxes = [
			{
				charge_type: "On Item Quantity",
				account_head: "Levy - CO",
				rate: 20,
				included_in_print_rate: 0,
			},
		];
		const items = [
			{
				item_code: "QTY-TAX-ITEM",
				qty: 1,
				rate: 100,
				trade_price: 89,
			},
		];
		const doc = {
			apply_discount_on: "Grand Total",
			discount_amount: 12,
			grand_total: 108,
			total_taxes_and_charges: 20,
			taxes,
			items,
		};
		const invoiceDiscountPercentage = resolveInvoiceDiscountPercentage(
			doc,
			items,
		);
		expect(invoiceDiscountPercentage).toBeCloseTo(12);

		const risk = getItemLossRisk(items[0], {
			invoiceDiscountPercentage,
			applyDiscountOn: "Grand Total",
			taxes,
		});
		expect(risk?.belowCost).toBe(true);
		expect(risk?.sellingRate).toBeCloseTo(88);
	});

	it("returns null floor when foreign-currency buying price lacks FX even if lower valuation_rate exists", () => {
		const item = {
			item_code: "EUR-BUY-NO-FX",
			qty: 1,
			rate: 8,
			currency: "USD",
			company_currency: "USD",
			conversion_rate: 1,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			buying_price_currency: "EUR",
			buying_rate_currency: "EUR",
			trade_price: 10,
			buying_rate: 10,
			valuation_rate: 5,
		};

		expect(getItemCostFloor(item)).toBeNull();
	});

	it("treats item_tax_rate N/A as 0% tax instead of falling back to inclusive template rate", () => {
		const taxes = [
			{
				charge_type: "On Net Total",
				account_head: "VAT",
				rate: 20,
				included_in_print_rate: 1,
			},
		];
		const item = {
			item_code: "NA-TAX-ITEM",
			qty: 1,
			rate: 100,
			trade_price: 95,
			item_tax_rate: JSON.stringify({ VAT: "N/A" }),
		};

		expect(computeRowTaxParams(item, taxes)).toEqual({
			inclA: 0,
			inclB: 0,
			allA: 0,
			allB: 0,
		});
		expect(getItemLossRisk(item, { taxes })).toBeNull();

		const below = getItemLossRisk({ ...item, trade_price: 101 }, { taxes });
		expect(below?.belowCost).toBe(true);
		expect(below?.sellingRate).toBeCloseTo(100);
	});

	it("returns null floor on merge_item_row output with unresolved foreign _buying_prices_by_uom despite lower USD valuation", () => {
		const mergedItem = {
			item_code: "EUR-MERGE-ROW",
			item_name: "EUR Cost Merged Item",
			qty: 1,
			rate: 8,
			price_list_rate: 8,
			currency: "USD",
			price_list_currency: "USD",
			conversion_rate: 1,
			plc_conversion_rate: 1,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
			base_buying_rate: null,
			trade_price: null,
			buying_rate: null,
			buying_rate_currency: null,
			buying_price_currency: "EUR",
			buying_exchange_rate: null,
			valuation_rate: 5,
			_buying_prices_by_uom: {
				Nos: {
					price_list_rate: 10,
					base_price_list_rate: null,
					currency: "EUR",
					price_list: "Standard Buying",
				},
			},
		};

		expect(getItemCostFloor(mergedItem)).toBeNull();
	});

	it("honors native rounded net_rate on reconciled multi-unit line (3 x 1.00 with 0.01 discount)", () => {
		const items = [
			{
				item_code: "MULTI-UNIT",
				qty: 3,
				rate: 1.0,
				net_amount: 2.99,
				net_rate: 1.0,
				base_net_rate: 1.0,
				trade_price: 1.0,
			},
		];
		const doc = {
			apply_discount_on: "Net Total",
			discount_amount: 0.01,
			items,
		};
		const invoiceDiscountPercentage = resolveInvoiceDiscountPercentage(
			doc,
			items,
		);
		expect(
			findLossRiskItems(items, {
				invoiceDiscountPercentage,
				applyDiscountOn: "Net Total",
				currencyPrecision: 2,
			}),
		).toEqual([]);

		// Forged inflated net_rate inconsistent with net_amount must still be flagged.
		const forgedItems = [
			{
				item_code: "MULTI-UNIT-FORGED",
				qty: 3,
				rate: 1.0,
				net_amount: 2.5,
				net_rate: 1.0,
				base_net_rate: 1.0,
				trade_price: 1.0,
			},
		];
		const forgedPct = resolveInvoiceDiscountPercentage(
			{
				apply_discount_on: "Net Total",
				discount_amount: 0.5,
				items: forgedItems,
			},
			forgedItems,
		);
		const forgedRisks = findLossRiskItems(forgedItems, {
			invoiceDiscountPercentage: forgedPct,
			applyDiscountOn: "Net Total",
			currencyPrecision: 2,
		});
		expect(forgedRisks.map((risk) => risk.itemCode)).toEqual([
			"MULTI-UNIT-FORGED",
		]);
	});

	it("does not let multi-UOM _buying_prices_by_uom insertion order override backend base_buying_rate when row uom/stock_uom are omitted", () => {
		const item = {
			item_code: "MULTI-UOM-NO-ROW-UOM",
			qty: 1,
			rate: 15,
			currency: "USD",
			company_currency: "USD",
			conversion_rate: 1,
			base_buying_rate: 10,
			_buying_prices_by_uom: {
				Box: {
					price_list_rate: 100,
					base_price_list_rate: 100,
					currency: "USD",
				},
				Nos: {
					price_list_rate: 10,
					base_price_list_rate: 10,
					currency: "USD",
				},
			},
		};

		const floor = getItemCostFloor(item);
		expect(floor).not.toBeNull();
		expect(floor?.rate).toBeCloseTo(10);
		expect(getItemLossRisk(item)).toBeNull();
	});

	it("fails closed when foreign _buying_prices_by_uom entry has null base_price_list_rate without positive buying_exchange_rate in a different company currency", () => {
		const item = {
			item_code: "USD-INV-UGX-CO-NULL-BASE",
			qty: 1,
			rate: 12,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			uom: "Nos",
			stock_uom: "Nos",
			_company_buying_price: 37000,
			_buying_prices_by_uom: {
				Nos: {
					currency: "USD",
					price_list_rate: 10,
					base_price_list_rate: null,
				},
			},
		};

		expect(getItemCostFloor(item)).toBeNull();
		expect(getItemLossRisk(item)).toBeNull();
	});

	it("preserves legitimate rounded zero net_rate === 0 on 3 x USD 0.01 with 0.02 discount against UGX 10 floor", () => {
		const items = [
			{
				item_code: "ZERO-NET-RATE",
				qty: 3,
				rate: 0.01,
				net_amount: 0.01,
				net_rate: 0,
				base_net_rate: 0,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 10,
			},
		];
		const doc = {
			apply_discount_on: "Net Total",
			discount_amount: 0.02,
			items,
		};
		const invoiceDiscountPercentage = resolveInvoiceDiscountPercentage(
			doc,
			items,
		);
		const risks = findLossRiskItems(items, {
			invoiceDiscountPercentage,
			applyDiscountOn: "Net Total",
			currencyPrecision: 2,
		});
		expect(risks).toHaveLength(1);
		expect(risks[0]?.itemCode).toBe("ZERO-NET-RATE");
		expect(risks[0]?.sellingRate).toBe(0);
	});

	it("returns null floor when _source_buying_currency preserves an unresolved foreign cost after switching currency", () => {
		const { applyCurrencyConversionToItem } = useItemCurrency();

		// Foreign -> foreign (EUR -> USD) without EUR FX: _source_buying_currency="EUR" must fail closed.
		const eurToUsdItem: Record<string, any> = {
			item_code: "EUR-TO-USD-NO-FX",
			item_name: "EUR Unresolved Foreign Switch",
			qty: 1,
			rate: 12,
			price_list_rate: 12,
			currency: "EUR",
			company_currency: "UGX",
			trade_price: 10,
		};
		applyCurrencyConversionToItem(eurToUsdItem, {
			pos_profile: { currency: "UGX" },
			price_list_currency: "USD",
			selected_currency: "USD",
			exchange_rate: 1,
			conversion_rate: 3700,
			currency_precision: 2,
			flt: (value: unknown) => Number(value),
		});
		expect(eurToUsdItem.currency).toBe("USD");
		expect(eurToUsdItem._source_buying_currency).toBe("EUR");
		expect(getItemCostFloor(eurToUsdItem)).toBeNull();

		// Foreign -> company (EUR -> UGX) without EUR FX: must also fail closed rather than reading 10 as UGX 10.
		const eurToCompanyItem: Record<string, any> = {
			item_code: "EUR-TO-UGX-NO-FX",
			item_name: "EUR Unresolved Company Switch",
			qty: 1,
			rate: 50000,
			price_list_rate: 50000,
			currency: "EUR",
			company_currency: "UGX",
			trade_price: 10,
		};
		applyCurrencyConversionToItem(eurToCompanyItem, {
			pos_profile: { currency: "UGX" },
			price_list_currency: "UGX",
			selected_currency: "UGX",
			exchange_rate: 1,
			conversion_rate: 1,
			currency_precision: 2,
			flt: (value: unknown) => Number(value),
		});
		expect(eurToCompanyItem.currency).toBe("UGX");
		expect(eurToCompanyItem._source_buying_currency).toBe("EUR");
		expect(getItemCostFloor(eurToCompanyItem)).toBeNull();

		// Preserved _source_buying_price/_source_buying_currency alone (without trade_price) must also fail closed.
		expect(
			getItemCostFloor({
				item_code: "EUR-SOURCE-ONLY",
				qty: 1,
				rate: 12,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				_source_buying_price: 10,
				_source_buying_currency: "EUR",
				valuation_rate: 5000,
			}),
		).toBeNull();
	});

	it("honors legitimate zero net_amount and net_rate on multi-line USD Net Total discount while ignoring uncalculated draft zeros", () => {
		const items = [
			{
				item_code: "SMALL-ZERO-LINE",
				item_name: "Small Rounded Zero Line",
				qty: 1,
				rate: 0.01,
				net_amount: 0,
				net_rate: 0,
				base_net_rate: 0,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 10,
			},
			{
				item_code: "SMALL-NONZERO-LINE",
				item_name: "Small Positive Line",
				qty: 1,
				rate: 0.04,
				net_amount: 0.02,
				net_rate: 0.02,
				base_net_rate: 74,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 10,
			},
		];
		const doc = {
			apply_discount_on: "Net Total",
			discount_amount: 0.03,
			items,
		};
		const invoiceDiscountPercentage = resolveInvoiceDiscountPercentage(
			doc,
			items,
		);
		const risks = findLossRiskItems(items, {
			invoiceDiscountPercentage,
			applyDiscountOn: "Net Total",
			currencyPrecision: 2,
		});
		expect(risks).toHaveLength(1);
		expect(risks[0]?.itemCode).toBe("SMALL-ZERO-LINE");
		expect(risks[0]?.sellingRate).toBe(0);

		// Uncalculated draft rows with net_amount=0, net_rate=0 and no discount must NOT trigger false below-cost blocks.
		const uncalculatedTinyDraft = [
			{
				item_code: "UNCALC-TINY",
				qty: 1,
				rate: 0.01,
				net_amount: 0,
				net_rate: 0,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 10,
			},
		];
		expect(
			findLossRiskItems(uncalculatedTinyDraft, {
				invoiceDiscountPercentage: 0,
				applyDiscountOn: "Net Total",
				currencyPrecision: 2,
			}),
		).toEqual([]);

		const uncalculatedLargeDraft = [
			{
				item_code: "UNCALC-LARGE",
				qty: 1,
				rate: 100,
				net_amount: 0,
				net_rate: 0,
				trade_price: 50,
			},
		];
		expect(
			findLossRiskItems(uncalculatedLargeDraft, {
				invoiceDiscountPercentage: 0,
				applyDiscountOn: "Net Total",
				currencyPrecision: 2,
			}),
		).toEqual([]);
	});

	it("does not treat uncalculated all-zero draft [0, 0, 0] as reconciled when 0.01 net remains, while honoring ERPNext [0, 0.01, 0]", () => {
		const buildThreeLines = (netValues: [number, number, number]) =>
			netValues.map((netVal, idx) => ({
				item_code: `CENT-LINE-${idx + 1}`,
				item_name: `Cent Line ${idx + 1}`,
				qty: 1,
				rate: 0.01,
				net_amount: netVal,
				net_rate: netVal,
				base_net_rate: netVal * 3700,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 10,
			}));

		// Uncalculated all-zero draft [0, 0, 0] has sum 0.00 vs expected 0.01 (1 cent off):
		// must NOT reconcile, falling back to 0.003333 * 3700 = UGX 12.33 >= UGX 10.
		const draftItems = buildThreeLines([0, 0, 0]);
		const draftPct = resolveInvoiceDiscountPercentage(
			{
				apply_discount_on: "Net Total",
				discount_amount: 0.02,
				items: draftItems,
			},
			draftItems,
		);
		expect(
			findLossRiskItems(draftItems, {
				invoiceDiscountPercentage: draftPct,
				applyDiscountOn: "Net Total",
				currencyPrecision: 2,
			}),
		).toEqual([]);

		// ERPNext calculated [0, 0.01, 0] reconciles cleanly: middle line passes (UGX 37 >= 10), outer lines fail at 0.
		const calculatedItems = buildThreeLines([0, 0.01, 0]);
		const calcPct = resolveInvoiceDiscountPercentage(
			{
				apply_discount_on: "Net Total",
				discount_amount: 0.02,
				items: calculatedItems,
			},
			calculatedItems,
		);
		const calcRisks = findLossRiskItems(calculatedItems, {
			invoiceDiscountPercentage: calcPct,
			applyDiscountOn: "Net Total",
			currencyPrecision: 2,
		});
		expect(calcRisks.map((risk) => risk.itemCode)).toEqual([
			"CENT-LINE-1",
			"CENT-LINE-3",
		]);
		expect(calcRisks.every((risk) => risk.sellingRate === 0)).toBe(true);
	});

	it("includes nonzero free_item_rate promotional row in invoice Net Total discount base while keeping the promo row floor-exempt", () => {
		const items = [
			{
				item_code: "PAID-100",
				item_name: "Paid Item",
				qty: 1,
				rate: 100,
				trade_price: 90,
			},
			{
				item_code: "PROMO-10",
				item_name: "Discounted Promo Item",
				qty: 1,
				rate: 10,
				is_free_item: 1,
				pricing_rules: "PR-FREE",
				trade_price: 100,
			},
		];
		const doc = {
			apply_discount_on: "Net Total",
			discount_amount: 11,
			items,
		};
		const pct = resolveInvoiceDiscountPercentage(doc, items);
		expect(pct).toBeCloseTo(10, 6);
		expect(
			findLossRiskItems(items, {
				invoiceDiscountPercentage: pct,
				applyDiscountOn: "Net Total",
				currencyPrecision: 2,
			}),
		).toEqual([]);

		// Reconciled native net amounts [90, 9] also pass against floor 90.
		const nativeItems = [
			{ ...items[0]!, net_amount: 90, net_rate: 90 },
			{ ...items[1]!, net_amount: 9, net_rate: 9 },
		];
		expect(
			findLossRiskItems(nativeItems, {
				invoiceDiscountPercentage: pct,
				applyDiscountOn: "Net Total",
				currencyPrecision: 2,
			}),
		).toEqual([]);
	});

	it("honors legitimately rounded ERPNext tax-inclusive net_amount (1 x USD 0.01 @ 18% VAT -> UGX 37 >= UGX 35)", () => {
		const taxes = [
			{
				charge_type: "On Net Total",
				account_head: "VAT 18% - CO",
				rate: 18,
				included_in_print_rate: 1,
			},
		];
		const baseItem = {
			item_code: "INCL-ROUNDED",
			item_name: "Rounded Inclusive Item",
			qty: 1,
			rate: 0.01,
			net_amount: 0.01,
			net_rate: 0.01,
			base_net_rate: 37,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
		};

		expect(
			findLossRiskItems([{ ...baseItem, valuation_rate: 35 }], {
				taxes,
				currencyPrecision: 2,
			}),
		).toEqual([]);

		const blocked = findLossRiskItems([{ ...baseItem, valuation_rate: 38 }], {
			taxes,
			currencyPrecision: 2,
		});
		expect(blocked).toHaveLength(1);
		expect(blocked[0]?.itemCode).toBe("INCL-ROUNDED");
	});

	it("compares selling rate and cost floor at ERPNext currency precision rather than fixed 0.0001 epsilon", () => {
		// At 2 decimals, 10.00 vs 10.004 both round to 10.00 (passes), whereas 10.00 vs 10.006 rounds to 10.00 < 10.01 (blocks).
		expect(
			getItemLossRisk(
				{
					item_code: "PREC-2DP-OK",
					qty: 1,
					rate: 10.0,
					valuation_rate: 10.004,
				},
				{ currencyPrecision: 2 },
			),
		).toBeNull();

		expect(
			getItemLossRisk(
				{
					item_code: "PREC-2DP-LOW",
					qty: 1,
					rate: 10.0,
					valuation_rate: 10.006,
				},
				{ currencyPrecision: 2 },
			)?.belowCost,
		).toBe(true);

		// At 0 decimals (UGX), 4500 vs 4500.4 both round to 4500 (passes), whereas 4500 vs 4500.6 blocks.
		expect(
			getItemLossRisk(
				{
					item_code: "PREC-0DP-OK",
					qty: 1,
					rate: 4500,
					valuation_rate: 4500.4,
				},
				{ currencyPrecision: 0 },
			),
		).toBeNull();

		expect(
			getItemLossRisk(
				{
					item_code: "PREC-0DP-LOW",
					qty: 1,
					rate: 4500,
					valuation_rate: 4500.6,
				},
				{ currencyPrecision: 0 },
			)?.belowCost,
		).toBe(true);
	});

	it("does not multiply stock-UOM _buying_prices_by_uom fallback by conversion_factor when price_not_uom_dependent=1", () => {
		const floor = getItemCostFloor({
			item_code: "PNUD-TABLE",
			uom: "Box",
			stock_uom: "Each",
			conversion_factor: 10,
			currency: "USD",
			company_currency: "USD",
			conversion_rate: 1,
			_buying_prices_by_uom: {
				Each: {
					price_list_rate: 8,
					base_price_list_rate: 8,
					currency: "USD",
					price_not_uom_dependent: 1,
				},
			},
		});
		expect(floor?.rate).toBeCloseTo(8);
	});

	it("applies buying_exchange_rate on raw foreign buying scalars even when source currency equals invoice currency", () => {
		// Raw USD 10 trade_price @ buying FX 4,050 vs USD invoice @ selling FX 3,700 (UGX company):
		// floor in USD is 10 * 4050 / 3700 = 10.945946 -> USD 10.50 MUST block, USD 11.00 passes.
		const baseItem = {
			item_code: "USD-RAW-SCALAR",
			item_name: "USD Raw Scalar Item",
			qty: 1,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			buying_price_currency: "USD",
			buying_exchange_rate: 4050,
			trade_price: 10,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
		};

		const risk = getItemLossRisk(
			{ ...baseItem, rate: 10.5 },
			{ currencyPrecision: 2 },
		);
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBeCloseTo((10 * 4050) / 3700, 4);

		expect(
			getItemLossRisk(
				{ ...baseItem, rate: 11.0 },
				{ currencyPrecision: 2 },
			),
		).toBeNull();

		// Explicit invalid buying_exchange_rate (<= 0) on a foreign buying scalar MUST fail closed.
		for (const badFx of [0, -1]) {
			expect(
				getItemCostFloor({ ...baseItem, buying_exchange_rate: badFx }),
			).toBeNull();
		}
	});

	it("accepts valid ERPNext carried tax-group rounding [0.03, 0.04] while rejecting forged or cross-group inflation", () => {
		const taxes = [
			{
				charge_type: "On Net Total",
				account_head: "VAT 18% - CO",
				rate: 18,
				included_in_print_rate: 1,
			},
		];
		const row1 = {
			item_code: "CARRY-1",
			qty: 1,
			rate: 0.04,
			amount: 0.04,
			net_rate: 0.03,
			net_amount: 0.03,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			valuation_rate: 100,
		};
		const row2 = {
			item_code: "CARRY-2",
			qty: 1,
			rate: 0.04,
			amount: 0.04,
			net_rate: 0.04,
			net_amount: 0.04,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			valuation_rate: 140,
		};

		// Two same-tax-group rows: native [0.03, 0.04] sums to 0.07 == round(0.08/1.18, 2).
		expect(
			findLossRiskItems([row1, row2], { taxes, currencyPrecision: 2 }),
		).toEqual([]);

		// Single forged row claiming 0.04 (unrounded 0.0339 -> UGX 125.42 < 140) MUST still block.
		const singleBlocked = findLossRiskItems([row2], {
			taxes,
			currencyPrecision: 2,
		});
		expect(singleBlocked).toHaveLength(1);
		expect(singleBlocked[0]?.itemCode).toBe("CARRY-2");

		// Cross-tax-group tampering (0% tax row shaved 10.00 -> 9.99 to fund +0.01 on 18% VAT row) MUST still block.
		const taxFreeShaved = {
			item_code: "TAX-FREE",
			qty: 1,
			rate: 10.0,
			amount: 10.0,
			net_rate: 9.99,
			net_amount: 9.99,
			item_tax_rate: JSON.stringify({ "VAT 18% - CO": 0 }),
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			valuation_rate: 1000,
		};
		const crossGroupBlocked = findLossRiskItems([taxFreeShaved, row2], {
			taxes,
			currencyPrecision: 2,
		});
		expect(crossGroupBlocked).toHaveLength(1);
		expect(crossGroupBlocked[0]?.itemCode).toBe("CARRY-2");
	});

	it("matches Frappe banker's rounding (legacy default at 0dp and explicit Banker's Rounding at 2dp)", () => {
		// Default Frappe "Banker's Rounding (legacy)" at 0 decimals: 42.5 -> 42 (passes), 43.5 -> 44 (blocks).
		expect(
			getItemLossRisk(
				{
					item_code: "BANKER-EVEN-0DP",
					qty: 1,
					rate: 42,
					valuation_rate: 42.5,
				},
				{ currencyPrecision: 0 },
			),
		).toBeNull();

		expect(
			getItemLossRisk(
				{
					item_code: "BANKER-ODD-0DP",
					qty: 1,
					rate: 43,
					valuation_rate: 43.5,
				},
				{ currencyPrecision: 0 },
			)?.belowCost,
		).toBe(true);

		// At 2 decimals, default legacy mode avoids binary FP drift (10.005 -> 10.01, 10.015 -> 10.02).
		expect(
			getItemLossRisk(
				{
					item_code: "LEGACY-2DP-005",
					qty: 1,
					rate: 10.0,
					valuation_rate: 10.005,
				},
				{ currencyPrecision: 2 },
			)?.belowCost,
		).toBe(true);
		expect(
			getItemLossRisk(
				{
					item_code: "LEGACY-2DP-015",
					qty: 1,
					rate: 10.01,
					valuation_rate: 10.015,
				},
				{ currencyPrecision: 2 },
			)?.belowCost,
		).toBe(true);

		// Explicit Frappe "Banker's Rounding" applies round-half-to-even at 2 decimals too (10.005 -> 10.00, 10.015 -> 10.02).
		const previousFrappe = (globalThis as any).frappe;
		(globalThis as any).frappe = {
			...(previousFrappe || {}),
			boot: {
				...(previousFrappe?.boot || {}),
				sysdefaults: {
					...(previousFrappe?.boot?.sysdefaults || {}),
					rounding_method: "Banker's Rounding",
				},
			},
		};
		try {
			expect(
				getItemLossRisk(
					{
						item_code: "BANKER-2DP-EVEN",
						qty: 1,
						rate: 10.0,
						valuation_rate: 10.005,
					},
					{ currencyPrecision: 2 },
				),
			).toBeNull();
			expect(
				getItemLossRisk(
					{
						item_code: "BANKER-2DP-ODD",
						qty: 1,
						rate: 10.01,
						valuation_rate: 10.015,
					},
					{ currencyPrecision: 2 },
				)?.belowCost,
			).toBe(true);
		} finally {
			if (previousFrappe === undefined) {
				delete (globalThis as any).frappe;
			} else {
				(globalThis as any).frappe = previousFrappe;
			}
		}
	});

	it("does not turn explicit invalid buying_exchange_rate: 0 into _company_buying_price in useItemCurrency", () => {
		const { applyCurrencyConversionToItem } = useItemCurrency();
		const item: Record<string, unknown> = {
			item_code: "INVALID-BUY-FX-ITEM",
			qty: 1,
			rate: 12,
			price_list_rate: 12,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			buying_price_currency: "USD",
			buying_exchange_rate: 0,
			trade_price: 10,
			buying_rate: 10,
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
		};

		applyCurrencyConversionToItem(item, {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "USD" },
			price_list_currency: "USD",
			selected_currency: "USD",
			exchange_rate: 1,
			conversion_rate: 3700,
			currency_precision: 2,
		});

		expect(item._company_buying_price).toBeUndefined();
		expect(item.base_buying_rate).toBeUndefined();
		expect(item.buying_exchange_rate).toBe(0);
		expect(getItemCostFloor(item)).toBeNull();
	});

	it("flags legitimate zero-net carried inclusive-tax row on undiscounted invoice while ignoring all-zero draft", () => {
		const taxes = [
			{
				charge_type: "On Net Total",
				account_head: "VAT 18% - CO",
				rate: 18,
				included_in_print_rate: 1,
			},
		];
		// Five USD 0.01 lines @ 18% inclusive VAT -> ERPNext carried rounding [0.01, 0.01, 0.01, 0, 0.01].
		const carriedNets = [0.01, 0.01, 0.01, 0, 0.01];
		const items = carriedNets.map((net, idx) => ({
			item_code: `CARRY-ZERO-${idx + 1}`,
			item_name: `Carry Zero Line ${idx + 1}`,
			qty: 1,
			rate: 0.01,
			amount: 0.01,
			net_rate: net,
			net_amount: net,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			valuation_rate: 20,
		}));

		const risks = findLossRiskItems(items, { taxes, currencyPrecision: 2 });
		expect(risks).toHaveLength(1);
		expect(risks[0]?.itemCode).toBe("CARRY-ZERO-4");
		expect(risks[0]?.sellingRate).toBe(0);

		// Uncalculated all-zero draft [0, 0, 0, 0, 0] does NOT reconcile and falls back to computed 0.00847 (UGX 31.35 >= 20).
		const uncalculatedDraft = items.map((row) => ({
			...row,
			net_rate: 0,
			net_amount: 0,
		}));
		expect(
			findLossRiskItems(uncalculatedDraft, { taxes, currencyPrecision: 2 }),
		).toEqual([]);
	});

	it("reconciles independently rounded inclusive-tax lines when round_row_wise_tax is enabled", () => {
		const taxes = [
			{
				charge_type: "On Net Total",
				account_head: "VAT 18% - CO",
				rate: 18,
				included_in_print_rate: 1,
			},
		];
		// Two USD 0.03 lines @ 18% VAT: unrounded 0.02542 each -> flt(0.02542, 2) == 0.03 each under round_row_wise_tax=1.
		// At FX 3,700 each earns UGX 111 >= UGX 100 floor (whereas unrounded 94.07 < 100).
		const items = [1, 2].map((idx) => ({
			item_code: `ROW-WISE-${idx}`,
			qty: 1,
			rate: 0.03,
			amount: 0.03,
			net_rate: 0.03,
			net_amount: 0.03,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			valuation_rate: 100,
		}));

		// Enabled via option -> passes.
		expect(
			findLossRiskItems(items, {
				taxes,
				currencyPrecision: 2,
				roundRowWiseTax: true,
			}),
		).toEqual([]);

		// Enabled via frappe.boot.sysdefaults.round_row_wise_tax = 1 -> passes.
		const previousFrappe = (globalThis as any).frappe;
		(globalThis as any).frappe = {
			...(previousFrappe || {}),
			boot: {
				...(previousFrappe?.boot || {}),
				sysdefaults: {
					...(previousFrappe?.boot?.sysdefaults || {}),
					round_row_wise_tax: 1,
				},
			},
		};
		try {
			expect(
				findLossRiskItems(items, { taxes, currencyPrecision: 2 }),
			).toEqual([]);
		} finally {
			if (previousFrappe === undefined) {
				delete (globalThis as any).frappe;
			} else {
				(globalThis as any).frappe = previousFrappe;
			}
		}

		// Disabled (default carried group rounding) -> [0.03, 0.03] sum 0.06 vs 0.0508 still blocks both lines.
		expect(
			findLossRiskItems(items, { taxes, currencyPrecision: 2 }),
		).toHaveLength(2);

		// Even with roundRowWiseTax: true, forged USD 0.04 claiming net_amount=0.04 (flt(0.0339, 2)=0.03) still blocks.
		const forged = [
			{
				item_code: "ROW-WISE-FORGED",
				qty: 1,
				rate: 0.04,
				amount: 0.04,
				net_rate: 0.04,
				net_amount: 0.04,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 140,
			},
		];
		expect(
			findLossRiskItems(forged, {
				taxes,
				currencyPrecision: 2,
				roundRowWiseTax: true,
			}),
		).toHaveLength(1);
	});

	it("preserves item.company_currency and foreign conversion_rate in useItemCurrency when context omits company on a foreign till", () => {
		const { applyCurrencyConversionToItem } = useItemCurrency();
		const item: Record<string, any> = {
			item_code: "USD-TILL-UGX-CO",
			qty: 1,
			rate: 10.5,
			price_list_rate: 10.5,
			original_rate: 10.5,
			original_currency: "USD",
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			buying_price_list: "USD Buying",
			buying_price_currency: "USD",
			buying_exchange_rate: 4050,
			base_buying_rate: 40500,
			trade_price: 40500 / 3700,
			buying_rate: 40500 / 3700,
			_buying_prices_by_uom: {
				Nos: {
					price_list_rate: 10,
					base_price_list_rate: 40500,
					currency: "USD",
					price_list: "USD Buying",
				},
			},
			uom: "Nos",
			stock_uom: "Nos",
			conversion_factor: 1,
		};

		// Context omits `company` (and omits `conversion_rate`) on a fixed USD till:
		// must NOT overwrite item.company_currency ("UGX") with pos_profile.currency ("USD")
		// or replace conversion_rate (3700) with 1.
		applyCurrencyConversionToItem(item, {
			pos_profile: { currency: "USD" },
			price_list_currency: "USD",
			selected_currency: "USD",
			exchange_rate: 1,
			currency_precision: 2,
		});

		expect(item.company_currency).toBe("UGX");
		expect(item.conversion_rate).toBe(3700);
		expect(item.trade_price).toBeCloseTo(40500 / 3700, 4);
		const risk = getItemLossRisk(item, { currencyPrecision: 2 });
		expect(risk?.belowCost).toBe(true);
		expect(risk?.costRate).toBeCloseTo(40500 / 3700, 4);
	});

	it("fails closed when API emits buying_exchange_rate: null and base_price_list_rate: null for foreign buying currency matching invoice currency", () => {
		// Exact shape emitted by item_fetchers.py:665 & :708 when for_buying FX is unavailable.
		const apiTableItem = {
			item_code: "API-NULL-FX-TABLE",
			qty: 1,
			rate: 10.5,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			uom: "Nos",
			stock_uom: "Nos",
			valuation_rate: 100,
			buying_price_list: "USD Buying",
			buying_price_currency: "USD",
			buying_exchange_rate: null,
			base_buying_rate: null,
			trade_price: null,
			buying_rate: null,
			buying_rate_currency: null,
			_buying_prices_by_uom: {
				Nos: {
					price_list_rate: 10,
					base_price_list_rate: null,
					currency: "USD",
					price_list: "USD Buying",
					price_not_uom_dependent: 0,
				},
			},
		};
		expect(getItemCostFloor(apiTableItem)).toBeNull();
		expect(getItemLossRisk(apiTableItem, { currencyPrecision: 2 })).toBeNull();

		// Raw scalar paths (trade_price / buying_rate / _source_buying_price) with buying_exchange_rate: null
		// must also fail closed instead of using raw USD 10 as a 1:1 USD floor.
		const apiScalarItem = {
			item_code: "API-NULL-FX-SCALAR",
			qty: 1,
			rate: 10.5,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			uom: "Nos",
			stock_uom: "Nos",
			valuation_rate: 100,
			buying_price_currency: "USD",
			buying_rate_currency: "USD",
			buying_exchange_rate: null,
			base_buying_rate: null,
			trade_price: 10,
			buying_rate: 10,
		};
		expect(getItemCostFloor(apiScalarItem)).toBeNull();

		const apiSourceScalarItem = {
			item_code: "API-NULL-FX-SOURCE",
			qty: 1,
			rate: 10.5,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			uom: "Nos",
			stock_uom: "Nos",
			valuation_rate: 100,
			buying_price_currency: "USD",
			buying_exchange_rate: null,
			base_buying_rate: null,
			_source_buying_currency: "USD",
			_source_buying_price: 10,
		};
		expect(getItemCostFloor(apiSourceScalarItem)).toBeNull();
	});

	it("reads ERPNext browser frappe.flags.round_row_wise_tax when reconciling row-wise inclusive tax", () => {
		const taxes = [
			{
				charge_type: "On Net Total",
				account_head: "VAT 18% - CO",
				rate: 18,
				included_in_print_rate: 1,
			},
		];
		const items = [1, 2].map((idx) => ({
			item_code: `FLAG-RW-${idx}`,
			qty: 1,
			rate: 0.03,
			amount: 0.03,
			net_rate: 0.03,
			net_amount: 0.03,
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			valuation_rate: 100,
		}));

		const previousFrappe = (globalThis as any).frappe;
		(globalThis as any).frappe = {
			flags: { round_row_wise_tax: 1 },
		};
		try {
			expect(
				findLossRiskItems(items, { taxes, currencyPrecision: 2 }),
			).toEqual([]);
		} finally {
			if (previousFrappe === undefined) {
				delete (globalThis as any).frappe;
			} else {
				(globalThis as any).frappe = previousFrappe;
			}
		}
	});

	it("rounds gross line amount before extracting inclusive tax on fractional quantities matching ERPNext taxes_and_totals.py:287", () => {
		const taxes = [
			{
				charge_type: "On Net Total",
				account_head: "VAT 18% - CO",
				rate: 18,
				included_in_print_rate: 1,
			},
		];
		// qty=0.6, rate=0.03 -> ERPNext rounds amount=flt(0.018, 2)=0.02, net_amount=flt(0.02/1.18, 2)=0.02,
		// net_rate=flt(0.02/0.6, 2)=0.03 -> base_net_rate=UGX 111 >= UGX 100 floor.
		const validFractional = [
			{
				item_code: "FRAC-INCL-VAT",
				qty: 0.6,
				rate: 0.03,
				amount: 0.02,
				net_amount: 0.02,
				net_rate: 0.03,
				base_net_amount: 74,
				base_net_rate: 111,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				valuation_rate: 100,
			},
		];
		expect(
			findLossRiskItems(validFractional, { taxes, currencyPrecision: 2 }),
		).toEqual([]);

		// Forged fractional line claiming net_amount=0.03 (above rounded gross 0.02) must still block.
		const forgedFractional = [
			{
				...validFractional[0],
				item_code: "FRAC-INCL-VAT-FORGED",
				net_amount: 0.03,
				net_rate: 0.05,
			},
		];
		expect(
			findLossRiskItems(forgedFractional, { taxes, currencyPrecision: 2 }),
		).toHaveLength(1);
	});

	it("preserves ERPNext unit net_rate on valid weighed sales (0.10 kg x $1.03/kg vs $1.02/kg floor) without dividing rounded net_amount by qty", () => {
		// ERPNext taxes_and_totals.py:275-291: with no inclusive tax and no header discount,
		// amount = net_amount = flt(1.03 * 0.10, 2) = 0.10 while net_rate stays 1.03.
		const validWeighed = [
			{
				item_code: "WEIGHED-OK",
				qty: 0.1,
				rate: 1.03,
				amount: 0.1,
				net_amount: 0.1,
				net_rate: 1.03,
				base_net_amount: 0.1,
				base_net_rate: 1.03,
				currency: "USD",
				company_currency: "USD",
				conversion_rate: 1,
				valuation_rate: 1.02,
			},
		];
		expect(
			findLossRiskItems(validWeighed, { currencyPrecision: 2 }),
		).toEqual([]);
		expect(
			getItemLossRisk(validWeighed[0], { currencyPrecision: 2 }),
		).toBeNull();

		// Below-floor weighed sale ($1.01/kg vs $1.02/kg floor) must still block.
		const belowCostWeighed = [
			{
				...validWeighed[0],
				item_code: "WEIGHED-BELOW",
				rate: 1.01,
				net_rate: 1.01,
				base_net_rate: 1.01,
			},
		];
		expect(
			findLossRiskItems(belowCostWeighed, { currencyPrecision: 2 }),
		).toHaveLength(1);

		// Tampered lower net_amount ($0.05 instead of rounded $0.10) claiming net_rate=$1.03 must still block.
		const tamperedAmount = [
			{
				...validWeighed[0],
				item_code: "WEIGHED-TAMPERED-AMT",
				net_amount: 0.05,
			},
		];
		expect(
			findLossRiskItems(tamperedAmount, { currencyPrecision: 2 }),
		).toHaveLength(1);

		// Forged inflated net_rate ($1.50 > gross $1.03) against $1.05 floor must still block.
		const forgedRate = [
			{
				...validWeighed[0],
				item_code: "WEIGHED-FORGED-RATE",
				net_rate: 1.5,
				base_net_rate: 1.5,
				valuation_rate: 1.05,
			},
		];
		expect(
			findLossRiskItems(forgedRate, { currencyPrecision: 2 }),
		).toHaveLength(1);
	});
});
