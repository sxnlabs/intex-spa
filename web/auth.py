"""Optional single-password auth — dependency-free signed-cookie sessions.

Off unless a password is configured (HERMES_PASSWORD). This protects the *web UI*
only; it does nothing for the spa's own unauthenticated TCP port — lock that down at
the firewall. See README.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

COOKIE_NAME = "spa_session"
DEFAULT_MAX_AGE = 30 * 24 * 3600  # 30 days


def load_or_create_secret(path: str | Path) -> bytes:
    """Persist a random HMAC secret so cookies survive restarts."""
    p = Path(path)
    if p.exists() and p.read_bytes():
        return p.read_bytes()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    p.write_bytes(data)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return data


def issue_token(secret: bytes, now: float | None = None) -> str:
    ts = str(int(time.time() if now is None else now))
    sig = hmac.new(secret, ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def token_valid(
    token: str | None, secret: bytes, max_age: int = DEFAULT_MAX_AGE, now: float | None = None
) -> bool:
    if not token:
        return False
    try:
        ts_s, sig = token.split(".", 1)
        expected = hmac.new(secret, ts_s.encode(), hashlib.sha256).hexdigest()
    except ValueError:
        return False
    if not hmac.compare_digest(sig, expected):
        return False
    age = (time.time() if now is None else now) - int(ts_s)
    return 0 <= age <= max_age


def password_ok(supplied: str | None, expected: str) -> bool:
    return hmac.compare_digest((supplied or "").encode(), (expected or "").encode())
