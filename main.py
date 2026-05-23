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

    if not signals:
        logger.info("No signals today. Patience.")
        return

    logger.info("Signals:")
    for i, s in enumerate(signals):
        logger.info("  %d. [%s] %s  t=%.3f  pctl=%.0f%%  entry=$%.2f  stop=$%.2f  tp=$%.2f  kelly=%.3f  hold~%dd  pattern=%s",
                    i+1, s.trade_type[:3], s.symbol, s.t_statistic, s.composite_percentile*100,
                    s.entry_price, s.stop_price, s.take_profit, s.kelly_fraction, s.hold_target_days,
                    getattr(s, "pattern_signal", "") or "none")

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
                                {"t_stat":      sig.t_statistic,
                                 "stop":        sig.stop_price,
                                 "tp":          sig.take_profit,
                                 "regime":      sig.regime,
                                 "trade_type":  sig.trade_type,
                                 "pattern":     getattr(sig, "pattern_signal", ""),
                                 "conviction":  round(getattr(sig, "book_conviction", 0.0), 4)})
            logger.info("ORDER [%s|%s]: BUY %d %s @ $%.2f  pattern=%s  conviction=%.3f",
                        MODEL_ID, sig.trade_type, shares, sig.symbol,
                        limit or sig.entry_price,
                        getattr(sig, "pattern_signal", "") or "none",
                        getattr(sig, "book_conviction", 0.0))
            # ── Per-book trade log ────────────────────────────────────────────
            _book_log = logging.getLogger(
                "raptor.momentum" if sig.trade_type == "MOMENTUM" else "raptor.mean_reversion"
            )
            _book_log.info("ENTRY %s @ $%.2f  stop=$%.2f  tp=$%.2f  pattern=%s  conviction=%.3f  hold~%dd",
                           sig.symbol, sig.entry_price, sig.stop_price, sig.take_profit,
                           getattr(sig, "pattern_signal", "") or "none",
                           getattr(sig, "book_conviction", 0.0), sig.hold_target_days)
        else:
            logger.error("FAILED: %s - %s", sig.symbol, result["error"])

    logger.info("Scan complete. %d orders placed for %s.", placed, MODEL_ID)


if __name__ == "__main__":
    run_daily_scan()
