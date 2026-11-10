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
so cameras, an iPhone Continuity Camera or Desk View don't get in the way.

---

## (a) Mirror mode — 960×540, 15 fps, quality 65

The short form, with the preset's defaults and this Mac's LAN address filled in:

```bash
sudo python3 switchdisplay.py --switch --preset mac-mirror \
  --click-secret 'CHANGE-ME-to-a-long-random-string'
```

`--switch` stands for `--bind <mac-lan-ip> --port 80 --dns-port 53 --dns-answer
<mac-lan-ip>`. The same thing spelled out:

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

## Filling the Switch screen

A display that isn't 16:9, like a 1920×1243 MacBook screen, shows black bars on the
Switch. Tap **fill** at the top right of the viewer to switch live, or set the
starting mode with `--fill`:

| Mode | Result |
|---|---|
| `fit` (default) | Shows everything, with black bars |
| `crop` | Fills the screen and cuts off what overflows. On a 1920×1243 screen that's about 81 points at the top and at the bottom, which can include the menu bar and Dock. The pointer can move into the cut-off area |
| `stretch` | Fills the screen and shows everything, but changes the shape (about 15% wider on a 1920×1243 screen). Starting with `--fill stretch` also encodes frames at the full size, which is sharper than switching to it with the button |

Taps land in the right place in all three. A 16:9 display needs neither: use a 16:9
external monitor or dummy display with `--screen N`.

---

## Making text readable

From most to least effect:

1. **Make text bigger on the Mac:** System Settings → Displays → choose a setting
   toward **Larger Text**. A 1920×1243 screen shown 648 high is drawn at about half
   size, so a lower "looks like" resolution helps more than any encoder setting.
2. **Use `--profile text`:** frames are sized for the Switch viewer's 1280×648, so the
   browser draws them close to 1:1, with better scaling, light sharpening and quality 85.
3. **Use `--fill crop`:** the picture is drawn about 28% larger than `fit`, at the cost
   of the cut-off edges.

`--profile text` sends about twice the data per frame, and three times with `--fill
crop`, so the Switch may draw fewer frames. If it drops too far, add `--fps 20` or
lower `--quality`.

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

1. Nothing to install: the server posts the click through CoreGraphics.
2. Grant **Accessibility** permission to the terminal running the server
   (System Settings → Privacy & Security → Accessibility), then restart it.
   Screen Recording (needed for capture) is a *separate* permission.
3. Pick a long random secret and pass it with `--click-secret` (or set
   `SWITCHDISPLAY_SECRET`; the flag wins). With no secret the endpoint is
   refused and the page renders **view only**.
4. Display geometry is auto-detected for the `--screen` you capture (check the
   `[click] screen N: ... (auto-detected)` startup line). Override with
   `--click-display-size`/`--click-display-origin` only when capturing via
   a custom `--source`.

**Test it without moving the cursor first**

Add `--click-dry-run`: the server works out where the click would land and
reports it, but performs nothing.

```bash
curl -s -X POST http://<mac-lan-ip>:80/click \
  -H 'Content-Type: application/json' \
  -d '{"x":480,"y":270,"secret":"YOUR-SECRET"}'
# {"ok":true,"click":{"nx":0.5755,"ny":0.5,"px":1105,"py":622,"result":"dry-run",...}}
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
# "last_click": {"nx":..,"ny":..,"px":..,"py":..,"result":"clicked"}
```

**Guardrails**

* Secret compared in constant time; without one, `/click` returns 403 and the
  page has no tap handler at all.
* Rate limit: at most **10 taps per 3 s**, minimum **80 ms** apart → 429 beyond.
* Coordinates outside the current frame → 400 (no click).
* With click enabled, the default bind is the LAN address rather than all
  interfaces. Host validation rejects unrelated Host headers; use `--allow-host`
  for an intentional additional name.
* The page embeds the secret so its tap handler can POST, so the server gives the
  controlling page to **one device only**: the first to open the viewer, until the
  server restarts. The log says which (`[lock] control locked to <ip>`), and
  `/health` shows it under `control`. Other devices get the view-only page, and
  their clicks and input are refused even with the right secret. **Open the viewer
  on the Switch first** and check that the logged address is the Switch's.
* `--click-allow IP` (repeatable) names the controller up front, so nothing depends
  on who connects first. Use it with `--text`. The Switch's address is under
  System Settings → Internet → Connection Status.
* A browser on the Mac itself can always control and doesn't take the lock.
* To keep the secret out of shell history and `ps`, put options in a file, one per
  line (`--click-secret=...`), and pass `@FILE` on the command line.

---

## Joy-Con controls (optional)

Add `--keys` (it needs `--click-secret`) to control the Mac from the Switch:
the left stick walks with WASD, the right stick moves the Mac's pointer, and ZR
clicks:

```bash
sudo python3 switchdisplay.py --preset mac-mirror --screen 0 \
  --bind <mac-lan-ip> --port 80 --dns-port 53 --dns-answer <mac-lan-ip> \
  --click-secret "$(openssl rand -hex 16)" --keys
```

| Switch input | Name | Default Mac key |
|---|---|---|
| Left stick / D-pad / touch stick up, left, down, right | `up` `left` `down` `right` | W A S D |
| Right stick | (not remappable) | moves the Mac's pointer |
| ZR, right-stick press | `btn-zr` `btn-rstick` | left mouse button (hold to drag) |
| A, B, X, Y | `btn-a` `btn-b` `btn-x` `btn-y` | Space, E, F, R |
| L, R, ZL | `btn-l` `btn-r` `btn-zl` | Q, Tab, Shift |
| +, − | `btn-plus` `btn-minus` | Escape, M |
| Enter (USB keyboard, when no gamepad is connected) | `enter` | Return |
| USB keyboard letters/digits | `w`, `7`, … | the same key |
| Any other key code | `k<code>`, e.g. `k113` | not mapped |

Remap with `--keymap 'btn-a=e,enter=space'` (repeatable; `name=none` unmaps).
Mac key names: letters, digits, `space return tab escape delete shift ctrl
option up down left right minus equal comma period slash semicolon quote
bracketleft bracketright backslash grave`, plus `mouse-left` and `mouse-right`
(for example `--keymap btn-zl=mouse-right`). Command can't be mapped, so a stray
press can never send Cmd+Q.

The pointer stays on the screen being shown on the Switch, and a small tilt moves
it slowly for aiming. The Mac's arrow is only a few pixels tall on the Switch and
games may hide it, so a yellow marker shows where the right stick put it, then fades
after 2.5 s. Moves also carry relative motion, which games that capture
the mouse for camera control read, but that hasn't been tried in a game. Two
quick clicks don't count as a macOS double-click yet.

The left stick also moves a blue ring on the Switch. The Switch's browser draws
that ring itself, and CSS on the page doesn't hide it. It doesn't touch the Mac.
**To stop the ring moving, press the left stick in** (the footer's **Cursor**
hint). WASD and the right stick keep working in that mode. The browser also turns
A into Enter there, so while a gamepad is connected the page ignores that Enter.
This includes the Enter key on a USB keyboard.

**Two ways to send input**

* **Joy-Cons:** use them while the viewer is open. The browser reports them as a
  gamepad. Button layouts aren't documented, so check what arrives (below).
* **Touch pad (handheld):** tap **pad** at the top right. A stick appears
  bottom-left and A/B/X/Y bottom-right; they work with several fingers at once
  and never send taps. Tap **pad** again to hide it (that releases everything).

**Switch keyboard (`--text`, optional).** Add `--text` to `--keys` to type on the
Mac with the Switch's on-screen keyboard:

* **Automatic:** when a click or tap focuses a text field in a normal Mac app, a
  text box opens on the Switch. Games that draw their own text boxes aren't
  visible to macOS, so this doesn't trigger there.
* **By hand:** press **−** or tap **kbd** at any time. **−** no longer sends M in
  this mode, and pressing it again closes the box.
* The Switch's keyboard opens with the box (confirmed on a console). If it doesn't,
  tap the box, or press **A** on it with the left stick in select mode (press the
  left stick in). The Joy-Cons don't control the Mac while the box is open.

The text is typed where the Mac's focus is, without pressing Return, so a chat box
still needs a key mapped for that, for example `--keymap btn-plus=return`. Tabs and
line breaks are removed.

`--text` allows any text, not just the keymap, and the text crosses your network
over **plain HTTP**. Don't type passwords with it. Password fields don't open the
box, though **−** still can. Some games may read the text as repeated key presses.

**Find out what each Joy-Con button sends.** Press each button once with the
viewer open, then check any of these:

* the HUD on the Switch, e.g. `keys key up+` or `keys key k113-`. A trailing
  `gp` means the browser has the Gamepad API, and buttons then show as
  `pad0 btn-a+`.
* the server log: `[keys] unmapped input 'k113' (map it with --keymap k113=KEY)`
* `curl -s http://<mac-lan-ip>:80/health`, whose `"input": {"seen": {...}}`
  counts every name received, `"probe"` counts every event type the page received
  (including `gamepad-axes`, `right-stick`, `kb-opened` and `kb-sent`), and
  `"stats"` counts pointer `moves`, `typed` characters and `keyboard_prompts`

Then map what you found, for example `--keymap k113=space`. A button the
browser keeps for itself (Home always, possibly B for "back") never reaches
the page.

**Safety:** keys go through the same secret as taps, and only keys in the
keymap can be pressed. While held, the page re-sends each key every 500 ms.
A key not refreshed for 1.5 s, or whose connection closes, is released, so
leaving the applet or losing Wi‑Fi can't leave W held down. Like taps, keys
need **Accessibility** permission. Without it they're refused and the HUD says
so.

**Games:** this suits games that read the keyboard normally (most browser,
indie and many Steam games). Some games read raw HID devices and ignore
synthesized input.

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
| Viewer stays black, `/health` `frames` stays 0, server warns `no frames` | The server restarts the capture by itself and counts `source_restarts`. If it keeps repeating, a stuck capture from an earlier run is holding the screen: check with `pgrep -fl avfoundation`, stop it with `pkill -f avfoundation`, then restart the server. Also check that the display is awake |
| Server logs `capture exited (...)` | ffmpeg stopped; the line carries its last error and the capture is started again automatically. A display that was unplugged or renumbered needs a new `--screen N` |
| HUD says "view only" although `--click-secret` is set | Another device opened the viewer first and holds control (`[lock]` lines in the log, `control` in `/health`). Restart the server and open the viewer on the Switch first, or pass `--click-allow <switch-ip>` |
| HUD shows 0 fps on a still screen | Expected: frames identical to the last one aren't sent. `/health` `duplicate_frames` counts them |
| `Address already in use` on port 53 or 80 | mDNSResponder/another resolver holds it. Bind another interface/service, or use 5353/8090 and pf redirects. DNS example: `rdr on en0 inet proto udp from any to any port 53 -> <mac-lan-ip> port 5353` HTTP example: `rdr on en0 inet proto tcp from any to any port 80 -> <mac-lan-ip> port 8090` (also add matching `nat`/`pass` rules; `sudo pfctl -f <file> -e`) |
| Switch shows "Could not connect" | Mac firewall blocked `python3`, or the router has client isolation, or Mac and Switch are on different subnets/SSIDs |
| Stream works on the phone but not the Switch | re-check primary DNS is exactly `<mac-lan-ip>`, secondary `8.8.8.8`, and that the applet (not a normal page) is what opened |
| Very low fps | expected — the Switch's WebKit has **no JIT**; drop `--width/--height` (e.g. 800×450) or `--fps 10`, or lower `--quality`. `/health` `viewers` shows `render_fps` (what the Switch draws) next to `sent_fps` and `kb_per_frame`, so you can compare settings and `?mode=` choices |
| Words are hard to read | see [Making text readable](#making-text-readable) |
| Tap does nothing | HUD says `Accessibility permission missing` → grant it to the terminal, restart the terminal and the server. HUD says "view only" → `--click-secret` was not passed, or another device holds control (see the "view only" row above). HUD says `bad secret` → the secret in the page is stale, reload the applet |
| Clicks land offset by a constant amount | `--click-display-size` is in **points**, not Retina pixels; for a dummy also set `--click-display-origin` |
| `403 bad secret` in `/health` counters | page and server secrets differ — restart the server and reload the applet |
| Many `429` | rate limit (10 per 3 s, 80 ms apart) — that is deliberate |
| `503` + `failed` counter climbing | Accessibility permission missing (`"accessibility": false` in `/health`) |
| Joy-Con buttons do nothing | Check the HUD and `/health` `input.seen`. Nothing arriving means the browser keeps that button; use the touch pad. `k<code>` arriving means it isn't mapped: add `--keymap k<code>=KEY` |
| HUD shows `keys...` | the `/input` socket is reconnecting; check that the server was started with `--keys` |
| A key stays held in the game | it releases within 1.5 s by itself. If it doesn't, check `/health` `input.held` and report it |
| `502` + `failed` counter climbing | CoreGraphics refused the click; the response's `error` says why |
| Wrong screen shown | screens changed. Re-run `python3 switchdisplay.py --list-displays` and pick `--screen N` |
| `/health` frames climb by hundreds per second | a custom `--source` without `-fps_mode passthrough` (the presets include it) |

### Limits

* **Mirror** shows one whole display; **extended** needs BetterDisplay (macOS has
  no built-in user-creatable virtual display).
* **One session at a time**; the applet is modal and ends when you leave it.
* **One controlling device** per server run, unless `--click-allow` lists more.
* **Tap = one left click.** No drag, no hover, no right click. Keys only with
  `--keys`, and then only the keys in the keymap plus right-stick pointer motion.
  For analog movement and low latency use Sunshine (Mac) + Moonlight-NX (Switch,
  requires homebrew).
* Everything stays on the LAN; nothing is sent off-machine.
