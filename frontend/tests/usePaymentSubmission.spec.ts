import { ref } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePaymentSubmission } from "../src/posapp/composables/pos/payments/usePaymentSubmission";
import { ApiEnvelopeError } from "../src/posapp/services/api";

vi.mock("../src/offline/index", () => ({
	enqueueInvoiceOutboxEntry: vi.fn(async () => ({})),
	isOffline: vi.fn(() => false),
	persistInvoiceIntentJournal: vi.fn(() => "test-request-id"),
	removeInvoiceOutboxEntry: vi.fn(async () => 1),
	saveOfflineInvoice: vi.fn(),
	updateLocalStock: vi.fn(),
}));

vi.mock("../src/posapp/services/invoiceService", () => ({
	default: {
		submitInvoice: vi.fn(),
		submitExchange: vi.fn(),
	},
}));

vi.mock("../src/posapp/utils/stockCoordinator", () => ({
	default: {
		applyInvoiceConsumption: vi.fn(),
	},
}));

describe("usePaymentSubmission", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.stubGlobal("__", (value: string, args?: any[]) => {
			if (!args?.length) return value;
			return value.replace(/\{(\d+)\}/g, (_match, index) =>
				String(args[Number(index)] ?? ""),
			);
		});
		vi.stubGlobal("frappe", {
			utils: {
				play_sound: vi.fn(),
			},
		});
	});

	it("restores negative return payments back to normal amounts", () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			payments: [
				{
					mode_of_payment: "Cash",
					amount: -120,
					base_amount: -120,
					default: 1,
				},
				{ mode_of_payment: "Card", amount: 0, base_amount: 0 },
				{ mode_of_payment: "Bank", amount: 35, base_amount: 35 },
			],
		});

		const { restoreReturnPayments } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			isCashback: ref(true),
		});

		restoreReturnPayments();

		expect(invoiceDoc.value.payments).toEqual([
			{
				mode_of_payment: "Cash",
				amount: 120,
				base_amount: 120,
				default: 1,
			},
			{ mode_of_payment: "Card", amount: 0, base_amount: 0 },
			{ mode_of_payment: "Bank", amount: 35, base_amount: 35 },
		]);
	});

	it("blocks submission validation when a sale row is below trade price", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			items: [
				{
					item_code: "02017",
					item_name: "ARINAC FORT",
					qty: 1,
					rate: 10,
					trade_price: 12.75,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 10, type: "Cash" }],
			rounded_total: 10,
			grand_total: 10,
		});

		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_allow_partial_payment: 0 }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
			},
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(validateSubmission(true)).rejects.toThrow(
			/below Trade Price/i,
		);
	});

	it("allows warning-only below-cost policy and shows a warning", async () => {
		const toastShow = vi.fn();
		const invoiceDoc = ref<any>({
			is_return: 0,
			items: [{ item_code: "LOW", qty: 1, rate: 9, trade_price: 10 }],
			payments: [{ mode_of_payment: "Cash", amount: 9, type: "Cash" }],
			rounded_total: 9,
			grand_total: 9,
		});
		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Warning Only" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: toastShow } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(validateSubmission(true)).resolves.toBe(true);
		expect(toastShow).toHaveBeenCalledWith(
			expect.objectContaining({ color: "warning" }),
		);
	});

	it("captures a POS supervisor override reason before submission", async () => {
		const requestBelowCostOverride = vi.fn().mockResolvedValue({
			approved: true,
			reason: "Approved clearance",
		});
		const invoiceDoc = ref<any>({
			is_return: 0,
			items: [{ item_code: "LOW", qty: 1, rate: 9, trade_price: 10 }],
			payments: [{ mode_of_payment: "Cash", amount: 9, type: "Cash" }],
			rounded_total: 9,
			grand_total: 9,
		});
		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_below_cost_action: "POS Supervisor Override",
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			requestBelowCostOverride,
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(validateSubmission(true)).resolves.toBe(true);
		expect(invoiceDoc.value.posa_below_cost_override).toBe(1);
		expect(invoiceDoc.value.posa_below_cost_override_reason).toBe(
			"Approved clearance",
		);
	});

	it("requests a supervisor override for a below-cost POS Offer give-away", async () => {
		const requestBelowCostOverride = vi.fn().mockResolvedValue({
			approved: true,
			reason: "Approved offer give-away",
		});
		const invoiceDoc = ref<any>({
			is_return: 0,
			items: [
				{ item_code: "PAID", qty: 1, rate: 20, trade_price: 10 },
				{
					item_code: "GIFT",
					qty: 1,
					rate: 0,
					trade_price: 10,
					is_free_item: 1,
					posa_is_offer: 1,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 20, type: "Cash" }],
			rounded_total: 20,
			grand_total: 20,
		});
		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_below_cost_action: "POS Supervisor Override",
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			requestBelowCostOverride,
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(validateSubmission(true)).resolves.toBe(true);
		expect(requestBelowCostOverride).toHaveBeenCalledTimes(1);
		expect(invoiceDoc.value.posa_below_cost_override).toBe(1);
	});

	it("blocks a POS Offer give-away with no buying floor when missing cost is Block", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			items: [
				{ item_code: "PAID", qty: 1, rate: 20, trade_price: 10 },
				{
					item_code: "GIFT",
					item_name: "Offer Gift",
					qty: 1,
					rate: 0,
					is_free_item: 1,
					posa_is_offer: 1,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 20, type: "Cash" }],
			rounded_total: 20,
			grand_total: 20,
		});
		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_missing_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(validateSubmission(true)).rejects.toThrow(
			/no valid buying floor is available for Offer Gift/i,
		);
	});

	it("does not ask for an override on the till's pricing rule free lines", async () => {
		const requestBelowCostOverride = vi.fn();
		const invoiceDoc = ref<any>({
			is_return: 0,
			items: [
				{ item_code: "PAID", qty: 1, rate: 20, trade_price: 10 },
				{
					item_code: "PROMO",
					qty: 1,
					rate: 0,
					trade_price: 10,
					is_free_item: 1,
					source_rule: "PR-FREE",
					auto_free_source: "PR-FREE::PAID::PROMO",
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 20, type: "Cash" }],
			rounded_total: 20,
			grand_total: 20,
		});
		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_below_cost_action: "POS Supervisor Override",
				posa_missing_cost_action: "Block",
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			requestBelowCostOverride,
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(validateSubmission(true)).resolves.toBe(true);
		expect(requestBelowCostOverride).not.toHaveBeenCalled();
	});

	it("routes unresolved cross-currency FX through missing-cost policy without 1:1 floor comparison", async () => {
		const buildInvoice = () =>
			ref<any>({
				is_return: 0,
				currency: "USD",
				items: [
					{
						item_code: "FX-MISSING",
						item_name: "Missing FX Item",
						qty: 1,
						rate: 15,
						currency: "USD",
						company_currency: "UGX",
						conversion_rate: 0,
						buying_price_currency: "UGX",
						trade_price: 37000,
						_buying_prices_by_uom: {
							Nos: {
								price_list_rate: 37000,
								base_price_list_rate: 37000,
								currency: "UGX",
							},
						},
					},
				],
				payments: [{ mode_of_payment: "Cash", amount: 15, type: "Cash" }],
				rounded_total: 15,
				grand_total: 15,
			});

		const blockSubmit = usePaymentSubmission({
			invoiceDoc: buildInvoice(),
			posProfile: ref({ posa_missing_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});
		await expect(blockSubmit.validateSubmission(true)).rejects.toThrow(
			/no valid buying floor is available for Missing FX Item/i,
		);

		const allowSubmit = usePaymentSubmission({
			invoiceDoc: buildInvoice(),
			posProfile: ref({ posa_missing_cost_action: "Allow" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});
		await expect(allowSubmit.validateSubmission(true)).resolves.toBe(true);
	});

	it("matches ERPNext Grand Total discount distribution during checkout validation", async () => {
		const buildTaxedInvoice = (floor: number, includeNativeNet: boolean) =>
			ref<any>({
				is_return: 0,
				apply_discount_on: "Grand Total",
				discount_amount: 11,
				total_taxes_and_charges: 9,
				grand_total: 99,
				rounded_total: 99,
				taxes: [
					{
						charge_type: "On Net Total",
						account_head: "VAT 10% - CO",
						rate: 10,
						included_in_print_rate: 0,
						tax_amount: 9,
					},
				],
				items: [
					{
						item_code: "TAXED-100",
						item_name: "Taxed Item",
						qty: 1,
						rate: 100,
						...(includeNativeNet ? { net_rate: 90 } : {}),
						trade_price: floor,
					},
				],
				payments: [{ mode_of_payment: "Cash", amount: 99, type: "Cash" }],
			});

		for (const includeNativeNet of [true, false]) {
			const acceptSubmit = usePaymentSubmission({
				invoiceDoc: buildTaxedInvoice(89.5, includeNativeNet),
				posProfile: ref({}),
				stockSettings: ref({}),
				invoiceType: ref("Invoice"),
				formatFloat: (value) => Number(value || 0),
				stores: { toastStore: { show: vi.fn() } },
				diff_payment: ref(0) as any,
				isCashback: ref(true),
			});
			await expect(acceptSubmit.validateSubmission(true)).resolves.toBe(true);

			const blockSubmit = usePaymentSubmission({
				invoiceDoc: buildTaxedInvoice(90.5, includeNativeNet),
				posProfile: ref({}),
				stockSettings: ref({}),
				invoiceType: ref("Invoice"),
				formatFloat: (value) => Number(value || 0),
				stores: { toastStore: { show: vi.fn() } },
				diff_payment: ref(0) as any,
				isCashback: ref(true),
			});
			await expect(blockSubmit.validateSubmission(true)).rejects.toThrow(
				/Taxed Item is selling at 90, below Trade Price 90\.5/i,
			);
		}
	});

	it("blocks checkout when company-currency valuation is unconvertible even if lower invoice-currency trade_price exists", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			items: [
				{
					item_code: "HIDDEN-VAL",
					item_name: "Hidden Valuation Item",
					qty: 1,
					rate: 15,
					currency: "USD",
					company_currency: "UGX",
					conversion_rate: 0,
					buying_rate_currency: "USD",
					trade_price: 9,
					valuation_rate: 74000,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 15, type: "Cash" }],
			rounded_total: 15,
			grand_total: 15,
		});

		const blockSubmit = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_missing_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});
		await expect(blockSubmit.validateSubmission(true)).rejects.toThrow(
			/no valid buying floor is available for Hidden Valuation Item/i,
		);
	});

	it("blocks below-cost VND sale at 0.00004 USD/VND under both Block and Allow missing-cost policies", async () => {
		const buildVndInvoice = () =>
			ref<any>({
				is_return: 0,
				currency: "VND",
				items: [
					{
						item_code: "VND-SMALL-FX",
						item_name: "VND Item",
						qty: 1,
						rate: 20000,
						currency: "VND",
						company_currency: "USD",
						conversion_rate: 0.00004,
						valuation_rate: 1,
					},
				],
				payments: [{ mode_of_payment: "Cash", amount: 20000, type: "Cash" }],
				rounded_total: 20000,
				grand_total: 20000,
			});

		for (const missingCostAction of ["Block", "Allow"] as const) {
			const submission = usePaymentSubmission({
				invoiceDoc: buildVndInvoice(),
				posProfile: ref({ posa_missing_cost_action: missingCostAction }),
				stockSettings: ref({}),
				invoiceType: ref("Invoice"),
				formatFloat: (value) => Number(value || 0),
				stores: { toastStore: { show: vi.fn() } },
				diff_payment: ref(0) as any,
				isCashback: ref(true),
			});
			await expect(submission.validateSubmission(true)).rejects.toThrow(
				/VND Item is selling at 20000, below Warehouse Valuation 25000/i,
			);
		}
	});

	it("accepts middle line with native net_rate == gross rate when 3x 1.00 lines share 0.02 discount", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			apply_discount_on: "Net Total",
			discount_amount: 0.02,
			items: [
				{
					item_code: "ITEM-1",
					item_name: "Line 1",
					qty: 1,
					rate: 1.0,
					net_rate: 0.99,
					net_amount: 0.99,
					trade_price: 0.99,
				},
				{
					item_code: "ITEM-2",
					item_name: "Line 2",
					qty: 1,
					rate: 1.0,
					net_rate: 1.0,
					net_amount: 1.0,
					trade_price: 1.0,
				},
				{
					item_code: "ITEM-3",
					item_name: "Line 3",
					qty: 1,
					rate: 1.0,
					net_rate: 0.99,
					net_amount: 0.99,
					trade_price: 0.99,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 2.98, type: "Cash" }],
			rounded_total: 2.98,
			grand_total: 2.98,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("blocks below-cost Grand Total discount on On Item Quantity tax without net_rate", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			apply_discount_on: "Grand Total",
			discount_amount: 12,
			grand_total: 108,
			total_taxes_and_charges: 20,
			taxes: [
				{
					charge_type: "On Item Quantity",
					account_head: "Levy - CO",
					rate: 20,
					included_in_print_rate: 0,
				},
			],
			items: [
				{
					item_code: "QTY-TAX-ITEM",
					item_name: "Qty Tax Item",
					qty: 1,
					rate: 100,
					trade_price: 89,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 108, type: "Cash" }],
			rounded_total: 108,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).rejects.toThrow(
			/Qty Tax Item is selling at 88, below Trade Price 89/i,
		);
	});

	it("blocks checkout under Block missing-cost policy when foreign buying price lacks FX even if lower valuation_rate exists", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			items: [
				{
					item_code: "EUR-BUY-NO-FX",
					item_name: "EUR Cost Item",
					qty: 1,
					rate: 8,
					currency: "USD",
					company_currency: "USD",
					conversion_rate: 1,
					buying_price_currency: "EUR",
					buying_rate_currency: "EUR",
					trade_price: 10,
					buying_rate: 10,
					valuation_rate: 5,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 8, type: "Cash" }],
			rounded_total: 8,
			grand_total: 8,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_missing_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).rejects.toThrow(
			/no valid buying floor is available for EUR Cost Item/i,
		);
	});

	it("allows checkout when item_tax_rate N/A exempts item from 20% inclusive VAT template", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			taxes: [
				{
					charge_type: "On Net Total",
					account_head: "VAT",
					rate: 20,
					included_in_print_rate: 1,
				},
			],
			items: [
				{
					item_code: "NA-TAX-ITEM",
					item_name: "NA Tax Item",
					qty: 1,
					rate: 100,
					trade_price: 95,
					item_tax_rate: JSON.stringify({ VAT: "N/A" }),
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 100, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("blocks checkout under Block policy on merge_item_row output with unresolved foreign _buying_prices_by_uom despite lower USD valuation", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			items: [
				{
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
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 8, type: "Cash" }],
			rounded_total: 8,
			grand_total: 8,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_missing_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).rejects.toThrow(
			/no valid buying floor is available for EUR Cost Merged Item/i,
		);
	});

	it("does not overturn reconciled invoiceDoc.items when invoiceStore cart rows lack calculated net fields", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			apply_discount_on: "Net Total",
			discount_amount: 0.02,
			items: [
				{
					item_code: "ITEM-1",
					item_name: "Line 1",
					qty: 1,
					rate: 1.0,
					net_rate: 0.99,
					net_amount: 0.99,
					trade_price: 0.99,
				},
				{
					item_code: "ITEM-2",
					item_name: "Line 2",
					qty: 1,
					rate: 1.0,
					net_rate: 1.0,
					net_amount: 1.0,
					trade_price: 1.0,
				},
				{
					item_code: "ITEM-3",
					item_name: "Line 3",
					qty: 1,
					rate: 1.0,
					net_rate: 0.99,
					net_amount: 0.99,
					trade_price: 0.99,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 2.98, type: "Cash" }],
			rounded_total: 2.98,
			grand_total: 2.98,
		});

		const cartItems = ref([
			{
				item_code: "ITEM-1",
				item_name: "Line 1",
				qty: 1,
				rate: 1.0,
				trade_price: 0.99,
			},
			{
				item_code: "ITEM-2",
				item_name: "Line 2",
				qty: 1,
				rate: 1.0,
				trade_price: 1.0,
			},
			{
				item_code: "ITEM-3",
				item_name: "Line 3",
				qty: 1,
				rate: 1.0,
				trade_price: 0.99,
			},
		]);

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				invoiceStore: {
					invoiceDoc: invoiceDoc.value,
					items: cartItems,
				},
			},
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("accepts reconciled 3-unit line at 1.00 with 0.01 discount and native net_rate 1.00", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			apply_discount_on: "Net Total",
			discount_amount: 0.01,
			items: [
				{
					item_code: "MULTI-3",
					item_name: "Three Unit Item",
					qty: 3,
					rate: 1.0,
					net_amount: 2.99,
					net_rate: 1.0,
					base_net_rate: 1.0,
					trade_price: 1.0,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 2.99, type: "Cash" }],
			rounded_total: 2.99,
			grand_total: 2.99,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			currencyPrecision: ref(2),
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("preserves configured currencyPrecision 0 for zero-decimal UGX rounding reconciliation", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "UGX",
			apply_discount_on: "Net Total",
			discount_amount: 2,
			items: [
				{
					item_code: "UGX-1",
					item_name: "UGX Line 1",
					qty: 1,
					rate: 100,
					net_rate: 99,
					net_amount: 99,
					trade_price: 99,
				},
				{
					item_code: "UGX-2",
					item_name: "UGX Line 2",
					qty: 1,
					rate: 100,
					net_rate: 100,
					net_amount: 100,
					trade_price: 100,
				},
				{
					item_code: "UGX-3",
					item_name: "UGX Line 3",
					qty: 1,
					rate: 100,
					net_rate: 99,
					net_amount: 99,
					trade_price: 99,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 298, type: "Cash" }],
			rounded_total: 298,
			grand_total: 298,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			currencyPrecision: ref(0),
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("copies missing currency and FX metadata from cart when enriching doc items so USD 15 passes UGX 37000 floor at 3700", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			conversion_rate: 3700,
			items: [
				{
					item_code: "FX-ENRICH-1",
					item_name: "USD Item",
					qty: 1,
					rate: 15,
					net_rate: 15,
					net_amount: 15,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 15, type: "Cash" }],
			rounded_total: 15,
			grand_total: 15,
		});

		const cartItems = ref([
			{
				item_code: "FX-ENRICH-1",
				item_name: "USD Item",
				qty: 1,
				rate: 15,
				currency: "USD",
				company_currency: "UGX",
				conversion_rate: 3700,
				plc_conversion_rate: 3700,
				_company_buying_price: 37000,
			},
		]);

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				invoiceStore: {
					invoiceDoc: invoiceDoc.value,
					items: cartItems,
				},
			},
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("enriches missing higher buying_rate from cart even when invoiceDoc already has lower valuation_rate", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			items: [
				{
					item_code: "COST-MERGE-1",
					item_name: "Partial Cost Item",
					qty: 1,
					rate: 8,
					valuation_rate: 5,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 8, type: "Cash" }],
			rounded_total: 8,
			grand_total: 8,
		});

		const cartItems = ref([
			{
				item_code: "COST-MERGE-1",
				item_name: "Partial Cost Item",
				qty: 1,
				rate: 8,
				buying_rate: 10,
				valuation_rate: 5,
			},
		]);

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				invoiceStore: {
					invoiceDoc: invoiceDoc.value,
					items: cartItems,
				},
			},
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).rejects.toThrow(
			/Partial Cost Item is selling at 8, below Buying Rate 10/i,
		);
	});

	it("uses enriched cart cost in missing-cost Block check when invoiceDoc line has no cost fields", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			items: [
				{
					item_code: "ENRICH-BLOCK-1",
					item_name: "Server Stripped Cost Item",
					qty: 1,
					rate: 12,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 12, type: "Cash" }],
			rounded_total: 12,
			grand_total: 12,
		});

		const cartItems = ref([
			{
				item_code: "ENRICH-BLOCK-1",
				item_name: "Server Stripped Cost Item",
				qty: 1,
				rate: 12,
				trade_price: 10,
			},
		]);

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_below_cost_action: "Block",
				posa_missing_cost_action: "Block",
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				invoiceStore: {
					invoiceDoc: invoiceDoc.value,
					items: cartItems,
				},
			},
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("matches cart rows by posa_row_id before index fallback when duplicate SKU rows are reordered", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			items: [
				{
					item_code: "DUP-SKU",
					item_name: "Duplicate SKU High",
					posa_row_id: "row-high",
					qty: 1,
					rate: 20,
				},
				{
					item_code: "DUP-SKU",
					item_name: "Duplicate SKU Low",
					posa_row_id: "row-low",
					qty: 1,
					rate: 7,
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 27, type: "Cash" }],
			rounded_total: 27,
			grand_total: 27,
		});

		const cartItems = ref([
			{
				item_code: "DUP-SKU",
				item_name: "Duplicate SKU Low",
				posa_row_id: "row-low",
				qty: 1,
				rate: 7,
				trade_price: 6,
			},
			{
				item_code: "DUP-SKU",
				item_name: "Duplicate SKU High",
				posa_row_id: "row-high",
				qty: 1,
				rate: 20,
				trade_price: 18,
			},
		]);

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				invoiceStore: {
					invoiceDoc: invoiceDoc.value,
					items: cartItems,
				},
			},
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("accepts valid USD 12 sale under Block when _buying_prices_by_uom has USD 10 with base_price_list_rate null and valid UGX conversion", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			conversion_rate: 3700,
			items: [
				{
					item_code: "USD-UOM-NULL-BASE",
					item_name: "USD Table Item",
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
				},
			],
			payments: [{ mode_of_payment: "Cash", amount: 12, type: "Cash" }],
			rounded_total: 12,
			grand_total: 12,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_below_cost_action: "Block",
				posa_missing_cost_action: "Block",
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).resolves.toBe(true);
	});

	it("blocks checkout when ERPNext rounds multi-unit net_rate to 0.00 against a UGX 10 valuation floor", async () => {
		const invoiceDoc = ref<any>({
			is_return: 0,
			currency: "USD",
			conversion_rate: 3700,
			apply_discount_on: "Net Total",
			discount_amount: 0.02,
			items: [
				{
					item_code: "ZERO-NET-RATE",
					item_name: "Rounded Zero Net Rate Item",
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
			],
			payments: [{ mode_of_payment: "Cash", amount: 0.01, type: "Cash" }],
			rounded_total: 0.01,
			grand_total: 0.01,
		});

		const submission = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_below_cost_action: "Block" }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: { toastStore: { show: vi.fn() } },
			currencyPrecision: ref(2),
			diff_payment: ref(0) as any,
			isCashback: ref(true),
		});

		await expect(submission.validateSubmission(true)).rejects.toThrow(
			/Rounded Zero Net Rate Item is selling at 0/i,
		);
	});

	it("defers print and schedules background wait when invoice submission is queued", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0001",
			doctype: "Sales Invoice",
			status: 0,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0001",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [],
			payments: [{ mode_of_payment: "Cash", amount: 690, type: "Cash" }],
			rounded_total: 690,
			grand_total: 690,
		});
		const onPrint = vi.fn();
		const onScheduleBackgroundCheck = vi.fn();

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(true),
			paidChange: ref(10),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(100),
			customerCreditDict: ref([]),
			diff_payment: ref(-10),
		});

		await submitInvoice(true, {
			onPrint,
			onScheduleBackgroundCheck,
			onFinishNavigation: vi.fn(),
		});

		expect(onPrint).not.toHaveBeenCalled();
		expect(onScheduleBackgroundCheck).toHaveBeenCalledWith(
			expect.objectContaining({
				name: "ACC-SINV-0001",
				doctype: "Sales Invoice",
				print: true,
				waitForInvoiceProcessing: true,
				waitForPostSubmitPayments: true,
			}),
		);
	});

	it("schedules deferred printing instead of calling onPrint when post-submit work remains", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0002",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0002",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [],
			payments: [{ mode_of_payment: "Cash", amount: 690, type: "Cash" }],
			rounded_total: 690,
			grand_total: 690,
		});
		const onPrint = vi.fn();
		const onScheduleBackgroundCheck = vi.fn();

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(true),
			paidChange: ref(10),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(100),
			customerCreditDict: ref([]),
			diff_payment: ref(-10),
		});

		await submitInvoice(true, {
			onPrint,
			onFinishNavigation: vi.fn(),
			onScheduleBackgroundCheck,
		});

		expect(onPrint).not.toHaveBeenCalled();
		expect(onScheduleBackgroundCheck).toHaveBeenCalledWith(
			expect.objectContaining({
				name: "ACC-SINV-0002",
				doctype: "Sales Invoice",
				waitForInvoiceProcessing: false,
				waitForPostSubmitPayments: true,
			}),
		);
	});

	it("prints immediately when there is no deferred post-submit work", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0004",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0004",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [],
			payments: [{ mode_of_payment: "Cash", amount: 690, type: "Cash" }],
			rounded_total: 690,
			grand_total: 690,
		});
		const onPrint = vi.fn();
		const onScheduleBackgroundCheck = vi.fn();

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(true),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await submitInvoice(true, {
			onPrint,
			onFinishNavigation: vi.fn(),
			onScheduleBackgroundCheck,
		});

		expect(onPrint).toHaveBeenCalledWith(
			expect.objectContaining({
				name: "ACC-SINV-0004",
				doctype: "Sales Invoice",
				docstatus: 1,
			}),
			expect.objectContaining({
				name: "ACC-SINV-0004",
				doctype: "Sales Invoice",
				waitForInvoiceProcessing: false,
				waitForPostSubmitPayments: false,
			}),
		);
		expect(onScheduleBackgroundCheck).not.toHaveBeenCalled();
	});

	it("prints a newly submitted Sales Order with the server-assigned name", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "SAL-ORD-0001",
		});

		const invoiceDoc = ref<any>({
			doctype: "Sales Order",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 100, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			posa_delivery_date: "2026-07-01",
		});
		const onPrint = vi.fn();
		const setLastInvoice = vi.fn();
		const mergeInvoiceDoc = vi.fn((patch) => {
			Object.assign(invoiceDoc.value, patch);
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				posa_allow_sales_order: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Order"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice,
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value, mergeInvoiceDoc },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await submitInvoice(true, {
			onPrint,
			onFinishNavigation: vi.fn(),
			onScheduleBackgroundCheck: vi.fn(),
		});

		expect(onPrint).toHaveBeenCalledWith(
			expect.objectContaining({
				name: "SAL-ORD-0001",
				doctype: "Sales Order",
				docstatus: 1,
			}),
			expect.objectContaining({
				name: "SAL-ORD-0001",
				doctype: "Sales Order",
				waitForInvoiceProcessing: false,
				waitForPostSubmitPayments: false,
			}),
		);
		expect(mergeInvoiceDoc).toHaveBeenCalledWith({
			name: "SAL-ORD-0001",
			doctype: "Sales Order",
			docstatus: 1,
		});
		expect(invoiceDoc.value.name).toBe("SAL-ORD-0001");
		expect(setLastInvoice).toHaveBeenCalledWith("SAL-ORD-0001");
	});

	it("shows a merged processing toast instead of a plain success toast when post-submit payments are pending", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0003",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0003",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [],
			payments: [{ mode_of_payment: "Cash", amount: 690, type: "Cash" }],
			rounded_total: 690,
			grand_total: 690,
		});
		const toastShow = vi.fn();

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: toastShow },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(true),
			paidChange: ref(10),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(100),
			customerCreditDict: ref([]),
			diff_payment: ref(-10),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
			onScheduleBackgroundCheck: vi.fn(),
		});

		expect(toastShow).toHaveBeenCalledWith(
			expect.objectContaining({
				key: "invoice-processing::ACC-SINV-0003",
				title: "Invoice Submitted",
				loading: true,
			}),
		);
	});

	it("submits both exchange documents and reports an even exchange", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitExchange as any).mockResolvedValue({
			name: "ACC-SINV-REPLACEMENT",
			doctype: "Sales Invoice",
			docstatus: 1,
			return_invoice: "ACC-SINV-RETURN",
			replacement_invoice: "ACC-SINV-REPLACEMENT",
			return_invoice_doc: { items: [{ item_code: "OLD", qty: -1 }] },
			exchange_summary: {
				return_total: 50,
				sale_total: 50,
				difference_amount: 0,
				settlement_type: "Even Exchange",
			},
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-DRAFT",
			doctype: "Sales Invoice",
			currency: "PKR",
			customer: "CUST-0001",
			is_return: 0,
			items: [{ item_code: "NEW", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 0, type: "Cash" }],
			rounded_total: 50,
			grand_total: 50,
			posa_exchange_credit: 50,
		});
		const exchangeSession = ref({
			stage: "sale",
			clientRequestId: "exchange-request-1",
			returnTotal: 50,
			returnDoc: {
				name: "ACC-SINV-RETURN-DRAFT",
				is_return: 1,
				return_against: "ACC-SINV-ORIGINAL",
				customer: "CUST-0001",
				items: [{ item_code: "OLD", qty: -1 }],
			},
		});
		const toastShow = vi.fn();

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			exchangeSession,
			stores: {
				toastStore: { show: toastShow },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: {
					invoiceDoc: invoiceDoc.value,
					mergeInvoiceDoc: vi.fn(),
				},
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await submitInvoice(false, { onFinishNavigation: vi.fn() });

		expect(invoiceService.submitExchange).toHaveBeenCalledWith(
			expect.any(Object),
			expect.objectContaining({ posa_exchange_credit: 50 }),
			exchangeSession.value.returnDoc,
			expect.objectContaining({ name: "Main POS" }),
			"exchange-request-1",
		);
		expect(toastShow).toHaveBeenCalledWith(
			expect.objectContaining({
				title: "Item exchange completed",
				summary:
					"Return ACC-SINV-RETURN and replacement ACC-SINV-REPLACEMENT submitted",
				detail: "Even exchange - no payment collected",
				color: "success",
			}),
		);
	});

	it("blocks an exchange from being saved as a standalone offline invoice", async () => {
		const offlineModule = await import("../src/offline/index");
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(offlineModule.isOffline as any).mockReturnValue(true);

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-EXCHANGE-OFFLINE",
			doctype: "Sales Invoice",
			company: "Test Company",
			currency: "PKR",
			customer: "CUST-0001",
			is_return: 0,
			items: [{ item_code: "NEW", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 50, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			posa_exchange_credit: 50,
		});
		const exchangeSession = ref({
			stage: "sale",
			clientRequestId: "exchange-offline-1",
			returnTotal: 50,
			returnDoc: {
				is_return: 1,
				return_against: "ACC-SINV-ORIGINAL",
				customer: "CUST-0001",
				items: [{ item_code: "OLD", qty: -1 }],
			},
		});
		const onFinishNavigation = vi.fn();

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "PKR",
				customer: "Default Customer",
				posa_allow_submissions_in_background_job: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			exchangeSession,
			stores: {
				toastStore: { show: vi.fn() },
				syncStore: { updatePendingCount: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(
			submitInvoice(false, { onFinishNavigation }),
		).rejects.toThrow("Item exchanges require an online connection");

		expect(offlineModule.saveOfflineInvoice).not.toHaveBeenCalled();
		expect(invoiceService.submitExchange).not.toHaveBeenCalled();
		expect(onFinishNavigation).not.toHaveBeenCalled();
		(offlineModule.isOffline as any).mockReturnValue(false);
	});

	it("preserves the exchange session after a synchronous submission failure", async () => {
		const consoleError = vi
			.spyOn(console, "error")
			.mockImplementation(() => undefined);
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitExchange as any).mockRejectedValueOnce(
			new Error("Exchange validation failed"),
		);

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-EXCHANGE-FAILED",
			doctype: "Sales Invoice",
			company: "Test Company",
			currency: "PKR",
			customer: "CUST-0001",
			is_return: 0,
			items: [{ item_code: "NEW", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 50, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			posa_exchange_credit: 50,
		});
		const exchangeSession = ref({
			stage: "sale",
			clientRequestId: "exchange-failed-1",
			returnTotal: 50,
			returnDoc: {
				is_return: 1,
				return_against: "ACC-SINV-ORIGINAL",
				customer: "CUST-0001",
				items: [{ item_code: "OLD", qty: -1 }],
			},
		});
		const onFinishNavigation = vi.fn();
		const onScheduleBackgroundCheck = vi.fn();

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "PKR",
				posa_allow_submissions_in_background_job: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			exchangeSession,
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(
			submitInvoice(false, {
				onFinishNavigation,
				onScheduleBackgroundCheck,
			}),
		).rejects.toThrow("Exchange validation failed");

		expect(onFinishNavigation).not.toHaveBeenCalled();
		expect(onScheduleBackgroundCheck).not.toHaveBeenCalled();
		expect(exchangeSession.value.clientRequestId).toBe("exchange-failed-1");
		consoleError.mockRestore();
	});

	it("includes gift card redemptions in the submit payload", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0005",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0005",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [],
			payments: [{ mode_of_payment: "Cash", amount: 390, type: "Cash" }],
			rounded_total: 690,
			grand_total: 690,
		});

		const giftCardRedemptions = ref([
			{
				gift_card_code: "GC-0001",
				amount: 300,
				cashier: "cashier@example.com",
			},
		]);

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 1,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			giftCardRedemptions,
			diff_payment: ref(390),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
			onScheduleBackgroundCheck: vi.fn(),
		});

		expect(invoiceService.submitInvoice).toHaveBeenCalledWith(
			expect.objectContaining({
				gift_card_redemptions: [
					expect.objectContaining({
						gift_card_code: "GC-0001",
						amount: 300,
					}),
				],
			}),
			expect.objectContaining({
				payments: [
					expect.objectContaining({
						mode_of_payment: "Cash",
						amount: 390,
					}),
				],
			}),
			"Invoice",
			expect.any(Object),
		);
	});

	it("adds a stable client request id to invoice submissions", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0099",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0099",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 50, type: "Cash" }],
			rounded_total: 50,
			grand_total: 50,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc.posa_client_request_id).toEqual(expect.any(String));
		expect(invoiceDoc.value.posa_client_request_id).toBe(
			submittedDoc.posa_client_request_id,
		);
	});

	it("computes base write-off amount with the invoice conversion rate", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-MC-WRITEOFF",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-MC-WRITEOFF",
			doctype: "Sales Invoice",
			currency: "USD",
			conversion_rate: 280,
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 90, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				currency: "PKR",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(10),
			is_write_off_change: ref(true),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc.write_off_amount).toBe(10);
		expect(submittedDoc.base_write_off_amount).toBe(2800);
	});

	it("reuses one identity after an ambiguous timeout and Submit & Print retry", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		const offlineModule = await import("../src/offline/index");
		(invoiceService.submitInvoice as any)
			.mockRejectedValueOnce(
				new ApiEnvelopeError({
					ok: false,
					data: null,
					error: {
						code: "TIMEOUT",
						message: "Request timed out",
						retryable: true,
					},
					requestId: "transport-timeout-001",
					serverTime: null,
				}),
			)
			.mockResolvedValueOnce({
				name: "ACC-SINV-0100",
				doctype: "Sales Invoice",
				docstatus: 1,
			});
		const consoleError = vi
			.spyOn(console, "error")
			.mockImplementation(() => undefined);

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0100",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 50, type: "Cash" }],
			rounded_total: 50,
			grand_total: 50,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(
			submitInvoice(false, {
				onFinishNavigation: vi.fn(),
			}),
		).rejects.toThrow("Request timed out");
		await submitInvoice(true, {
			onFinishNavigation: vi.fn(),
		});

		const firstData = (invoiceService.submitInvoice as any).mock
			.calls[0][0];
		const firstSubmittedDoc = (invoiceService.submitInvoice as any).mock
			.calls[0][1];
		const secondData = (invoiceService.submitInvoice as any).mock
			.calls[1][0];
		const secondSubmittedDoc = (invoiceService.submitInvoice as any).mock
			.calls[1][1];

		expect(firstSubmittedDoc.posa_client_request_id).toEqual(
			expect.any(String),
		);
		expect(secondSubmittedDoc.posa_client_request_id).toBe(
			firstSubmittedDoc.posa_client_request_id,
		);
		expect(invoiceDoc.value.posa_client_request_id).toBe(
			firstSubmittedDoc.posa_client_request_id,
		);
		expect(firstData).toEqual(
			expect.objectContaining({
				idempotency_key: firstSubmittedDoc.posa_client_request_id,
				client_request_id: firstSubmittedDoc.posa_client_request_id,
			}),
		);
		expect(secondData).toEqual(
			expect.objectContaining({
				idempotency_key: firstSubmittedDoc.posa_client_request_id,
				client_request_id: firstSubmittedDoc.posa_client_request_id,
			}),
		);
		expect(offlineModule.enqueueInvoiceOutboxEntry).toHaveBeenCalledTimes(
			2,
		);
		expect(offlineModule.enqueueInvoiceOutboxEntry).toHaveBeenNthCalledWith(
			1,
			expect.objectContaining({
				invoice: expect.objectContaining({
					posa_client_request_id:
						firstSubmittedDoc.posa_client_request_id,
				}),
			}),
		);
		expect(offlineModule.removeInvoiceOutboxEntry).toHaveBeenCalledWith(
			firstSubmittedDoc.posa_client_request_id,
		);
		consoleError.mockRestore();
	});

	it("normalizes loyalty redemption fields before online submit", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-LOYALTY-ONLINE",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-LOYALTY-ONLINE",
			doctype: "Sales Invoice",
			is_return: 0,
			customer: "CUST-LOYALTY",
			company: "Test Company",
			currency: "USD",
			conversion_rate: 1,
			update_stock: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 60, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			loyalty_amount: 0,
			redeem_loyalty_points: 0,
			loyalty_points: 0,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "USD",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
			loyaltyAmount: ref(40),
			customerInfo: ref({
				name: "CUST-LOYALTY",
				loyalty_program: "Retail Loyalty",
				conversion_factor: 10,
			}),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc).toEqual(
			expect.objectContaining({
				loyalty_amount: 40,
				redeem_loyalty_points: 1,
				loyalty_points: 4,
				loyalty_program: "Retail Loyalty",
			}),
		);
	});

	it("recomputes loyalty points when explicit loyalty amount differs from document amount", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-LOYALTY-STALE",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-LOYALTY-STALE",
			doctype: "Sales Invoice",
			is_return: 0,
			customer: "CUST-LOYALTY",
			company: "Test Company",
			currency: "USD",
			conversion_rate: 1,
			update_stock: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 80, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			loyalty_amount: 10,
			redeem_loyalty_points: 1,
			loyalty_points: 1,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "USD",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
			loyaltyAmount: ref(20),
			customerInfo: ref({
				name: "CUST-LOYALTY",
				loyalty_program: "Retail Loyalty",
				conversion_factor: 10,
			}),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc).toEqual(
			expect.objectContaining({
				loyalty_amount: 20,
				redeem_loyalty_points: 1,
				loyalty_points: 2,
			}),
		);
	});

	it("clears stale document loyalty redemption when explicit loyalty amount is zero", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-LOYALTY-CLEAR",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-LOYALTY-CLEAR",
			doctype: "Sales Invoice",
			is_return: 0,
			customer: "CUST-LOYALTY",
			company: "Test Company",
			currency: "USD",
			conversion_rate: 1,
			update_stock: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 100, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			loyalty_amount: 10,
			redeem_loyalty_points: 1,
			loyalty_points: 1,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "USD",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
			loyaltyAmount: ref(0),
			customerInfo: ref({
				name: "CUST-LOYALTY",
				loyalty_program: "Retail Loyalty",
				conversion_factor: 10,
			}),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc).toEqual(
			expect.objectContaining({
				loyalty_amount: 0,
				redeem_loyalty_points: 0,
				loyalty_points: 0,
			}),
		);
	});

	it("derives loyalty points from company currency during multi-currency submit", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-LOYALTY-MULTI",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-LOYALTY-MULTI",
			doctype: "Sales Invoice",
			is_return: 0,
			customer: "CUST-LOYALTY",
			company: "Test Company",
			currency: "USD",
			conversion_rate: 280,
			update_stock: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 90, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			loyalty_amount: 0,
			redeem_loyalty_points: 0,
			loyalty_points: 0,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "PKR",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
			loyaltyAmount: ref(10),
			customerInfo: ref({
				name: "CUST-LOYALTY",
				loyalty_program: "Retail Loyalty",
				conversion_factor: 70,
			}),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc).toEqual(
			expect.objectContaining({
				loyalty_amount: 10,
				redeem_loyalty_points: 1,
				loyalty_points: 40,
				loyalty_program: "Retail Loyalty",
			}),
		);
	});

	it("clears loyalty redemption when amount is too small to redeem one point", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-LOYALTY-TINY",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-LOYALTY-TINY",
			doctype: "Sales Invoice",
			is_return: 0,
			customer: "CUST-LOYALTY",
			company: "Test Company",
			currency: "USD",
			conversion_rate: 1,
			update_stock: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 100, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			loyalty_amount: 0,
			redeem_loyalty_points: 0,
			loyalty_points: 0,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "USD",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
			loyaltyAmount: ref(0.5),
			customerInfo: ref({
				name: "CUST-LOYALTY",
				loyalty_program: "Retail Loyalty",
				conversion_factor: 10,
			}),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc).toEqual(
			expect.objectContaining({
				loyalty_amount: 0,
				redeem_loyalty_points: 0,
				loyalty_points: 0,
			}),
		);
	});

	it("normalizes loyalty redemption fields before saving an offline invoice", async () => {
		const offlineModule = await import("../src/offline/index");
		(offlineModule.isOffline as any).mockReturnValue(true);

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-LOYALTY-OFFLINE",
			doctype: "Sales Invoice",
			is_return: 0,
			customer: "CUST-LOYALTY",
			company: "Test Company",
			currency: "USD",
			conversion_rate: 1,
			update_stock: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 60, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
			loyalty_amount: 0,
			redeem_loyalty_points: 0,
			loyalty_points: 0,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				name: "Main POS",
				company: "Test Company",
				currency: "USD",
				customer: "Default Customer",
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				syncStore: { updatePendingCount: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
			loyaltyAmount: ref(40),
			customerInfo: ref({
				name: "CUST-LOYALTY",
				loyalty_program: "Retail Loyalty",
				conversion_factor: 10,
			}),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		expect(offlineModule.saveOfflineInvoice).toHaveBeenCalledWith(
			expect.objectContaining({
				invoice: expect.objectContaining({
					loyalty_amount: 40,
					redeem_loyalty_points: 1,
					loyalty_points: 4,
					loyalty_program: "Retail Loyalty",
				}),
			}),
		);

		(offlineModule.isOffline as any).mockReturnValue(false);
	});

	it("blocks offline invoice save when gift card redemption is present", async () => {
		const offlineModule = await import("../src/offline/index");
		(offlineModule.isOffline as any).mockReturnValue(true);

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0006",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [
				{ mode_of_payment: "Gift Card", amount: 300, type: "Bank" },
			],
			rounded_total: 300,
			grand_total: 300,
		});

		const giftCardRedemptions = ref([
			{
				gift_card_code: "GC-0002",
				amount: 300,
				cashier: "cashier@example.com",
			},
		]);

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				syncStore: { updatePendingCount: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			giftCardRedemptions,
			diff_payment: ref(0),
		});

		await expect(
			submitInvoice(false, {
				onFinishNavigation: vi.fn(),
			}),
		).rejects.toThrow("Gift card redemption requires an online connection");

		(offlineModule.isOffline as any).mockReturnValue(false);
	});

	it("submits gift card redemptions without requiring a gift card payment row", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0007",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0007",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [
				{
					mode_of_payment: "Cash",
					type: "Cash",
					account: "1110 - Cash",
					amount: 0,
				},
			],
			rounded_total: 300,
			grand_total: 300,
		});

		const giftCardRedemptions = ref([
			{
				gift_card_code: "GC-ONLY",
				amount: 300,
				cashier: "cashier@example.com",
			},
		]);

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
				posa_allow_partial_payment: 0,
				payments: [
					{
						mode_of_payment: "Cash",
						type: "Cash",
						account: "1110 - Cash",
						default: 1,
					},
				],
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			giftCardRedemptions,
			diff_payment: ref(0),
		});

		await expect(
			submitInvoice(false, {
				onFinishNavigation: vi.fn(),
			}),
		).resolves.not.toThrow();

		expect(invoiceService.submitInvoice).toHaveBeenCalledWith(
			expect.objectContaining({
				gift_card_redemptions: [
					expect.objectContaining({
						gift_card_code: "GC-ONLY",
						amount: 300,
					}),
				],
			}),
			expect.objectContaining({
				payments: [
					expect.objectContaining({
						mode_of_payment: "Cash",
						amount: 0,
						account: "1110 - Cash",
					}),
				],
			}),
			"Invoice",
			expect.any(Object),
		);
	});

	it("maps validation envelope failures and preserves the request id", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockRejectedValue(
			new ApiEnvelopeError({
				ok: false,
				data: null,
				error: {
					code: "VALIDATION_ERROR",
					message: "Customer is required",
					retryable: false,
				},
				requestId: "req-validation-1",
				serverTime: "2026-05-01T06:00:00Z",
			}),
		);
		const consoleError = vi
			.spyOn(console, "error")
			.mockImplementation(() => undefined);
		const toastStore = { show: vi.fn() };

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-VALIDATION",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 100, type: "Cash" }],
			rounded_total: 100,
			grand_total: 100,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore,
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(
			submitInvoice(false, {
				onFinishNavigation: vi.fn(),
			}),
		).rejects.toThrow("Customer is required");

		expect(toastStore.show).toHaveBeenCalledWith(
			expect.objectContaining({
				title: "Unable to submit invoice",
				detail: expect.stringContaining("req-validation-1"),
				color: "error",
			}),
		);
		expect(consoleError).toHaveBeenCalledWith(
			"Error submitting invoice:",
			expect.objectContaining({
				code: "VALIDATION_ERROR",
				requestId: "req-validation-1",
			}),
		);
		consoleError.mockRestore();
	});

	it("normalizes return payment rows before submit even when cashback is disabled", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-RETURN-0001",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-RETURN-0001",
			doctype: "Sales Invoice",
			is_return: 1,
			items: [{ item_code: "ITEM-1", qty: -1 }],
			payments: [
				{
					mode_of_payment: "Cash",
					amount: 90,
					base_amount: 90,
					type: "Cash",
				},
			],
			rounded_total: -90,
			grand_total: -90,
		});

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
			}),
			stockSettings: ref({}),
			invoiceType: ref("Return"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await submitInvoice(false, {
			onFinishNavigation: vi.fn(),
		});

		const [, submittedDoc] = (invoiceService.submitInvoice as any).mock
			.calls[0];
		expect(submittedDoc.payments).toEqual([
			expect.objectContaining({
				mode_of_payment: "Cash",
				amount: -90,
				base_amount: -90,
			}),
		]);
	});

	it("allows cashback validation for returns without an original invoice", async () => {
		const invoiceDoc = ref<any>({
			name: "ACC-SINV-RETURN-WITHOUT-INVOICE",
			doctype: "Sales Invoice",
			is_return: 1,
			items: [{ item_code: "ITEM-1", qty: -1 }],
			payments: [
				{
					mode_of_payment: "Cash",
					amount: -2625,
					base_amount: -2625,
					type: "Cash",
				},
			],
			rounded_total: -2625,
			grand_total: -2625,
			posa_refundable_amount: 0,
		});

		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({}),
			stockSettings: ref({}),
			invoiceType: ref("Return"),
			formatFloat: (value) => Number(value || 0),
			isCashback: ref(true),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(validateSubmission(false)).resolves.toBe(true);
	});

	it("rejects cashback above paid amount for returns against an original invoice", async () => {
		const invoiceDoc = ref<any>({
			name: "ACC-SINV-RETURN-AGAINST-INVOICE",
			doctype: "Sales Invoice",
			is_return: 1,
			return_against: "ACC-SINV-0001",
			items: [{ item_code: "ITEM-1", qty: -1 }],
			payments: [
				{
					mode_of_payment: "Cash",
					amount: -2625,
					base_amount: -2625,
					type: "Cash",
				},
			],
			rounded_total: -2625,
			grand_total: -2625,
			posa_refundable_amount: 0,
		});

		const { validateSubmission } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({}),
			stockSettings: ref({}),
			invoiceType: ref("Return"),
			formatFloat: (value) => Number(value || 0),
			isCashback: ref(true),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(validateSubmission(false)).rejects.toThrow(
			"Cannot refund 2625 for this return: only 0 was paid on the original invoice",
		);
	});

	it("allows gift card submission when no gift card mode of payment is configured", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValue({
			name: "ACC-SINV-0008",
			doctype: "Sales Invoice",
			docstatus: 1,
		});

		const invoiceDoc = ref<any>({
			name: "ACC-SINV-0008",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 0, type: "Cash" }],
			rounded_total: 300,
			grand_total: 300,
		});

		const giftCardRedemptions = ref([
			{
				gift_card_code: "GC-MISSING",
				amount: 300,
				cashier: "cashier@example.com",
			},
		]);

		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({
				posa_allow_submissions_in_background_job: 0,
				create_pos_invoice_instead_of_sales_invoice: 0,
				posa_allow_partial_payment: 0,
				payments: [
					{
						mode_of_payment: "Cash",
						type: "Cash",
						account: "1110 - Cash",
						default: 1,
					},
				],
			}),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				toastStore: { show: vi.fn() },
				uiStore: {
					setLastInvoice: vi.fn(),
					setLastStockAdjustment: vi.fn(),
				},
				customersStore: { setSelectedCustomer: vi.fn() },
				invoiceStore: { invoiceDoc: invoiceDoc.value },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			giftCardRedemptions,
			diff_payment: ref(0),
		});

		await expect(
			submitInvoice(false, {
				onFinishNavigation: vi.fn(),
			}),
		).resolves.not.toThrow();

		expect(invoiceService.submitInvoice).toHaveBeenCalledWith(
			expect.objectContaining({
				gift_card_redemptions: [
					expect.objectContaining({
						gift_card_code: "GC-MISSING",
						amount: 300,
					}),
				],
			}),
			expect.objectContaining({
				payments: [
					expect.objectContaining({
						mode_of_payment: "Cash",
						amount: 0,
					}),
				],
			}),
			"Invoice",
			expect.any(Object),
		);
	});

	it("waits for terminal unlock and retries the same pending invoice", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any)
			.mockResolvedValueOnce({
				ok: false,
				data: null,
				error: {
					code: "TERMINAL_LOCKED",
					message: "This POS terminal is locked.",
					retryable: false,
				},
				requestId: "terminal-locked-1",
				serverTime: null,
			})
			.mockResolvedValueOnce({
				name: "ACC-SINV-UNLOCKED",
				doctype: "Sales Invoice",
				docstatus: 1,
			});
		const requestTerminalUnlock = vi.fn().mockResolvedValue(true);
		const invoiceDoc = ref<any>({
			name: "ACC-SINV-UNLOCKED",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [{ item_code: "ITEM-1", qty: 1 }],
			payments: [{ mode_of_payment: "Cash", amount: 100, type: "Cash" }],
			grand_total: 100,
		});
		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_allow_submissions_in_background_job: 0 }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				employeeStore: { requestTerminalUnlock },
				toastStore: { show: vi.fn() },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(submitInvoice(false)).resolves.toMatchObject({
			success: true,
		});
		expect(requestTerminalUnlock).toHaveBeenCalledOnce();
		expect(invoiceService.submitInvoice).toHaveBeenCalledTimes(2);
		expect((invoiceService.submitInvoice as any).mock.calls[1][0]).toBe(
			(invoiceService.submitInvoice as any).mock.calls[0][0],
		);
		expect((invoiceService.submitInvoice as any).mock.calls[1][1]).toBe(
			(invoiceService.submitInvoice as any).mock.calls[0][1],
		);
	});

	it("keeps the invoice unsubmitted when terminal unlock is cancelled", async () => {
		const invoiceService = (
			await import("../src/posapp/services/invoiceService")
		).default;
		(invoiceService.submitInvoice as any).mockResolvedValueOnce({
			ok: false,
			data: null,
			error: {
				code: "TERMINAL_LOCKED",
				message: "This POS terminal is locked.",
				retryable: false,
			},
			requestId: "terminal-locked-cancelled",
			serverTime: null,
		});
		const invoiceDoc = ref<any>({
			name: "ACC-SINV-CANCELLED",
			doctype: "Sales Invoice",
			is_return: 0,
			items: [],
			payments: [{ mode_of_payment: "Cash", amount: 50, type: "Cash" }],
			grand_total: 50,
		});
		const { submitInvoice } = usePaymentSubmission({
			invoiceDoc,
			posProfile: ref({ posa_allow_submissions_in_background_job: 0 }),
			stockSettings: ref({}),
			invoiceType: ref("Invoice"),
			formatFloat: (value) => Number(value || 0),
			stores: {
				employeeStore: {
					requestTerminalUnlock: vi.fn().mockResolvedValue(false),
				},
				toastStore: { show: vi.fn() },
			},
			isCashback: ref(false),
			paidChange: ref(0),
			creditChange: ref(0),
			redeemedCustomerCredit: ref(0),
			customerCreditDict: ref([]),
			diff_payment: ref(0),
		});

		await expect(submitInvoice(false)).resolves.toEqual({
			cancelled: true,
			reason: "terminal_locked",
		});
		expect(invoiceService.submitInvoice).toHaveBeenCalledOnce();
	});
});
