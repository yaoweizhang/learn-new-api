"""In-memory quota store.

Real implementation uses SQLite (s09 onward) + transactional deduction.
This chapter uses an in-process dict with a lock to keep the math obvious.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_balances: dict[str, int] = {}


def reset() -> None:
    with _lock:
        _balances.clear()


def set_balance(user_id: str, amount: int) -> None:
    with _lock:
        _balances[user_id] = amount


def get_balance(user_id: str) -> int:
    with _lock:
        return _balances.get(user_id, 0)


def deduct(user_id: str, amount: int) -> bool:
    """Atomic conditional deduction. Returns True on success, False if insufficient."""
    if amount < 0:
        raise ValueError("deduct amount must be >= 0")
    with _lock:
        bal = _balances.get(user_id, 0)
        if bal < amount:
            return False
        _balances[user_id] = bal - amount
        return True


def refund(user_id: str, amount: int) -> None:
    if amount < 0:
        raise ValueError("refund amount must be >= 0")
    with _lock:
        _balances[user_id] = _balances.get(user_id, 0) + amount


def settle(user_id: str, pre_deducted: int, actual: int) -> int:
    """Settle the difference between pre-deducted and actual cost.

    If actual < pre_deducted: refund the unused portion.
    If actual > pre_deducted: charge the overage.
    Returns the actual amount charged.
    """
    diff = actual - pre_deducted
    if diff > 0:
        # Actual exceeded the pre-consume — charge the overage. If the user
        # has insufficient balance for the overage, deduct silently fails
        # (the user is now in the red; real impl surfaces a billing exception).
        deduct(user_id, diff)
    elif diff < 0:
        refund(user_id, -diff)
    return actual
