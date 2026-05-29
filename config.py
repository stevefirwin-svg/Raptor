"""
Raptor v5.2 — Configuration
All parameters in ONE file. No magic numbers elsewhere.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


@dataclass
class AlpacaConfig:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    secret_key: str = os.getenv("ALPACA_SECRET_KEY", "")
    base_url: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    data_url: str = "https://data.alpaca.markets"
    news_url: str = "https://data.alpaca.markets/v1beta1/news"
    feed: str = "iex"


@dataclass
class FREDConfig:
    api_key: str = os.getenv("FRED_API_KEY", "")
    base_url: str = "https://api.stlouisfed.org/fred/series/observations"
    series: Dict[str, str] = field(default_factory=lambda: {
        "yield_curve_10y2y": "T10Y2Y",
        "fed_funds_rate":    "FEDFUNDS",
        "initial_claims":    "ICSA",
        "vix":               "VIXCLS",
        "credit_spread":     "BAMLC0A4CBBB",
        "cpi_yoy":           "CPIAUCSL",
        "ism_pmi":           "IPMAN",      # Industrial Production: Manufacturing — NAPM was discontinued by FRED.
                                              # IPMAN is monthly, freely available, directionally equivalent for
                                              # regime classification (rising = expansion, falling = contraction).
        "m2_money_supply":   "M2SL",
        "retail_sales":      "RSXFS",
        "unemployment_rate": "UNRATE",
    })
    cache_hours: int = 6


@dataclass
class SignalConfig:
    # Lookback: 150 calendar days ≈ 105 trading bars.
    # Required: 50-EMA needs 50, ATR percentile needs 74,
    # BB squeeze needs 80, Hurst needs 40. 105 covers all with margin.
    lookback_days: int = 150


@dataclass
class RiskConfig:
    kelly_fraction: float = 0.12  # H-8: was 0.15 but signals.py hard-clips to 0.12 ceiling — aligning config to actual cap. TODO:DERIVE both via EVT tail on closed trade returns.
    max_position_pct: float = 0.08
    min_position_pct: float = 0.02
    max_positions: int = 10  # Reduced from 15 — 15 positions requires margin by design at $105K equity
    max_sector_pct: float = 0.30
    max_pairwise_correlation: float = 0.70
    initial_stop_atr_mult: float = 3.0    # Kaminski & Lo 2014: 3-4x ATR optimal for MR swings
    # Time-dependent trailing stop (Bertsimas & Lo 1998)
    # For MR under OU process, optimal exit boundary WIDENS early
    # then NARROWS as expected reversion time approaches.
    # Implementation: trail multiplier shrinks with days held
    #   Day 1-3:  trail = 2.5x ATR (wide, survive noise)
    #   Day 4-10: trail = 2.0x ATR (reversion should be starting)
    #   Day 11-20: trail = 1.5x ATR (lock in the move)
    #   Day 21+:  trail = 1.0x ATR (tight, extract remaining edge)
    trail_early_atr: float = 2.5        # Days 1-3
    trail_mid_atr: float = 2.0          # Days 4-10
    trail_late_atr: float = 1.5         # Days 11-20
    trail_final_atr: float = 1.0        # Days 21+
    trail_early_days: int = 3           # When to switch from early to mid
    trail_mid_days: int = 10            # When to switch from mid to late
    trail_late_days: int = 20           # When to switch from late to final
    # NO fixed take-profit. Trailing stop is the only exit mechanism.
    # Winners run until the trail catches them.
    atr_period: int = 14
    time_stop_days: int = 30       # 6-week max hold
    min_hold_days: int = 1
    max_portfolio_drawdown: float = 0.12
    drawdown_reduction_factor: float = 0.5
    halt_in_crisis: bool = True
    reduce_in_bearish: float = 0.5


@dataclass
class BacktestConfig:
    start_date: str = "2020-01-01"
    end_date: str = "2025-12-31"
    initial_capital: float = 100_000.0
    slippage_bps: float = 5.0
    benchmark_symbol: str = "SPY"
    risk_free_rate: float = 0.05


@dataclass
class ExecutionConfig:
    scan_time: str = "09:35"  # Reference only — Task Scheduler controls actual timing
    order_type: str = "limit"
    limit_offset_bps: float = 10.0
    max_orders_per_scan: int = 5
    paper_trading: bool = True


@dataclass
class LogConfig:
    log_dir: str = "logs"
    trade_log_file: str = "trades.csv"
    signal_log_file: str = "signals.csv"
    backtest_results_dir: str = "backtest_results"
    log_level: str = "INFO"


@dataclass
class RaptorConfig:
    version: str = "5.4.0"
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    fred: FREDConfig = field(default_factory=FREDConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    log: LogConfig = field(default_factory=LogConfig)

    def validate_all(self):
        assert self.alpaca.api_key, "ALPACA_API_KEY not set"
        assert self.alpaca.secret_key, "ALPACA_SECRET_KEY not set"
        assert self.fred.api_key, "FRED_API_KEY not set"
        assert self.risk.trail_early_atr < self.risk.initial_stop_atr_mult, \
            "Trail must be tighter than initial stop"
        assert self.risk.trail_final_atr < self.risk.trail_early_atr, \
            "Trail must tighten over time"
        print(f"[CONFIG] Raptor v{self.version} validated")
        return self


CONFIG = RaptorConfig()
