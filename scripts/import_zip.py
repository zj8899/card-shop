"""
ZIP file import utility for backtest data.
Supports importing CSV/Parquet files from ZIP archives
and merging them into the local data store with deduplication.
"""
import argparse
import logging
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"
META_DB = PROJECT_ROOT / "data" / "metadata.db"


def get_meta_conn() -> sqlite3.Connection:
    META_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(META_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_catalog (
            symbol TEXT NOT NULL,
            name TEXT,
            period TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            row_count INTEGER,
            last_updated TEXT,
            file_path TEXT,
            PRIMARY KEY (symbol, period)
        )
    """)
    conn.commit()
    return conn


REQUIRED_DAILY_COLS = {"date", "open", "high", "low", "close", "volume"}
REQUIRED_MINUTE_COLS = {"datetime", "open", "high", "low", "close", "volume"}


def detect_period(filename: str) -> str:
    """Detect data period from filename."""
    fname_lower = filename.lower()
    if "daily" in fname_lower or "day" in fname_lower or "日线" in filename:
        return "daily"
    for p in ["1min", "5min", "15min", "30min", "60min", "120min"]:
        if p in fname_lower:
            return p
    # Default to daily
    return "daily"


def detect_symbol(filename: str) -> str:
    """Try to extract symbol from filename. Expects symbol in name like '000001_daily.csv'."""
    import re
    match = re.match(r"(\d{6})", filename)
    if match:
        return match.group(1)
    return filename.rsplit(".", 1)[0]


def merge_into_store(df: pd.DataFrame, symbol: str, period: str,
                     conn: sqlite3.Connection) -> bool:
    """Merge dataframe into local data store."""
    # Determine date column
    date_col = "date" if period == "daily" else "datetime"

    if date_col not in df.columns:
        logger.error(f"Missing date column '{date_col}' in data for {symbol}")
        return False

    # Ensure symbol column exists
    if "symbol" not in df.columns:
        df["symbol"] = symbol

    # Determine output path
    if period == "daily":
        out_dir = DATA_DIR / "daily"
    else:
        out_dir = DATA_DIR / "minute" / period
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"{symbol}.parquet"

    # Merge with existing data
    if file_path.exists():
        existing = pd.read_parquet(file_path)
        existing[date_col] = pd.to_datetime(existing[date_col])
        df[date_col] = pd.to_datetime(df[date_col])
        df = pd.concat([existing, df], ignore_index=True)
        df = df.drop_duplicates(subset=[date_col], keep="last")
        df = df.sort_values(date_col)

    df.to_parquet(file_path, index=False)

    # Update metadata
    conn.execute("""
        INSERT OR REPLACE INTO data_catalog
        (symbol, name, period, start_date, end_date, row_count, last_updated, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol, symbol, period,
        df[date_col].min().strftime("%Y-%m-%d"),
        df[date_col].max().strftime("%Y-%m-%d"),
        len(df),
        datetime.now().isoformat(),
        str(file_path)
    ))
    conn.commit()

    logger.info(f"Merged {len(df)} rows for {symbol} ({period}) -> {file_path}")
    return True


def import_zip(zip_path: str) -> dict:
    """Import data from ZIP file."""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        logger.error(f"ZIP file not found: {zip_path}")
        return {"success": False, "imported": 0, "errors": ["File not found"]}

    conn = get_meta_conn()
    imported = 0
    errors = []

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir)

        tmp = Path(tmpdir)
        data_files = list(tmp.rglob("*.csv")) + list(tmp.rglob("*.parquet"))

        logger.info(f"Found {len(data_files)} data files in ZIP")

        for fpath in data_files:
            try:
                fname = fpath.name
                symbol = detect_symbol(fname)
                period = detect_period(fname)

                if fpath.suffix == ".parquet":
                    df = pd.read_parquet(fpath)
                else:
                    df = pd.read_csv(fpath)

                if df.empty:
                    errors.append(f"Empty file: {fname}")
                    continue

                # Validate columns
                cols = set(df.columns.str.lower())
                required = REQUIRED_DAILY_COLS if period == "daily" else REQUIRED_MINUTE_COLS
                if not required.issubset(cols):
                    errors.append(f"Missing columns in {fname}: {required - cols}")
                    continue

                if merge_into_store(df, symbol, period, conn):
                    imported += 1

            except Exception as e:
                errors.append(f"Error importing {fpath.name}: {e}")

    conn.close()
    result = {"success": len(errors) == 0, "imported": imported, "errors": errors}
    logger.info(f"ZIP import complete: {result}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Import backtest data from ZIP")
    parser.add_argument("zip_file", help="Path to ZIP file containing CSV/Parquet data")
    args = parser.parse_args()
    result = import_zip(args.zip_file)
    print(f"Imported: {result['imported']} files")
    if result["errors"]:
        print("Errors:")
        for e in result["errors"]:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
