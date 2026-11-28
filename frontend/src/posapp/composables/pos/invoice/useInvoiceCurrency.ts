/**
 * Currency, exchange-rate, and price-list management for the active invoice.
 *
 * **Two exchange rates**
 * The composable tracks two distinct rates that serve different purposes:
 * - `exchange_rate` — price-list currency → selected display currency. Used when
 *   converting item prices for display in the customer's preferred currency.
 * - `conversion_rate` — selected currency → company base currency. Used in
 *   accounting fields (`base_*`) on the invoice document.
 *
 * **Currency and price list data**
 * `fetch_available_currencies` retrieves supported currencies from the server and
 * falls back to `getCachedCurrencyOptions` when offline. Similarly,
 * `fetch_price_lists` uses `getCachedPriceListMeta` as its offline source.
 *
 * **Precision helpers**
 * `flt(value, precision?)` rounds to `currency_precision` by default, while
 * `roundAmount(value)` always uses `currency_precision`. These wrap the shared
 * `format.methods.flt` utility.
 *
 * **Item rate propagation**
 * Calling `update_currency` or `update_item_rates` iterates the current invoice
 * items and recalculates their `rate`, `base_rate`, and `amount` fields using the
 * active exchange and conversion rates. The `bus` event system notifies other
 * composables (e.g. discount recalculation) after each rate change.
 */
import { ref, computed, watch } from "vue";
import { useInvoiceStore } from "../../../stores/invoiceStore";
import { useToastStore } from "../../../stores/toastStore";
import { useUIStore } from "../../../stores/uiStore";
import {
	getCachedCurrencyOptions,
	getCachedPriceListMeta,
	saveCurrencyOptionsCache,
	savePriceListMetaCache,
} from "../../../../offline/index";
import { resolveExchangeRate } from "../../../services/exchangeRateResolver";
import {
	fromCompanyCurrency,
	getPlcConversionRate,
	toCompanyCurrency,
} from "../../../utils/erpnextCurrency";

// @ts-ignore
const __ = window.__ || ((s) => s);
// @ts-ignore
const frappe = window.frappe;

/**
 * useInvoiceCurrency Composable
 * Manages currency conversion, exchange rates, and pricing list logic.
 */
export function useInvoiceCurrency() {
	const invoiceStore = useInvoiceStore();
	const toastStore = useToastStore();
	const uiStore = useUIStore();

	const available_currencies = ref<any[]>([]);
	const selected_currency = ref("");
	const exchange_rate = ref(1);
	const conversion_rate = ref(1);
	const exchange_rate_date = ref(frappe.datetime.nowdate());
	const price_lists = ref<string[]>([]);
	const selected_price_list = ref("");
	const price_list_currency = ref("");

	// Precision settings
	const float_precision = ref(6);
	const currency_precision = ref(6);

	// Get company and pos_profile from UI store
	const pos_profile = computed(() => uiStore.posProfile);
	const company = computed(() => uiStore.companyDoc);

	// Watch for pos_profile changes to update precision
	watch(
		pos_profile,
		(newProfile) => {
			if (newProfile) {
				const prec = parseInt(newProfile.posa_decimal_precision);
				if (!isNaN(prec)) {
					float_precision.value = prec;
					currency_precision.value = prec;
				}
			}
		},
		{ immediate: true },
	);

	const flt = (value: any, precision: number | null = null) => {
		const prec = precision !== null ? precision : float_precision.value;
		const _value = Number(value);
		if (isNaN(_value)) return 0;
		if (Math.abs(_value) < 0.000001) return _value;
		return Number((_value || 0).toFixed(prec));
	};

	const parseFinite = (value: unknown): number | null => {
		const numeric = Number.parseFloat(String(value ?? ""));
		return Number.isFinite(numeric) ? numeric : null;
	};

	const resolveOriginalBasePriceListRate = (
		item: any,
		currencyContext: Record<string, any>,
	): number | null => {
		const conversionFactor = parseFinite(item?.conversion_factor) || 1;
		const originalBasePriceListRate = parseFinite(
			item?.original_base_price_list_rate,
		);
		if (originalBasePriceListRate !== null) {
			return originalBasePriceListRate * conversionFactor;
		}

		const originalRate = parseFinite(item?.original_rate);
		if (originalRate !== null) {
			return originalRate * getPlcConversionRate(currencyContext);
		}

		return null;
	};

	const refreshCanonicalBaseRates = (
		item: any,
		currencyContext: Record<string, any>,
	) => {
		if (!item || item._manual_rate_set === true || item.locked_price) {
			return;
		}

		const canonicalBasePriceListRate =
			resolveOriginalBasePriceListRate(item, currencyContext);
		if (canonicalBasePriceListRate === null) {
			return;
		}

		const baseDiscountAmount =
			parseFinite(item.base_discount_amount) ?? 0;
		item.base_price_list_rate = canonicalBasePriceListRate;

		if (baseDiscountAmount > 0) {
			item.base_rate = Math.max(
				canonicalBasePriceListRate - baseDiscountAmount,
				0,
			);
		} else if (!item.posa_offer_applied) {
			item.base_rate = canonicalBasePriceListRate;
		}
	};

	const fetch_available_currencies = async () => {
		if (!pos_profile.value) return [];
		const profileName = pos_profile.value.name;
		const configured = Array.isArray(pos_profile.value.posa_allowed_currencies)
			? pos_profile.value.posa_allowed_currencies
					.filter(
						(row: any) =>
							row?.allow_for_invoices === undefined ||
							row.allow_for_invoices === 1 ||
							row.allow_for_invoices === "1" ||
							row.allow_for_invoices === true,
					)
					.map((row: any) => row?.currency)
					.filter(Boolean)
			: [];
		if (configured.length) {
			available_currencies.value = [...new Set(configured)].map((currency) => ({
				value: currency,
				title: currency,
			}));
			const baseCurrency = pos_profile.value.currency;
			available_currencies.value.sort((a, b) => {
				if (a.value === baseCurrency) return -1;
				if (b.value === baseCurrency) return 1;
				return a.value.localeCompare(b.value);
			});
			selected_currency.value ||= baseCurrency;
			saveCurrencyOptionsCache(profileName, available_currencies.value);
			return available_currencies.value;
		}
		try {
			const r = await frappe.call({
				method: "posawesome.posawesome.api.invoices.get_available_currencies",
			});

			if (r.message) {
				const baseCurrency = pos_profile.value.currency;
				available_currencies.value = r.message.map((currency: any) => ({
					value: currency.name,
					title: currency.name,
				}));

				available_currencies.value.sort((a, b) => {
					if (a.value === baseCurrency) return -1;
					if (b.value === baseCurrency) return 1;
					return a.value.localeCompare(b.value);
				});

				if (!selected_currency.value) {
					selected_currency.value = baseCurrency;
				}
				saveCurrencyOptionsCache(profileName, available_currencies.value);
				return available_currencies.value;
			}
			return [];
		} catch (error) {
			console.error("Error fetching currencies:", error);
			const cachedCurrencies = getCachedCurrencyOptions(profileName);
			if (Array.isArray(cachedCurrencies) && cachedCurrencies.length) {
				available_currencies.value = cachedCurrencies;
				if (!selected_currency.value) {
					selected_currency.value = pos_profile.value.currency;
				}
				return available_currencies.value;
			}
			const defaultCurrency = pos_profile.value.currency;
			available_currencies.value = [
				{ value: defaultCurrency, title: defaultCurrency },
			];
			selected_currency.value = defaultCurrency;
			return available_currencies.value;
		}
	};

	const update_currency_and_rate = async () => {
		if (!selected_currency.value || !pos_profile.value) return;

		const rateDate =
			(typeof frappe !== "undefined" && frappe.datetime?.get_today?.()) ||
			exchange_rate_date.value ||
			new Date().toISOString().slice(0, 10);
		const companyCurrency =
			(company.value && company.value.default_currency) ||
			pos_profile.value.currency;
		const plCurrency = price_list_currency.value || companyCurrency;

		const common = {
			profileName: pos_profile.value.name,
			company: pos_profile.value.company,
			effectiveDate: rateDate,
		};
		const companyResult = await resolveExchangeRate({
			...common,
			fromCurrency: selected_currency.value,
			toCurrency: companyCurrency,
		});
		const displayResult = await resolveExchangeRate({
			...common,
			fromCurrency: plCurrency,
			toCurrency: selected_currency.value,
		});

		conversion_rate.value = companyResult.rate || 0;
		exchange_rate.value = displayResult.rate || 0;
		exchange_rate_date.value =
			companyResult.rateDate || displayResult.rateDate || rateDate;

		if (!companyResult.found || !displayResult.found) {
			toastStore.show({
				title: __("No valid exchange rate found"),
				color: "error",
			});
		}
	};

	const reset_currency_to_default = async () => {
		const defaultCurrency =
			pos_profile.value?.currency || company.value?.default_currency || "";
		if (!defaultCurrency) return;

		selected_currency.value = defaultCurrency;
		await update_currency_and_rate();
	};

	const parsePositiveFinite = (value: unknown): number | null => {
		const numeric = parseFinite(value);
		return numeric !== null && numeric > 0 ? numeric : null;
	};

	const resolveStrictPlcToCompanyRate = (
		item: any,
		companyCurrency: string,
		priceListCurrency: string,
		selectedCurrency: string,
		rawConversionRate: number | null,
		rawExchangeRate: number | null,
	): number | null => {
		if (
			!priceListCurrency ||
			!companyCurrency ||
			priceListCurrency === companyCurrency
		) {
			return 1;
		}
		const explicitItemPlc = parsePositiveFinite(item?.plc_conversion_rate);
		if (explicitItemPlc !== null) {
			return explicitItemPlc;
		}
		if (selectedCurrency && priceListCurrency === selectedCurrency) {
			return rawConversionRate;
		}
		if (selectedCurrency && selectedCurrency === companyCurrency) {
			return rawExchangeRate;
		}
		if (rawExchangeRate !== null && rawConversionRate !== null) {
			return rawExchangeRate * rawConversionRate;
		}
		return null;
	};

	const preserveItemCompanyBuyingPrice = (
		item: any,
		companyCurrency: string,
		priceListCurrency: string,
		strictPlcToCompanyRate: number | null,
		selectedCurrency: string,
		itemConversionRate: number,
	): number | null => {
		const storedCompanyBuying =
			parsePositiveFinite(item?._company_buying_price) ??
			parsePositiveFinite(item?.base_buying_rate);
		if (storedCompanyBuying !== null) {
			return storedCompanyBuying;
		}
		const scalarBuying =
			parsePositiveFinite(item?._source_buying_price) ??
			parsePositiveFinite(item?.trade_price) ??
			parsePositiveFinite(item?.buying_rate) ??
			parsePositiveFinite(item?.buying_price);
		if (scalarBuying === null) {
			return null;
		}
		const previousCurrency = String(item?.currency || "").trim();
		const previousConversionRate = parsePositiveFinite(
			item?.conversion_rate,
		);
		const sourceBuyingCurrency = String(
			item?._source_buying_currency || "",
		).trim();
		const buyingRateCurrency = String(
			item?.buying_rate_currency || "",
		).trim();
		const buyingPriceCurrency = String(
			item?.buying_price_currency || "",
		).trim();
		const scalarCurrency =
			sourceBuyingCurrency ||
			buyingRateCurrency ||
			buyingPriceCurrency ||
			previousCurrency;

		if (!scalarCurrency || scalarCurrency === companyCurrency) {
			return scalarBuying;
		}
		if (item._source_buying_price === undefined) {
			item._source_buying_price = scalarBuying;
		}
		if (!item._source_buying_currency) {
			item._source_buying_currency = scalarCurrency;
		}

		const buyingExchangeRate = parsePositiveFinite(
			item?.buying_exchange_rate,
		);
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
			previousConversionRate !== null
		) {
			return scalarBuying * previousConversionRate;
		}
		if (
			priceListCurrency &&
			scalarCurrency === priceListCurrency &&
			strictPlcToCompanyRate !== null &&
			strictPlcToCompanyRate > 0
		) {
			return scalarBuying * strictPlcToCompanyRate;
		}
		if (
			selectedCurrency &&
			scalarCurrency === selectedCurrency &&
			itemConversionRate > 0
		) {
			return scalarBuying * itemConversionRate;
		}
		return null;
	};

	const update_item_rates = async () => {
		console.log(
			"Updating item rates with exchange rate:",
			exchange_rate.value,
		);
		const items = invoiceStore.items;
		const companyCurrency =
			(company.value && company.value.default_currency) ||
			pos_profile.value?.currency ||
			"";
		const selectedCurrency = selected_currency.value || companyCurrency;
		const rawConversionRate = parsePositiveFinite(conversion_rate.value);
		const rawExchangeRate = parsePositiveFinite(exchange_rate.value);
		const itemConversionRate =
			selectedCurrency === companyCurrency
				? 1
				: rawConversionRate !== null
					? rawConversionRate
					: 0;
		const conversionRate = conversion_rate.value || 1;
		const precision = currency_precision.value;
		const resolvedPriceListCurrency =
			price_list_currency.value || companyCurrency;
		const currencyContext = {
			pos_profile: pos_profile.value,
			company: company.value,
			price_list_currency: resolvedPriceListCurrency,
			selected_currency: selectedCurrency,
			exchange_rate: exchange_rate.value || 1,
			conversion_rate: conversionRate,
		};

		items.forEach((item: any) => {
			item._skip_calc = true;
			const strictPlcToCompanyRate = resolveStrictPlcToCompanyRate(
				item,
				companyCurrency,
				resolvedPriceListCurrency,
				selectedCurrency,
				rawConversionRate,
				rawExchangeRate,
			);
			const companyBuyingPrice = preserveItemCompanyBuyingPrice(
				item,
				companyCurrency,
				resolvedPriceListCurrency,
				strictPlcToCompanyRate,
				selectedCurrency,
				itemConversionRate,
			);
			if (companyBuyingPrice !== null) {
				item._company_buying_price = companyBuyingPrice;
				item.base_buying_rate = companyBuyingPrice;
			}
			refreshCanonicalBaseRates(item, currencyContext);

			// Ensure base rates exist
			if (!item.base_rate) {
				if (selectedCurrency === companyCurrency) {
					item.base_rate = item.rate;
					item.base_price_list_rate = item.price_list_rate;
					item.base_discount_amount = item.discount_amount || 0;
				} else {
					item.base_rate = toCompanyCurrency(
						currencyContext,
						item.rate,
					);
					item.base_price_list_rate =
						toCompanyCurrency(
							currencyContext,
							item.price_list_rate,
						);
					item.base_discount_amount =
						toCompanyCurrency(
							currencyContext,
							item.discount_amount || 0,
						);
				}
			}

			if (selectedCurrency === companyCurrency) {
				item.price_list_rate = item.base_price_list_rate;
				item.rate = item.base_rate;
				item.discount_amount = item.base_discount_amount;
			} else {
				const converted_price = flt(
					fromCompanyCurrency(
						currencyContext,
						item.base_price_list_rate,
					),
					precision,
				);
				const converted_rate = flt(
					fromCompanyCurrency(
						currencyContext,
						item.base_rate,
					),
					precision,
				);
				const converted_discount = flt(
					fromCompanyCurrency(
						currencyContext,
						item.base_discount_amount,
					),
					precision,
				);

				item.price_list_rate =
					converted_price < 0.000001 ? 0 : converted_price;
				item.rate = converted_rate < 0.000001 ? 0 : converted_rate;
				item.discount_amount =
					converted_discount < 0.000001 ? 0 : converted_discount;
			}

			item.company_currency = companyCurrency;
			item.currency = selectedCurrency;
			item.conversion_rate = itemConversionRate;
			if (companyBuyingPrice !== null) {
				if (itemConversionRate > 0) {
					const normalizedBuying =
						selectedCurrency === companyCurrency
							? companyBuyingPrice
							: companyBuyingPrice / itemConversionRate;
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

			item.amount = flt(item.qty * item.rate, precision);
			item.base_amount = flt(item.qty * item.base_rate, precision);
		});

		if (typeof invoiceStore.recalculateTotals === "function") {
			invoiceStore.recalculateTotals();
		}
		if (typeof invoiceStore.touch === "function") {
			invoiceStore.touch();
		}
	};

	const roundAmount = (amount: number) => {
		if (pos_profile.value?.disable_rounded_total) {
			return flt(amount, currency_precision.value);
		}
		const baseCurrency =
			price_list_currency.value || pos_profile.value?.currency;
		if (
			pos_profile.value?.posa_allow_multi_currency &&
			selected_currency.value !== baseCurrency
		) {
			return flt(amount, 2);
		}
		return Math.round(amount);
	};

	const fetch_price_lists = async () => {
		if (!pos_profile.value) return [];
		const profileName = pos_profile.value.name;

		if (pos_profile.value.posa_enable_price_list_dropdown) {
			try {
				const r = await frappe.call({
					method: "posawesome.posawesome.api.utilities.get_selling_price_lists",
				});
				if (r && r.message) {
					price_lists.value = r.message.map((pl: any) => pl.name);
				}
			} catch (error) {
				console.error("Failed fetching price lists", error);
				price_lists.value = [pos_profile.value.selling_price_list];
			}
		} else {
			price_lists.value = [pos_profile.value.selling_price_list];
		}

		if (!selected_price_list.value) {
			selected_price_list.value = pos_profile.value.selling_price_list;
		}

		try {
			const r = await frappe.call({
				method: "posawesome.posawesome.api.invoices.get_price_list_currency",
				args: { price_list: selected_price_list.value },
			});
			if (r && r.message) {
				price_list_currency.value = r.message;
			}
		} catch (error) {
			console.error("Failed fetching price list currency", error);
			const cachedMeta = getCachedPriceListMeta(profileName);
			if (cachedMeta?.price_list_currency) {
				price_list_currency.value = cachedMeta.price_list_currency;
			}
		}

		savePriceListMetaCache(profileName, {
			price_lists: price_lists.value,
			selected_price_list: selected_price_list.value,
			price_list_currency: price_list_currency.value,
		});

		return price_lists.value;
	};

	return {
		available_currencies,
		selected_currency,
		exchange_rate,
		conversion_rate,
		exchange_rate_date,
		price_lists,
		selected_price_list,
		price_list_currency,
		float_precision,
		currency_precision,
		flt,
		fetch_available_currencies,
		update_currency_and_rate,
		reset_currency_to_default,
		update_currency: async (val: string) => {
			if (val) selected_currency.value = val;
			await update_currency_and_rate();
		},
		update_item_rates,
		roundAmount,
		fetch_price_lists,
	};
}
