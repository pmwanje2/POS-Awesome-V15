import { ref, computed, type Ref } from "vue";

declare const flt: (_value: unknown, _precision?: number) => number;
declare const __: (_text: string, _args?: any[]) => string;

type PosPaySelectionArgs = {
	posProfile: Ref<any>;
	currency_filter: Ref<string>;
};

export function isInvoiceOutstandingConversionUnavailable(invoice: any): boolean {
	return Boolean(
		invoice?.outstanding_conversion_unavailable ||
			(invoice?.outstanding_amount_in_invoice_currency == null &&
				invoice?.party_account_currency &&
				invoice?.currency &&
				invoice.party_account_currency !== invoice.currency),
	);
}

export function resolveInvoiceCurrency(
	invoice: any,
	fallbackCurrency?: string | null,
): string {
	return String(invoice?.currency || fallbackCurrency || "").trim();
}

export function hasPositivePaymentMethodAmount(
	method: { amount?: unknown } | null | undefined,
): boolean {
	return flt(method?.amount) > 0;
}

export function getSelectedInvoiceCurrencies(
	invoices: any[],
	fallbackCurrency?: string | null,
): string[] {
	if (!Array.isArray(invoices) || !invoices.length) return [];
	const currencies = new Set<string>();
	invoices.forEach((invoice: any) => {
		const currency = resolveInvoiceCurrency(invoice, fallbackCurrency);
		if (currency) {
			currencies.add(currency);
		}
	});
	return Array.from(currencies);
}

export function calculateEligibleSelectedInvoicesTotal(
	invoices: any[],
	{
		currencyFilter = "ALL",
		fallbackCurrency = "",
		fltFn = (value: unknown) => flt(value),
	}: {
		currencyFilter?: string | null;
		fallbackCurrency?: string | null;
		fltFn?: (_value: unknown, _precision?: number) => number;
	} = {},
): number {
	if (!Array.isArray(invoices) || !invoices.length) return 0;
	const isAllFilter = !currencyFilter || currencyFilter === "ALL";
	if (isAllFilter && getSelectedInvoiceCurrencies(invoices, fallbackCurrency).length > 1) {
		return 0;
	}
	return invoices.reduce((acc: number, cur: any) => {
		if (isInvoiceOutstandingConversionUnavailable(cur)) {
			return acc;
		}
		const invoiceCurrency = resolveInvoiceCurrency(cur, fallbackCurrency);
		if (isAllFilter || invoiceCurrency === currencyFilter) {
			return acc + fltFn(cur?.outstanding_amount_in_invoice_currency ?? cur?.outstanding_amount ?? 0);
		}
		return acc;
	}, 0);
}

export function usePosPaySelection({
	posProfile,
	currency_filter,
}: PosPaySelectionArgs) {
	const selected_invoices = ref<any[]>([]);
	const selected_payments = ref<any[]>([]);
	const selected_mpesa_payments = ref<any[]>([]);
	const payment_methods = ref<any[]>([]);

	const hasMixedInvoiceCurrencies = computed(
		() =>
			getSelectedInvoiceCurrencies(
				selected_invoices.value,
				posProfile.value?.currency,
			).length > 1,
	);

	const total_selected_invoices = computed(() =>
		calculateEligibleSelectedInvoicesTotal(selected_invoices.value, {
			currencyFilter: currency_filter.value,
			fallbackCurrency: posProfile.value?.currency,
			fltFn: flt,
		}),
	);

	const total_selected_payments = computed(() => {
		if (!selected_payments.value.length) return 0;
		return selected_payments.value.reduce(
			(acc: number, cur: any) =>
				acc + flt(cur?.invoice_equivalent ?? cur?.unallocated_amount ?? 0),
			0,
		);
	});

	const total_selected_mpesa_payments = computed(() => {
		if (!selected_mpesa_payments.value.length) return 0;
		return selected_mpesa_payments.value.reduce(
			(acc: number, cur: any) => acc + flt(cur?.invoice_equivalent ?? cur?.amount ?? 0),
			0,
		);
	});

	const total_payment_methods = computed(() => {
		return payment_methods.value.reduce((acc: number, cur: any) => {
			if (!hasPositivePaymentMethodAmount(cur)) return acc;
			const amount = parseFloat(cur?.invoice_equivalent ?? cur?.amount ?? 0);
			return acc + (isNaN(amount) ? 0 : amount);
		}, 0);
	});

	const selected_payments_detail = computed(() => {
		if (!selected_payments.value.length) return [];
		return selected_payments.value.map((payment: any) => ({
			mode_of_payment: payment.mode_of_payment || __("Unknown"),
			paid_amount: flt(payment.paid_amount || 0),
			received_amount: flt(payment.received_amount || 0),
			unallocated_amount: flt(payment.unallocated_amount || 0),
			currency: payment.currency || posProfile.value?.currency,
			exchange_rate: flt(payment.source_exchange_rate ?? payment.exchange_rate ?? 1),
			account: payment.account || "",
		}));
	});

	const total_of_diff = computed(() => {
		const invoiceTotal = total_selected_invoices.value || 0;
		const paymentTotal =
			total_selected_payments.value +
			total_selected_mpesa_payments.value +
			total_payment_methods.value;
		return flt(invoiceTotal - paymentTotal);
	});

	function toggleInvoiceSelection(
		item: any,
		customerName: Ref<string>,
		onCustomerSelected?: (_v: string) => void,
	): boolean {
		const index = selected_invoices.value.findIndex(
			(i: any) => i.voucher_no === item.voucher_no,
		);
		if (index > -1) {
			selected_invoices.value.splice(index, 1);
			return true;
		}
		const existingCurrencies = getSelectedInvoiceCurrencies(
			selected_invoices.value,
			posProfile.value?.currency,
		);
		const nextCurrency = resolveInvoiceCurrency(item, posProfile.value?.currency);
		if (
			existingCurrencies.length > 0 &&
			nextCurrency &&
			existingCurrencies.some((currency) => currency !== nextCurrency)
		) {
			return false;
		}
		selected_invoices.value.push(item);
		if (item.customer && !customerName.value && onCustomerSelected) {
			onCustomerSelected(item.customer);
		}
		return true;
	}

	function isInvoiceSelected(item: any) {
		return selected_invoices.value.some(
			(i: any) => i.voucher_no === item.voucher_no,
		);
	}

	function clearSelections() {
		selected_invoices.value = [];
		selected_payments.value = [];
		selected_mpesa_payments.value = [];
	}

	function resetPaymentMethodAmounts() {
		payment_methods.value = payment_methods.value.map((method: any) => ({
			...method,
			amount: 0,
			invoice_amount: 0,
			invoice_equivalent: 0,
			base_amount:
				method?.base_amount !== undefined ? 0 : method?.base_amount,
		}));
	}

	return {
		selected_invoices,
		selected_payments,
		selected_mpesa_payments,
		payment_methods,
		hasMixedInvoiceCurrencies,
		total_selected_invoices,
		total_selected_payments,
		total_selected_mpesa_payments,
		total_payment_methods,
		selected_payments_detail,
		total_of_diff,
		toggleInvoiceSelection,
		isInvoiceSelected,
		clearSelections,
		resetPaymentMethodAmounts,
	};
}
