"""Raptor v5.2 Diagnostics — python diagnose.py"""
import logging
logging.basicConfig(level=logging.WARNING)

from config import CONFIG
from data_feeds import DataManager
from signals import QuantSignalEngine

dm = DataManager(CONFIG)
universe = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "AMD", "CRM", "NFLX", "ADBE", "PYPL", "SQ", "SHOP",
    "UBER", "ABNB", "COIN", "SNOW", "DDOG", "NET",
    "JPM", "BAC", "GS", "MS", "V", "MA",
    "XOM", "CVX", "LLY", "UNH", "JNJ", "PFE",
    "CAT", "DE", "BA", "RTX", "LMT", "GE",
    "HD", "LOW", "TGT", "WMT", "COST", "NKE",
    "DIS", "CMCSA", "SPY",
]
dataset = dm.get_full_dataset(universe, lookback_days=CONFIG.signals.lookback_days)
engine = QuantSignalEngine(CONFIG)
print(engine.get_diagnostics(dataset["bars"], dataset["macro"],
                              dataset["sentiment"], dataset["bars"].get("SPY")))
