"""Raptor v5.2 — Daily Scan Runner (with A/B ledger tracking)"""
import logging, os, sys
from datetime import datetime
from config import CONFIG
from data_feeds import DataManager
from signals import QuantSignalEngine
from ledger import Ledger
from margin_guard import check_margin_safety

MODEL_ID = "v5.4"
EQUITY_ALLOCATION = 1.00  # Full account — v6 removed

os.makedirs(CONFIG.log.log_dir, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, CONFIG.log.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(CONFIG.log.log_dir, f"raptor_{datetime.now():%Y%m%d}.log")),
    ],
)
logger = logging.getLogger("raptor.main")


def _get_cooldown_symbols(cooldown_days: int = 5) -> set:
    """
    GAP 6 — Re-entry cooldown after stop-out.

    Returns set of symbols that were stopped out within the last `cooldown_days`
    trading days and are therefore blocked from re-entry.

    A stop-out on a momentum-driven decline means the thesis failed. Re-entering
    the next day if the symbol still ranks in top-N burns capital twice on the
    same failing trade. Math must re-qualify the thesis via cooldown + re-scoring.

    Sources (checked in order of reliability):
      1. outcome_log.json — exit_path=hard_stop/trail_loss in last N days
      2. position_ledger.json closed — exit_reason containing stop/trail in last N days

    Only hard stops and trail_loss exits trigger cooldown.
    Profitable trail exits (trail_profit) do NOT — the thesis worked, re-entry ok.
    """
    import json as _j
    from datetime import date as _date, timedelta as _td

    cooldown_symbols = set()
    cutoff = _date.today() - _td(days=cooldown_days)

    stop_paths = {"hard_stop", "trail_loss", "trailing_stop"}

    # Source 1: outcome_log.json
    try:
        if os.path.exists("outcome_log.json"):
            with open("outcome_log.json") as f:
                records = _j.load(f)
            for r in records:
                exit_path = r.get("actual_exit_path", "")
                exit_date = r.get("exit_date", "")
                if exit_path in stop_paths and exit_date:
                    try:
                        ed = datetime.strptime(exit_date[:10], "%Y-%m-%d").date()
                        if ed >= cutoff:
                            cooldown_symbols.add(r["symbol"])
                    except Exception:
                        pass
    except Exception:
        pass

    # Source 2: position_ledger.json closed trades
    try:
        if os.path.exists("position_ledger.json"):
            with open("position_ledger.json") as f:
                ledger_data = _j.load(f)
            for trade in ledger_data.get("closed", []):
                reason = trade.get("exit_reason", "")
                exit_date = trade.get("exit_date", "")
                if any(p in reason for p in ["hard_stop", "trail_loss", "trailing_stop"]) and exit_date:
                    try:
                        ed = datetime.strptime(exit_date[:10], "%Y-%m-%d").date()
                        if ed >= cutoff:
                            cooldown_symbols.add(trade["symbol"])
                    except Exception:
                        pass
    except Exception:
        pass

    return cooldown_symbols


def _get_composite_velocity(symbol: str, lookback_days: int = 3) -> float:
    """
    GAP 5 — Composite velocity: rate of change of composite score.

    composite_velocity = composite_today - composite_{N days ago}

    Positive = accelerating (signal strengthening) — priority entry, size up.
    Negative = decelerating (signal fading) — defer or size down.

    Reads hold_history.json snapshots if the symbol has recent history.
    Falls back to 0.0 (neutral) if no history available — new entries without
    history are not penalized, just not boosted.

    Only used as a sizing modifier, not a hard gate — ensures we don't block
    valid entries just because the symbol has no hold_history yet.
    """
    import json as _j
    try:
        if not os.path.exists("hold_history.json"):
            return 0.0
        with open("hold_history.json") as f:
            hh = _j.load(f)
        snaps = hh.get("positions", {}).get(symbol, {}).get("snapshots", [])
        if len(snaps) < 2:
            return 0.0
        # Sort by timestamp, get last and Nth-last
        snaps_sorted = sorted(snaps, key=lambda s: s.get("timestamp", ""))
        latest = snaps_sorted[-1].get("composite", 0.0)
        prior_idx = max(0, len(snaps_sorted) - 1 - lookback_days)
        prior = snaps_sorted[prior_idx].get("composite", 0.0)
        return round(float(latest - prior), 4)
    except Exception:
        return 0.0


def run_daily_scan():
    logger.info("=" * 60)
    logger.info("RAPTOR %s DAILY SCAN - %s", MODEL_ID, datetime.now().isoformat())
    logger.info("=" * 60)
    CONFIG.validate_all()

    dm = DataManager(CONFIG)
    ledger = Ledger()
    positions = dm.alpaca.get_positions()
    account = dm.alpaca.get_account()

    # Source of truth is Alpaca, not ledger
    all_held = {p["symbol"] for p in positions}
    current_position_count = len(positions)
    my_equity = account["equity"] * EQUITY_ALLOCATION

    logger.info("Account: equity=$%.2f  my_allocation=$%.2f  positions=%d",
                account["equity"], my_equity, current_position_count)

    # ── Market Agent gate — Layer 4 ───────────────────────────────────────────
    try:
        from market_agent import load_market_decision
        market_decision = load_market_decision()
        session_mode   = market_decision.get("decision", "SCAN")
        risk_scalar    = float(market_decision.get("risk_scalar", 1.0))
        logger.info("[MarketAgent] %s | scalar=%.2f | %s",
                    session_mode, risk_scalar, market_decision.get("reasoning", ""))
        if session_mode == "STANDBY":
            logger.warning("[MarketAgent] STANDBY — skipping entry scan entirely.")
            return
        if session_mode == "REDUCE":
            my_equity *= risk_scalar
            logger.info("[MarketAgent] REDUCE — equity allocation scaled to $%.2f", my_equity)
    except Exception as e:
        logger.warning("[MarketAgent] Unavailable (%s) — proceeding with full scan.", e)
        risk_scalar = 1.0
    # ─────────────────────────────────────────────────────────────────────────

    # ── Margin guard — block or reduce entries when overleveraged ─────────────
    _mg_allowed, _mg_max_new, _mg_reason = check_margin_safety(dm)
    if not _mg_allowed:
        logger.warning("MARGIN GUARD BLOCK: %s — skipping entry scan", _mg_reason)
        return
    if _mg_max_new < CONFIG.execution.max_orders_per_scan:
        logger.warning("MARGIN GUARD REDUCE: capping new entries at %d — %s", _mg_max_new, _mg_reason)
    # ─────────────────────────────────────────────────────────────────────────

    # Dynamic universe
    try:
        from universe_builder import UniverseBuilder
        ub = UniverseBuilder(CONFIG)
        universe = ub.build(max_symbols=150)
        logger.info("Dynamic universe: %d symbols", len(universe))
    except Exception as e:
        logger.warning("Universe builder failed (%s), using core list", e)
        universe = [
            "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
            "AMD", "CRM", "NFLX", "ADBE", "PYPL", "SQ", "SHOP",
            "UBER", "ABNB", "COIN", "SNOW", "DDOG", "NET",
            "JPM", "BAC", "GS", "MS", "V", "MA",
            "XOM", "CVX", "LLY", "UNH", "JNJ", "PFE",
            "CAT", "DE", "BA", "RTX", "LMT", "GE",
            "HD", "LOW", "TGT", "WMT", "COST", "NKE",
            "DIS", "CMCSA",
        ]
    if "SPY" not in universe:
        universe.append("SPY")

    dataset = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
    bars, macro, sentiment = dataset["bars"], dataset["macro"], dataset["sentiment"]
    spy_bars = bars.get("SPY")

    logger.info("Data: %d symbols | Macro: %s (%.3f)",
                len(bars), macro["regime"], macro["score"])

    engine = QuantSignalEngine(CONFIG)
    signals = engine.generate_signals(bars, macro, sentiment, spy_bars)
    signals = [s for s in signals if s.symbol not in all_held]

    # GAP 6 — Re-entry cooldown: block symbols stopped out in last 5 days
    _cooldown = _get_cooldown_symbols(cooldown_days=5)
    if _cooldown:
        before_cd = len(signals)
        signals = [s for s in signals if s.symbol not in _cooldown]
        blocked = before_cd - len(signals)
        if blocked:
            logger.info("COOLDOWN: blocked %d symbol(s) stopped out within 5 days: %s",
                       blocked, sorted(_cooldown & {s.symbol for s in signals} | _cooldown))

    if not signals:
        logger.info("No signals today. Patience.")
        return

    logger.info("Signals:")
    for i, s in enumerate(signals):
        logger.info("  %d. %s  t=%.3f  pctl=%.0f%%  entry=$%.2f  stop=$%.2f  tp=$%.2f  kelly=%.3f  hold~%dd",
                    i+1, s.symbol, s.t_statistic, s.composite_percentile*100,
                    s.entry_price, s.stop_price, s.take_profit, s.kelly_fraction, s.hold_target_days)

    if not CONFIG.execution.paper_trading:
        logger.critical("PAPER TRADING OFF - refusing to execute")
        return

    # ── Entry Agent screening ─────────────────────────────────────────────────
    try:
        from agent_layer import run_entry_screening, entry_passes
        candidates = [{
            "symbol":                  s.symbol,
            "composite_score":         round(s.composite_score, 4),
            "score_rank":              i + 1,
            "regime":                  s.regime,
            "kelly_fraction":          round(s.kelly_fraction, 4),
            "market_momentum_scalar":  round(getattr(s, "market_momentum_scalar", 1.0), 4),
            "atr_pct":                 round(getattr(s, "atr_pct", 2.0), 4),
            "days_since_earnings":     int(getattr(s, "days_since_earnings", 30)),
            "vix_regime":              macro.get("vix_regime", "NORMAL"),
            "macro_regime":            macro.get("regime", "NEUTRAL"),
        } for i, s in enumerate(signals)]
        agent_decisions = run_entry_screening(candidates)
        before = len(signals)
        signals = [s for s in signals if entry_passes(s.symbol, agent_decisions)]
        vetoed  = before - len(signals)
        if vetoed:
            logger.info("EntryAgent vetoed %d candidate(s).", vetoed)
    except Exception as e:
        logger.warning("EntryAgent unavailable (%s) — proceeding without screening.", e)
    # ─────────────────────────────────────────────────────────────────────────

    max_my_positions = CONFIG.risk.max_positions  # Full slots — v6 removed
    # Use buying_power not cash — cash is negative when on margin but buying_power
    # reflects actual Alpaca-enforced available capital including margin limits.
    # Cap available_bp at 95% to preserve a 5% buffer.
    available_bp = float(account.get("buying_power", 0)) * 0.95
    placed = 0
    for sig in signals:
        if current_position_count + placed >= max_my_positions:
            break
        if placed >= _mg_max_new:
            logger.info("SKIP %s — margin guard cap reached (%d new positions)", sig.symbol, _mg_max_new)
            break

        # GAP 5 — Composite velocity sizing modifier
        # Entry is based on composite score at a single point in time. A stock
        # accelerating (composite rising day over day) is a far better entry than
        # one decelerating toward the threshold — same score, very different trajectory.
        # velocity = composite_today - composite_3d_ago from hold_history.json
        # Modifier scales kelly continuously: +0.2 per unit velocity, capped at ±20%.
        # Decelerating signals near threshold are sized smaller, not blocked.
        comp_vel = _get_composite_velocity(sig.symbol, lookback_days=3)
        vel_modifier = max(0.80, min(1.20, 1.0 + comp_vel * 0.2))
        effective_kelly = sig.kelly_fraction * vel_modifier

        # Size from MY allocation, not full account
        shares = int((my_equity * effective_kelly) / sig.entry_price)
        if shares < 1:
            continue
        # Buying power guard — ensures Alpaca won't reject the order
        order_cost = shares * sig.entry_price
        if available_bp < order_cost:
            logger.info("SKIP %s — insufficient buying power ($%.2f needed, $%.2f available)", sig.symbol, order_cost, available_bp)
            continue
        if CONFIG.execution.order_type == "limit":
            limit = round(sig.entry_price + sig.entry_price * CONFIG.execution.limit_offset_bps / 10000, 2)
        else:
            limit = None
        result = dm.alpaca.submit_order(sig.symbol, shares, "BUY", CONFIG.execution.order_type, limit)
        if "error" not in result:
            placed += 1
            available_bp -= order_cost
            ledger.record_entry(MODEL_ID, sig.symbol, shares, sig.entry_price,
                                datetime.now().strftime("%Y-%m-%d"),
                                {"t_stat": sig.t_statistic, "stop": sig.stop_price,
                                 "tp": sig.take_profit, "regime": sig.regime,
                                 "composite_score": sig.composite_score,
                                 "composite_velocity": comp_vel,
                                 "kelly_fraction": round(effective_kelly, 4)})
            logger.info("ORDER [%s]: BUY %d %s @ $%.2f  vel=%.3f  kelly_adj=%.3f",
                       MODEL_ID, shares, sig.symbol, limit or sig.entry_price,
                       comp_vel, effective_kelly)
        else:
            logger.error("FAILED: %s - %s", sig.symbol, result["error"])

    logger.info("Scan complete. %d orders placed for %s.", placed, MODEL_ID)


if __name__ == "__main__":
    run_daily_scan()
