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

type CurrencyRateLookup = {
	profileName: string;
	company: string;
	fromCurrency: string;
	toCurrency: string;
	date: string;
	purpose?: "for_buying" | "for_selling" | string | null;
};

function isFlagEnabled(value: unknown): boolean {
	return value === 1 || value === true || value === "1";
}

function hasExplicitPurposeFlags(row: Record<string, any>): boolean {
	return (
		(row?.for_buying !== undefined && row?.for_buying !== null) ||
		(row?.for_selling !== undefined && row?.for_selling !== null)
	);
}

/**
 * Select the authoritative Currency Exchange row matching ERPNext setup/utils.py:66-98:
 * - Date on or before `targetDate`
 * - Purpose filter (`for_buying` / `for_selling`), staying backwards-compatible with legacy
 *   cached rows that omit both purpose flags
 * - Order: `date desc`, purpose-specific before dual-purpose (`for_selling asc` for buying,
 *   `for_buying asc` for selling), `name desc`, falling back to `modified desc` only if tied.
 */
export function selectCurrencyRateRecord<T extends Record<string, any>>(
	rows: T[],
	targetDate?: string | null,
	purpose?: string | null,
): T | null {
	const cutoff = String(targetDate || "").slice(0, 10);
	const normalizedPurpose =
		purpose === "for_buying" || purpose === "for_selling" ? purpose : null;
	const eligible = (rows || []).filter((row) => {
		if (!row) {
			return false;
		}
		const rowDate = String(row.date || "").slice(0, 10);
		if (cutoff && rowDate && rowDate > cutoff) {
			return false;
		}
		if (normalizedPurpose && hasExplicitPurposeFlags(row)) {
			return isFlagEnabled(row[normalizedPurpose]);
		}
		return true;
	});

	eligible.sort((left, right) => {
		const dateOrder = String(right?.date || "")
			.slice(0, 10)
			.localeCompare(String(left?.date || "").slice(0, 10));
		if (dateOrder) {
			return dateOrder;
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
		const nameOrder = String(right?.name || "").localeCompare(
			String(left?.name || ""),
		);
		if (nameOrder) {
			return nameOrder;
		}
		return String(right?.modified || "").localeCompare(
			String(left?.modified || ""),
		);
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

	async findLatestOnOrBefore(
		lookup: CurrencyRateLookup,
	): Promise<OfflineCurrencyRateRecord | null> {
		const rows = await this.findForPair(lookup);
		return selectCurrencyRateRecord(rows, lookup.date, lookup.purpose);
	}
}

export const currencyRateRepository = new CurrencyRateRepository();
