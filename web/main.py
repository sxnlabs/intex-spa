"""FastAPI app: one process, one supervisor, one TCP connection to the spa.

Run with uvicorn's factory mode (single worker — never more, or you'd get N
supervisors fighting over the spa's single-client socket):

    INTEX_SPA_HOST=192.168.20.189 uvicorn web.main:make_app --factory --workers 1

The UI is HTMX + the SSE extension (assets vendored under static/vendor): the panel
re-renders on every state push from the supervisor's poll loop; buttons POST commands
that return the same partial. A Chart.js graph reads /history.

Optional auth: set HERMES_PASSWORD to gate the UI behind a signed-cookie login. This
protects the web UI only — NOT the spa's open TCP port (lock that down at the firewall).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from intex_spa import protocol
from intex_spa.client import SpaUnreachable
from intex_spa.history import TempHistory
from intex_spa.scheduler import Scheduler
from intex_spa.supervisor import Supervisor
from intex_spa.weather import GUIPAVAS_LAT, GUIPAVAS_LON, WeatherClient

from . import auth

_BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))

# functions exposed in the UI for this model (Baltik: no jets, no sanitizer)
UI_TOGGLES = [
    ("power", "Power", "⚡"),
    ("heater", "Chauffage", "🔥"),
    ("filter", "Filtration", "🌀"),
    ("bubbles", "Bulles", "🫧"),
]


def _fmt_ts(epoch: float | None) -> str:
    if not epoch:
        return ""
    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


templates.env.filters["ts"] = _fmt_ts
templates.env.globals["ui_toggles"] = UI_TOGGLES


def create_app(
    host: str,
    *,
    port: int = protocol.PORT,
    poll_interval: float = 10.0,
    history_path: str | None = "state/history.jsonl",
    password: str | None = None,
    secret_path: str = "state/.secret",
    schedule_path: str | None = "state/schedule.json",
    weather_enabled: bool = True,
    weather_lat: float = GUIPAVAS_LAT,
    weather_lon: float = GUIPAVAS_LON,
    weather_cache_path: str | None = "state/weather.json",
) -> FastAPI:
    weather = (
        WeatherClient(weather_lat, weather_lon, cache_path=weather_cache_path)
        if weather_enabled
        else None
    )
    history = TempHistory(path=history_path)
    supervisor = Supervisor(
        host,
        port=port,
        poll_interval=poll_interval,
        history=history,
        air_provider=(weather.air_now if weather else None),
    )
    scheduler = Scheduler(supervisor, config_path=schedule_path, weather=weather)
    secret = auth.load_or_create_secret(secret_path) if password else b""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if weather is not None:
            # warm the forecast in the background — never block startup on the network
            # (the scheduler tick also refreshes; air_now() returns None until it lands)
            asyncio.create_task(weather.refresh(force=True))
        await supervisor.start()
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await supervisor.stop()

    app = FastAPI(title="Intex Spa", lifespan=lifespan)
    app.state.supervisor = supervisor
    app.state.scheduler = scheduler
    app.state.weather = weather
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")

    if password:

        @app.middleware("http")
        async def _gate(request: Request, call_next):
            path = request.url.path
            public = path == "/login" or path.startswith("/static/") or path == "/healthz"
            if not public and not auth.token_valid(request.cookies.get(auth.COOKIE_NAME), secret):
                if request.method == "GET":
                    return RedirectResponse("/login", status_code=303)
                return PlainTextResponse("authentication required", status_code=401)
            return await call_next(request)

    def render_panel(request: Request):
        return templates.TemplateResponse(request, "_panel.html", {"s": supervisor.state})

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        if not password:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": False})

    @app.post("/login")
    async def login_submit(request: Request):
        body = (await request.body()).decode("utf-8", "replace")
        supplied = parse_qs(body).get("password", [""])[0]
        if password and auth.password_ok(supplied, password):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(
                auth.COOKIE_NAME,
                auth.issue_token(secret),
                max_age=auth.DEFAULT_MAX_AGE,
                httponly=True,
                samesite="lax",
            )
            return resp
        return templates.TemplateResponse(request, "login.html", {"error": True}, status_code=401)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {"s": supervisor.state, "spa_host": host}
        )

    @app.get("/panel", response_class=HTMLResponse)
    async def panel(request: Request):
        return render_panel(request)

    @app.post("/toggle/{field}", response_class=HTMLResponse)
    async def toggle(request: Request, field: str):
        if field not in protocol.TOGGLE_FIELDS:
            raise HTTPException(status_code=404, detail=f"unknown field {field!r}")
        current = bool((supervisor.state.get("status") or {}).get(field))
        try:
            await supervisor.set_field(field, not current)
        except SpaUnreachable as e:
            raise HTTPException(status_code=503, detail=str(e))
        scheduler.note_manual(field)  # don't let the scheduler immediately revert
        return render_panel(request)

    @app.post("/preset/{temp}", response_class=HTMLResponse)
    async def preset(request: Request, temp: int):
        try:
            await supervisor.set_preset(temp)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except SpaUnreachable as e:
            raise HTTPException(status_code=503, detail=str(e))
        scheduler.note_manual("preset")
        return render_panel(request)

    @app.get("/history")
    async def history_json(hours: float = 24.0):
        unit = (supervisor.state.get("status") or {}).get("unit") or "C"
        return {"unit": unit, "points": supervisor.history.recent(hours=hours)}

    @app.get("/weather")
    async def weather_json():
        if weather is None:
            return {"enabled": False}
        snap = weather.snapshot()
        snap["enabled"] = True
        # surface the scheduler's latest rate reasoning so the UI can explain itself
        plan = scheduler.last_plan or {}
        snap["rate_explain"] = plan.get("rate_explain")
        snap["preheat"] = plan.get("preheat")
        return snap

    @app.get("/api/schedule")
    async def api_schedule_get():
        return {"config": scheduler.get_config(), "plan": scheduler.last_plan}

    @app.post("/api/schedule")
    async def api_schedule_post(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        try:
            cfg = scheduler.set_config(body)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"invalid config: {e}")
        return {"ok": True, "config": cfg}

    @app.get("/healthz")
    async def healthz():
        s = supervisor.state
        return {"online": s["online"], "updated_at": s["updated_at"], "error": s["error"]}

    @app.get("/events")
    async def events(request: Request):
        async def gen():
            q = supervisor.subscribe()
            try:
                while not await request.is_disconnected():
                    try:
                        state = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": ""}
                        continue
                    html = templates.env.get_template("_panel.html").render(s=state)
                    yield {"event": "update", "data": html}
            finally:
                supervisor.unsubscribe(q)

        return EventSourceResponse(gen())

    return app


def _configured_password() -> str | None:
    """UI password from HERMES_PASSWORD, else state/.password (written by install.sh)."""
    env = os.environ.get("HERMES_PASSWORD")
    if env:
        return env
    pf = Path("state/.password")
    if pf.exists():
        return pf.read_text().strip() or None
    return None


def make_app() -> FastAPI:
    """uvicorn --factory entry point. Reads config from the environment."""
    host = os.environ.get("INTEX_SPA_HOST")
    if not host:
        raise RuntimeError("INTEX_SPA_HOST is required (e.g. 192.168.20.189)")
    return create_app(
        host,
        port=int(os.environ.get("INTEX_SPA_PORT", protocol.PORT)),
        poll_interval=float(os.environ.get("INTEX_SPA_POLL", "10")),
        password=_configured_password(),
        weather_enabled=os.environ.get("WEATHER_ENABLED", "1") not in ("0", "false", "no", ""),
        weather_lat=float(os.environ.get("WEATHER_LAT", GUIPAVAS_LAT)),
        weather_lon=float(os.environ.get("WEATHER_LON", GUIPAVAS_LON)),
    )
