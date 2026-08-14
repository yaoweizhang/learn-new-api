"""In-memory log store with async flush.

Real impl writes to SQLite/MySQL/PostgreSQL. Here we use a thread-safe
deque and an async task that flushes every 0.5s.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque

_lock = threading.Lock()
_buffer: deque[dict] = deque()
_flushed: list[dict] = []


def reset_logs() -> None:
    with _lock:
        _buffer.clear()
        _flushed.clear()


def enqueue(entry: dict) -> None:
    with _lock:
        _buffer.append(entry)


def _drain_now() -> None:
    """Move all buffered entries to _flushed. Used by the shutdown hook so
    tests (which exit TestClient immediately) don't lose the last batch.
    """
    with _lock:
        while _buffer:
            _flushed.append(_buffer.popleft())


async def flush_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(0.1)
        with _lock:
            while _buffer:
                _flushed.append(_buffer.popleft())


def list_logs() -> list[dict]:
    with _lock:
        return list(_flushed)