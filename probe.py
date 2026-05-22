#!/usr/bin/env python3
"""Intex Spa LAN probe — read-only first-contact tool.

Single round-trip against the spa's wifi module (TCP, no auth, JSON-over-line).
Sends one status request, verifies the checksum, decodes the frame, and prints
everything raw so we can sanity-check the decode against the physical device.

Protocol reproduced byte-exact from mathieu-mp/aio-intex-spa (verified against
its test vectors via --selftest). Stdlib only — runs on system python3, no deps.

Usage:
    python3 probe.py 192.168.20.42        # live probe
    python3 probe.py 192.168.20.42 --watch 10   # poll every 10s (Ctrl-C to stop)
    python3 probe.py --selftest           # offline: prove decode vs gold vectors
"""

import asyncio
import json
import sys
import time

PORT = 8990
TIMEOUT = 8  # seconds for connect + round-trip

# --- protocol: command requests (hex prefix, before checksum) -----------------
# NOTE: status request is 8888060FEE0F01. (The earlier recap mislabeled
# 8888050F0C as "status" — that's actually the preset_temp command prefix.)
STATUS_REQUEST = "8888060FEE0F01"


def checksum_int(data_hex: str) -> int:
    """Intex checksum: 0xFF minus the sum of bytes, modulo 0xFF, 0x00->0xFF."""
    c = 0xFF
    for i in range(0, len(data_hex), 2):
        c -= int(data_hex[i : i + 2], 16)
    c %= 0xFF
    if c == 0x00:
        c = 0xFF
    return c


def checksum_str(data_hex: str) -> str:
    """Hex checksum, NO zero padding — must match the module's expectation."""
    return hex(checksum_int(data_hex))[2:].upper()


def build_request(req_hex: str = STATUS_REQUEST) -> bytes:
    """Frame a request exactly like the reference lib (default json separators)."""
    payload = {
        "data": req_hex + checksum_str(req_hex),
        "sid": str(int(time.time() * 10000)),
        "type": 1,  # 1 = command (client -> spa)
    }
    return json.dumps(payload).encode(), payload["sid"]


def decode_status(data_hex: str) -> dict:
    """Decode a status `data` hex string (sans nothing — full 38-char frame)."""
    raw = int("0x" + data_hex, 16)
    preset = (raw >> 24) & 0xFF
    current_raw = (raw >> 88) & 0xFF
    current_temp = current_raw if current_raw < 181 else False
    error_code = f"E{current_raw - 100}" if current_raw >= 181 else False
    return {
        "power": bool((raw >> 104) & 1),
        "filter": bool((raw >> 105) & 1),
        "heater": bool((raw >> 106) & 1),
        "jets": bool((raw >> 107) & 1),
        "bubbles": bool((raw >> 108) & 1),
        "sanitizer": bool((raw >> 109) & 1),
        "current_temp": current_temp,
        "preset_temp": preset,
        "unit": "°C" if preset <= 40 else "°F",
        "error_code": error_code,
    }


async def probe_once(host: str) -> None:
    req_bytes, sid = build_request()
    print(f"→ connecting to {host}:{PORT} (timeout {TIMEOUT}s)")
    print(f"→ sending: {req_bytes.decode()}")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, PORT), timeout=TIMEOUT
    )
    try:
        writer.write(req_bytes)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=TIMEOUT)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    if not line:
        print("✗ empty response (connection closed with no data)")
        return

    print(f"← raw frame: {line.decode(errors='replace').rstrip()}")
    resp = json.loads(line.decode())

    data_hex = resp.get("data", "")
    # diagnostics — warn instead of asserting, this is a commissioning tool
    if resp.get("sid") != sid:
        print(f"⚠ sid mismatch: sent {sid}, got {resp.get('sid')}")
    if resp.get("result") != "ok":
        print(f"⚠ result != ok: {resp.get('result')!r}")
    if resp.get("type") != 2:
        print(f"⚠ type != 2 (expected status response): {resp.get('type')!r}")
    if len(data_hex) >= 2:
        calc = checksum_int(data_hex[:-2])
        got = int("0x" + data_hex[-2:], 16)
        print(f"  checksum: calc={calc:#04x} got={got:#04x} "
              f"{'OK' if calc == got else 'MISMATCH'}")

    print(f"  data hex ({len(data_hex)} chars): {data_hex}")
    print("  decoded:")
    for k, v in decode_status(data_hex).items():
        print(f"    {k:<13} {v}")


async def watch(host: str, every: float) -> None:
    while True:
        try:
            await probe_once(host)
        except Exception as e:  # noqa: BLE001 — surface everything during commissioning
            print(f"✗ {type(e).__name__}: {e}")
        print(f"--- sleeping {every}s ---\n")
        await asyncio.sleep(every)


def selftest() -> int:
    """Offline correctness check against aio-intex-spa's published vectors."""
    ok = True

    def check(label, got, want):
        nonlocal ok
        status = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{status}] {label}: got {got!r} want {want!r}")

    print("checksum vectors:")
    check("status request checksum", checksum_str(STATUS_REQUEST), "DA")
    check("filter request checksum", checksum_str("8888060F010004"), "D4")

    print("status decode (standard frame):")
    d = decode_status("FFFF110F010700220000000080808022000012")
    check("power", d["power"], True)
    check("filter", d["filter"], True)
    check("heater", d["heater"], True)
    check("jets", d["jets"], False)
    check("bubbles", d["bubbles"], False)
    check("sanitizer", d["sanitizer"], False)
    check("unit", d["unit"], "°C")
    check("current_temp", d["current_temp"], 34)
    check("preset_temp", d["preset_temp"], 34)
    check("error_code", d["error_code"], False)

    print("status decode (E81 error frame):")
    e = decode_status("FFFF110F010700B50000000080808022000012")
    check("current_temp", e["current_temp"], False)
    check("error_code", e["error_code"], "E81")
    check("preset_temp", e["preset_temp"], 34)

    print("checksum validation (valid frames accept, zero frame rejects):")
    for frame, want_valid in [
        ("FFFF110F010700220000000080808022000012", True),
        ("FFFF110F01070064000000008080806700008A", True),
        ("00000000000000000000000000000000000000", False),
    ]:
        calc = checksum_int(frame[:-2])
        got = int("0x" + frame[-2:], 16)
        check(f"frame ...{frame[-4:]} valid", calc == got, want_valid)

    print("\nSELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--selftest":
        return selftest()

    host = args[0]
    if "--watch" in args:
        i = args.index("--watch")
        every = float(args[i + 1]) if i + 1 < len(args) else 10.0
        try:
            asyncio.run(watch(host, every))
        except KeyboardInterrupt:
            print("\nstopped.")
        return 0

    try:
        asyncio.run(probe_once(host))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"✗ {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
