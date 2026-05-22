"""Experimental ROI-based cover ON/OFF heuristic.

The camera shows only part of the spa, so this can never be authoritative. The
heuristic crops a user-calibrated rectangle (`roi` in `state/camera.json`),
computes mean luminance + standard deviation, and classifies:

  - low luminance AND low variance  → "on"  (cover surface is dark + uniform)
  - high luminance OR  high variance → "off" (water reflects + glints; varied colors)

Pillow + numpy are OPTIONAL. Without them every call returns `unknown` so the
endpoint and UI still light up — the user can install the extra later:

    uv sync --extra camera

Calibration UX (frontend draws ROI on the live frame, posts {x,y,w,h}) lives in
the web layer; this module is pure pixel maths.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

_LOG = logging.getLogger("intex_spa.cover_detect")

try:
    from PIL import Image
    import numpy as np
    HAVE_DEPS = True
except ImportError:  # pillow/numpy absent — tests and core stay green
    HAVE_DEPS = False

# 8-bit luma thresholds. Wide bands deliberately — anything in between → "unknown"
# rather than guessing. Tunable per-install via state/camera.json once we have data.
LUMA_ON_MAX = 80
LUMA_OFF_MIN = 110
STD_ON_MAX = 22
STD_OFF_MIN = 32


def classify(frame_path: str | Path, roi: dict | None) -> dict:
    """Classify a single frame inside `roi`. Returns a result dict.

    Result shape (always the same keys, even on errors):

        {
          "state": "on" | "off" | "unknown",
          "confidence": float,     # 0..1
          "luma": float | None,    # mean Y in ROI (0..255)
          "std":  float | None,    # std-dev of Y in ROI
          "at":   epoch_seconds,
          "reason": str            # human-readable
        }
    """
    out = {"state": "unknown", "confidence": 0.0,
           "luma": None, "std": None, "at": time.time(), "reason": ""}

    if not HAVE_DEPS:
        out["reason"] = "pillow/numpy not installed"
        return out
    if not roi or not all(k in roi for k in ("x", "y", "w", "h")):
        out["reason"] = "no ROI calibrated"
        return out
    p = Path(frame_path)
    if not p.exists():
        out["reason"] = "no frame yet"
        return out

    try:
        with Image.open(p) as im:
            im.load()
            crop = im.crop((
                int(roi["x"]), int(roi["y"]),
                int(roi["x"]) + int(roi["w"]),
                int(roi["y"]) + int(roi["h"]),
            )).convert("L")           # grayscale (BT.601 luma)
        arr = np.asarray(crop, dtype=np.uint8)
    except Exception as e:  # noqa: BLE001 — never break a poll on a bad frame
        _LOG.warning("cover_detect: frame read failed: %s", e)
        out["reason"] = f"read failed: {e}"
        return out

    if arr.size == 0:
        out["reason"] = "ROI is empty"
        return out

    luma = float(arr.mean())
    std = float(arr.std())
    out["luma"] = round(luma, 1)
    out["std"] = round(std, 1)

    if luma <= LUMA_ON_MAX and std <= STD_ON_MAX:
        # both metrics agree on a dark uniform surface — high confidence
        out["state"] = "on"
        # confidence rises as we sit further inside both bands
        score = ((LUMA_ON_MAX - luma) / LUMA_ON_MAX + (STD_ON_MAX - std) / STD_ON_MAX) / 2
        out["confidence"] = round(min(0.99, max(0.5, score)), 2)
        out["reason"] = "luma low + uniform"
    elif luma >= LUMA_OFF_MIN or std >= STD_OFF_MIN:
        out["state"] = "off"
        # at least one of the two stats clearly says "open water"
        l_score = max(0.0, (luma - LUMA_OFF_MIN) / (255 - LUMA_OFF_MIN))
        s_score = max(0.0, (std - STD_OFF_MIN) / (128 - STD_OFF_MIN))
        out["confidence"] = round(min(0.99, max(0.5, max(l_score, s_score))), 2)
        out["reason"] = "luma high or varied"
    else:
        out["reason"] = (
            f"between bands (luma {round(luma)} ∈ [{LUMA_ON_MAX},{LUMA_OFF_MIN}] "
            f"or std {round(std)} ∈ [{STD_ON_MAX},{STD_OFF_MIN}])"
        )
    return out


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
