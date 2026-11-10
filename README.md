# switchdisplay — a second display on a stock Nintendo Switch

A stock Switch as a wireless second display for a Mac or Linux host, using
nothing but the console's built-in browser. Optional tap-to-click, Joy-Con
control and Switch-keyboard text entry send input back to the Mac.

**Short answer:** it works. The Switch's captive-portal browser cannot play
video, but it can do WebSocket + canvas + JPEG images — and that is all a
second screen needs. This folder contains a small server that streams the
host's display that way, with optional tap-to-click. If you want low latency
and full input, the honest route is Sunshine + Moonlight-NX, which needs
homebrew on the console.

The Switch runs no app and installs nothing — it just opens a web page.

---

## 1. Design: why WebSocket + JPEG + canvas

The Switch's portal browser sets hard limits: no `<video>`, no HLS, no MSE,
no WebRTC, no ES6 modules — but it does have WebSocket, `<canvas>` with
`drawImage`, `Blob` URLs, and JPEG decoding. So the design is deliberately
minimal: the host captures the screen to JPEG frames, pushes them over a
binary WebSocket (with an MJPEG `<img>` fallback that needs nothing but image
decoding), and the page draws each frame to canvas.

Flow control is ACK-driven: the server holds only the newest frame per viewer
and drops the rest, so a slow renderer degrades to lower fps instead of
growing lag. A frame identical to the previous one is not sent at all, so an
unchanged screen costs the Switch nothing. A tap on the page can optionally
POST back as one left click, and `--keys`/`--text` add Joy-Con and keyboard
input. All control sits behind a shared secret that only one device is given
(see "Who can control the Mac").

## 2. The Switch angle

| Fact | Evidence |
|---|---|
| Browser engine | **NetFront NX** (WebKit + JavaScriptCore), used for eShop/account/captive-portal applets |
| JS speed | **"No JIT recompilation is used for JavaScript"** (ReSwitched wiki) |
| WebKit versions | 2.0.0 ≈ Safari 9.1.1 (WebKit 601.6); 10.0.0 → 606.4; 20.1.0 → 613.0 |
| Official browser | none on HOME Menu |
| Best stock entry point | **`WifiWebAuthApplet`** (captive portal, applet 0x19): **no URL whitelist**, plain HTTP allowed, invalid TLS shows a dialog **with an ignore option** |
| How it is reached | the system opens it when `conntest.nintendowifi.net` does not serve the expected page; SwitchBru DNS (45.55.142.122) abuses this to give the console a browsable page |
| Video | **disabled** in `WifiWebAuthApplet` since 3.0 ("Support Code: 2809-1212 / This feature is not available") — `<video>`, HLS, MSE are all out |
| What does work | HTML5 canvas, JavaScript, **WebSocket** (ACCESS lists WebSocket support for NX), XMLHttpRequest, image decoding (JPEG/PNG) |

**Verdict: a stock Switch works as a second screen with this server.**

- No app to install, no modding — the console just opens a web page served
  from the host's own DNS/HTTP on the LAN.
- Constraints to expect: no JIT (expect single-digit to low-double-digit fps at
  ~960×540), one page at a time, the applet is a modal session (leaving it ends
  the display), and the Switch's DNS must be pointed at the host while you use it.

Homebrew alternative (needs *modding* — not done, not assumed): **Moonlight-NX**
(`rock88/moonlight-nx`) + **Sunshine** on the laptop (VA-API H.264 works on Intel
iGPUs). That is a real low-latency stream with input, but requires Atmosphère +
hbmenu with title redirection for full RAM. Only worth it if the browser route
proves too slow.

## 3. Host side: macOS first, Linux second

| Option | State |
|---|---|
| Sidecar | macOS → **iPad/Apple** only; the Switch is not an Apple device — impossible |
| Duet Display / Luna | clients for iOS/Android/Mac/Windows, **no Switch** client — impossible |
| Deskreen / Weylus | browser viewers that require **WebRTC** → the Switch applet cannot join |
| macOS Screen Sharing (VNC 5900) | works, but needs a browser VNC client; noVNC is ES6-module based and will not parse on WebKit 601.6 |
| VNC + noVNC | needs WebSocket + canvas → capability-compatible, but heavier than needed for a passive second screen |
| **Sunshine (macOS host) + Moonlight-NX** | real low-latency streaming with input, VideoToolbox H.264; Moonlight-NX is **Switch homebrew** → needs modding |
| **this prototype** | WebSocket + JPEG + canvas, the smallest capability set that matches the Switch; runs on macOS and Linux |

Recommendation: for a *passive* second screen (docs, logs, dashboards) use this
prototype. For interactive/low-latency use, Sunshine + Moonlight-NX (modded).

## 4. Running it

**The Mac runbook (exact commands, Switch steps, troubleshooting) is
[MAC-SETUP.md](MAC-SETUP.md).** Short version:

```bash
# which screens exist (size/origin in points)
python3 switchdisplay.py --list-displays

# everything a stock Switch needs, on this Mac's LAN address:
# HTTP on port 80 plus the DNS answer on port 53
sudo python3 switchdisplay.py --switch --preset mac-mirror
```

`--switch` stands for `--bind <mac-lan-ip> --port 80 --dns-port 53 --dns-answer
<mac-lan-ip>`; any of those can still be given explicitly. The long form:

```bash

# mirror the main display: 960x540, 15 fps, quality 65
sudo python3 switchdisplay.py --preset mac-mirror --screen 0 \
  --width 960 --height 540 --fps 15 --quality 65 \
  --bind <mac-lan-ip> --port 8090 \
  --dns-port 53 --dns-answer <mac-lan-ip>

# extended: a BetterDisplay dummy at 1280x720, captured by its screen number
sudo python3 switchdisplay.py --preset mac-extended --screen 1 \
  --width 1280 --height 720 --fps 15 --quality 65 \
  --bind <mac-lan-ip> --port 8090 \
  --dns-port 53 --dns-answer <mac-lan-ip>
```

Presets: `test` (built-in pattern, default), `mac-mirror`, `mac-extended`,
`x11`. `--print-source` prints the resolved ffmpeg argv without starting the
server — use it to confirm the capture line before running.

### Streaming efficiency profiles

Use `--profile sharp|balanced|low` for 1280×720, 960×540, or 800×450, or
`--profile text` for readable words: sized for the Switch viewer, with better
scaling, light sharpening and quality 85, at about twice the data per frame. The
default remains each preset's existing size; explicit `--width`/`--height`
always override a profile. Presets use JPEG `yuvj420p` with optimal Huffman
tables and a `fast_bilinear` scaler (where scaling is present). On the local
test source, optimal Huffman reduced frame bytes by about 25% and
`fast_bilinear` reduced encoder CPU by about 22% (machine/source dependent).

`--static` is opt-in for presets and adds `mpdecimate,setpts=N/FRAME_RATE/TB`
after scaling, so unchanged screens can stop producing frames. The server
keeps MJPEG and WebSocket connections alive with keepalives and the viewer
does not reconnect merely because a frame is unchanged. Custom `--source`
commands are intentionally untouched; add those filters yourself if needed.

The viewer supports `?mode=canvas` (default WebSocket canvas), `?mode=img`
(WebSocket image), `?mode=mjpeg` (native multipart image), and `?mode=pull`
(client-pull WebSocket flow control). For example:
`http://host:8090/?mode=img` and `http://host:8090/?mode=pull`. The HUD shows
the selected mode and measured render fps for A/B comparisons on the Switch.

`--fill fit|crop|stretch` sets how a picture that isn't 16:9 fills the screen, and
the viewer's **fill** button switches live (see MAC-SETUP.md).

Endpoints: `/` viewer page, `/ws` binary JPEG WebSocket, `/mjpeg` MJPEG
fallback, `POST /click` (tap-to-click), `/input` (Joy-Con/touch keys, with
`--keys`), `/health` counters.

### Measuring what the Switch really draws

`/health` lists every connected viewer under `viewers`: its address, mode, frames
sent per second, KB per frame, and `render_fps`, which the viewer page reports
itself every two seconds. Compare modes and profiles with that number instead of
reading the HUD:

```bash
curl -s http://<mac-lan-ip>/health | python3 -m json.tool
# "viewers": [{"ip": "...", "mode": "canvas", "sent_fps": 14.8,
#              "kb_per_frame": 41.2, "render_fps": 9.5, ...}]
```

`frames` counts captured frames and `duplicate_frames` those that matched the
previous one and were not sent, so `sent_fps` drops to zero on a still screen.

### Capture restarts by itself

If the capture command exits, the server starts it again (after 1 s, backing off
to 15 s while it keeps failing) and `/health` counts `source_restarts`. Preset
captures are also restarted when they never produce a first frame, or stop
producing frames for 15 s. A custom `--source` and `--static` may legitimately go
quiet, so those are only restarted when they exit. `--no-restart` turns all of
this off.

### Options in a file

Any options can live in a file, one per line, loaded with `@FILE`. That keeps the
secret out of shell history and `ps`:

```bash
printf -- '--click-secret=%s\n--keys\n' "$(openssl rand -hex 16)" > ~/.switchdisplay
chmod 600 ~/.switchdisplay
sudo python3 switchdisplay.py --switch --preset mac-mirror @$HOME/.switchdisplay
```

**Tap-to-click** is opt-in and single-purpose: a tap on the Switch becomes one
left click at that point on the Mac display. No drag, no hover, no right click,
no keyboard. Enabled only when a shared secret is supplied:

```bash
--click-secret 'long-random-string'        # or SWITCHDISPLAY_SECRET
--click-dry-run                            # resolve + log the click, execute nothing
# mac presets auto-detect the --screen's size/origin in points; override with:
--click-display-size 1280x720 --click-display-origin 1920,27
```

Synthesizer: `cliclick` if installed, otherwise a CoreGraphics `CGEvent` posted
directly (`quartz`, no install needed), with `osascript` as a last resort if
CoreGraphics can't be loaded. All of them need **Accessibility** permission for the terminal
(capture needs **Screen Recording**, which is a separate permission). Without a
secret `/click` returns 403 and the page renders view-only.

**Who can control the Mac.** The viewer page carries the secret, so whoever is
served the controlling page can click, and with `--keys`/`--text` press keys and
type. The server therefore gives it to one device only: the **first device to
open the viewer** becomes the controller until the server restarts, and the log
names it (`[lock] control locked to <ip>`). Every other device gets the
view-only page and is refused by `/click` and `/input`, even with the right
secret. Open the viewer on the Switch before anything else on the network does,
and check that log line. `--click-allow IP` (repeatable) names the controller
up front instead, which is the safer choice with `--text`. The Mac itself can
always control, without taking the lock.

**Joy-Con / touch-pad control** (`--keys`, opt-in, needs the click secret) turns
Switch input into held Mac keys and pointer motion, for example WASD movement in
games with the right stick as the mouse:

```bash
--keys                                     # left stick/D-pad -> W A S D, right stick -> pointer,
                                           # ZR or right-stick press -> left click (hold to drag),
                                           # A space, B e, X f, Y r, L q, R tab, ZL shift,
                                           # + escape, - m
--keymap 'btn-a=e,btn-b=space'             # remap; 'name=none' unmaps; repeatable
```

The viewer forwards three sources over a separate `/input` WebSocket, so key
presses never wait behind video frames: keyboard events the applet delivers
(Joy-Con buttons it maps to keys, or a USB keyboard), the Gamepad API if the
browser has it, and an on-screen stick plus A/B/X/Y pad (the **pad** button,
for handheld mode). Only keys and mouse buttons in the keymap can be pressed,
Command is not mappable, and the pointer stays on the captured screen. Held inputs are re-sent every 500 ms, and any key not refreshed for
1.5 s, or whose socket closes, is released, so a dropped link can't leave W
held down. Which Joy-Con buttons the Switch's browser passes to a page is not
documented. The HUD and `/health` `input.seen`/`input.probe` show what arrives, and the
server log names unmapped inputs (see MAC-SETUP.md, "Joy-Con controls").

`--text` (with `--keys`) adds the Switch's on-screen keyboard: a text box opens on
the Switch when a click focuses a text field in a normal Mac app, or when you press
**−** or tap **kbd**, and what you enter is typed on the Mac. It allows any text and
sends it over plain HTTP, so it's opt-in, and password fields don't open the box.

### Custom capture (`--source`)

Any command that writes MJPEG to stdout. Placeholders: `{w} {h} {fps}`,
`{q}` (1–100, friendly) and `{qv}` (ffmpeg `-q:v`, 1–31, inverted — use this one
inside ffmpeg commands; `{q}` would be wrong there).

```bash
# X11 / Xwayland
--source 'ffmpeg -hide_banner -loglevel error -f x11grab -framerate 20 -i :0.0 -vf scale={w}:{h} -c:v mjpeg -q:v {qv} -f mjpeg -'

# Wayland via OBS virtual camera (needs OBS + v4l2loopback; most reliable on KDE)
--source 'ffmpeg -hide_banner -loglevel error -f v4l2 -i /dev/video10 -c:v mjpeg -q:v {qv} -f mjpeg -'

# Wayland via PipeWire node (portal-granted; this box has vajpegenc but not jpegenc)
--source 'gst-launch-1.0 pipewiresrc path=<node-id> ! videoconvert ! video/x-raw,format=I420 ! vajpegenc ! fdsink'

# Hyprland: a real *extended* output, then capture just that output
hyprctl output create headless
--source 'sh -c "while :; do grim -o HEADLESS-1 -t jpeg -q 70 - | cat; sleep 0.1; done"'
```

Extended (not mirrored) desktop: on macOS use a BetterDisplay dummy
(MAC-SETUP.md); on Hyprland `hyprctl output create headless`; KDE Plasma has no
user-creatable virtual output, so on KDE this is a mirror of a screen region.

### On the host Mac

Full runbook: **[MAC-SETUP.md](MAC-SETUP.md)** — prerequisites (5 GHz Wi‑Fi, `brew install ffmpeg`,
Screen Recording permission for the terminal, firewall prompt, LAN IP
`<mac-lan-ip>`), both run lines, the Switch-side DNS steps (primary
`<mac-lan-ip>`, secondary `8.8.8.8`, portal applet, revert to Auto), and a
symptom→cause table (black frames = missing permission, etc.).

### Pointing the Switch at it (no modding)

1. On the host: `sudo python3 switchdisplay.py --switch` (ports 53 and 80 need
   root, and the server drops back to your user once they are bound; if another
   service holds port 80, stop it or use `--port 8090` with a redirect).
2. Switch → System Settings → Internet → Internet Settings → your Wi-Fi →
   Change Settings → DNS Settings → Manual → Primary DNS = laptop IP.
3. Connect: the Switch reports "Registration is required to use this network" →
   **Next** → the captive-portal applet loads `conntest.nintendowifi.net`,
   which now resolves to the laptop and shows the viewer.
4. When done, set DNS back to Automatic (online play needs it).

The applet closes the session when it decides the portal is satisfied; keeping
the page served at that hostname keeps it open in practice.

## 5. Tests

```bash
python3 -m unittest discover -s tests     # stdlib only, about 15 s
tools/check_viewer_es5.sh                  # parses the viewer script as strict ES5 (needs Node.js)
```

Both run on every push and pull request (`.github/workflows/tests.yml`). The
unit tests also scan the viewer script for ES6 syntax, which the Switch's browser
cannot parse.

## 6. First prototype checks (Linux host, 2026-10-23)

These predate Joy-Con input, the keyboard, fill modes and the control lock.

* server → `/health`: `{"frames": 406, "source_alive": true}` (test source)
* `/mjpeg`: 7 JPEG parts in 3 s, ~20 KB/frame
* `/ws`: 87 binary JPEG frames in 5.1 s (**17.2 fps** on the wire)
* real browser (Chromium, same page): mode `ws`, **25 frames rendered in 2 s
  (12.5 fps)**, canvas 960×540 with a decoded pixel `(254,255,0)` — i.e. the
  Blob→Image→`drawImage` path really draws
* layout check after the ES5 hardening: viewport 780×437 → canvas CSS
  777×437 (16:9 of 960×540) centred at `left:2px`, frames still arriving —
  the manual letterbox replaces `object-fit` (Safari 10+, unavailable on the Switch)
* page JS is ES5-clean (no arrows/let/const/template literals/fetch/async) —
  required for the Switch's WebKit 601.6-class engine
* DNS responder: `conntest.nintendowifi.net` and `ctest.cdn.nintendo.net`
  resolve to the configured answer; other names refused
* tap-to-click (all with `--click-dry-run` or a fake `cliclick` on PATH, so no
  cursor was ever moved): wrong secret → `403`; no secret configured → `403` and
  the page renders view-only; centre tap `(480,270)` of a 960×540 frame
  → `nx/ny 0.5`, `px/py 756,491` on a 1512×982 display; corners `(0,0)`/`(959,539)`
  → `0,0`/`1510,980`; out-of-frame `(5000,270)` → `400`; 15 rapid taps → mixed
  `200`/`429`; counters reconciled exactly (`received = limited +
  synthesized + bad_coords`); real execution path ran the synthesizer and the
  fake `cliclick` logged `c:756,491`
* **not tested then:** the Switch itself, and real screen
  capture on Wayland (needs a portal approval dialog / OBS v4l2loopback)

The viewer has since been used on a console (see MAC-SETUP.md), but no frame
rates from it are recorded here. Expect fewer than 12.5 fps (no JIT): fine for
text and static dashboards, poor for video or scrolling. `/health` `viewers`
gives the real figure for your setup.

## 7. Trying it yourself

* The host needs to be reachable on the LAN (no remote shell is assumed —
  install and run the server on the host itself).
* the Switch, its DNS change (and setting it back afterwards)
* a capture choice on the Mac: `brew install ffmpeg` (best), the zero-install
  `screencapture` loop, or Screen Recording permission for a portal-based tool
* only if the browser route disappoints: modding for Moonlight-NX (+ Sunshine on
  the Mac)
