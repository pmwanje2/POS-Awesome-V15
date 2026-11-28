import { db } from "../db";

export type OfflineCurrencyRateRecord = {
	name: string;
	profile_name: string;
	company: string;
	from_currency: string;
	to_currency: string;
	exchange_rate: number;
	date: string;
	for_buying?: number | boolean | null;
	for_selling?: number | boolean | null;
	modified?: string | null;
	[key: string]: any;
};

export type CurrencyExchangeSettings = {
	allow_stale?: number | boolean | null;
	stale_days?: number | null;
};

type CurrencyRateLookup = {
	profileName: string;
	company: string;
	fromCurrency: string;
	toCurrency: string;
	date: string;
	purpose?: "for_buying" | "for_selling" | string | null;
	allowStale?: number | boolean | null;
	staleDays?: number | null;
};

export type CurrencyRateSelectionOptions = {
	allowStale?: number | boolean | null;
	staleDays?: number | null;
};

const CURRENCY_EXCHANGE_SETTINGS_KEY = "currency_exchange_settings";

function isFlagEnabled(value: unknown): boolean {
	return value === 1 || value === true || value === "1";
}

function hasExplicitPurposeFlags(row: Record<string, any>): boolean {
	return (
		(row?.for_buying !== undefined && row?.for_buying !== null) ||
		(row?.for_selling !== undefined && row?.for_selling !== null)
	);
}

function subtractDaysIso(isoDate: string, days: number): string | null {
	const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate.slice(0, 10));
	if (!match) {
		return null;
	}
	const year = Number(match[1]);
	const month = Number(match[2]);
	const day = Number(match[3]);
	const dt = new Date(Date.UTC(year, month - 1, day - days));
	if (Number.isNaN(dt.getTime())) {
		return null;
	}
	return dt.toISOString().slice(0, 10);
}

/**
 * Select the authoritative Currency Exchange row matching ERPNext setup/utils.py:66-125:
 * - Date on or before `targetDate`
 * - Strict `date > (targetDate - staleDays)` when `allowStale` is explicitly `0`/`false`
 * - Purpose filter (`for_buying` / `for_selling`), staying backwards-compatible with legacy
 *   cached rows that omit both purpose flags
 * - Order: `date desc`, purpose-specific before dual-purpose (`for_selling asc` for buying,
 *   `for_buying asc` for selling), `name desc`, falling back to `modified desc` only if tied.
 */
export function selectCurrencyRateRecord<T extends Record<string, any>>(
	rows: T[],
	targetDate?: string | null,
	purpose?: string | null,
	options?: CurrencyRateSelectionOptions | null,
): T | null {
	const cutoff = String(targetDate || "").slice(0, 10);
	const normalizedPurpose =
		purpose === "for_buying" || purpose === "for_selling" ? purpose : null;
	const enforceStaleLimit = Boolean(
		cutoff &&
			options &&
			options.allowStale !== undefined &&
			options.allowStale !== null &&
			!isFlagEnabled(options.allowStale),
	);
	const staleDays =
		options?.staleDays !== undefined &&
		options?.staleDays !== null &&
		Number.isFinite(Number(options.staleDays)) &&
		Number(options.staleDays) >= 0
			? Math.trunc(Number(options.staleDays))
			: 1;
	const checkpointDate = enforceStaleLimit
		? subtractDaysIso(cutoff, staleDays)
		: null;

	const eligible = (rows || []).filter((row) => {
		if (!row) {
			return false;
		}
		const rowDate = String(row.date || "").slice(0, 10);
		if (cutoff && rowDate && rowDate > cutoff) {
			return false;
		}
		if (checkpointDate && (!rowDate || rowDate <= checkpointDate)) {
			return false;
		}
		if (normalizedPurpose && hasExplicitPurposeFlags(row)) {
			return isFlagEnabled(row[normalizedPurpose]);
		}
		return true;
	});

	eligible.sort((left, right) => {
		const leftDate = String(left?.date || "").slice(0, 10);
		const rightDate = String(right?.date || "").slice(0, 10);
		if (rightDate !== leftDate) {
			return rightDate > leftDate ? 1 : -1;
		}
		if (normalizedPurpose === "for_buying") {
			const leftSelling = hasExplicitPurposeFlags(left)
				? isFlagEnabled(left.for_selling)
					? 1
					: 0
				: 1;
			const rightSelling = hasExplicitPurposeFlags(right)
				? isFlagEnabled(right.for_selling)
					? 1
					: 0
				: 1;
			if (leftSelling !== rightSelling) {
				return leftSelling - rightSelling;
			}
		} else if (normalizedPurpose === "for_selling") {
			const leftBuying = hasExplicitPurposeFlags(left)
				? isFlagEnabled(left.for_buying)
					? 1
					: 0
				: 1;
			const rightBuying = hasExplicitPurposeFlags(right)
				? isFlagEnabled(right.for_buying)
					? 1
					: 0
				: 1;
			if (leftBuying !== rightBuying) {
				return leftBuying - rightBuying;
			}
		}
		const leftName = String(left?.name || "");
		const rightName = String(right?.name || "");
		if (rightName !== leftName) {
			return rightName > leftName ? 1 : -1;
		}
		const leftMod = String(left?.modified || "");
		const rightMod = String(right?.modified || "");
		if (rightMod !== leftMod) {
			return rightMod > leftMod ? 1 : -1;
		}
		return 0;
	});

	return eligible[0] || null;
}

class CurrencyRateRepository {
	async clear() {
		await db.table("currency_rate_records").clear();
	}

	async upsertMany(rows: OfflineCurrencyRateRecord[]) {
		const validRows = (rows || []).filter(
			(row) =>
				row?.name &&
				row?.profile_name &&
				row?.from_currency &&
				row?.to_currency &&
				row?.date,
		);
		if (!validRows.length) {
			return;
		}
		await db.table("currency_rate_records").bulkPut(validRows);
	}

	async deleteByNames(names: string[]) {
		const keys = [...new Set((names || []).filter(Boolean))];
		if (!keys.length) {
			return;
		}
		await db.table("currency_rate_records").bulkDelete(keys);
	}

	async findForPair({
		profileName,
		company,
		fromCurrency,
		toCurrency,
	}: Omit<CurrencyRateLookup, "date">): Promise<
		OfflineCurrencyRateRecord[]
	> {
		if (!profileName || !fromCurrency || !toCurrency) {
			return [];
		}
		return db
			.table("currency_rate_records")
			.where("[profile_name+company+from_currency+to_currency]")
			.equals([
				profileName,
				company || "",
				fromCurrency,
				toCurrency,
			])
			.toArray();
	}

	async saveExchangeSettings(settings: CurrencyExchangeSettings) {
		if (!settings || typeof settings !== "object") {
			return;
		}
		await db.table("settings").put({
			key: CURRENCY_EXCHANGE_SETTINGS_KEY,
			value: {
				allow_stale:
					settings.allow_stale === undefined || settings.allow_stale === null
						? 1
						: isFlagEnabled(settings.allow_stale)
							? 1
							: 0,
				stale_days:
					settings.stale_days !== undefined &&
					settings.stale_days !== null &&
					Number.isFinite(Number(settings.stale_days)) &&
					Number(settings.stale_days) >= 0
						? Math.trunc(Number(settings.stale_days))
						: 1,
			},
		});
	}

	async getExchangeSettings(): Promise<CurrencyRateSelectionOptions | null> {
		try {
			const entry = await db
				.table("settings")
				.get(CURRENCY_EXCHANGE_SETTINGS_KEY);
			const value = entry?.value;
			if (!value || typeof value !== "object") {
				return null;
			}
			return {
				allowStale: value.allow_stale,
				staleDays: value.stale_days,
			};
		} catch {
			return null;
		}
	}

	async findLatestOnOrBefore(
		lookup: CurrencyRateLookup,
	): Promise<OfflineCurrencyRateRecord | null> {
		const rows = await this.findForPair(lookup);
		const selectionOptions =
			lookup.allowStale !== undefined
				? {
						allowStale: lookup.allowStale,
						staleDays: lookup.staleDays,
					}
				: await this.getExchangeSettings();
		return selectCurrencyRateRecord(
			rows,
			lookup.date,
			lookup.purpose,
			selectionOptions,
		);
	}
}

export const currencyRateRepository = new CurrencyRateRepository();
