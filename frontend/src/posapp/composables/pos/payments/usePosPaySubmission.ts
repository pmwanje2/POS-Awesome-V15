import { ref, type Ref } from "vue";
import { isOffline, saveOfflinePayment } from "../../../../offline/index";
import { ensurePaymentClientRequestId } from "../../../../offline/idempotency";
import {
	calculateEligibleSelectedInvoicesTotal,
	getSelectedInvoiceCurrencies,
	hasPositivePaymentMethodAmount,
	isInvoiceOutstandingConversionUnavailable,
	resolveInvoiceCurrency,
} from "./usePosPaySelection";

declare const frappe: any;
declare const __: (_text: string, _args?: any[]) => string;
declare const flt: (_value: unknown, _precision?: number) => number;

type PosPaySubmissionArgs = {
	customerName: Ref<string>;
	partyName?: Ref<string>;
	partyType?: Ref<string>;
	paymentType?: Ref<string>;
	company: Ref<any>;
	companyCurrency?: Ref<string | null> | Ref<string>;
	posProfile: Ref<any>;
	posOpeningShift: Ref<any>;
	postingDate?: Ref<string | null>;
	currencyFilter?: Ref<string>;
	exchangeRate: Ref<number | null> | Ref<number>;
	requiresExchangeRate?: Ref<boolean>;
	exchangeRateLoading?: Ref<boolean>;
	exchangeRateError?: Ref<string | null> | Ref<string>;
	exchangeRateRequestKey?: Ref<string>;
	invoiceTotalCurrency: Ref<string>;
	referenceNo: Ref<string>;
	referenceDate: Ref<string>;
	autoAllocatePaymentAmount: Ref<boolean>;
	payment_methods: Ref<any[]>;
	selected_invoices: Ref<any[]>;
	selected_payments: Ref<any[]>;
	selected_mpesa_payments: Ref<any[]>;
	total_selected_payments: Ref<number>;
	total_selected_mpesa_payments: Ref<number>;
	total_payment_methods: Ref<number>;
	clearSelections: () => void;
	resetPaymentMethodAmounts: () => void;
	load_print_page: (_name: string) => void;
	eventBus: { emit: (_event: string, _payload?: unknown) => void };
	get_outstanding_invoices: () => void;
	get_unallocated_payments: () => void;
	get_draft_mpesa_payments_register: () => void;
	set_mpesa_search_params: () => void;
	autoReconcile: (
		_posProfileSearch?: string | null,
		_options?: { suppressToast?: boolean },
	) => Promise<any>;
};

export function usePosPaySubmission({
	customerName,
	partyName,
	partyType,
	paymentType,
	company,
	companyCurrency,
	posProfile,
	posOpeningShift,
	postingDate,
	currencyFilter,
	exchangeRate,
	requiresExchangeRate,
	exchangeRateLoading,
	exchangeRateError,
	exchangeRateRequestKey,
	invoiceTotalCurrency,
	referenceNo,
	referenceDate,
	autoAllocatePaymentAmount,
	payment_methods,
	selected_invoices,
	selected_payments,
	selected_mpesa_payments,
	total_selected_payments,
	total_selected_mpesa_payments,
	total_payment_methods,
	clearSelections,
	resetPaymentMethodAmounts,
	load_print_page,
	eventBus,
	get_outstanding_invoices,
	get_unallocated_payments,
	get_draft_mpesa_payments_register,
	set_mpesa_search_params,
	autoReconcile,
}: PosPaySubmissionArgs) {
	const isSubmitting = ref(false);

	async function processPayment({
		printAfter = false,
	}: { printAfter?: boolean } = {}) {
		if (isSubmitting.value) return;

		isSubmitting.value = true;
		const party = partyName?.value || customerName.value;
		const resolvedPartyType = partyType?.value || "Customer";
		const resolvedPaymentType = paymentType?.value || "Receive";
		const resolvedPostingDate = postingDate?.value || null;
		const resolvedReferenceNo =
			referenceNo?.value?.trim() || posOpeningShift.value?.name || null;
		const resolvedReferenceDate =
			referenceDate?.value?.trim() || resolvedPostingDate || null;

		const finalizeSubmission = () => {
			clearSelections();
			resetPaymentMethodAmounts();
			referenceNo.value = "";
			referenceDate.value = "";
			customerName.value = party;
			get_outstanding_invoices();
			get_unallocated_payments();
			set_mpesa_search_params();
			get_draft_mpesa_payments_register();
			eventBus.emit("payment-submitted");
		};

		try {
			if (!party) {
				frappe.throw(__("Please select a party"));
			}
			const selectedInvoiceCurrencies = getSelectedInvoiceCurrencies(
				selected_invoices.value,
				posProfile.value?.currency,
			);
			if (selectedInvoiceCurrencies.length > 1) {
				const message = __(
					"Cannot submit payment for invoices in multiple currencies. Select invoices in a single currency.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}
			const normalizedSubmissionCurrency = String(
				invoiceTotalCurrency.value || "",
			).trim();
			const normalizedCurrencyFilter = String(currencyFilter?.value || "").trim();
			const activeCurrencyFilter =
				normalizedCurrencyFilter && normalizedCurrencyFilter !== "ALL"
					? normalizedCurrencyFilter
					: "";
			const hasCurrencyMismatch = selected_invoices.value.some((invoice: any) => {
				const invoiceCurrency = resolveInvoiceCurrency(
					invoice,
					posProfile.value?.currency,
				);
				if (activeCurrencyFilter && invoiceCurrency !== activeCurrencyFilter) {
					return true;
				}
				if (
					normalizedSubmissionCurrency &&
					invoiceCurrency &&
					invoiceCurrency !== normalizedSubmissionCurrency
				) {
					return true;
				}
				if (!normalizedSubmissionCurrency && invoiceCurrency) {
					return true;
				}
				return false;
			});
			if (hasCurrencyMismatch) {
				const message = __(
					"Selected invoice currency does not match the active payment currency filter.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}
			if (
				selected_invoices.value.some((invoice: any) =>
					isInvoiceOutstandingConversionUnavailable(invoice),
				)
			) {
				const message = __(
					"Cannot submit payment for an invoice whose currency conversion rate is unavailable.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}

			const resolvedPurpose =
				resolvedPaymentType === "Pay" || resolvedPartyType === "Supplier"
					? "for_buying"
					: "for_selling";
			const resolvedCompanyCurrency = String(
				companyCurrency?.value || posProfile.value?.company_currency || "",
			).trim();
			const isMainFxRequired =
				requiresExchangeRate !== undefined
					? Boolean(requiresExchangeRate.value)
					: Boolean(
							normalizedSubmissionCurrency &&
								resolvedCompanyCurrency &&
								normalizedSubmissionCurrency !== resolvedCompanyCurrency,
						);
			const expectedMainFxKey =
				resolvedPostingDate &&
				normalizedSubmissionCurrency &&
				resolvedCompanyCurrency
					? `${normalizedSubmissionCurrency}|${resolvedCompanyCurrency}|${resolvedPurpose}|${resolvedPostingDate}`
					: "";

			if (
				exchangeRateLoading?.value &&
				(requiresExchangeRate === undefined || requiresExchangeRate.value)
			) {
				const message = __(
					"Exchange rate lookup is still in progress. Please wait and try again.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}
			if (
				(isMainFxRequired &&
					(Boolean(exchangeRateError?.value) ||
						!(flt(exchangeRate.value) > 0))) ||
				(requiresExchangeRate === undefined && Boolean(exchangeRateError?.value))
			) {
				const message = __(
					"Resolve all exchange rates before submitting the payment.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}
			if (
				isMainFxRequired &&
				exchangeRateRequestKey?.value &&
				expectedMainFxKey &&
				exchangeRateRequestKey.value !== expectedMainFxKey
			) {
				const message = __(
					"Exchange rate is out of date for the selected posting date or currency. Please wait for the rate to refresh.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}

			const isRowFxPendingOrStale = (
				row: any,
				kind: "method" | "selected",
			): { loading: boolean; error: boolean; stale: boolean } => {
				const loading = Boolean(row?._rate_loading);
				const error = Boolean(row?._rate_error);
				if (loading || error) {
					return { loading, error, stale: false };
				}
				if (row?._fx_request_key === null) {
					return { loading: false, error: false, stale: true };
				}
				if (
					typeof row?._fx_request_key === "string" &&
					row._fx_request_key &&
					resolvedPostingDate &&
					normalizedSubmissionCurrency
				) {
					const expectedPrefix = `${resolvedPostingDate}|${resolvedPurpose}|${normalizedSubmissionCurrency}`;
					if (kind === "method") {
						const rowPaymentCurrency = String(
							row?.payment_currency || "",
						).trim();
						if (rowPaymentCurrency && resolvedCompanyCurrency) {
							const expectedFull = `${expectedPrefix}|${rowPaymentCurrency}|${resolvedCompanyCurrency}`;
							if (row._fx_request_key !== expectedFull) {
								return { loading: false, error: false, stale: true };
							}
						} else if (rowPaymentCurrency) {
							if (
								!row._fx_request_key.startsWith(
									`${expectedPrefix}|${rowPaymentCurrency}|`,
								)
							) {
								return { loading: false, error: false, stale: true };
							}
						} else if (!row._fx_request_key.startsWith(`${expectedPrefix}|`)) {
							return { loading: false, error: false, stale: true };
						}
					} else {
						const rowCurrency = String(
							row?.currency || posProfile.value?.currency || "",
						).trim();
						if (rowCurrency) {
							const expectedFull = `${expectedPrefix}|${rowCurrency}`;
							if (row._fx_request_key !== expectedFull) {
								return { loading: false, error: false, stale: true };
							}
						} else if (!row._fx_request_key.startsWith(`${expectedPrefix}|`)) {
							return { loading: false, error: false, stale: true };
						}
					}
				}
				return { loading: false, error: false, stale: false };
			};

			const positivePaymentMethods = payment_methods.value.filter(hasPositivePaymentMethodAmount);
			const allRowFxStates = [
				...positivePaymentMethods.map((row: any) => isRowFxPendingOrStale(row, "method")),
				...selected_payments.value.map((row: any) =>
					isRowFxPendingOrStale(row, "selected"),
				),
				...selected_mpesa_payments.value.map((row: any) =>
					isRowFxPendingOrStale(row, "selected"),
				),
			];
			if (allRowFxStates.some((state) => state.loading)) {
				const message = __(
					"Exchange rate lookup is still in progress. Please wait and try again.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}
			if (allRowFxStates.some((state) => state.stale)) {
				const message = __(
					"Exchange rate is out of date for the selected posting date or currency. Please wait for the rate to refresh.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}
			if (allRowFxStates.some((state) => state.error)) {
				const message = __(
					"Resolve all exchange rates before submitting the payment.",
				);
				eventBus?.emit?.("show_message", {
					title: message,
					color: "error",
				});
				frappe.throw(message);
			}

			const total_payments =
				total_selected_payments.value +
				total_selected_mpesa_payments.value +
				total_payment_methods.value;

			if (total_payments <= 0) {
				frappe.throw(__("Please make a payment or select a payment"));
			}

			const hasNewPayments = flt(total_payment_methods.value) > 0;
			const hasAllocatedSelections =
				flt(total_selected_payments.value) > 0 ||
				flt(total_selected_mpesa_payments.value) > 0;

			if (
				!hasNewPayments &&
				selected_invoices.value.length === 0 &&
				hasAllocatedSelections
			) {
				frappe.throw(__("Please select an invoice"));
			}

			const selectedInvoicesForPayload = selected_invoices.value.map(
				(invoice: any) => ({ ...invoice }),
			);

			const totalSelectedInvoicesAmount = calculateEligibleSelectedInvoicesTotal(
				selectedInvoicesForPayload,
				{
					currencyFilter: activeCurrencyFilter || "ALL",
					fallbackCurrency: posProfile.value?.currency,
					fltFn: flt,
				},
			);

		const payload = {
			customer: party,
			party,
			party_type: resolvedPartyType,
			payment_type: resolvedPaymentType,
			company: company.value,
			currency: invoiceTotalCurrency.value,
			posting_date: resolvedPostingDate,
			exchange_rate: exchangeRate.value || null,
			reference_no: resolvedReferenceNo,
			reference_date: resolvedReferenceDate,
			pos_opening_shift_name: posOpeningShift.value.name,
			pos_profile_name: posProfile.value.name,
			pos_profile: posProfile.value,
				payment_methods: positivePaymentMethods.map((m: any) => ({
					mode_of_payment: m.mode_of_payment,
					amount: m.amount,
					payment_currency: m.payment_currency,
					original_amount: m.amount,
					invoice_equivalent: m.invoice_equivalent,
					invoice_exchange_rate: m.invoice_exchange_rate,
					exchange_rate: m.company_exchange_rate,
					rate_date: m.rate_date,
					rate_source: m.rate_source,
					row_id: m.row_id,
					bank_account: m.bank_account || null,
				})),
				selected_invoices: selectedInvoicesForPayload,
				selected_payments: selected_payments.value,
				total_selected_invoices: flt(totalSelectedInvoicesAmount),
				selected_mpesa_payments: selected_mpesa_payments.value,
				total_selected_payments: flt(total_selected_payments.value),
				total_payment_methods: flt(total_payment_methods.value),
				auto_allocate_payment_amount: !!autoAllocatePaymentAmount.value,
				total_selected_mpesa_payments: flt(
					total_selected_mpesa_payments.value,
				),
			};
			ensurePaymentClientRequestId(payload);

			if (isOffline()) {
				try {
					await saveOfflinePayment({ args: { payload } });
					eventBus.emit("show_message", {
						title: __("Payment saved offline"),
						color: "warning",
					});
					finalizeSubmission();
				} catch (error: any) {
					frappe.msgprint(
						__("Cannot Save Offline Payment: ") +
							(error.message || __("Unknown error")),
					);
				}
				return;
			}

			const response: any = await new Promise((resolve, reject) => {
				frappe.call({
					method: "posawesome.posawesome.api.payment_entry.process_pos_payment",
					args: { payload },
					freeze: true,
					freeze_message: __("Processing Payment"),
					callback: (r: any) => resolve(r),
					error: (err: any) => reject(err),
				});
			});

			if (!response || !response.message) {
				return;
			}

			frappe.utils.play_sound("submit");

			// Show exchange gain/loss notification as toast (non-intrusive)
			if (response.message?.net_gain_loss) {
				const netAmount = response.message.net_gain_loss;
				const currency = response.message.exchange_gain_loss_summary?.[0]?.currency || '';
				const label = netAmount >= 0 ? __("Gain") : __("Loss");

				// Use toast notification (auto-dismiss)
				eventBus.emit("show_notification", {
					message: __("{0}: {1} {2}", [label, Math.abs(netAmount).toFixed(2), currency]),
					type: netAmount >= 0 ? "success" : "warning",
					duration: 4000,
				});
			}

			if (autoAllocatePaymentAmount.value) {
				const autoReconcileResult = await autoReconcile(null, {
					suppressToast: true,
				});
				const autoReconcileSummary =
					autoReconcileResult?.summary ||
					__("Auto reconciliation completed after payment submit.");
				eventBus.emit("show_message", {
					title: `${__("Payment submitted.")} ${autoReconcileSummary}`,
					color: autoReconcileResult?.total_allocated ? "success" : "info",
				});
			}

			if (printAfter) {
				const payment_name =
					response.message.new_payments_entry &&
					response.message.new_payments_entry.length > 0
						? response.message.new_payments_entry[0].name
						: null;

				if (payment_name) {
					load_print_page(payment_name);
				} else {
					frappe.msgprint(
						__(
							"Payment submitted but print function could not be executed. Payment name not found.",
						),
					);
				}
			}

			finalizeSubmission();
		} catch (error) {
			console.error("Failed to process payment", error);
			throw error;
		} finally {
			isSubmitting.value = false;
		}
	}

	return {
		isSubmitting,
		processPayment,
	};
}
