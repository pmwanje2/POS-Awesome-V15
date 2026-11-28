import { ref } from "vue";
import { describe, expect, it, vi } from "vitest";

import invoiceSource from "../src/posapp/components/pos/Invoice.vue?raw";
import { useItemCurrency } from "../src/posapp/composables/pos/items/useItemCurrency";
import { registerItemsSelectorEvents } from "../src/posapp/composables/pos/items/useItemsSelectorEvents";

function extractSyncExchangeRate(): (_this: Record<string, any>) => void {
	const match = /\n[ \t]*sync_exchange_rate\(\)\s*\{/.exec(invoiceSource);
	if (!match) {
		throw new Error("sync_exchange_rate() definition not found in Invoice.vue");
	}
	const bodyStart = match.index + match[0].length - 1;
	let depth = 0;
	let bodyEnd = -1;
	for (let i = bodyStart; i < invoiceSource.length; i += 1) {
		const ch = invoiceSource[i];
		if (ch === "{") depth += 1;
		else if (ch === "}") {
			depth -= 1;
			if (depth === 0) {
				bodyEnd = i;
				break;
			}
		}
	}
	if (bodyEnd === -1) {
		throw new Error("Unterminated sync_exchange_rate() in Invoice.vue");
	}
	const body = invoiceSource.slice(bodyStart + 1, bodyEnd);
	const fn = new Function(body) as () => void;
	return (vm: Record<string, any>) => fn.call(vm);
}

describe("Invoice.vue sync_exchange_rate", () => {
	it("emits 0 instead of 1 when foreign conversion_rate is cleared so buying cost is not poisoned at 1:1", () => {
		const syncExchangeRate = extractSyncExchangeRate();
		const handlers = new Map<string, (..._args: any[]) => void>();
		const eventBus = {
			on: (event: string, handler: (..._args: any[]) => void) => {
				handlers.set(event, handler);
			},
			off: (event: string) => {
				handlers.delete(event);
			},
			emit: (event: string, payload: unknown) => {
				handlers.get(event)?.(payload);
			},
		};
		const selectedCurrency = ref("USD");
		const selectedExchangeRate = ref(1);
		const selectedConversionRate = ref(3700);
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

		const vm: Record<string, any> = {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "USD",
			selected_currency: "USD",
			exchange_rate: 1,
			conversion_rate: "",
			currency_precision: 2,
			eventBus,
			update_item_rates: vi.fn(),
			update_delivery_charges: vi.fn(),
		};

		syncExchangeRate(vm);

		expect(vm.conversion_rate).toBe(0);
		expect(selectedConversionRate.value).toBe(0);

		const { applyCurrencyConversionToItem } = useItemCurrency();
		const item: Record<string, unknown> = {
			item_code: "USD-CLEARED-INPUT",
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

		vm.conversion_rate = 3700;
		syncExchangeRate(vm);
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

		cleanup();
	});

	it("still defaults cleared same-currency conversion_rate to 1", () => {
		const syncExchangeRate = extractSyncExchangeRate();
		const emitted: unknown[] = [];
		const vm: Record<string, any> = {
			company: { default_currency: "UGX" },
			pos_profile: { currency: "UGX" },
			price_list_currency: "UGX",
			selected_currency: "UGX",
			exchange_rate: "",
			conversion_rate: "",
			currency_precision: 0,
			eventBus: {
				emit: (_event: string, payload: unknown) => {
					emitted.push(payload);
				},
			},
			update_item_rates: vi.fn(),
			update_delivery_charges: vi.fn(),
		};

		syncExchangeRate(vm);

		expect(vm.exchange_rate).toBe(1);
		expect(vm.conversion_rate).toBe(1);
		expect(emitted).toEqual([
			{ currency: "UGX", exchange_rate: 1, conversion_rate: 1 },
		]);
	});
});
