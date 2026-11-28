import { ref } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePosPaySubmission } from "../src/posapp/composables/pos/payments/usePosPaySubmission";

vi.mock("../src/offline/index", () => ({
	isOffline: vi.fn(() => false),
	saveOfflinePayment: vi.fn(),
}));

describe("usePosPaySubmission", () => {
	beforeEach(() => {
		(globalThis as any).__ = (value: string) => value;
		(globalThis as any).flt = (value: unknown) => Number(value || 0);
		(globalThis as any).frappe = {
			throw: (message: string) => {
				throw new Error(message);
			},
			call: vi.fn(),
			msgprint: vi.fn(),
			utils: {
				play_sound: vi.fn(),
			},
		};
	});

	it("triggers auto reconcile across all outstanding invoices after a successful submit", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-0001" }],
				},
			});
		});

		const autoReconcile = vi.fn().mockResolvedValue({
			summary: "Allocated $120 across 2 payment(s). Remaining outstanding: $0 across 0 invoice(s).",
			total_allocated: 120,
		});
		const getOutstandingInvoices = vi.fn();
		const getUnallocatedPayments = vi.fn();
		const getDraftMpesaPaymentsRegister = vi.fn();
		const setMpesaSearchParams = vi.fn();
		const eventBus = { emit: vi.fn() };

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(true),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 100 }]),
			selected_invoices: ref([]),
			selected_payments: ref([{ name: "ACC-PAY-EXISTING-1", unallocated_amount: 20 }]),
			selected_mpesa_payments: ref([{ name: "MPESA-1", amount: 10 }]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(20),
			total_selected_mpesa_payments: ref(10),
			total_payment_methods: ref(100),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus,
			get_outstanding_invoices: getOutstandingInvoices,
			get_unallocated_payments: getUnallocatedPayments,
			get_draft_mpesa_payments_register: getDraftMpesaPaymentsRegister,
			set_mpesa_search_params: setMpesaSearchParams,
			autoReconcile,
		});

		await processPayment();

		expect(autoReconcile).toHaveBeenCalledTimes(1);
		expect(autoReconcile).toHaveBeenCalledWith(null, {
			suppressToast: true,
		});
		expect(eventBus.emit).toHaveBeenCalledWith("show_message", {
			title: "Payment submitted. Allocated $120 across 2 payment(s). Remaining outstanding: $0 across 0 invoice(s).",
			color: "success",
		});
		expect(getOutstandingInvoices).toHaveBeenCalled();
		expect(getUnallocatedPayments).toHaveBeenCalled();
	});

	it("reconciles across all outstanding invoices after submit when no POS filter is selected", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-0003" }],
				},
			});
		});

		const autoReconcile = vi.fn().mockResolvedValue({
			summary: "Allocated $28020 across 1 payment(s). Remaining outstanding: $0 across 0 invoice(s).",
			total_allocated: 28020,
		});

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(true),
			payment_methods: ref([{ mode_of_payment: "Online", amount: 32000 }]),
			selected_invoices: ref([]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(32000),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile,
		});

		await processPayment();

		expect(autoReconcile).toHaveBeenCalledWith(null, {
			suppressToast: true,
		});
	});

	it("preserves per-entry tender currency metadata in the payment payload", async () => {
		let submittedPayload: any = null;
		(globalThis as any).frappe.call.mockImplementation(({ args, callback }: any) => {
			submittedPayload = args.payload;
			callback({ message: { new_payments_entry: [{ name: "ACC-PAY-0006" }] } });
		});

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(285),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([
				{
					mode_of_payment: "Cash",
					amount: 29.93,
					payment_currency: "PKR",
					invoice_equivalent: 0.105,
					invoice_exchange_rate: 0.003508772,
					company_exchange_rate: 1,
					rate_date: "2026-08-21",
					rate_source: "currency_exchange",
					bank_account: "Cash PKR - TC",
				},
			]),
			selected_invoices: ref([]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(0.105),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await processPayment();

		expect(submittedPayload.payment_methods[0]).toMatchObject({
			amount: 29.93,
			original_amount: 29.93,
			payment_currency: "PKR",
			invoice_equivalent: 0.105,
			invoice_exchange_rate: 0.003508772,
			exchange_rate: 1,
			bank_account: "Cash PKR - TC",
		});
	});

	it("clears entered payment method amounts after a successful submit", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-0004" }],
				},
			});
		});

		const paymentMethods = ref([
			{ mode_of_payment: "Cash", amount: 5000 },
			{ mode_of_payment: "Online", amount: 32000 },
		]);

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: paymentMethods,
			selected_invoices: ref([]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(37000),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: () => {
				paymentMethods.value = paymentMethods.value.map((method) => ({
					...method,
					amount: 0,
				}));
			},
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await processPayment();

		expect(paymentMethods.value).toEqual([
			{ mode_of_payment: "Cash", amount: 0 },
			{ mode_of_payment: "Online", amount: 0 },
		]);
	});

	it("skips post-submit auto reconcile when auto allocation is disabled", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-0002" }],
				},
			});
		});

		const autoReconcile = vi.fn().mockResolvedValue(undefined);

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 100 }]),
			selected_invoices: ref([]),
			selected_payments: ref([{ name: "ACC-PAY-EXISTING-1", unallocated_amount: 20 }]),
			selected_mpesa_payments: ref([]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(20),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(100),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile,
		});

		await processPayment();

		expect(autoReconcile).not.toHaveBeenCalled();
	});

	it("includes the selected posting date in the payment payload", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-0005" }],
				},
			});
		});

		const postingDate = ref("2026-03-29");

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			postingDate,
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 100 }]),
			selected_invoices: ref([]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(100),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await processPayment();

		const callConfig = (globalThis as any).frappe.call.mock.calls[0][0];
		expect(callConfig.args.payload.posting_date).toBe("2026-03-29");
	});

	it("sends posting date and opening shift when the user leaves reference fields blank", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-0007" }],
				},
			});
		});

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			postingDate: ref("2026-04-03"),
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref("   "),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 100 }]),
			selected_invoices: ref([]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(100),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await processPayment();

		const callConfig = (globalThis as any).frappe.call.mock.calls[0][0];
		expect(callConfig.args.payload.reference_no).toBe("POS-OPEN-0001");
		expect(callConfig.args.payload.reference_date).toBe("2026-04-03");
	});

	it("includes generic payment and party fields for supplier pay mode", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-0006" }],
				},
			});
		});

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Supp-001"),
			partyName: ref("Supp-001"),
			partyType: ref("Supplier"),
			paymentType: ref("Pay"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			postingDate: ref("2026-03-30"),
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Bank", amount: 250 }]),
			selected_invoices: ref([]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(250),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		} as any);

		await processPayment();

		const callConfig = (globalThis as any).frappe.call.mock.calls[0][0];
		expect(callConfig.args.payload.payment_type).toBe("Pay");
		expect(callConfig.args.payload.party_type).toBe("Supplier");
		expect(callConfig.args.payload.party).toBe("Supp-001");
	});

	it("adds a stable client request id to payment submissions", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({
				message: {
					new_payments_entry: [{ name: "ACC-PAY-1000" }],
				},
			});
		});

		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Test Company"),
			posProfile: ref({ name: "Main POS" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(1),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 100 }]),
			selected_invoices: ref([]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_invoices: ref(0),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(100),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await processPayment();

		const callConfig = (globalThis as any).frappe.call.mock.calls[0][0];
		expect(callConfig.args.payload.client_request_id).toEqual(expect.any(String));
	});

	it("blocks submission without calling RPC when selected invoices mix distinct currencies", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({ message: { new_payments_entry: [{ name: "ACC-PAY-MIXED" }] } });
		});
		const eventBus = { emit: vi.fn() };
		const { processPayment } = usePosPaySubmission({
			customerName: ref("Customer 727"),
			company: ref("Uganda Co"),
			posProfile: ref({ name: "POS-USD", currency: "USD" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(3700),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 10 }]),
			selected_invoices: ref([
				{
					voucher_no: "SINV-USD-10",
					currency: "USD",
					party_account_currency: "USD",
					outstanding_amount: 10,
					outstanding_amount_in_invoice_currency: 10,
				},
				{
					voucher_no: "SINV-UGX-37000",
					currency: "UGX",
					party_account_currency: "UGX",
					outstanding_amount: 37000,
					outstanding_amount_in_invoice_currency: 37000,
				},
			]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(10),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus,
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await expect(processPayment()).rejects.toThrow(/multiple currencies|single currency/i);
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();
		expect(eventBus.emit).toHaveBeenCalledWith(
			"show_message",
			expect.objectContaining({ color: "error" }),
		);
	});

	it("blocks submission for unconvertible selected invoices and submits eligible invoice-currency total otherwise", async () => {
		(globalThis as any).frappe.call.mockImplementation(({ callback }: any) => {
			callback({ message: { new_payments_entry: [{ name: "ACC-PAY-UNCONV" }] } });
		});
		const eventBus = { emit: vi.fn() };
		const { processPayment: submitUnconvertible } = usePosPaySubmission({
			customerName: ref("Customer EUR"),
			company: ref("Uganda Co"),
			posProfile: ref({ name: "POS-USD", currency: "USD" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(3700),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 10 }]),
			selected_invoices: ref([
				{
					voucher_no: "SINV-EUR-NOQUOTE",
					currency: "USD",
					party_account_currency: "EUR",
					outstanding_amount: 10,
					outstanding_amount_in_invoice_currency: null,
					outstanding_conversion_unavailable: true,
				},
			]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(10),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus,
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await expect(submitUnconvertible()).rejects.toThrow(/conversion rate is unavailable/i);
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();
		expect(eventBus.emit).toHaveBeenCalledWith(
			"show_message",
			expect.objectContaining({ color: "error" }),
		);

		// For a converted USD 10 invoice whose party account is UGX 37,000, payload total_selected_invoices must match display (10, not 37,000).
		let submittedPayload: any = null;
		(globalThis as any).frappe.call.mockImplementation(({ args, callback }: any) => {
			submittedPayload = args.payload;
			callback({ message: { new_payments_entry: [{ name: "ACC-PAY-CONVERTED" }] } });
		});

		const { processPayment: submitConverted } = usePosPaySubmission({
			customerName: ref("Customer UGX"),
			company: ref("Uganda Co"),
			posProfile: ref({ name: "POS-USD", currency: "USD" }),
			posOpeningShift: ref({ name: "POS-OPEN-0001" }),
			exchangeRate: ref(3700),
			invoiceTotalCurrency: ref("USD"),
			referenceNo: ref(""),
			referenceDate: ref(""),
			autoAllocatePaymentAmount: ref(false),
			payment_methods: ref([{ mode_of_payment: "Cash", amount: 10 }]),
			selected_invoices: ref([
				{
					voucher_no: "SINV-USD-UGX",
					currency: "USD",
					party_account_currency: "UGX",
					outstanding_amount: 37000,
					outstanding_amount_in_invoice_currency: 10,
					outstanding_conversion_unavailable: false,
				},
			]),
			selected_payments: ref([]),
			selected_mpesa_payments: ref([]),
			total_selected_payments: ref(0),
			total_selected_mpesa_payments: ref(0),
			total_payment_methods: ref(10),
			clearSelections: vi.fn(),
			resetPaymentMethodAmounts: vi.fn(),
			load_print_page: vi.fn(),
			eventBus: { emit: vi.fn() },
			get_outstanding_invoices: vi.fn(),
			get_unallocated_payments: vi.fn(),
			get_draft_mpesa_payments_register: vi.fn(),
			set_mpesa_search_params: vi.fn(),
			autoReconcile: vi.fn(),
		});

		await submitConverted();
		expect(submittedPayload.total_selected_invoices).toBe(10);
	});
});
