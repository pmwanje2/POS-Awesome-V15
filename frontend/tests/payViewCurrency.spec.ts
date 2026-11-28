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

		const exchangeRatePurpose = computed(() => "for_selling");
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
			"exchangeRatePurpose",
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
			exchangeRatePurpose,
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

	it("does not shadow selected invoice company_currency with pos_profile.currency when opening data omits companyDoc and profile.company_currency", async () => {
		const applyOpeningBody = extractBlock(
			payViewSource,
			/\bconst applyOpeningData\s*=\s*async\s*\(data\)\s*=>\s*\{/,
			"applyOpeningData",
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
		const selected_invoices = ref<any[]>([]);
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
		const exchangeRatePurpose = computed(() => "for_selling");

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
			"exchangeRatePurpose",
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
			exchangeRatePurpose,
		);

		// Open USD till with neither companyDoc nor pos_profile.company_currency.
		await applyOpeningData({
			pos_profile: {
				name: "POS-USD-NO-CO",
				company: "Uganda Co",
				currency: "USD",
				payments: [],
			},
			pos_opening_shift: { name: "OPEN-USD-1" },
			company: null,
		});

		// Then select a USD invoice carrying authoritative company_currency = UGX.
		selected_invoices.value = [{ currency: "USD", company_currency: "UGX" }];
		await fetchExchangeRate();

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
	});

	it("uses purpose for_buying for Supplier Pay across fetchExchangeRate, resolvePaymentMethodRate, and normalizeSelectedPayment", async () => {
		const fetchExchangeBody = extractBlock(
			payViewSource,
			/\bconst fetchExchangeRate\s*=\s*async\s*\(\)\s*=>\s*\{/,
			"fetchExchangeRate",
		);
		const resolvePaymentMethodBody = extractBlock(
			payViewSource,
			/\bconst resolvePaymentMethodRate\s*=\s*async\s*\(method\)\s*=>\s*\{/,
			"resolvePaymentMethodRate",
		);
		const normalizeSelectedBody = extractBlock(
			payViewSource,
			/\bconst normalizeSelectedPayment\s*=\s*async\s*\(row,\s*amountField\)\s*=>\s*\{/,
			"normalizeSelectedPayment",
		);

		const paymentEntryType = ref("Pay");
		const partyType = ref("Supplier");
		const purposeMarker = /\bconst exchangeRatePurpose\s*=\s*computed\(\s*/;
		const exchangeRatePurpose = purposeMarker.test(payViewSource)
			? new Function(
					"computed",
					"paymentEntryType",
					"partyType",
					`return computed(${extractExpression(payViewSource, purposeMarker, "exchangeRatePurpose")});`,
				)(computed, paymentEntryType, partyType)
			: computed(() => "for_selling");

		const pos_profile = ref<any>({
			name: "POS-SUP-PAY",
			company: "Uganda Co",
			currency: "USD",
			company_currency: "UGX",
		});
		const company = ref("Uganda Co");
		const companyCurrencyLocal = computed(() => "UGX");
		const invoiceTotalCurrency = computed(() => "USD");
		const requiresExchangeRate = computed(() => true);
		const exchangeRate = ref<number | null>(null);
		const exchangeRateLoading = ref(false);
		const exchangeRateError = ref<string | null>(null);
		const postingDate = ref("2026-11-28");
		const available_bank_accounts = ref<Record<string, any[]>>({});

		const resolveExchangeRate = vi.fn(async ({ fromCurrency, toCurrency, purpose }) => ({
			found: true,
			fromCurrency,
			toCurrency,
			rate:
				fromCurrency === toCurrency
					? 1
					: purpose === "for_buying"
						? 4050
						: 3700,
			effectiveDate: "2026-11-28",
			rateDate: "2026-11-28",
			source: fromCurrency === toCurrency ? "same_currency" : "currency_exchange",
		}));

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
			"exchangeRatePurpose",
			"paymentEntryType",
			"partyType",
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
			exchangeRatePurpose,
			paymentEntryType,
			partyType,
		);

		const resolvePaymentMethodRate = new Function(
			"available_bank_accounts",
			"getPaymentMethodCurrency",
			"invoiceTotalCurrency",
			"companyCurrencyLocal",
			"pos_profile",
			"company",
			"postingDate",
			"getTodayDate",
			"resolveExchangeRate",
			"flt",
			"exchangeRatePurpose",
			"paymentEntryType",
			"partyType",
			`return async (method) => { ${resolvePaymentMethodBody} };`,
		)(
			available_bank_accounts,
			() => "USD",
			invoiceTotalCurrency,
			companyCurrencyLocal,
			pos_profile,
			company,
			postingDate,
			() => "2026-11-28",
			resolveExchangeRate,
			(v: unknown) => Number(v || 0),
			exchangeRatePurpose,
			paymentEntryType,
			partyType,
		);

		const normalizeSelectedPayment = new Function(
			"pos_profile",
			"invoiceTotalCurrency",
			"resolveExchangeRate",
			"company",
			"postingDate",
			"getTodayDate",
			"flt",
			"exchangeRatePurpose",
			"paymentEntryType",
			"partyType",
			`return async (row, amountField) => { ${normalizeSelectedBody} };`,
		)(
			pos_profile,
			invoiceTotalCurrency,
			resolveExchangeRate,
			company,
			postingDate,
			() => "2026-11-28",
			(v: unknown) => Number(v || 0),
			exchangeRatePurpose,
			paymentEntryType,
			partyType,
		);

		await fetchExchangeRate();
		const method: any = { mode_of_payment: "Cash", amount: 10, payment_currency: "USD" };
		await resolvePaymentMethodRate(method);
		const unallocatedRow: any = { currency: "EUR", unallocated_amount: 10 };
		await normalizeSelectedPayment(unallocatedRow, "unallocated_amount");

		expect(exchangeRate.value).toBe(4050);
		expect(method.company_exchange_rate).toBe(4050);
		const purposes = resolveExchangeRate.mock.calls.map(([arg]) => arg.purpose);
		expect(purposes.length).toBeGreaterThanOrEqual(4);
		for (const p of purposes) {
			expect(p).toBe("for_buying");
		}
	});

	it("cancels stale in-flight for_selling FX lookup when switching to Supplier Pay (for_buying)", async () => {
		const fetchExchangeBody = extractBlock(
			payViewSource,
			/\bconst fetchExchangeRate\s*=\s*async\s*\(\)\s*=>\s*\{/,
			"fetchExchangeRate",
		);
		const purposeExpr = extractExpression(
			payViewSource,
			/\bconst exchangeRatePurpose\s*=\s*computed\(\s*/,
			"exchangeRatePurpose",
		);

		const paymentEntryType = ref("Receive");
		const partyType = ref("Customer");
		const exchangeRatePurpose = new Function(
			"computed",
			"paymentEntryType",
			"partyType",
			`return computed(${purposeExpr});`,
		)(computed, paymentEntryType, partyType);

		const pos_profile = ref<any>({
			name: "POS-RACE",
			company: "Uganda Co",
			currency: "USD",
			company_currency: "UGX",
		});
		const company = ref("Uganda Co");
		const companyCurrencyLocal = computed(() => "UGX");
		const invoiceTotalCurrency = computed(() => "USD");
		const requiresExchangeRate = computed(() => true);
		const exchangeRate = ref<number | null>(null);
		const exchangeRateLoading = ref(false);
		const exchangeRateError = ref<string | null>(null);
		const postingDate = ref("2026-11-28");

		let releaseSellingLookup!: () => void;
		const sellingPromise = new Promise<any>((resolve) => {
			releaseSellingLookup = () =>
				resolve({
					found: true,
					fromCurrency: "USD",
					toCurrency: "UGX",
					rate: 3700,
					effectiveDate: "2026-11-28",
					rateDate: "2026-11-28",
					source: "currency_exchange",
				});
		});

		const resolveExchangeRate = vi.fn(({ fromCurrency, toCurrency, purpose }) => {
			if (purpose === "for_selling") {
				return sellingPromise;
			}
			return Promise.resolve({
				found: true,
				fromCurrency,
				toCurrency,
				rate: 4050,
				effectiveDate: "2026-11-28",
				rateDate: "2026-11-28",
				source: "currency_exchange",
			});
		});

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
			"exchangeRatePurpose",
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
			exchangeRatePurpose,
		);

		// Kick off slow Customer Receive (for_selling) lookup.
		const sellingCall = fetchExchangeRate();
		expect(exchangeRateLoading.value).toBe(true);

		// While in flight, switch to Supplier Pay (for_buying) and trigger fetchExchangeRate.
		paymentEntryType.value = "Pay";
		partyType.value = "Supplier";
		expect(exchangeRatePurpose.value).toBe("for_buying");
		await fetchExchangeRate();

		// Release the stale for_selling response late — must NOT overwrite 4050 with 3700.
		releaseSellingLookup();
		await sellingCall;

		expect(resolveExchangeRate.mock.calls.map(([arg]) => arg.purpose)).toEqual([
			"for_selling",
			"for_buying",
		]);
		expect(exchangeRate.value).toBe(4050);
		expect(exchangeRateLoading.value).toBe(false);
	});

	it("binds resolved companyCurrencyLocal to PayTotalsSidebar and returns it from setup", () => {
		const sidebarMatch = /<PayTotalsSidebar[\s\S]*?\/>/.exec(payViewSource);
		expect(sidebarMatch).not.toBeNull();
		expect(sidebarMatch?.[0]).toContain(':company-currency="companyCurrencyLocal"');

		const setupReturnMatch = /\breturn\s*\{[\s\S]*?\};\s*\}\s*,\s*\}\s*;\s*<\/script>/.exec(
			payViewSource,
		);
		expect(setupReturnMatch).not.toBeNull();
		expect(setupReturnMatch?.[0]).toMatch(/\bcompanyCurrencyLocal\b/);
	});
});
