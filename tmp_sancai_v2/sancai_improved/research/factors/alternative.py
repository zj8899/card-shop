"""Alternative data alpha factors — NLP sentiment, analyst revisions, news flow.

Implements the "另类数据" (Alternative Data) paradigm from the quant document:

  1. NewsSentimentFactor      — Keyword-based news sentiment (轻量级 NLP)
  2. AnalystRevisionFactor    — Analyst EPS/revenue revision magnitude
  3. NorthboundFlowFactor     — Northbound capital flow anomaly
  4. SocialMediaHeatFactor    — Discussion volume surge detection

All factors work with existing data_sources/* modules.  When heavier NLP
models (snowNLP, transformers) are available, the NewsSentimentFactor can
be upgraded transparently by swapping the scorer function.
"""
import logging
import re
from datetime import datetime

import numpy as np
import pandas as pd

from .base import AlphaFactor

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Keyword-based sentiment scoring (lightweight baseline)
# ═══════════════════════════════════════════════════════════════════════════════

BULLISH_KEYWORDS = [
    "突破", "大涨", "涨停", "反弹", "放量", "利好", "增持", "买入",
    "业绩预增", "超预期", "扭亏", "分红", "回购", "中标", "签约",
    "突破压力", "创新高", "政策利好", "资金流入", "主力加仓",
    "底部放量", "强势突破", "趋势向上", "多头排列", "金叉",
    "upgrade", "buy", "overweight", "outperform", "beat",
    "positive", "growth", "breakout", "rally", "surge",
]

BEARISH_KEYWORDS = [
    "暴跌", "跌停", "破位", "缩量", "利空", "减持", "卖出",
    "业绩预亏", "低于预期", "亏损", "退市", "暴雷", "违约",
    "跌破支撑", "创新低", "政策收紧", "资金流出", "主力出货",
    "顶部放量", "破位下跌", "趋势向下", "空头排列", "死叉",
    "downgrade", "sell", "underweight", "underperform", "miss",
    "negative", "decline", "breakdown", "crash", "plunge",
]

# Stock-code detection regex
STOCK_CODE_RE = re.compile(r'\b(60\d{4}|00\d{4}|30\d{4}|68\d{4})\b')


def _keyword_sentiment(text: str) -> float:
    """Simple keyword-count sentiment score in [-1, +1].

    score = (bullish_hits - bearish_hits) / (bullish_hits + bearish_hits + 1)

    This is a lightweight baseline.  Replace with snowNLP or a fine-tuned
    BERT classifier for production use.
    """
    if not text:
        return 0.0
    text_lower = text.lower()
    bull = sum(1 for kw in BULLISH_KEYWORDS if kw.lower() in text_lower)
    bear = sum(1 for kw in BEARISH_KEYWORDS if kw.lower() in text_lower)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


# Allow external injection of a better scorer
_sentiment_scorer = _keyword_sentiment


def set_sentiment_scorer(fn):
    """Replace the sentiment scoring function (e.g. with a transformer model)."""
    global _sentiment_scorer
    _sentiment_scorer = fn


# ═══════════════════════════════════════════════════════════════════════════════
# 1. News Sentiment Factor
# ═══════════════════════════════════════════════════════════════════════════════

class NewsSentimentFactor(AlphaFactor):
    """Aggregate news sentiment for a stock over a rolling window.

    For each day, collects news headlines + content from the data_sources
    cache and scores them with the current sentiment function.

    The factor value is the z-score of rolling average sentiment relative
    to its own history — capturing sentiment *regime shifts* rather than
    absolute levels.

    Data source: data_sources/wscn_news.py (华尔街见闻)
    """

    def __init__(self, window: int = 20, z_window: int = 60,
                 cache_dir: str | None = None):
        self._window = window
        self._z_window = z_window

    @property
    def name(self) -> str:
        return "news_sentiment"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        """Compute news sentiment from locally cached text data.

        Strategies by priority:
          1. If df has a 'news_sentiment' column → use it directly
          2. If df has 'news_text' column → compute sentiment on the fly
          3. Try news_event_db for recent news → attach to last bar
          4. Otherwise → return neutral (0.0) with clear logging
        """
        n = len(df)

        if "news_sentiment" in df.columns:
            raw = df["news_sentiment"].values.astype(float)
        elif "news_text" in df.columns:
            raw = np.array([_sentiment_scorer(str(t)) for t in df["news_text"].values])
        else:
            # Attempt real data from news_event_db
            raw = np.zeros(n)
            try:
                from pathlib import Path
                from server.news_event_db import query_by_symbol

                # Check if we can deduce the symbol from the DataFrame
                symbol = None
                if hasattr(df, 'attrs') and 'symbol' in df.attrs:
                    symbol = df.attrs['symbol']
                elif 'symbol' in df.columns and len(df) > 0:
                    symbol = str(df['symbol'].iloc[0])

                if symbol:
                    news_items = query_by_symbol(symbol, limit=1000)
                    if news_items:
                        # Build a date-to-sentiment map from news items
                        date_sentiments = {}
                        for ni in news_items:
                            pub_date = ni.get("published_at") or ni.get("date") or ""
                            if not pub_date:
                                continue
                            date_key = str(pub_date)[:10]  # YYYY-MM-DD
                            text = (ni.get("title", "") + " " +
                                    ni.get("content_text", "") +
                                    ni.get("content_more", ""))
                            score = _sentiment_scorer(text)
                            if date_key not in date_sentiments:
                                date_sentiments[date_key] = []
                            date_sentiments[date_key].append(score)

                        # Map to dataframe dates
                        if "date" in df.columns:
                            for i in range(n):
                                date_val = str(df["date"].iloc[i])[:10]
                                if date_val in date_sentiments:
                                    scores = date_sentiments[date_val]
                                    raw[i] = float(np.mean(scores))
                        logger.info(f"Populated news sentiment for {symbol} from news_event_db ({len(date_sentiments)} days)")
            except ImportError:
                logger.debug("news_event_db not available for news sentiment factor")
            except Exception as e:
                logger.debug(f"News sentiment from DB failed: {e}")

            if raw.sum() == 0.0:
                logger.info("No news sentiment data available (no column + no DB match); returning neutral")
                return pd.Series(np.zeros(n), index=df.index)

        # Rolling average then z-score
        rolling_avg = pd.Series(raw).rolling(self._window, min_periods=1).mean().values
        result = np.full(len(df), np.nan)

        for i in range(self._z_window, len(df)):
            mu = rolling_avg[i - self._z_window:i].mean()
            sigma = rolling_avg[i - self._z_window:i].std()
            if sigma > 0:
                result[i] = (rolling_avg[i] - mu) / sigma
            else:
                result[i] = 0.0

        return pd.Series(result, index=df.index)

    def score_news_batch(self, news_items: list[dict]) -> dict[str, float]:
        """Score a batch of news items, returning {stock_code: sentiment}.

        Call this from a daily data pipeline to populate the 'news_sentiment'
        column in each stock's parquet file.
        """
        stock_scores: dict[str, list[float]] = {}
        for item in news_items:
            text = (item.get("title", "") + " " +
                    item.get("content_text", "") +
                    item.get("content_more", ""))
            score = _sentiment_scorer(text)

            # Find stock codes in text and via symbols field
            codes_found = set(STOCK_CODE_RE.findall(text))
            for sym in item.get("symbols", []):
                if isinstance(sym, dict):
                    codes_found.add(sym.get("symbol", ""))
                elif isinstance(sym, str):
                    codes_found.add(sym)

            for code in codes_found:
                if len(code) == 6 and code.isdigit():
                    stock_scores.setdefault(code, []).append(score)

        return {code: float(np.mean(scores))
                for code, scores in stock_scores.items()}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Analyst Revision Factor
# ═══════════════════════════════════════════════════════════════════════════════

class AnalystRevisionFactor(AlphaFactor):
    """Analyst rating/EPS revision magnitude factor.

    Tracks how analysts are revising their expectations over time.
    Positive → analysts are upgrading / raising EPS estimates.
    Negative → analysts are downgrading / cutting estimates.

    Data source: data_sources/eastmoney_reports.py

    Since historical analyst data requires crawling, this factor supports
    two input modes:
      1. Pre-populated columns: 'analyst_revision' or 'avg_eps_est'
      2. Placeholder zeros (no data available)
    """

    def __init__(self, window: int = 60):
        self._window = window

    @property
    def name(self) -> str:
        return "analyst_revision"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        # Mode 1: precomputed revision score
        if "analyst_revision" in df.columns:
            raw = df["analyst_revision"].values.astype(float)
            return pd.Series(raw, index=df.index)

        # Mode 2: derive from EPS estimates
        if "avg_eps_est" in df.columns:
            eps = df["avg_eps_est"].values.astype(float)
            # Revision = 3-month change in EPS estimate, normalized
            result = np.full(n, np.nan)
            for i in range(self._window, n):
                prev = eps[i - self._window]
                curr = eps[i]
                if prev > 0 and not np.isnan(prev):
                    result[i] = (curr - prev) / prev
            return pd.Series(result, index=df.index)

        # Mode 3: placeholder
        logger.debug("No analyst data columns; returning neutral")
        return pd.Series(np.zeros(n), index=df.index)

    @staticmethod
    def extract_revision_from_reports(reports: list[dict]) -> dict[str, dict]:
        """Extract revision signals from Eastmoney report records.

        Returns {stock_code: {rating, eps_this_year, eps_next_year, org_count}}.
        """
        stock_data: dict[str, dict] = {}
        for r in reports:
            code = str(r.get("stockCode", "") or r.get("code", ""))
            if not code or len(code) != 6:
                continue

            if code not in stock_data:
                stock_data[code] = {
                    "eps_estimates": [],
                    "ratings": [],
                    "orgs": set(),
                }

            eps_this = r.get("predictThisYearEps")
            if eps_this and float(eps_this) > 0:
                stock_data[code]["eps_estimates"].append(float(eps_this))

            rating = r.get("emRatingName", "") or r.get("ratingName", "")
            if rating:
                stock_data[code]["ratings"].append(rating)

            org = r.get("orgSName", "")
            if org:
                stock_data[code]["orgs"].add(org)

        result = {}
        for code, data in stock_data.items():
            result[code] = {
                "avg_eps_est": float(np.mean(data["eps_estimates"])) if data["eps_estimates"] else 0.0,
                "eps_std": float(np.std(data["eps_estimates"])) if len(data["eps_estimates"]) > 1 else 0.0,
                "n_estimates": len(data["eps_estimates"]),
                "n_orgs": len(data["orgs"]),
                "ratings": data["ratings"],
            }
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Northbound Flow Factor
# ═══════════════════════════════════════════════════════════════════════════════

class NorthboundFlowFactor(AlphaFactor):
    """Northbound capital flow anomaly factor.

    Measures whether recent northbound flow deviates significantly from
    its own history.  Large positive → smart money is aggressively buying.
    Large negative → smart money is fleeing.

    Data source: data_sources/northbound.py
    """

    def __init__(self, short_window: int = 5, long_window: int = 60):
        self._short = short_window
        self._long = long_window

    @property
    def name(self) -> str:
        return "northbound_flow"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        n = len(df)

        if "northbound_flow" in df.columns:
            flow = df["northbound_flow"].values.astype(float)
        else:
            # Attempt real data from northbound source
            flow = np.zeros(n)
            try:
                from data_sources.northbound import get_northbound_history

                symbol = None
                if hasattr(df, 'attrs') and 'symbol' in df.attrs:
                    symbol = df.attrs['symbol']
                elif 'symbol' in df.columns:
                    symbol = str(df['symbol'].iloc[0])

                if symbol and "date" in df.columns:
                    hist = get_northbound_history(symbol)
                    if hist:
                        # Build date-to-flow map
                        date_flows = {}
                        for entry in hist:
                            d = str(entry.get("date", ""))[:10]
                            amt = float(entry.get("net_flow", 0) or 0)
                            if d:
                                date_flows[d] = date_flows.get(d, 0.0) + amt
                        for i in range(n):
                            date_val = str(df["date"].iloc[i])[:10]
                            flow[i] = date_flows.get(date_val, 0.0)
                        logger.info(f"Populated northbound flow for {symbol} from data source ({len(date_flows)} days)")
            except ImportError:
                logger.debug("Northbound data source not available")
            except Exception as e:
                logger.debug(f"Northbound flow from source failed: {e}")

            if flow.sum() == 0.0:
                logger.debug("No northbound_flow column or source data; returning neutral")
                return pd.Series(np.zeros(n), index=df.index)

        short_ma = pd.Series(flow).rolling(self._short).mean().values
        long_ma = pd.Series(flow).rolling(self._long).mean().values
        long_std = pd.Series(flow).rolling(self._long).std().values

        result = np.full(n, np.nan)
        for i in range(self._long, n):
            if long_std[i] > 0:
                result[i] = (short_ma[i] - long_ma[i]) / long_std[i]
            else:
                result[i] = 0.0

        return pd.Series(result, index=df.index)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Limit-Up Heat Factor (涨停板热度)
# ═══════════════════════════════════════════════════════════════════════════════

class LimitUpHeatFactor(AlphaFactor):
    """Market-wide limit-up board heat as a sentiment indicator.

    When many stocks hit limit-up consecutively, it signals extreme
    speculative fervor (greed).  When none do, it signals fear.

    This is a *market-level* factor — it has the same value for all
    stocks on a given day.

    High positive → speculative bubble forming (caution).
    High negative → extreme fear (contrarian opportunity).
    """

    def __init__(self, window: int = 10):
        self._window = window

    @property
    def name(self) -> str:
        return "limit_up_heat"

    def calculate(self, df: pd.DataFrame) -> pd.Series:
        if "limit_up_count" not in df.columns:
            logger.debug("No limit_up_count column; returning neutral")
            return pd.Series(np.zeros(len(df)), index=df.index)

        count = df["limit_up_count"].values.astype(float)
        result = np.full(len(df), np.nan)

        for i in range(self._window, len(df)):
            window_vals = count[i - self._window:i]
            mu = window_vals.mean()
            sigma = window_vals.std()
            if sigma > 0:
                result[i] = (count[i] - mu) / sigma
            else:
                result[i] = 0.0

        return pd.Series(result, index=df.index)
