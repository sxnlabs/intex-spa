# spa — local web control for the Intex PureSpa (Baltik)

Replaces the Intex iOS app with a single-process web UI served from the Mac.
Talks directly to the spa's wifi module over the LAN (TCP/8990, no cloud, no auth).
Protocol reverse-engineered from [`mathieu-mp/aio-intex-spa`](https://github.com/mathieu-mp/aio-intex-spa)
and validated byte-for-byte against the real device (192.168.20.189) on 2026-05-22.

## Layout

```
spa/
├── intex_spa/          # protocol + runtime core
│   ├── protocol.py     # checksum / request framing / status decode (pure, dep-free)
│   ├── client.py       # single asyncio TCP connection + lock; toggle read-before-write
│   ├── supervisor.py   # owns the one client, polls (=keepalive), fans out state via SSE
│   ├── history.py      # throttled temperature history (JSONL, self-healing)
│   ├── schedule.py     # config + pure decision engine (heat / filter / ready-by)
│   └── scheduler.py    # async reconciler: applies the desired state, manual overrides
├── web/
│   ├── main.py         # FastAPI app (factory) + routes + SSE
│   ├── templates/      # index.html shell + _panel.html partial (HTMX)
│   └── static/app.css  # mobile-first dark UI
├── tests/              # offline: protocol vectors, client/supervisor e2e (fake spa), HTTP
├── probe.py            # standalone first-contact diagnostic (stdlib only)
└── install.sh          # uv sync + LaunchAgent (com.sxnlabs.spa)
```

## Run

Dev:

```bash
cd ~/Hermes/apps/spa
uv sync
INTEX_SPA_HOST=192.168.20.189 uv run uvicorn web.main:make_app --factory --reload
```

Tests (no hardware needed):

```bash
uv run pytest -q
```

Probe the spa directly (read-only, zero deps):

```bash
python3 probe.py 192.168.20.189            # one shot
python3 probe.py 192.168.20.189 --watch 10 # poll
python3 probe.py --selftest                # offline decode check
```

## Install as a service

```bash
# localhost only:
INTEX_SPA_HOST=192.168.20.189 ./install.sh
# reachable from the iPhone on the LAN:
HERMES_HOST=0.0.0.0 INTEX_SPA_HOST=192.168.20.189 ./install.sh
```

Config (env vars): `INTEX_SPA_HOST` (required), `INTEX_SPA_PORT` (8990),
`INTEX_SPA_POLL` (10s), `HERMES_HOST` (127.0.0.1), `HERMES_PORT` (8731),
`HERMES_PASSWORD` (optional — gate the UI).

Uninstall: `launchctl bootout gui/$(id -u)/com.sxnlabs.spa && rm ~/Library/LaunchAgents/com.sxnlabs.spa.plist`

## Security — read this before exposing it

**The spa firmware has no authentication and no encryption.** Anyone who can open a
TCP socket to `192.168.20.189:8990` can control it directly, bypassing this app
entirely. The single-client quirk (only one TCP client at a time) is a race, not a
control. The real protection is **network-level**:

- On the UDM Pro, keep the spa on the IoT VLAN and add a firewall rule so **only the
  Mac's IP** can reach `192.168.20.189:8990`; drop everything else.
- **Block the spa's WAN egress** (stops phone-home and a possible Tuya firmware push).

The app's **optional password** (`HERMES_PASSWORD`, or `state/.password`) is
defense-in-depth for the **web UI only** — single shared password, signed-cookie
session (login once, persists). It does nothing for the open port above. The
installer warns if you bind `0.0.0.0` without a password.

## Design constraints (why it's built this way)

- **One TCP connection.** The firmware tolerates a single client on :8990, so the
  whole app funnels through one `IntexSpaClient` (internal lock) owned by one
  `Supervisor`. Run uvicorn as a **single in-process server** — the LaunchAgent runs it
  with **no `--workers` flag at all**. (Do not "pin" `--workers 1`: that flag switches
  uvicorn to a multiprocess manager that spawns a child worker, which wedges under
  launchd. The default single process is what gives us exactly one supervisor.)
- **Polling = keepalive.** The firmware drops the connection if nothing talks to it,
  so the 10s status poll keeps it warm and feeds the live UI.
- **Toggles, not absolutes.** Every functional command (power/heater/filter/bubbles)
  *inverts* current state. The client reads status first and only sends when the
  desired state differs — idempotent. `preset_temp` is the one absolute command.
- **Stale-but-useful on error.** On a dropped/unreachable spa the UI shows an offline
  banner while keeping the last known reading; the next poll recovers.
- **Python is pinned to 3.12** (`.python-version`). On this Mac, CPython **3.14**
  produced silent ~30 s deaths under launchd — uvicorn process exits with no traceback
  and no Python-side shutdown hook, the poll loop just stops. `uvicorn[standard]` pulls
  three native deps (`uvloop`, `httptools`, `pydantic-core`), and at least one of them
  segfaulted in the background long-running case on 3.14. Foreground runs survived
  because the user kept the process alive interactively. Don't bump without testing
  the LaunchAgent for ≥30 min and checking `~/Library/Logs/DiagnosticReports/` for
  fresh Python `.ips` reports. To bump: `uv python pin 3.X && rm -rf .venv && uv sync`.
- **LaunchAgent hardening.** The plist sets `ThrottleInterval=15` (cap respawn rate so
  a fast-failing process can't peg launchd into the "inefficient checked allocations"
  state), `ExitTimeOut=20` (lifespan shutdown needs to close the spa TCP socket and
  cancel the poll task — 5 s is too tight), `ProcessType=Adaptive` (NOT Background —
  Background drops the jetsam priority so the OS kills us first under memory pressure),
  and `HOME` is set explicitly in `EnvironmentVariables` because launchd does not
  inherit it for LaunchAgents (httpx/anyio trust stores need it).

## Known limits / gotchas

- **`heater` is the function toggle, not the element state.** The firmware reports
  whether heating is *enabled*, not whether it's drawing power. A Shelly EM on the
  spa's supply would close that gap (and give kWh telemetry).
- **macOS sleep** suspends the LaunchAgent. Enable *Settings → Energy → Wake for
  network access* (and keep the Mac on power) to stay reachable from the phone.
- **Inter-VLAN.** The spa is on the IoT VLAN; the Mac/phone must be allowed to reach
  `192.168.20.189:8990` (UDM firewall rule) or sit on the same VLAN.
- **Assets are vendored** under `static/vendor/` (htmx, the SSE extension, Chart.js) —
  no CDN, works fully offline. Re-vendor with `npm i` + copy if you bump versions.
- Temperature setpoint is bounded to 20–40 °C (`protocol.TEMP_MIN_C/MAX_C`).

## History & chart

The supervisor records a throttled temperature sample on each successful poll (a new
point only when the temp changes or ≥60 s elapsed), persisted to `state/history.jsonl`
(7-day retention) so it survives restarts. Each sample also carries the outside air temp
(`air`) when the weather client has data. `GET /history?hours=N` serves it; the UI draws
water temp + setpoint + outside air with Chart.js and a 24 h / 7 j range toggle (defaults
to 7 j; x-axis switches to day/month labels for the week view).

## Scheduler

A built-in scheduler replaces the Intex app's clunky timer (the device's native timer
isn't exposed by the LAN protocol, so we run our own — strictly more flexible). The
engine (`schedule.evaluate`) is pure and fully unit-tested; the async reconciler applies
the desired state each minute via the supervisor (idempotent sets). Edited inline on the
main page (a form UI — day chips, time pickers, temp inputs; no JSON), persisted to
`state/schedule.json`, served by `GET/POST /api/schedule`. Features:

- **Timed heat setpoint** (`heat_rules`) — thermostat schedule: target temp by time/day.
- **Filtration windows** (`filter_windows`) — run filtration on a timer; also forced on
  whenever heating (circulation required).
- **"Ready by" pre-heat** (`ready_by`) — reach a temp by a target time; the start time is
  estimated from the heat rate, which **starts at 1 °C/h and is learned from history**
  (`estimate_heat_rate`, falling back to `heat_rate_c_per_h`).

The heater follows demand (on while below setpoint, off at/above; the spa's thermostat
holds). Manual UI actions register a per-field override (default 60 min) so the scheduler
won't immediately revert a manual change. The card shows the live plan (setpoint, heat/
rest, filter, learned rate).

## Weather-aware pre-heat

The spa loses heat (and so climbs slower) roughly in proportion to (water − outside air),
so the "ready by" lead must grow when it's cold. `intex_spa/weather.py` is a cached,
dependency-free Open-Meteo client for **Guipavas** (48.45, −4.42): it fetches the hourly
forecast (real air, apparent/feels-like, wind) in a worker thread, caches it in memory +
`state/weather.json` (30 min TTL), and degrades gracefully (keeps the last good forecast
on any error). Read helpers (`air_now`, `air_window`, `low_ahead`, `snapshot`) are pure
and instant — the supervisor stamps `air` into history via a non-blocking `air_provider`,
the scheduler refreshes the forecast each tick.

The heat-rate fed to the pre-heat math is computed by `schedule.effective_heat_rate`:

- **Calibrated** (preferred): once enough air-stamped history exists, `calibrate_rates`
  learns `r_net = r_gross − k_loss·(water − air)` from the spa's own heating (rising) and
  cooling (falling) segments — no hardcoded wattage. `predict_heat_rate(water, air, …)`
  then gives the achievable °C/h at the forecast air temp.
- **Weather-derate** (bootstrap, before enough data): the measured base rate is scaled by
  `1 − 0.025·max(0, 15 − air)`, floored at ×0.5 — a gentle, legible cold penalty.
- **Measured** (no weather): the plain `estimate_heat_rate` scalar, as before.

A colder forecast lowers the effective rate → `evaluate`'s `lead = gap / rate` grows → the
window opens earlier. `GET /weather` returns the snapshot plus the live `rate_explain`
and `preheat` (target, computed start, lead hours); the UI's **Météo · Guipavas** card
shows current conditions and spells out the algorithm's decision so it's auditable.

Config (env, defaults shown): `WEATHER_ENABLED=1`, `WEATHER_LAT=48.45`, `WEATHER_LON=-4.42`.

## Next steps (discussed, not built)

Shelly EM for true heater state + energy; Pushcut alerts on `Exx` / low-temp; pre-emptive
floor bump before a forecast cold night (the weather plumbing is already in place).
