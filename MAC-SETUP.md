# Mac → Switch runbook (host 192.168.68.56)

Copy-paste steps for showing Steven's Mac screen on a **stock** Switch.
Two modes: **(a) mirror** one Mac display, or **(b) extended** — a virtual
display created by BetterDisplay that you drag windows onto.

Everything runs on the Mac. The Switch only needs its DNS changed for the
duration, and changed back afterwards.

---

## 0. Prerequisites on the Mac

| Step | How |
|---|---|
| **Same Wi‑Fi** | Mac and Switch on the **same 5 GHz SSID** (2.4 GHz caps bandwidth and adds latency). Router must **not** have "AP/client isolation" enabled, or the Switch cannot reach the Mac at all. |
| **LAN IP confirmed** | `ipconfig getifaddr en0` → must print `192.168.68.56`. If it differs, use whatever it prints everywhere below. Do **not** use the Tailscale `100.87.10.17` — the Switch cannot join the tailnet. |
| **Python 3** | `python3 --version`. If macOS asks to install Command Line Tools, accept once. |
| **ffmpeg** | `brew install ffmpeg` (needs Homebrew). Verify: `ffmpeg -version`. |
| **Screen Recording permission** | System Settings → Privacy & Security → **Screen Recording** → enable **Terminal** (or iTerm/the app you launch from), then **quit and reopen** that terminal. *Without this, AVFoundation returns BLACK frames and no error.* |
| **cliclick** (tap-to-click) | `brew install cliclick` — the click synthesizer. Fallback is `osascript`/System Events (works, less reliable). |
| **Accessibility permission** (tap-to-click) | System Settings → Privacy & Security → **Accessibility** → enable **Terminal**, then restart it. *Without this the click is silently swallowed.* |
| **macOS firewall** | When `python3` first listens, macOS asks "Allow incoming network connections?" → **Allow**. If it never asked, check System Settings → Network → Firewall. |
| **Script on the Mac** | Taildrop or `scp` `switchdisplay.py` to the Mac (single file, stdlib only, no pip). |

Find the display index you need (do this once, and again after plugging/unplugging
displays or creating a dummy):

```bash
ffmpeg -f avfoundation -list_devices true -i ""
# [AVFoundation video devices]
#   [0] FaceTime HD Camera
#   [1] Capture screen 0      <-- built-in display (mirror mode)
#   [2] Capture screen 1      <-- external or BetterDisplay dummy (extended mode)
```

---

## (a) Mirror mode — 960×540, 15 fps, quality 65

```bash
sudo python3 switchdisplay.py \
  --preset mac-mirror \
  --display-index 1 \
  --width 960 --height 540 --fps 15 --quality 65 \
  --bind 192.168.68.56 --port 8090 \
  --dns-port 53 --dns-answer 192.168.68.56 \
  --click-secret 'CHANGE-ME-to-a-long-random-string' \
  --click-display-size 1512x982
```

`--click-display-size` is the display size in **points** (the "Looks like
1512 × 982" figure in System Settings → Displays, or `system_profiler
SPDisplaysDataType`), **not** the Retina pixel size. Omit `--click-secret` and
the page is view-only. See [Tap-to-click](#tap-to-click-optional) below.

`sudo` is only needed to bind DNS on port 53. If you would rather not run the
whole thing as root, see "port 53 already in use" below.

The resolved capture command (printed by `--print-source`, check it matches):

```
ffmpeg -hide_banner -loglevel error -f avfoundation -framerate 15 -capture_cursor 1 \
  -i 1:none -vf scale=960:540:force_original_aspect_ratio=decrease \
  -c:v mjpeg -q:v 11 -f mjpeg -
```

---

## (b) Extended mode — BetterDisplay dummy at 1280×720

1. Install **BetterDisplay** (free) and create a **Dummy Display** with
   resolution **1280×720**.
2. Make sure macOS treats it as **extended**, not mirrored:
   System Settings → Displays → **Arrange…** → untick "Mirror displays" and
   position the dummy beside the built-in screen.
3. Get its capture index (it is usually the last "Capture screen N"):

   ```bash
   ffmpeg -f avfoundation -list_devices true -i ""
   ```

4. Run it (replace `2` with the dummy's index):

```bash
sudo python3 switchdisplay.py \
  --preset mac-extended \
  --display-index 2 \
  --width 1280 --height 720 --fps 15 --quality 65 \
  --bind 192.168.68.56 --port 8090 \
  --dns-port 53 --dns-answer 192.168.68.56 \
  --click-secret 'CHANGE-ME-to-a-long-random-string' \
  --click-display-size 1280x720 \
  --click-display-origin 1512,0
```

`--click-display-origin` is where that display starts in **global screen
coordinates** — a dummy placed to the right of a 1512-point-wide built-in screen
starts at `1512,0`; the primary display is always `0,0`. Get both numbers right
or taps land on the wrong display (they will still be *inside* the click area,
just offset).

5. Move the windows you want on the Switch onto the dummy display; the Switch
   shows exactly that display.

---

## Switch-side steps (same for both modes)

1. **System Settings → Internet → Internet Settings →** your Wi‑Fi →
   **Change Settings**.
2. **DNS Settings → Manual**:
   * **Primary DNS `192.168.68.56`**
   * **Secondary DNS `8.8.8.8`**
   * Save.
3. **Connect to This Network** → the test reports
   *"Registration is required to use this network"* → **Next**.
4. The captive-portal applet opens `conntest.nintendowifi.net`, which now
   resolves to the Mac → the viewer page appears and starts streaming.
5. To stop: press **Home** to leave the applet.
6. **Revert DNS to Automatic** when you are done — online play and system
   updates need normal DNS.

---

## Tap-to-click (optional)

A tap on the Switch becomes **one left click** at the same point on the Mac
display. Scope is deliberately narrow: **no drag, no hover, no right click, no
keyboard** — tap-as-click only.

**Setup**

1. `brew install cliclick` (preferred synthesizer). Without it the server falls
   back to `osascript` + System Events, which works but is less reliable.
2. Grant **Accessibility** permission to the terminal running the server
   (System Settings → Privacy & Security → Accessibility), then restart it.
   Screen Recording (needed for capture) is a *separate* permission.
3. Pick a long random secret and pass it with `--click-secret` (or set
   `SWITCHDISPLAY_SECRET`; the flag wins). With no secret the endpoint is
   refused and the page renders **view only**.
4. Tell it the display geometry in **points**:
   `--click-display-size 1512x982` (mirror) or
   `--click-display-size 1280x720 --click-display-origin 1512,0` (dummy).

**Test it without moving the cursor first**

Add `--click-dry-run`: the server resolves and logs the synthesizer command but
executes nothing.

```bash
curl -s -X POST http://192.168.68.56:8090/click \
  -H 'Content-Type: application/json' \
  -d '{"x":480,"y":270,"secret":"YOUR-SECRET"}'
# {"ok":true,"click":{"nx":0.5,"ny":0.5,"px":756,"py":491,"tool":"cliclick","result":"dry-run",...},
#  "command":"/opt/homebrew/bin/cliclick c:756,491"}
```

A centre tap on a 960×540 frame must map to the centre of the display
(756,491 on 1512×982). Then drop `--click-dry-run` and tap for real.

**Verify what happened**

```bash
curl -s http://192.168.68.56:8090/health | python3 -m json.tool
# "clicks": {"received":N,"rejected":0,"limited":0,"synthesized":N,"bad_coords":0}
# "last_click": {"nx":..,"ny":..,"px":..,"py":..,"tool":"cliclick","result":"clicked"}
```

**Guardrails**

* Secret compared in constant time; without one, `/click` returns 403 and the
  page has no tap handler at all.
* Rate limit: at most **10 taps per 3 s**, minimum **80 ms** apart → 429 beyond.
* Coordinates outside the current frame → 400 (no click).
* Bind stays on the LAN address; nothing is exposed beyond the local network.
* The page embeds the secret so its tap handler can POST (the applet has no
  address bar, so nobody can fetch the page casually). Treat the secret as a
  guard against stray LAN POSTs, not as authentication of a user.

---

## Verify before blaming the Switch

On the Mac:

```bash
curl -s http://192.168.68.56:8090/health      # frames should be climbing
curl -s --max-time 3 http://192.168.68.56:8090/mjpeg | wc -c   # non-zero bytes
```

On any other device on the same Wi‑Fi (phone is enough): open
`http://192.168.68.56:8090/` — if the phone shows the screen, the Mac side is
good and the problem is the Switch's DNS/applet step.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Frames are black | Screen Recording permission missing for the terminal → grant, **restart the terminal** |
| `Address already in use` on port 53 | mDNSResponder/another resolver holds it. Either bind a different interface (`--dns-bind 192.168.68.56` is already the default when `--dns-answer` is set), or run `--dns-port 5353` and redirect with pf: `rdr on en0 inet proto udp from any to any port 53 -> 192.168.68.56 port 5353` (also add the matching `nat`/`pass` rules; `sudo pfctl -f <file> -e`) |
| Switch shows "Could not connect" | Mac firewall blocked `python3`, or the router has client isolation, or Mac and Switch are on different subnets/SSIDs |
| Stream works on the phone but not the Switch | re-check primary DNS is exactly `192.168.68.56`, secondary `8.8.8.8`, and that the applet (not a normal page) is what opened |
| Very low fps | expected — the Switch's WebKit has **no JIT**; drop `--width/--height` (e.g. 800×450) or `--fps 10`, or lower `--quality` |
| Tap does nothing | Accessibility permission missing for the terminal (system → Privacy & Security → Accessibility), or `--click-secret` was not passed (page shows "view only"), or the secret in the page is stale — reload the applet |
| Clicks land offset by a constant amount | `--click-display-size` is in **points**, not Retina pixels; for a dummy also set `--click-display-origin` |
| `403 bad secret` in `/health` counters | page and server secrets differ — restart the server and reload the applet |
| Many `429` | rate limit (10 per 3 s, 80 ms apart) — that is deliberate |
| `502` + `failed` counter climbing | `cliclick` missing or blocked; `brew install cliclick`, or install Accessibility permission |
| Wrong screen shown | capture index changed after display changes — re-run `ffmpeg -f avfoundation -list_devices true -i ""` |

### Limits

* **Mirror** shows one whole display; **extended** needs BetterDisplay (macOS has
  no built-in user-creatable virtual display).
* **One session at a time**; the applet is modal and ends when you leave it.
* **Tap = one left click.** No drag, no hover, no right click, no keyboard — by
  design, so it stays useful for presentations and media control without turning
  the Switch into a half-broken KVM. For full input and low latency use
  Sunshine (Mac) + Moonlight-NX (Switch, requires homebrew).
* Everything stays on the LAN; nothing is sent off-machine.
