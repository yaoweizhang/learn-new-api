"""In-memory log store abstraction with async flush.

Defines the LogStore Protocol and an InMemoryLogStore default. Module-level
helpers (`enqueue`, `list_logs`, `reset_logs`, `_drain_now`, `flush_loop`)
are thin wrappers that delegate to a module-level default instance — existing
call sites stay green.

Why a Protocol rather than an ABC: tests and adapters only need duck-typed
members; Protocol keeps the seam lightweight without forcing inheritance.
The per-instance lock lets future SQLite impl + test isolation coexist.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Protocol


class LogStore(Protocol):
    def enqueue(self, entry: dict) -> None: ...
    def list(self) -> list[dict]: ...
    def reset(self) -> None: ...
    def drain_now(self) -> None: ...


class InMemoryLogStore:
    def __init__(self, flush_interval: float = 0.1) -> None:
        self._lock = threading.Lock()
        self._buffer: deque[dict] = deque()
        self._flushed: list[dict] = []
        self._flush_interval = flush_interval
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    def enqueue(self, entry: dict) -> None:
        with self._lock:
            self._buffer.append(entry)

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._flushed.clear()

    def drain_now(self) -> None:
        with self._lock:
            while self._buffer:
                self._flushed.append(self._buffer.popleft())

    def list(self) -> list[dict]:
        with self._lock:
            return list(self._flushed)

    async def flush_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(self._flush_interval)
            self.drain_now()


_default: LogStore = InMemoryLogStore()


def set_default(store: LogStore) -> None:
    """Test-only seam. Production code should call this exactly zero times."""
    global _default
    _default = store


def get_default() -> LogStore:
    return _default


# Module-level wrappers — every existing call site stays green.
def enqueue(entry: dict) -> None:
    _default.enqueue(entry)


def list_logs() -> list[dict]:
    return _default.list()


def reset_logs() -> None:
    _default.reset()


def _drain_now() -> None:
    _default.drain_now()


async def flush_loop(stop_event: asyncio.Event) -> None:
    """Backwards-compatible wrapper that runs flush on the default store until
    `stop_event` is set. Tests and `code.py` can keep calling this directly."""
    while not stop_event.is_set():
        await asyncio.sleep(0.1)
        _default.drain_now()