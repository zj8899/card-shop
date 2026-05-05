"""
A-share data downloader using akshare.
Downloads daily and minute K-line data for configured stock universe.
"""
import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "raw"
CONFIG_PATH = PROJECT_ROOT / "config" / "defaults.yaml"
META_DB = PROJECT_ROOT / "data" / "metadata.db"


def load_universe() -> list[dict]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("universe", [])


def get_meta_conn() -> sqlite3.Connection:
    """Get or create metadata database."""
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


def update_metadata(conn: sqlite3.Connection, symbol: str, name: str,
                    period: str, start_date: str, end_date: str,
                    row_count: int, file_path: str):
    conn.execute("""
        INSERT OR REPLACE INTO data_catalog (symbol, name, period, start_date, end_date, row_count, last_updated, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, name, period, start_date, end_date, row_count,
          datetime.now().isoformat(), file_path))
    conn.commit()


def download_daily(symbol: str, name: str, conn: sqlite3.Connection,
                   start_date: str = "20200101", end_date: str = None):
    """Download daily K-line data via akshare."""
    try:
        import akshare as ak

        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        logger.info(f"Downloading daily data for {symbol} {name}...")

        # akshare stock daily history
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq"  # forward-adjusted
        )

        if df is None or df.empty:
            logger.warning(f"No daily data returned for {symbol}")
            return False

        # Standardize column names
        col_map = {
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
            "成交额": "amount", "振幅": "amplitude", "涨跌幅": "pct_change",
            "涨跌额": "change", "换手率": "turnover"
        }
        df = df.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = symbol
        df["name"] = name

        # Save as parquet
        out_dir = DATA_DIR / "daily"
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / f"{symbol}.parquet"

        # Merge with existing data if available
        if file_path.exists():
            existing = pd.read_parquet(file_path)
            existing["date"] = pd.to_datetime(existing["date"])
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["date"], keep="last")
            df = df.sort_values("date")

        df.to_parquet(file_path, index=False)

        # Update metadata
        update_metadata(
            conn, symbol, name, "daily",
            df["date"].min().strftime("%Y-%m-%d"),
            df["date"].max().strftime("%Y-%m-%d"),
            len(df),
            str(file_path)
        )

        logger.info(f"  -> {len(df)} daily bars saved to {file_path}")
        return True

    except Exception as e:
        logger.error(f"Failed to download daily data for {symbol}: {e}")
        return False


def download_minute(symbol: str, name: str, conn: sqlite3.Connection,
                    period: str = "30", start_date: str = "20240101"):
    """Download minute K-line data via akshare."""
    try:
        import akshare as ak

        logger.info(f"Downloading {period}min data for {symbol} {name}...")

        # akshare stock minute history
        df = ak.stock_zh_a_hist_min_em(
            symbol=symbol,
            period=period,
            start_date=start_date,
            end_date=datetime.now().strftime("%Y%m%d"),
            adjust="qfq"
        )

        if df is None or df.empty:
            logger.warning(f"No {period}min data returned for {symbol}")
            return False

        col_map = {
            "时间": "datetime", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
            "成交额": "amount"
        }
        df = df.rename(columns=col_map)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["symbol"] = symbol
        df["name"] = name

        period_name = f"{period}min"
        out_dir = DATA_DIR / "minute" / period_name
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / f"{symbol}.parquet"

        if file_path.exists():
            existing = pd.read_parquet(file_path)
            existing["datetime"] = pd.to_datetime(existing["datetime"])
            df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["datetime"], keep="last")
            df = df.sort_values("datetime")

        df.to_parquet(file_path, index=False)

        update_metadata(
            conn, symbol, name, period_name,
            df["datetime"].min().strftime("%Y-%m-%d %H:%M"),
            df["datetime"].max().strftime("%Y-%m-%d %H:%M"),
            len(df),
            str(file_path)
        )

        logger.info(f"  -> {len(df)} {period_name} bars saved")
        return True

    except Exception as e:
        logger.error(f"Failed to download {period}min data for {symbol}: {e}")
        return False


def download_all(periods: list[str] = None, start_date: str = "20240101",
                 limit_symbols: list[str] = None):
    """Download data for all stocks in universe."""
    universe = load_universe()
    conn = get_meta_conn()

    if limit_symbols:
        universe = [s for s in universe if s["symbol"] in limit_symbols]

    if periods is None:
        periods = ["daily", "30", "60"]

    total_success = 0
    total_fail = 0

    for stock in universe:
        symbol = stock["symbol"]
        name = stock["name"]

        for period in periods:
            if period == "daily":
                ok = download_daily(symbol, name, conn, start_date=start_date)
            else:
                ok = download_minute(symbol, name, conn, period=period, start_date=start_date)

            if ok:
                total_success += 1
            else:
                total_fail += 1

            time.sleep(0.5)  # Rate limiting

    conn.close()
    logger.info(f"Download complete. Success: {total_success}, Failed: {total_fail}")


def check_gaps(conn: sqlite3.Connection = None):
    """Check for data gaps in all stored series."""
    if conn is None:
        conn = get_meta_conn()

    cursor = conn.execute(
        "SELECT symbol, name, period, start_date, end_date, row_count FROM data_catalog"
    )
    gaps = []
    for row in cursor:
        symbol, name, period, start, end, count = row
        gaps.append({
            "symbol": symbol,
            "name": name,
            "period": period,
            "start_date": start,
            "end_date": end,
            "row_count": count,
        })

    conn.close()
    return gaps


def main():
    parser = argparse.ArgumentParser(description="Download A-share K-line data")
    parser.add_argument("--universe", help="Path to universe config (optional)")
    parser.add_argument("--symbols", nargs="*", help="Limit to specific symbols")
    parser.add_argument("--periods", nargs="*", default=["daily", "30", "60"],
                        help="Periods to download (daily, 30, 60, 15, 5, 1)")
    parser.add_argument("--start", default="20240101", help="Start date YYYYMMDD")
    parser.add_argument("--check", action="store_true", help="Only check for gaps")
    args = parser.parse_args()

    if args.check:
        gaps = check_gaps()
        print(json.dumps(gaps, ensure_ascii=False, indent=2))
        return

    download_all(periods=args.periods, start_date=args.start,
                 limit_symbols=args.symbols)


if __name__ == "__main__":
    main()
