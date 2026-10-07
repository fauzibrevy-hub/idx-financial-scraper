"""Export the wide financial_reports.csv into a long-format SQLite database.

Design (plan: idx-data-mc-design.md, Option A):
  - One file: data/idx.sqlite (stdlib sqlite3 + csv only, no third-party deps).
  - Long format: metrics(ticker, year, period, metric, value, source_file).
  - Idempotent: full rebuild from CSV on every run (DROP + recreate).
  - FY is stored as-is with period='FY'; derived Q4 is NEVER persisted. The UI
    computes Q4 = FY - sum(Q1..Q3) so no derived number lives in the DB.

Usage:
    python3 model/sqlite_export.py [--data-dir data] [--db data/idx.sqlite]

The CSV header is read dynamically, so new taxonomy metrics flow through
without touching this script (risk #4 in the plan).
"""
import argparse
import csv
import sqlite3
from pathlib import Path

# CSV columns that are NOT metrics (identity / provenance / non-Tier-1 source).
NON_METRIC_COLUMNS = {
    "ticker", "year", "period", "currency", "fx_rate_at_report", "revenue_tag_source",
}

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

SCHEMA = """
DROP TABLE IF EXISTS metrics;
DROP VIEW  IF EXISTS quarterly;
DROP TABLE IF EXISTS emitens;

CREATE TABLE emitens (
    ticker TEXT PRIMARY KEY,
    nama   TEXT,
    sektor TEXT
);

CREATE TABLE metrics (
    ticker      TEXT    NOT NULL,
    year        INTEGER NOT NULL,
    period      TEXT    NOT NULL,   -- Q1|Q2|Q3|FY
    metric      TEXT    NOT NULL,   -- revenue, net_income, total_assets, ...
    value       REAL,               -- full Rupiah (XBRL native unit)
    source_file TEXT,               -- XBRL path for traceability
    PRIMARY KEY (ticker, year, period, metric)
);

CREATE INDEX idx_metrics_lookup ON metrics (ticker, metric, year, period);

-- Quarterly view: FY excluded. Q4 = FY - sum(Q1..Q3) is computed by the caller,
-- not stored here.
CREATE VIEW quarterly AS
    SELECT * FROM metrics WHERE period != 'FY';
"""


def _to_float(raw: str):
    """Return a float, or None for blank/non-numeric cells. Keeps NULL honest:
    a missing tag must stay NULL, never become 0 (plan risk #3)."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _source_file(ticker: str, year: int, period: str) -> str:
    return f"data/XBRL/{year}/{period}/{ticker}_{year}_{period}.xbrl"


def load_emitens(path: Path):
    """Return {ticker: (nama, sektor)} from emitens.csv (best-effort)."""
    emitens = {}
    if not path.exists():
        return emitens
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row.get("ticker") or "").strip()
            if not ticker:
                continue
            emitens[ticker] = (
                (row.get("name") or row.get("nama") or "").strip() or None,
                (row.get("sector") or row.get("sektor") or "").strip() or None,
            )
    return emitens


def export(data_dir: Path, db_path: Path) -> dict:
    csv_path = data_dir / "financial_reports.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    emitens = load_emitens(data_dir / "emitens.csv")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Rebuild from scratch so the export is idempotent (no stale rows survive).
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO emitens (ticker, nama, sektor) VALUES (?, ?, ?)",
            [(t, n, s) for t, (n, s) in sorted(emitens.items())],
        )

        metric_rows = []
        rows_seen = 0
        tickers_seen = set()

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            metric_cols = [c for c in reader.fieldnames if c not in NON_METRIC_COLUMNS]

            for row in reader:
                ticker = (row.get("ticker") or "").strip()
                period = (row.get("period") or "").strip()
                try:
                    year = int((row.get("year") or "").strip())
                except ValueError:
                    continue
                if not ticker or not period:
                    continue

                rows_seen += 1
                tickers_seen.add(ticker)
                source_file = _source_file(ticker, year, period)

                for col in metric_cols:
                    value = _to_float(row.get(col))
                    if value is None:
                        continue  # keep blanks NULL, don't store a fake 0
                    metric_rows.append((ticker, year, period, col, value, source_file))

        conn.executemany(
            "INSERT OR REPLACE INTO metrics "
            "(ticker, year, period, metric, value, source_file) VALUES (?, ?, ?, ?, ?, ?)",
            metric_rows,
        )
        conn.commit()

        total, distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT ticker) FROM metrics"
        ).fetchone()
    finally:
        conn.close()

    return {
        "db": str(db_path),
        "csv_rows": rows_seen,
        "metric_rows": total,
        "distinct_tickers": distinct,
        "tickers_from_csv": sorted(tickers_seen),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export financial_reports.csv to idx.sqlite")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--db", type=Path, default=None,
                        help="Output DB path (default: <data-dir>/idx.sqlite)")
    args = parser.parse_args()

    db_path = args.db or (args.data_dir / "idx.sqlite")
    summary = export(args.data_dir, db_path)

    print(f"Wrote {summary['metric_rows']} metric rows "
          f"({summary['csv_rows']} CSV rows, "
          f"{summary['distinct_tickers']} tickers) -> {summary['db']}")


if __name__ == "__main__":
    main()
