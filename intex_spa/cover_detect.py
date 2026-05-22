"""Experimental ROI-based cover ON/OFF heuristic.

The camera shows only part of the spa, so this can never be authoritative.

Two classification paths, picked automatically by `classify()`:

  1. **Nearest-baseline** (preferred, used once the user has clicked the calibrate
     buttons in both states). Each frame's ROI is reduced to `(luma, std)` and we
     pick the closer of `baseline_on` / `baseline_off` in that 2-D space. This is
     self-tuning per-install — covers a cover with shadows / reflections / texture
     that generic thresholds miss.

  2. **Threshold heuristic** (cold-start fallback, before calibration). A simple
     hand-tuned band on luma + std — fine for a flat blue cover, fragile otherwise.

Pillow + numpy are OPTIONAL. Without them every call returns `unknown` so the
endpoint and UI still light up — the user can install the extra later:

    uv sync --extra camera

Calibration UX (POST /api/camera/cover/calibrate?state=on|off → record current
ROI stats as a baseline) lives in the web layer; this module is pure pixel maths.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

_LOG = logging.getLogger("intex_spa.cover_detect")

try:
    from PIL import Image
    import numpy as np
    HAVE_DEPS = True
except ImportError:  # pillow/numpy absent — tests and core stay green
    HAVE_DEPS = False

# Cold-start thresholds. Used only when no baselines are calibrated yet.
LUMA_ON_MAX = 80
LUMA_OFF_MIN = 110
STD_ON_MAX = 22
STD_OFF_MIN = 32

# Distance below which a single baseline is "trusted" before its opposite has
# been recorded — gives a usable state after one calibration click instead of
# making the user wait until both are taken.
SINGLE_BASELINE_RADIUS = 25.0


def sample(frame_path: str | Path, roi: dict) -> tuple[float, float]:
    """Return (luma_mean, luma_std) for `roi` in `frame_path`.

    Raises on missing deps, missing frame, bad ROI, or empty crop. The web layer
    catches these and turns them into 4xx so the user knows what to fix.
    """
    if not HAVE_DEPS:
        raise RuntimeError("pillow/numpy not installed")
    if not roi or not all(k in roi for k in ("x", "y", "w", "h")):
        raise ValueError("no ROI")
    p = Path(frame_path)
    if not p.exists():
        raise FileNotFoundError(f"no frame at {p}")
    with Image.open(p) as im:
        im.load()
        crop = im.crop((
            int(roi["x"]), int(roi["y"]),
            int(roi["x"]) + int(roi["w"]),
            int(roi["y"]) + int(roi["h"]),
        )).convert("L")           # grayscale (BT.601 luma)
    arr = np.asarray(crop, dtype=np.uint8)
    if arr.size == 0:
        raise ValueError("empty ROI")
    return round(float(arr.mean()), 1), round(float(arr.std()), 1)


def classify(
    frame_path: str | Path,
    roi: dict | None,
    *,
    baseline_on: dict | None = None,
    baseline_off: dict | None = None,
    forced_state: str | None = None,
) -> dict:
    """Classify a single frame inside `roi`. Returns a result dict.

    `baseline_on` / `baseline_off`, when provided, are dicts `{luma, std}`
    captured by the calibration endpoint. With both present we ignore the
    cold-start thresholds entirely and use nearest-baseline.

    Result shape (always the same keys, even on errors):

        {
          "state": "on" | "off" | "unknown",
          "confidence": float,     # 0..1
          "luma": float | None,
          "std":  float | None,
          "at":   epoch_seconds,
          "reason": str
        }
    """
    out = {"state": "unknown", "confidence": 0.0,
           "luma": None, "std": None, "at": time.time(), "reason": "", "forced": False}

    # User-forced override wins outright. Still try to record luma/std for the
    # helper text so the user can see what the algo *would* have classified as.
    if forced_state in ("on", "off"):
        out["state"] = forced_state
        out["confidence"] = 1.0
        out["reason"] = "forcé par l'utilisateur"
        out["forced"] = True
        if HAVE_DEPS and roi:
            try:
                luma, std = sample(frame_path, roi)
                out["luma"] = luma
                out["std"] = std
            except Exception:  # noqa: BLE001
                pass
        return out

    if not HAVE_DEPS:
        out["reason"] = "pillow/numpy not installed"
        return out
    if not roi:
        out["reason"] = "no ROI calibrated"
        return out
    try:
        luma, std = sample(frame_path, roi)
    except FileNotFoundError:
        out["reason"] = "no frame yet"
        return out
    except (ValueError, RuntimeError) as e:
        out["reason"] = str(e)
        return out
    except Exception as e:  # noqa: BLE001 — never break a poll on a bad frame
        _LOG.warning("cover_detect: sample failed: %s", e)
        out["reason"] = f"read failed: {e}"
        return out

    out["luma"] = luma
    out["std"] = std

    # path 1: both baselines → nearest wins
    if _valid(baseline_on) and _valid(baseline_off):
        d_on = math.hypot(luma - baseline_on["luma"], std - baseline_on["std"])
        d_off = math.hypot(luma - baseline_off["luma"], std - baseline_off["std"])
        if d_on <= d_off:
            out["state"] = "on"
            margin = d_off - d_on
            out["reason"] = f"nearest baseline ON (d={d_on:.0f} vs OFF d={d_off:.0f})"
        else:
            out["state"] = "off"
            margin = d_on - d_off
            out["reason"] = f"nearest baseline OFF (d={d_off:.0f} vs ON d={d_on:.0f})"
        # Confidence = how decisive the choice was, normalized against the
        # sum so it doesn't blow up when both distances are large.
        denom = d_on + d_off + 1e-6
        out["confidence"] = round(min(0.99, max(0.5, margin / denom)), 2)
        return out

    # path 2: only one baseline → trust it if close, else say so
    if _valid(baseline_on):
        d = math.hypot(luma - baseline_on["luma"], std - baseline_on["std"])
        if d <= SINGLE_BASELINE_RADIUS:
            out["state"] = "on"
            out["confidence"] = round(max(0.5, 1 - d / SINGLE_BASELINE_RADIUS), 2)
            out["reason"] = f"close to ON baseline (d={d:.0f})"
        else:
            out["reason"] = f"far from ON baseline (d={d:.0f}) — record OFF too"
        return out
    if _valid(baseline_off):
        d = math.hypot(luma - baseline_off["luma"], std - baseline_off["std"])
        if d <= SINGLE_BASELINE_RADIUS:
            out["state"] = "off"
            out["confidence"] = round(max(0.5, 1 - d / SINGLE_BASELINE_RADIUS), 2)
            out["reason"] = f"close to OFF baseline (d={d:.0f})"
        else:
            out["reason"] = f"far from OFF baseline (d={d:.0f}) — record ON too"
        return out

    # path 3: cold-start thresholds
    if luma <= LUMA_ON_MAX and std <= STD_ON_MAX:
        out["state"] = "on"
        score = ((LUMA_ON_MAX - luma) / LUMA_ON_MAX + (STD_ON_MAX - std) / STD_ON_MAX) / 2
        out["confidence"] = round(min(0.99, max(0.5, score)), 2)
        out["reason"] = "luma low + uniform (uncalibrated)"
    elif luma >= LUMA_OFF_MIN or std >= STD_OFF_MIN:
        out["state"] = "off"
        l_score = max(0.0, (luma - LUMA_OFF_MIN) / (255 - LUMA_OFF_MIN))
        s_score = max(0.0, (std - STD_OFF_MIN) / (128 - STD_OFF_MIN))
        out["confidence"] = round(min(0.99, max(0.5, max(l_score, s_score))), 2)
        out["reason"] = "luma high or varied (uncalibrated)"
    else:
        out["reason"] = (
            f"between bands (luma {round(luma)}, std {round(std)}) — calibrate"
        )
    return out


def _valid(baseline: dict | None) -> bool:
    """A baseline is usable iff it has both numeric fields."""
    return bool(
        baseline
        and isinstance(baseline.get("luma"), (int, float))
        and isinstance(baseline.get("std"), (int, float))
    )


def save_state(path: str | Path, result: dict) -> None:
    """Persist the last classification so it survives restarts (for the UI)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(p)


def load_state(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
