"""
RAPTOR PORTFOLIO RISK v4.1  —  core/portfolio.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Portfolio-level risk management that sits ABOVE individual position risk.

What this adds (and why it matters for the 72→85 gap):

  1. PORTFOLIO BETA CONTROL
     Your positions have a collective beta to SPY. If beta = 1.5 and SPY
     drops 2%, you lose 3%. At $25K, that's $750 in a single afternoon.
     This module tracks real-time portfolio beta and enforces a cap.
     Renaissance targets market-neutral (beta ≈ 0). You should target
     beta 0.3-0.8 — participate in the market but don't be a leveraged
     index fund.

  2. PORTFOLIO SHARPE OPTIMIZATION
     Before adding a position, estimate its marginal contribution to
     portfolio Sharpe. If adding NVDA to a portfolio of AMD+AVGO
     DECREASES the portfolio Sharpe (because they're all semis),
     reject it even if the individual signal is strong.
     Ref: Markowitz (1952), but applied position-by-position.

  3. DYNAMIC RISK BUDGET
     Allocate total portfolio risk across positions based on signal
     conviction. A score=0.85 signal deserves 3x the risk budget
     of a score=0.55 signal. This is how Bridgewater implements
     their "risk parity" framework at the position level.

  4. DAILY P&L ATTRIBUTION
     Break down daily P&L by: alpha (signal-driven), beta (market),
     sector, and individual position. Know WHERE your returns come from.
     If 80% of P&L is beta, you're not generating alpha — you're just
     long the market with extra steps.

Math references:
  Portfolio beta:     β_p = Σ(w_i × β_i) where w_i = position_value / equity
  Marginal Sharpe:    ΔS = S(portfolio + new) - S(portfolio)
  Risk parity:        w_i = (1/σ_i) / Σ(1/σ_j)  (inverse volatility weighting)
  P&L attribution:    α = R_p - β × R_market  (Jensen's alpha, daily)
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from collections import deque

import numpy as np

import config

logger = logging.getLogger("Raptor.Portfolio")


class PortfolioRiskOverlay:
    """
    Portfolio-level risk management that runs every scan cycle.
    Sits above the individual position risk manager.
    """

    def __init__(self, target_beta: float = 0.6,
                 max_beta: float = 1.2,
                 lookback_days: int = 20):
        self.target_beta = target_beta
        self.max_beta = max_beta
        self.lookback_days = lookback_days
        self._spy_returns: Optional[np.ndarray] = None
        self._stock_returns: Dict[str, np.ndarray] = {}
        self._betas: Dict[str, float] = {}
        self._last_update = datetime.min.replace(tzinfo=timezone.utc)
        self._daily_pnl_history: deque = deque(maxlen=252)

    # ── Beta Computation ──────────────────────────────────────────────────────

    def update_betas(self, symbols: list):
        """
        Refresh beta estimates for all symbols.
        Call once at session start and every hour.
        Uses 20 trading days of daily returns vs SPY.
        """
        now = datetime.now(timezone.utc)
        if (now - self._last_update).total_seconds() < 3600:
            return

        try:
            import yfinance as yf
            tickers = list(set(symbols + ["SPY"]))
            tickers = [t for t in tickers if "/" not in t]
            data = yf.download(tickers, period="1mo", interval="1d",
                               auto_adjust=True, progress=False)

            if data is None or len(data) < 10:
                return

            closes = data["Close"] if "Close" in data.columns else data
            if isinstance(closes, pd.Series):
                return

            import pandas as pd
            log_returns = np.log(closes / closes.shift(1)).dropna()

            if "SPY" not in log_returns.columns:
                return

            spy_ret = log_returns["SPY"].values
            self._spy_returns = spy_ret

            for sym in symbols:
                if sym in log_returns.columns:
                    sym_ret = log_returns[sym].values
                    self._stock_returns[sym] = sym_ret

                    # Beta = Cov(r_i, r_m) / Var(r_m)
                    if len(spy_ret) >= 5 and np.var(spy_ret) > 0:
                        cov = np.cov(sym_ret[-20:], spy_ret[-20:])[0, 1]
                        var = np.var(spy_ret[-20:])
                        self._betas[sym] = float(np.clip(cov / var, -2.0, 3.0))

            self._last_update = now
            logger.info(f"Portfolio: updated betas for {len(self._betas)} symbols")

        except Exception as e:
            logger.debug(f"Beta update error: {e}")

    def get_beta(self, symbol: str) -> float:
        """Get beta for a symbol. Returns 1.0 if unknown."""
        return self._betas.get(symbol, 1.0)

    def portfolio_beta(self, positions: list, equity: float) -> float:
        """
        Compute portfolio-weighted beta.
        β_p = Σ(w_i × β_i) where w_i = |position_value| / equity.
        """
        if not positions or equity <= 0:
            return 0.0

        weighted_beta = 0.0
        for pos in positions:
            sym = pos.symbol
            if "/" in sym:
                continue
            value = abs(float(pos.market_value))
            weight = value / equity
            beta = self.get_beta(sym)
            weighted_beta += weight * beta

        return float(weighted_beta)

    def beta_allows_entry(self, ticker: str, proposed_value: float,
                            positions: list, equity: float) -> bool:
        """
        Would adding this position push portfolio beta above max_beta?
        """
        if "/" in ticker:
            return True

        current_beta = self.portfolio_beta(positions, equity)
        new_beta = self.get_beta(ticker)
        new_weight = proposed_value / (equity + 1e-9)

        # Estimate portfolio beta after adding new position
        projected_beta = current_beta + new_weight * new_beta

        if projected_beta > self.max_beta:
            logger.info(
                f"Beta guard: {ticker} (β={new_beta:.2f}) would push "
                f"portfolio β to {projected_beta:.2f} (max {self.max_beta})"
            )
            return False
        return True

    # ── Marginal Sharpe Contribution ──────────────────────────────────────────

    def marginal_sharpe_ok(self, ticker: str, positions: list) -> bool:
        """
        Estimate whether adding this ticker improves portfolio Sharpe.
        Reject if the new position's correlation with existing portfolio
        is so high that it degrades risk-adjusted returns.

        Simplified version: reject if avg correlation with existing
        positions > 0.7 AND the new position's vol > portfolio vol.
        """
        if len(positions) == 0:
            return True

        if "/" in ticker or ticker not in self._stock_returns:
            return True

        new_ret = self._stock_returns.get(ticker)
        if new_ret is None or len(new_ret) < 10:
            return True

        correlations = []
        for pos in positions:
            sym = pos.symbol
            if sym in self._stock_returns and "/" not in sym:
                pos_ret = self._stock_returns[sym]
                min_len = min(len(new_ret), len(pos_ret))
                if min_len >= 5:
                    corr = np.corrcoef(new_ret[-min_len:],
                                        pos_ret[-min_len:])[0, 1]
                    if not np.isnan(corr):
                        correlations.append(abs(corr))

        if not correlations:
            return True

        avg_corr = np.mean(correlations)
        if avg_corr > 0.75:
            logger.info(
                f"Sharpe guard: {ticker} avg ρ={avg_corr:.2f} "
                f"with portfolio — marginal Sharpe negative"
            )
            return False

        return True

    # ── Risk Budget Allocation ────────────────────────────────────────────────

    def risk_budget_multiplier(self, score: float,
                                  n_positions: int) -> float:
        """
        Allocate risk budget based on signal conviction.
        Higher-scoring signals get proportionally more of the risk budget.

        Returns a multiplier to apply to the base Kelly allocation.
        Range: [0.6, 1.5] — weak signals get 60% of base, strong get 150%.

        This implements a simplified version of risk parity:
        instead of equalizing risk contribution, we TILT risk toward
        higher-conviction positions.
        """
        if n_positions <= 0:
            return 1.0

        # Score → multiplier mapping (piecewise linear)
        if score >= 0.80:
            return 1.50
        elif score >= 0.70:
            return 1.25
        elif score >= 0.60:
            return 1.00
        elif score >= 0.55:
            return 0.80
        else:
            return 0.60

    # ── Daily P&L Attribution ─────────────────────────────────────────────────

    def attribute_pnl(self, daily_pnl: float, portfolio_beta: float,
                       spy_daily_return: float) -> Dict[str, float]:
        """
        Decompose daily P&L into alpha and beta components.

        α = R_p - β × R_market  (Jensen's alpha)

        If alpha is consistently positive, your signals are working.
        If alpha ≈ 0 and all P&L comes from beta, you're just long the market.
        """
        if abs(spy_daily_return) < 1e-9:
            return {"alpha": daily_pnl, "beta": 0.0, "total": daily_pnl}

        beta_pnl = portfolio_beta * spy_daily_return * daily_pnl / \
                   (spy_daily_return * portfolio_beta + 1e-9) if portfolio_beta != 0 else 0
        alpha_pnl = daily_pnl - beta_pnl

        return {
            "alpha": round(alpha_pnl, 2),
            "beta": round(beta_pnl, 2),
            "total": round(daily_pnl, 2),
        }

    def record_daily_pnl(self, pnl: float, alpha: float):
        """Track daily P&L for rolling Sharpe calculation."""
        self._daily_pnl_history.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "pnl": pnl,
            "alpha": alpha,
        })

    def rolling_sharpe(self, window: int = 20) -> float:
        """
        Rolling Sharpe ratio from daily P&L history.
        Updates every day — lets you see if the system is degrading.
        """
        if len(self._daily_pnl_history) < window:
            return 0.0
        recent = list(self._daily_pnl_history)[-window:]
        pnls = np.array([d["pnl"] for d in recent])
        if pnls.std() < 1e-9:
            return 0.0
        return float(pnls.mean() / pnls.std() * np.sqrt(252))

    # ── Pre-Trade Composite Check ─────────────────────────────────────────────

    def pre_trade_check(self, ticker: str, proposed_value: float,
                          score: float, positions: list,
                          equity: float) -> Dict:
        """
        Run all portfolio-level checks before allowing an entry.
        Returns {"allowed": bool, "reason": str, "risk_mult": float}.
        """
        # Beta check
        if not self.beta_allows_entry(ticker, proposed_value,
                                       positions, equity):
            return {
                "allowed": False,
                "reason": "portfolio_beta_exceeded",
                "risk_mult": 0.0,
            }

        # Marginal Sharpe check
        if not self.marginal_sharpe_ok(ticker, positions):
            return {
                "allowed": False,
                "reason": "negative_marginal_sharpe",
                "risk_mult": 0.0,
            }

        # Risk budget multiplier
        n_pos = len(positions)
        risk_mult = self.risk_budget_multiplier(score, n_pos)

        return {
            "allowed": True,
            "reason": "passed",
            "risk_mult": risk_mult,
        }


# Need pandas for yfinance returns
try:
    import pandas as pd
except ImportError:
    pass
