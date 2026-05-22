"""Supervisor tests: state population, SSE fan-out, and error handling.

This is where the SSE behavior is actually verified (the /events route is thin
glue over subscribe/publish — see the note in test_http.py).
"""

import asyncio

from fake_spa import FakeSpa
from intex_spa.supervisor import Supervisor


async def _sup(spa: FakeSpa) -> Supervisor:
    host, port = await spa.start()
    return Supervisor(host, port=port, poll_interval=9999)


async def test_refresh_populates_state():
    spa = FakeSpa()
    sup = await _sup(spa)
    try:
        st = await sup.refresh()
        assert st["online"] is True
        assert st["error"] is None
        assert st["status"]["preset_temp"] == 37
        assert st["updated_at"] is not None
    finally:
        await sup.client.close()
        await spa.stop()


async def test_subscribe_gets_immediate_snapshot():
    spa = FakeSpa()
    sup = await _sup(spa)
    try:
        await sup.refresh()
        q = sup.subscribe()
        snap = q.get_nowait()  # pushed at subscribe time
        assert snap["status"]["current_temp"] == 19
    finally:
        sup.unsubscribe(q)
        await sup.client.close()
        await spa.stop()


async def test_command_publishes_to_subscribers():
    spa = FakeSpa()
    sup = await _sup(spa)
    try:
        await sup.refresh()
        q = sup.subscribe()
        q.get_nowait()  # drain initial snapshot
        await sup.set_field("bubbles", True)
        pushed = await asyncio.wait_for(q.get(), timeout=2)
        assert pushed["status"]["bubbles"] is True
        assert pushed["online"] is True
    finally:
        await sup.client.close()
        await spa.stop()


async def test_offline_marks_state_but_keeps_last_status():
    spa = FakeSpa()
    sup = await _sup(spa)
    try:
        await sup.refresh()
        last = sup.state["status"]
        assert last is not None

        # spa disappears; make reconnects fail fast
        await spa.stop()
        sup.client.timeout = 0.5
        sup.client.retries = 0

        st = await sup.refresh()
        assert st["online"] is False
        assert st["error"]
        assert st["status"] == last  # last known reading retained for the UI
    finally:
        await sup.client.close()
