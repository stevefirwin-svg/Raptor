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

# ── Process lock ──────────────────────────────────────────────────────────────
# Prevents concurrent duplicate runs. Root cause of 2026-06-19 double-orders:
# two Task Scheduler tasks (old OneDrive path + new C:\Raptor) both fired at
# 9:35:01, both fetched Alpaca positions before any fill confirmed, both saw 0
# held symbols, both submitted identical orders → CPNG/HOOD/GOOGL doubled,
# $26K margin. The all_held check was correct but lost the race.
# Fix: first process writes a lock file with its start timestamp. Any second
# process that finds a lock younger than LOCK_TTL_SECONDS exits immediately.
LOCK_FILE        = "logs/raptor_scan.lock"
LOCK_TTL_SECONDS = 30 * 60  # 30 min — longer than any normal scan

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


# ── Process lock helpers ──────────────────────────────────────────────────────

def _acquire_lock() -> bool:
    """
    Write a timestamp lock file. Returns False if a younger lock exists
    (caller must abort); True if lock acquired or file-write failed (fail open —
    don't block trading over a permissions issue).
    """
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    if os.path.exists(LOCK_FILE):
        try:
            age = (datetime.now() - datetime.fromisoformat(open(LOCK_FILE).read().strip())).total_seconds()
            if age < LOCK_TTL_SECONDS:
                logger.warning(
                    "LOCK: another scan is already running (lock age %.0fs < %ds TTL). "
                    "Aborting this instance to prevent duplicate orders. "
                    "If stale, delete: %s",
                    age, LOCK_TTL_SECONDS, os.path.abspath(LOCK_FILE),
                )
                return False
            logger.warning("LOCK: stale lock (age %.0fs) — overwriting.", age)
        except Exception as e:
            logger.warning("LOCK: could not read lock file (%s) — overwriting.", e)
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(datetime.now().isoformat())
        return True
    except Exception as e:
        logger.warning("LOCK: could not write lock file (%s) — proceeding without lock.", e)
        return True


def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception as e:
        logger.warning("LOCK: could not remove lock file (%s).", e)


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
    if not cache:
        return signals, {}
    passed, velocities = [], {}
    for s in signals:
        yesterday = cache.get(s.symbol, None)
        if yesterday is None:
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
    if not os.path.exists(COOLDOWN_LOG_PATH):
        return {}
    try:
        with open(COOLDOWN_LOG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cooldown_log(cooldowns: dict):
    tmp = COOLDOWN_LOG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cooldowns, f, indent=2)
    os.replace(tmp, COOLDOWN_LOG_PATH)


def _cooldown_filter(signals, cooldown_days=5, min_snr=0.8):
    cooldowns = _load_cooldown_log()
    today = date.today()
    cutoff = today - timedelta(days=cooldown_days)
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
            if s.t_statistic < min_snr:
                logger.info("COOLDOWN GATE [skip] %s cooldown expired but SNR=%.3f < %.1f — not re-qualified",
                           s.symbol, s.t_statistic, min_snr)
                continue
            logger.info("COOLDOWN GATE [pass] %s cooldown expired + SNR=%.3f re-qualified",
                       s.symbol, s.t_statistic)
        passed.append(s)
    if active != cooldowns:
        _save_cooldown_log(active)
    return passed


def _record_stopout_cooldown(symbol: str):
    cooldowns = _load_cooldown_log()
    cooldowns[symbol] = date.today().strftime("%Y-%m-%d")
    _save_cooldown_log(cooldowns)
    logger.info("COOLDOWN: %s added — blocked for 5 trading days", symbol)


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_daily_scan():
    if not _acquire_lock():
        sys.exit(0)  # Clean exit — not a crash, yields to the already-running instance
    try:
        _run_scan()
    finally:
        _release_lock()


def _run_scan():
    logger.info("=" * 60)
    logger.info("RAPTOR %s DAILY SCAN - %s", MODEL_ID, datetime.now().isoformat())
    logger.info("=" * 60)
    CONFIG.validate_all()

    dm = DataManager(CONFIG)
    ledger = Ledger()
    positions = dm.alpaca.get_positions()
    account   = dm.alpaca.get_account()

    # Source of truth is Alpaca, not ledger
    all_held = {p["symbol"] for p in positions}
    current_position_count = len(positions)

    _cap      = CONFIG.risk.equity_cap
    my_equity = account["equity"] if _cap is None else min(account["equity"], _cap)

    logger.info("Account: equity=$%.2f  cap=%s  sizing_base=$%.2f  positions=%d",
                account["equity"],
                "none (compound)" if _cap is None else f"${_cap:,.0f}",
                my_equity, current_position_count)

    # ── Market Agent gate ─────────────────────────────────────────────────────
    try:
        from market_agent import load_market_decision
        market_decision = load_market_decision()
        session_mode    = market_decision.get("decision", "SCAN")
        risk_scalar     = float(market_decision.get("risk_scalar", 1.0))
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

    # ── Margin guard ──────────────────────────────────────────────────────────
    _mg_allowed, _mg_max_new, _mg_reason = check_margin_safety(dm)
    if not _mg_allowed:
        logger.warning("MARGIN GUARD BLOCK: %s — skipping entry scan", _mg_reason)
        return
    if _mg_max_new < CONFIG.execution.max_orders_per_scan:
        logger.warning("MARGIN GUARD REDUCE: capping new entries at %d — %s", _mg_max_new, _mg_reason)

    # ── Universe ──────────────────────────────────────────────────────────────
    try:
        from universe_builder import UniverseBuilder
        ub       = UniverseBuilder(CONFIG)
        universe = ub.build(max_symbols=150)
        logger.info("Dynamic universe: %d symbols", len(universe))
    except Exception as e:
        logger.error("Universe builder failed (%s) — aborting scan.", e)
        return
    if "SPY" not in universe:
        universe.append("SPY")

    dataset        = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
    bars, macro, sentiment = dataset["bars"], dataset["macro"], dataset["sentiment"]
    spy_bars       = bars.get("SPY")

    # P0-8: regime label override
    try:
        import json as _mcj
        _mc    = _mcj.loads(open("macro_context.json").read())
        _label = _mc.get("macro_regime") or _mc.get("regime")
        if _label in ("RISK_ON", "NEUTRAL", "RISK_OFF", "CRISIS"):
            macro["regime"] = _label
            logger.info("[MacroOverride] regime → %s (from macro_context.json)", _label)
        else:
            logger.warning("[MacroOverride] unrecognised label %r — keeping data_feeds value", _label)
    except Exception as _mce:
        logger.warning("[MacroOverride] Could not load macro_context.json (%s)", _mce)

    logger.info("Data: %d symbols | Macro: %s (%.3f)",
                len(bars), macro.get("regime","?"),
                macro.get("macro_score", macro.get("score", 0.0)))

    engine  = QuantSignalEngine(CONFIG)
    signals = engine.generate_signals(bars, macro, sentiment, spy_bars)
    signals = [s for s in signals if s.symbol not in all_held]

    if not signals:
        logger.info("No signals today. Patience.")
        return

    # ── Velocity gate ─────────────────────────────────────────────────────────
    comp_cache          = _load_composite_cache()
    signals, velocities = _velocity_filter(signals, comp_cache)
    if not signals:
        logger.info("All signals filtered by velocity gate. Patience.")
        return
    logger.info("Velocity gate: %d signals remain | velocities: %s",
               len(signals), {s: f"{v:+.3f}" if v is not None else "new"
                              for s, v in list(velocities.items())[:5]})

    # ── Cooldown gate ─────────────────────────────────────────────────────────
    signals = _cooldown_filter(signals)
    if not signals:
        logger.info("All signals blocked by cooldown gate.")
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
        before  = len(signals)
        signals = [s for s in signals if entry_passes(s.symbol, agent_decisions)]
        vetoed  = before - len(signals)
        if vetoed:
            logger.info("EntryAgent vetoed %d candidate(s).", vetoed)
    except Exception as e:
        logger.warning("EntryAgent unavailable (%s) — proceeding without screening.", e)

    # ── Capital deployment ────────────────────────────────────────────────────
    max_my_positions = CONFIG.risk.max_positions
    _cash      = float(account.get("cash", 0))
    _total_mv  = sum(abs(float(p.get("qty", 0))) * float(p.get("current_price", 0)) for p in positions)
    _cash_avail = max(0.0, _cash * 0.95)
    if CONFIG.risk.allow_margin:
        available_capital = float(account.get("buying_power", 0)) * 0.95
        _headroom_log = available_capital
    elif _cap is None:
        available_capital = _cash_avail
        _headroom_log     = available_capital
    else:
        _cap_headroom     = max(0.0, _cap - _total_mv)
        available_capital = min(_cash_avail, _cap_headroom)
        _headroom_log     = _cap_headroom
    logger.info(
        "Deployable: cap=%s  market_value=$%.0f  cash=$%.0f  headroom=$%.0f  available=$%.0f  (margin=%s)",
        "none" if _cap is None else f"${_cap:,.0f}",
        _total_mv, _cash, _headroom_log, available_capital,
        "ON" if CONFIG.risk.allow_margin else "OFF",
    )

    placed = 0
    for sig in signals:
        if current_position_count + placed >= max_my_positions:
            break
        if placed >= _mg_max_new:
            logger.info("SKIP %s — margin guard cap reached (%d new positions)", sig.symbol, _mg_max_new)
            break
        shares     = int((my_equity * sig.kelly_fraction) / sig.entry_price)
        if shares < 1:
            continue
        order_cost = shares * sig.entry_price
        if available_capital < order_cost:
            logger.info("SKIP %s — insufficient deployable capital ($%.2f needed, $%.2f available; no margin)",
                        sig.symbol, order_cost, available_capital)
            continue
        limit  = (round(sig.entry_price + sig.entry_price * CONFIG.execution.limit_offset_bps / 10000, 2)
                  if CONFIG.execution.order_type == "limit" else None)
        result = dm.alpaca.submit_order(sig.symbol, shares, "BUY", CONFIG.execution.order_type, limit)
        if "error" not in result:
            placed            += 1
            available_capital -= order_cost
            ledger.record_entry(MODEL_ID, sig.symbol, shares, sig.entry_price,
                                datetime.now().strftime("%Y-%m-%d"),
                                {"t_stat": sig.t_statistic, "stop": sig.stop_price,
                                 "tp": sig.take_profit, "regime": sig.regime,
                                 "kelly_fraction": sig.kelly_fraction,
                                 "composite_score": sig.composite_score,
                                 "velocity": velocities.get(sig.symbol)})
            logger.info("ORDER [%s]: BUY %d %s @ $%.2f", MODEL_ID, shares, sig.symbol, limit or sig.entry_price)
            try:
                from slippage_tracker import record_fill as _record_fill
                _record_fill(symbol=sig.symbol, side="BUY", qty=shares,
                             decision_price=sig.entry_price, order_result=result)
            except Exception as _se:
                logger.warning("Slippage log failed for %s: %s", sig.symbol, _se)
        else:
            logger.error("FAILED: %s - %s", sig.symbol, result["error"])

    logger.info("Scan complete. %d orders placed for %s.", placed, MODEL_ID)


if __name__ == "__main__":
    try:
        run_daily_scan()
    except SystemExit:
        raise
    except BaseException:
        logging.getLogger("raptor.main").exception("FATAL: uncaught exception — daily scan aborted")
        sys.exit(1)
