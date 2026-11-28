import {
	getCompanyCurrency,
	getPlcConversionRate,
	getPriceListCurrency,
	getSelectedCurrency,
	priceListToSelectedCurrency,
} from "../../../utils/erpnextCurrency";

type CurrencyItem = {
	plc_conversion_rate?: number;
	original_rate?: number;
	original_currency?: string;
	rate?: number;
	currency?: string;
	base_rate?: number;
	base_price_list_rate?: number;
	price_list_rate?: number;
	[key: string]: unknown;
};

type CurrencyContext = {
	company?: { default_currency?: string };
	pos_profile: { currency: string };
	price_list_currency?: string;
	selected_currency?: string;
	exchange_rate?: number;
	conversion_rate?: number;
	plc_conversion_rate?: number;
	currency_precision?: number;
	flt?: (_value: unknown, _precision?: number) => number;
};

const parseFinite = (value: unknown): number | null => {
	const numeric = Number.parseFloat(String(value ?? ""));
	return Number.isFinite(numeric) ? numeric : null;
};

const parsePositiveFinite = (value: unknown): number | null => {
	const numeric = parseFinite(value);
	return numeric !== null && numeric > 0 ? numeric : null;
};

const resolveCompanyBuyingPrice = (
	item: CurrencyItem,
	companyCurrency: string,
	previousCurrency: string,
	previousConversionRate: number | null,
	priceListCurrency: string,
	plcToCompanyRate: number,
): number | null => {
	const storedCompanyBuying =
		parsePositiveFinite(item._company_buying_price) ??
		parsePositiveFinite(item.base_buying_rate);
	if (storedCompanyBuying !== null) {
		return storedCompanyBuying;
	}

	const scalarBuying =
		parsePositiveFinite(item.trade_price) ??
		parsePositiveFinite(item.buying_rate) ??
		parsePositiveFinite(item.buying_price);
	if (scalarBuying === null) {
		return null;
	}

	const buyingRateCurrency = String(item.buying_rate_currency || "").trim();
	const buyingPriceCurrency = String(item.buying_price_currency || "").trim();
	const scalarCurrency =
		buyingRateCurrency || buyingPriceCurrency || previousCurrency;

	if (!scalarCurrency || scalarCurrency === companyCurrency) {
		return scalarBuying;
	}
	const buyingExchangeRate = parsePositiveFinite(item.buying_exchange_rate);
	if (
		buyingPriceCurrency &&
		scalarCurrency === buyingPriceCurrency &&
		buyingExchangeRate !== null
	) {
		return scalarBuying * buyingExchangeRate;
	}
	if (
		previousCurrency &&
		scalarCurrency === previousCurrency &&
		previousConversionRate !== null &&
		previousConversionRate > 0
	) {
		return scalarBuying * previousConversionRate;
	}
	if (
		priceListCurrency &&
		scalarCurrency === priceListCurrency &&
		plcToCompanyRate > 0
	) {
		return scalarBuying * plcToCompanyRate;
	}
	return null;
};

export function useItemCurrency() {
	/**
	 * Calculates the rate from Price List Currency to Company Currency.
	 * @param {Object} item
	 * @param {Object} context - Needs: pos_profile, price_list_currency, exchange_rate, conversion_rate
	 * @returns {number}
	 */
	const getPlcToCompanyRate = (
		item: CurrencyItem,
		context: CurrencyContext,
	): number => {
		// Benchmark note: favor item-level plc_conversion_rate to avoid recomputing PLC->CC.
		return item.plc_conversion_rate ?? getPlcConversionRate(context);
	};

	/**
	 * Applies currency conversion to a single item.
	 * Updates item.rate, item.currency, item.base_rate, item.base_price_list_rate
	 * @param {Object} item
	 * @param {Object} context - Needs: pos_profile, price_list_currency, selected_currency, exchange_rate, currency_precision, flt
	 */
	const applyCurrencyConversionToItem = (
		item: CurrencyItem | null | undefined,
		context: CurrencyContext,
	) => {
		if (!item) return;
		const base = getCompanyCurrency(context) || context.pos_profile.currency;
		const previousCurrency = String(item.currency || "").trim();
		const previousConversionRate = parsePositiveFinite(item.conversion_rate);

		if (!item.original_rate) {
			const sourceRate =
				parseFinite(item.price_list_rate) ??
				parseFinite(item.rate) ??
				0;
			item.original_rate = sourceRate;
		}
		if (!item.original_currency) {
			item.original_currency =
				context.price_list_currency || item.currency || base;
		}

		// original_rate is in price list currency
		const price_list_rate = item.original_rate || 0;

		// Determine base rate using available conversion info (Price List -> Company)
		const plc_to_cc_rate = getPlcToCompanyRate(item, context);
		const base_rate = price_list_rate * plc_to_cc_rate;

		item.base_rate = base_rate;
		item.base_price_list_rate = base_rate;

		// Determine selected rate using exchange rate (Price List -> Selected)
		// item.original_currency is the Price List Currency
		const priceListCurrency = getPriceListCurrency(context) || base;
		const selectedCurrency = getSelectedCurrency(context) || base;

		// Preserve company-currency buying cost before switching item.currency/conversion_rate.
		const companyBuyingPrice = resolveCompanyBuyingPrice(
			item,
			base,
			previousCurrency,
			previousConversionRate,
			priceListCurrency,
			plc_to_cc_rate,
		);
		if (companyBuyingPrice !== null) {
			item._company_buying_price = companyBuyingPrice;
			item.base_buying_rate = companyBuyingPrice;
		}

		// Benchmark note: when PLC === SC, keep the displayed rate in PLC to avoid CC bleed-through.
		const converted_rate =
			selectedCurrency === priceListCurrency
				? price_list_rate
				: item.original_currency === selectedCurrency
					? price_list_rate
					: priceListToSelectedCurrency(context, price_list_rate);

		// context.flt is expected to be available or passed
		const flt = context.flt || ((v: unknown) => Number(v));

		item.rate = flt(converted_rate, context.currency_precision);
		item.currency = selectedCurrency;
		item.company_currency = base;
		item.price_list_rate = item.rate;

		let nextConversionRate: number;
		if (selectedCurrency === base) {
			nextConversionRate = 1;
		} else if (context.conversion_rate !== undefined) {
			nextConversionRate = parsePositiveFinite(context.conversion_rate) ?? 0;
		} else {
			const displayRate = parsePositiveFinite(context.exchange_rate);
			nextConversionRate =
				displayRate !== null && plc_to_cc_rate > 0
					? plc_to_cc_rate / displayRate
					: 0;
		}
		item.conversion_rate = nextConversionRate;

		if (companyBuyingPrice !== null) {
			if (nextConversionRate > 0) {
				const normalizedBuying =
					selectedCurrency === base
						? companyBuyingPrice
						: companyBuyingPrice / nextConversionRate;
				if (item.trade_price !== undefined) {
					item.trade_price = normalizedBuying;
				}
				if (item.buying_rate !== undefined) {
					item.buying_rate = normalizedBuying;
				}
				if (item.buying_price !== undefined) {
					item.buying_price = normalizedBuying;
				}
				item.buying_rate_currency = selectedCurrency;
			} else if (item.buying_rate_currency !== undefined) {
				item.buying_rate_currency = undefined;
			}
		}
	};

	/**
	 * Batch applies conversion to a list of items.
	 * @param {Array} items
	 * @param {Object} context
	 */
	const applyCurrencyConversionToItems = (
		items: CurrencyItem[],
		context: CurrencyContext,
	) => {
		if (!items || !items.length) return;
		items.forEach((it) => applyCurrencyConversionToItem(it, context));
	};

	return {
		getPlcToCompanyRate,
		applyCurrencyConversionToItem,
		applyCurrencyConversionToItems,
	};
}
