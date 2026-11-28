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
	if (rawCurrencyRate !== null && rawCurrencyRate > EPSILON) {
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

export function getItemCostFloor(
	item: Record<string, unknown> | null | undefined,
): LossCostFloor | null {
	if (!item) {
		return null;
	}
	const rawUomFactor = toFiniteNumber(item.conversion_factor);
	const uomConversionFactor =
		rawUomFactor !== null && rawUomFactor > EPSILON ? rawUomFactor : 1;

	const resolvedCandidates: LossCostFloor[] = [];
	let hasNormalizedBuyingPrice = false;
	let selectedTableCurrency = "";

	const pricesByUom = item._buying_prices_by_uom;
	let selectedTableRate: number | null = null;
	if (pricesByUom && typeof pricesByUom === "object") {
		const prices = pricesByUom as Record<
			string,
			Record<string, unknown> | undefined
		>;
		const selectedUom = String(item.uom || item.stock_uom || "");
		const stockUom = String(item.stock_uom || "");
		const exact = prices[selectedUom];
		const fallback = prices[stockUom] || prices[""];
		const selected = exact || fallback;
		selectedTableCurrency = String(selected?.currency || "").trim();
		const baseRate = toFiniteNumber(selected?.base_price_list_rate);
		if (baseRate !== null && baseRate > EPSILON) {
			const conversionFactor = exact ? 1 : uomConversionFactor;
			selectedTableRate = baseRate * conversionFactor;
			hasNormalizedBuyingPrice = true;
		}
	}

	const invoiceToCompanyRate = resolveInvoiceToCompanyRate(
		item,
		selectedTableCurrency,
	);
	if (selectedTableRate !== null && invoiceToCompanyRate !== null) {
		resolvedCandidates.push({
			field: "_buying_prices_by_uom",
			label: "Trade Price",
			rate: selectedTableRate,
			baseCurrency: true,
		});
	}

	if (!hasNormalizedBuyingPrice) {
		const storedCompanyBuying = toFiniteNumber(item._company_buying_price);
		const baseBuyingRate = toFiniteNumber(item.base_buying_rate);
		const companyBuyingPrice =
			storedCompanyBuying !== null && storedCompanyBuying > EPSILON
				? storedCompanyBuying
				: baseBuyingRate !== null && baseBuyingRate > EPSILON
					? baseBuyingRate
					: null;
		if (companyBuyingPrice !== null) {
			if (invoiceToCompanyRate !== null) {
				resolvedCandidates.push({
					field:
						storedCompanyBuying !== null && storedCompanyBuying > EPSILON
							? "_company_buying_price"
							: "base_buying_rate",
					label: "Trade Price",
					rate: companyBuyingPrice,
					baseCurrency: true,
				});
			}
			hasNormalizedBuyingPrice = true;
		}
	}

	const invoiceCurrency = String(
		item.currency || item.price_list_currency || "",
	).trim();
	const buyingPriceCurrency = String(item.buying_price_currency || "").trim();
	const buyingRateCurrency = String(item.buying_rate_currency || "").trim();
	const companyCurrency = String(item.company_currency || "").trim();
	const rawBuyingExchangeRate = toFiniteNumber(item.buying_exchange_rate);

	for (const candidate of COST_CANDIDATES) {
		const rawRate = toFiniteNumber(item[candidate.field]);
		if (rawRate === null || rawRate <= EPSILON) {
			continue;
		}
		if (candidate.baseCurrency && invoiceToCompanyRate === null) {
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
					rawBuyingExchangeRate > EPSILON
				) {
					resolvedCandidates.push({
						field: candidate.field,
						label: candidate.label,
						rate: rawRate * rawBuyingExchangeRate,
						baseCurrency: true,
					});
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

	if (!resolvedCandidates.length) {
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
	const sellingRate = getEffectiveSellingRate(item);
	const invoiceDiscountPercentage = Math.min(
		Math.max(toFiniteNumber(options.invoiceDiscountPercentage) || 0, 0),
		100,
	);
	const effectiveSellingRate =
		sellingRate * (1 - invoiceDiscountPercentage / 100);
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
	const displayCostRate = cost.baseCurrency
		? minimumCostRate / currencyConversionRate
		: minimumCostRate;
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
	return items
		.map((item) => getItemLossRisk(item, options))
		.filter((risk): risk is LossRisk => Boolean(risk?.belowCost));
}
