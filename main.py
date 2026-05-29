"""Raptor v5.2 — Daily Scan Runner (with A/B ledger tracking)"""
import json, logging, os, sys
from datetime import datetime, date, timedelta
from config import CONFIG
from data_feeds import DataManager
from signals import QuantSignalEngine
from ledger import Ledger
from margin_guard import check_margin_safety

MODEL_ID = "v5.4"

COMPOSITE_CACHE_PATH = "composite_cache.json"
COOLDOWN_LOG_PATH    = "cooldown_log.json"

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


# ── Velocity gate ─────────────────────────────────────────────────────────────

def _load_composite_cache() -> dict:
    """Load yesterday's composite scores. Returns {} if missing (first run)."""
    if not os.path.exists(COMPOSITE_CACHE_PATH):
        return {}
    try:
        with open(COMPOSITE_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _velocity_filter(signals, cache, min_velocity=-0.15):
    """
    Filter signals by composite velocity = today - yesterday.
    Removes signals where the score is decelerating below min_velocity.
    Accelerating signals (velocity > 0) are kept unconditionally.
    Flat signals (abs(velocity) < 0.05) are kept — no data to condemn them.
    Decelerating signals (velocity < min_velocity) are skipped.

    min_velocity=-0.15: a signal must not have dropped more than 0.15
    composite points day-over-day to qualify for entry. This prevents
    entering a position that scored well yesterday but is already fading.
    If cache is empty (first run), all signals pass — no data = no veto.
    """
    if not cache:
        return signals, {}

    passed, velocities = [], {}
    for s in signals:
        yesterday = cache.get(s.symbol, None)
        if yesterday is None:
            # Not in cache — new to universe, pass through
            velocities[s.symbol] = None
            passed.append(s)
            continue
        velocity = s.composite_score - yesterday
        velocities[s.symbol] = round(velocity, 4)
        if velocity < min_velocity:
            logger.info("VELOCITY GATE [skip] %s velocity=%.3f (today=%.3f yesterday=%.3f) — decelerating",
                       s.symbol, velocity, s.composite_score, yesterday)
        else:
            passed.append(s)
    return passed, velocities


# ── Cooldown gate ─────────────────────────────────────────────────────────────

def _load_cooldown_log() -> dict:
    """Load active cooldowns {symbol: stop_out_date_str}."""
    if not os.path.exists(COOLDOWN_LOG_PATH):
        return {}
    try:
        with open(COOLDOWN_LOG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cooldown_log(cooldowns: dict):
    """Atomic write of cooldown log."""
    tmp = COOLDOWN_LOG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cooldowns, f, indent=2)
    os.replace(tmp, COOLDOWN_LOG_PATH)


def _cooldown_filter(signals, cooldown_days=5, min_snr=0.8):
    """
    Block re-entry for symbols stopped out within the last cooldown_days.
    After cooldown expires, also requires SNR > min_snr to re-qualify.

    Reads cooldown_log.json {symbol: stop_out_date}.
    Expired entries are pruned on each run (keeps file small).
    stop-out events are logged by exit_monitor — but we also check here
    against the ledger closed trades as a secondary source.

    min_snr=0.8: the signal must show genuine conviction to re-enter
    after a stop-out, not just barely clear the composite > 0 threshold.
    """
    cooldowns = _load_cooldown_log()
    today = date.today()
    cutoff = today - timedelta(days=cooldown_days)

    # Prune expired cooldowns
    active = {sym: d for sym, d in cooldowns.items()
              if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff}

    passed = []
    for s in signals:
        if s.symbol in active:
            stop_date = datetime.strptime(active[s.symbol], "%Y-%m-%d").date()
            days_since = (today - stop_date).days
            if days_since < cooldown_days:
                logger.info("COOLDOWN GATE [skip] %s stopped out %s (%d days ago, need %d)",
                           s.symbol, active[s.symbol], days_since, cooldown_days)
                continue
            # Cooldown expired — check SNR re-qualification
            if s.t_statistic < min_snr:
                logger.info("COOLDOWN GATE [skip] %s cooldown expired but SNR=%.3f < %.1f — not re-qualified",
                           s.symbol, s.t_statistic, min_snr)
                continue
            logger.info("COOLDOWN GATE [pass] %s cooldown expired + SNR=%.3f re-qualified",
                       s.symbol, s.t_statistic)
        passed.append(s)

    # Save pruned cooldown log
    if active != cooldowns:
        _save_cooldown_log(active)

    return passed


def _record_stopout_cooldown(symbol: str):
    """Called by exit_monitor indirectly — or directly here after a stop-out fill."""
    cooldowns = _load_cooldown_log()
    cooldowns[symbol] = date.today().strftime("%Y-%m-%d")
    _save_cooldown_log(cooldowns)
    logger.info("COOLDOWN: %s added — blocked for 5 trading days", symbol)


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
    my_equity = account["equity"]  # H-7: EQUITY_ALLOCATION=1.00 removed — was a no-op dead variable

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
        # No static fallback — a fabricated universe violates Rule 3.
        # If universe_builder fails, the filter criteria are unknown; any
        # signals generated would be from an uncalibrated symbol set.
        # Abort and fix the underlying error.
        logger.error("Universe builder failed (%s) — aborting scan. Fix universe_builder before next run.", e)
        return
    if "SPY" not in universe:
        universe.append("SPY")

    dataset = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
    bars, macro, sentiment = dataset["bars"], dataset["macro"], dataset["sentiment"]
    spy_bars = bars.get("SPY")

    # P0-8: override macro["regime"] with canonical {RISK_ON,NEUTRAL,RISK_OFF,CRISIS}
    # label from macro_context.json. data_feeds.compute_regime_score uses a different
    # taxonomy (BULLISH/BEARISH) — without this override the EntryAgent veto rules
    # for RISK_OFF/CRISIS can never fire.
    try:
        import json as _mcj
        _mc = _mcj.loads(open("macro_context.json").read())
        _label = _mc.get("macro_regime") or _mc.get("regime")
        if _label in ("RISK_ON", "NEUTRAL", "RISK_OFF", "CRISIS"):
            macro["regime"] = _label
            logger.info("[MacroOverride] regime → %s (from macro_context.json)", _label)
        else:
            logger.warning("[MacroOverride] macro_context.json has unrecognised label %r — keeping data_feeds value", _label)
    except Exception as _mce:
        logger.warning("[MacroOverride] Could not load macro_context.json (%s) — using data_feeds regime", _mce)

    logger.info("Data: %d symbols | Macro: %s (%.3f)",
                len(bars), macro.get("regime","?"),
                macro.get("macro_score", macro.get("score", 0.0)))

    engine = QuantSignalEngine(CONFIG)
    signals = engine.generate_signals(bars, macro, sentiment, spy_bars)
    signals = [s for s in signals if s.symbol not in all_held]

    if not signals:
        logger.info("No signals today. Patience.")
        return

    # ── Velocity gate ─────────────────────────────────────────────────────────
    # Skip signals where composite score is decelerating > 0.15 points day-over-day.
    # Prevents entering a position that scored well yesterday but is already fading.
    comp_cache = _load_composite_cache()
    signals, velocities = _velocity_filter(signals, comp_cache)
    if not signals:
        logger.info("All signals filtered by velocity gate. Patience.")
        return
    logger.info("Velocity gate: %d signals remain | velocities: %s",
               len(signals), {s: f"{v:+.3f}" if v is not None else "new"
                              for s, v in list(velocities.items())[:5]})

    # ── Cooldown gate ─────────────────────────────────────────────────────────
    # Block re-entry for symbols stopped out within last 5 trading days.
    # Expired cooldowns require SNR > 0.8 to re-qualify.
    signals = _cooldown_filter(signals)
    if not signals:
        logger.info("All signals blocked by cooldown gate.")
        return
    # ─────────────────────────────────────────────────────────────────────────

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
        # Size from MY allocation, not full account
        shares = int((my_equity * sig.kelly_fraction) / sig.entry_price)
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
                                 "kelly_fraction": sig.kelly_fraction,
                                 "composite_score": sig.composite_score,
                                 "velocity": velocities.get(sig.symbol)})
            logger.info("ORDER [%s]: BUY %d %s @ $%.2f", MODEL_ID, shares, sig.symbol, limit or sig.entry_price)
        else:
            logger.error("FAILED: %s - %s", sig.symbol, result["error"])

    logger.info("Scan complete. %d orders placed for %s.", placed, MODEL_ID)


if __name__ == "__main__":
    run_daily_scan()
