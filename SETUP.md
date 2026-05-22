# Bootstrap on a fresh Mac

Step-by-step to rebuild the spa controller from zero on a new MacBook. Order
matters — later steps assume earlier ones succeeded. Expected total time: 15-30
minutes if everything works first try, longer if Protect creds or ngrok need a
detour.

If you're migrating *from* an existing Mac and want to keep history /
calibration / passwords, jump to **§ 10 — Restore state from backup** first.

---

## 1. macOS prerequisites

- Apple Silicon (everything in this repo runs on `aarch64` wheels). Intel works
  but you'll trade some speed.
- Settings → **Battery / Energy** → enable **Wake for network access**. The
  LaunchAgent gets suspended when the lid closes otherwise; iPhone access via
  ngrok dies with it.
- Stay on power. Macs throttle background tasks (incl. our poll loop) when on
  battery + asleep.

## 2. Network setup — stable IPs + firewall

The app addresses the spa and (optionally) the UniFi camera/UDM by IP. Those
IPs must not drift on DHCP renewals, or your config breaks every couple of
weeks. Do this **once per device**, then forget it.

### 2.1 — Pair the spa to your wifi

Use the Intex iOS app to add the spa to your home wifi (its "Add a device"
flow). Once paired, the spa exposes its TCP/8990 control port on the LAN —
that's all this repo speaks to. You can uninstall the Intex app afterwards;
it's never spoken to again.

### 2.2 — Find the spa's MAC + current IP

Either:

- **UniFi controller**: Settings → **Insights** → **Known Clients** → look for
  an Intex/Tuya-prefixed MAC. Note the assigned IP.
- **Generic** (any router, any LAN): from the Mac, scan for hosts that answer
  on the spa's control port. Skip everything that doesn't.
  ```bash
  # adjust the subnet to your LAN
  for ip in 192.168.20.{1..254}; do
    nc -zw1 "$ip" 8990 2>/dev/null && echo "spa: $ip"
  done
  ```
  Or `nmap -p 8990 --open 192.168.20.0/24` if you have nmap installed.

### 2.3 — Reserve a static DHCP lease for the spa

The lease tells the router "always hand this MAC the same IP". The spa itself
stays on DHCP — no need to change anything on the spa side.

**UDM Pro / UniFi Network**:
1. Settings → **Clients** → click the spa.
2. Settings ⚙ → **Fixed IP Address** → toggle on, accept the current IP (or
   pick a new one inside your DHCP range).
3. Unplug + replug the spa's power brick so it picks up the lease on its next
   DHCP request.

**Generic router**: most have a "DHCP reservation" or "Static lease" table
under LAN settings. Add the spa's MAC → the IP you want.

The IP you reserve here is the value you'll paste into `INTEX_SPA_HOST=` in
**§ 7 — Install the LaunchAgent**.

### 2.4 — Same treatment for the camera + UDM (if you'll use the camera)

If you plan to enable feature 2 / 3 / 4 (snapshot, housse detection,
timelapse — see **§ 9**), repeat **2.2 + 2.3** for the camera and for the
UniFi Protect controller (the UDM itself, or a UNVR). The Protect host IP
goes into `state/camera.json` → `protect.host`; the camera IP shows up
inside the RTSPS URL you'll grab from the Protect UI.

### 2.5 — (Optional) Firewall: only your Mac reaches the spa

The spa firmware has **no authentication and no encryption**. Anyone on the
same LAN can `nc <spa-ip> 8990` and control it directly, bypassing this app.
Defense-in-depth: a UniFi traffic rule that drops everything to that host:port
except your Mac.

UniFi Network → Settings → **Security** → **Traffic Rules** → *New rule*:

| Field | Value |
|---|---|
| Type | LAN In |
| Action | Block |
| Source | Any |
| Destination | IP `<spa-ip>`, port `8990` |
| Add exception | Source = Mac's IP (reserve it too — § 2.3 style) |

Verify from your phone (or another LAN device that's not the Mac):
`nc -zw1 <spa-ip> 8990` should fail.

### 2.6 — (Optional) Block the spa's WAN egress

The spa's wifi module phones home and can accept firmware updates from Tuya.
A "Block all outbound" rule scoped to the spa's IP shuts both off without
affecting your LAN control:

UniFi Network → Settings → **Security** → **Traffic Rules** → *New rule*:
type **Internet**, action **Block**, source IP = `<spa-ip>`.

### 2.7 — Sanity check from the Mac

```bash
ping -c 3 <spa-ip>                 # ICMP — basic L3 reachability
nc -zv <spa-ip> 8990               # spa control port (silent success = ok)
nc -zv <udm-ip> 7441 2>/dev/null   # UniFi Protect RTSPS, only if using camera
```

If any of these fails, fix the network before going further. Every later step
assumes the Mac can reach these.

## 3. Homebrew + system packages

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

ngrok is **§ 8** — optional, for remote (iPhone) access.

## 4. Clone the repo

```bash
mkdir -p ~/Hermes/apps
cd ~/Hermes/apps
git clone git@github.com:sxnlabs/intex-spa.git spa
cd spa
```

(Or `https://github.com/sxnlabs/intex-spa.git` if no SSH key on this Mac yet.)

Working directory is now `~/Hermes/apps/spa`. **Every command from here on
assumes you're in that directory.**

## 5. Bootstrap the venv

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

## 6. Configure (`state/camera.json` + `state/.password`)

The `state/` directory is gitignored — runtime state never reaches git. You
have to recreate it locally.

### 6.1 — camera config

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

### 6.2 — UI password (`state/.password`)

If you'll expose the UI through ngrok (§ 8), put a strong password here. The
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

### 6.3 — ROI + baselines (calibration, run after § 7)

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

## 7. Install the LaunchAgent

```bash
INTEX_SPA_HOST=<spa-ip> ./install.sh
# the spa IP is the one you reserved in § 2.3
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

## 8. Remote access via ngrok (optional)

Skip this if you'll only ever access the UI from this Mac.

### 8.1 — Install + authenticate ngrok

```bash
brew install --cask ngrok
ngrok config add-authtoken <your-ngrok-authtoken>   # from dashboard.ngrok.com
```

### 8.2 — Tunnel config

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

### 8.3 — Run ngrok at boot via launchd

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

### 8.4 — Verify the public URL

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

## 9. Optional: UniFi Protect creds (feature 2 — activity overlay)

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

## 10. Restore state from backup (migrating)

If you have access to the old Mac, copy these files **before** doing § 6 — they
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

## 11. Troubleshooting

**Service won't stay up; `runs` keeps incrementing.**
Check `state/spa.err.log`. The two failure modes I've actually hit:

- `address already in use` — the previous launchd job is still draining its
  socket. `launchctl bootout`, wait 5 s, `bootstrap`. Or run `lsof -ti tcp:8731 | xargs kill`.
- Silent ~30 s deaths with no traceback — almost certainly a Python version
  issue (uvloop / httptools / pydantic-core native segfault). Stay on the
  pinned 3.12. Check `~/Library/Logs/DiagnosticReports/` for fresh
  `Python-*.ips` files.

**"ffmpeg timeout" in `/api/camera/status`.**
Run the standalone ffmpeg command from § 6.1 manually. If that works but the
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

**The spa's IP changed after a router reboot.**
DHCP lease wasn't reserved. Go back to **§ 2.3**, reserve the lease, then
update `INTEX_SPA_HOST` in the LaunchAgent (re-run `install.sh` with the new
IP — it regenerates the plist).

**The housse classifier keeps drifting between night and day.**
Expected — the baselines are luma-based, and luma drops 30+ points after dark.
Either re-calibrate ON twice (day + night, the algo will use the closest of
the two), or use the manual override (⚙ → Auto / En place / Retirée).

## 12. Uninstall

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
