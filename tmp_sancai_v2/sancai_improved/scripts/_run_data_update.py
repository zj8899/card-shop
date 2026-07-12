"""Thin CLI wrapper for data download — called from server data download endpoint.

Usage: python _run_data_update.py <period> [max_symbols]

Prints a single JSON line to stdout and exits 0 on success, 1 on failure.

Data sources:
  - daily: Tencent/EastMoney HTTP (no V8/JS engine needed)
  - minute: akshare EastMoney API (no V8/JS engine needed)
"""
import json
import sys
import traceback
from pathlib import Path

# Ensure the project root is on sys.path so `scripts.*` imports work
# regardless of where the subprocess is launched from.
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "missing period argument"}))
        sys.exit(1)

    period = sys.argv[1]
    max_symbols = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    try:
        import logging
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

        if period == "daily":
            result = _update_daily(max_symbols)
        else:
            result = _update_minute(period, max_symbols)

        # Only emit a single clean JSON line so the caller can parse it
        print(json.dumps({
            "symbols_checked": result.get("symbols_checked", 0),
            "symbols_updated": result.get("symbols_updated", 0),
            "bars_added": result.get("bars_added", 0),
            "errors": result.get("errors", 0),
        }, ensure_ascii=False))
        sys.exit(0)

    except Exception:
        # Last-resort error message — single JSON line on stdout
        print(json.dumps({"error": traceback.format_exc()}, ensure_ascii=False))
        sys.exit(1)


def _update_daily(max_symbols: int) -> dict:
    """Update daily data via Tencent/EastMoney HTTP (no py_mini_racer needed).

    This is an INCREMENTAL update — only symbols with tail gaps (missing recent data)
    are downloaded. Stocks already up-to-date through the latest business day are skipped.
    To force a full re-download, delete the parquet files first.
    """
    from scripts.update_daily_tencent import update_all, DAILY_DIR

    symbols = sorted(f.stem for f in DAILY_DIR.glob("*.parquet")
                    if not f.stem.startswith(("bj", "92")))

    # Respect max_symbols cap (0 = unlimited)
    if max_symbols > 0 and max_symbols < len(symbols):
        import random
        random.shuffle(symbols)
        symbols = symbols[:max_symbols]
        symbols.sort()

    result = update_all(symbols=symbols, days=10, max_workers=2, delay=0.0)
    return {
        "symbols_checked": result.get("symbols_total", 0),
        "symbols_updated": result.get("symbols_updated", 0),
        "bars_added": result.get("bars_added", 0),
        "errors": result.get("errors", 0),
    }


def _update_minute(period: str, max_symbols: int) -> dict:
    """Update minute data via akshare EastMoney (no py_mini_racer needed)."""
    from scripts.data_update import run_tail_update
    return run_tail_update(period=period, max_symbols=max_symbols, dry_run=False, delay=0.4)


if __name__ == "__main__":
    main()
