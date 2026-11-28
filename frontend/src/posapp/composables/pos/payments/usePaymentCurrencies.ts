import { computed, unref, type Ref } from "vue";
import {
	convertCurrencyAmount,
	resolveExchangeRate,
	type ExchangeRateResult,
} from "../../../services/exchangeRateResolver";

type PaymentCurrencyOptions = {
	invoiceDoc: Ref<any>;
	posProfile: Ref<any>;
	currencyPrecision: Ref<number>;
	formatFloat?: (value: unknown, precision?: number) => number;
};

const enabled = (value: unknown) =>
	value === true || value === 1 || value === "1";

export function usePaymentCurrencies(options: PaymentCurrencyOptions) {
	const flt = (value: unknown) =>
		options.formatFloat
			? options.formatFloat(value, unref(options.currencyPrecision))
			: Number(value) || 0;
	const preciseTenderAmount = (value: unknown) => {
		const numeric = Number(value);
		return Number.isFinite(numeric) ? Number(numeric.toFixed(9)) : 0;
	};

	const multiCurrencyEnabled = computed(() =>
		enabled(unref(options.posProfile)?.posa_enable_multi_currency_payments),
	);
	const allowCurrencySelection = computed(
		() =>
			multiCurrencyEnabled.value &&
			enabled(unref(options.posProfile)?.posa_allow_payment_currency_selection),
	);
	const allowManualRate = computed(
		() =>
			multiCurrencyEnabled.value &&
			enabled(unref(options.posProfile)?.posa_allow_manual_payment_exchange_rate),
	);

	const companyCurrency = computed(() => {
		const profile = unref(options.posProfile) || {};
		const doc = unref(options.invoiceDoc) || {};
		return (
			profile.company_currency ||
			doc.company_currency ||
			profile.currency ||
			doc.currency ||
			""
		);
	});

	const allowedPaymentCurrencies = computed(() => {
		const profile = unref(options.posProfile) || {};
		const doc = unref(options.invoiceDoc) || {};
		const configured = Array.isArray(profile.posa_allowed_currencies)
			? profile.posa_allowed_currencies
					.filter(
						(row: any) =>
							row?.currency &&
							(row.allow_for_payments === undefined || enabled(row.allow_for_payments)),
					)
					.map((row: any) => String(row.currency))
			: [];
		const fallback = [
			doc.currency,
			companyCurrency.value,
			profile.posa_default_payment_currency,
			...(Array.isArray(profile.payments)
				? profile.payments.flatMap((row: any) => [
						row?.posa_default_payment_currency,
						row?.account_currency,
					])
				: []),
		];
		return [...new Set((configured.length ? configured : fallback).filter(Boolean))] as string[];
	});

	const resolverContext = (fromCurrency: string, toCurrency: string, manualRate?: unknown) => {
		const profile = unref(options.posProfile) || {};
		const doc = unref(options.invoiceDoc) || {};
		return {
			profileName: profile.name,
			company: profile.company || doc.company,
			fromCurrency,
			toCurrency,
			effectiveDate: doc.posting_date || window.frappe?.datetime?.nowdate?.(),
			purpose: "for_selling" as const,
			allowManualRate: allowManualRate.value,
			manualRate: manualRate as number | string | null | undefined,
		};
	};

	const resolveRate = async (
		fromCurrency: string,
		toCurrency: string,
		manualRate?: unknown,
	): Promise<ExchangeRateResult> =>
		resolveExchangeRate(resolverContext(fromCurrency, toCurrency, manualRate));

	const defaultCurrencyFor = (payment: any) => {
		const profile = unref(options.posProfile) || {};
		const doc = unref(options.invoiceDoc) || {};
		if (!multiCurrencyEnabled.value) return doc.currency;
		const configured =
			payment?.posa_default_payment_currency ||
			profile.posa_default_payment_currency ||
			payment?.account_currency ||
			doc.currency;
		return allowedPaymentCurrencies.value.includes(configured) ? configured : doc.currency;
	};

	const clearPayment = (payment: any) => {
		payment.amount = 0;
		payment.base_amount = 0;
		payment.posa_original_amount = 0;
		payment.posa_account_amount = 0;
		payment._posa_rate_error = null;
	};

	const normalizePayment = async (
		payment: any,
		{ preserveInvoiceAmount = false }: { preserveInvoiceAmount?: boolean } = {},
	) => {
		const doc = unref(options.invoiceDoc) || {};
		const invoiceCurrency = doc.currency;
		const paymentCurrency = payment.posa_payment_currency || defaultCurrencyFor(payment);
		payment.posa_payment_currency = paymentCurrency;

		const manualRate =
			allowManualRate.value && payment.posa_rate_source === "manual"
				? payment.posa_exchange_rate
				: null;
		const invoiceRate = await resolveRate(paymentCurrency, invoiceCurrency, manualRate);
		const companyRate = await resolveRate(paymentCurrency, companyCurrency.value);
		if (!invoiceRate.found || !invoiceRate.rate || !companyRate.found || !companyRate.rate) {
			payment._posa_rate_error = "rate_unavailable";
			payment.posa_exchange_rate = invoiceRate.rate || 0;
			payment.posa_company_exchange_rate = companyRate.rate || 0;
			return false;
		}

		let original = preciseTenderAmount(payment.posa_original_amount);
		if (preserveInvoiceAmount) {
			original = preciseTenderAmount(flt(payment.amount) / invoiceRate.rate);
		}
		const invoiceAmount = convertCurrencyAmount(original, invoiceRate.rate);
		const baseAmount = convertCurrencyAmount(original, companyRate.rate);
		if (invoiceAmount === null || baseAmount === null) {
			payment._posa_rate_error = "rate_unavailable";
			return false;
		}

		payment.posa_original_amount = original;
		payment.posa_exchange_rate = invoiceRate.rate;
		payment.posa_company_exchange_rate = companyRate.rate;
		payment.posa_rate_date = invoiceRate.rateDate || invoiceRate.effectiveDate;
		payment.posa_rate_source = invoiceRate.source;
		payment.amount = flt(invoiceAmount);
		payment.base_amount = flt(baseAmount);
		payment._posa_rate_error = null;
		return true;
	};

	const initializePayment = async (payment: any) => {
		const doc = unref(options.invoiceDoc) || {};
		const hadCurrency = Boolean(payment.posa_payment_currency);
		payment.posa_payment_currency = payment.posa_payment_currency || defaultCurrencyFor(payment);
		if (!multiCurrencyEnabled.value || payment.posa_payment_currency === doc.currency) {
			payment.posa_original_amount = hadCurrency
				? flt(payment.posa_original_amount)
				: flt(payment.amount);
		}
		return normalizePayment(payment, {
			preserveInvoiceAmount: !hadCurrency && payment.posa_payment_currency !== doc.currency,
		});
	};

	const initializePayments = async () => {
		const doc = unref(options.invoiceDoc);
		if (!Array.isArray(doc?.payments)) return true;
		const results = await Promise.all(doc.payments.map((payment: any) => initializePayment(payment)));
		return results.every(Boolean);
	};

	const updateOriginalAmount = async (payment: any, value: unknown) => {
		payment.posa_original_amount = flt(value);
		if (unref(options.invoiceDoc)?.is_return && payment.posa_original_amount > 0) {
			payment.posa_original_amount = -payment.posa_original_amount;
		}
		return normalizePayment(payment);
	};

	const updateCurrency = async (payment: any, currency: string) => {
		const doc = unref(options.invoiceDoc) || {};
		const invoiceAmount = flt(payment.amount);
		payment.posa_payment_currency = currency || doc.currency;
		payment.posa_exchange_rate = null;
		payment.posa_rate_source = null;
		return normalizePayment(payment, { preserveInvoiceAmount: true }).then((ok) => {
			if (!ok) payment.amount = invoiceAmount;
			return ok;
		});
	};

	const setInvoiceEquivalent = async (
		payment: any,
		invoiceAmount: unknown,
		companyAmount?: number,
	) => {
		payment.amount = flt(invoiceAmount);
		if (Number.isFinite(companyAmount)) {
			if (!Number(payment.posa_company_exchange_rate)) {
				const initialized = await normalizePayment(payment, { preserveInvoiceAmount: true });
				if (!initialized) return false;
			}
			const companyRate = Number(payment.posa_company_exchange_rate);
			if (companyRate > 0) {
				payment.posa_original_amount = preciseTenderAmount(
					Number(companyAmount) / companyRate,
				);
				return normalizePayment(payment);
			}
		}
		return normalizePayment(payment, { preserveInvoiceAmount: true });
	};

	return {
		multiCurrencyEnabled,
		allowCurrencySelection,
		allowManualRate,
		companyCurrency,
		allowedPaymentCurrencies,
		initializePayment,
		initializePayments,
		normalizePayment,
		updateOriginalAmount,
		updateCurrency,
		setInvoiceEquivalent,
		clearPayment,
	};
}
