"""UniFi Protect bridge — polls person-detection events into `UsageStore`.

`uiprotect` is OPTIONAL. Without it (and without creds), the poller never starts
and `usage.jsonl` stays empty. The chart sees no bands; nothing breaks.

Why polling and not the websocket: UniFi Protect's websocket needs a long-lived
TLS session and reconnect logic that's noisy under macOS sleep/wake. Polling
`bootstrap.events_recent()` every ~10 s covers the use-case (overlay shaded
bands on an hourly chart) at the cost of ≤10 s latency, which is invisible to
the user. The bridge stays a few dozen lines and is easy to unit-test.

We feed events into `UsageStore.append(start, end)` — its `merge_gap` logic
welds adjacent micro-events into one human-readable session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

_LOG = logging.getLogger("intex_spa.protect_client")

try:
    from uiprotect import ProtectApiClient  # type: ignore[import-not-found]
    HAVE_UIPROTECT = True
except ImportError:
    HAVE_UIPROTECT = False


# Smart-detect event types from uiprotect we treat as "near-spa activity".
# Person is the obvious one; we keep the door open for "vehicle"/"animal" later
# if useful (a roaming cat is not activity).
PERSON_SMART_TYPES = {"person"}


class ProtectPoller:
    """Poll a UniFi Protect controller for person-detection events.

    Usage:
        poller = ProtectPoller("<udm-host>", "user", "pass", usage_store, lookback_hours=2)
        await poller.start()
        ...
        await poller.stop()

    Lookback is the rolling window we ask Protect for on each tick — we union
    those events into `UsageStore`. We don't track which events we've seen; the
    store's interval-merge logic makes repeated appends idempotent.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        usage_store,
        *,
        port: int = 443,
        verify_ssl: bool = False,
        poll_seconds: float = 30.0,
        lookback_hours: float = 2.0,
        connect_factory: Callable | None = None,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.verify_ssl = verify_ssl
        self.usage = usage_store
        self.poll_seconds = float(poll_seconds)
        self.lookback_seconds = float(lookback_hours) * 3600
        # connect_factory lets tests inject a fake client without uiprotect installed
        self._connect_factory = connect_factory
        self._client = None
        self._task: asyncio.Task | None = None
        self.last_poll_at: float | None = None
        self.last_error: str | None = None
        self.events_seen: int = 0

    @property
    def enabled(self) -> bool:
        """True iff we can actually connect (creds present, lib available)."""
        if not self.username or not self.password or not self.host:
            return False
        return HAVE_UIPROTECT or self._connect_factory is not None

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            _LOG.info("protect: disabled (creds missing or uiprotect not installed)")
            return
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            with _suppress():
                await self._client.close_session()
            self._client = None

    # -- main loop --------------------------------------------------------
    async def _loop(self) -> None:
        await self._tick()
        while True:
            await asyncio.sleep(self.poll_seconds)
            await self._tick()

    async def _tick(self) -> None:
        try:
            await self._ensure_connected()
            events = await self._fetch_recent_events()
        except Exception as e:  # noqa: BLE001 — best-effort; drop client to force reconnect
            self.last_error = f"{type(e).__name__}: {e}"
            _LOG.warning("protect: poll failed: %s", self.last_error)
            with _suppress():
                if self._client is not None:
                    await self._client.close_session()
            self._client = None
            return
        self.last_error = None
        self.last_poll_at = time.time()
        for ev in events:
            self.usage.append(ev["start"], ev["end"], source=ev.get("source", "protect-person"))
            self.events_seen += 1

    # -- connection -------------------------------------------------------
    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        if self._connect_factory is not None:
            self._client = await self._connect_factory()
            return
        # uiprotect API: build client → update() pulls the bootstrap
        client = ProtectApiClient(
            self.host, self.port, self.username, self.password,
            verify_ssl=self.verify_ssl,
        )
        await client.update()
        self._client = client

    async def _fetch_recent_events(self) -> list[dict]:
        """Pull closed person-detection events in our lookback window.

        Returns a list of `{start, end, source}` ready to feed into UsageStore.
        We deliberately ignore events with `end is None` (still in progress) —
        UsageStore is interval-based, and the next poll will catch them once
        Protect closes them.
        """
        end = datetime.now(timezone.utc)
        start = datetime.fromtimestamp(end.timestamp() - self.lookback_seconds, tz=timezone.utc)
        # uiprotect's get_events signature has shifted between releases; we
        # try the modern kwargs and fall back to a positional call.
        events = await _safe_get_events(self._client, start, end)
        out: list[dict] = []
        for ev in events:
            # smart_detect_types is a list[str]; if any tag we care about is in
            # it, we count the event. Plain motion (no smart-detect tag) is
            # noisier — skip until the user asks for it.
            tags = set(getattr(ev, "smart_detect_types", None) or [])
            if not tags & PERSON_SMART_TYPES:
                continue
            ev_start = getattr(ev, "start", None)
            ev_end = getattr(ev, "end", None)
            if ev_start is None or ev_end is None:
                continue
            out.append(
                {
                    "start": ev_start.timestamp() if hasattr(ev_start, "timestamp") else float(ev_start),
                    "end": ev_end.timestamp() if hasattr(ev_end, "timestamp") else float(ev_end),
                    "source": "protect-person",
                }
            )
        return out


async def _safe_get_events(client, start, end):
    """Best-effort call across uiprotect API shapes — recent releases changed kw names."""
    try:
        return await client.get_events(start=start, end=end)
    except TypeError:
        return await client.get_events(start, end)


class _suppress:
    """Tiny no-fail context manager — exceptions are best-effort cleanup noise here."""
    def __enter__(self): return self
    def __exit__(self, *_): return True
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return True
