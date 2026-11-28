import { computed, unref, type Ref } from "vue";
import { resolveExchangeRate } from "../../../services/exchangeRateResolver";

type Options = {
	invoiceDoc: Ref<any>;
	posProfile: Ref<any>;
	changeDue: Ref<number>;
	currencyPrecision: Ref<number>;
	formatFloat: (value: unknown, precision?: number) => number;
};

const enabled = (value: unknown) => value === true || value === 1 || value === "1";

export function useChangeCurrencies(options: Options) {
	const featureEnabled = computed(
		() =>
			enabled(unref(options.posProfile)?.posa_enable_multi_currency_payments) &&
			enabled(unref(options.posProfile)?.posa_enable_multi_currency_change),
	);
	const allowedChangeCurrencies = computed(() => {
		const profile = unref(options.posProfile) || {};
		const doc = unref(options.invoiceDoc) || {};
		const configured = Array.isArray(profile.posa_allowed_currencies)
			? profile.posa_allowed_currencies
					.filter(
						(row: any) =>
							row?.currency &&
							(row.allow_for_change === undefined || enabled(row.allow_for_change)),
					)
					.map((row: any) => String(row.currency))
			: [];
		return [...new Set((configured.length ? configured : [doc.currency, profile.currency]).filter(Boolean))] as string[];
	});
	const rows = computed<any[]>(() => {
		const doc = unref(options.invoiceDoc);
		if (!doc) return [];
		if (!Array.isArray(doc.posa_change_returns)) doc.posa_change_returns = [];
		return doc.posa_change_returns;
	});
	const returnedTotal = computed(() =>
		options.formatFloat(
			rows.value.reduce((sum, row) => sum + Number(row?.invoice_amount || 0), 0),
			unref(options.currencyPrecision),
		),
	);
	const remainingChange = computed(() =>
		Math.max(
			options.formatFloat(unref(options.changeDue) - returnedTotal.value, unref(options.currencyPrecision)),
			0,
		),
	);

	const syncTotals = () => {
		const doc = unref(options.invoiceDoc);
		if (!doc) return;
		doc.posa_change_returned = returnedTotal.value;
		doc.posa_remaining_change = remainingChange.value;
	};

	const normalizeRow = async (row: any) => {
		const doc = unref(options.invoiceDoc) || {};
		const profile = unref(options.posProfile) || {};
		const companyCurrency =
			profile.company_currency ||
			doc.company_currency ||
			profile.currency ||
			doc.currency;
		const effectiveDate = doc.posting_date || window.frappe?.datetime?.nowdate?.();
		const invoiceRate = await resolveExchangeRate({
			profileName: profile.name,
			company: profile.company || doc.company,
			fromCurrency: row.currency,
			toCurrency: doc.currency,
			effectiveDate,
			purpose: "for_selling",
		});
		const companyRate = await resolveExchangeRate({
			profileName: profile.name,
			company: profile.company || doc.company,
			fromCurrency: row.currency,
			toCurrency: companyCurrency,
			effectiveDate,
			purpose: "for_selling",
		});
		if (!invoiceRate.found || !invoiceRate.rate || !companyRate.found || !companyRate.rate) {
			row._posa_rate_error = true;
			row.invoice_amount = 0;
			row.base_amount = 0;
			syncTotals();
			return false;
		}
		row.invoice_currency = doc.currency;
		row.company_currency = companyCurrency;
		row.exchange_rate = invoiceRate.rate;
		row.company_exchange_rate = companyRate.rate;
		row.invoice_amount = options.formatFloat(row.original_amount * invoiceRate.rate, unref(options.currencyPrecision));
		row.base_amount = options.formatFloat(row.original_amount * companyRate.rate, unref(options.currencyPrecision));
		row.rate_date = invoiceRate.rateDate || effectiveDate;
		row.rate_source = invoiceRate.source;
		row._posa_rate_error = false;
		syncTotals();
		return true;
	};

	const addRow = () => {
		const doc = unref(options.invoiceDoc);
		if (!doc) return;
		if (!Array.isArray(doc.posa_change_returns)) doc.posa_change_returns = [];
		doc.posa_change_returns.push({
			currency: doc.currency,
			invoice_currency: doc.currency,
			original_amount: 0,
			exchange_rate: 1,
			invoice_amount: 0,
		});
		syncTotals();
	};
	const removeRow = (index: number) => {
		rows.value.splice(index, 1);
		syncTotals();
	};
	const updateRowAmount = async (row: any, amount: unknown) => {
		row.original_amount = Math.max(options.formatFloat(amount, unref(options.currencyPrecision)), 0);
		return normalizeRow(row);
	};
	const updateRowCurrency = async (row: any, currency: string) => {
		row.currency = currency;
		return normalizeRow(row);
	};

	return {
		featureEnabled,
		allowedChangeCurrencies,
		rows,
		returnedTotal,
		remainingChange,
		addRow,
		removeRow,
		updateRowAmount,
		updateRowCurrency,
		syncTotals,
	};
}
