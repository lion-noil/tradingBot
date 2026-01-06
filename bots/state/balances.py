# bots/state/balances.py
from __future__ import annotations
from typing import Tuple, Dict, Any

def get_total_balance_usd(wallet: Dict[str, Any]) -> float:
    return float(wallet.get("USDT") or wallet.get("USD") or 0.0)

def get_total_balance_and_ccy(wallet: Dict[str, Any]) -> Tuple[float, str]:
    if wallet is None:
        return 0.0, "USD"
    if wallet.get("USDT") not in (None, 0, "0", 0.0):
        return float(wallet.get("USDT") or 0.0), "USDT"
    if wallet.get("USD") not in (None, 0, "0", 0.0):
        return float(wallet.get("USD") or 0.0), "USD"
    return 0.0, "USD"
