export type LossCostFloor = {
	field: string;
	label: string;
	rate: number;
	baseCurrency?: boolean;
};

export type LossRisk = {
	belowCost: boolean;
	itemCode: string;
	itemName: string;
	sellingRate: number;
	costRate: number;
	costLabel: string;
	costField: string;
};

export type SaleFloorPolicy = {
	enabled: boolean;
	action: "Block" | "POS Supervisor Override" | "Warning Only";
	minimumMarginPercentage: number;
	missingCostAction: "Allow" | "Block";
};

export type LossPreventionOptions = {
	minimumMarginPercentage?: number;
	invoiceDiscountPercentage?: number;
	applyDiscountOn?: string;
	isCashOrNonTradeDiscount?: boolean | number;
	taxes?: Array<Record<string, unknown>>;
	currencyPrecision?: number;
	reconciledNativeNet?: boolean;
};

const COST_CANDIDATES: Array<{
	field: string;
	label: string;
	baseCurrency?: boolean;
	stockUomRate?: boolean;
	buyingListField?: boolean;
}> = [
	{ field: "trade_price", label: "Trade Price", buyingListField: true },
	{ field: "buying_price", label: "Buying Price", buyingListField: true },
	{ field: "buying_rate", label: "Buying Rate", buyingListField: true },
	{ field: "last_buying_rate", label: "Last Buying Rate" },
	{
		field: "valuation_rate",
		label: "Warehouse Valuation",
		baseCurrency: true,
		stockUomRate: true,
	},
	{
		field: "last_purchase_rate",
		label: "Last Purchase Rate",
		baseCurrency: true,
		stockUomRate: true,
	},
	{
		field: "manufacturing_cost",
		label: "Manufacturing Cost",
		baseCurrency: true,
		stockUomRate: true,
	},
];

const EPSILON = 0.0001;

export function resolveSaleFloorPolicy(
	profile: Record<string, unknown> | null | undefined,
): SaleFloorPolicy {
	const enabledValue = profile?.posa_enable_below_cost_guard;
	const enabled =
		enabledValue === null || enabledValue === undefined || enabledValue === ""
			? true
			: [true, 1, "1", "true", "Yes"].includes(enabledValue as any);
	const rawAction = String(profile?.posa_below_cost_action || "Block");
	const action = ["Block", "POS Supervisor Override", "Warning Only"].includes(
		rawAction,
	)
		? (rawAction as SaleFloorPolicy["action"])
		: "Block";
	const rawMissingAction = String(
		profile?.posa_missing_cost_action || "Allow",
	);
	return {
		enabled,
		action,
		minimumMarginPercentage: Math.max(
			toFiniteNumber(profile?.posa_minimum_margin_percentage) || 0,
			0,
		),
		missingCostAction:
			rawMissingAction === "Block" ? "Block" : "Allow",
	};
}

export function toFiniteNumber(value: unknown): number | null {
	if (value === null || value === undefined || value === "") {
		return null;
	}
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

function resolveInvoiceToCompanyRate(
	item: Record<string, unknown>,
	buyingTableCurrency = "",
): number | null {
	const invoiceCurrency = String(
		item.currency || item.price_list_currency || "",
	).trim();
	const companyCurrency = String(item.company_currency || "").trim();
	const buyingPriceCurrency = String(
		item.buying_price_currency || buyingTableCurrency || "",
	).trim();

	if (
		invoiceCurrency &&
		companyCurrency &&
		invoiceCurrency === companyCurrency
	) {
		return 1;
	}

	const rawCurrencyRate = toFiniteNumber(item.conversion_rate);
	if (rawCurrencyRate !== null && rawCurrencyRate > 0) {
		return rawCurrencyRate;
	}

	const isCrossCurrency = Boolean(
		(invoiceCurrency &&
			companyCurrency &&
			invoiceCurrency !== companyCurrency) ||
			(invoiceCurrency &&
				!companyCurrency &&
				buyingPriceCurrency &&
				buyingPriceCurrency !== invoiceCurrency),
	);
	if (isCrossCurrency) {
		return null;
	}

	return 1;
}

function parseItemTaxRateMap(
	item: Record<string, unknown> | null | undefined,
): Record<string, number> {
	const raw = item?.item_tax_rate;
	if (!raw) {
		return {};
	}
	if (typeof raw === "object" && !Array.isArray(raw)) {
		const out: Record<string, number> = {};
		for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
			// ERPNext taxes_and_totals.py:476-480, 510-517 treats "N/A" (NOT_APPLICABLE_TAX) as 0%.
			if (
				v === null ||
				(typeof v === "string" && v.trim().toUpperCase() === "N/A")
			) {
				out[k] = 0;
				continue;
			}
			const parsed = toFiniteNumber(v);
			if (parsed !== null) {
				out[k] = parsed;
			}
		}
		return out;
	}
	if (typeof raw === "string") {
		try {
			const parsed = JSON.parse(raw);
			if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
				return parseItemTaxRateMap({ item_tax_rate: parsed });
			}
		} catch {
			return {};
		}
	}
	return {};
}

export function computeRowTaxParams(
	item: Record<string, unknown> | null | undefined,
	taxes: Array<Record<string, unknown>> | null | undefined,
): { inclA: number; inclB: number; allA: number; allB: number } {
	if (!Array.isArray(taxes) || !taxes.length) {
		return { inclA: 0, inclB: 0, allA: 0, allB: 0 };
	}
	const itemTaxMap = parseItemTaxRateMap(item);
	const inclTerms: Array<[number, number]> = [];
	const allTerms: Array<[number, number]> = [];
	let inclA = 0;
	let inclB = 0;
	let allA = 0;
	let allB = 0;

	const computeTerm = (
		chargeType: string,
		taxRate: number,
		rowIdRaw: unknown,
		terms: Array<[number, number]>,
	): [number, number] => {
		if (chargeType === "On Net Total") {
			return [taxRate / 100, 0];
		}
		if (chargeType === "On Item Quantity") {
			return [0, taxRate];
		}
		if (chargeType === "On Previous Row Amount") {
			const refIdx = Math.trunc(Number(rowIdRaw || 0)) - 1;
			if (refIdx >= 0 && refIdx < terms.length) {
				const [prevA, prevB] = terms[refIdx]!;
				return [(taxRate / 100) * prevA, (taxRate / 100) * prevB];
			}
			return [0, 0];
		}
		if (chargeType === "On Previous Row Total") {
			const refIdx = Math.trunc(Number(rowIdRaw || 0)) - 1;
			if (refIdx >= 0 && refIdx < terms.length) {
				let sumA = 0;
				let sumB = 0;
				for (let idx = 0; idx <= refIdx; idx++) {
					sumA += terms[idx]![0];
					sumB += terms[idx]![1];
				}
				return [(taxRate / 100) * (1 + sumA), (taxRate / 100) * sumB];
			}
			return [taxRate / 100, 0];
		}
		return [0, 0];
	};

	for (const tax of taxes) {
		if (!tax || typeof tax !== "object") {
			inclTerms.push([0, 0]);
			allTerms.push([0, 0]);
			continue;
		}
		const chargeType = String(tax.charge_type || "").trim();
		const accountHead = String(tax.account_head || "");
		const taxRate =
			accountHead && Object.prototype.hasOwnProperty.call(itemTaxMap, accountHead)
				? itemTaxMap[accountHead]!
				: toFiniteNumber(tax.rate) || 0;
		const isInclusive =
			tax.included_in_print_rate === true ||
			tax.included_in_print_rate === 1 ||
			tax.included_in_print_rate === "1";

		const [curAllA, curAllB] = computeTerm(
			chargeType,
			taxRate,
			tax.row_id,
			allTerms,
		);
		allTerms.push([curAllA, curAllB]);
		allA += curAllA;
		allB += curAllB;

		const [curInclA, curInclB] = isInclusive
			? computeTerm(chargeType, taxRate, tax.row_id, inclTerms)
			: [0, 0];
		inclTerms.push([curInclA, curInclB]);
		inclA += curInclA;
		inclB += curInclB;
	}

	return {
		inclA: Math.max(inclA, 0),
		inclB: Math.max(inclB, 0),
		allA: Math.max(allA, 0),
		allB: Math.max(allB, 0),
	};
}

export function computeInvoiceActualTaxTotal(
	taxes: Array<Record<string, unknown>> | null | undefined,
): number {
	if (!Array.isArray(taxes) || !taxes.length) {
		return 0;
	}
	let totalActualTax = 0;
	const actualTaxes = new Map<number, [number, number]>();

	const record = (rowIdx: number, tax: Record<string, unknown>, rawAmount: number) => {
		let amount = rawAmount;
		if (String(tax.add_deduct_tax || "").trim() === "Deduct") {
			amount *= -1;
		}
		if (String(tax.category || "").trim() !== "Valuation") {
			totalActualTax += amount;
		}
		actualTaxes.set(rowIdx, [amount, totalActualTax]);
	};

	for (let pos = 0; pos < taxes.length; pos++) {
		const tax = taxes[pos];
		if (!tax || typeof tax !== "object") {
			continue;
		}
		const rowIdx = Math.trunc(Number(tax.idx || 0)) || pos + 1;
		const chargeType = String(tax.charge_type || "").trim();
		if (chargeType === "Actual") {
			record(rowIdx, tax, toFiniteNumber(tax.tax_amount) || 0);
			continue;
		}
		if (
			chargeType !== "On Previous Row Amount" &&
			chargeType !== "On Previous Row Total"
		) {
			continue;
		}
		const refIdx = Math.trunc(Number(tax.row_id || 0));
		const baseEntry = actualTaxes.get(refIdx);
		if (!baseEntry) {
			continue;
		}
		const baseAmount =
			chargeType === "On Previous Row Amount"
				? baseEntry[0]
				: baseEntry[1];
		record(rowIdx, tax, (baseAmount * (toFiniteNumber(tax.rate) || 0)) / 100);
	}
	return totalActualTax;
}

export function resolveInvoiceDiscountPercentage(
	doc: Record<string, unknown> | null | undefined,
	items?: Array<Record<string, unknown>> | null,
): number {
	if (!doc) {
		return 0;
	}
	const applyDiscountOn = String(
		doc.apply_discount_on || "Grand Total",
	).trim();
	const isCashDiscount =
		Boolean(doc.is_cash_or_non_trade_discount) &&
		applyDiscountOn === "Grand Total";
	if (isCashDiscount) {
		return 0;
	}

	const explicitPct = Math.max(
		toFiniteNumber(doc.additional_discount_percentage) || 0,
		0,
	);
	const discountAmount = Math.max(toFiniteNumber(doc.discount_amount) || 0, 0);
	if (explicitPct <= 0 && discountAmount <= 0) {
		return 0;
	}

	const itemList = Array.isArray(items)
		? items
		: Array.isArray(doc.items)
			? (doc.items as Array<Record<string, unknown>>)
			: [];
	const taxes = Array.isArray(doc.taxes)
		? (doc.taxes as Array<Record<string, unknown>>)
		: [];

	let eligibleNetTotal = 0;
	let eligibleScalableGrandTotal = 0;
	let eligibleFixedQtyTaxTotal = 0;

	for (const item of itemList) {
		if (
			!item ||
			item.is_return ||
			item.posa_is_replace ||
			isPricingRuleFreeLine(item)
		) {
			continue;
		}
		const qty = toFiniteNumber(item.qty) || 0;
		if (qty <= 0) {
			continue;
		}
		const grossRate = getEffectiveSellingRate(item);
		const { inclA, inclB, allA, allB } = computeRowTaxParams(item, taxes);
		const preDiscountNet = Math.max((grossRate - inclB) / (1 + inclA), 0);
		eligibleNetTotal += preDiscountNet * qty;
		eligibleScalableGrandTotal += preDiscountNet * (1 + allA) * qty;
		eligibleFixedQtyTaxTotal += allB * qty;
	}

	if (applyDiscountOn === "Net Total") {
		if (explicitPct > 0) {
			return explicitPct;
		}
		return eligibleNetTotal > 0 ? (discountAmount / eligibleNetTotal) * 100 : 0;
	}

	const actualTaxTotal = computeInvoiceActualTaxTotal(taxes);
	const hasStructuredTaxRows = taxes.some((tax) =>
		Boolean(
			tax && typeof tax === "object" && String(tax.charge_type || "").trim(),
		),
	);
	if (
		!hasStructuredTaxRows &&
		Math.abs(eligibleScalableGrandTotal - eligibleNetTotal) <= EPSILON &&
		eligibleFixedQtyTaxTotal === 0 &&
		actualTaxTotal === 0
	) {
		const docGrandTotal = toFiniteNumber(doc.grand_total) || 0;
		const docTaxesTotal = toFiniteNumber(doc.total_taxes_and_charges) || 0;
		if (docGrandTotal > 0 && discountAmount > 0) {
			const preDiscountGrandTotal = docGrandTotal + discountAmount;
			if (preDiscountGrandTotal > eligibleScalableGrandTotal + EPSILON) {
				eligibleScalableGrandTotal = preDiscountGrandTotal;
			}
		} else if (docTaxesTotal > 0) {
			eligibleScalableGrandTotal += docTaxesTotal;
		}
	}

	const eligibleGrandTotal =
		eligibleScalableGrandTotal + eligibleFixedQtyTaxTotal + actualTaxTotal;
	if (eligibleScalableGrandTotal <= 0) {
		return 0;
	}
	if (explicitPct > 0) {
		const effectiveDiscount = (explicitPct / 100) * eligibleGrandTotal;
		return Math.min((effectiveDiscount / eligibleScalableGrandTotal) * 100, 100);
	}
	return Math.min((discountAmount / eligibleScalableGrandTotal) * 100, 100);
}

export function getItemCostFloor(
	item: Record<string, unknown> | null | undefined,
): LossCostFloor | null {
	if (!item) {
		return null;
	}
	const rawUomFactor = toFiniteNumber(item.conversion_factor);
	const uomConversionFactor =
		rawUomFactor !== null && rawUomFactor > 0 ? rawUomFactor : 1;

	const resolvedCandidates: LossCostFloor[] = [];
	let hasNormalizedBuyingPrice = false;
	let hasUnresolvedCrossCurrencyCost = false;
	let selectedTableCurrency = "";

	const invoiceCurrency = String(
		item.currency || item.price_list_currency || "",
	).trim();
	const buyingPriceCurrency = String(item.buying_price_currency || "").trim();
	const buyingRateCurrency = String(item.buying_rate_currency || "").trim();
	const companyCurrency = String(item.company_currency || "").trim();
	const rawBuyingExchangeRate = toFiniteNumber(item.buying_exchange_rate);

	const pricesByUom = item._buying_prices_by_uom;
	let selectedTableRate: number | null = null;
	let selectedTableInvoiceRate: number | null = null;
	if (pricesByUom && typeof pricesByUom === "object") {
		const prices = pricesByUom as Record<
			string,
			Record<string, unknown> | undefined
		>;
		const selectedUom = String(item.uom || item.stock_uom || "");
		const stockUom = String(item.stock_uom || "");
		const exact = selectedUom ? prices[selectedUom] : undefined;
		const priceKeys = Object.keys(prices);
		const fallback =
			(stockUom ? prices[stockUom] : undefined) ||
			prices[""] ||
			(!selectedUom && !stockUom && priceKeys.length === 1
				? prices[priceKeys[0]!]
				: undefined);
		const selected = exact || fallback;
		selectedTableCurrency = String(selected?.currency || "").trim();
		const baseRate =
			toFiniteNumber(selected?.base_price_list_rate) ??
			toFiniteNumber(selected?.base_rate);
		const rawTableRate = toFiniteNumber(selected?.price_list_rate);
		const conversionFactor = exact ? 1 : uomConversionFactor;
		if (baseRate !== null && baseRate > 0) {
			selectedTableRate = baseRate * conversionFactor;
			hasNormalizedBuyingPrice = true;
		} else if (rawTableRate !== null && rawTableRate > 0) {
			const sameAsCompany = Boolean(
				selectedTableCurrency &&
					companyCurrency &&
					selectedTableCurrency === companyCurrency,
			);
			const sameAsInvoice = Boolean(
				selectedTableCurrency &&
					invoiceCurrency &&
					selectedTableCurrency === invoiceCurrency,
			);
			if (sameAsCompany) {
				selectedTableRate = rawTableRate * conversionFactor;
				hasNormalizedBuyingPrice = true;
			} else if (sameAsInvoice) {
				selectedTableInvoiceRate = rawTableRate * conversionFactor;
				hasNormalizedBuyingPrice = true;
			} else if (
				buyingPriceCurrency &&
				selectedTableCurrency === buyingPriceCurrency &&
				rawBuyingExchangeRate !== null &&
				rawBuyingExchangeRate > 0
			) {
				selectedTableRate =
					rawTableRate * conversionFactor * rawBuyingExchangeRate;
				hasNormalizedBuyingPrice = true;
			} else if (selectedTableCurrency) {
				hasUnresolvedCrossCurrencyCost = true;
			}
		}
	}

	const invoiceToCompanyRate = resolveInvoiceToCompanyRate(
		item,
		selectedTableCurrency,
	);
	if (selectedTableRate !== null) {
		if (invoiceToCompanyRate !== null) {
			resolvedCandidates.push({
				field: "_buying_prices_by_uom",
				label: "Trade Price",
				rate: selectedTableRate,
				baseCurrency: true,
			});
		} else {
			hasUnresolvedCrossCurrencyCost = true;
		}
	} else if (selectedTableInvoiceRate !== null) {
		resolvedCandidates.push({
			field: "_buying_prices_by_uom",
			label: "Trade Price",
			rate: selectedTableInvoiceRate,
			baseCurrency: false,
		});
	}

	if (!hasNormalizedBuyingPrice) {
		const storedCompanyBuying = toFiniteNumber(item._company_buying_price);
		const baseBuyingRate = toFiniteNumber(item.base_buying_rate);
		const companyBuyingPrice =
			storedCompanyBuying !== null && storedCompanyBuying > 0
				? storedCompanyBuying
				: baseBuyingRate !== null && baseBuyingRate > 0
					? baseBuyingRate
					: null;
		if (companyBuyingPrice !== null) {
			if (invoiceToCompanyRate !== null) {
				resolvedCandidates.push({
					field:
						storedCompanyBuying !== null && storedCompanyBuying > 0
							? "_company_buying_price"
							: "base_buying_rate",
					label: "Trade Price",
					rate: companyBuyingPrice,
					baseCurrency: true,
				});
			} else {
				hasUnresolvedCrossCurrencyCost = true;
			}
			hasNormalizedBuyingPrice = true;
		}
	}

	for (const candidate of COST_CANDIDATES) {
		const rawRate = toFiniteNumber(item[candidate.field]);
		if (rawRate === null || rawRate <= 0) {
			continue;
		}
		if (candidate.baseCurrency && invoiceToCompanyRate === null) {
			hasUnresolvedCrossCurrencyCost = true;
			continue;
		}
		if (candidate.buyingListField) {
			if (hasNormalizedBuyingPrice) {
				continue;
			}
			const scalarCurrency = buyingRateCurrency || buyingPriceCurrency;
			const currencyMismatch = Boolean(
				scalarCurrency &&
					invoiceCurrency &&
					scalarCurrency !== invoiceCurrency,
			);
			if (currencyMismatch) {
				if (invoiceToCompanyRate === null) {
					hasUnresolvedCrossCurrencyCost = true;
					continue;
				}
				if (
					companyCurrency &&
					scalarCurrency === companyCurrency
				) {
					resolvedCandidates.push({
						field: candidate.field,
						label: candidate.label,
						rate: rawRate,
						baseCurrency: true,
					});
				} else if (
					buyingPriceCurrency &&
					scalarCurrency === buyingPriceCurrency &&
					rawBuyingExchangeRate !== null &&
					rawBuyingExchangeRate > 0
				) {
					resolvedCandidates.push({
						field: candidate.field,
						label: candidate.label,
						rate: rawRate * rawBuyingExchangeRate,
						baseCurrency: true,
					});
				} else {
					hasUnresolvedCrossCurrencyCost = true;
				}
				continue;
			}
		}
		const scaledRate = candidate.stockUomRate
			? rawRate * uomConversionFactor
			: rawRate;
		resolvedCandidates.push({
			field: candidate.field,
			label: candidate.label,
			rate: scaledRate,
			baseCurrency: candidate.baseCurrency,
		});
	}

	if (hasUnresolvedCrossCurrencyCost || !resolvedCandidates.length) {
		return null;
	}

	const comparisonRate = invoiceToCompanyRate ?? 1;
	let highest = resolvedCandidates[0]!;
	let highestBaseRate = highest.baseCurrency
		? highest.rate
		: highest.rate * comparisonRate;

	for (const candidate of resolvedCandidates.slice(1)) {
		const candidateBaseRate = candidate.baseCurrency
			? candidate.rate
			: candidate.rate * comparisonRate;
		if (candidateBaseRate > highestBaseRate + EPSILON) {
			highest = candidate;
			highestBaseRate = candidateBaseRate;
		}
	}

	return highest;
}

export function getEffectiveSellingRate(
	item: Record<string, unknown> | null | undefined,
): number {
	if (!item) {
		return 0;
	}
	const rate = toFiniteNumber(item.rate);
	if (rate !== null) {
		return Math.abs(rate);
	}
	const amount = toFiniteNumber(item.amount);
	const qty = toFiniteNumber(item.qty);
	if (amount !== null && qty !== null && Math.abs(qty) > EPSILON) {
		return Math.abs(amount / qty);
	}
	const priceListRate = toFiniteNumber(item.price_list_rate) || 0;
	const discountAmount = Math.abs(toFiniteNumber(item.discount_amount) || 0);
	return Math.max(priceListRate - discountAmount, 0);
}

const hasMarker = (value: unknown) =>
	Array.isArray(value) ? value.length > 0 : Boolean(value);

/**
 * Mirrors the server's `_strip_client_freebies_from_payload`: pricing-rule free
 * lines are dropped on submit and re-derived by ERPNext, so they carry no floor.
 * Other free rows (POS Offer give-aways, rows without a rule) are floor-checked.
 */
export function isPricingRuleFreeLine(
	item: Record<string, unknown> | null | undefined,
): boolean {
	if (!item) {
		return false;
	}
	if (hasMarker(item.auto_free_source)) {
		return true;
	}
	const isFree =
		item.is_free_item === true ||
		item.is_free_item === 1 ||
		item.is_free_item === "1";
	return (
		isFree &&
		(hasMarker(item.source_rule) ||
			hasMarker(item.pricing_rule) ||
			hasMarker(item.pricing_rules))
	);
}

function resolveNativeNetAmount(
	item: Record<string, unknown>,
	grossSellingRate: number,
	qty: number,
): number | null {
	const rawNetAmount = toFiniteNumber(item.net_amount);
	if (
		rawNetAmount !== null &&
		rawNetAmount >= 0 &&
		(rawNetAmount > 0 || grossSellingRate <= 0)
	) {
		return Math.abs(rawNetAmount);
	}
	const rawNetRate = toFiniteNumber(item.net_rate);
	if (
		rawNetRate !== null &&
		rawNetRate >= 0 &&
		(rawNetRate > 0 || grossSellingRate <= 0)
	) {
		return Math.abs(rawNetRate) * qty;
	}
	return null;
}

function resolveNativeUnitNetRate(
	item: Record<string, unknown>,
	grossSellingRate: number,
	qty: number | null,
	precision = 2,
): number | null {
	const rawNetAmount = toFiniteNumber(item.net_amount);
	const netFromAmount =
		rawNetAmount !== null &&
		rawNetAmount >= 0 &&
		(rawNetAmount > 0 || grossSellingRate <= 0) &&
		qty !== null &&
		qty > 0
			? Math.abs(rawNetAmount) / qty
			: null;
	const rawNetRate = toFiniteNumber(item.net_rate);
	const halfUlp = 0.5 * 10 ** -precision;
	const hasValidNetRate =
		rawNetRate !== null &&
		rawNetRate >= 0 &&
		(rawNetRate > 0 ||
			grossSellingRate <= 0 ||
			(netFromAmount !== null &&
				Math.abs(Math.abs(rawNetRate) - netFromAmount) <= halfUlp + 1e-9));
	if (hasValidNetRate) {
		const normalizedNetRate = Math.abs(rawNetRate!);
		// ERPNext taxes_and_totals.py:1118-1121 rounds net_rate = flt(net_amount / qty, prec).
		// Honor native rounded net_rate (including 0.00) when consistent with net_amount / qty within 0.5 ULP.
		if (
			netFromAmount === null ||
			Math.abs(normalizedNetRate - netFromAmount) <= halfUlp + 1e-9
		) {
			return normalizedNetRate;
		}
		return netFromAmount;
	}
	return netFromAmount;
}

function areNativeNetAmountsReconciled(
	items: Array<Record<string, unknown>>,
	options: LossPreventionOptions,
): boolean {
	const applyDiscountOn = String(
		options.applyDiscountOn || "Grand Total",
	).trim();
	const isCashDiscount =
		Boolean(options.isCashOrNonTradeDiscount) &&
		applyDiscountOn === "Grand Total";
	const invoiceDiscountPercentage = isCashDiscount
		? 0
		: Math.min(
				Math.max(toFiniteNumber(options.invoiceDiscountPercentage) || 0, 0),
				100,
			);
	const rawPrecision = toFiniteNumber(options.currencyPrecision);
	const precision =
		rawPrecision !== null && rawPrecision >= 0 ? Math.trunc(rawPrecision) : 2;
	const ulp = 10 ** -precision;

	let eligibleCount = 0;
	let nativeSum = 0;
	let expectedSum = 0;
	let preDiscountSum = 0;

	for (const item of items) {
		if (
			!item ||
			item.is_return ||
			item.posa_is_replace ||
			isPricingRuleFreeLine(item)
		) {
			continue;
		}
		const qty = toFiniteNumber(item.qty) || 0;
		if (qty <= 0) {
			continue;
		}
		eligibleCount += 1;
		const grossRate = getEffectiveSellingRate(item);
		const nativeAmount = resolveNativeNetAmount(item, grossRate, qty);
		if (nativeAmount === null) {
			return false;
		}
		const { inclA, inclB } = computeRowTaxParams(item, options.taxes);
		const preDiscountNet = Math.max((grossRate - inclB) / (1 + inclA), 0);
		const preAmount = preDiscountNet * qty;
		const computedAmount =
			preDiscountNet * (1 - invoiceDiscountPercentage / 100) * qty;
		if (
			nativeAmount > preAmount + 1e-9 ||
			nativeAmount > computedAmount + ulp + 1e-9
		) {
			return false;
		}
		nativeSum += nativeAmount;
		expectedSum += computedAmount;
		preDiscountSum += preAmount;
	}

	if (eligibleCount === 0) {
		return false;
	}
	const hasUnappliedDiscount =
		expectedSum < preDiscountSum - 0.5 * ulp - 1e-9 &&
		nativeSum >= preDiscountSum - 1e-9;
	return !hasUnappliedDiscount && Math.abs(nativeSum - expectedSum) <= ulp + 1e-9;
}

export function getItemLossRisk(
	item: Record<string, unknown> | null | undefined,
	options: LossPreventionOptions = {},
): LossRisk | null {
	if (
		!item ||
		item.is_return ||
		item.posa_is_replace ||
		isPricingRuleFreeLine(item)
	) {
		return null;
	}
	const qty = toFiniteNumber(item.qty);
	if (qty !== null && qty < 0) {
		return null;
	}
	const cost = getItemCostFloor(item);
	if (!cost) {
		return null;
	}
	const grossSellingRate = getEffectiveSellingRate(item);
	const { inclA, inclB } = computeRowTaxParams(item, options.taxes);
	const preDiscountNetRate = Math.max(
		(grossSellingRate - inclB) / (1 + inclA),
		0,
	);
	const applyDiscountOn = String(
		options.applyDiscountOn || "Grand Total",
	).trim();
	const isCashDiscount =
		Boolean(options.isCashOrNonTradeDiscount) &&
		applyDiscountOn === "Grand Total";
	const invoiceDiscountPercentage = isCashDiscount
		? 0
		: Math.min(
				Math.max(toFiniteNumber(options.invoiceDiscountPercentage) || 0, 0),
				100,
			);
	const rawPrecision = toFiniteNumber(options.currencyPrecision);
	const precision =
		rawPrecision !== null && rawPrecision >= 0 ? Math.trunc(rawPrecision) : 2;
	const nativeUnitNetRate = resolveNativeUnitNetRate(
		item,
		grossSellingRate,
		qty,
		precision,
	);
	let effectiveSellingRate: number;
	if (options.reconciledNativeNet && nativeUnitNetRate !== null) {
		effectiveSellingRate = Math.min(nativeUnitNetRate, preDiscountNetRate);
	} else if (
		nativeUnitNetRate !== null &&
		(nativeUnitNetRate < preDiscountNetRate - EPSILON ||
			isCashDiscount ||
			invoiceDiscountPercentage <= 0)
	) {
		effectiveSellingRate = Math.min(nativeUnitNetRate, preDiscountNetRate);
	} else {
		effectiveSellingRate =
			preDiscountNetRate * (1 - invoiceDiscountPercentage / 100);
	}
	const minimumMarginPercentage = Math.max(
		toFiniteNumber(options.minimumMarginPercentage) || 0,
		0,
	);
	const minimumCostRate =
		cost.rate * (1 + minimumMarginPercentage / 100);
	let currencyConversionRate = 1;
	if (cost.baseCurrency) {
		const resolvedRate = resolveInvoiceToCompanyRate(item);
		if (resolvedRate === null) {
			return null;
		}
		currencyConversionRate = resolvedRate;
	}
	const comparisonSellingRate = cost.baseCurrency
		? effectiveSellingRate * currencyConversionRate
		: effectiveSellingRate;
	if (comparisonSellingRate + EPSILON >= minimumCostRate) {
		return null;
	}
	const rawDisplayCostRate = cost.baseCurrency
		? minimumCostRate / currencyConversionRate
		: minimumCostRate;
	const displayCostRate = Number.isFinite(rawDisplayCostRate)
		? Number(rawDisplayCostRate.toPrecision(12))
		: rawDisplayCostRate;
	return {
		belowCost: true,
		itemCode: String(item.item_code || ""),
		itemName: String(item.item_name || item.item_code || ""),
		sellingRate: effectiveSellingRate,
		costRate: displayCostRate,
		costLabel:
			minimumMarginPercentage > 0 ? "Minimum Rate" : cost.label,
		costField: cost.field,
	};
}

export function findLossRiskItems(
	items: Array<Record<string, unknown>> | null | undefined,
	options: LossPreventionOptions = {},
): LossRisk[] {
	if (!Array.isArray(items)) {
		return [];
	}
	const reconciledNativeNet =
		options.reconciledNativeNet ??
		areNativeNetAmountsReconciled(items, options);
	const effectiveOptions =
		reconciledNativeNet === options.reconciledNativeNet
			? options
			: { ...options, reconciledNativeNet };
	return items
		.map((item) => getItemLossRisk(item, effectiveOptions))
		.filter((risk): risk is LossRisk => Boolean(risk?.belowCost));
}
