import { ref } from "vue";
import { describe, expect, it, vi } from "vitest";

vi.mock("../src/posapp/services/exchangeRateResolver", () => ({
	convertCurrencyAmount: (amount: number, rate: number) => amount * rate,
	resolveExchangeRate: vi.fn(async ({ fromCurrency, toCurrency }) => ({
		found: true,
		fromCurrency,
		toCurrency,
		rate: fromCurrency === toCurrency ? 1 : 285,
		effectiveDate: "2026-08-21",
		rateDate: "2026-08-21",
		source: fromCurrency === toCurrency ? "same_currency" : "currency_exchange",
	})),
}));

import { usePaymentCurrencies } from "../src/posapp/composables/pos/payments/usePaymentCurrencies";
import { useChangeCurrencies } from "../src/posapp/composables/pos/payments/useChangeCurrencies";
import { resolveExchangeRate } from "../src/posapp/services/exchangeRateResolver";

describe("usePaymentCurrencies", () => {
	it("replaces a stale initialized amount with the Alt shortcut tender", async () => {
		const invoiceDoc = ref<any>({
			currency: "PKR",
			company: "Farooq Chemicals",
			posting_date: "2026-08-21",
			payments: [],
		});
		const payment: any = {
			amount: 120,
			base_amount: 120,
			posa_payment_currency: "PKR",
			posa_original_amount: 120,
			posa_company_exchange_rate: 1,
		};
		invoiceDoc.value.payments = [payment];
		const { setInvoiceEquivalent } = usePaymentCurrencies({
			invoiceDoc,
			posProfile: ref({
				name: "POS-PROFILE-1",
				company: "Farooq Chemicals",
				currency: "PKR",
				company_currency: "PKR",
			}),
			currencyPrecision: ref(2),
			formatFloat: (value) => Number(Number(value || 0).toFixed(2)),
		});

		payment.amount = 150;
		const result = await setInvoiceEquivalent(payment, payment.amount);

		expect(result).toBe(true);
		expect(payment.amount).toBe(150);
		expect(payment.posa_original_amount).toBe(150);
		expect(payment.base_amount).toBe(150);
	});

	it("preserves an exact tender remainder while rounding invoice and base fields normally", async () => {
		const invoiceDoc = ref<any>({
			currency: "USD",
			company: "Farooq Chemicals",
			posting_date: "2026-08-21",
		});
		const payment: any = {
			posa_payment_currency: "USD",
			posa_original_amount: 0.315,
			posa_company_exchange_rate: 285,
		};
		const roundToThree = (value: unknown) =>
			Number(Number(value || 0).toFixed(3));
		const { setInvoiceEquivalent } = usePaymentCurrencies({
			invoiceDoc,
			posProfile: ref({
				name: "POS-PROFILE-1",
				company: "Farooq Chemicals",
				currency: "PKR",
				company_currency: "PKR",
				posa_enable_multi_currency_payments: 1,
			}),
			currencyPrecision: ref(3),
			formatFloat: roundToThree,
		});

		const result = await setInvoiceEquivalent(payment, 0.315, 89.7);

		expect(result).toBe(true);
		expect(payment.posa_original_amount).toBeCloseTo(0.314736842, 9);
		expect(payment.amount).toBe(0.315);
		expect(payment.base_amount).toBe(89.7);
	});

	it("passes purpose for_selling on payment and change currency FX lookups", async () => {
		vi.mocked(resolveExchangeRate).mockClear();
		const invoiceDoc = ref<any>({
			currency: "USD",
			company: "Uganda Co",
			company_currency: "UGX",
			posting_date: "2026-08-21",
			payments: [],
			posa_change_returns: [],
		});
		const posProfile = ref({
			name: "POS-USD-1",
			company: "Uganda Co",
			currency: "USD",
			company_currency: "UGX",
			posa_enable_multi_currency_payments: 1,
			posa_enable_multi_currency_change: 1,
		});

		const { setInvoiceEquivalent } = usePaymentCurrencies({
			invoiceDoc,
			posProfile,
			currencyPrecision: ref(2),
			formatFloat: (value) => Number(Number(value || 0).toFixed(2)),
		});
		const payment: any = {
			posa_payment_currency: "EUR",
			posa_original_amount: 10,
		};
		await setInvoiceEquivalent(payment, 10);

		const { addRow, updateRowCurrency } = useChangeCurrencies({
			invoiceDoc,
			posProfile,
			changeDue: ref(5),
			currencyPrecision: ref(2),
			formatFloat: (value) => Number(Number(value || 0).toFixed(2)),
		});
		addRow();
		await updateRowCurrency(invoiceDoc.value.posa_change_returns[0], "EUR");

		const calls = vi.mocked(resolveExchangeRate).mock.calls.map(([arg]) => arg);
		expect(calls.length).toBeGreaterThanOrEqual(4);
		for (const call of calls) {
			expect(call.purpose).toBe("for_selling");
		}
	});

	it("matches server three-currency tender parity via tender->invoice selling rate and invoice conversion_rate", async () => {
		const rateMap: Record<string, number> = {
			"EUR->USD": 1.1,
			"USD->UGX": 3700,
			"EUR->UGX": 4200,
		};
		vi.mocked(resolveExchangeRate).mockImplementation(
			async ({ fromCurrency, toCurrency }) => ({
				found: true,
				fromCurrency,
				toCurrency,
				rate:
					fromCurrency === toCurrency
						? 1
						: rateMap[`${fromCurrency}->${toCurrency}`] || 0,
				effectiveDate: "2026-11-28",
				rateDate: "2026-11-28",
				source:
					fromCurrency === toCurrency ? "same_currency" : "currency_exchange",
			}),
		);

		const invoiceDoc = ref<any>({
			currency: "USD",
			company: "Uganda Co",
			company_currency: "UGX",
			conversion_rate: 3700,
			posting_date: "2026-11-28",
			payments: [],
		});
		const posProfile = ref({
			name: "POS-USD-UGX",
			company: "Uganda Co",
			currency: "USD",
			company_currency: "UGX",
			posa_enable_multi_currency_payments: 1,
		});
		const payment: any = {
			posa_payment_currency: "EUR",
			posa_original_amount: 10,
		};
		invoiceDoc.value.payments = [payment];

		const { normalizePayment } = usePaymentCurrencies({
			invoiceDoc,
			posProfile,
			currencyPrecision: ref(2),
			formatFloat: (value) => Number(Number(value || 0).toFixed(2)),
		});

		const ok = await normalizePayment(payment);
		expect(ok).toBe(true);
		// Must match server payment_currency.py: EUR 10 * 1.1 = USD 11; USD 11 * 3700 = UGX 40,700 (not direct EUR->UGX 42,000).
		expect(payment.amount).toBe(11);
		expect(payment.posa_exchange_rate).toBeCloseTo(1.1, 6);
		expect(payment.posa_company_exchange_rate).toBeCloseTo(4070, 6);
		expect(payment.base_amount).toBe(40700);
	});

	it("preserves invoice conversion_rate and accepts explicit companyCurrency on three-currency tenders when doc and profile omit company_currency", async () => {
		const rateMap: Record<string, number> = {
			"EUR->USD": 1.1,
			"USD->UGX": 3700,
			"EUR->UGX": 4200,
		};
		vi.mocked(resolveExchangeRate).mockImplementation(
			async ({ fromCurrency, toCurrency }) => ({
				found: true,
				fromCurrency,
				toCurrency,
				rate:
					fromCurrency === toCurrency
						? 1
						: rateMap[`${fromCurrency}->${toCurrency}`] || 0,
				effectiveDate: "2026-11-28",
				rateDate: "2026-11-28",
				source:
					fromCurrency === toCurrency ? "same_currency" : "currency_exchange",
			}),
		);

		// Neither invoiceDoc nor posProfile sets company_currency; only invoice conversion_rate=3700 is present.
		const invoiceDoc = ref<any>({
			currency: "USD",
			company: "Uganda Co",
			conversion_rate: 3700,
			posting_date: "2026-11-28",
			payments: [],
		});
		const posProfile = ref({
			name: "POS-USD-OMITTED-CO",
			company: "Uganda Co",
			currency: "USD",
			posa_enable_multi_currency_payments: 1,
		});
		const payment: any = {
			posa_payment_currency: "EUR",
			posa_original_amount: 10,
		};
		invoiceDoc.value.payments = [payment];

		const { normalizePayment } = usePaymentCurrencies({
			invoiceDoc,
			posProfile,
			currencyPrecision: ref(2),
			formatFloat: (value) => Number(Number(value || 0).toFixed(2)),
		});

		const ok = await normalizePayment(payment);
		expect(ok).toBe(true);
		expect(payment.amount).toBe(11);
		expect(payment.posa_company_exchange_rate).toBeCloseTo(4070, 6);
		expect(payment.base_amount).toBe(40700);

		// Also verify explicit companyCurrency ref (wired from Payments.vue) resolves USD->UGX when conversion_rate is absent.
		const invoiceWithoutRate = ref<any>({
			currency: "USD",
			company: "Uganda Co",
			posting_date: "2026-11-28",
			payments: [],
		});
		const payment2: any = {
			posa_payment_currency: "EUR",
			posa_original_amount: 10,
		};
		const { normalizePayment: normalizeWithExplicitCompany } =
			usePaymentCurrencies({
				invoiceDoc: invoiceWithoutRate,
				posProfile,
				companyCurrency: ref("UGX"),
				currencyPrecision: ref(2),
				formatFloat: (value) => Number(Number(value || 0).toFixed(2)),
			} as any);

		const ok2 = await normalizeWithExplicitCompany(payment2);
		expect(ok2).toBe(true);
		expect(payment2.amount).toBe(11);
		expect(payment2.posa_company_exchange_rate).toBeCloseTo(4070, 6);
		expect(payment2.base_amount).toBe(40700);
	});
});
