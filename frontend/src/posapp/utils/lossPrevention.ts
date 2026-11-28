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
	roundRowWiseTax?: boolean;
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

/**
 * Mirror Frappe `frappe/utils/data.py:1242-1338` and `number_format.js:208-265`.
 * Defaults to Frappe's `"Banker's Rounding (legacy)"` (half-to-even at precision 0,
 * decimal-stabilized half-up at precision > 0) and honors `frappe.boot.sysdefaults.rounding_method`
 * when configured to `"Banker's Rounding"` or `"Commercial Rounding"`.
 */
function roundToPrecision(value: number, precision: number): number {
	if (!Number.isFinite(value)) {
		return value;
	}
	if (value === 0) {
		return 0;
	}
	const d = Math.max(0, Math.trunc(precision));
	const isNegative = value < 0;
	const absValue = Math.abs(value);
	const multiplier = 10 ** d;
	const roundingMethod =
		(globalThis as any)?.frappe?.boot?.sysdefaults?.rounding_method ||
		"Banker's Rounding (legacy)";

	if (roundingMethod === "Banker's Rounding") {
		const scaled = Number((d ? absValue * multiplier : absValue).toFixed(12));
		if (scaled === 0) {
			return 0;
		}
		const floorNum = Math.floor(scaled);
		const decimalPart = scaled - floorNum;
		const epsilon = 2 ** (Math.log2(Math.abs(scaled)) - 52);
		const isTie =
			epsilon < 0.5
				? Math.abs(decimalPart - 0.5) < epsilon
				: decimalPart === 0.5;
		const roundedInt = isTie
			? floorNum % 2 === 0
				? floorNum
				: floorNum + 1
			: Math.round(scaled);
		const result = d ? roundedInt / multiplier : roundedInt;
		return isNegative ? -result : result;
	}

	if (roundingMethod === "Commercial Rounding") {
		const scaled = d ? absValue * multiplier : absValue;
		let epsilon = 2 ** (Math.log2(Math.abs(scaled)) - 52);
		if (epsilon >= 0.25) {
			epsilon = 0;
		}
		const roundedInt = Math.round(scaled + epsilon);
		const result = d ? roundedInt / multiplier : roundedInt;
		return isNegative ? -result : result;
	}

	// Default: Frappe "Banker's Rounding (legacy)"
	const scaled = Number((d ? absValue * multiplier : absValue).toFixed(8));
	const floorNum = Math.floor(scaled);
	const decimalPart = scaled - floorNum;
	const roundedInt =
		d === 0 && decimalPart === 0.5
			? floorNum % 2 === 0
				? floorNum
				: floorNum + 1
			: Math.round(scaled);
	const result = d ? roundedInt / multiplier : roundedInt;
	return isNegative ? -result : result;
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
		if (!item || item.is_return || item.posa_is_replace) {
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
	const sourceBuyingCurrency = String(
		item._source_buying_currency || "",
	).trim();
	const companyCurrency = String(item.company_currency || "").trim();
	const effectiveInvoiceCurrency = invoiceCurrency || companyCurrency;
	const rawBuyingExchangeRate = toFiniteNumber(item.buying_exchange_rate);
	const hasExplicitInvalidBuyingFx = Boolean(
		Object.prototype.hasOwnProperty.call(item, "buying_exchange_rate") &&
			item.buying_exchange_rate !== undefined &&
			(rawBuyingExchangeRate === null || rawBuyingExchangeRate <= 0),
	);
	const hasExplicitNullBaseBuying = Boolean(
		Object.prototype.hasOwnProperty.call(item, "base_buying_rate") &&
			item.base_buying_rate === null,
	);

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
		const hasExplicitNullBaseRate = Boolean(
			selected &&
				Object.prototype.hasOwnProperty.call(
					selected,
					"base_price_list_rate",
				) &&
				selected.base_price_list_rate === null,
		);
		const baseRate =
			toFiniteNumber(selected?.base_price_list_rate) ??
			toFiniteNumber(selected?.base_rate);
		const rawTableRate = toFiniteNumber(selected?.price_list_rate);
		const priceNotUomDependent =
			selected?.price_not_uom_dependent === true ||
			selected?.price_not_uom_dependent === 1 ||
			selected?.price_not_uom_dependent === "1";
		const conversionFactor =
			exact || priceNotUomDependent ? 1 : uomConversionFactor;
		if (selected?.unresolved_fx === true) {
			hasUnresolvedCrossCurrencyCost = true;
			hasNormalizedBuyingPrice = true;
		} else if (baseRate !== null && baseRate > 0) {
			selectedTableRate = baseRate * conversionFactor;
			hasNormalizedBuyingPrice = true;
		} else if (rawTableRate !== null && rawTableRate > 0) {
			const sameAsCompany = Boolean(
				selectedTableCurrency &&
					companyCurrency &&
					selectedTableCurrency === companyCurrency,
			);
			const isForeignTableEntry = Boolean(
				selectedTableCurrency &&
					companyCurrency &&
					selectedTableCurrency !== companyCurrency,
			);
			const sameAsInvoice = Boolean(
				selectedTableCurrency &&
					invoiceCurrency &&
					selectedTableCurrency === invoiceCurrency,
			);
			if (sameAsCompany) {
				selectedTableRate = rawTableRate * conversionFactor;
				hasNormalizedBuyingPrice = true;
			} else if (isForeignTableEntry && hasExplicitInvalidBuyingFx) {
				hasUnresolvedCrossCurrencyCost = true;
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
			} else if (isForeignTableEntry && hasExplicitNullBaseRate) {
				hasUnresolvedCrossCurrencyCost = true;
				hasNormalizedBuyingPrice = true;
			} else if (sameAsInvoice) {
				selectedTableInvoiceRate = rawTableRate * conversionFactor;
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
			const scalarCurrency =
				buyingRateCurrency || sourceBuyingCurrency || buyingPriceCurrency;
			const isForeignBuyingScalar = Boolean(
				scalarCurrency &&
					companyCurrency &&
					scalarCurrency !== companyCurrency &&
					(!buyingPriceCurrency || scalarCurrency === buyingPriceCurrency),
			);
			const buyingScalarFxUnresolved =
				hasExplicitInvalidBuyingFx ||
				(hasExplicitNullBaseBuying &&
					(rawBuyingExchangeRate === null || rawBuyingExchangeRate <= 0));
			const currencyMismatch = Boolean(
				scalarCurrency &&
					effectiveInvoiceCurrency &&
					scalarCurrency !== effectiveInvoiceCurrency,
			);
			// Mirror ERPNext setup/utils.py:77 & backend _resolve_floor_rates: even when
			// sourceCurrency === invoiceCurrency !== companyCurrency, a raw buying scalar with
			// buying_exchange_rate converts via for_buying FX (and fails closed if null/<= 0).
			if (
				isForeignBuyingScalar &&
				(buyingScalarFxUnresolved || rawBuyingExchangeRate !== null)
			) {
				if (
					buyingScalarFxUnresolved ||
					rawBuyingExchangeRate === null ||
					rawBuyingExchangeRate <= 0 ||
					invoiceToCompanyRate === null
				) {
					hasUnresolvedCrossCurrencyCost = true;
				} else {
					resolvedCandidates.push({
						field: candidate.field,
						label: candidate.label,
						rate: rawRate * rawBuyingExchangeRate,
						baseCurrency: true,
					});
				}
				continue;
			}
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

	if (!hasNormalizedBuyingPrice) {
		const rawSourceBuyingPrice = toFiniteNumber(item._source_buying_price);
		if (
			rawSourceBuyingPrice !== null &&
			rawSourceBuyingPrice > 0 &&
			(!buyingRateCurrency || buyingRateCurrency === sourceBuyingCurrency)
		) {
			const sourceCurrency = sourceBuyingCurrency || buyingPriceCurrency;
			const isForeignSource = Boolean(
				sourceCurrency &&
					companyCurrency &&
					sourceCurrency !== companyCurrency &&
					(!buyingPriceCurrency || sourceCurrency === buyingPriceCurrency),
			);
			const sourceFxUnresolved =
				hasExplicitInvalidBuyingFx ||
				(hasExplicitNullBaseBuying &&
					(rawBuyingExchangeRate === null || rawBuyingExchangeRate <= 0));
			const sourceMismatch = Boolean(
				sourceCurrency &&
					effectiveInvoiceCurrency &&
					sourceCurrency !== effectiveInvoiceCurrency,
			);
			if (
				isForeignSource &&
				(sourceFxUnresolved || rawBuyingExchangeRate !== null)
			) {
				if (
					sourceFxUnresolved ||
					rawBuyingExchangeRate === null ||
					rawBuyingExchangeRate <= 0 ||
					invoiceToCompanyRate === null
				) {
					hasUnresolvedCrossCurrencyCost = true;
				} else {
					resolvedCandidates.push({
						field: "_source_buying_price",
						label: "Trade Price",
						rate: rawSourceBuyingPrice * rawBuyingExchangeRate,
						baseCurrency: true,
					});
				}
			} else if (sourceMismatch) {
				if (invoiceToCompanyRate === null) {
					hasUnresolvedCrossCurrencyCost = true;
				} else if (
					companyCurrency &&
					sourceCurrency === companyCurrency
				) {
					resolvedCandidates.push({
						field: "_source_buying_price",
						label: "Trade Price",
						rate: rawSourceBuyingPrice,
						baseCurrency: true,
					});
				} else if (
					buyingPriceCurrency &&
					sourceCurrency === buyingPriceCurrency &&
					rawBuyingExchangeRate !== null &&
					rawBuyingExchangeRate > 0
				) {
					resolvedCandidates.push({
						field: "_source_buying_price",
						label: "Trade Price",
						rate: rawSourceBuyingPrice * rawBuyingExchangeRate,
						baseCurrency: true,
					});
				} else {
					hasUnresolvedCrossCurrencyCost = true;
				}
			} else if (
				!resolvedCandidates.some((candidate) =>
					["trade_price", "buying_price", "buying_rate"].includes(
						candidate.field,
					),
				)
			) {
				resolvedCandidates.push({
					field: "_source_buying_price",
					label: "Trade Price",
					rate: rawSourceBuyingPrice,
					baseCurrency: false,
				});
			}
		}
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
	allowZero = false,
): number | null {
	const rawNetAmount = toFiniteNumber(item.net_amount);
	if (
		rawNetAmount !== null &&
		rawNetAmount >= 0 &&
		(rawNetAmount > 0 || grossSellingRate <= 0 || allowZero)
	) {
		return Math.abs(rawNetAmount);
	}
	const rawNetRate = toFiniteNumber(item.net_rate);
	if (
		rawNetRate !== null &&
		rawNetRate >= 0 &&
		(rawNetRate > 0 || grossSellingRate <= 0 || allowZero)
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
	allowZero = false,
	preserveUnitNetRate = false,
): number | null {
	const rawNetAmount = toFiniteNumber(item.net_amount);
	const lineGrossAmount =
		qty !== null && qty > 0
			? roundToPrecision(grossSellingRate * qty, precision)
			: null;
	const netAmountMatchesLineGross =
		rawNetAmount !== null &&
		lineGrossAmount !== null &&
		Math.abs(Math.abs(rawNetAmount) - lineGrossAmount) <= 1e-9;
	const netFromAmount =
		rawNetAmount !== null &&
		rawNetAmount >= 0 &&
		(rawNetAmount > 0 || grossSellingRate <= 0 || allowZero) &&
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
			allowZero ||
			(netFromAmount !== null &&
				Math.abs(Math.abs(rawNetRate) - netFromAmount) <= halfUlp + 1e-9));
	if (hasValidNetRate) {
		const normalizedNetRate = Math.abs(rawNetRate!);
		// ERPNext taxes_and_totals.py:275-291 keeps net_rate = rate (unmodified unit rate)
		// when there is no inclusive tax and no invoice discount, while rounding net_amount =
		// flt(rate * qty, prec). On fractional/weighed qty (e.g. 0.10 kg x $1.03 = $0.10),
		// preserve net_rate instead of dividing the rounded line amount by qty.
		if (
			preserveUnitNetRate &&
			netAmountMatchesLineGross &&
			normalizedNetRate <= grossSellingRate + 1e-9
		) {
			return normalizedNetRate;
		}
		// ERPNext taxes_and_totals.py:1118-1121 rounds net_rate = flt(net_amount / qty, prec).
		// Honor native rounded net_rate (including 0.00) when consistent with net_amount / qty within 0.5 ULP.
		if (
			netFromAmount === null ||
			Math.abs(normalizedNetRate - netFromAmount) <= halfUlp + 1e-9
		) {
			return normalizedNetRate;
		}
		return Math.min(netFromAmount, roundToPrecision(netFromAmount, precision));
	}
	if (preserveUnitNetRate && netAmountMatchesLineGross) {
		return grossSellingRate;
	}
	return netFromAmount;
}

function resolveRoundRowWiseTax(options: LossPreventionOptions): boolean {
	if (options.roundRowWiseTax !== undefined) {
		return Boolean(options.roundRowWiseTax);
	}
	const flagVal = (globalThis as any)?.frappe?.flags?.round_row_wise_tax;
	if (flagVal !== undefined && flagVal !== null) {
		return flagVal === 1 || flagVal === true || flagVal === "1";
	}
	const sysVal = (globalThis as any)?.frappe?.boot?.sysdefaults
		?.round_row_wise_tax;
	return sysVal === 1 || sysVal === true || sysVal === "1";
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
	const roundRowWiseTax = resolveRoundRowWiseTax(options);

	let eligibleCount = 0;
	let nativeSum = 0;
	let expectedSum = 0;
	let preDiscountSum = 0;
	let hasZeroAboveHalfUlp = false;
	let allRowsMatchRowWise = true;
	const taxGroupTotals = new Map<
		string,
		{ nativeSum: number; preSum: number }
	>();

	for (const item of items) {
		if (!item || item.is_return || item.posa_is_replace) {
			continue;
		}
		const qty = toFiniteNumber(item.qty) || 0;
		if (qty <= 0) {
			continue;
		}
		eligibleCount += 1;
		const grossRate = getEffectiveSellingRate(item);
		const { inclA, inclB } = computeRowTaxParams(item, options.taxes);
		const hasInclusiveTax = inclA > 0 || inclB > 0;
		// Mirror ERPNext taxes_and_totals.py:287 & :438-442: round gross item.amount
		// to currency precision before extracting inclusive tax into net_amount.
		const lineGrossAmount = roundToPrecision(grossRate * qty, precision);
		const preAmount = Math.max(
			(lineGrossAmount - inclB * qty) / (1 + inclA),
			0,
		);
		const computedAmount =
			preAmount * (1 - invoiceDiscountPercentage / 100);
		let nativeAmount = resolveNativeNetAmount(item, grossRate, qty, true);
		if (nativeAmount === null) {
			if (isPricingRuleFreeLine(item) && grossRate <= 0) {
				nativeAmount = 0;
			} else {
				return false;
			}
		}
		// ERPNext taxes_and_totals.py:446-455 can legitimately carry rounding onto an
		// inclusive-tax row with preAmount <= 1 ULP, yielding net_amount = 0 even with
		// discount === 0. Only require an invoice discount when a zero row cannot come
		// from carried inclusive-tax rounding.
		if (
			nativeAmount === 0 &&
			grossRate > 0 &&
			computedAmount > 0.5 * ulp + 1e-9 &&
			(!hasInclusiveTax || computedAmount > ulp + 1e-9)
		) {
			hasZeroAboveHalfUlp = true;
		}
		const rowWiseExpected = Math.min(
			lineGrossAmount,
			roundToPrecision(preAmount, precision),
		);
		if (Math.abs(nativeAmount - rowWiseExpected) > 1e-9) {
			allRowsMatchRowWise = false;
		}
		// ERPNext taxes_and_totals.py:438-462 carries inclusive-tax rounding across rows
		// within the same tax group, permitting up to +1 ULP on an individual inclusive row
		// (strictly capped at rounded gross line amount) while keeping the tax-group total within 0.5 ULP.
		const maxPreAmount = hasInclusiveTax
			? Math.min(lineGrossAmount, preAmount + ulp)
			: preAmount;
		if (
			nativeAmount > maxPreAmount + 1e-9 ||
			nativeAmount > computedAmount + ulp + 1e-9
		) {
			return false;
		}
		nativeSum += nativeAmount;
		expectedSum += computedAmount;
		preDiscountSum += preAmount;
		const itemTaxKey = Object.entries(parseItemTaxRateMap(item))
			.sort(([left], [right]) => left.localeCompare(right))
			.map(([k, v]) => `${k}=${v}`)
			.join("|");
		const groupKey = `${Math.round(inclA * 1e9)}:${Math.round(inclB * 1e9)}:${itemTaxKey}`;
		const existingGroup = taxGroupTotals.get(groupKey) || {
			nativeSum: 0,
			preSum: 0,
		};
		existingGroup.nativeSum += nativeAmount;
		existingGroup.preSum += preAmount;
		taxGroupTotals.set(groupKey, existingGroup);
	}

	if (eligibleCount === 0) {
		return false;
	}
	const hasUnappliedDiscount =
		expectedSum < preDiscountSum - 0.5 * ulp - 1e-9 &&
		nativeSum >= preDiscountSum - 1e-9;
	if (hasUnappliedDiscount) {
		return false;
	}
	if (hasZeroAboveHalfUlp) {
		return (
			expectedSum < preDiscountSum - 1e-9 &&
			Math.abs(nativeSum - expectedSum) <= 0.5 * ulp + 1e-9
		);
	}
	// When undiscounted, ERPNext either rounds each row independently (round_row_wise_tax=1,
	// taxes_and_totals.py:442) or carries rounding strictly within each tax group
	// (taxes_and_totals.py:446-455). When discounted, taxes_and_totals.py:1105-1116
	// carries rounding across the whole invoice so sum(item.net_amount) stays within 0.5 ULP.
	if (Math.abs(expectedSum - preDiscountSum) <= 1e-9) {
		if (roundRowWiseTax) {
			return allRowsMatchRowWise;
		}
		for (const group of taxGroupTotals.values()) {
			if (Math.abs(group.nativeSum - group.preSum) > 0.5 * ulp + 1e-9) {
				return false;
			}
		}
		return true;
	}
	return Math.abs(nativeSum - expectedSum) <= 0.5 * ulp + 1e-9;
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
	const rawPrecision = toFiniteNumber(
		options.currencyPrecision ?? item.currency_precision,
	);
	const precision =
		rawPrecision !== null && rawPrecision >= 0 ? Math.trunc(rawPrecision) : 2;
	const computedNetRate =
		preDiscountNetRate * (1 - invoiceDiscountPercentage / 100);
	const halfUlp = 0.5 * 10 ** -precision;
	const allowZeroNativeNet =
		options.reconciledNativeNet === true ||
		(options.reconciledNativeNet === undefined &&
			computedNetRate <= halfUlp + 1e-9);
	const preserveUnitNetRate =
		inclA === 0 && inclB === 0 && invoiceDiscountPercentage <= 0;
	const nativeUnitNetRate = resolveNativeUnitNetRate(
		item,
		grossSellingRate,
		qty,
		precision,
		allowZeroNativeNet,
		preserveUnitNetRate,
	);
	const maxUnreconciledNetRate =
		options.reconciledNativeNet === false
			? preDiscountNetRate
			: Math.min(
					grossSellingRate,
					Math.max(
						preDiscountNetRate,
						roundToPrecision(preDiscountNetRate, precision),
					),
				);
	let effectiveSellingRate: number;
	if (options.reconciledNativeNet && nativeUnitNetRate !== null) {
		effectiveSellingRate = nativeUnitNetRate;
	} else if (
		nativeUnitNetRate !== null &&
		(nativeUnitNetRate < preDiscountNetRate - EPSILON ||
			isCashDiscount ||
			invoiceDiscountPercentage <= 0)
	) {
		effectiveSellingRate = Math.min(
			nativeUnitNetRate,
			maxUnreconciledNetRate,
		);
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
	// Mirror ERPNext selling_controller.py:473-487: round both selling rate and
	// cost floor to currency precision before comparison rather than fixed 0.0001.
	const comparedSellingRate = roundToPrecision(comparisonSellingRate, precision);
	const comparedMinimumRate = roundToPrecision(minimumCostRate, precision);
	if (comparedSellingRate >= comparedMinimumRate) {
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
