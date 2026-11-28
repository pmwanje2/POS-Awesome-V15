import { computed, ref } from "vue";
import { describe, expect, it, vi } from "vitest";

import payViewSource from "../src/posapp/components/pos/shell/PayView.vue?raw";

function extractBlock(source: string, marker: RegExp, label: string): string {
	const match = marker.exec(source);
	if (!match) {
		throw new Error(`${label} not found in PayView.vue`);
	}
	const bodyStart = match.index + match[0].length - 1;
	let depth = 0;
	let bodyEnd = -1;
	for (let i = bodyStart; i < source.length; i += 1) {
		const ch = source[i];
		if (ch === "{") depth += 1;
		else if (ch === "}") {
			depth -= 1;
			if (depth === 0) {
				bodyEnd = i;
				break;
			}
		}
	}
	if (bodyEnd === -1) {
		throw new Error(`Unterminated ${label} in PayView.vue`);
	}
	return source.slice(bodyStart + 1, bodyEnd);
}

function extractExpression(source: string, marker: RegExp, label: string): string {
	const match = marker.exec(source);
	if (!match) {
		throw new Error(`${label} not found in PayView.vue`);
	}
	const start = match.index + match[0].length;
	const end = source.indexOf(");", start);
	if (end === -1) {
		throw new Error(`Unterminated ${label} in PayView.vue`);
	}
	return source.slice(start, end);
}

describe("PayView.vue company currency & selling FX resolution", () => {
	it("resolves company currency from pos_profile.company_currency and fetches selling FX when opening companyDoc is absent", async () => {
		const applyOpeningBody = extractBlock(
			payViewSource,
			/\bconst applyOpeningData\s*=\s*async\s*\(data\)\s*=>\s*\{/,
			"applyOpeningData",
		);
		const fetchCompanyBody = extractBlock(
			payViewSource,
			/\bconst fetchCompanyCurrency\s*=\s*async\s*\(\)\s*=>\s*\{/,
			"fetchCompanyCurrency",
		);
		const fetchExchangeBody = extractBlock(
			payViewSource,
			/\bconst fetchExchangeRate\s*=\s*async\s*\(\)\s*=>\s*\{/,
			"fetchExchangeRate",
		);
		const companyCurrencyLocalExpr = extractExpression(
			payViewSource,
			/\bconst companyCurrencyLocal\s*=\s*computed\(\s*/,
			"companyCurrencyLocal",
		);

		const pos_profile = ref<any>(null);
		const pos_opening_shift = ref<any>(null);
		const company = ref<string>("");
		const companyCurrency = ref<string | null>(null);
		const selected_invoices = ref<any[]>([{ currency: "USD", company_currency: "UGX" }]);
		const invoiceTotalCurrency = computed(
			() => selected_invoices.value[0]?.currency || pos_profile.value?.currency || "",
		);
		const exchangeRate = ref<number | null>(null);
		const exchangeRateLoading = ref(false);
		const exchangeRateError = ref<string | null>(null);
		const postingDate = ref("2026-11-28");
		const payment_methods_list = ref<string[]>([]);

		const companyCurrencyLocal = new Function(
			"computed",
			"companyCurrency",
			"pos_profile",
			"selected_invoices",
			`return computed(${companyCurrencyLocalExpr});`,
		)(computed, companyCurrency, pos_profile, selected_invoices);

		const requiresExchangeRate = computed(
			() => invoiceTotalCurrency.value !== companyCurrencyLocal.value,
		);

		const resolveExchangeRate = vi.fn(async ({ fromCurrency, toCurrency }) => ({
			found: true,
			fromCurrency,
			toCurrency,
			rate: fromCurrency === toCurrency ? 1 : 3700,
			effectiveDate: "2026-11-28",
			rateDate: "2026-11-28",
			source: fromCurrency === toCurrency ? "same_currency" : "currency_exchange",
		}));

		const applyOpeningData = new Function(
			"pos_profile",
			"pos_opening_shift",
			"company",
			"companyCurrency",
			"uiStore",
			"proxy",
			"set_payment_methods",
			"loadPaymentMethodCurrencies",
			"payment_methods_list",
			`return async (data) => { ${applyOpeningBody} };`,
		)(
			pos_profile,
			pos_opening_shift,
			company,
			companyCurrency,
			{ setRegisterData: vi.fn() },
			{ eventBus: { emit: vi.fn() } },
			vi.fn(),
			vi.fn(async () => undefined),
			payment_methods_list,
		);

		const fetchExchangeRate = new Function(
			"exchangeRateLoading",
			"requiresExchangeRate",
			"exchangeRate",
			"invoiceTotalCurrency",
			"companyCurrencyLocal",
			"exchangeRateError",
			"resolveExchangeRate",
			"pos_profile",
			"company",
			"postingDate",
			"getTodayDate",
			"__",
			`return async () => { ${fetchExchangeBody} };`,
		)(
			exchangeRateLoading,
			requiresExchangeRate,
			exchangeRate,
			invoiceTotalCurrency,
			companyCurrencyLocal,
			exchangeRateError,
			resolveExchangeRate,
			pos_profile,
			company,
			postingDate,
			() => "2026-11-28",
			(s: string) => s,
		);

		const frappeStub = {
			call: vi.fn(async () => {
				throw new Error("offline");
			}),
		};
		const fetchCompanyCurrency = new Function(
			"company",
			"frappe",
			"companyCurrency",
			"pos_profile",
			"selected_invoices",
			`return async () => { ${fetchCompanyBody} };`,
		)(company, frappeStub, companyCurrency, pos_profile, selected_invoices);

		// Opening data omits company doc (company: null), while pos_profile carries company_currency: "UGX" and currency: "USD".
		await applyOpeningData({
			pos_profile: {
				name: "POS-USD-UGX",
				company: "Uganda Co",
				currency: "USD",
				company_currency: "UGX",
				payments: [],
			},
			pos_opening_shift: { name: "OPEN-1" },
			company: null,
		});
		await fetchExchangeRate();

		expect(companyCurrency.value).toBe("UGX");
		expect(companyCurrencyLocal.value).toBe("UGX");
		expect(requiresExchangeRate.value).toBe(true);
		expect(exchangeRate.value).toBe(3700);
		expect(resolveExchangeRate).toHaveBeenCalledWith(
			expect.objectContaining({
				fromCurrency: "USD",
				toCurrency: "UGX",
				purpose: "for_selling",
			}),
		);

		// If fetchCompanyCurrency fails offline, it must keep pos_profile.company_currency ("UGX"), never overwrite with "USD".
		await fetchCompanyCurrency();
		expect(companyCurrency.value).toBe("UGX");
	});
});
