# switchdisplay — a second display on a stock Nintendo Switch

Assessment + minimal prototype: can a stock Switch be a wireless second display
for a Linux laptop, and does spacedesk help?

**Short answer:** A stock Switch cannot use spacedesk. Its only possible spacedesk
role is the browser viewer, and spacedesk has **officially discontinued the HTML5
viewer** (support, Jan 2026: "The HTML5 viewer is not supported anymore"; Sep 2026:
"that version of the Viewer is already out-of-support"). The Switch has no
Android/iOS/Windows app, so there is no supported spacedesk client for it.

The Switch *can* show a second screen without modding — over **WebSocket + canvas**
in its built-in browser — and this folder contains a working server for that,
which runs on **macOS or Linux**. If you want latency and input, the honest route
is Sunshine + Moonlight-NX, which needs homebrew on the console.

---

## 1. What spacedesk actually is on the wire

Sources: datronicsoft manual (ports/firewall), an HTML5-viewer RE write-up
(`jeromeollivon-star/spacedesk-linux`), a Linux server reimplementation
(`leabergero/spacedesk-linux-server`), and the shipped viewer bundle
(`viewer.spacedesk.net/spacedesk.min.js`, 414 KB).

| Aspect | Finding |
|---|---|
| Ports | **TCP 28252** (session), **UDP 28252** (discovery) |
| Server platforms | **Windows 11/10** (driver 2.2.34) **and macOS 14.4+** (Apple MAC Driver **BETA 1.0.06**, public dmg). The Mac beta release notes admit "partial implementation of the display pipeline"; it added password protection/encryption in 1.0.03 |
| Supported viewers | Android, iOS, Windows — **and the HTML5 viewer, which is discontinued** |
| Discovery | UDP broadcast with magic `SPACEDESK-NET-CLIENT\0` |
| Transport | raw TCP to the Windows service; the **HTML5 viewer speaks the same packets over WebSocket** (`ws://<host>:28252/`) |
| Framing | 128-byte header + optional payload (`payloadLen` = u32 at offset 4), little-endian |
| Packet types | 1 ping, **2 frame buffer**, 3 visibility, 4 cursor pos, 5 cursor bitmap, **7 flow-control ack**, 8 disconnect, 9 rotation, 10 mouse, 11 keyboard, 12 touch |
| Video encoding | **JPEG tiles** (one JPEG per type-2 payload), the driver splits each frame into 4 horizontal bands. H.264 is *planned*, not shipped ("planning to update … to HTTPS, WSS and H264") |
| Handshake | 462-byte identification packet: version 4.8, clientType **`WindowsRemoteMonitor`** (=0) is mandatory, plus name/GUID |
| Latency model | **Flow-control ACK driven** — the driver sends no next frame until the client ACKs; ACK once per 4 fragments |
| Input back-channel | mouse (10) / keyboard (11) / touch (12) packets, same socket, header-only |
| HTML5 viewer endpoint | `http://viewer.spacedesk.net/` (public page) → opens `ws://<lan-ip>:28252/`. **Currently broken in 2025**: the page's CSP forbids `ws://` and Chrome/Edge/Firefox block public→private WebSocket (Private Network Access). Workaround per spacedesk support: save the page locally. Their rep: "spacedesk HTML5 Viewer hasn't been maintained for many years … it does not support modern secure HTTPS and WSS" |
| Viewer's browser requirements | verified by scanning `spacedesk.min.js`: `new WebSocket`, `<canvas>` (`getContext`×13, `drawImage`×7), `Blob`+`createObjectURL`, `Image`, `requestAnimationFrame`. **No WebRTC, no MediaSource, no WebCodecs, no ImageBitmap** |

So spacedesk's client is not a video player — it is a **JPEG-tile compositor over
a WebSocket**. That is the single most useful fact for the Switch question.

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

**Verdict for the Switch: feasible without mods, but not with spacedesk.**

- spacedesk has no Switch client and no Linux server; and its Windows driver is not reachable from a Linux host.
- Its *viewer model* (WebSocket + JPEG + canvas) is exactly what the stock
  captive-portal applet can do. So the honest route is: **serve our own
  WebSocket+JPEG page from the laptop's own DNS/HTTP**, not spacedesk's protocol.
- Constraints to expect: no JIT (expect single-digit to low-double-digit fps at
  ~960×540), one page at a time, the applet is a modal session (leaving it ends
  the display), and the Switch's DNS must be pointed at the laptop while you use it.

Homebrew alternative (needs *modding* — not done, not assumed): **Moonlight-NX**
(`rock88/moonlight-nx`) + **Sunshine** on the laptop (VA-API H.264 works on Intel
iGPUs). That is a real low-latency stream with input, but requires Atmosphère +
hbmenu with title redirection for full RAM. Only worth it if the browser route
proves too slow.

## 3. Host side: macOS first, Linux second

| Option | State |
|---|---|
| **spacedesk on macOS** | server exists — Apple MAC Driver **BETA 1.0.06**, macOS 14.4+, public dmg. But the only viewer the Switch could run (HTML5) is **discontinued**, and the beta is still "partial display pipeline" |
| Sidecar | macOS → **iPad/Apple** only; the Switch is not an Apple device — impossible |
| Duet Display / Luna | clients for iOS/Android/Mac/Windows, **no Switch** client — impossible |
| Deskreen / Weylus | browser viewers that require **WebRTC** → the Switch applet cannot join |
| macOS Screen Sharing (VNC 5900) | works, but needs a browser VNC client; noVNC is ES6-module based and will not parse on WebKit 601.6. Port 5900 is currently closed on the Mac |
| VNC + noVNC | needs WebSocket + canvas → capability-compatible, but heavier than needed for a passive second screen |
| **Sunshine (macOS host) + Moonlight-NX** | real low-latency streaming with input, VideoToolbox H.264; Moonlight-NX is **Switch homebrew** → needs modding |
| spacedesk server on Linux | does not exist officially; `leabergero/spacedesk-linux-server` is a hobby RE (GNOME-only capture) |
| **this prototype** | WebSocket + JPEG + canvas, the smallest capability set that matches the Switch; runs on macOS and Linux |

Recommendation: for a *passive* second screen (docs, logs, dashboards) use this
prototype. For interactive/low-latency use, Sunshine + Moonlight-NX (modded).
Do not build on spacedesk's HTML5 viewer — it is discontinued.

## 4. Running it

**The Mac runbook (exact commands, Switch steps, troubleshooting) is
[MAC-SETUP.md](MAC-SETUP.md).** Short version:

```bash
# mirror the built-in display: 960x540, 15 fps, quality 65
sudo python3 switchdisplay.py --preset mac-mirror --display-index 1 \
  --width 960 --height 540 --fps 15 --quality 65 \
  --bind <mac-lan-ip> --port 8090 \
  --dns-port 53 --dns-answer <mac-lan-ip>

# extended: a BetterDisplay dummy at 1280x720, captured by its index
sudo python3 switchdisplay.py --preset mac-extended --display-index 2 \
  --width 1280 --height 720 --fps 15 --quality 65 \
  --bind <mac-lan-ip> --port 8090 \
  --dns-port 53 --dns-answer <mac-lan-ip>
```

Presets: `test` (built-in pattern, default), `mac-mirror`, `mac-extended`,
`x11`. `--print-source` prints the resolved ffmpeg argv without starting the
server — use it to confirm the capture line before running.

Endpoints: `/` viewer page, `/ws` binary JPEG WebSocket, `/mjpeg` MJPEG
fallback, `POST /click` (tap-to-click), `/health` counters.

**Tap-to-click** is opt-in and single-purpose: a tap on the Switch becomes one
left click at that point on the Mac display. No drag, no hover, no right click,
no keyboard. Enabled only when a shared secret is supplied:

```bash
--click-secret 'long-random-string'        # or SWITCHDISPLAY_SECRET
--click-display-size 1512x982              # display size in POINTS (Retina!)
--click-display-origin 1512,0              # dummy placed right of a 1512-wide screen
--click-dry-run                            # resolve + log the click, execute nothing
```

Synthesizer: `cliclick` if installed (`brew install cliclick`), else
`osascript`/System Events. Both need **Accessibility** permission for the
terminal (capture needs **Screen Recording** — separate permission). Without a
secret `/click` returns 403 and the page renders view-only.

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

1. On the host: `sudo python3 switchdisplay.py --port 80 --dns-port 53 --dns-answer <lan-ip>`
   (port 53/80 need root; on this box **port 80 is already occupied** by another
   service, so use a redirect or stop that service first).
2. Switch → System Settings → Internet → Internet Settings → your Wi-Fi →
   Change Settings → DNS Settings → Manual → Primary DNS = laptop IP.
3. Connect: the Switch reports "Registration is required to use this network" →
   **Next** → the captive-portal applet loads `conntest.nintendowifi.net`,
   which now resolves to the laptop and shows the viewer.
4. When done, set DNS back to Automatic (online play needs it).

The applet closes the session when it decides the portal is satisfied; keeping
the page served at that hostname keeps it open in practice.

## 5. Verified (this box, 2026-10-23)

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
* DNS responder: `conntest.nintendowifi.net → 192.168.1.77` and
  `ctest.cdn.nintendo.net → 192.168.1.77`; other names refused
* tap-to-click (all with `--click-dry-run` or a fake `cliclick` on PATH, so no
  cursor was ever moved): wrong secret → `403`; no secret configured → `403` and
  the page renders `CLICK_ON = false`; centre tap `(480,270)` of a 960×540 frame
  → `nx/ny 0.5`, `px/py 756,491` on a 1512×982 display; corners `(0,0)`/`(959,539)`
  → `0,0`/`1510,980`; out-of-frame `(5000,270)` → `400`; 15 rapid taps → mixed
  `200`/`429`; `/health` counters reconciled exactly (`received = limited +
  synthesized + bad_coords`); real execution path ran the synthesizer and the
  fake `cliclick` logged `c:756,491`
* **not tested:** the Switch itself (needs Steven's console), and real screen
  capture on Wayland (needs a portal approval dialog / OBS v4l2loopback)

Expected on the Switch: lower fps than 12.5 (no JIT), fine for text and static
dashboards, poor for video or scrolling.

## 6. Needs Steven

* **The Mac** — nothing here is installed or run on it: Tailscale shows it active
  but every probed port (22, 5900, 28252, 3283, 445) is refused, so there is no
  remote shell and no spacedesk/VNC listener. Installing anything there is his call.
* the Switch, its DNS change (and setting it back afterwards)
* a capture choice on the Mac: `brew install ffmpeg` (best), the zero-install
  `screencapture` loop, or Screen Recording permission for a portal-based tool
* only if the browser route disappoints: modding for Moonlight-NX (+ Sunshine on
  the Mac)
