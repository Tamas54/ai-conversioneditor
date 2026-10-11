"""
In-memory event bus for the dashboard.

Tools and routes call `emit(...)` to publish; the SSE endpoint subscribes
and forwards to connected dashboard clients.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from typing import Any

_HISTORY_LIMIT = 100
_history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_LIMIT)
_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()


def emit(kind: str, **payload: Any) -> dict[str, Any]:
    from app import tenancy
    evt = {
        "id": uuid.uuid4().hex[:8],
        "ts": time.time(),
        "kind": kind,
        "uid": tenancy.current_user_id(),  # a feliratkozó csak a saját eseményeit kapja
        **payload,
    }
    _history.append(evt)
    for q in list(_subscribers):
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            pass
    return evt


def history(uid: str | None = None) -> list[dict[str, Any]]:
    return [e for e in _history if uid is None or e.get("uid") == uid]


def subscribe() -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue[dict[str, Any]]) -> None:
    _subscribers.discard(q)
