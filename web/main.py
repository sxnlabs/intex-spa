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
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from intex_spa import camera as cam_mod
from intex_spa import cover_detect, protocol
from intex_spa.camera import CameraSnapshot, UsageStore
from intex_spa.client import SpaUnreachable
from intex_spa.history import TempHistory
from intex_spa.protect_client import ProtectPoller
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
    camera_config_path: str | None = "state/camera.json",
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

    # -- camera subsystem (master switch via state/camera.json) ----------
    # weather pattern: instantiate once, hold None when unconfigured; every
    # route checks `camera is None` and degrades cleanly. No env vars.
    camera_config = cam_mod.load_config(camera_config_path)
    if camera_config is not None:
        def _classify_cover(frame_path):
            # only run when an ROI is calibrated; classify() itself bails out
            # cleanly without pillow installed (returns "unknown")
            roi = camera_config.get("roi")
            if not roi:
                return
            result = cover_detect.classify(frame_path, roi)
            cover_detect.save_state(camera_config["cover_state_path"], result)

        camera = CameraSnapshot(
            camera_config["rtsps_url"],
            frame_path=camera_config["frame_path"],
            history_dir=camera_config["history_dir"],
            poll_seconds=camera_config["poll_seconds"],
            timelapse_every_seconds=camera_config["timelapse_every_seconds"],
            timelapse_retention_days=camera_config["timelapse_retention_days"],
            timelapse_fps=camera_config["timelapse_fps"],
            jpeg_quality=camera_config["jpeg_quality"],
            snapshot_max_width=camera_config["snapshot_max_width"],
            ffmpeg_bin=camera_config["ffmpeg"],
            ffmpeg_extra_args=camera_config["ffmpeg_extra_args"],
            post_grab=_classify_cover,
        )
        usage = UsageStore(path=camera_config["usage_path"])
        prot_cfg = camera_config.get("protect") or {}
        protect = ProtectPoller(
            prot_cfg.get("host", ""),
            prot_cfg.get("user", ""),
            prot_cfg.get("pass", ""),
            usage,
        )
    else:
        camera = None
        usage = None
        protect = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if weather is not None:
            # warm the forecast in the background — never block startup on the network
            # (the scheduler tick also refreshes; air_now() returns None until it lands)
            asyncio.create_task(weather.refresh(force=True))
        await supervisor.start()
        await scheduler.start()
        if camera is not None:
            await camera.start()
        if protect is not None:
            await protect.start()  # no-op if creds missing / uiprotect not installed
        try:
            yield
        finally:
            if protect is not None:
                await protect.stop()
            if camera is not None:
                await camera.stop()
            await scheduler.stop()
            await supervisor.stop()

    app = FastAPI(title="Intex Spa", lifespan=lifespan)
    app.state.supervisor = supervisor
    app.state.scheduler = scheduler
    app.state.weather = weather
    app.state.camera = camera
    app.state.usage = usage
    app.state.protect = protect
    app.state.camera_config = camera_config
    app.state.camera_config_path = camera_config_path
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
            request,
            "index.html",
            {
                "s": supervisor.state,
                "spa_host": host,
                "camera_enabled": camera is not None,
            },
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

    # -- camera endpoints (all degrade to {"enabled": false} when off) ----
    @app.get("/api/camera/status")
    async def camera_status():
        if camera is None:
            return {"enabled": False}
        snap = camera.snapshot()
        snap["enabled"] = True
        snap["protect_enabled"] = bool(protect and protect.enabled)
        snap["roi"] = (camera_config or {}).get("roi")
        # last persisted cover state (None if never run / no pillow / no ROI)
        if camera_config:
            snap["cover"] = cover_detect.load_state(camera_config["cover_state_path"])
        return snap

    @app.get("/camera.jpg")
    async def camera_jpg():
        if camera is None or camera.last_frame_at is None:
            raise HTTPException(status_code=404, detail="no frame")
        return FileResponse(
            str(camera.frame_path),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/camera/roi")
    async def camera_set_roi(request: Request):
        if camera is None or not camera_config:
            raise HTTPException(status_code=503, detail="camera disabled")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if body is None:
            new_roi = None
        else:
            try:
                new_roi = {
                    "x": int(body["x"]), "y": int(body["y"]),
                    "w": int(body["w"]), "h": int(body["h"]),
                }
            except (KeyError, TypeError, ValueError):
                raise HTTPException(status_code=400, detail="roi needs {x, y, w, h}")
            if new_roi["w"] <= 0 or new_roi["h"] <= 0:
                raise HTTPException(status_code=400, detail="roi w/h must be > 0")
        camera_config["roi"] = new_roi
        cam_mod.save_config(app.state.camera_config_path, camera_config)
        return {"ok": True, "roi": new_roi}

    @app.get("/usage")
    async def usage_json(hours: float = 24.0):
        if usage is None:
            return {"enabled": False, "intervals": []}
        return {"enabled": True, "intervals": usage.recent(hours=hours)}

    @app.get("/timelapse")
    async def timelapse(date: str):
        if camera is None:
            raise HTTPException(status_code=503, detail="camera disabled")
        try:
            _dt.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        mp4 = await asyncio.to_thread(camera.generate_timelapse, date)
        if mp4 is None:
            raise HTTPException(status_code=404, detail=f"no frames for {date}")
        return FileResponse(str(mp4), media_type="video/mp4")

    @app.get("/recap", response_class=HTMLResponse)
    async def recap(request: Request, date: str | None = None):
        if camera is None:
            raise HTTPException(status_code=503, detail="camera disabled")
        d = date or _dt.date.today().isoformat()
        try:
            day = _dt.datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        # Day window in local epoch seconds (matches history.t and usage.{start,end})
        start = _dt.datetime.combine(day, _dt.time(0, 0)).timestamp()
        end = start + 86400
        pts = [p for p in supervisor.history.recent(hours=24 * 8)
               if start <= p["t"] < end and p.get("cur") is not None]
        temps = [p["cur"] for p in pts]
        intervals = []
        if usage is not None:
            intervals = [it for it in usage.recent(hours=24 * 8)
                         if it["end"] >= start and it["start"] < end]
        total_use = sum(max(0.0, min(it["end"], end) - max(it["start"], start))
                        for it in intervals)
        return templates.TemplateResponse(
            request, "recap.html",
            {
                "date": d,
                "min_t": min(temps) if temps else None,
                "max_t": max(temps) if temps else None,
                "samples": len(pts),
                "intervals": intervals,
                "total_use_minutes": round(total_use / 60),
            },
        )

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
