import { beforeEach, describe, expect, it, vi } from "vitest";

const offlineMocks = vi.hoisted(() => ({
	getCachedExchangeRate: vi.fn(() => null),
	saveExchangeRateCache: vi.fn(),
}));
const repositoryMocks = vi.hoisted(() => ({
	currencyRateRepository: {
		findLatestOnOrBefore: vi.fn().mockResolvedValue(null),
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
		repositoryMocks.currencyRateRepository.findLatestOnOrBefore.mockResolvedValue({
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
			repositoryMocks.currencyRateRepository.findLatestOnOrBefore,
		).toHaveBeenCalledWith(
			expect.objectContaining({
				fromCurrency: "USD",
				toCurrency: "UGX",
				purpose: "for_buying",
			}),
		);
	});
});
