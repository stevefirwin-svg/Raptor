"""
Raptor v5.0 — Data Feeds
=========================
Three data sources, unified interface:
  1. Alpaca Markets  — Daily OHLCV bars, account data, order execution
  2. FRED API        — Macro economic series (yield curve, VIX, claims, etc.)
  3. Alpaca News     — Symbol-level news sentiment via Alpaca's news API

All feeds cache aggressively to avoid redundant API calls.
"""

import json
import os
import time
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce

from config import RaptorConfig

logger = logging.getLogger("raptor.data_feeds")


class AlpacaDataError(Exception):
    """Raised when Alpaca returns None/unparseable data for a CAPITAL- or
    POSITION-CRITICAL field (equity, cash, buying_power, portfolio_value,
    qty, avg_entry_price, current_price).

    DESIGN (2026-07-06, "real money" hardening pass): the 2026-07-06
    daytrade_count crash was first patched by defaulting EVERY get_account()/
    get_positions() field to 0.0/0 on None (safe_float/safe_int below). That
    was correct for daytrade_count (informational only, feeds no sizing or
    risk decision) but WRONG for equity/cash/buying_power/portfolio_value/
    qty/avg_entry_price/current_price — those feed position sizing, margin
    checks, and exit order quantities. Silently defaulting equity to 0.0
    is itself a fabricated value that *looks real* (a valid float depended on
    downstream) — exactly what RAPTOR_MASTER_PLAN.md's own "Data integrity
    corollary" prohibits ("Real data or skip. Never fabricate a fallback value
    that looks real"). A caller that reads equity=0.0 has no way to tell
    "the account really has $0" from "Alpaca didn't return this field."

    So critical fields now RAISE instead of defaulting. This is not new
    exception-throwing behavior — every caller of get_account()/
    get_positions() already wraps the call in a try/except with fail-closed
    handling (margin_guard.py explicitly: "Fail CLOSED... previous behavior
    was a silent capital risk"; main.py/exit_monitor.py/hold_monitor.py's
    __main__ blocks log FATAL and abort the cycle; raptor_monitor.py catches
    per-call since S14-1) — a raise here is caught the same way an opaque
    TypeError already was today, just with a clear, typed, well-labeled error
    naming the exact field instead of an arbitrary conversion crashing
    wherever it happens to land. Skip the cycle rather than trade on a
    fabricated number.
    """
    pass


def require_float(value, field: str) -> float:
    """Coerce to float for a CRITICAL field — raise AlpacaDataError on
    None/unparseable rather than defaulting. Use only for fields where a
    silently-fabricated number could feed a sizing, margin, or order-quantity
    decision. See AlpacaDataError's docstring for the full rationale.
    """
    if value is None:
        raise AlpacaDataError(f"{field} was None — Alpaca did not return this critical field")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise AlpacaDataError(f"{field}={value!r} is not a valid number")


def safe_int(value, default: int = 0, field: str = "") -> int:
    """Coerce to int, treating None/missing/unparseable as `default`.

    Alpaca's TradeAccount.daytrade_count is None for account types that
    don't track PDT status (was seen live 2026-07-06, crashed daily_recap.py
    with TypeError: int() argument ... not 'NoneType'). Mirrors the
    safe_float() pattern already used in raptor_monitor.py for the same
    None-but-present-key failure class.
    """
    if value is None:
        if field:
            logger.warning("safe_int: %s was None, defaulting to %s", field, default)
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("safe_int: %s=%r unparseable, defaulting to %s", field or "value", value, default)
        return default


def safe_float(value, default: float = 0.0, field: str = "") -> float:
    """Coerce to float, treating None/missing/unparseable as `default`.

    Same rationale as safe_int() above — Alpaca account/position fields can
    come back None for edge-case account states, and a bare float(None) call
    takes down the whole recap/monitor run instead of just that one field.
    Logs a warning (not silent) whenever it substitutes, per S10-3's "no
    silent fallback" convention.
    """
    if value is None:
        if field:
            logger.warning("safe_float: %s was None, defaulting to %s", field, default)
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("safe_float: %s=%r unparseable, defaulting to %s", field or "value", value, default)
        return default


# ═══════════════════════════════════════════════
# 1. ALPACA MARKET DATA
# ═══════════════════════════════════════════════

class AlpacaDataFeed:
    """Daily OHLCV bars + account/order management via Alpaca SDK."""

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self.data_client = StockHistoricalDataClient(
            api_key=cfg.alpaca.api_key,
            secret_key=cfg.alpaca.secret_key,
        )
        self.trading_client = TradingClient(
            api_key=cfg.alpaca.api_key,
            secret_key=cfg.alpaca.secret_key,
            paper=cfg.execution.paper_trading,
        )
        self._bar_cache: Dict[str, pd.DataFrame] = {}
        self._cache_timestamp: Optional[float] = None
        self._cache_ttl = 300  # 5 min cache for bars

    def get_daily_bars(
        self,
        symbols: List[str],
        lookback_days: int = 60,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch daily OHLCV bars for a list of symbols.
        Returns {symbol: DataFrame} with columns: open, high, low, close, volume, vwap
        """
        now = time.time()
        cache_valid = bool(self._cache_timestamp and (now - self._cache_timestamp) < self._cache_ttl)

        if cache_valid and all(s in self._bar_cache for s in symbols):
            logger.debug("Returning cached bars for %d symbols", len(symbols))
            return {s: self._bar_cache[s] for s in symbols}

        # Fetch only symbols missing from cache; on expiry clear and re-fetch all
        to_fetch = [s for s in symbols if s not in self._bar_cache] if cache_valid else list(symbols)
        if not cache_valid:
            self._bar_cache.clear()

        end = end_date or datetime.now()
        start = end - timedelta(days=lookback_days + 10)  # Buffer for weekends

        # Alpaca SDK limits to 200 symbols per request
        results = {}
        for i in range(0, len(to_fetch), 200):
            batch = to_fetch[i : i + 200]
            request = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=self.cfg.alpaca.feed,
            )
            try:
                bars = self.data_client.get_stock_bars(request)
                for symbol in batch:
                    if symbol in bars.data:
                        rows = []
                        for bar in bars.data[symbol]:
                            rows.append({
                                "timestamp": bar.timestamp,
                                "open": float(bar.open),
                                "high": float(bar.high),
                                "low": float(bar.low),
                                "close": float(bar.close),
                                "volume": int(bar.volume),
                                "vwap": float(bar.vwap) if bar.vwap else np.nan,
                            })
                        df = pd.DataFrame(rows)
                        df["timestamp"] = pd.to_datetime(df["timestamp"])
                        df = df.set_index("timestamp").sort_index()
                        if len(df) >= 20:  # Need minimum history
                            results[symbol] = df
                            self._bar_cache[symbol] = df
            except Exception as e:
                logger.error("Alpaca bar fetch failed for batch %d: %s", i, e)

        self._cache_timestamp = time.time()
        logger.info("Fetched %d new bars / %d total cached", len(results),
                    sum(1 for s in symbols if s in self._bar_cache))
        return {s: self._bar_cache[s] for s in symbols if s in self._bar_cache}

    def get_account(self) -> Dict[str, Any]:
        """Return account summary: equity, cash, buying power.

        equity/cash/buying_power/portfolio_value are CAPITAL-CRITICAL — feed
        position sizing (main.py my_equity), margin checks (margin_guard.py),
        and cap-headroom math (daily_recap.py). Raise via require_float()
        rather than default; see AlpacaDataError. day_trade_count is
        informational only (PDT compliance display) — safe to default.
        """
        acct = self.trading_client.get_account()
        return {
            "equity": require_float(acct.equity, "account.equity"),
            "cash": require_float(acct.cash, "account.cash"),
            "buying_power": require_float(acct.buying_power, "account.buying_power"),
            "portfolio_value": require_float(acct.portfolio_value, "account.portfolio_value"),
            "day_trade_count": safe_int(acct.daytrade_count, field="account.daytrade_count"),
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return current open positions.

        qty/avg_entry/current_price are POSITION-CRITICAL — qty feeds exit
        order size and duplicate-entry detection (main.py `all_held`),
        current_price*qty feeds market_value/margin/capital-utilization math.
        A silently-defaulted qty=0.0 could make a real position invisible to
        sizing/exposure checks (over-allocation risk) or to exit_monitor
        (missed stop-loss risk) — worse than raising and skipping the whole
        cycle. Raises AlpacaDataError (whole call, not just one position) if
        any position has a bad critical field, same fail-closed pattern as
        get_account(). unrealized_pnl/unrealized_pnl_pct are informational/
        analytics (hold_monitor health scoring self-corrects next cycle) —
        safe to default.
        """
        positions = self.trading_client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "qty": require_float(p.qty, f"{p.symbol}.qty"),
                "avg_entry": require_float(p.avg_entry_price, f"{p.symbol}.avg_entry_price"),
                "current_price": require_float(p.current_price, f"{p.symbol}.current_price"),
                "unrealized_pnl": safe_float(p.unrealized_pl, field=f"{p.symbol}.unrealized_pl"),
                "unrealized_pnl_pct": safe_float(p.unrealized_plpc, field=f"{p.symbol}.unrealized_plpc"),
                "side": str(p.side),
            }
            for p in positions
        ]

    def get_portfolio_history(self, period: str = "1A", timeframe: str = "1D"):
        """
        Return the account equity time series (broker truth).

        Alpaca's portfolio history is mark-to-market and already reflects every
        closed position — when a position is sold, the realized result stays in
        the equity curve. So equity change over any window = total PnL (realized
        + unrealized) over that window. This is the correct source for windowed
        account PnL and inherently 'syncs' positions already sold.

        Returns a dict:
            {
              "dates":  [datetime, ...],     # one per daily point
              "equity": [float, ...],        # account equity at each point
              "base_value": float,           # equity at the start of the window
            }
        or None on any failure (caller must skip gracefully — never fabricate).
        """
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest
            req = GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
            hist = self.trading_client.get_portfolio_history(req)

            ts = list(getattr(hist, "timestamp", []) or [])
            eq = list(getattr(hist, "equity", []) or [])
            if not ts or not eq or len(ts) != len(eq):
                return None

            dates, equity = [], []
            for t, e in zip(ts, eq):
                if e is None:
                    continue
                # timestamps are epoch seconds (int) or datetime depending on SDK
                if isinstance(t, (int, float)):
                    d = datetime.fromtimestamp(t)
                else:
                    d = t
                dates.append(d)
                equity.append(float(e))

            if len(equity) < 2:
                return None

            base_value = float(getattr(hist, "base_value", equity[0]) or equity[0])
            return {"dates": dates, "equity": equity, "base_value": base_value}
        except Exception as e:
            logger.warning("Portfolio history error: %s", e)
            return None

    def submit_order(self, symbol: str, qty, side: str, order_type: str = "market",
                     limit_price: float = None, client_order_id: str = None) -> Dict[str, Any]:
        """Submit a buy or sell order. Returns order confirmation dict."""
        order_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL

        if order_type == "limit" and limit_price:
            request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=client_order_id,
            )
        else:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )

        try:
            order = self.trading_client.submit_order(request)
            result = {
                "id": str(order.id),
                "symbol": order.symbol,
                "qty": str(order.qty),
                "side": str(order.side),
                "type": str(order.type),
                "status": str(order.status),
                "submitted_at": str(order.submitted_at),
            }
            logger.info("Order submitted: %s %s %s @ %s", side, qty, symbol, limit_price)
            return result
        except Exception as e:
            logger.error("Order submission failed: %s", e)
            return {"error": str(e)}


# ═══════════════════════════════════════════════
# 2. FRED MACRO DATA
# ═══════════════════════════════════════════════

class FREDDataFeed:
    """
    Pull macro economic data from FRED.
    Computes a composite macro regime score from multiple series.
    """

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self._cache: Dict[str, Tuple[pd.Series, float]] = {}  # series_id -> (data, timestamp)
        self._cache_dir = os.path.join("cache", "fred")
        os.makedirs(self._cache_dir, exist_ok=True)

    def _fetch_series(
        self,
        series_id: str,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
    ) -> pd.Series:
        """Fetch a single FRED series. Returns pd.Series indexed by date."""
        # Check memory cache
        if series_id in self._cache:
            data, ts = self._cache[series_id]
            if (time.time() - ts) < self.cfg.fred.cache_hours * 3600:
                return data

        # Check disk cache
        cache_file = os.path.join(self._cache_dir, f"{series_id}.json")
        if os.path.exists(cache_file):
            age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
            if age_hours < self.cfg.fred.cache_hours:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                series = pd.Series(
                    cached["values"],
                    index=pd.to_datetime(cached["dates"]),
                    dtype=float,
                )
                self._cache[series_id] = (series, time.time())
                return series

        # Fetch from API
        if not observation_start:
            observation_start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
        if not observation_end:
            observation_end = datetime.now().strftime("%Y-%m-%d")

        params = {
            "series_id": series_id,
            "api_key": self.cfg.fred.api_key,
            "file_type": "json",
            "observation_start": observation_start,
            "observation_end": observation_end,
            "sort_order": "asc",
        }

        try:
            resp = requests.get(self.cfg.fred.base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            dates, values = [], []
            for obs in data.get("observations", []):
                if obs["value"] != ".":  # FRED uses "." for missing
                    dates.append(obs["date"])
                    values.append(float(obs["value"]))

            series = pd.Series(values, index=pd.to_datetime(dates), dtype=float)

            # Write disk cache
            with open(cache_file, "w") as f:
                json.dump({"dates": dates, "values": values}, f)

            self._cache[series_id] = (series, time.time())
            logger.info("FRED fetch OK: %s (%d observations)", series_id, len(series))
            return series

        except Exception as e:
            # FIX (2026-06-29, audit P1): requests embeds the full request URL
            # (including the api_key query param, in plaintext) in the string
            # representation of HTTPError/ConnectionError/Timeout. Logging `e`
            # directly was writing the live FRED API key to logs/ on every
            # failed fetch. Redact the key before it ever reaches the logger.
            safe_err = self._redact_api_key(str(e))
            logger.error("FRED fetch failed for %s: %s", series_id, safe_err)
            return pd.Series(dtype=float)

    def _redact_api_key(self, text: str) -> str:
        """Strip this feed's FRED api_key out of an error/URL string before logging."""
        key = getattr(self.cfg.fred, "api_key", None)
        if key:
            text = text.replace(key, "***REDACTED***")
        return text

    def get_macro_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """
        Fetch all configured FRED series and return latest values + z-scores.
        Returns: {label: {value, z_score, direction, series_id}}
        """
        snapshot = {}
        for label, series_id in self.cfg.fred.series.items():
            series = self._fetch_series(series_id)
            if len(series) < 10:
                continue

            latest = series.iloc[-1]
            mean = series.iloc[-252:].mean() if len(series) >= 252 else series.mean()
            std = series.iloc[-252:].std() if len(series) >= 252 else series.std()
            z_score = (latest - mean) / std if std > 0 else 0.0

            # Direction: 1-month change
            if len(series) >= 22:
                direction = "rising" if latest > series.iloc[-22] else "falling"
            else:
                direction = "flat"

            snapshot[label] = {
                "value": round(latest, 4),
                "z_score": round(z_score, 3),
                "direction": direction,
                "series_id": series_id,
                "last_updated": str(series.index[-1].date()),
            }

        return snapshot

    def compute_regime_score(self) -> Dict[str, Any]:
        """
        Composite macro regime score: -1.0 (crisis) to +1.0 (expansion).

        Scoring logic:
          - Yield curve < 0        → bearish signal
          - VIX z-score > 1.5      → fear signal
          - Initial claims rising   → labor weakness
          - Credit spread widening  → stress signal
          - PMI < 50               → contraction signal
        """
        snapshot = self.get_macro_snapshot()
        if not snapshot:
            return {"score": 0.0, "regime": "UNKNOWN", "detail": {}}

        score = 0.0
        signals = {}

        # Yield curve (inverted = bad)
        if "yield_curve_10y2y" in snapshot:
            yc = snapshot["yield_curve_10y2y"]["value"]
            if yc < 0:
                score -= 0.25
                signals["yield_curve"] = "INVERTED"
            elif yc < 0.5:
                score -= 0.10
                signals["yield_curve"] = "FLAT"
            else:
                score += 0.15
                signals["yield_curve"] = "NORMAL"

        # VIX (elevated = bad)
        if "vix" in snapshot:
            vix_z = snapshot["vix"]["z_score"]
            if vix_z > 2.0:
                score -= 0.25
                signals["vix"] = "EXTREME_FEAR"
            elif vix_z > 1.0:
                score -= 0.15
                signals["vix"] = "ELEVATED"
            elif vix_z < -0.5:
                score += 0.15
                signals["vix"] = "COMPLACENT"
            else:
                score += 0.05
                signals["vix"] = "NORMAL"

        # Initial claims (rising = bad)
        if "initial_claims" in snapshot:
            ic = snapshot["initial_claims"]
            if ic["direction"] == "rising" and ic["z_score"] > 1.0:
                score -= 0.20
                signals["claims"] = "DETERIORATING"
            elif ic["direction"] == "falling":
                score += 0.10
                signals["claims"] = "IMPROVING"
            else:
                signals["claims"] = "STABLE"

        # Credit spread (widening = bad)
        if "credit_spread" in snapshot:
            cs_z = snapshot["credit_spread"]["z_score"]
            if cs_z > 1.5:
                score -= 0.20
                signals["credit"] = "STRESS"
            elif cs_z < -0.5:
                score += 0.10
                signals["credit"] = "TIGHT"
            else:
                signals["credit"] = "NORMAL"

        # Manufacturing employment (proxy for PMI — uses z-score and direction)
        if "ism_pmi" in snapshot:
            mfg = snapshot["ism_pmi"]
            if mfg["direction"] == "falling" and mfg["z_score"] < -1.0:
                score -= 0.20
                signals["manufacturing"] = "DECLINING"
            elif mfg["direction"] == "falling":
                score -= 0.05
                signals["manufacturing"] = "SOFTENING"
            elif mfg["direction"] == "rising" and mfg["z_score"] > 0.5:
                score += 0.15
                signals["manufacturing"] = "EXPANDING"
            else:
                score += 0.05
                signals["manufacturing"] = "STABLE"

        # Clamp to [-1, 1]
        score = max(-1.0, min(1.0, score))

        # Classify regime
        if score <= -0.40:
            regime = "CRISIS"
        elif score <= -0.15:
            regime = "BEARISH"
        elif score <= 0.15:
            regime = "NEUTRAL"
        elif score <= 0.40:
            regime = "BULLISH"
        else:
            regime = "EXPANSION"

        result = {
            "score": round(score, 3),
            "regime": regime,
            "detail": signals,
            "snapshot": snapshot,
        }
        logger.info("FRED regime: %s (score=%.3f)", regime, score)
        return result


# ═══════════════════════════════════════════════
# 3. ALPACA NEWS SENTIMENT
# ═══════════════════════════════════════════════

class AlpacaNewsFeed:
    """
    Fetch news articles via Alpaca News API and compute sentiment scores.
    Uses simple keyword-based sentiment (no external NLP dependency).
    Upgrade path: swap in FinBERT or a transformer model later.
    """

    # Curated financial sentiment lexicon
    POSITIVE_WORDS = {
        "upgrade", "beat", "beats", "exceeded", "surpassed", "record",
        "growth", "profit", "gains", "bullish", "outperform", "strong",
        "rally", "breakout", "momentum", "raised", "boost", "positive",
        "expansion", "innovative", "soaring", "dividend", "buyback",
        "upside", "optimistic", "recovery", "accelerate", "robust",
    }

    NEGATIVE_WORDS = {
        "downgrade", "miss", "missed", "fell", "decline", "loss",
        "bearish", "weak", "warning", "risk", "crash", "selloff",
        "underperform", "cut", "negative", "layoff", "lawsuit",
        "investigation", "recall", "bankruptcy", "debt", "default",
        "plunge", "slump", "concern", "volatility", "downturn",
    }

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self._cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (score, timestamp)
        self._cache_ttl = 600  # 10 min

    def fetch_news(
        self,
        symbols: List[str],
        limit: int = 50,
        lookback_days: int = 3,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch recent news articles for given symbols.
        Returns {symbol: [article_dicts]}
        """
        headers = {
            "APCA-API-KEY-ID": self.cfg.alpaca.api_key,
            "APCA-API-SECRET-KEY": self.cfg.alpaca.secret_key,
        }

        start = (datetime.now() - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%dT00:00:00Z"
        )

        results: Dict[str, List[Dict]] = {s: [] for s in symbols}

        # Batch symbols (max 50 per request)
        for i in range(0, len(symbols), 50):
            batch = symbols[i : i + 50]
            params = {
                "symbols": ",".join(batch),
                "start": start,
                "limit": limit,
                "sort": "desc",
            }

            try:
                resp = requests.get(
                    self.cfg.alpaca.news_url,
                    headers=headers,
                    params=params,
                    timeout=15,
                )
                resp.raise_for_status()
                news_data = resp.json()

                for article in news_data.get("news", []):
                    article_info = {
                        "headline": article.get("headline", ""),
                        "summary": article.get("summary", ""),
                        "source": article.get("source", ""),
                        "created_at": article.get("created_at", ""),
                        "symbols": article.get("symbols", []),
                    }
                    # Assign to each relevant symbol
                    for sym in article.get("symbols", []):
                        if sym in results:
                            results[sym].append(article_info)

            except Exception as e:
                logger.error("Alpaca news fetch failed for batch %d: %s", i, e)

        return results

    def _score_text(self, text: str) -> float:
        """
        Simple keyword sentiment score for a text string.
        Returns float in [-1.0, 1.0].
        """
        words = set(text.lower().split())
        pos = len(words & self.POSITIVE_WORDS)
        neg = len(words & self.NEGATIVE_WORDS)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    def get_sentiment_scores(
        self,
        symbols: List[str],
        lookback_days: int = 3,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Compute aggregate sentiment score per symbol.
        Returns {symbol: {score, article_count, latest_headline}}

        Score interpretation:
          -1.0 = maximally negative
           0.0 = neutral / no news
          +1.0 = maximally positive
        """
        # Check cache
        now = time.time()
        uncached = []
        cached_results = {}
        for s in symbols:
            if s in self._cache:
                score, ts = self._cache[s]
                if (now - ts) < self._cache_ttl:
                    cached_results[s] = score
                else:
                    uncached.append(s)
            else:
                uncached.append(s)

        # Fetch news for uncached symbols
        news_by_symbol = {}
        if uncached:
            news_by_symbol = self.fetch_news(uncached, lookback_days=lookback_days)

        results = {}
        for symbol in symbols:
            if symbol in cached_results:
                results[symbol] = {
                    "score": cached_results[symbol],
                    "article_count": -1,  # Unknown from cache
                    "latest_headline": "",
                }
                continue

            articles = news_by_symbol.get(symbol, [])
            if not articles:
                results[symbol] = {
                    "score": 0.0,
                    "article_count": 0,
                    "latest_headline": "",
                }
                self._cache[symbol] = (0.0, time.time())
                continue

            # Score each article: headline (2x weight) + summary (1x weight)
            scores = []
            for art in articles:
                headline_score = self._score_text(art["headline"]) * 2.0
                summary_score = self._score_text(art.get("summary", ""))
                combined = (headline_score + summary_score) / 3.0
                scores.append(combined)

            # Recency-weighted average (newer articles matter more)
            weights = np.array([1.0 / (i + 1) for i in range(len(scores))])
            weights /= weights.sum()
            weighted_score = float(np.dot(scores, weights))
            weighted_score = max(-1.0, min(1.0, weighted_score))

            results[symbol] = {
                "score": round(weighted_score, 4),
                "article_count": len(articles),
                "latest_headline": articles[0]["headline"] if articles else "",
            }
            self._cache[symbol] = (weighted_score, time.time())

        return results


# ═══════════════════════════════════════════════
# UNIFIED DATA MANAGER
# ═══════════════════════════════════════════════

class DataManager:
    """
    Single entry point for all data.
    Coordinates Alpaca bars, FRED macro, and news sentiment.
    """

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self.alpaca = AlpacaDataFeed(cfg)
        self.fred = FREDDataFeed(cfg)
        self.news = AlpacaNewsFeed(cfg)
        logger.info("DataManager initialized with all feeds")

    def get_full_dataset(
        self,
        symbols: List[str],
        lookback_days: int = 60,
    ) -> Dict[str, Any]:
        """
        One-shot fetch: bars + macro regime for all symbols.
        Sentiment removed 2026-05-22 (P1-15): lexicon sentiment was computed
        from Alpaca news API on every scan but sentiment_score=0.0 on every
        Signal object — consuming API calls with zero alpha contribution.
        Re-enable when a real NLP sentiment model is integrated.
        """
        bars = self.alpaca.get_daily_bars(symbols, lookback_days=lookback_days)
        macro = self.fred.compute_regime_score()

        # SCHEMA FIX 2026-07-01 (JSON schema audit): this in-memory macro dict
        # has always used "regime"/"score", while the persisted macro_context.json
        # and market_decision.json files use "macro_regime"/"macro_score" for the
        # same concepts. That split already caused one confirmed bug
        # (outcome_tracker.py, fixed 2026-06-26) and is patched ad hoc with `or`
        # fallback chains in main.py, exit_monitor.py, outcome_tracker.py instead
        # of being fixed once at the source. Alias both key names here so every
        # consumer works regardless of which convention it happens to check —
        # this only adds keys, never removes the originals, so nothing existing
        # can break.
        if "macro_regime" not in macro:
            macro["macro_regime"] = macro.get("regime")
        if "macro_score" not in macro:
            macro["macro_score"] = macro.get("score")

        return {
            "bars": bars,
            "macro": macro,
            "sentiment": {},   # disabled — see P1-15
            "symbols_with_data": list(bars.keys()),
            "timestamp": datetime.now().isoformat(),
        }
