import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/offline/index", () => ({
	getTaxTemplate: vi.fn(() => null),
	getTaxInclusiveSetting: vi.fn(() => false),
	isOffline: vi.fn(() => false),
}));

vi.mock("../src/posapp/components/pos/invoice_utils/currency", () => ({
	_getPlcConversionRate: vi.fn(() => 1),
}));

import { get_invoice_doc } from "../src/posapp/components/pos/invoice_utils/document";

describe("get_invoice_doc", () => {
	beforeEach(() => {
		(globalThis as any).flt = (value: unknown, precision = 2) => {
			const number = Number(value || 0);
			return Number(number.toFixed(precision));
		};
	});

	it("updates customer title when draft customer changes", () => {
		const context: any = {
			invoiceType: "Invoice",
			pos_profile: {
				company: "Test Company",
				name: "Main POS",
				currency: "PKR",
				ignore_pricing_rule: 1,
				payments: [{ mode_of_payment: "Cash", account: "Cash", type: "Cash", default: 1 }],
			},
			selected_currency: "PKR",
			conversion_rate: 1,
			company: { default_currency: "PKR" },
			price_list_currency: "PKR",
			get_price_list: () => "Standard Selling",
			customer_info: {
				customer: "CUST-NEW",
				customer_name: "New Customer",
			},
			customer: "CUST-NEW",
			isReturnInvoice: false,
			items: [],
			packed_items: [],
			Total: 0,
			subtotal: 0,
			additional_discount: 0,
			additional_discount_percentage: 0,
			roundAmount: (value: number) => value,
			pos_opening_shift: { name: "SHIFT-1" },
			posa_offers: [],
			posa_coupons: [],
			selected_delivery_charge: null,
			delivery_charges_rate: 0,
			posting_date_display: "2026-03-28",
			formatDateForBackend: (value: string) => value,
			invoice_doc: {
				name: "SINV-DRAFT",
				customer: "CUST-OLD",
				customer_name: "Old Customer",
				payments: [],
				taxes: [],
			},
		};

		const doc = get_invoice_doc(context);

		expect(doc.customer).toBe("CUST-NEW");
		expect(doc.customer_name).toBe("New Customer");
		expect(doc.ignore_pricing_rule).toBe(1);
	});

	it("clears stale party details when customer changes on a reused source doc", () => {
		const context: any = {
			invoiceType: "Invoice",
			pos_profile: {
				company: "Test Company",
				name: "Main POS",
				currency: "PKR",
				payments: [{ mode_of_payment: "Cash", account: "Cash", type: "Cash", default: 1 }],
			},
			selected_currency: "PKR",
			conversion_rate: 1,
			company: { default_currency: "PKR" },
			price_list_currency: "PKR",
			get_price_list: () => "Standard Selling",
			customer_info: {
				customer: "CUST-NEW",
				customer_name: "New Customer",
			},
			customer: "CUST-NEW",
			isReturnInvoice: false,
			items: [],
			packed_items: [],
			Total: 0,
			subtotal: 0,
			additional_discount: 0,
			additional_discount_percentage: 0,
			roundAmount: (value: number) => value,
			pos_opening_shift: { name: "SHIFT-1" },
			posa_offers: [],
			posa_coupons: [],
			selected_delivery_charge: null,
			delivery_charges_rate: 0,
			posting_date_display: "2026-03-28",
			formatDateForBackend: (value: string) => value,
			invoice_doc: {
				name: "ACC-SINV-0001",
				customer: "CUST-OLD",
				customer_name: "Old Customer",
				customer_address: "ADDR-OLD",
				shipping_address_name: "SHIP-OLD",
				contact_person: "CONT-OLD",
				address_display: "Old Address",
				contact_display: "Old Contact",
				contact_mobile: "0300",
				contact_email: "old@example.com",
				territory: "Old Territory",
				payments: [],
				taxes: [],
			},
		};

		const doc = get_invoice_doc(context);

		expect(doc.customer).toBe("CUST-NEW");
		expect(doc.customer_name).toBe("New Customer");
		expect(doc.customer_address).toBeNull();
		expect(doc.shipping_address_name).toBeNull();
		expect(doc.contact_person).toBeNull();
		expect(doc.address_display).toBeNull();
		expect(doc.contact_display).toBeNull();
		expect(doc.contact_mobile).toBeNull();
		expect(doc.contact_email).toBeNull();
		expect(doc.territory).toBeNull();
	});

	it("ignores mismatched cached customer info when resolving a different customer", () => {
		const context: any = {
			invoiceType: "Invoice",
			pos_profile: {
				company: "Test Company",
				name: "Main POS",
				currency: "PKR",
				payments: [{ mode_of_payment: "Cash", account: "Cash", type: "Cash", default: 1 }],
			},
			selected_currency: "PKR",
			conversion_rate: 1,
			company: { default_currency: "PKR" },
			price_list_currency: "PKR",
			get_price_list: () => "Standard Selling",
			customer_info: {
				customer: "CUST-OLD",
				customer_name: "Old Customer",
				customer_address: "ADDR-OLD",
				shipping_address: "SHIP-OLD",
				contact_person: "CONT-OLD",
				territory: "Old Territory",
			},
			customer: "CUST-NEW",
			isReturnInvoice: false,
			items: [],
			packed_items: [],
			Total: 0,
			subtotal: 0,
			additional_discount: 0,
			additional_discount_percentage: 0,
			roundAmount: (value: number) => value,
			pos_opening_shift: { name: "SHIFT-1" },
			posa_offers: [],
			posa_coupons: [],
			selected_delivery_charge: null,
			delivery_charges_rate: 0,
			posting_date_display: "2026-03-28",
			formatDateForBackend: (value: string) => value,
			invoice_doc: {
				name: "ACC-SINV-0002",
				customer: "CUST-OLD",
				customer_name: "Old Customer",
				customer_address: "ADDR-OLD",
				shipping_address_name: "SHIP-OLD",
				contact_person: "CONT-OLD",
				territory: "Old Territory",
				payments: [],
				taxes: [],
			},
		};

		const doc = get_invoice_doc(context);

		expect(doc.customer).toBe("CUST-NEW");
		expect(doc.customer_name).toBe("CUST-NEW");
		expect(doc.customer_address).toBeNull();
		expect(doc.shipping_address_name).toBeNull();
		expect(doc.contact_person).toBeNull();
		expect(doc.territory).toBeNull();
	});

	it("marks backdated invoices to preserve the selected posting date on submit", () => {
		(globalThis as any).frappe = {
			datetime: {
				nowdate: () => "2026-03-28",
			},
		};

		const context: any = {
			invoiceType: "Invoice",
			pos_profile: {
				company: "Test Company",
				name: "Main POS",
				currency: "PKR",
				payments: [{ mode_of_payment: "Cash", account: "Cash", type: "Cash", default: 1 }],
			},
			selected_currency: "PKR",
			conversion_rate: 1,
			company: { default_currency: "PKR" },
			price_list_currency: "PKR",
			get_price_list: () => "Standard Selling",
			customer_info: {
				customer: "CUST-001",
				customer_name: "Walk-in Customer",
			},
			customer: "CUST-001",
			isReturnInvoice: false,
			items: [],
			packed_items: [],
			Total: 0,
			subtotal: 0,
			additional_discount: 0,
			additional_discount_percentage: 0,
			roundAmount: (value: number) => value,
			pos_opening_shift: { name: "SHIFT-1" },
			posa_offers: [],
			posa_coupons: [],
			selected_delivery_charge: null,
			delivery_charges_rate: 0,
			posting_date_display: "2026-03-20",
			posting_date: "2026-03-20",
			formatDateForBackend: (value: string) => value,
			invoice_doc: {
				payments: [],
				taxes: [],
			},
		};

		const doc = get_invoice_doc(context);

		expect(doc.posting_date).toBe("2026-03-20");
		expect(doc.set_posting_time).toBe(1);
	});

	it("serializes base totals and item base rates using invoice_doc.company_currency when companyDoc and profile.company_currency are absent", () => {
		const context: any = {
			invoiceType: "Invoice",
			pos_profile: {
				company: "Uganda Co",
				name: "POS-USD",
				currency: "USD",
				payments: [{ mode_of_payment: "Cash", account: "Cash USD", type: "Cash", default: 1 }],
			},
			selected_currency: "USD",
			conversion_rate: 3700,
			company: null,
			price_list_currency: "USD",
			currency_precision: 2,
			get_price_list: () => "USD Selling",
			customer_info: {
				customer: "CUST-001",
				customer_name: "Walk-in Customer",
			},
			customer: "CUST-001",
			isReturnInvoice: false,
			items: [
				{
					item_code: "ITEM-USD-1",
					item_name: "Imported Item",
					qty: 1,
					rate: 10,
					price_list_rate: 10,
					discount_amount: 0,
					discount_percentage: 0,
					uom: "Nos",
					conversion_factor: 1,
					company_currency: "UGX",
				},
			],
			packed_items: [],
			Total: 10,
			subtotal: 10,
			additional_discount: 0,
			additional_discount_percentage: 0,
			roundAmount: (value: number) => value,
			pos_opening_shift: { name: "SHIFT-USD-1" },
			posa_offers: [],
			posa_coupons: [],
			selected_delivery_charge: null,
			delivery_charges_rate: 0,
			posting_date_display: "2026-11-28",
			formatDateForBackend: (value: string) => value,
			invoice_doc: {
				company_currency: "UGX",
				payments: [{ mode_of_payment: "Cash", amount: 10 }],
				taxes: [],
			},
		};

		const doc = get_invoice_doc(context);

		expect(doc.currency).toBe("USD");
		expect(doc.conversion_rate).toBe(3700);
		expect(doc.base_total).toBe(37000);
		expect(doc.base_net_total).toBe(37000);
		expect(doc.base_grand_total).toBe(37000);
		expect(doc.base_rounded_total).toBe(37000);
		expect(doc.payments[0].base_amount).toBe(37000);
		expect(doc.items[0].base_rate).toBe(37000);
		expect(doc.items[0].base_amount).toBe(37000);
	});
});
