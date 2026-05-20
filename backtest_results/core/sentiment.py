"""
RAPTOR SENTIMENT ENGINE v4.1  —  core/sentiment.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Real-time news sentiment analysis using:
  1. Alpaca News API (free, built into alpaca-py)
  2. FinBERT (HuggingFace, finance-specific BERT model)
     - Trained on 10K+ financial texts (analyst reports, earnings calls)
     - Outperforms generic VADER/TextBlob by 15-20% on financial text
     - Ref: Araci (2019) "FinBERT: Financial Sentiment Analysis with BERT"

  Fallback: VADER (nltk) if FinBERT unavailable (no GPU, can't install torch)

Architecture:
  SentimentEngine runs as a background cache that the signal engine queries.
  It does NOT make trading decisions — it produces a sentiment score [-1, +1]
  that becomes one factor in the IC-weighted ensemble.

  Key insight from institutional practice:
  Sentiment VELOCITY (rate of change) matters more than sentiment LEVEL.
  A stock going from neutral (0.0) to negative (-0.6) in 15 minutes
  is a stronger signal than a stock that's been at -0.6 for 3 days.

Factors produced:
  sentiment_score:    [-1, +1] weighted average of recent article sentiments
  sentiment_velocity: [-1, +1] rate of change in sentiment over last hour
  news_volume:        [0, 1]   normalized news flow (more articles = more attention)

Install:
  pip install transformers torch   (for FinBERT, recommended)
  OR
  pip install nltk                 (for VADER fallback)
"""

import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from typing import Dict, Tuple, Optional

import numpy as np

import config

logger = logging.getLogger("Raptor.Sentiment")


# ═══════════════════════════════════════════════════════════════════════════════
# SENTIMENT ANALYZER (FinBERT with VADER fallback)
# ═══════════════════════════════════════════════════════════════════════════════

class SentimentAnalyzer:
    """
    Wraps FinBERT (preferred) or VADER (fallback) for financial text analysis.
    Lazy-loads the model on first call to avoid slowing bot startup.
    """

    def __init__(self):
        self._analyzer = None
        self._mode = None  # "finbert", "vader", or None
        self._initialized = False

    def _init(self):
        """Lazy-load sentiment model. Try FinBERT first, fall back to VADER."""
        if self._initialized:
            return

        # Try FinBERT (best for financial text)
        try:
            from transformers import pipeline
            self._analyzer = pipeline(
                "sentiment-analysis",
                model="ProsusAI/finbert",
                tokenizer="ProsusAI/finbert",
                device=-1,  # CPU (use 0 for GPU if available)
                top_k=None,
            )
            self._mode = "finbert"
            logger.info("Sentiment: FinBERT loaded (finance-specific NLP)")
        except Exception as e:
            logger.warning(f"FinBERT unavailable ({e}), trying VADER...")

            # Fallback to VADER
            try:
                import nltk
                nltk.download("vader_lexicon", quiet=True)
                from nltk.sentiment.vader import SentimentIntensityAnalyzer
                self._analyzer = SentimentIntensityAnalyzer()
                self._mode = "vader"
                logger.info("Sentiment: VADER loaded (general-purpose fallback)")
            except Exception as e2:
                logger.warning(f"VADER unavailable ({e2}). Sentiment disabled.")
                self._mode = None

        self._initialized = True

    def score(self, text: str) -> float:
        """
        Score text sentiment. Returns float in [-1, +1].
        +1 = very positive, -1 = very negative, 0 = neutral.
        """
        if not self._initialized:
            self._init()

        if self._mode is None or not text or len(text.strip()) < 10:
            return 0.0

        try:
            if self._mode == "finbert":
                result = self._analyzer(text[:512])  # BERT max 512 tokens
                # FinBERT returns list of dicts: [{"label": "positive", "score": 0.9}, ...]
                if isinstance(result, list) and len(result) > 0:
                    if isinstance(result[0], list):
                        result = result[0]
                    scores = {r["label"].lower(): r["score"] for r in result}
                    pos = scores.get("positive", 0)
                    neg = scores.get("negative", 0)
                    return float(np.clip(pos - neg, -1.0, 1.0))

            elif self._mode == "vader":
                scores = self._analyzer.polarity_scores(text)
                return float(np.clip(scores["compound"], -1.0, 1.0))

        except Exception as e:
            logger.debug(f"Sentiment score error: {e}")

        return 0.0

    @property
    def is_available(self) -> bool:
        if not self._initialized:
            self._init()
        return self._mode is not None


# ═══════════════════════════════════════════════════════════════════════════════
# SENTIMENT CACHE (per-ticker rolling window)
# ═══════════════════════════════════════════════════════════════════════════════

class SentimentCache:
    """
    Maintains a rolling window of sentiment scores per ticker.
    Computes: current sentiment, velocity, and news volume.

    The cache is populated by fetch_and_score() which pulls from
    Alpaca's news API. It's designed to be called once per scan cycle
    (every 5 minutes) — NOT per-ticker.
    """

    def __init__(self, window_minutes: int = 120, max_articles: int = 50):
        self.window_minutes = window_minutes
        self.max_articles = max_articles
        # ticker → deque of (timestamp, score, headline)
        self._data: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_articles)
        )
        self._last_fetch: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._analyzer = SentimentAnalyzer()

    def fetch_and_score(self, symbols: list):
        """
        Fetch recent news from Alpaca and score each article.
        Call once per scan cycle for the entire universe.
        """
        # Rate limit: don't fetch more than once per 3 minutes
        now = datetime.now(timezone.utc)
        if (now - self._last_fetch).total_seconds() < 180:
            return

        if not self._analyzer.is_available:
            return

        try:
            from alpaca.data.news import NewsClient
            from alpaca.data.requests import NewsRequest

            client = NewsClient(
                config.ALPACA_API_KEY,
                config.ALPACA_SECRET_KEY,
            )

            # Fetch news for all symbols at once (Alpaca supports multi-symbol)
            # Limit to last 2 hours of news
            start = now - timedelta(minutes=self.window_minutes)
            req = NewsRequest(
                symbols=symbols[:30],  # Alpaca limits symbols per request
                start=start,
                end=now,
                limit=50,
                sort="desc",
            )
            news = client.get_news(req)

            if not news or not hasattr(news, 'news'):
                self._last_fetch = now
                return

            articles = news.news if hasattr(news, 'news') else news
            scored = 0

            for article in articles:
                # Build text from headline + summary
                headline = getattr(article, 'headline', '') or ''
                summary = getattr(article, 'summary', '') or ''
                text = f"{headline}. {summary}".strip()

                if len(text) < 15:
                    continue

                score = self._analyzer.score(text)
                article_time = getattr(article, 'created_at', now) or now

                # Assign to each relevant symbol
                article_symbols = getattr(article, 'symbols', []) or []
                for sym in article_symbols:
                    if sym in symbols:
                        self._data[sym].append((article_time, score, headline[:80]))
                        scored += 1

            self._last_fetch = now
            if scored > 0:
                logger.info(f"Sentiment: scored {scored} articles across {len(set(s for a in articles for s in (getattr(a, 'symbols', []) or [])))} symbols")

        except ImportError:
            logger.warning("Alpaca NewsClient not available. Install alpaca-py >= 0.20")
        except Exception as e:
            logger.debug(f"News fetch error: {e}")
            self._last_fetch = now

    def get_sentiment(self, ticker: str) -> Dict[str, float]:
        """
        Get current sentiment metrics for a ticker.

        Returns:
            sentiment_score:    [-1, +1] recency-weighted average
            sentiment_velocity: [-1, +1] rate of change (positive = improving)
            news_volume:        [0, 1]   normalized article count
        """
        entries = self._data.get(ticker, deque())

        if not entries:
            return {
                "sentiment_score": 0.0,
                "sentiment_velocity": 0.0,
                "news_volume": 0.0,
            }

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=self.window_minutes)

        # Filter to window
        recent = [(t, s, h) for t, s, h in entries
                  if t and t >= cutoff]

        if not recent:
            return {
                "sentiment_score": 0.0,
                "sentiment_velocity": 0.0,
                "news_volume": 0.0,
            }

        # ── Recency-weighted sentiment score ─────────────────────────
        # More recent articles get exponentially more weight
        # half-life = 30 minutes
        scores = []
        weights = []
        for t, s, _ in recent:
            age_min = (now - t).total_seconds() / 60.0
            w = np.exp(-0.693 * age_min / 30.0)  # half-life = 30 min
            scores.append(s)
            weights.append(w)

        scores = np.array(scores)
        weights = np.array(weights)
        weight_sum = weights.sum() + 1e-9
        sentiment_score = float(np.sum(scores * weights) / weight_sum)

        # ── Sentiment velocity ───────────────────────────────────────
        # Compare avg sentiment in last 30 min vs previous 30-90 min
        recent_30 = [(t, s) for t, s, _ in recent
                     if (now - t).total_seconds() < 1800]
        older = [(t, s) for t, s, _ in recent
                 if 1800 <= (now - t).total_seconds() < 5400]

        if recent_30 and older:
            avg_recent = np.mean([s for _, s in recent_30])
            avg_older = np.mean([s for _, s in older])
            velocity = float(np.clip(avg_recent - avg_older, -1.0, 1.0))
        else:
            velocity = 0.0

        # ── News volume (normalized) ─────────────────────────────────
        # 0 articles = 0.0, 10+ articles in 2 hours = 1.0
        volume = float(np.clip(len(recent) / 10.0, 0.0, 1.0))

        return {
            "sentiment_score": round(sentiment_score, 4),
            "sentiment_velocity": round(velocity, 4),
            "news_volume": round(volume, 4),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# VIX REGIME MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

class VIXMonitor:
    """
    Monitors the CBOE Volatility Index (VIX) to adjust risk parameters.

    VIX regimes (based on 30 years of market data):
      < 15  : LOW_VOL   — calm markets, widen targets, increase size
      15-22 : NORMAL    — standard parameters
      22-30 : ELEVATED  — tighten stops, reduce Kelly by 30%
      > 30  : FEAR      — reduce Kelly by 50%, minimum position sizes
      > 40  : CRISIS    — sit out or trade minimum size only

    The VIX is the market's price of insurance against a crash.
    When insurance is cheap (VIX < 15), the market is complacent.
    When insurance is expensive (VIX > 30), institutions are hedging hard.

    How this affects your bot:
      - Kelly fraction scales inversely with VIX regime
      - Stop multipliers tighten in high-VIX (faster moves)
      - Target multipliers widen in low-VIX (less noise)
      - Position count reduces in FEAR/CRISIS

    Data source: yfinance (free, no API key needed).
    """

    def __init__(self, refresh_minutes: int = 15):
        self.refresh_minutes = refresh_minutes
        self._vix: float = 20.0  # default to "normal"
        self._regime: str = "NORMAL"
        self._last_fetch: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._vix_history: deque = deque(maxlen=100)

    def update(self):
        """Fetch current VIX. Rate-limited to once per refresh_minutes."""
        now = datetime.now(timezone.utc)
        if (now - self._last_fetch).total_seconds() < self.refresh_minutes * 60:
            return

        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="5d", interval="1h")
            if hist is not None and len(hist) > 0:
                self._vix = float(hist["Close"].iloc[-1])
                self._vix_history.append((now, self._vix))
                self._regime = self._classify(self._vix)
                self._last_fetch = now
                logger.info(f"VIX: {self._vix:.1f} | Regime: {self._regime}")
        except Exception as e:
            logger.debug(f"VIX fetch error: {e}")
            self._last_fetch = now

    def _classify(self, vix: float) -> str:
        if vix < 15:
            return "LOW_VOL"
        elif vix < 22:
            return "NORMAL"
        elif vix < 30:
            return "ELEVATED"
        elif vix < 40:
            return "FEAR"
        else:
            return "CRISIS"

    @property
    def vix(self) -> float:
        return self._vix

    @property
    def regime(self) -> str:
        return self._regime

    def kelly_multiplier(self) -> float:
        """
        Scale Kelly fraction by VIX regime.
        LOW_VOL → 1.2x (slightly more aggressive in calm markets)
        NORMAL  → 1.0x
        ELEVATED → 0.7x
        FEAR    → 0.5x
        CRISIS  → 0.25x
        """
        mult = {
            "LOW_VOL": 1.20,
            "NORMAL": 1.00,
            "ELEVATED": 0.70,
            "FEAR": 0.50,
            "CRISIS": 0.25,
        }
        return mult.get(self._regime, 1.0)

    def stop_multiplier(self) -> float:
        """
        In high-VIX, prices move faster → tighten stops.
        In low-VIX, noise is lower → can give more room.
        """
        mult = {
            "LOW_VOL": 1.10,   # wider stops (less noise)
            "NORMAL": 1.00,
            "ELEVATED": 0.85,  # tighter stops (faster moves)
            "FEAR": 0.75,
            "CRISIS": 0.65,
        }
        return mult.get(self._regime, 1.0)

    def target_multiplier(self) -> float:
        """
        In low-VIX, trends are smoother → wider targets.
        In high-VIX, take profit faster.
        """
        mult = {
            "LOW_VOL": 1.15,
            "NORMAL": 1.00,
            "ELEVATED": 0.90,
            "FEAR": 0.80,
            "CRISIS": 0.70,
        }
        return mult.get(self._regime, 1.0)

    def max_positions(self) -> int:
        """Reduce max positions in high-fear environments."""
        pos = {
            "LOW_VOL": config.MAX_POSITIONS,
            "NORMAL": config.MAX_POSITIONS,
            "ELEVATED": config.MAX_POSITIONS,
            "FEAR": max(2, config.MAX_POSITIONS - 1),
            "CRISIS": 1,
        }
        return pos.get(self._regime, config.MAX_POSITIONS)

    def should_trade(self) -> bool:
        """In CRISIS, optionally halt all new entries."""
        return self._regime != "CRISIS"

    def vix_velocity(self) -> float:
        """
        Rate of change of VIX. Positive = fear increasing.
        Computed from last 4 hours of VIX readings.
        """
        if len(self._vix_history) < 3:
            return 0.0
        recent = list(self._vix_history)
        vals = [v for _, v in recent[-8:]]
        if len(vals) < 2:
            return 0.0
        slope = (vals[-1] - vals[0]) / (len(vals) + 1e-9)
        return float(np.clip(slope / 5.0, -1.0, 1.0))


# ═══════════════════════════════════════════════════════════════════════════════
# EARNINGS CALENDAR GATE
# ═══════════════════════════════════════════════════════════════════════════════

class EarningsGate:
    """
    Prevents entries on stocks reporting earnings today or tomorrow.

    Why: Earnings announcements cause 3-10% moves that dwarf any
    intraday technical signal. Your stop will get blown through by
    a gap regardless of ATR sizing. This is not a wave you can ride —
    it's a tsunami that either doubles your money or wipes you out.

    The right play is to AVOID earnings days entirely for intraday
    momentum strategies and treat them as binary events (which is
    a completely different strategy requiring options).

    Data source: yfinance earnings calendar (free).
    """

    def __init__(self):
        self._blacklist: set = set()
        self._last_fetch: datetime = datetime.min.replace(tzinfo=timezone.utc)

    def update(self, symbols: list):
        """Refresh earnings blacklist. Call once at start of session."""
        now = datetime.now(timezone.utc)
        # Only refresh once per day
        if (now - self._last_fetch).total_seconds() < 86400:
            return

        try:
            import yfinance as yf
            today = datetime.now().strftime("%Y-%m-%d")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            blacklist = set()

            # Check each symbol (batch to limit API calls)
            for sym in symbols:
                if "/" in sym:  # skip crypto
                    continue
                try:
                    ticker = yf.Ticker(sym)
                    cal = ticker.calendar
                    if cal is not None:
                        # yfinance calendar format varies
                        if isinstance(cal, dict):
                            earnings_date = cal.get("Earnings Date")
                        else:
                            earnings_date = None

                        if earnings_date:
                            # Check if earnings is today or tomorrow
                            if isinstance(earnings_date, list):
                                for ed in earnings_date:
                                    ed_str = str(ed)[:10]
                                    if ed_str in (today, tomorrow):
                                        blacklist.add(sym)
                                        break
                            else:
                                ed_str = str(earnings_date)[:10]
                                if ed_str in (today, tomorrow):
                                    blacklist.add(sym)
                except Exception:
                    continue

            self._blacklist = blacklist
            self._last_fetch = now

            if blacklist:
                logger.info(f"Earnings blacklist: {', '.join(sorted(blacklist))}")
            else:
                logger.info("Earnings: no symbols reporting today/tomorrow")

        except Exception as e:
            logger.debug(f"Earnings calendar error: {e}")
            self._last_fetch = now

    def is_safe(self, ticker: str) -> bool:
        """Returns True if the ticker is NOT reporting earnings today/tomorrow."""
        return ticker not in self._blacklist
