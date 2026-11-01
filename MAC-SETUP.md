# Mac → Switch runbook (host <mac-lan-ip>)

Copy-paste steps for showing a Mac screen on a **stock** Switch.
Two modes: **(a) mirror** one Mac display, or **(b) extended** — a virtual
display created by BetterDisplay that you drag windows onto.

Everything runs on the Mac. The Switch only needs its DNS changed for the
duration, and changed back afterwards.

---

## 0. Prerequisites on the Mac

| Step | How |
|---|---|
| **Same Wi‑Fi** | Mac and Switch on the **same 5 GHz SSID** (2.4 GHz caps bandwidth and adds latency). Router must **not** have "AP/client isolation" enabled, or the Switch cannot reach the Mac at all. |
| **LAN IP confirmed** | `ipconfig getifaddr en0` → note the IP it prints and use it everywhere below as `<mac-lan-ip>`. Use the address on the same local network as the Switch. |
| **Python 3** | `python3 --version`. If macOS asks to install Command Line Tools, accept once. |
| **ffmpeg** | `brew install ffmpeg` (needs Homebrew). Verify: `ffmpeg -version`. |
| **Screen Recording permission** | System Settings → Privacy & Security → **Screen Recording** → enable **Terminal** (or iTerm/the app you launch from), then **quit and reopen** that terminal. *Without this, AVFoundation returns BLACK frames and no error.* |
| **cliclick** (tap-to-click, optional) | Not required: the server posts clicks itself through CoreGraphics (`quartz`). If `cliclick` is installed (`brew install cliclick`) it is used instead. |
| **Accessibility permission** (tap-to-click) | System Settings → Privacy & Security → **Accessibility** → enable **Terminal**, then restart it. On startup the server checks this and, if it's missing, asks macOS to show its permission prompt. Until it's granted, taps are refused with `503 Accessibility permission missing`, and the Switch HUD shows that message. (macOS would otherwise drop the click silently.) |
| **macOS firewall** | When `python3` first listens, macOS asks "Allow incoming network connections?" → **Allow**. If it never asked, check System Settings → Network → Firewall. |
| **Script on the Mac** | Taildrop or `scp` `switchdisplay.py` to the Mac (single file, stdlib only, no pip). |

List your screens (do this again after plugging/unplugging displays or creating
a dummy):

```bash
python3 switchdisplay.py --list-displays
# screen  size (points)  origin (points)
#   0     1920x1243      0,0              <-- main display (mirror mode default)
#   1     1920x1080      1920,27          <-- external or BetterDisplay dummy
```

Pick a screen with `--screen N`. Screens are captured by name (`Capture screen N`),
so cameras don't get in the way. The raw AVFoundation index from
`ffmpeg -f avfoundation -list_devices true -i ""` counts cameras first, and an
iPhone Continuity Camera or Desk View can push the screens to index 4–5. With
`--display-index 1` you can end up streaming a webcam. Only use `--display-index`
if you know you need it.

---

## (a) Mirror mode — 960×540, 15 fps, quality 65

```bash
sudo python3 switchdisplay.py \
  --preset mac-mirror \
  --screen 0 \
  --width 960 --height 540 --fps 15 --quality 65 \
  --bind <mac-lan-ip> --port 80 \
  --dns-port 53 --dns-answer <mac-lan-ip> \
  --click-secret 'CHANGE-ME-to-a-long-random-string'
```

Tap-to-click geometry (the display's size in **points** and its global origin)
is read from CoreGraphics for the chosen `--screen` and printed at startup
(`[click] screen 0: 1920x1243 points at 0,0 (auto-detected)`). Pass
`--click-display-size`/`--click-display-origin` only to override it. Omit
`--click-secret` and the page is view-only. See
[Tap-to-click](#tap-to-click-optional) below.

`sudo` is needed to bind ports 53 and 80. The server drops to the invoking user
after binding those sockets and before starting ffmpeg. To avoid privileged HTTP,
use `--port 8090` plus a pf TCP 80→8090 redirect.

The resolved capture command (printed by `--print-source`, check it matches):

```
ffmpeg -hide_banner -loglevel error -f avfoundation -framerate 15 -pixel_format nv12 \
  -capture_cursor 1 -i 'Capture screen 0:none' \
  -vf scale=960:540:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear \
  -fps_mode passthrough -pix_fmt yuvj420p -huffman optimal -c:v mjpeg -q:v 11 -f mjpeg -
```

`-fps_mode passthrough` matters. Without it, ffmpeg duplicates AVFoundation
frames up to hundreds of fps (about 680 fps measured for `-framerate 15`, roughly
14 MB/s) and floods the Wi‑Fi link. A healthy run shows `/health` frames rising
by about `--fps` per second.

---

## (b) Extended mode — BetterDisplay dummy at 1280×720

1. Install **BetterDisplay** (free) and create a **Dummy Display** with
   resolution **1280×720**.
2. Make sure macOS treats it as **extended**, not mirrored:
   System Settings → Displays → **Arrange…** → untick "Mirror displays" and
   position the dummy beside the built-in screen.
3. Find its screen number (usually the last row):

   ```bash
   python3 switchdisplay.py --list-displays
   ```

4. Run it (replace `1` with the dummy's screen number):

```bash
sudo python3 switchdisplay.py \
  --preset mac-extended \
  --screen 1 \
  --width 1280 --height 720 --fps 15 --quality 65 \
  --bind <mac-lan-ip> --port 80 \
  --dns-port 53 --dns-answer <mac-lan-ip> \
  --click-secret 'CHANGE-ME-to-a-long-random-string'
```

The dummy's size and global origin (for example `1920,27` for a screen to the
right of a 1920-point-wide main display) are detected automatically. To override,
pass `--click-display-size WxH --click-display-origin X,Y`. A negative origin
(a display to the left of or above the main one) needs the `=` form:
`--click-display-origin=-1920,0`.

5. Move the windows you want on the Switch onto the dummy display; the Switch
   shows exactly that display.

---

## Switch-side steps (same for both modes)

1. **System Settings → Internet → Internet Settings →** your Wi‑Fi →
   **Change Settings**.
2. **DNS Settings → Manual**:
   * **Primary DNS `<mac-lan-ip>`**
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

1. Nothing to install: the server posts the click through CoreGraphics
   (`tool: "quartz"`). If `cliclick` is on PATH it is used instead.
2. Grant **Accessibility** permission to the terminal running the server
   (System Settings → Privacy & Security → Accessibility), then restart it.
   Screen Recording (needed for capture) is a *separate* permission.
3. Pick a long random secret and pass it with `--click-secret` (or set
   `SWITCHDISPLAY_SECRET`; the flag wins). With no secret the endpoint is
   refused and the page renders **view only**.
4. Display geometry is auto-detected for the `--screen` you capture (check the
   `[click] screen N: ... (auto-detected)` startup line). Override with
   `--click-display-size`/`--click-display-origin` only when capturing via
   `--display-index` or a custom `--source`.

**Test it without moving the cursor first**

Add `--click-dry-run`: the server resolves and logs the synthesizer command but
executes nothing.

```bash
curl -s -X POST http://<mac-lan-ip>:80/click \
  -H 'Content-Type: application/json' \
  -d '{"x":480,"y":270,"secret":"YOUR-SECRET"}'
# {"ok":true,"click":{"nx":0.5755,"ny":0.5,"px":1105,"py":622,"tool":"quartz","result":"dry-run",...},
#  "command":"CGEventPost leftclick:1105,622"}
```

The frame keeps the display's aspect ratio (a 1920×1243 display becomes
834×540), so `nx`/`ny` are relative to `frame_size` in `/health`. A tap at the
centre of the frame must land at the centre of the display. Then drop
`--click-dry-run` and tap for real.

**Verify what happened**

```bash
curl -s http://<mac-lan-ip>:80/health | python3 -m json.tool
# "accessibility": true      <- false means macOS will not accept synthesized clicks
# "clicks": {"received":N,"rejected":0,"limited":0,"synthesized":N,"bad_coords":0}
# "last_click": {"nx":..,"ny":..,"px":..,"py":..,"tool":"cliclick","result":"clicked"}
```

**Guardrails**

* Secret compared in constant time; without one, `/click` returns 403 and the
  page has no tap handler at all.
* Rate limit: at most **10 taps per 3 s**, minimum **80 ms** apart → 429 beyond.
* Coordinates outside the current frame → 400 (no click).
* With click enabled, the default bind is the LAN address rather than all
  interfaces. Host validation rejects unrelated Host headers; use `--allow-host`
  for an intentional additional name.
* The page embeds the secret so its tap handler can POST. The secret is readable
  by allowed LAN clients unless `--click-allow IP` is supplied; treat it as a
  guard against stray POSTs, not as authentication of a user.

---

## Verify before blaming the Switch

On the Mac:

```bash
curl -s http://<mac-lan-ip>:80/health      # frames should be climbing
curl -s --max-time 3 http://<mac-lan-ip>:80/mjpeg | wc -c   # non-zero bytes
```

On any other device on the same Wi‑Fi (phone is enough): open
`http://<mac-lan-ip>:80/` — if the phone shows the screen, the Mac side is
good and the problem is the Switch's DNS/applet step.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Frames are black | Screen Recording permission missing for the terminal → grant, **restart the terminal** |
| `Address already in use` on port 53 or 80 | mDNSResponder/another resolver holds it. Bind another interface/service, or use 5353/8090 and pf redirects. DNS example: `rdr on en0 inet proto udp from any to any port 53 -> <mac-lan-ip> port 5353` HTTP example: `rdr on en0 inet proto tcp from any to any port 80 -> <mac-lan-ip> port 8090` (also add matching `nat`/`pass` rules; `sudo pfctl -f <file> -e`) |
| Switch shows "Could not connect" | Mac firewall blocked `python3`, or the router has client isolation, or Mac and Switch are on different subnets/SSIDs |
| Stream works on the phone but not the Switch | re-check primary DNS is exactly `<mac-lan-ip>`, secondary `8.8.8.8`, and that the applet (not a normal page) is what opened |
| Very low fps | expected — the Switch's WebKit has **no JIT**; drop `--width/--height` (e.g. 800×450) or `--fps 10`, or lower `--quality` |
| Tap does nothing | HUD says `Accessibility permission missing` → grant it to the terminal, restart the terminal and the server. HUD says "view only" → `--click-secret` was not passed. HUD says `bad secret` → the secret in the page is stale, reload the applet |
| Clicks land offset by a constant amount | `--click-display-size` is in **points**, not Retina pixels; for a dummy also set `--click-display-origin` |
| `403 bad secret` in `/health` counters | page and server secrets differ — restart the server and reload the applet |
| Many `429` | rate limit (10 per 3 s, 80 ms apart) — that is deliberate |
| `503` + `failed` counter climbing | Accessibility permission missing (`"accessibility": false` in `/health`) |
| `502` + `failed` counter climbing | the synthesizer itself errored (e.g. a broken `cliclick`); the response's `command` shows what ran |
| Wrong screen shown | screens changed. Re-run `python3 switchdisplay.py --list-displays` and pick `--screen N` |
| A webcam is shown instead of the screen | `--display-index` points at a camera. Drop it and use `--screen N` |
| `/health` frames climb by hundreds per second | a custom `--source` without `-fps_mode passthrough` (the presets include it) |

### Limits

* **Mirror** shows one whole display; **extended** needs BetterDisplay (macOS has
  no built-in user-creatable virtual display).
* **One session at a time**; the applet is modal and ends when you leave it.
* **Tap = one left click.** No drag, no hover, no right click, no keyboard — by
  design, so it stays useful for presentations and media control without turning
  the Switch into a half-broken KVM. For full input and low latency use
  Sunshine (Mac) + Moonlight-NX (Switch, requires homebrew).
* Everything stays on the LAN; nothing is sent off-machine.
