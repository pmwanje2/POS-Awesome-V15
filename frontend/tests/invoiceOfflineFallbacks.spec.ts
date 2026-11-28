// @vitest-environment jsdom

import "fake-indexeddb/auto";

import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { ref } from "vue";

vi.mock("../src/posapp/composables/pos/shared/useStockUtils", () => ({
	useStockUtils: () => ({
		calc_stock_qty: vi.fn(),
	}),
}));

vi.mock("../src/posapp/composables/pos/items/useItemAddition", () => ({
	useItemAddition: () => ({
		removeItem: vi.fn(),
		addItem: vi.fn(),
	}),
}));

describe("invoice offline fallbacks", () => {
	beforeEach(() => {
		vi.resetModules();
		setActivePinia(createPinia());
		(window as any).__ = (value: string) => value;
		(window as any).frappe = {
			call: vi.fn(),
			datetime: {
				nowdate: () => "2026-04-09",
				get_today: () => "2026-04-09",
			},
		};
	});

	it("uses cached delivery charges when the live request fails", async () => {
		const offlineCache = await import("../src/offline/cache");
		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const uiStore = useUIStore();
		uiStore.setPosProfile({
			name: "POS-1",
			company: "Test Company",
			currency: "PKR",
		} as any);

		offlineCache.saveDeliveryChargesCache("POS-1", "CUST-1", [
			{ name: "DEL-1", rate: 250 },
		]);
		(window as any).frappe.call = vi
			.fn()
			.mockRejectedValue(new Error("offline"));

		const { useInvoiceItems } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceItems"
		);
		const invoiceItems = useInvoiceItems(ref("Invoice"));

		await invoiceItems.fetch_delivery_charges("CUST-1");

		expect(invoiceItems.delivery_charges.value).toEqual([
			{ name: "DEL-1", rate: 250 },
		]);
	});

	it("uses cached customer addresses when address lookup fails", async () => {
		const offlineCache = await import("../src/offline/cache");
		offlineCache.saveCustomerAddressesCache("CUST-1", [
			{ name: "ADDR-1", address_title: "Main Address" },
		]);
		(window as any).frappe.call = vi.fn(({ error }: any) => {
			error?.(new Error("offline"));
		});

		const { useInvoiceDetails } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceDetails"
		);
		const invoiceDetails = useInvoiceDetails({
			invoiceDoc: ref({ customer: "CUST-1" }),
			posProfile: ref({ name: "POS-1" }),
			invoiceType: ref("Invoice"),
		});

		invoiceDetails.get_addresses();

		expect(invoiceDetails.addresses.value).toEqual([
			{
				name: "ADDR-1",
				address_title: "Main Address",
				display_title: "Main Address",
			},
		]);
	});

	it("uses cached currency data when currency endpoints fail", async () => {
		const offlineCache = await import("../src/offline/cache");
		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const uiStore = useUIStore();
		uiStore.setPosProfile({
			name: "POS-1",
			company: "Test Company",
			currency: "PKR",
			selling_price_list: "Retail",
			posa_decimal_precision: "2",
		} as any);
		uiStore.setCompanyDoc({
			default_currency: "PKR",
		});

		offlineCache.saveCurrencyOptionsCache("POS-1", [
			{ value: "PKR", title: "PKR" },
			{ value: "USD", title: "USD" },
		]);
		offlineCache.savePriceListMetaCache("POS-1", {
			price_lists: ["Retail"],
			price_list_currency: "USD",
		});
		offlineCache.saveExchangeRateCache({
			profileName: "POS-1",
			company: "Test Company",
			fromCurrency: "USD",
			toCurrency: "EUR",
			date: "2026-04-09",
			purpose: "for_selling",
			exchange_rate: 1.5,
		});
		offlineCache.saveExchangeRateCache({
			profileName: "POS-1",
			company: "Test Company",
			fromCurrency: "EUR",
			toCurrency: "PKR",
			date: "2026-04-09",
			purpose: "for_selling",
			exchange_rate: 300,
		});

		(window as any).frappe.call = vi
			.fn()
			.mockRejectedValue(new Error("offline"));

		const { useInvoiceCurrency } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceCurrency"
		);
		const invoiceCurrency = useInvoiceCurrency();

		const currencies = await invoiceCurrency.fetch_available_currencies();
		invoiceCurrency.selected_currency.value = "EUR";
		invoiceCurrency.price_list_currency.value = "USD";
		await invoiceCurrency.update_currency_and_rate();

		expect(currencies).toEqual([
			{ value: "PKR", title: "PKR" },
			{ value: "USD", title: "USD" },
		]);
		expect(invoiceCurrency.exchange_rate.value).toBe(1.5);
		expect(invoiceCurrency.conversion_rate.value).toBe(300);
	});

	it("resets the next invoice to the POS Profile currency", async () => {
		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const uiStore = useUIStore();
		uiStore.setPosProfile({
			name: "POS-1",
			company: "Test Company",
			currency: "PKR",
			posa_decimal_precision: "2",
		} as any);
		uiStore.setCompanyDoc({ default_currency: "PKR" });

		const { useInvoiceCurrency } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceCurrency"
		);
		const invoiceCurrency = useInvoiceCurrency();
		invoiceCurrency.selected_currency.value = "USD";
		invoiceCurrency.exchange_rate.value = 0.0035;
		invoiceCurrency.conversion_rate.value = 285;

		await invoiceCurrency.reset_currency_to_default();

		expect(invoiceCurrency.selected_currency.value).toBe("PKR");
		expect(invoiceCurrency.exchange_rate.value).toBe(1);
		expect(invoiceCurrency.conversion_rate.value).toBe(1);
	});

	it("rebuilds cart rates from the original price-list rate when currency is toggled back", async () => {
		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const { useInvoiceStore } = await import("../src/posapp/stores/invoiceStore");
		const { useInvoiceCurrency } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceCurrency"
		);

		const uiStore = useUIStore();
		uiStore.setPosProfile({
			name: "POS-1",
			company: "Test Company",
			currency: "PKR",
			selling_price_list: "Retail",
			posa_decimal_precision: "2",
		} as any);
		uiStore.setCompanyDoc({
			default_currency: "PKR",
		});

		const invoiceStore = useInvoiceStore();
		invoiceStore.addItem({
			item_code: "ITEM-10",
			qty: 1,
			rate: 0.04,
			price_list_rate: 0.04,
			base_rate: 11.2,
			base_price_list_rate: 11.2,
			original_rate: 10,
			original_currency: "PKR",
			conversion_factor: 1,
			discount_amount: 0,
			base_discount_amount: 0,
			amount: 0.04,
			base_amount: 11.2,
			posa_row_id: "row-10",
		} as any);

		const invoiceCurrency = useInvoiceCurrency();
		invoiceCurrency.price_list_currency.value = "PKR";
		invoiceCurrency.selected_currency.value = "USD";
		invoiceCurrency.exchange_rate.value = 0.0035714285714285713;
		invoiceCurrency.conversion_rate.value = 280;

		await invoiceCurrency.update_item_rates();

		expect(invoiceStore.items[0].base_rate).toBe(10);
		expect(invoiceStore.items[0].rate).toBe(0.04);

		invoiceCurrency.selected_currency.value = "PKR";
		invoiceCurrency.exchange_rate.value = 1;
		invoiceCurrency.conversion_rate.value = 1;

		await invoiceCurrency.update_item_rates();

		expect(invoiceStore.items[0].base_rate).toBe(10);
		expect(invoiceStore.items[0].rate).toBe(10);
		expect(invoiceStore.grossTotal).toBe(10);
	});

	it("does not poison _company_buying_price with 1:1 when price-list FX is missing in useInvoiceCurrency", async () => {
		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const { useInvoiceStore } = await import("../src/posapp/stores/invoiceStore");
		const { useInvoiceCurrency } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceCurrency"
		);

		const uiStore = useUIStore();
		uiStore.setPosProfile({
			name: "POS-1",
			company: "Test Company",
			currency: "UGX",
			selling_price_list: "USD List",
			posa_decimal_precision: "2",
		} as any);
		uiStore.setCompanyDoc({
			default_currency: "UGX",
		});

		const invoiceStore = useInvoiceStore();
		invoiceStore.addItem({
			item_code: "ITEM-USD-NOFX",
			qty: 1,
			rate: 15,
			price_list_rate: 15,
			original_rate: 15,
			original_currency: "USD",
			currency: "USD",
			buying_rate_currency: "USD",
			trade_price: 10,
			buying_rate: 10,
			conversion_factor: 1,
			discount_amount: 0,
			base_discount_amount: 0,
			amount: 15,
			posa_row_id: "row-nofx",
		} as any);

		const invoiceCurrency = useInvoiceCurrency();
		invoiceCurrency.price_list_currency.value = "USD";
		invoiceCurrency.selected_currency.value = "USD";
		invoiceCurrency.exchange_rate.value = 1;
		invoiceCurrency.conversion_rate.value = 0;

		await invoiceCurrency.update_item_rates();

		expect(invoiceStore.items[0]._company_buying_price).toBeUndefined();
		expect(invoiceStore.items[0].base_buying_rate).toBeUndefined();

		invoiceCurrency.conversion_rate.value = 3700;
		await invoiceCurrency.update_item_rates();

		expect(invoiceStore.items[0]._company_buying_price).toBeCloseTo(37000);
		expect(invoiceStore.items[0].base_buying_rate).toBeCloseTo(37000);
	});

	it("does not synthesize _company_buying_price from selling FX when buying_exchange_rate is explicitly 0 in useInvoiceCurrency", async () => {
		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const { useInvoiceStore } = await import("../src/posapp/stores/invoiceStore");
		const { useInvoiceCurrency } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceCurrency"
		);
		const { getItemCostFloor } = await import(
			"../src/posapp/utils/lossPrevention"
		);

		const uiStore = useUIStore();
		uiStore.setPosProfile({
			name: "POS-USD-BAD-FX",
			company: "Test Company",
			currency: "USD",
			selling_price_list: "USD List",
			posa_decimal_precision: "2",
		} as any);
		uiStore.setCompanyDoc({ default_currency: "UGX" });

		const invoiceStore = useInvoiceStore();
		invoiceStore.addItem({
			item_code: "ITEM-BAD-BUY-FX",
			qty: 1,
			rate: 12,
			price_list_rate: 12,
			original_rate: 12,
			original_currency: "USD",
			currency: "USD",
			company_currency: "UGX",
			conversion_rate: 3700,
			buying_price_currency: "USD",
			buying_exchange_rate: 0,
			trade_price: 10,
			buying_rate: 10,
			conversion_factor: 1,
			discount_amount: 0,
			base_discount_amount: 0,
			amount: 12,
			posa_row_id: "row-bad-buy-fx",
		} as any);

		const invoiceCurrency = useInvoiceCurrency();
		invoiceCurrency.price_list_currency.value = "USD";
		invoiceCurrency.selected_currency.value = "USD";
		invoiceCurrency.exchange_rate.value = 1;
		invoiceCurrency.conversion_rate.value = 3700;

		await invoiceCurrency.update_item_rates();

		expect(invoiceStore.items[0]._company_buying_price).toBeUndefined();
		expect(invoiceStore.items[0].base_buying_rate).toBeUndefined();
		expect(invoiceStore.items[0].buying_exchange_rate).toBe(0);
		expect(getItemCostFloor(invoiceStore.items[0])).toBeNull();
	});

	it("preserves company_currency and selling conversion_rate on a foreign till when companyDoc is unloaded", async () => {
		const { db } = await import("../src/offline/db");
		if (!db.isOpen()) await db.open();
		await db.table("currency_rate_records").clear();
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-USD-UGX-SELL-UNLOADED",
				profile_name: "POS-USD-UNLOADED",
				company: "Uganda Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3700,
				date: "2026-04-09",
				for_buying: 0,
				for_selling: 1,
				modified: "2026-04-09T10:00:00",
			},
		]);

		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const { useInvoiceStore } = await import("../src/posapp/stores/invoiceStore");
		const { useInvoiceCurrency } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceCurrency"
		);
		const { getItemLossRisk } = await import(
			"../src/posapp/utils/lossPrevention"
		);

		const uiStore = useUIStore();
		// Company doc intentionally unloaded (null), while profile & cart row carry company_currency = UGX.
		uiStore.setCompanyDoc(null);
		uiStore.setPosProfile({
			name: "POS-USD-UNLOADED",
			company: "Uganda Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			posa_decimal_precision: "2",
		} as any);

		const invoiceStore = useInvoiceStore();
		invoiceStore.addItem({
			item_code: "ITEM-UNLOADED-CO",
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
			discount_amount: 0,
			base_discount_amount: 0,
			amount: 10.5,
			posa_row_id: "row-unloaded-co",
		} as any);

		const invoiceCurrency = useInvoiceCurrency();
		invoiceCurrency.selected_currency.value = "USD";
		invoiceCurrency.price_list_currency.value = "USD";
		await invoiceCurrency.update_currency_and_rate();
		await invoiceCurrency.update_item_rates();

		expect(invoiceCurrency.conversion_rate.value).toBe(3700);
		expect(invoiceStore.items[0].company_currency).toBe("UGX");
		expect(invoiceStore.items[0].conversion_rate).toBe(3700);
		expect(
			getItemLossRisk(invoiceStore.items[0], { currencyPrecision: 2 })
				?.belowCost,
		).toBe(true);

		// Even if profile.company_currency is also omitted and update_item_rates runs directly
		// before update_currency_and_rate, known row company_currency and conversion_rate must stay intact.
		uiStore.setPosProfile({
			name: "POS-USD-UNLOADED",
			company: "Uganda Co",
			currency: "USD",
			selling_price_list: "USD Selling",
			posa_decimal_precision: "2",
		} as any);
		const freshCurrency = useInvoiceCurrency();
		freshCurrency.selected_currency.value = "USD";
		freshCurrency.price_list_currency.value = "USD";
		await freshCurrency.update_item_rates();
		expect(invoiceStore.items[0].company_currency).toBe("UGX");
		expect(invoiceStore.items[0].conversion_rate).toBe(3700);
	});

	it("passes purpose for_selling on invoice currency refresh so a newer buying-only FX rate is not selected", async () => {
		const { db } = await import("../src/offline/db");
		if (!db.isOpen()) await db.open();
		await db.table("currency_rate_records").clear();
		await db.table("currency_rate_records").bulkPut([
			{
				name: "FX-USD-UGX-SELL-OLD",
				profile_name: "POS-SELL-PURPOSE",
				company: "Uganda Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 3700,
				date: "2026-04-08",
				for_buying: 0,
				for_selling: 1,
				modified: "2026-04-08T10:00:00",
			},
			{
				name: "FX-USD-UGX-BUY-NEW",
				profile_name: "POS-SELL-PURPOSE",
				company: "Uganda Co",
				from_currency: "USD",
				to_currency: "UGX",
				exchange_rate: 4050,
				date: "2026-04-09",
				for_buying: 1,
				for_selling: 0,
				modified: "2026-04-09T10:00:00",
			},
		]);

		const { useUIStore } = await import("../src/posapp/stores/uiStore");
		const { useInvoiceCurrency } = await import(
			"../src/posapp/composables/pos/invoice/useInvoiceCurrency"
		);

		const uiStore = useUIStore();
		uiStore.setPosProfile({
			name: "POS-SELL-PURPOSE",
			company: "Uganda Co",
			currency: "USD",
			company_currency: "UGX",
			selling_price_list: "USD Selling",
			posa_decimal_precision: "2",
		} as any);
		uiStore.setCompanyDoc({ default_currency: "UGX" });

		const invoiceCurrency = useInvoiceCurrency();
		invoiceCurrency.selected_currency.value = "USD";
		invoiceCurrency.price_list_currency.value = "USD";
		await invoiceCurrency.update_currency_and_rate();

		// Must select the for_selling=1 rate (3,700), never the newer buying-only rate (4,050).
		expect(invoiceCurrency.conversion_rate.value).toBe(3700);
	});
});
