# Bootstrap on a fresh Mac

Step-by-step to rebuild the spa controller from zero on a new MacBook. Order
matters — later steps assume earlier ones succeeded. Expected total time: 15-30
minutes if everything works first try, longer if Protect creds or ngrok need a
detour.

If you're migrating *from* an existing Mac and want to keep history /
calibration / passwords, jump to **§ 9 — Restore state from backup** first.

---

## 1. macOS prerequisites

- Apple Silicon (everything in this repo runs on `aarch64` wheels). Intel works
  but you'll trade some speed.
- Settings → **Battery / Energy** → enable **Wake for network access**. The
  LaunchAgent gets suspended when the lid closes otherwise; iPhone access via
  ngrok dies with it.
- Stay on power. Macs throttle background tasks (incl. our poll loop) when on
  battery + asleep.

## 2. Homebrew + system packages

```bash
# Homebrew (skip if already installed):
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# The two system binaries this project shells out to:
brew install ffmpeg           # RTSPS frame grab + timelapse mux
brew install uv               # Python runtime + venv manager (replaces pip/pyenv)
```

Verify:

```bash
ffmpeg -version | head -1     # → "ffmpeg version 7.x" or 8.x
uv --version                  # → "uv 0.x"
```

ngrok is **§ 7** — optional, for remote (iPhone) access.

## 3. Clone the repo

```bash
mkdir -p ~/Hermes/apps
cd ~/Hermes/apps
git clone git@github.com:sxnlabs/intex-spa.git spa
cd spa
```

(Or `https://github.com/sxnlabs/intex-spa.git` if no SSH key on this Mac yet.)

Working directory is now `~/Hermes/apps/spa`. **Every command from here on
assumes you're in that directory.**

## 4. Bootstrap the venv

```bash
# .python-version pins CPython 3.12 (3.14 silently segfaulted under launchd
# on the previous Mac — uvloop / httptools / pydantic-core natives. Don't bump
# the pin without verifying the LaunchAgent survives ≥30 min and checking
# ~/Library/Logs/DiagnosticReports/ for fresh Python .ips files.)
uv sync --extra camera --extra dev
```

The `--extra camera` pulls Pillow + numpy + uiprotect (needed for housse-state
detection and person-event overlay). `--extra dev` pulls pytest + httpx so the
offline test suite runs locally.

Smoke test that everything imports cleanly + tests pass:

```bash
uv run pytest -q
# → 135 passed in ~3s
```

If anything is red, fix it before going further — every later step assumes a
green suite.

## 5. Configure (`state/camera.json` + `state/.password`)

The `state/` directory is gitignored — runtime state never reaches git. You
have to recreate it locally.

### 5.1 — camera config

`state/camera.json` is the master switch for the camera subsystem. Empty / missing
⇒ the whole subsystem is off (every endpoint returns `{"enabled": false}`, UI
hides the camera card). Full populated shape:

```jsonc
{
  "rtsps_url": "rtsps://<UDM_IP>:7441/<TOKEN>?enableSrtp",
  "poll_seconds": 10,
  "protect": {
    "host": "<UDM_IP>",
    "user": "",
    "pass": ""
  },
  "roi": null,
  "cover_baseline_on":  null,
  "cover_baseline_off": null,
  "cover_forced_state": null,
  "frame_path": "state/cam.jpg",
  "history_dir": "state/cam_history",
  "usage_path": "state/usage.jsonl",
  "cover_state_path": "state/cover_state.json",
  "timelapse_every_seconds": 60.0,
  "timelapse_retention_days": 7,
  "timelapse_fps": 24,
  "jpeg_quality": 7,
  "snapshot_max_width": 1280,
  "ffmpeg": "ffmpeg",
  "ffmpeg_extra_args": []
}
```

Where the token comes from:

1. UniFi OS → **Protect** → tap the camera → **Settings** → **Advanced** →
   **RTSPS streams** → enable a stream → copy the URL. It contains a per-stream
   token; treat it like a credential.
2. Paste into `rtsps_url`. **Never** paste this URL into a committed file, the
   README, a chat log you might share, or a screenshot.

Quick frame-grab test once the URL is in place (this is "Step 0" — confirms
your network can reach the camera before we wire the service):

```bash
URL=$(python3 -c "import json; print(json.load(open('state/camera.json'))['rtsps_url'])")
ffmpeg -y -hide_banner -loglevel error -rtsp_transport tcp -i "$URL" -frames:v 1 -f image2 -q:v 7 /tmp/cam_test.jpg
ls -la /tmp/cam_test.jpg     # should be ~1 MB
open /tmp/cam_test.jpg       # visual check
```

The `-f image2` flag is mandatory and easy to forget — without it ffmpeg picks
the muxer from the filename extension and the in-app `.tmp` atomic-write file
makes it silently hang. The test pinned in the suite enforces this; don't strip
it.

### 5.2 — UI password (`state/.password`)

If you'll expose the UI through ngrok (§ 7), put a strong password here. The
file is the alternative to setting `HERMES_PASSWORD` as an env var; the
launchd plist reads `state/.password` automatically.

```bash
python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)))" \
  > state/.password
chmod 600 state/.password
cat state/.password           # save this once in 1Password / iOS Keychain
```

Skip this only if you're sure the UI will stay bound to `127.0.0.1` and no
tunnel is opened. The install script will warn you if you bind `0.0.0.0`
without a password.

### 5.3 — ROI + baselines (calibration, run after § 6)

These are recorded **through the UI** after the LaunchAgent is up, not by hand
editing — the live frame is needed:

1. Open `http://127.0.0.1:8731/`, log in.
2. Click **⚙** on the camera card → **Zone d'analyse → Redessiner** → drag a
   rectangle on the housse (cover) area only, **Enregistrer**. ROI persists into
   `camera.json`.
3. With the housse in place, click **Apprendre l'apparence → Housse en place**.
4. The next time you take the housse off (or right then, briefly), click
   **Housse retirée**. Both baselines now in `camera.json` → classifier swaps
   to nearest-baseline (self-tuned to your lighting).

You can re-do these at any time — they're idempotent.

## 6. Install the LaunchAgent

```bash
INTEX_SPA_HOST=<your_spa_ip> ./install.sh
# e.g. INTEX_SPA_HOST=192.168.20.189 ./install.sh
```

What this does (read `install.sh` for the full story):

- `uv sync --extra dev` to provision the venv if it's not already there.
- Runs the offline test suite as a smoke check (continues even on failure with
  a warning — useful for "I'm in a hurry" installs, not great for prod).
- Substitutes paths into `com.sxnlabs.spa.plist.tmpl` and writes
  `~/Library/LaunchAgents/com.sxnlabs.spa.plist`. The plist hardenings
  (`ThrottleInterval=15`, `ExitTimeOut=20`, `ProcessType=Adaptive`, explicit
  `HOME`, no `--workers`) are not negotiable — they exist to dodge specific
  launchd failure modes documented in the README.
- `launchctl bootout` any old job, sleeps 2 s (avoids the "I/O error" race),
  then `bootstrap` the new one.

Verify within ~15 s:

```bash
curl -s http://127.0.0.1:8731/healthz
# → {"online":true,"updated_at":...,"error":null}

launchctl print gui/$(id -u)/com.sxnlabs.spa | grep -E "state|pid|runs|last exit"
# state should be "running"; runs increments on each kickstart
```

Open the UI:

```bash
open http://127.0.0.1:8731/
```

Watch a frame land:

```bash
ls -la state/cam.jpg          # appears within ~10 s
```

## 7. Remote access via ngrok (optional)

Skip this if you'll only ever access the UI from this Mac.

### 7.1 — Install + authenticate ngrok

```bash
brew install --cask ngrok
ngrok config add-authtoken <your-ngrok-authtoken>   # from dashboard.ngrok.com
```

### 7.2 — Tunnel config

ngrok 3 stores its config at `~/Library/Application Support/ngrok/ngrok.yml`.
Reserve a stable subdomain at dashboard.ngrok.com first (or use a random
one). Then make the file look like:

```yaml
version: "3"
agent:
    authtoken: <your-token>

endpoints:
  - name: jacuzzi
    url: https://<your-reserved-subdomain>.ngrok.io
    upstream:
      url: 8731
```

### 7.3 — Run ngrok at boot via launchd

Create `~/Library/LaunchAgents/com.sxnlabs.ngrok.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.sxnlabs.ngrok</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/ngrok</string>
    <string>start</string>
    <string>--all</string>
    <string>--log=stdout</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/<you>/Hermes/state/ngrok-agent.stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/<you>/Hermes/state/ngrok-agent.stderr.log</string>
</dict>
</plist>
```

Load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sxnlabs.ngrok.plist
launchctl print gui/$(id -u)/com.sxnlabs.ngrok | grep state    # → running
```

### 7.4 — Verify the public URL

```bash
curl -s http://127.0.0.1:4040/api/tunnels | python3 -m json.tool | grep public_url
# → "public_url": "https://<your-subdomain>.ngrok.io"
```

Test end-to-end (the `/healthz` route is intentionally public for monitoring):

```bash
curl -s https://<your-subdomain>.ngrok.io/healthz
# → {"online":true,...}
```

Then in iPhone Safari, open the public URL, type the password from
`state/.password`, save to Keychain on first prompt.

## 8. Optional: UniFi Protect creds (feature 2 — activity overlay)

The "in use" green bands on the temperature chart come from UniFi Protect's
person-detection events. Skip this unless you actually want them.

1. UniFi OS → **Protect** → **Settings** → **Users** → create a local user
   (NOT a Ubiquiti SSO account — `uiprotect` doesn't handle the OAuth
   redirect). Role `Viewer` is enough.
2. Fill `protect.host` / `protect.user` / `protect.pass` in `state/camera.json`.
3. `launchctl kickstart -k gui/$(id -u)/com.sxnlabs.spa` to pick up the new
   creds.
4. Verify:
   ```bash
   curl -s http://127.0.0.1:8731/api/camera/status -b "<session-cookie>" | python3 -m json.tool | grep protect_enabled
   # → "protect_enabled": true
   ```

## 9. Restore state from backup (migrating)

If you have access to the old Mac, copy these files **before** doing § 5 — they
contain irreplaceable runtime state and would otherwise have to be redone:

```bash
# from the old Mac:
scp -r ~/Hermes/apps/spa/state newmac:~/Hermes/apps/spa/
```

What's inside `state/`:

| File | Why it matters | Regeneratable? |
|---|---|---|
| `camera.json` | RTSPS URL token + ROI + housse baselines | Token: re-grab in Protect. Baselines: re-do via UI. |
| `.password` | UI password | Yes — generate a new one + re-save to Keychain. |
| `.secret` | HMAC key for login cookies | Yes (auto-created), but all existing sessions on iPhone die. |
| `schedule.json` | Heat / filter / ready-by rules | Yes via UI, but it's tedious. |
| `history.jsonl` | 7-day temp history | No — irrecoverable, the only off-Mac copy is here. |
| `weather.json` | Last Open-Meteo snapshot | Yes (auto-refetches in 30 min). |
| `cam_history/` | Timelapse archives | No — burnt-in image data, not recreatable. |
| `usage.jsonl` | Activity intervals from Protect | No — irrecoverable. |

`chmod 600` `state/.password` again after the copy — `scp` may not preserve
mode.

## 10. Troubleshooting

**Service won't stay up; `runs` keeps incrementing.**
Check `state/spa.err.log`. The two failure modes I've actually hit:

- `address already in use` — the previous launchd job is still draining its
  socket. `launchctl bootout`, wait 5 s, `bootstrap`. Or run `lsof -ti tcp:8731 | xargs kill`.
- Silent ~30 s deaths with no traceback — almost certainly a Python version
  issue (uvloop / httptools / pydantic-core native segfault). Stay on the
  pinned 3.12. Check `~/Library/Logs/DiagnosticReports/` for fresh
  `Python-*.ips` files.

**"ffmpeg timeout" in `/api/camera/status`.**
Run the standalone ffmpeg command from § 5.1 manually. If that works but the
service can't reach the camera, the most likely cause is that you forgot
`-f image2` in `intex_spa/camera.py::build_cmd` (don't — the test enforces it).
Second most likely: an inter-VLAN firewall rule blocks the Mac → camera path
for the launchd-spawned ffmpeg but not for the terminal-spawned one (macOS
Local Network privacy in some setups).

**iPhone gets ERR_NGROK_3004.**
Usually a transient race during a kickstart: the spa is restarting while ngrok
tries to forward to it. Wait 30 s and refresh. If persistent, check
`state/ngrok-agent.stdout.log` for `failed to open private leg` errors —
that's the spa process being unreachable from ngrok, not a tunnel issue.

**The housse classifier keeps drifting between night and day.**
Expected — the baselines are luma-based, and luma drops 30+ points after dark.
Either re-calibrate ON twice (day + night, the algo will use the closest of
the two), or use the manual override (⚙ → Auto / En place / Retirée).

## 11. Uninstall

Total teardown:

```bash
# Stop + remove the LaunchAgents:
launchctl bootout gui/$(id -u)/com.sxnlabs.spa
launchctl bootout gui/$(id -u)/com.sxnlabs.ngrok 2>/dev/null
rm -f ~/Library/LaunchAgents/com.sxnlabs.spa.plist
rm -f ~/Library/LaunchAgents/com.sxnlabs.ngrok.plist

# Optional: remove the project + venv:
rm -rf ~/Hermes/apps/spa
```

`brew uninstall ffmpeg uv ngrok` if you're done with the dependencies too.
