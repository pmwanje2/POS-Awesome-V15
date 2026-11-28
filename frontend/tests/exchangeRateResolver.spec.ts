import { beforeEach, describe, expect, it, vi } from "vitest";

const offlineMocks = vi.hoisted(() => ({
	getCachedExchangeRate: vi.fn(() => null),
	saveExchangeRateCache: vi.fn(),
	isOffline: vi.fn(() => false),
}));
const repositoryMocks = vi.hoisted(() => ({
	currencyRateRepository: {
		findLatestOnOrBefore: vi.fn().mockResolvedValue(null),
		findDirectLatestOnOrBefore: vi.fn().mockResolvedValue(null),
		findInverseLatestOnOrBefore: vi.fn().mockResolvedValue(null),
		getExchangeSettings: vi.fn().mockResolvedValue(null),
	},
}));

vi.mock("../src/offline/index", () => offlineMocks);
vi.mock("../src/offline/repositories", () => repositoryMocks);

import {
	clearExchangeRateResolverCache,
	resolveExchangeRate,
} from "../src/posapp/services/exchangeRateResolver";

describe("exchange rate resolver", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		offlineMocks.getCachedExchangeRate.mockReset();
		offlineMocks.getCachedExchangeRate.mockReturnValue(null);
		offlineMocks.isOffline.mockReset();
		offlineMocks.isOffline.mockReturnValue(false);
		repositoryMocks.currencyRateRepository.findLatestOnOrBefore.mockReset();
		repositoryMocks.currencyRateRepository.findLatestOnOrBefore.mockResolvedValue(null);
		repositoryMocks.currencyRateRepository.findDirectLatestOnOrBefore.mockReset();
		repositoryMocks.currencyRateRepository.findDirectLatestOnOrBefore.mockResolvedValue(null);
		repositoryMocks.currencyRateRepository.findInverseLatestOnOrBefore.mockReset();
		repositoryMocks.currencyRateRepository.findInverseLatestOnOrBefore.mockResolvedValue(null);
		repositoryMocks.currencyRateRepository.getExchangeSettings.mockReset();
		repositoryMocks.currencyRateRepository.getExchangeSettings.mockResolvedValue(null);
		clearExchangeRateResolverCache();
		(globalThis as any).frappe = { call: vi.fn() };
	});

	it("returns one for the same currency without any I/O", async () => {
		const result = await resolveExchangeRate({
			fromCurrency: "AAA",
			toCurrency: "AAA",
			effectiveDate: "2026-08-20",
		});
		expect(result).toMatchObject({ found: true, rate: 1, source: "same_currency" });
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();
		expect(repositoryMocks.currencyRateRepository.findLatestOnOrBefore).not.toHaveBeenCalled();
	});

	it("deduplicates concurrent requests for the same dated pair", async () => {
		(globalThis as any).frappe.call.mockResolvedValue({
			message: { found: true, exchange_rate: 2.5, date: "2026-08-19" },
		});
		const args = {
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "AAA",
			toCurrency: "BBB",
			effectiveDate: "2026-08-20",
		};
		const [first, second] = await Promise.all([
			resolveExchangeRate(args),
			resolveExchangeRate(args),
		]);
		expect(first.rate).toBe(2.5);
		expect(second.rate).toBe(2.5);
		expect((globalThis as any).frappe.call).toHaveBeenCalledTimes(1);
	});

	it("returns an explicit missing-rate state and never substitutes one", async () => {
		(globalThis as any).frappe.call.mockResolvedValue({ message: { found: false } });
		const result = await resolveExchangeRate({
			fromCurrency: "AAA",
			toCurrency: "BBB",
			effectiveDate: "2026-08-20",
		});
		expect(result.found).toBe(false);
		expect(result.rate).toBeNull();
	});

	it("uses an allowed manual rate without server or cache lookup", async () => {
		const result = await resolveExchangeRate({
			fromCurrency: "AAA",
			toCurrency: "BBB",
			effectiveDate: "2026-08-20",
			allowManualRate: true,
			manualRate: 3,
		});
		expect(result).toMatchObject({ found: true, rate: 3, source: "manual" });
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();
	});

	it("bypasses unqualified legacy warm cache when explicit purpose for_buying is requested", async () => {
		offlineMocks.getCachedExchangeRate.mockReturnValue({
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-08-20",
			exchange_rate: 3700,
		});
		repositoryMocks.currencyRateRepository.findDirectLatestOnOrBefore.mockResolvedValue({
			name: "FX-BUY-4050",
			from_currency: "USD",
			to_currency: "UGX",
			date: "2026-08-20",
			for_buying: 1,
			for_selling: 0,
			exchange_rate: 4050,
		});

		const result = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-08-20",
			purpose: "for_buying",
		});

		expect(result).toMatchObject({
			found: true,
			rate: 4050,
			source: "offline_cache",
		});
		expect(
			repositoryMocks.currencyRateRepository.findDirectLatestOnOrBefore,
		).toHaveBeenCalledWith(
			expect.objectContaining({
				fromCurrency: "USD",
				toCurrency: "UGX",
				purpose: "for_buying",
			}),
		);
	});

	it("enforces allow_stale and stale_days on purpose-qualified warm cache hits matching ERPNext setup/utils.py:84", async () => {
		repositoryMocks.currencyRateRepository.getExchangeSettings.mockResolvedValue({
			allowStale: 0,
			staleDays: 30,
		});
		// Boundary rate dated 2026-10-29 for tx 2026-11-28 (exactly 30 days old -> ERPNext requires date > 2026-10-29).
		offlineMocks.getCachedExchangeRate.mockReturnValue({
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-10-29",
			purpose: "for_buying",
			exchange_rate: 4050,
		});
		(globalThis as any).frappe.call.mockResolvedValue({
			message: { found: false },
		});

		const staleResult = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_buying",
		});
		expect(staleResult.found).toBe(false);
		expect(staleResult.rate).toBeNull();
		expect((globalThis as any).frappe.call).toHaveBeenCalledTimes(1);

		// A fresh warm rate dated 2026-10-30 (> 2026-10-29) IS accepted without server call.
		clearExchangeRateResolverCache();
		vi.clearAllMocks();
		repositoryMocks.currencyRateRepository.getExchangeSettings.mockResolvedValue({
			allowStale: 0,
			staleDays: 30,
		});
		offlineMocks.getCachedExchangeRate.mockReturnValue({
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-10-30",
			purpose: "for_buying",
			exchange_rate: 4050,
		});

		const freshResult = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_buying",
		});
		expect(freshResult).toMatchObject({
			found: true,
			rate: 4050,
			source: "offline_cache",
		});
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();
	});

	it("derives opposite-purpose inverse exchange rate from warm cache when offline and direct purpose rate is absent", async () => {
		offlineMocks.isOffline.mockReturnValue(true);
		offlineMocks.getCachedExchangeRate.mockImplementation((query: any) => {
			if (
				query.fromCurrency === "UGX" &&
				query.toCurrency === "USD" &&
				query.purpose === "for_selling"
			) {
				return {
					fromCurrency: "UGX",
					toCurrency: "USD",
					date: "2026-11-25",
					purpose: "for_selling",
					exchange_rate: 1 / 4050,
				};
			}
			return null;
		});

		const result = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_buying",
		});
		expect(result.found).toBe(true);
		expect(result.rate).toBeCloseTo(4050, 2);
		expect(result.source).toBe("derived_inverse");
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();
	});

	it("prefers persistent direct and online direct rates over warm or in-memory inverse rates matching ERPNext setup/utils.py:124-134", async () => {
		// Warm cache holds opposite-purpose reverse rate UGX->USD = 1/4050 (for_buying),
		// while persistent repository holds direct USD->UGX = 3700 (for_selling).
		offlineMocks.getCachedExchangeRate.mockImplementation((query: any) => {
			if (
				query.fromCurrency === "UGX" &&
				query.toCurrency === "USD" &&
				query.purpose === "for_buying"
			) {
				return {
					fromCurrency: "UGX",
					toCurrency: "USD",
					date: "2026-11-25",
					purpose: "for_buying",
					exchange_rate: 1 / 4050,
				};
			}
			return null;
		});
		repositoryMocks.currencyRateRepository.findDirectLatestOnOrBefore.mockResolvedValue({
			name: "FX-USD-UGX-SELL-DIRECT",
			from_currency: "USD",
			to_currency: "UGX",
			date: "2026-11-25",
			for_buying: 0,
			for_selling: 1,
			exchange_rate: 3700,
		});
		repositoryMocks.currencyRateRepository.findLatestOnOrBefore.mockResolvedValue({
			name: "FX-USD-UGX-SELL-DIRECT",
			from_currency: "USD",
			to_currency: "UGX",
			date: "2026-11-25",
			for_buying: 0,
			for_selling: 1,
			exchange_rate: 3700,
		});

		const persistentDirect = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_selling",
		});
		expect(persistentDirect).toMatchObject({
			found: true,
			rate: 3700,
			source: "offline_cache",
		});

		// Online lookup with no local direct rate must still consult server direct before warm inverse.
		clearExchangeRateResolverCache();
		repositoryMocks.currencyRateRepository.findDirectLatestOnOrBefore.mockResolvedValue(null);
		repositoryMocks.currencyRateRepository.findLatestOnOrBefore.mockResolvedValue(null);
		(globalThis as any).frappe.call.mockResolvedValue({
			message: {
				found: true,
				exchange_rate: 3700,
				date: "2026-11-28",
				source: "currency_exchange",
			},
		});

		const onlineDirect = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_selling",
		});
		expect(onlineDirect).toMatchObject({
			found: true,
			rate: 3700,
			source: "currency_exchange",
		});
		expect((globalThis as any).frappe.call).toHaveBeenCalledTimes(1);
	});

	it("re-validates in-memory resolvedRates when stale policy tightens to allow_stale=0, stale_days=30", async () => {
		// Step 1: Resolve Oct 29 rate for Nov 28 under allow_stale=1 -> populates in-memory resolvedRates.
		repositoryMocks.currencyRateRepository.getExchangeSettings.mockResolvedValue({
			allowStale: 1,
			staleDays: 30,
		});
		offlineMocks.getCachedExchangeRate.mockReturnValue({
			fromCurrency: "USD",
			toCurrency: "UGX",
			date: "2026-10-29",
			purpose: "for_buying",
			exchange_rate: 4050,
		});

		const first = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_buying",
		});
		expect(first).toMatchObject({ found: true, rate: 4050, rateDate: "2026-10-29" });

		// Step 2: Policy tightens to allow_stale=0, stale_days=30 (ERPNext strict > excludes Oct 29 for Nov 28).
		// Calling resolveExchangeRate WITHOUT forceRefresh or cache clear MUST NOT return stale cached 4050.
		repositoryMocks.currencyRateRepository.getExchangeSettings.mockResolvedValue({
			allowStale: 0,
			staleDays: 30,
		});
		(globalThis as any).frappe.call.mockResolvedValue({
			message: { found: false },
		});

		const second = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_buying",
		});
		expect(second.found).toBe(false);
		expect(second.rate).toBeNull();
	});

	it("probes direct sources instead of returning a cached derived_inverse when a direct rate becomes available", async () => {
		offlineMocks.isOffline.mockReturnValue(true);
		offlineMocks.getCachedExchangeRate.mockImplementation((query: any) => {
			if (
				query.fromCurrency === "UGX" &&
				query.toCurrency === "USD" &&
				query.purpose === "for_buying"
			) {
				return {
					fromCurrency: "UGX",
					toCurrency: "USD",
					date: "2026-11-28",
					purpose: "for_buying",
					exchange_rate: 1 / 3700,
				};
			}
			return null;
		});

		const first = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_selling",
		});
		expect(first.found).toBe(true);
		expect(first.rate).toBeCloseTo(3700, 2);
		expect(first.source).toBe("derived_inverse");

		// A direct rate now becomes available for the same date/purpose; ordinary lookup must upgrade to 4050.
		repositoryMocks.currencyRateRepository.findDirectLatestOnOrBefore.mockResolvedValue({
			name: "FX-USD-UGX-DIRECT-4050",
			from_currency: "USD",
			to_currency: "UGX",
			date: "2026-11-28",
			for_buying: 0,
			for_selling: 1,
			exchange_rate: 4050,
		});

		const second = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_selling",
		});
		expect(second).toMatchObject({
			found: true,
			rate: 4050,
			source: "offline_cache",
		});
	});

	it("re-queries online server after an offline miss once connectivity returns", async () => {
		offlineMocks.isOffline.mockReturnValue(true);

		const offlineMiss = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_buying",
		});
		expect(offlineMiss.found).toBe(false);
		expect(offlineMiss.rate).toBeNull();
		expect((globalThis as any).frappe.call).not.toHaveBeenCalled();

		// Connectivity returns and server has a valid 4,050 rate.
		offlineMocks.isOffline.mockReturnValue(false);
		(globalThis as any).frappe.call.mockResolvedValue({
			message: {
				found: true,
				exchange_rate: 4050,
				date: "2026-11-28",
				source: "currency_exchange",
			},
		});

		const onlineHit = await resolveExchangeRate({
			profileName: "POS-1",
			company: "Example Co",
			fromCurrency: "USD",
			toCurrency: "UGX",
			effectiveDate: "2026-11-28",
			purpose: "for_buying",
		});
		expect(onlineHit).toMatchObject({
			found: true,
			rate: 4050,
			source: "currency_exchange",
		});
		expect((globalThis as any).frappe.call).toHaveBeenCalledTimes(1);
	});
});
