"""Live event bus for the control room. Producers run in worker threads; subscribers are SSE streams."""
import asyncio
import collections
import itertools
import json
import time

import store

_subscribers = set()
_recent = collections.deque(maxlen=400)
_ids = itertools.count(1)
loop = None


def emit(kind, **data):
    event = {"id": next(_ids), "t": time.time(), "kind": kind, "r": store.current.get(), **data}
    _recent.append(event)
    if loop:
        for q in list(_subscribers):
            loop.call_soon_threadsafe(q.put_nowait, event)
    return event


async def stream(rid, since=0):
    """Server-sent events of one restaurant only."""
    q = asyncio.Queue()
    _subscribers.add(q)
    try:
        for e in list(_recent):
            if e["id"] > since and e["r"] == rid:
                yield f"id: {e['id']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
        while True:
            try:
                e = await asyncio.wait_for(q.get(), 15)
                if e["r"] != rid:
                    continue
                yield f"id: {e['id']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
            except TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(q)
