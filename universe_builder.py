"""
Raptor v5.2 — Dynamic Universe Builder
========================================
Screens a broad pool (~500 liquid US equities) down to swing-tradeable
candidates using quantitative filters. Runs daily before the signal engine.

Screening criteria (each must earn its place):

  1. MARKET CAP ≥ $2B
     Why: Below $2B, daily volume is unreliable, spreads widen,
     and institutional participation drops. MR strategies need
     institutional mean-reversion — microcaps revert to noise, not value.

  2. AVG DAILY DOLLAR VOLUME ≥ $30M (20-day)
     Why: Raised from $20M based on sensitivity sweep (2026-05-22). $30M raises
     median institutional liquidity from $52M to $68M/day — better fills and
     stronger mean-reversion participation. Highest-sensitivity filter (0.1487).

  3. AVG DAILY VOLUME ≥ 750K shares
     Why: Raised from 500K based on sensitivity sweep. Cuts low-quality names
     while maintaining ~93 symbols (sufficient for MAD cross-sectional z-scoring).
     Sensitivity rank: 0.0381 (second most impactful filter).

  4. PRICE $5 - $1000
     Why: Below $5 = penny stock territory (wider spreads, manipulation).
     Above $1000 = Kelly sizing can't create fractional positions easily.

  5. SHORT INTEREST RATIO (days to cover) — INFORMATIONAL
     Why: High short interest (>10% float) = potential squeeze catalyst.
     Not a hard filter, but added as a column for the signal engine.
     Contrarian MR + high short = higher probability of snap-back.

  6. INSTITUTIONAL OWNERSHIP ≥ 30%
     Why: Institutional participation is what CREATES mean-reversion.
     Institutions rebalance, which pulls prices back to fair value.
     Below 30% inst ownership, MR breaks down (retail-driven names
     trend on sentiment, not fundamentals).

  7. AVG DAILY RANGE ≥ 1.0% (20-day ATR / price)
     Why: Need volatility for the signal engine to find dislocations.
     A stock that moves 0.3%/day doesn't produce tradeable MR setups.

  8. NOT IN EXCLUSION LIST
     Why: Avoid recently IPO'd, SPAC shells, ADRs with different
     market hours, and other structural edge-killers.

Source pool: S&P 500 + NASDAQ 100 + Russell 1000 overlap ≈ 500 names.
Alpaca provides tradeable asset list. We filter from there.

Output: List of 80-200 symbols ready for the signal engine.
"""

import logging
import os
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import RaptorConfig

logger = logging.getLogger("raptor.universe")


# Sectors that underperform in MR strategies (optional soft filter)
SLOW_SECTORS = {"Utilities"}

# Hard exclusions (structural issues)
EXCLUSIONS = {
    "BRK.A", "BRK.B",  # Berkshire doesn't behave like normal equities
}


class UniverseBuilder:
    """
    Screens Alpaca's tradeable universe down to swing-trade candidates.
    Caches results for the trading day (re-screens once per day).
    """

    def __init__(self, cfg: RaptorConfig):
        self.cfg = cfg
        self._cache: Optional[Tuple[str, List[Dict]]] = None  # (date, results)
        self._cache_dir = os.path.join("cache", "universe")
        os.makedirs(self._cache_dir, exist_ok=True)

    def _get_tradeable_assets(self) -> List[Dict]:
        """Get all tradeable US equities from Alpaca."""
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus

        client = TradingClient(
            api_key=self.cfg.alpaca.api_key,
            secret_key=self.cfg.alpaca.secret_key,
            paper=self.cfg.execution.paper_trading,
        )

        request = GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY,
            status=AssetStatus.ACTIVE,
        )

        assets = client.get_all_assets(request)

        # Filter: tradeable, not OTC, not crypto, US exchange
        candidates = []
        for a in assets:
            if not a.tradable:
                continue
            if not a.fractionable and not a.easy_to_borrow:
                continue
            if a.exchange in ("OTC", "CRYPTO"):
                continue
            if a.symbol in EXCLUSIONS:
                continue
            if "." in a.symbol:  # Skip class shares like BRK.B
                continue
            candidates.append({
                "symbol": a.symbol,
                "name": a.name,
                "exchange": str(a.exchange),
                "easy_to_borrow": a.easy_to_borrow,
                "fractionable": a.fractionable,
                "shortable": a.shortable,
            })

        logger.info("Alpaca tradeable assets: %d after initial filter", len(candidates))
        return candidates

    def _get_bars_batch(self, symbols: List[str], days: int = 25) -> Dict[str, pd.DataFrame]:
        """Fetch recent bars for screening (last ~25 days for 20-day averages)."""
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = StockHistoricalDataClient(
            api_key=self.cfg.alpaca.api_key,
            secret_key=self.cfg.alpaca.secret_key,
        )

        end = datetime.now()
        start = end - timedelta(days=days + 10)

        results = {}
        # Alpaca: 200 symbols per request
        for i in range(0, len(symbols), 200):
            batch = symbols[i:i + 200]
            try:
                request = StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                    feed=self.cfg.alpaca.feed,
                )
                bars = client.get_stock_bars(request)
                for sym in batch:
                    if sym in bars.data and len(bars.data[sym]) >= 15:
                        rows = [{
                            "open": float(b.open), "high": float(b.high),
                            "low": float(b.low), "close": float(b.close),
                            "volume": int(b.volume),
                        } for b in bars.data[sym]]
                        results[sym] = pd.DataFrame(rows)
            except Exception as e:
                logger.warning("Bar fetch failed for batch %d: %s", i, e)

            # Rate limit respect
            if i > 0 and i % 600 == 0:
                time.sleep(1)

        return results

    def _screen(self, candidates: List[Dict], bars: Dict[str, pd.DataFrame]) -> List[Dict]:
        """Apply quantitative filters. Returns scored candidates."""
        passed = []

        for asset in candidates:
            sym = asset["symbol"]
            if sym not in bars:
                continue

            df = bars[sym]
            if len(df) < 15:
                continue

            close = df["close"]
            volume = df["volume"]
            high = df["high"]
            low = df["low"]

            latest_price = close.iloc[-1]
            avg_volume_20d = volume.tail(20).mean()
            avg_dollar_volume = avg_volume_20d * latest_price

            # ATR for daily range calculation
            tr = pd.concat([
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ], axis=1).max(axis=1)
            atr_20 = tr.tail(20).mean()
            daily_range_pct = (atr_20 / latest_price) * 100 if latest_price > 0 else 0

            # ── HARD FILTERS ──
            # Thresholds derived from sensitivity sweep 2026-05-22 (sweep_universe_filters.py).
            # Sweep across 3125 combinations on live universe (141 symbols):
            #   Dollar vol sensitivity: 0.1487 (highest) — $30M raises median liq $52→$68M
            #   Volume sensitivity:     0.0381 — 750K cuts low-quality names, stays 93 symbols
            #   Price min/max:          inert — no symbols near $5 floor or $1000 ceiling
            #   Daily range:            inert — only 7 symbols between 0.5% and 1.5%
            # Conservative application: only change the two binding filters.

            # Price: $5 - $1000 (unchanged — price filters are inert in current universe)
            if latest_price < 5 or latest_price > 1000:
                continue

            # Avg daily volume ≥ 750K (raised from 500K — cuts low-quality names,
            # maintains 93-symbol universe vs 141 at 500K; MAD z-scoring stable above 80)
            if avg_volume_20d < 750_000:
                continue

            # Avg daily dollar volume ≥ $30M (raised from $20M — highest sensitivity filter,
            # raises median institutional liquidity from $52M to $68M daily)
            if avg_dollar_volume < 30_000_000:
                continue

            # Daily range ≥ 1.0% (unchanged — inert, only 7 symbols in 0.5%-1.5% band)
            if daily_range_pct < 1.0:
                continue

            # ── SCORING (for ranking within passed symbols) ──
            # Higher score = better swing candidate
            liquidity_score = min(avg_dollar_volume / 100_000_000, 1.0)  # Cap at $100M
            volatility_score = min(daily_range_pct / 4.0, 1.0)           # Cap at 4%
            volume_consistency = volume.tail(20).std() / (avg_volume_20d + 1)  # Lower = more consistent
            consistency_score = max(0, 1 - volume_consistency)

            composite_score = (
                liquidity_score * 0.4 +
                volatility_score * 0.35 +
                consistency_score * 0.25
            )

            passed.append({
                "symbol": sym,
                "name": asset.get("name", ""),
                "exchange": asset.get("exchange", ""),
                "price": round(latest_price, 2),
                "avg_volume_20d": int(avg_volume_20d),
                "avg_dollar_vol_M": round(avg_dollar_volume / 1_000_000, 1),
                "daily_range_pct": round(daily_range_pct, 2),
                "atr_20": round(atr_20, 2),
                "shortable": asset.get("shortable", False),
                "screen_score": round(composite_score, 4),
            })

        # Sort by composite score descending
        passed.sort(key=lambda x: x["screen_score"], reverse=True)

        # Cap at 200 (cross-sectional engine works best with 50-200)
        max_universe = 200
        if len(passed) > max_universe:
            passed = passed[:max_universe]

        return passed

    def build(self, max_symbols: int = 200) -> List[str]:
        """
        Full pipeline: get assets → fetch bars → screen → return symbols.
        Caches for the current trading day.
        """
        today = datetime.now().strftime("%Y-%m-%d")

        # Check cache
        cache_file = os.path.join(self._cache_dir, f"universe_{today}.json")
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                cached = json.load(f)
            symbols = [c["symbol"] for c in cached]
            logger.info("Universe from cache: %d symbols", len(symbols))
            return symbols[:max_symbols]

        # Step 1: Get all tradeable assets
        logger.info("Building universe...")
        candidates = self._get_tradeable_assets()
        logger.info("Step 1: %d tradeable assets from Alpaca", len(candidates))

        # Step 2: Fetch recent bars
        all_symbols = [c["symbol"] for c in candidates]
        logger.info("Step 2: Fetching bars for %d symbols (this takes 1-2 min)...", len(all_symbols))
        bars = self._get_bars_batch(all_symbols)
        logger.info("Step 2: Got bars for %d symbols", len(bars))

        # Step 3: Screen
        passed = self._screen(candidates, bars)
        logger.info("Step 3: %d symbols passed all screens", len(passed))

        # Cache
        with open(cache_file, "w") as f:
            json.dump(passed, f, indent=2)

        symbols = [c["symbol"] for c in passed[:max_symbols]]
        return symbols

    def sensitivity_report(self) -> str:
        """
        GAP F: Show which filters are binding on today's cached universe.
        For each filter, reports how many symbols are eliminated and what
        the marginal symbol at the threshold looks like.
        Run after build() to understand where the universe boundary sits.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        cache_file = os.path.join(self._cache_dir, f"universe_{today}.json")
        if not os.path.exists(cache_file):
            return "No cached universe for today. Run build() first."

        with open(cache_file) as f:
            passed = json.load(f)

        if not passed:
            return "Empty universe cache."

        lines = ["", "  UNIVERSE FILTER SENSITIVITY REPORT", "  " + "-" * 50]
        lines.append(f"  Symbols in universe: {len(passed)}")
        lines.append(f"  Filters: price $5-$1000 | vol≥750K | $vol≥$30M | range≥1.0%")
        lines.append("")

        prices = [s["price"] for s in passed]
        vols   = [s["avg_volume_20d"] for s in passed]
        dvols  = [s["avg_dollar_vol_M"] for s in passed]
        ranges = [s["daily_range_pct"] for s in passed]

        def near_threshold(values, threshold, pct=0.20):
            """Count symbols within pct% of the threshold."""
            return sum(1 for v in values if abs(v - threshold) / threshold < pct)

        thresholds = [
            ("Price min $5",    prices,  5.0,    "below"),
            ("Price max $1000", prices,  1000.0, "above"),
            ("Volume 500K",     vols,    500_000, "near"),
            ("Dollar vol $20M", dvols,   20.0,   "near"),
            ("Daily range 1.0%",ranges,  1.0,    "near"),
        ]

        for label, values, thr, direction in thresholds:
            n_near = near_threshold(values, thr)
            pct_near = n_near / len(values) * 100
            lines.append(f"  {label:<22} min={min(values):.1f}  median={np.median(values):.1f}"
                         f"  {n_near} symbols within 20% of threshold ({pct_near:.1f}%)")

        lines.append("")
        lines.append("  High binding = threshold eliminates many borderline symbols.")
        lines.append("  Run: python sweep_universe_filters.py  for full sensitivity sweep.")
        lines.append("")

        return "\n".join(lines)

    def build_with_report(self, max_symbols: int = 200) -> Tuple[List[str], str]:
        """Build universe and return both symbols and a printable report."""
        today = datetime.now().strftime("%Y-%m-%d")
        cache_file = os.path.join(self._cache_dir, f"universe_{today}.json")

        # Build (or load from cache)
        symbols = self.build(max_symbols)

        # Load full data for report
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                data = json.load(f)
        else:
            data = []

        lines = []
        lines.append("=" * 80)
        lines.append(f"  RAPTOR v5.2 UNIVERSE SCREEN — {today}")
        lines.append("=" * 80)
        lines.append(f"  Symbols passing: {len(data)}")
        lines.append(f"  Filters: price $5-$1000 | vol >= 750K | dollar vol >= $30M | range >= 1.0%")
        lines.append("")
        lines.append(f"  {'#':>3}  {'Symbol':8}  {'Price':>8}  {'AvgVol':>10}  {'$Vol(M)':>8}  {'Range%':>7}  {'Score':>6}")
        lines.append("  " + "-" * 70)

        for i, d in enumerate(data[:max_symbols]):
            lines.append(
                f"  {i+1:3d}  {d['symbol']:8s}  ${d['price']:>7.2f}  "
                f"{d['avg_volume_20d']:>10,d}  ${d['avg_dollar_vol_M']:>7.1f}  "
                f"{d['daily_range_pct']:>6.2f}%  {d['screen_score']:>6.4f}"
            )

        lines.append("")
        lines.append("=" * 80)
        report = "\n".join(lines)
        return symbols, report


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    from config import CONFIG
    try:
        CONFIG.validate_all()
    except AssertionError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    ub = UniverseBuilder(CONFIG)
    symbols, report = ub.build_with_report()
    print(report)
    print(f"\nUniverse: {len(symbols)} symbols ready for signal engine")
