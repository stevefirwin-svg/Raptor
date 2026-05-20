"""
RAPTOR RISK MANAGER v4.0  —  core/risk.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Complete position risk management:

  ┌──────────────────────────────────────────────────────────────────────────┐
  │ LAYER 1: PRE-TRADE GATES                                                │
  │   - Daily/weekly loss limit (persisted to disk, survives restarts)      │
  │   - Position count limit                                                │
  │   - Sector concentration limit (max 50% in one sector)                 │
  │   - Portfolio correlation guard (reject if avg ρ > 0.70 with holdings) │
  │   - Buying power check (10% buffer)                                     │
  │                                                                         │
  │ LAYER 2: ENTRY SIZING                                                   │
  │   - Kelly fraction from signals.py → dollar allocation                 │
  │   - ATR-based stop/target from conviction tier                         │
  │   - Hard cap: max 30% of equity per position                           │
  │                                                                         │
  │ LAYER 3: POSITION LIFECYCLE (trailing stops, partial exits)            │
  │   Stage 0 (0-12 min):  Bracket stop only. Let it breathe.             │
  │   Stage 1 (12-45 min): Move stop to breakeven at +0.5 ATR.            │
  │   Stage 2 (45-75 min): Take 50% at +1.5 ATR. Trail remainder.         │
  │   Stage 3 (75-90 min): Tighten trail to 0.75 ATR.                     │
  │   Stage 4 (>90 min):   Time exit. Close at market.                     │
  └──────────────────────────────────────────────────────────────────────────┘

All stop/target prices flow from signals.py → risk.py → engine.py.
No duplicate Kelly formula. Single source of truth in signals.py.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import numpy as np

import config
from core.universe import get_sector, is_crypto

logger = logging.getLogger("Raptor.Risk")


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION STATE TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PositionState:
    """Full lifecycle state for one open position."""
    symbol: str
    direction: str
    entry_price: float
    stop: float
    target: float
    shares: float
    entry_time: datetime
    score: float
    atr: float
    order_id: str
    signal: dict
    peak_price: float = 0.0
    trough_price: float = 1e9
    partial_taken: bool = False
    breakeven_set: bool = False
    original_shares: float = 0.0

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price
        if self.trough_price == 1e9:
            self.trough_price = self.entry_price
        if self.original_shares == 0.0:
            self.original_shares = self.shares

    @property
    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_time).total_seconds() / 60.0

    def update_extremes(self, current_price: float):
        """Track peak/trough prices for trailing stop calculations."""
        if self.direction == "long":
            self.peak_price = max(self.peak_price, current_price)
        else:
            self.trough_price = min(self.trough_price, current_price)

    def profit_in_atr(self, current_price: float) -> float:
        """P&L expressed in ATR units."""
        if self.atr <= 0:
            return 0.0
        if self.direction == "long":
            return (current_price - self.entry_price) / self.atr
        return (self.entry_price - current_price) / self.atr

    def pnl_dollars(self, current_price: float) -> float:
        """P&L in dollars."""
        if self.direction == "long":
            return (current_price - self.entry_price) * self.shares
        return (self.entry_price - current_price) * self.shares


# ═══════════════════════════════════════════════════════════════════════════════
# DRAWDOWN PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════

class DrawdownTracker:
    """
    Persists drawdown state to disk so it survives bot restarts.
    Without this, a bot crash mid-session resets the loss counter,
    allowing unlimited losses.
    """

    def __init__(self, filepath: str = None):
        self.filepath = filepath or config.DRAWDOWN_FILE
        self._state = self._load()

    def _load(self) -> dict:
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r") as f:
                    state = json.load(f)
                # Reset if it's a new day
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if state.get("date") != today:
                    return self._fresh_state(today)
                return state
        except Exception:
            pass
        return self._fresh_state()

    def _fresh_state(self, date: str = None) -> dict:
        return {
            "date": date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "session_start_equity": None,
            "week_start_equity": None,
            "week_start_date": None,
            "daily_pnl": 0.0,
            "weekly_pnl": 0.0,
            "halted": False,
        }

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save drawdown state: {e}")

    def set_session_equity(self, equity: float):
        if self._state["session_start_equity"] is None:
            self._state["session_start_equity"] = equity
            logger.info(f"Session start equity: ${equity:,.2f}")
        if self._state["week_start_equity"] is None:
            self._state["week_start_equity"] = equity
            self._state["week_start_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Check if new week (Monday)
        today = datetime.now(timezone.utc)
        if today.weekday() == 0:  # Monday
            ws_date = self._state.get("week_start_date", "")
            if ws_date != today.strftime("%Y-%m-%d"):
                self._state["week_start_equity"] = equity
                self._state["week_start_date"] = today.strftime("%Y-%m-%d")
                self._state["weekly_pnl"] = 0.0
        self._save()

    def daily_loss_ok(self, current_equity: float) -> bool:
        """Check daily loss limit. Returns False if breached."""
        start = self._state.get("session_start_equity")
        if start is None or start <= 0:
            return True
        loss_pct = (start - current_equity) / start
        if loss_pct >= config.MAX_DAILY_LOSS_PCT:
            logger.warning(
                f"DAILY LOSS LIMIT: {loss_pct:.1%} "
                f"(${start - current_equity:,.0f}). Trading halted."
            )
            self._state["halted"] = True
            self._save()
            return False
        return True

    def weekly_loss_ok(self, current_equity: float) -> bool:
        """Check weekly loss limit."""
        start = self._state.get("week_start_equity")
        if start is None or start <= 0:
            return True
        loss_pct = (start - current_equity) / start
        if loss_pct >= config.MAX_WEEKLY_LOSS_PCT:
            logger.warning(
                f"WEEKLY LOSS LIMIT: {loss_pct:.1%}. Trading halted for the week."
            )
            self._state["halted"] = True
            self._save()
            return False
        return True

    @property
    def is_halted(self) -> bool:
        return self._state.get("halted", False)


# ═══════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class RiskManager:

    def __init__(self):
        self._drawdown = DrawdownTracker()
        self._closing: Set[str] = set()
        self._recent_returns: Dict[str, np.ndarray] = {}

    # ── Session Management ────────────────────────────────────────────────────

    def set_session_equity(self, equity: float):
        self._drawdown.set_session_equity(equity)

    def loss_limits_ok(self, current_equity: float) -> bool:
        """Check both daily and weekly loss limits."""
        if self._drawdown.is_halted:
            return False
        if not self._drawdown.daily_loss_ok(current_equity):
            return False
        if not self._drawdown.weekly_loss_ok(current_equity):
            return False
        return True

    # ── Pre-Trade Gates ───────────────────────────────────────────────────────

    def at_position_limit(self, n_open: int) -> bool:
        if n_open >= config.MAX_POSITIONS:
            logger.info(f"Max positions ({config.MAX_POSITIONS}) reached")
            return True
        return False

    def sector_concentration_ok(self, ticker: str,
                                  positions: list) -> bool:
        """
        Check that adding this ticker won't exceed sector concentration limit.
        Max 50% of equity in any one sector.
        """
        new_sector = get_sector(ticker)
        if new_sector in ("INDEX", "UNKNOWN"):
            return True  # ETFs and unknowns don't count toward sector limits

        sector_count = sum(
            1 for p in positions
            if get_sector(p.symbol) == new_sector
        )
        # Including the new position
        total = len(positions) + 1
        if total <= 0:
            return True
        sector_pct = (sector_count + 1) / total
        if sector_pct > config.MAX_SECTOR_PCT:
            logger.info(
                f"Sector limit: {new_sector} would be {sector_pct:.0%} "
                f"of portfolio (max {config.MAX_SECTOR_PCT:.0%})"
            )
            return False
        return True

    def correlation_ok(self, ticker: str, positions: list,
                        returns_cache: dict = None) -> bool:
        """
        Check that new position doesn't create excessive portfolio correlation.

        Computes average pairwise correlation between the candidate ticker
        and all existing positions. Reject if avg ρ > MAX_CORRELATION.

        This prevents loading up on 5 semiconductor stocks that all
        move together (effectively one giant correlated bet).
        """
        if len(positions) == 0:
            return True

        if is_crypto(ticker):
            # Crypto has low correlation with stocks — usually OK
            return True

        # Use cached returns if available
        if returns_cache and ticker in returns_cache:
            new_returns = returns_cache[ticker]
        else:
            return True  # If no return data, allow (can't compute correlation)

        correlations = []
        for pos in positions:
            sym = pos.symbol
            if is_crypto(sym):
                continue
            if returns_cache and sym in returns_cache:
                pos_returns = returns_cache[sym]
                min_len = min(len(new_returns), len(pos_returns))
                if min_len < 10:
                    continue
                corr = np.corrcoef(
                    new_returns[-min_len:],
                    pos_returns[-min_len:]
                )[0, 1]
                if not np.isnan(corr):
                    correlations.append(abs(corr))

        if not correlations:
            return True

        avg_corr = np.mean(correlations)
        if avg_corr > config.MAX_CORRELATION:
            logger.info(
                f"Correlation guard: {ticker} avg ρ={avg_corr:.2f} "
                f"with portfolio (max {config.MAX_CORRELATION})"
            )
            return False
        return True

    def buying_power_ok(self, cost: float, buying_power: float) -> bool:
        """Require 10% buffer above trade cost."""
        ok = buying_power >= cost * 1.10
        if not ok:
            logger.warning(
                f"Buying power: need ${cost * 1.10:,.0f}, "
                f"have ${buying_power:,.0f}"
            )
        return ok

    # ── Position Sizing ───────────────────────────────────────────────────────

    def position_dollars(self, kelly_f: float, equity: float) -> float:
        """Dollar allocation = kelly_f × equity × liquidity_buffer."""
        dollars = equity * kelly_f * config.LIQUIDITY_BUFFER
        max_dollars = equity * config.MAX_POSITION_PCT
        return min(dollars, max_dollars)

    def shares(self, dollars: float, price: float,
               is_crypto: bool = False) -> float:
        """Convert dollar allocation to share count."""
        qty = dollars / price
        return round(qty, 4) if is_crypto else int(qty)

    def compute_levels(self, price: float, atr: float,
                        stop_mult: float, target_mult: float,
                        direction: str) -> dict:
        """
        Compute stop and target prices from ATR multipliers.
        Returns dict with stop, target, rr, valid.
        """
        if direction == "long":
            stop = price - atr * stop_mult
            target = price + atr * target_mult
        else:
            stop = price + atr * stop_mult
            target = price - atr * target_mult

        risk = abs(price - stop)
        reward = abs(target - price)
        rr = reward / (risk + 1e-9)

        return {
            "stop": round(max(stop, 0.01), 2),
            "target": round(max(target, 0.01), 2),
            "rr": round(rr, 3),
            "valid": stop > 0 and target > 0 and rr >= config.MIN_RR,
        }

    def crypto_room(self, equity: float, positions: list) -> float:
        """Remaining crypto allocation as fraction of equity."""
        used = sum(
            abs(float(p.market_value)) for p in positions
            if is_crypto(p.symbol)
        )
        return max(0.0, config.CRYPTO_CAP_PCT - used / (equity + 1e-9))

    # ── Position Lifecycle ────────────────────────────────────────────────────

    def evaluate_position(self, pos: PositionState,
                           current_price: float) -> dict:
        """
        Evaluate what action to take on an open position.

        Returns:
            action: HOLD | MOVE_STOP_BREAKEVEN | PARTIAL_EXIT |
                    MOVE_STOP | TIME_EXIT
            And relevant fields (new_stop, shares_to_close, etc.)
        """
        age = pos.age_minutes
        atr = pos.atr
        pos.update_extremes(current_price)
        profit_atr = pos.profit_in_atr(current_price)
        pnl = pos.pnl_dollars(current_price)

        # ── STAGE 4: TIME EXIT (>90 min) ─────────────────────────────────
        if age >= config.MAX_HOLD_MINUTES:
            return {
                "action": "TIME_EXIT",
                "shares": pos.shares,
                "pnl": pnl,
                "reason": f"Max hold {config.MAX_HOLD_MINUTES}min reached",
            }

        # ── STAGE 0: MINIMUM HOLD (0-12 min) ─────────────────────────────
        if age < config.MIN_HOLD_MINUTES:
            return {
                "action": "HOLD",
                "reason": f"Min hold ({age:.0f}/{config.MIN_HOLD_MINUTES}min) "
                          f"P&L: {profit_atr:+.2f} ATR",
            }

        # ── STAGE 3: TIGHT TRAIL (75-90 min) ─────────────────────────────
        if age >= 75:
            if pos.direction == "long":
                trail = pos.peak_price - atr * config.TRAIL_ATR_STAGE3
            else:
                trail = pos.trough_price + atr * config.TRAIL_ATR_STAGE3

            hit = (pos.direction == "long" and current_price <= trail) or \
                  (pos.direction == "short" and current_price >= trail)

            if hit:
                return {
                    "action": "TRAIL_EXIT",
                    "shares": pos.shares,
                    "pnl": pnl,
                    "reason": f"Stage 3 trail hit @ ${trail:.2f}",
                }
            return {
                "action": "MOVE_STOP",
                "new_stop": round(trail, 2),
                "reason": f"Stage 3 tightening trail to ${trail:.2f}",
            }

        # ── STAGE 2: PARTIAL EXIT (45-75 min) ────────────────────────────
        if age >= 45:
            if profit_atr >= config.PARTIAL_EXIT_ATR and not pos.partial_taken:
                partial_shares = max(1, int(pos.shares // 2))
                return {
                    "action": "PARTIAL_EXIT",
                    "shares": partial_shares,
                    "pnl": pos.pnl_dollars(current_price) * partial_shares / max(pos.shares, 1),
                    "reason": f"Taking 50% at +{profit_atr:.1f} ATR",
                }

            if pos.partial_taken:
                if pos.direction == "long":
                    trail = pos.peak_price - atr * config.TRAIL_ATR_STAGE2
                else:
                    trail = pos.trough_price + atr * config.TRAIL_ATR_STAGE2

                hit = (pos.direction == "long" and current_price <= trail) or \
                      (pos.direction == "short" and current_price >= trail)
                if hit:
                    return {
                        "action": "TRAIL_EXIT",
                        "shares": pos.shares,
                        "pnl": pnl,
                        "reason": f"Stage 2 trail hit @ ${trail:.2f}",
                    }
                return {
                    "action": "MOVE_STOP",
                    "new_stop": round(trail, 2),
                    "reason": f"Stage 2 trail @ ${trail:.2f}",
                }

        # ── STAGE 1: BREAKEVEN (12-45 min) ────────────────────────────────
        if profit_atr >= config.BREAKEVEN_ATR and not pos.breakeven_set:
            return {
                "action": "MOVE_STOP_BREAKEVEN",
                "new_stop": pos.entry_price,
                "reason": f"Moving stop to BE at +{profit_atr:.1f} ATR",
            }

        # ── Let winners run — check if we should widen target ─────────────
        # If profit > 2 ATR and momentum still strong, raise target
        if profit_atr >= 2.0 and pos.breakeven_set and not pos.partial_taken:
            # Let it run — don't interfere
            return {
                "action": "HOLD",
                "reason": f"RUNNER: +{profit_atr:.1f} ATR — letting it run",
            }

        return {
            "action": "HOLD",
            "reason": f"Age={age:.0f}min | P&L={profit_atr:+.1f}ATR | "
                      f"Stop=${pos.stop:.2f}",
        }

    # ── Close tracking ────────────────────────────────────────────────────────

    def mark_closing(self, ticker: str):
        self._closing.add(ticker)

    def mark_closed(self, ticker: str):
        self._closing.discard(ticker)

    def is_closing(self, ticker: str) -> bool:
        return ticker in self._closing
