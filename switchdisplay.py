#!/usr/bin/env python3
"""switchdisplay — a second display for any browser a Nintendo Switch can run.

Serves a JPEG frame stream three ways so an old WebKit (NetFront NX on the
Switch: WebKit 601.6 / Safari 9.1-class, no JIT) can view it:

    GET /        minimal ES5 viewer (WebSocket + <canvas>, <img> MJPEG fallback)
    GET /ws      binary WebSocket stream of JPEG frames
    GET /mjpeg   multipart/x-mixed-replace JPEG stream
    GET /health  JSON counters (frames produced, clients, source running)

Plus an optional tiny DNS responder so the Switch's captive-portal applet can
be pointed at this machine (see README) — the applet loads whatever
conntest.nintendowifi.net resolves to, and has no URL whitelist.

Capture is a pluggable command that writes a JPEG byte stream to stdout
(default: ffmpeg testsrc, so the tool runs anywhere). See README for real
Wayland/X11 capture presets.

No third-party Python packages. No kernel modules. Read-only towards the
running desktop unless you pick a capture command that captures it.
"""
from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import hmac
import json
import os
import queue
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import signal
import select
from types import SimpleNamespace

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

VIEWER_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>switchdisplay</title>
<style>
 html,body{margin:0;padding:0;background:#000;height:100%;overflow:hidden;
   -webkit-tap-highlight-color:rgba(0,0,0,0)}
 #wrap{position:fixed;top:0;left:0;right:0;bottom:0;display:block;background:#000;overflow:hidden}
 #fill{position:fixed;right:144px;top:6px;z-index:3;font:14px monospace;color:#8f8;
   background:rgba(0,0,0,.45);border:1px solid #4a4;border-radius:4px;padding:2px 8px}
 canvas,img{position:absolute;left:0;top:0;background:#000}
 #hud{position:fixed;left:6px;top:6px;font:12px monospace;color:#8f8;opacity:.6;z-index:3}
 #fs{position:fixed;right:6px;top:6px;z-index:3;font:14px monospace;color:#8f8;
   background:rgba(0,0,0,.45);border:1px solid #4a4;border-radius:4px;padding:2px 8px}
 .dot{position:fixed;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:9px;
   background:rgba(0,255,128,.55);pointer-events:none;z-index:2}
 #padbtn{position:fixed;right:48px;top:6px;z-index:3;font:14px monospace;color:#8f8;display:none;
   background:rgba(0,0,0,.45);border:1px solid #4a4;border-radius:4px;padding:2px 8px}
 #pad{display:none}
 #ptr{display:none;position:fixed;width:26px;height:26px;margin:-15px 0 0 -15px;border-radius:15px;
   border:2px solid #000;background:rgba(255,230,0,.35);box-shadow:0 0 0 2px #ff0;
   pointer-events:none;z-index:2}
 #kbd{position:fixed;right:96px;top:6px;z-index:3;font:14px monospace;color:#8f8;display:none;
   background:rgba(0,0,0,.45);border:1px solid #4a4;border-radius:4px;padding:2px 8px}
 #kbbar{display:none;position:fixed;left:8%;right:8%;top:36px;z-index:4;padding:8px;
   background:rgba(0,0,0,.85);border:1px solid #4a4;border-radius:6px}
 #kbin{width:70%;font:22px sans-serif;padding:6px}
 #kbgo,#kbno{font:18px sans-serif;padding:6px 10px;margin-left:6px}
 #stick{position:fixed;left:24px;bottom:24px;width:150px;height:150px;border-radius:75px;z-index:3;
   background:rgba(255,255,255,.10);border:2px solid rgba(136,255,136,.45)}
 #knob{position:absolute;left:50px;top:50px;width:50px;height:50px;border-radius:25px;
   background:rgba(136,255,136,.45)}
 .pb{position:fixed;width:58px;height:58px;border-radius:29px;z-index:3;font:bold 20px sans-serif;
   line-height:58px;text-align:center;color:#cfc;background:rgba(255,255,255,.10);
   border:2px solid rgba(136,255,136,.45)}
 .pb.on{background:rgba(136,255,136,.55);color:#000}
</style></head>
<body>
<div id="wrap"><canvas id="c"></canvas></div>
<div id="hud">connecting...</div>
<div id="fs" class="ui">&#9974;</div>
<div id="padbtn" class="ui">pad</div>
<div id="ptr"></div>
<div id="fill" class="ui">fit</div>
<div id="kbd" class="ui">kbd</div>
<form id="kbbar" class="ui" action="#"><input id="kbin" class="ui" type="text"
 autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
 placeholder="Type for the Mac"><button id="kbgo" class="ui" type="submit">Send</button><button
 id="kbno" class="ui" type="button">Close</button></form>
<div id="pad" class="ui">
 <div id="stick" class="ui"><div id="knob" class="ui"></div></div>
 <div class="pb ui" data-btn="btn-x" style="right:88px;bottom:148px">X</div>
 <div class="pb ui" data-btn="btn-y" style="right:152px;bottom:86px">Y</div>
 <div class="pb ui" data-btn="btn-a" style="right:24px;bottom:86px">A</div>
 <div class="pb ui" data-btn="btn-b" style="right:88px;bottom:24px">B</div>
</div>
<script>
(function () {
  var CLICK_ON = __CLICK_ON__;
  var CLICK_SECRET = __CLICK_SECRET__;
  var INPUT_ON = __INPUT_ON__;
  var TEXT_ON = __TEXT_ON__;
  var fill = __FILL__;
  var hud = document.getElementById('hud');
  var canvas = document.getElementById('c');
  var ctx = canvas.getContext('2d');
  var wsBase = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;
  var url = wsBase + '/ws';
  var frames = 0, last = new Date().getTime(), taps = 0, tapError = '';
  var inState = 'keys off', lastInput = '';
  var gpApi = !!(navigator.getGamepads || navigator.webkitGetGamepads);
  window.__frames = 0;
  window.__taps = 0;
  window.__mode = 'none';

  // -- Letterbox manually for the Switch's Safari 9-class WebKit.
  var mediaEl = null;
  function fit() {
    if (!mediaEl) return;
    var vw = window.innerWidth, vh = window.innerHeight;
    var sw = mediaEl.naturalWidth || mediaEl.width || 16, sh = mediaEl.naturalHeight || mediaEl.height || 9;
    if (!sw || !sh) return;
    // fit shows it all with bars; crop overflows #wrap; stretch ignores the shape.
    // Taps map through the element's box, so they stay accurate in every mode.
    var scale = fill === 'crop' ? Math.max(vw / sw, vh / sh) : Math.min(vw / sw, vh / sh);
    var w = fill === 'stretch' ? vw : Math.round(sw * scale);
    var h = fill === 'stretch' ? vh : Math.round(sh * scale);
    mediaEl.style.width = w + 'px';
    mediaEl.style.height = h + 'px';
    mediaEl.style.left = Math.round((vw - w) / 2) + 'px';
    mediaEl.style.top = Math.round((vh - h) / 2) + 'px';
  }
  window.addEventListener('resize', fit, false);
  var fillBtn = document.getElementById('fill');
  var cycleFill = function (ev) {
    ev.stopPropagation(); if (ev.preventDefault) { ev.preventDefault(); }
    fill = fill === 'fit' ? 'crop' : fill === 'crop' ? 'stretch' : 'fit';
    fillBtn.textContent = fill;
    window.__fill = fill;
    fit();
  };
  fillBtn.textContent = fill;
  window.__fill = fill;
  fillBtn.addEventListener('click', cycleFill, false);
  fillBtn.addEventListener('touchend', cycleFill, false);

  function hudUpdate() {
    var now = new Date().getTime();
    var fps = frames * 1000 / Math.max(1, now - last);
    hud.textContent = window.__mode + ' ' + Math.round(fps) + ' fps' +
      (CLICK_ON ? ' \\u00b7 taps ' + taps : ' \\u00b7 view only') +
      (INPUT_ON ? ' \\u00b7 ' + inState + ' ' + (lastInput || '-') + (gpApi ? ' gp' : '') : '') +
      (tapError ? ' \\u00b7 ' + tapError : '');
    if (now - last > 2000) { frames = 0; last = now; }
  }
  window.setInterval(hudUpdate, 500);

  function queryMode() {
    var q = location.search.substring(1).split('&'), i, p;
    for (i = 0; i < q.length; i++) { p = q[i].split('=');
      if (p[0] === 'mode' && /^(canvas|img|mjpeg|pull)$/.test(p[1])) return p[1]; }
    return 'canvas';
  }
  var wantedMode = queryMode();
  function mjpegFallback() {
    if (window.__mode === 'mjpeg') return;
    window.__mode = 'mjpeg';
    var img = document.createElement('img'), stall, last = new Date().getTime();
    mediaEl = img;
    function restart() { img.src = '/mjpeg?' + (new Date().getTime());
      window.clearTimeout(stall); stall = window.setTimeout(restart, 10000); }
    img.onload = function () { frames++; window.__frames++; last = new Date().getTime(); fit(); };
    img.onerror = restart; restart(); document.getElementById('wrap').appendChild(img);
    canvas.style.display = 'none';
  }
  if (wantedMode === 'mjpeg' || !window.WebSocket) { mjpegFallback(); } else {
    (function connect() {
      var ws;
      var gotMessage = false;
      var attempts = window.__wsAttempts || 0;
      window.__wsAttempts = attempts + 1;
      try { ws = new WebSocket(url); } catch (e) { mjpegFallback(); return; }
       window.__mode = wantedMode === 'pull' ? 'pull' : wantedMode;
       ws.binaryType = 'arraybuffer';
       ws.onopen = function () { hud.textContent = window.__mode + ' open';
         if (wantedMode === 'pull') ws.send('pull'); };
       ws.onmessage = function (ev) {
        if (!gotMessage) { gotMessage = true; window.__wsAttempts = 0; }
        if (window.__decoding) {
          window.__pending = ev.data;
          return;
        }
        decode(ev.data);
      };
      function done() {
        if (wantedMode === 'pull' && ws.readyState === 1) ws.send('n');
        window.__decoding = false;
        if (window.__pending) { var next = window.__pending; window.__pending = null; decode(next); }
      }
      function decode(data) {
        window.__decoding = true;
        var blob = new Blob([data], { type: 'image/jpeg' });
        var u = window.URL.createObjectURL(blob);
        var img = new Image();
        img.onload = function () {
          if (wantedMode === 'img' || wantedMode === 'pull') {
            // swap <img> elements; the previous one and its blob URL are freed
            var old = mediaEl;
            img.__url = u;
            document.getElementById('wrap').appendChild(img);
            canvas.style.display = 'none';
            mediaEl = img; fit();
            if (old && old !== canvas && old !== img) {
              if (old.__url) window.URL.revokeObjectURL(old.__url);
              if (old.parentNode) old.parentNode.removeChild(old);
            }
          } else {
            var resized = canvas.width !== img.width || canvas.height !== img.height;
            if (resized) { canvas.width = img.width; canvas.height = img.height; }
            ctx.drawImage(img, 0, 0);
            window.URL.revokeObjectURL(u);
            if (resized || mediaEl !== canvas) { mediaEl = canvas; fit(); }
          }
          frames++;
          window.__frames++;
          done();
        };
        img.onerror = function () {
          window.URL.revokeObjectURL(u);
          done();
        };
        img.src = u;
      }
       ws.onclose = function () {
        if (!gotMessage && attempts >= 3) { mjpegFallback(); return; }
        hud.textContent = 'reconnecting...';
        window.setTimeout(connect, 1000);
      };
      ws.onerror = function () { hud.textContent = 'error'; };
    })();
  }

  // ---- fullscreen toggle (its own button: body taps are clicks on the Mac) ----
  function fullscreen(ev) {
    ev.stopPropagation();
    var el = document.documentElement;
    if (el.requestFullscreen) { el.requestFullscreen(); }
    else if (el.webkitRequestFullscreen) { el.webkitRequestFullscreen(); }
  }
  document.getElementById('fs').addEventListener('click', fullscreen, false);
  document.getElementById('fs').addEventListener('touchend', fullscreen, false);

  // ---- tap-to-click: tap only. No drag, no hover, no right click, no keys. ----
  function flash(cx, cy) {
    var d = document.createElement('div');
    d.className = 'dot';
    d.style.left = cx + 'px';
    d.style.top = cy + 'px';
    document.body.appendChild(d);
    window.setTimeout(function () {
      if (d.parentNode) { d.parentNode.removeChild(d); }
    }, 220);
  }

  function sendTap(fx, fy) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/click', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status === 200 || xhr.status === 429) { tapError = ''; return; }
      var msg = 'tap failed (' + xhr.status + ')';
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
      tapError = msg;
    };
    xhr.send(JSON.stringify({
      x: Math.round(fx), y: Math.round(fy), secret: CLICK_SECRET
    }));
    taps++;
    window.__taps++;
  }

  function tapAt(ev) {
    if (!mediaEl) return;
    var t = (ev.changedTouches && ev.changedTouches[0]) || ev;
    if (typeof t.clientX !== 'number') return;
    var rect = mediaEl.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    // letterboxed element -> frame pixels (the element box is exactly the image)
    var fw = mediaEl.naturalWidth || mediaEl.width;
    var fh = mediaEl.naturalHeight || mediaEl.height;
    if (!fw || !fh) return;
    var fx = (t.clientX - rect.left) * (fw / rect.width);
    var fy = (t.clientY - rect.top) * (fh / rect.height);
    if (fx < 0 || fy < 0 || fx > fw || fy > fh) return;
    sendTap(fx, fy);
    flash(t.clientX, t.clientY);
  }

  if (CLICK_ON) {
    var lastTap = 0;
    var onTap = function (ev) {
      var target = ev.target;
      while (target) {
        if (/(^| )ui( |$)/.test(target.className || '')) { return; }  // buttons, pad
        target = target.parentNode;
      }
      var now = new Date().getTime();
      if (now - lastTap < 400) { return; }   // swallow the synthetic click after touch
      lastTap = now;
      tapAt(ev);
      if (ev.preventDefault) { ev.preventDefault(); }
    };
    if ('ontouchstart' in window) {
      document.body.addEventListener('touchend', onTap, false);
    } else {
      document.body.addEventListener('click', onTap, false);
    }
  }

  // ---- Joy-Con / keyboard / touch-pad input -> Mac keys (--keys) ----
  // Every source reduces to a name ('up', 'btn-a', 'w', 'k113'...); the server
  // maps names to Mac keys and ignores anything outside its keymap. A name is
  // down while any source holds it, and held names are re-sent every 500 ms so
  // the server can release keys whose key-up was lost.
  if (INPUT_ON) {
    var held = {}, inWs = null, inReady = false;
    var anyHeld = function (name) {
      var h = held[name], s;
      for (s in h) { if (h.hasOwnProperty(s)) { return true; } }
      return false;
    };
    // Switch keyboard (--text): the applet opens it for a focused text input.
    // While the box is open the Joy-Cons belong to the browser, not the Mac.
    var kbOpen = false, kbToggled = 0, kbin = document.getElementById('kbin');
    var kbbar = document.getElementById('kbbar');
    var openKb = function () {
      if (!TEXT_ON || kbOpen) { return; }
      releaseAll();
      kbOpen = true;
      window.__kbOpen = true;
      kbbar.style.display = 'block';
      kbin.value = '';
      kbin.focus();
      probe['kb-opened'] = (probe['kb-opened'] || 0) + 1;
      if (document.activeElement === kbin) { probe['kb-focused'] = (probe['kb-focused'] || 0) + 1; }
    };
    var closeKb = function () {
      kbOpen = false;
      window.__kbOpen = false;
      kbbar.style.display = 'none';
      kbin.blur();
    };
    var sendText = function () {
      if (kbin.value && inReady) {
        probe['kb-sent'] = (probe['kb-sent'] || 0) + 1;
        inWs.send('t:' + kbin.value);
        kbin.value = '';
      }
    };
    var press = function (src, name, down) {
      if (TEXT_ON && name === 'btn-minus') {
        // debounced: blur and releaseAll make a still-held button look pressed again
        var at = new Date().getTime();
        if (down && at - kbToggled > 500) {
          kbToggled = at;
          if (kbOpen) { closeKb(); } else { openKb(); }
        }
        return;
      }
      if (kbOpen && down) { return; }
      var was = anyHeld(name);
      if (down) { if (!held[name]) { held[name] = {}; } held[name][src] = true; }
      else if (held[name]) { delete held[name][src]; }
      var now = anyHeld(name);
      if (now === was) { return; }
      lastInput = src + ' ' + name + (now ? '+' : '-');
      window.__lastInput = lastInput;
      if (inReady) { inWs.send((now ? 'd:' : 'u:') + name); }
    };
    var releaseAll = function () {
      var name, s;
      for (name in held) { if (held.hasOwnProperty(name)) {
        for (s in held[name]) { if (held[name].hasOwnProperty(s)) { press(s, name, false); } } } }
      padHeld = {};  // or the next press of a released input would be ignored
    };
    // probe: which events does the browser deliver at all? Joy-Cons may drive
    // its own pointer or scrolling instead of sending keys.
    var probe = {}, probeSent = {};
    var probeCount = function (ev) { probe[ev.type] = (probe[ev.type] || 0) + 1; };
    var PROBED = ['keydown', 'keyup', 'keypress', 'mousemove', 'mousedown', 'mouseup', 'click',
                  'contextmenu', 'wheel', 'mousewheel', 'scroll', 'touchstart', 'touchend',
                  'focus', 'blur', 'gamepadconnected', 'gamepaddisconnected'], pn;
    for (pn = 0; pn < PROBED.length; pn++) { window.addEventListener(PROBED[pn], probeCount, true); }
    window.setInterval(function () {
      var name, pads, i, n = 0;
      if (!inReady) { return; }
      for (name in held) { if (held.hasOwnProperty(name) && anyHeld(name)) { inWs.send('d:' + name); } }
      probe['viewport-w'] = window.innerWidth;
      probe['viewport-h'] = window.innerHeight;
      if (gpApi) {
        pads = navigator.getGamepads ? navigator.getGamepads() : navigator.webkitGetGamepads();
        for (i = 0; pads && i < pads.length; i++) { if (pads[i]) { n++; } }
        probe['gamepads-connected'] = n;
      } else { probe['no-gamepad-api'] = 1; }
      for (name in probe) { if (probe.hasOwnProperty(name) && probe[name] !== probeSent[name]) {
        probeSent[name] = probe[name];
        inWs.send('p:' + name + '=' + probe[name]);
      } }
    }, 500);
    // The Mac's arrow is a few pixels tall here and games may hide it, so mark where
    // the right stick put the pointer. Fades when the stick rests.
    var ptr = document.getElementById('ptr'), ptrTimer = null;
    var showPointer = function (pos) {
      var c = pos.split(','), r = mediaEl && mediaEl.getBoundingClientRect();
      if (!r || !r.width) { return; }
      // a pointer in cropped-off area sticks to the screen edge rather than vanishing
      ptr.style.left = Math.max(0, Math.min(window.innerWidth, r.left + c[0] * r.width)) + 'px';
      ptr.style.top = Math.max(0, Math.min(window.innerHeight, r.top + c[1] * r.height)) + 'px';
      ptr.style.display = 'block';
      window.__ptr = pos;
      window.clearTimeout(ptrTimer);
      ptrTimer = window.setTimeout(function () { ptr.style.display = 'none'; }, 2500);
    };
    var inputConnect = function () {
      try { inWs = new WebSocket(wsBase + '/input'); } catch (e) { inState = 'keys: no ws'; return; }
      inWs.onopen = function () { inWs.send('auth:' + CLICK_SECRET); };
      inWs.onmessage = function (ev) {
        var t = String(ev.data);
        if (t === 'ok') { inReady = true; inState = 'keys'; return; }
        if (t === 'kb') { openKb(); return; }
        if (t.indexOf('c:') === 0) { showPointer(t.substring(2)); return; }
        if (t.indexOf('err:') === 0) { tapError = t.substring(4); }
      };
      inWs.onclose = function () {
        inReady = false; inState = 'keys...';
        window.setTimeout(inputConnect, 1000);
      };
    };
    inputConnect();
    window.addEventListener('blur', releaseAll, false);

    // keyboard events: what the Joy-Cons (or a USB keyboard) deliver to the page
    var KEYS = {8: 'backspace', 9: 'tab', 13: 'enter', 16: 'shift', 27: 'escape',
                32: 'space', 37: 'left', 38: 'up', 39: 'right', 40: 'down'};
    var keyName = function (ev) {
      var c = ev.keyCode || ev.which || 0;
      if (KEYS[c]) { return KEYS[c]; }
      if ((c >= 65 && c <= 90) || (c >= 48 && c <= 57)) { return String.fromCharCode(c).toLowerCase(); }
      return 'k' + c;
    };
    var padSeen = false;
    var keyEvent = function (ev, down) {
      if (ev.target === kbin) { return; }  // typing belongs to the text box
      var name = keyName(ev);
      // In its select mode the applet also turns A into Enter; the gamepad
      // already reports A, so forwarding both would press two Mac keys.
      if (!(padSeen && name === 'enter')) { press('key', name, down); }
      if (ev.preventDefault) { ev.preventDefault(); }
    };
    document.addEventListener('keydown', function (ev) { keyEvent(ev, true); }, false);
    document.addEventListener('keyup', function (ev) { keyEvent(ev, false); }, false);

    // Gamepad API, if this browser has it (standard mapping, Nintendo labels)
    var BTN = ['btn-b', 'btn-a', 'btn-y', 'btn-x', 'btn-l', 'btn-r', 'btn-zl', 'btn-zr',
               'btn-minus', 'btn-plus', 'btn-lstick', 'btn-rstick', 'up', 'down', 'left', 'right'];
    var padHeld = {};
    var STICK_DEAD = 0.15, STICK_SPEED = 900;  // full tilt: points per second on the Mac
    var moveX = 0, moveY = 0, lastPoll = new Date().getTime();
    var pointerStick = function (rx, ry) {
      var now = new Date().getTime(), dt = Math.min(now - lastPoll, 100) / 1000, mag, dx, dy;
      lastPoll = now;
      if (kbOpen) { moveX = 0; moveY = 0; return; }
      if (typeof rx !== 'number' || typeof ry !== 'number') { return; }
      mag = Math.sqrt(rx * rx + ry * ry);
      if (mag < STICK_DEAD) { moveX = 0; moveY = 0; return; }
      probe['right-stick'] = (probe['right-stick'] || 0) + 1;
      // squared response: small tilts aim precisely, full tilt crosses the screen.
      // Fractions carry over so a slow tilt still moves.
      moveX += rx * mag * STICK_SPEED * dt;
      moveY += ry * mag * STICK_SPEED * dt;
      dx = moveX < 0 ? Math.ceil(moveX) : Math.floor(moveX);
      dy = moveY < 0 ? Math.ceil(moveY) : Math.floor(moveY);
      moveX -= dx; moveY -= dy;
      if ((dx || dy) && inReady) { inWs.send('m:' + dx + ',' + dy); }
    };
    var padSet = function (src, name, on) {
      if (on === !!padHeld[src + name]) { return; }
      padHeld[src + name] = on;
      press(src, name, on);
    };
    if (gpApi) {
      window.setInterval(function () {
        var pads = navigator.getGamepads ? navigator.getGamepads() : navigator.webkitGetGamepads();
        var i, b, p, v, ax;
        for (i = 0; pads && i < pads.length; i++) {
          p = pads[i];
          if (!p) { continue; }
          padSeen = true;
          probe['gamepad-buttons'] = p.buttons.length;
          probe['gamepad-axes'] = (p.axes || []).length;
          for (b = 0; b < p.buttons.length; b++) {
            v = p.buttons[b];
            padSet('pad' + i, BTN[b] || ('pad-b' + b), typeof v === 'object' ? v.pressed : v > 0.5);
          }
          ax = p.axes || [];
          padSet('stick' + i, 'left', ax[0] < -0.5); padSet('stick' + i, 'right', ax[0] > 0.5);
          padSet('stick' + i, 'up', ax[1] < -0.5); padSet('stick' + i, 'down', ax[1] > 0.5);
          pointerStick(ax[2], ax[3]);
        }
      }, 33);
    }

    // on-screen touch pad: stick (8-way) + A/B/X/Y
    var pad = document.getElementById('pad'), padbtn = document.getElementById('padbtn');
    var stick = document.getElementById('stick'), knob = document.getElementById('knob');
    padbtn.style.display = 'block';
    var togglePad = function (ev) {
      ev.stopPropagation(); if (ev.preventDefault) { ev.preventDefault(); }
      var show = pad.style.display !== 'block';
      pad.style.display = show ? 'block' : 'none';
      if (!show) { releaseAll(); }
    };
    padbtn.addEventListener('click', togglePad, false);
    padbtn.addEventListener('touchend', togglePad, false);

    if (TEXT_ON) {
      var kbd = document.getElementById('kbd');
      var kbOpenEv = function (ev) {
        ev.stopPropagation(); if (ev.preventDefault) { ev.preventDefault(); }
        openKb();
      };
      kbd.style.display = 'block';
      kbd.addEventListener('click', kbOpenEv, false);
      kbd.addEventListener('touchend', kbOpenEv, false);
      kbbar.addEventListener('submit', function (ev) {
        ev.preventDefault(); sendText(); closeKb();
      }, false);
      // the applet's keyboard returns its text as a change, so the box is done
      kbin.addEventListener('change', function () {
        if (kbin.value) { sendText(); closeKb(); }
      }, false);
      var kbno = document.getElementById('kbno');
      var kbDiscard = function () { kbin.value = ''; };  // before blur can fire change
      kbno.addEventListener('mousedown', kbDiscard, false);
      kbno.addEventListener('touchstart', kbDiscard, false);
      kbno.addEventListener('click', function (ev) {
        ev.preventDefault(); kbin.value = ''; closeKb();
      }, false);
    }

    var stickId = null, stickX = 0, stickY = 0;
    var stickMove = function (x, y) {
      var dx = x - stickX, dy = y - stickY, dz = 18;
      padSet('touch', 'up', dy < -dz); padSet('touch', 'down', dy > dz);
      padSet('touch', 'left', dx < -dz); padSet('touch', 'right', dx > dz);
      var dist = Math.sqrt(dx * dx + dy * dy), m = dist > 50 ? 50 / dist : 1;
      knob.style.left = Math.round(50 + dx * m) + 'px';
      knob.style.top = Math.round(50 + dy * m) + 'px';
    };
    var stickStart = function (x, y, id) {
      var r = stick.getBoundingClientRect();
      stickId = id; stickX = r.left + r.width / 2; stickY = r.top + r.height / 2;
      stickMove(x, y);
    };
    var stickEnd = function () { stickId = null; stickMove(stickX, stickY); };
    var findTouch = function (ev) {
      var i, t = ev.changedTouches || [];
      for (i = 0; i < t.length; i++) { if (t[i].identifier === stickId) { return t[i]; } }
      return null;
    };
    stick.addEventListener('touchstart', function (ev) {
      var t = ev.changedTouches[0];
      stickStart(t.clientX, t.clientY, t.identifier);
      ev.preventDefault(); ev.stopPropagation();
    }, false);
    document.addEventListener('touchmove', function (ev) {
      var t = stickId !== null && findTouch(ev);
      if (t) { stickMove(t.clientX, t.clientY); }
      if (ev.preventDefault) { ev.preventDefault(); }
    }, false);
    var stickTouchEnd = function (ev) { if (stickId !== null && findTouch(ev)) { stickEnd(); } };
    document.addEventListener('touchend', stickTouchEnd, false);
    document.addEventListener('touchcancel', stickTouchEnd, false);
    stick.addEventListener('mousedown', function (ev) {
      stickStart(ev.clientX, ev.clientY, 'mouse'); ev.preventDefault(); ev.stopPropagation();
    }, false);
    document.addEventListener('mousemove', function (ev) {
      if (stickId === 'mouse') { stickMove(ev.clientX, ev.clientY); }
    }, false);
    document.addEventListener('mouseup', function () { if (stickId === 'mouse') { stickEnd(); } }, false);

    var bindBtn = function (el) {
      var name = el.getAttribute('data-btn');
      var on = function (ev) {
        padSet('touch', name, true); el.className = 'pb ui on';
        ev.preventDefault(); ev.stopPropagation();
      };
      var off = function (ev) {
        padSet('touch', name, false); el.className = 'pb ui';
        ev.preventDefault(); ev.stopPropagation();
      };
      el.addEventListener('touchstart', on, false);
      el.addEventListener('touchend', off, false);
      el.addEventListener('touchcancel', off, false);
      el.addEventListener('mousedown', on, false);
      el.addEventListener('mouseup', off, false);
      el.addEventListener('mouseleave', off, false);
    };
    var pbs = pad.getElementsByTagName('div'), pi;
    for (pi = 0; pi < pbs.length; pi++) {
      if (pbs[pi].getAttribute('data-btn')) { bindBtn(pbs[pi]); }
    }
  }
})();
</script>
</body></html>
"""


def render_viewer(click_enabled: bool, secret: str, input_enabled: bool = False,
                  text_enabled: bool = False, fill: str = "fit") -> bytes:
    """Inject the click/input flags and secret into the static page (JS literals, JSON-escaped)."""
    literal = json.dumps(secret if click_enabled or input_enabled else "")
    literal = (literal.replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026"))
    return (VIEWER_HTML
            .replace(b"__CLICK_ON__", b"true" if click_enabled else b"false")
            .replace(b"__INPUT_ON__", b"true" if input_enabled else b"false")
            .replace(b"__TEXT_ON__", b"true" if input_enabled and text_enabled else b"false")
            .replace(b"__FILL__", json.dumps(fill).encode())
            .replace(b"__CLICK_SECRET__", literal.encode()))


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width/height from a JPEG's SOF marker (enough for coordinate mapping)."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg
    return None


@functools.lru_cache(maxsize=1)
def _coregraphics():
    """CoreGraphics via ctypes (macOS only), or None. Keeps the script stdlib-only."""
    if sys.platform != "darwin":
        return None
    import ctypes
    import ctypes.util

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class CGSize(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    class CGRect(ctypes.Structure):
        _fields_ = [("origin", CGPoint), ("size", CGSize)]

    try:
        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics") or
                         "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or
                         "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError:
        return None
    cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.POINTER(ctypes.c_uint32)]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = CGRect
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = CGPoint
    cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
    cg.CGEventSetIntegerValueField.restype = None
    cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                   ctypes.POINTER(ctypes.c_uint16)]
    cg.CGEventKeyboardSetUnicodeString.restype = None
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventPost.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    return SimpleNamespace(cg=cg, cf=cf, CGPoint=CGPoint, ctypes=ctypes)


def mac_displays() -> list[dict]:
    """Active displays in AVFoundation "Capture screen N" order.

    ffmpeg's avfoundation input numbers screens in CGGetActiveDisplayList order,
    so entry N here is "Capture screen N". Bounds are global points with a
    top-left origin — exactly what cliclick and CGEvent expect.
    """
    q = _coregraphics()
    if q is None:
        return []
    ids = (q.ctypes.c_uint32 * 16)()
    count = q.ctypes.c_uint32(0)
    if q.cg.CGGetActiveDisplayList(16, ids, q.ctypes.byref(count)) != 0:
        return []
    out = []
    for i in range(count.value):
        b = q.cg.CGDisplayBounds(ids[i])
        out.append({"screen": i, "id": ids[i],
                    "origin": (int(b.origin.x), int(b.origin.y)),
                    "size": (int(b.size.width), int(b.size.height))})
    return out


ACCESSIBILITY_HELP = ("grant Accessibility to the app that launched the server: System Settings "
                      "> Privacy & Security > Accessibility > enable Terminal (or your terminal), "
                      "then quit and reopen it")


def accessibility_trusted(prompt: bool = False) -> bool | None:
    """macOS: may this process post input events? None off macOS.

    Without the permission macOS drops synthesized clicks silently, so this is
    the only way to tell "clicked" from "nothing happened". With prompt=True
    macOS shows its own dialog offering to open the Accessibility settings.
    """
    if sys.platform != "darwin":
        return None
    import ctypes
    try:
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/"
                         "ApplicationServices")
        if not prompt:
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(ax.AXIsProcessTrusted())
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                          ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        keys = (ctypes.c_void_p * 1)(ctypes.c_void_p.in_dll(ax, "kAXTrustedCheckOptionPrompt").value)
        values = (ctypes.c_void_p * 1)(ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value)
        options = cf.CFDictionaryCreate(
            None, keys, values, 1,
            ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
            ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
        ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        try:
            return bool(ax.AXIsProcessTrustedWithOptions(options))
        finally:
            cf.CFRelease(options)
    except (OSError, ValueError, AttributeError):
        return None


def quartz_click(px: int, py: int) -> None:
    """One left click at global point (px, py) via CGEvent (needs Accessibility)."""
    q = _coregraphics()
    if q is None:
        raise OSError("CoreGraphics unavailable")
    point = q.CGPoint(px, py)
    # move, down, up: the move makes hover-sensitive targets see the pointer first
    for kind in (5, 1, 2):  # kCGEventMouseMoved, LeftMouseDown, LeftMouseUp
        event = q.cg.CGEventCreateMouseEvent(None, kind, point, 0)
        if not event:
            raise OSError("CGEventCreateMouseEvent failed")
        q.cg.CGEventPost(0, event)  # kCGHIDEventTap
        q.cf.CFRelease(event)


# macOS virtual keycodes (Carbon kVK_*) for the keys a keymap may target.
# Command and Caps Lock are left out on purpose: a stray Cmd+Q is not a game input.
MAC_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32,
    "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
    "minus": 27, "equal": 24, "bracketleft": 33, "bracketright": 30, "semicolon": 41,
    "quote": 39, "comma": 43, "period": 47, "slash": 44, "backslash": 42, "grave": 50,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "shift": 56, "ctrl": 59, "option": 58,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

# Keymap targets that are mouse buttons rather than keys (CGMouseButton numbers).
MOUSE_BUTTONS = {"mouse-left": 0, "mouse-right": 1}

# Viewer input names -> Mac keys. Directions come from the stick, D-pad, arrow
# keys or the touch pad; btn-* are Joy-Con/touch buttons; plain letters and
# digits come from a USB keyboard on the Switch. Override with --keymap.
DEFAULT_KEYMAP = {
    "up": "w", "left": "a", "down": "s", "right": "d",
    "btn-a": "space", "btn-b": "e", "btn-x": "f", "btn-y": "r",
    "btn-l": "q", "btn-r": "tab", "btn-zl": "shift", "btn-zr": "mouse-left",
    "btn-rstick": "mouse-left",
    "btn-plus": "escape", "btn-minus": "m",
    "enter": "return", "space": "space", "escape": "escape", "tab": "tab",
    "backspace": "delete", "shift": "shift",
    **{c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}


def parse_keymap(specs: list[str]) -> dict[str, str]:
    """Merge 'name=key[,name=key]' specs over DEFAULT_KEYMAP; key 'none' unmaps."""
    keymap = dict(DEFAULT_KEYMAP)
    for spec in specs:
        for item in filter(None, (p.strip() for p in spec.split(","))):
            name, sep, key = item.partition("=")
            name, key = name.strip().lower(), key.strip().lower()
            if not sep or not name:
                raise SystemExit(f"--keymap: expected name=key, got {item!r}")
            if key == "none":
                keymap.pop(name, None)
            elif key in MAC_KEYCODES or key in MOUSE_BUTTONS:
                keymap[name] = key
            else:
                raise SystemExit(f"--keymap: unknown Mac key {key!r} "
                                 f"(known: {', '.join(sorted([*MAC_KEYCODES, *MOUSE_BUTTONS]))})")
    return keymap


def quartz_key(code: int, down: bool) -> None:
    """Post one key down/up via CGEvent (needs Accessibility, like clicks)."""
    q = _coregraphics()
    if q is None:
        raise OSError("CoreGraphics unavailable")
    event = q.cg.CGEventCreateKeyboardEvent(None, code, down)
    if not event:
        raise OSError("CGEventCreateKeyboardEvent failed")
    q.cg.CGEventPost(0, event)  # kCGHIDEventTap
    q.cf.CFRelease(event)


def _pointer_location(q):
    event = q.cg.CGEventCreate(None)
    if not event:
        raise OSError("CGEventCreate failed")
    location = q.cg.CGEventGetLocation(event)
    q.cf.CFRelease(event)
    return location


def quartz_mouse_button(button: int, down: bool) -> None:
    """Press or release a mouse button wherever the pointer is now."""
    q = _coregraphics()
    if q is None:
        raise OSError("CoreGraphics unavailable")
    # kCGEventLeftMouseDown/Up = 1/2, kCGEventRightMouseDown/Up = 3/4
    kind = (1 if down else 2) if button == 0 else (3 if down else 4)
    event = q.cg.CGEventCreateMouseEvent(None, kind, _pointer_location(q), button)
    if not event:
        raise OSError("CGEventCreateMouseEvent failed")
    q.cg.CGEventPost(0, event)
    q.cf.CFRelease(event)


def quartz_mouse_move(dx: int, dy: int, bounds: tuple[int, int, int, int] | None,
                      kind: int, button: int) -> tuple[float, float]:
    """Move the pointer by (dx, dy), staying inside bounds; returns where it ended up."""
    q = _coregraphics()
    if q is None:
        raise OSError("CoreGraphics unavailable")
    location = _pointer_location(q)
    x, y = location.x + dx, location.y + dy
    if bounds:
        bx, by, bw, bh = bounds
        x, y = min(max(x, bx), bx + bw - 1), min(max(y, by), by + bh - 1)
    event = q.cg.CGEventCreateMouseEvent(None, kind, q.CGPoint(x, y), button)
    if not event:
        raise OSError("CGEventCreateMouseEvent failed")
    # explicit deltas: a game that captures the pointer reads these, not the position
    q.cg.CGEventSetIntegerValueField(event, 4, dx)  # kCGMouseEventDeltaX
    q.cg.CGEventSetIntegerValueField(event, 5, dy)  # kCGMouseEventDeltaY
    q.cg.CGEventPost(0, event)
    q.cf.CFRelease(event)
    return x, y


MAX_TEXT = 1000  # characters per Switch keyboard submission


def clean_text(text: str) -> str:
    """Printable text only: no newline, tab or escape can act as a key press."""
    return "".join(c for c in text
                   if unicodedata.category(c) not in ("Cc", "Zl", "Zp"))[:MAX_TEXT]


def quartz_text(text: str) -> None:
    """Type text into the focused app as Unicode key events (needs Accessibility)."""
    q = _coregraphics()
    if q is None:
        raise OSError("CoreGraphics unavailable")
    chunks, chunk = [], []
    for char in text:  # a chunk boundary must not split a surrogate pair
        units = len(char.encode("utf-16-le")) // 2
        if len(chunk) + units > 20:  # events carry at most 20 UTF-16 units
            chunks.append(chunk)
            chunk = []
        chunk.extend(int.from_bytes(char.encode("utf-16-le")[i:i + 2], "little")
                     for i in range(0, units * 2, 2))
    if chunk:
        chunks.append(chunk)
    for chunk in chunks:
        buf = (q.ctypes.c_uint16 * len(chunk))(*chunk)
        for down in (True, False):
            event = q.cg.CGEventCreateKeyboardEvent(None, 0, down)
            if not event:
                raise OSError("CGEventCreateKeyboardEvent failed")
            q.cg.CGEventKeyboardSetUnicodeString(event, len(chunk), buf)
            q.cg.CGEventPost(0, event)
            q.cf.CFRelease(event)
        time.sleep(0.01)


def _frontmost_pid() -> int | None:
    """The frontmost app's pid. lsappinfo, as NSWorkspace goes stale without a run loop."""
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                               timeout=1).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "pid", front], capture_output=True,
                              text=True, timeout=1).stdout
        match = re.search(r'\bpid"?\s*=\s*(\d+)', info)  # format varies by macOS version
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def focused_text_field() -> bool | None:
    """Is a non-password text field focused on the Mac? None if that can't be read.

    Only sees fields exposed through Accessibility: games drawing their own text
    boxes never match. Password fields don't count, because the Switch keyboard's
    text would cross the LAN over plain HTTP.
    """
    if sys.platform != "darwin":
        return None
    import ctypes
    try:
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/"
                         "ApplicationServices")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError:
        return None
    utf8 = 0x08000100
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
    ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
    ax.AXUIElementSetMessagingTimeout.argtypes = [ctypes.c_void_p, ctypes.c_float]
    ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
    ax.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_void_p)]

    def copy(element, name: bytes):
        key = cf.CFStringCreateWithCString(None, name, utf8)
        value = ctypes.c_void_p()
        err = ax.AXUIElementCopyAttributeValue(element, key, ctypes.byref(value))
        cf.CFRelease(key)
        return value.value if err == 0 else None

    def text(element, name: bytes) -> str:
        value = copy(element, name)
        if not value:
            return ""
        buf = ctypes.create_string_buffer(128)
        ok = cf.CFStringGetCString(value, buf, 128, utf8)
        cf.CFRelease(value)
        return buf.value.decode() if ok else ""

    pid = _frontmost_pid()
    if pid is None:
        return None
    # ask the app itself: the system-wide element fails with -25204 on macOS 27
    app = ax.AXUIElementCreateApplication(pid)
    ax.AXUIElementSetMessagingTimeout(app, 0.25)  # a hung app must not stall us
    focused = copy(app, b"AXFocusedUIElement")
    cf.CFRelease(app)
    if not focused:
        return False
    role, subrole = text(focused, b"AXRole"), text(focused, b"AXSubrole")
    cf.CFRelease(focused)
    return role in ("AXTextField", "AXTextArea", "AXComboBox") and subrole != "AXSecureTextField"


class KeyInjector:
    """Joy-Con/touch input -> held Mac keys and pointer motion, restricted to a keymap.

    The viewer re-sends "down" for every held input twice a second; an input
    not refreshed within hold_s is released. A dropped Wi-Fi link or a closed
    applet therefore cannot leave W held down on the Mac.
    """

    MAX_MOVE = 400  # points per message; a viewer can't fling the pointer further

    def __init__(self, keymap: dict[str, str], dry_run: bool, hold_s: float = 1.5,
                 poster=None, button_poster=None, mover=None,
                 bounds: tuple[int, int, int, int] | None = None,
                 text_enabled: bool = False, text_poster=None, focus_check=None,
                 focus_delay_s: float = 0.25):
        self.keymap = keymap
        self.dry_run = dry_run
        self.hold_s = hold_s
        self.poster = poster or quartz_key
        self.button_poster = button_poster or quartz_mouse_button
        self.mover = mover or quartz_mouse_move
        self.bounds = bounds
        self.last_pointer: tuple[float, float] | None = None
        self.text_enabled = text_enabled
        self.text_poster = text_poster or quartz_text
        self.focus_check = focus_check or focused_text_field
        self.focus_delay_s = focus_delay_s
        self._sockets: dict[socket.socket, threading.Lock] = {}
        self.stats = {"received": 0, "pressed": 0, "released": 0, "unmapped": 0,
                      "expired": 0, "failed": 0, "moves": 0, "typed": 0, "keyboard_prompts": 0}
        self.seen: dict[str, int] = {}
        self.probe: dict[str, int] = {}
        self.connections = 0
        self._holders: dict[str, dict[tuple, float]] = {}  # mac key -> {(conn, name): t}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._expire_loop, daemon=True).start()

    def _post(self, key: str, down: bool) -> None:
        self.stats["pressed" if down else "released"] += 1
        if self.dry_run:
            return
        try:
            if key in MOUSE_BUTTONS:
                self.button_poster(MOUSE_BUTTONS[key], down)
            else:
                self.poster(MAC_KEYCODES[key], down)
        except OSError:
            self.stats["failed"] += 1

    def type_text(self, text: str) -> str | None:
        """Type Switch keyboard text on the Mac; returns an error for the viewer, or None."""
        if not self.text_enabled:
            return None
        text = clean_text(text)
        if not text:
            return None
        with self._lock:
            self.stats["typed"] += len(text)
            if self.dry_run:
                return None
            if accessibility_trusted() is False:
                self.stats["failed"] += 1
                return "Accessibility permission missing"
            try:
                self.text_poster(text)
            except OSError:
                self.stats["failed"] += 1
        return None

    def add_socket(self, sock: socket.socket) -> threading.Lock:
        lock = threading.Lock()
        with self._lock:
            self._sockets[sock] = lock
        return lock

    def remove_socket(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.pop(sock, None)

    def after_click(self) -> None:
        """A click just happened: if it focused a text field, prompt the viewer."""
        if self.text_enabled:
            threading.Timer(self.focus_delay_s, self._prompt_if_text_field).start()

    def _prompt_if_text_field(self) -> None:
        if not self.focus_check():
            return
        with self._lock:
            self.stats["keyboard_prompts"] += 1
            sockets = list(self._sockets.items())
        for sock, lock in sockets:
            try:
                with lock:
                    ws_send_frame(sock, b"kb", 0x1)
            except OSError:
                pass

    def move(self, dx: int, dy: int) -> str | None:
        """Move the pointer by a viewer delta; returns an error for the viewer, or None."""
        dx = max(-self.MAX_MOVE, min(self.MAX_MOVE, dx))
        dy = max(-self.MAX_MOVE, min(self.MAX_MOVE, dy))
        with self._lock:
            self.stats["moves"] += 1
            if self.dry_run:
                if self.bounds:  # a pretend pointer, so the viewer's marker can be tried safely
                    bx, by, bw, bh = self.bounds
                    x, y = self.last_pointer or (bx + bw / 2, by + bh / 2)
                    self.last_pointer = (min(max(x + dx, bx), bx + bw - 1),
                                         min(max(y + dy, by), by + bh - 1))
                return None
            if accessibility_trusted() is False:
                self.stats["failed"] += 1
                return "Accessibility permission missing"
            # a held button turns motion into a drag; 5/6/7 = moved, left/right dragged
            if "mouse-left" in self._holders:
                kind, button = 6, 0
            elif "mouse-right" in self._holders:
                kind, button = 7, 1
            else:
                kind, button = 5, 0
            try:
                self.last_pointer = self.mover(dx, dy, self.bounds, kind, button)
            except OSError:
                self.stats["failed"] += 1
        return None

    def pointer_message(self) -> bytes | None:
        """Where the pointer is on the captured screen, as 'c:<0..1>,<0..1>' for the viewer."""
        if not self.bounds or not self.last_pointer:
            return None
        bx, by, bw, bh = self.bounds
        x, y = self.last_pointer
        return b"c:%.4f,%.4f" % ((x - bx) / bw, (y - by) / bh)

    def press(self, conn, name: str, down: bool) -> str | None:
        """Apply one input event; returns an error for the viewer, or None."""
        name = name[:32].lower()
        with self._lock:
            self.stats["received"] += 1
            if name in self.seen or len(self.seen) < 64:  # bounded probe record
                self.seen[name] = self.seen.get(name, 0) + 1
            key = self.keymap.get(name)
            if key is None:
                self.stats["unmapped"] += 1
                if self.seen.get(name) == 1:
                    print(f"[keys] unmapped input '{name}' "
                          f"(map it with --keymap {name}=KEY)", flush=True)
                return None
            if down and not self.dry_run and accessibility_trusted() is False:
                self.stats["failed"] += 1
                return "Accessibility permission missing"
            holders = self._holders.setdefault(key, {})
            if down:
                if not holders:
                    self._post(key, True)
                holders[(conn, name)] = time.monotonic()
            elif holders.pop((conn, name), None) is not None and not holders:
                del self._holders[key]
                self._post(key, False)
                if key == "mouse-left":
                    self.after_click()
        return None

    def note_probe(self, item: str) -> None:
        """Record a viewer 'event=count' total, logging each event type once."""
        name, _, count = item.partition("=")
        name = name[:32]
        if not count.isdigit() or (name not in self.probe and len(self.probe) >= 32):
            return
        with self._lock:
            if name not in self.probe:
                print(f"[probe] page received '{name}'", flush=True)
            self.probe[name] = int(count)

    def release_all(self, conn) -> None:
        with self._lock:
            for key in list(self._holders):
                holders = self._holders[key]
                for owner in [o for o in holders if o[0] is conn]:
                    del holders[owner]
                if not holders:
                    del self._holders[key]
                    self._post(key, False)

    def _expire_loop(self) -> None:
        while not self._stop.wait(0.25):
            now = time.monotonic()
            with self._lock:
                for key in list(self._holders):
                    holders = self._holders[key]
                    for owner in [o for o, t in holders.items() if now - t > self.hold_s]:
                        del holders[owner]
                        self.stats["expired"] += 1
                    if not holders:
                        del self._holders[key]
                        self._post(key, False)

    def close(self) -> None:
        """Release everything (server shutdown) and stop the expiry thread."""
        self._stop.set()
        with self._lock:
            for key in list(self._holders):
                del self._holders[key]
                self._post(key, False)

    def snapshot(self) -> dict:
        with self._lock:
            return {"connections": self.connections, "held": sorted(self._holders),
                    "stats": dict(self.stats), "seen": dict(self.seen),
                    "probe": dict(self.probe)}


class Clicker:
    """Tap-to-click: validate a shared secret, rate-limit, synthesise one left click.

    Deliberately single-purpose — no drag, no hover, no right click, no keyboard.
    """

    def __init__(self, secret: str, display_size: tuple[int, int] | None,
                 origin: tuple[int, int], dry_run: bool,
                 max_per_window: int = 10, window_s: float = 3.0,
                 min_gap_s: float = 0.08):
        self.secret = secret
        self.display_size = display_size      # Mac display points; None -> use frame size
        self.origin = origin                  # display origin in global screen coords
        self.dry_run = dry_run
        self.max_per_window = max_per_window
        self.window_s = window_s
        self.min_gap_s = min_gap_s
        self.stats = {"received": 0, "rejected": 0, "limited": 0,
                      "synthesized": 0, "bad_coords": 0, "failed": 0}
        self.last_click: dict | None = None
        self._lock = threading.Lock()
        self._by_ip: dict[str, list[float]] = {}
        self._last_time = 0.0

    def check_secret(self, candidate: str) -> bool:
        return hmac.compare_digest(str(candidate).encode(), self.secret.encode())

    def reject(self, ip: str) -> None:
        """Count a failed authentication attempt against the per-IP limiter."""
        with self._lock:
            self.stats["rejected"] += 1
            self._rate_ok(ip)

    def _rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        if now - self._last_time < self.min_gap_s:
            return False
        q = self._by_ip.setdefault(ip, [])
        q[:] = [t for t in q if now - t < self.window_s]
        if len(q) >= self.max_per_window:
            return False
        q.append(now)
        self._last_time = now
        return True

    def synthesizer(self, px: int, py: int) -> tuple[str, list[str]]:
        cliclick = shutil.which("cliclick")
        if cliclick:
            # cliclick reads a leading '-' as relative; '=' forces absolute, which
            # matters for displays left of or above the main one
            fmt = lambda v: f"={v}" if v < 0 else str(v)
            return "cliclick", [cliclick, f"c:{fmt(px)},{fmt(py)}"]
        if _coregraphics() is not None:
            return "quartz", ["CGEventPost", f"leftclick:{px},{py}"]
        script = f'tell application "System Events" to click at {{{px}, {py}}}'
        return "osascript", ["osascript", "-e", script]

    def click(self, ip: str, x: float, y: float,
              frame_size: tuple[int, int] | None) -> tuple[int, dict]:
        with self._lock:
            self.stats["received"] += 1
            if not self._rate_ok(ip):
                self.stats["limited"] += 1
                return 429, {"error": "rate limited"}

        fw, fh = frame_size or (0, 0)
        if not fw or not fh or not (0 <= x <= fw and 0 <= y <= fh):
            with self._lock:
                self.stats["bad_coords"] += 1
            return 400, {"error": "coordinates outside the frame"}

        # frame pixels -> normalised -> display points (scale-invariant, so the
        # ffmpeg downscale and the page letterbox both cancel out)
        nx, ny = x / fw, y / fh
        dw, dh = self.display_size or (fw, fh)
        ox, oy = self.origin
        px = ox + min(max(0, int(round(nx * dw))), dw - 1)
        py = oy + min(max(0, int(round(ny * dh))), dh - 1)

        tool, argv = self.synthesizer(px, py)
        if self.dry_run:
            result = "dry-run"
        elif accessibility_trusted() is False:
            # macOS would swallow the click and we would report success
            with self._lock:
                self.stats["failed"] += 1
            return 503, {"error": "Accessibility permission missing", "fix": ACCESSIBILITY_HELP}
        else:
            try:
                if tool == "quartz":
                    quartz_click(px, py)
                else:
                    subprocess.run(argv, check=True, timeout=3,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                result = "clicked"
            except (OSError, subprocess.SubprocessError) as exc:
                with self._lock:
                    self.stats["failed"] = self.stats.get("failed", 0) + 1
                return 502, {"error": f"{tool} failed", "detail": type(exc).__name__,
                             "command": " ".join(argv)}

        with self._lock:
            self.stats["synthesized"] += 1
            self.last_click = {"nx": round(nx, 4), "ny": round(ny, 4),
                               "px": px, "py": py, "tool": tool,
                               "result": result, "ts": round(time.time(), 2)}
        return 200, {"ok": True, "click": self.last_click, "command": " ".join(argv)}


class JpegStreamSplitter:
    """Incrementally split a concatenated JPEG stream at SOI/EOI markers."""

    def __init__(self, max_buffer: int = 8 * 1024 * 1024):
        self.max_buffer = max_buffer
        self.buf = bytearray()

    def feed(self, chunk: bytes):
        self.buf.extend(chunk)
        if len(self.buf) > self.max_buffer:
            del self.buf[:]
            return
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                if len(self.buf) > 1:
                    del self.buf[:-1]
                return
            if start:
                del self.buf[:start]
            end = self.buf.find(b"\xff\xd9", 2)
            if end < 0:
                return
            frame = bytes(self.buf[:end + 2])
            del self.buf[:end + 2]
            yield frame


def split_jpeg_stream(chunks, max_buffer: int = 8 * 1024 * 1024):
    """Yield complete JPEGs from arbitrary chunks of a concatenated stream."""
    splitter = JpegStreamSplitter(max_buffer)
    for chunk in chunks:
        yield from splitter.feed(chunk)


class Hub:
    """One capture subprocess feeding N websocket clients and the MJPEG endpoint."""

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self.latest: bytes | None = None
        self.frame_size: tuple[int, int] | None = None
        self.lock = threading.Lock()
        self.clients: set[Client] = set()
        self.frames = 0
        self.proc: subprocess.Popen | None = None
        self.stderr_tail: list[str] = []
        self.started = time.time()
        self.frame_id = 0
        self.no_frames_warn_s = 8.0

    def start(self) -> None:
        # stdin must not be the terminal: ffmpeg treats keys as commands, and once the
        # terminal is gone it can spin at full CPU instead of exiting, holding the capture.
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True)
        threading.Thread(target=self._read_frames, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._warn_if_no_frames, daemon=True).start()

    def _warn_if_no_frames(self) -> None:
        time.sleep(self.no_frames_warn_s)
        if not self.frames and self.proc and self.proc.poll() is None:
            print(f"[src ] WARNING: no frames after {self.no_frames_warn_s:g} s, so the viewer "
                  "will stay black. A stuck capture from an earlier run is the usual cause: "
                  "stop this server, run pkill -f avfoundation, then start it again.", flush=True)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            del self.stderr_tail[:-20]

    def _read_frames(self) -> None:
        """Split a concatenated JPEG stream at SOI/EOI markers.

        Bytes before the first SOI are discarded.  EOI is used so each frame is
        published immediately; an 8 MiB cap prevents a broken source growing
        memory without bound.
        """
        assert self.proc and self.proc.stdout
        splitter = JpegStreamSplitter()
        while True:
            # read1 returns whatever is available: read(n) would block until the
            # full 64 KB fills, which throttles a 20 fps stream to a few fps
            # and makes the newest-frame-only client queue drop frames.
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                break
            for frame in splitter.feed(chunk):
                self._publish(frame)
        if self.proc and self.proc.returncode is None:
            self.proc.poll()
        self.stderr_tail.append(f"capture process exited ({self.proc.returncode})")

    def _publish(self, frame: bytes) -> None:
        if len(frame) < 64:  # truncated tail: not a frame
            return
        with self.lock:
            self.latest = frame
            self.frame_size = jpeg_size(frame) or self.frame_size
            self.frames += 1
            self.frame_id += 1
            clients = list(self.clients)
        for client in clients:
            client.offer(frame)

    def add_client(self) -> "Client":
        client = Client()
        with self.lock:
            self.clients.add(client)
        return client

    def remove_client(self, client: "Client") -> None:
        with self.lock:
            self.clients.discard(client)
        client.close()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                # a second Ctrl-C while waiting must not leave the capture running
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    self.proc.kill()


class Client:
    """A newest-frame queue and an fd which wakes its consumer on publish."""

    def __init__(self):
        self.queue: queue.Queue = queue.Queue(maxsize=1)
        self.wakeup_fd, self.wakeup_write = socket.socketpair()
        self.wakeup_fd.setblocking(False)
        self.wakeup_write.setblocking(False)

    def offer(self, frame: bytes) -> None:
        """Replace any unsent frame with this newer one, then wake the consumer.

        The wakeup is sent even when nobody drains the queue (pull mode reads
        hub.latest instead), so a full queue never silences publish signals.
        """
        try:
            self.queue.get_nowait()  # stale: the newest frame matters, not every frame
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            pass  # the consumer refilled it between the two calls; still fresh
        try:
            self.wakeup_write.send(b"\0")
        except (BlockingIOError, OSError):
            pass  # already signalled

    def drain_wakeup(self) -> None:
        while True:
            try:
                if not self.wakeup_fd.recv(4096):
                    return
            except BlockingIOError:
                return

    def close(self) -> None:
        self.wakeup_fd.close()
        self.wakeup_write.close()


def ws_ping(sock: socket.socket) -> None:
    ws_send_frame(sock, b"", opcode=0x9)


def ws_upgrade(handler) -> bool:
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        handler.send_error(400, "missing Sec-WebSocket-Key")
        return False
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.wfile.flush()
    return True


def ws_send_frame(sock: socket.socket, payload: bytes, opcode: int = 0x2) -> None:
    """Send one complete frame, waiting for a non-blocking socket as needed."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < (1 << 16):
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    header = bytes(header)
    parts = [memoryview(header), memoryview(payload)]
    deadline = time.monotonic() + 10.0

    def wait_writable() -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("websocket send timed out")
        while True:
            try:
                _, writable, _ = select.select([], [sock], [], remaining)
                break
            except InterruptedError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("websocket send timed out")
        if not writable:
            raise TimeoutError("websocket send timed out")

    use_sendmsg = hasattr(sock, "sendmsg")
    while parts:
        try:
            if use_sendmsg:
                sent = sock.sendmsg(parts)
            else:
                sent = sock.send(parts[0])
        except (AttributeError, NotImplementedError):
            use_sendmsg = False
            continue
        except (BlockingIOError, InterruptedError):
            wait_writable()
            continue
        if sent <= 0:
            raise ConnectionError("websocket send returned zero")
        while parts and sent >= len(parts[0]):
            sent -= len(parts[0])
            parts.pop(0)
        if parts and sent:
            parts[0] = parts[0][sent:]


class WSClientParser:
    """Incremental parser for the small masked client frames we accept."""

    def __init__(self, max_message: int = 4096):
        self.buf = bytearray()
        self.max_message = max_message
        self.fragment = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        self.buf.extend(data)
        events = []
        while True:
            if len(self.buf) < 2:
                break
            first, second = self.buf[0], self.buf[1]
            masked = bool(second & 0x80)
            length = second & 0x7f
            pos = 2
            if length == 126:
                if len(self.buf) < 4: break
                length = struct.unpack(">H", self.buf[2:4])[0]; pos = 4
            elif length == 127:
                if len(self.buf) < 10: break
                length = struct.unpack(">Q", self.buf[2:10])[0]; pos = 10
            if length > self.max_message or not masked:
                raise ValueError("invalid or oversized websocket client frame")
            if len(self.buf) < pos + 4 + length:
                break
            mask = self.buf[pos:pos + 4]; pos += 4
            payload = bytes(self.buf[pos + i] ^ mask[i % 4] for i in range(length))
            del self.buf[:pos + length]
            opcode = first & 0x0f
            final = bool(first & 0x80)
            if opcode == 0x8:
                events.append(("close", payload)); continue
            if opcode == 0x9:
                events.append(("ping", payload)); continue
            if opcode == 0x0:
                if not self.fragment: raise ValueError("unexpected continuation")
                self.fragment.extend(payload)
                if final:
                    events.append(("text", bytes(self.fragment))); self.fragment.clear()
            elif opcode == 0x1:
                if self.fragment: raise ValueError("nested websocket fragment")
                if final: events.append(("text", payload))
                else: self.fragment.extend(payload)
            else:
                raise ValueError("unsupported websocket opcode")
            if len(self.fragment) > self.max_message:
                raise ValueError("oversized websocket message")
        return events


def make_handler(hub: Hub, clicker: "Clicker | None" = None,
                 allowed_hosts: set[str] | None = None,
                 click_allow: set[str] | None = None,
                 injector: "KeyInjector | None" = None, fill: str = "fit"):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep stdout clean
            pass

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "").rsplit("@", 1)[-1]
            if host.startswith("["):
                host = host[1:].split("]", 1)[0]
            else:
                host = host.split(":", 1)[0]
            if host.lower() in (allowed_hosts or set()):
                return True
            self.send_error(403, "host not allowed")
            return False

        def _click_client_ok(self) -> bool:
            return not click_allow or self.client_address[0] in click_allow

        def do_POST(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if path != "/click":
                self.send_error(404)
                return
            if clicker is None:
                self._json(403, {"error": "tap-to-click is disabled "
                                          "(start with --click-secret)"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 4096:
                self._json(413, {"error": "bad body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "bad json"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "json object required"})
                return
            if not self._click_client_ok():
                self._json(403, {"error": "clicks not allowed from this client"})
                return
            if not clicker.check_secret(payload.get("secret", "")):
                clicker.reject(self.client_address[0])
                self._json(403, {"error": "bad secret"})
                return
            try:
                x = float(payload["x"])
                y = float(payload["y"])
            except (KeyError, TypeError, ValueError):
                self._json(400, {"error": "x and y are required numbers"})
                return
            code, body = clicker.click(self.client_address[0], x, y, hub.frame_size)
            if code == 200 and injector:
                injector.after_click()
            self._json(code, body)

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if path == "/":
                allowed = clicker is not None and self._click_client_ok()
                page = render_viewer(allowed, clicker.secret if clicker else "",
                                     allowed and injector is not None,
                                     allowed and injector is not None and injector.text_enabled,
                                     fill)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
            elif path == "/health":
                body = json.dumps({
                    "frames": hub.frames,
                    "clients": len(hub.clients),
                    "uptime_s": round(time.time() - hub.started, 1),
                    "source_alive": bool(hub.proc and hub.proc.poll() is None),
                    "source_exit_code": hub.proc.returncode if hub.proc else None,
                    "source_stderr": hub.stderr_tail[-3:],
                    "frame_size": hub.frame_size,
                    "clicks": clicker.stats if clicker else None,
                    "clicks_enabled": clicker is not None,
                    "last_click": clicker.last_click if clicker else None,
                    "accessibility": accessibility_trusted() if clicker else None,
                    "input": injector.snapshot() if injector else None,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/mjpeg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                client = hub.add_client()
                try:
                    while True:
                        try:
                            frame = client.queue.get(timeout=3.0)
                        except queue.Empty:
                            with hub.lock:
                                frame = hub.latest
                            if frame is None:
                                # nothing to write means a gone viewer goes unnoticed
                                readable, _, _ = select.select([self.connection], [], [], 0)
                                if readable and not self.connection.recv(1, socket.MSG_PEEK):
                                    break
                                continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
                        self.wfile.write(frame + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    hub.remove_client(client)
            elif path == "/ws":
                if not ws_upgrade(self):
                    return
                client = hub.add_client()
                parser = WSClientParser()
                pull = False
                requested = False
                last_sent = -1
                last_ping = time.monotonic()
                self.connection.setblocking(False)
                try:
                    while True:
                        readable, _, _ = select.select(
                            [self.connection, client.wakeup_fd], [], [], 10.0)
                        timed_out = not readable
                        if client.wakeup_fd in readable:
                            client.drain_wakeup()
                        if self.connection in readable:
                            try:
                                data = self.connection.recv(4096)
                            except BlockingIOError:
                                data = None
                            if data == b"":
                                break
                            if data is not None:
                                for kind, payload in parser.feed(data):
                                    if kind == "close":
                                        ws_send_frame(self.connection, payload[:125], 0x8)
                                        return
                                    if kind == "ping":
                                        ws_send_frame(self.connection, payload[:125], 0xA)
                                    if kind == "text":
                                        text = payload.decode("ascii", "ignore")
                                        if text == "pull":
                                            pull = True
                                            # Do not wait for a frame that the
                                            # viewer cannot render yet.
                                            requested = True
                                        elif pull and text == "n":
                                            requested = True
                        frame = None
                        if pull:
                            if requested:
                                with hub.lock:
                                    if hub.latest is not None and hub.frame_id != last_sent:
                                        frame = hub.latest
                                        last_sent = hub.frame_id
                                        requested = False
                        else:
                            try:
                                frame = client.queue.get_nowait()
                            except queue.Empty:
                                pass
                        if frame is not None:
                            ws_send_frame(self.connection, frame)
                        elif not pull and timed_out and time.monotonic() - last_ping >= 10:
                            ws_ping(self.connection)
                            last_ping = time.monotonic()
                except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                    pass
                finally:
                    hub.remove_client(client)
                self.close_connection = True
            elif path == "/input":
                self._input()
            else:
                self.send_error(404)

        def _input(self) -> None:
            """Input socket: 'auth:<secret>', then 'd:<name>', 'u:<name>', 'm:<dx>,<dy>', 't:<text>', 'p:<probe>'."""
            if injector is None or clicker is None:
                self.send_error(403, "input is disabled (start with --keys)")
                return
            if not self._click_client_ok():
                self.send_error(403, "input not allowed from this client")
                return
            if not ws_upgrade(self):
                return
            self.close_connection = True
            ip = self.client_address[0]
            conn = object()
            parser = WSClientParser()
            authed = False
            budget, refill = 60.0, time.monotonic()  # ~60 events/s per socket
            send_lock = injector.add_socket(self.connection)

            def send(payload: bytes, opcode: int = 0x1) -> None:
                with send_lock:  # a keyboard prompt may arrive from a timer thread
                    ws_send_frame(self.connection, payload, opcode)
            with injector._lock:
                injector.connections += 1
            try:
                while True:
                    data = self.connection.recv(4096)
                    if not data:
                        return
                    for kind, payload in parser.feed(data):
                        if kind == "close":
                            send(payload[:125], 0x8)
                            return
                        if kind == "ping":
                            send(payload[:125], 0xA)
                            continue
                        text = payload.decode("utf-8", "ignore")
                        if not authed:
                            if text.startswith("auth:") and clicker.check_secret(text[5:]):
                                authed = True
                                send(b"ok")
                                continue
                            clicker.reject(ip)
                            send(b"err:bad secret")
                            return
                        now = time.monotonic()
                        budget = min(60.0, budget + (now - refill) * 60.0)
                        refill = now
                        if budget < 1:
                            continue  # over budget: drop; held keys still expire
                        budget -= 1
                        if text.startswith("p:"):
                            injector.note_probe(text[2:])
                        elif text.startswith("m:"):
                            dx, _, dy = text[2:].partition(",")
                            try:
                                err = injector.move(int(dx), int(dy))
                            except ValueError:
                                continue
                            if err:
                                send(b"err:" + err.encode())
                            elif injector.pointer_message():
                                send(injector.pointer_message())
                        elif text.startswith("t:"):
                            err = injector.type_text(text[2:])
                            if err:
                                send(b"err:" + err.encode())
                        elif text[:2] in ("d:", "u:"):
                            err = injector.press(conn, text[2:], text[0] == "d")
                            if err:
                                send(b"err:" + err.encode())
            except (OSError, ValueError):
                pass
            finally:
                injector.remove_socket(self.connection)
                injector.release_all(conn)
                with injector._lock:
                    injector.connections -= 1

    return Handler


def dns_answer(data: bytes, answer: str, wanted: set[str]) -> bytes | None:
    """Build a minimal DNS response, or None for malformed/non-query input."""
    try:
        tid, flags, qd = struct.unpack(">HHH", data[:6])
        if flags & 0x8000 or qd != 1:
            return None
        pos, labels = 12, []
        while pos < len(data) and data[pos]:
            ln = data[pos]
            if ln > 63 or pos + 1 + ln > len(data):
                return None
            labels.append(data[pos + 1:pos + 1 + ln].decode("ascii", "replace"))
            pos += 1 + ln
        pos += 1
        if pos + 4 > len(data):
            return None
        qtype, qclass = struct.unpack(">HH", data[pos:pos + 4])
        question = data[12:pos + 4]
        name = ".".join(labels).lower()
        if qtype != 1:  # NOERROR/NODATA for non-A queries
            return struct.pack(">HHHHHH", tid, 0x8180, 1, 0, 0, 0) + question
        if wanted and name not in wanted:
            return struct.pack(">HHHHHH", tid, 0x8183, 1, 0, 0, 0) + question
        rr = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 30, 4) + socket.inet_aton(answer)
        return struct.pack(">HHHHHH", tid, 0x8180, 1, 1, 0, 0) + question + rr
    except (IndexError, struct.error, OSError, UnicodeError):
        return None


def dns_responder(sock: socket.socket, answer: str, names: list[str] | None) -> None:
    """Answer A queries with `answer` so the Switch's portal check lands here.

    Only the plain DNS subset needed for an A lookup is implemented.
    """
    wanted = {n.lower() for n in (names or [])}
    print(f"[dns] answering A queries -> {answer}", flush=True)
    while True:
        data, addr = sock.recvfrom(1500)
        resp = dns_answer(data, answer, wanted)
        if resp:
            sock.sendto(resp, addr)


def _qv(quality: int) -> int:
    """Map a friendly 1..100 quality to ffmpeg's -q:v 1..31 (inverted)."""
    return max(1, min(31, 31 - int(quality * 0.31)))


def _scale_filter(width: int, height: int, static: bool, sharp: bool,
                  stretch: bool = False) -> str:
    """Scale to fit. sharp costs some CPU and data but keeps small text readable.

    stretch fills width x height exactly, so the viewer needn't enlarge the frame again.
    """
    flags, extra = ("lanczos", ",unsharp=3:3:0.5") if sharp else ("fast_bilinear", "")
    shape = "" if stretch else "force_original_aspect_ratio=decrease:force_divisible_by=2:"
    return (f"scale={width}:{height}:{shape}flags={flags}{extra}" +
            (",mpdecimate,setpts=N/FRAME_RATE/TB" if static else ""))


def default_capture_cmd(width: int, height: int, fps: int, quality: int,
                        static: bool = False) -> list[str]:
    vf = "mpdecimate,setpts=N/FRAME_RATE/TB" if static else None
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re", "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}",
        "-pix_fmt", "yuvj420p", "-huffman", "optimal",
        *( ["-vf", vf] if vf else [] ),
        "-c:v", "mjpeg", "-q:v", str(_qv(quality)), "-f", "mjpeg", "-",
    ]


def mac_capture_cmd(index: int, width: int, height: int, fps: int, quality: int,
                    static: bool = False, sharp: bool = False, stretch: bool = False) -> list[str]:
    """macOS: AVFoundation screen device -> MJPEG on stdout.

    `index` is an AVFoundation video device: a name such as "Capture screen 0"
    (what --screen produces; stable) or the numeric index printed by
    `ffmpeg -f avfoundation -list_devices true -i ""` (shifts whenever a camera,
    iPhone Continuity Camera or Desk View appears ahead of the screens).
    Requires Screen Recording permission for the calling terminal, else frames
    come out black with no error.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-framerate", str(fps), "-pixel_format", "nv12",
        "-capture_cursor", "1", "-i", f"{index}:none",
        "-vf", _scale_filter(width, height, static, sharp, stretch),
        # passthrough: the default CFR mode duplicates AVFoundation frames to
        # hundreds of fps (measured ~680 fps for -framerate 15), flooding Wi-Fi
        "-fps_mode", "passthrough",
        "-pix_fmt", "yuvj420p", "-huffman", "optimal",
        "-c:v", "mjpeg", "-q:v", str(_qv(quality)), "-f", "mjpeg", "-",
    ]


def x11_capture_cmd(display: str, width: int, height: int, fps: int, quality: int,
                    static: bool = False, sharp: bool = False, stretch: bool = False) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "x11grab", "-framerate", str(fps), "-i", display,
        "-vf", _scale_filter(width, height, static, sharp, stretch),
        "-pix_fmt", "yuvj420p", "-huffman", "optimal",
        "-c:v", "mjpeg", "-q:v", str(_qv(quality)), "-f", "mjpeg", "-",
    ]


PRESETS = {
    # name: (default width, height, fps)
    "test": (960, 540, 20),
    "mac-mirror": (960, 540, 15),
    "mac-extended": (1280, 720, 15),
    "x11": (960, 540, 20),
}
# text is sized to the Switch viewer's 1280x648 (the footer takes the rest), so the
# browser draws frames close to 1:1 instead of blurring them with a second scale.
PROFILES = {"sharp": (1280, 720), "balanced": (960, 540), "low": (800, 450),
            "text": (1280, 648)}


def resolve_capture(args) -> list[str]:
    """Turn CLI flags into the capture argv (or a shell command)."""
    if args.source:
        values = {"{w}": str(args.width), "{h}": str(args.height), "{fps}": str(args.fps),
                  "{q}": str(args.quality), "{qv}": str(_qv(args.quality))}
        source = args.source
        for key, value in values.items():
            source = source.replace(key, value)
        return ["/bin/sh", "-c", source]
    preset = args.preset or "test"
    quality = args.quality
    if preset == "test":
        return default_capture_cmd(args.width, args.height, args.fps, quality, getattr(args, "static", False))
    if preset in ("mac-mirror", "mac-extended"):
        return mac_capture_cmd(_mac_device(args), args.width, args.height, args.fps, quality,
                               getattr(args, "static", False), getattr(args, "profile", None) == "text",
                               getattr(args, "fill", "fit") == "stretch")
    if preset == "x11":
        return x11_capture_cmd(args.display, args.width, args.height, args.fps, quality,
                               getattr(args, "static", False), getattr(args, "profile", None) == "text",
                               getattr(args, "fill", "fit") == "stretch")
    raise SystemExit(f"unknown preset: {preset}")


def _mac_screen(args) -> int | None:
    """The "Capture screen N" a mac preset captures, or None for a raw --display-index."""
    if getattr(args, "display_index", None) is not None:
        return None
    screen = getattr(args, "screen", None)
    if screen is None:
        screen = 1 if args.preset == "mac-extended" else 0
    return screen


def _mac_device(args) -> str | int:
    screen = _mac_screen(args)
    return args.display_index if screen is None else f"Capture screen {screen}"


def list_displays() -> int:
    displays = mac_displays()
    if not displays:
        print("no CoreGraphics displays (macOS only)")
        return 1
    print("screen  size (points)  origin (points)")
    for d in displays:
        size = "%dx%d" % d["size"]
        origin = "%d,%d" % d["origin"]
        print(f"  {d['screen']:<5} {size:<14} {origin}")
    print("\nPick one with --screen N (mac-mirror defaults to 0, mac-extended to 1);"
          "\ntap-to-click size/origin are taken from this table automatically.")
    return 0


def _parse_size(text: str) -> tuple[int, int]:
    if not text:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT")
    w, _, h = text.lower().partition("x")
    try:
        value = (int(w), int(h))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT") from exc
    if min(value) <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return value


def _parse_xy(text: str) -> tuple[int, int]:
    x, _, y = text.partition(",")
    try:
        return (int(x), int(y))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected X,Y") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description="Second display for the Nintendo Switch's browser.")
    ap.add_argument("--port", type=int, default=8090, help="HTTP port (default 8090)")
    ap.add_argument("--bind", default="0.0.0.0", help="HTTP bind address")
    ap.add_argument("--preset", choices=sorted(PRESETS), default=None,
                     help="capture preset: test | mac-mirror | mac-extended | x11 (default test)")
    ap.add_argument("--profile", choices=sorted(PROFILES), default=None,
                     help="size profile: sharp 1280x720, balanced 960x540, low 800x450, or text: "
                          "sized for the Switch viewer with better scaling and quality 85, "
                          "for readable words at some cost in frame rate")
    ap.add_argument("--source", default=None,
                    help="explicit capture command writing JPEG to stdout; placeholders "
                         "{w} {h} {fps} {q} (1..100) {qv} (ffmpeg -q:v 1..31)")
    ap.add_argument("--screen", type=int, default=None,
                    help="mac presets: capture \"Capture screen N\" (default 0 for "
                         "mac-mirror, 1 for mac-extended); see --list-displays")
    ap.add_argument("--display-index", type=int, default=None,
                    help="mac presets: raw AVFoundation device index instead of --screen "
                         "(cameras shift it; prefer --screen)")
    ap.add_argument("--list-displays", action="store_true",
                    help="macOS: list screens with their size/origin in points and exit")
    ap.add_argument("--display", default=":0.0", help="X11 display for the x11 preset")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--quality", type=int, default=None,
                    help="1..100 (default 65, or 85 with --profile text)")
    ap.add_argument("--fill", choices=("fit", "crop", "stretch"), default="fit",
                    help="how the picture fills the Switch screen: fit keeps it all with black "
                         "bars, crop cuts the overflow, stretch changes its shape (default fit; "
                         "the viewer's fill button switches live)")
    ap.add_argument("--static", action="store_true",
                    help="drop unchanged frames with mpdecimate (preset capture only)")
    ap.add_argument("--dns-port", type=int, default=0,
                    help="also answer DNS A queries on this port (53 needs root)")
    ap.add_argument("--dns-answer", default=None, help="IP to answer with (default: LAN IP)")
    ap.add_argument("--dns-bind", default=None,
                    help="address to bind the DNS responder to (default: --dns-answer); "
                         "binding the LAN IP avoids mDNSResponder on macOS")
    ap.add_argument("--print-source", action="store_true",
                    help="print the resolved capture command and exit (no server)")
    ap.add_argument("--click-secret", default=None,
                    help="enable tap-to-click with this shared secret "
                         "(or set SWITCHDISPLAY_SECRET); without it /click is refused")
    ap.add_argument("--click-display-size", type=_parse_size, default=None, metavar="WxH",
                    help="target display size in POINTS, e.g. 1512x982 (Retina!) — "
                         "omit to map frame pixels 1:1")
    ap.add_argument("--click-display-origin", type=_parse_xy, default=None, metavar="X,Y",
                    help="display origin in global screen coordinates (default 0,0; "
                         "negative needs the = form: --click-display-origin=-1920,0)")
    ap.add_argument("--click-dry-run", action="store_true",
                     help="log the synthesizer command instead of clicking (safe test)")
    ap.add_argument("--keys", action="store_true",
                    help="forward Joy-Con/touch-pad input as Mac key presses "
                         "(needs --click-secret; only keys in the keymap can be pressed)")
    ap.add_argument("--keymap", action="append", default=[], metavar="NAME=KEY,...",
                    help="remap inputs, e.g. 'btn-a=e,up=up'; KEY 'none' unmaps "
                         "(default: left stick/D-pad -> WASD, A space, B e, X f, Y r, "
                         "L q, R tab, ZL shift, ZR and right-stick press mouse-left, "
                         "+ escape, - m; the right stick moves the pointer)")
    ap.add_argument("--text", action="store_true",
                    help="with --keys: type Switch keyboard text on the Mac, opening the "
                         "keyboard when a click focuses a text field. Allows any text, "
                         "beyond the keymap; it crosses the LAN over plain HTTP")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="additional HTTP Host name allowed (repeatable)")
    ap.add_argument("--click-allow", action="append", default=[], metavar="IP",
                    help="only this client IP may click or receive the secret (repeatable)")
    args = ap.parse_args()
    if args.list_displays:
        return list_displays()

    pw, ph, pf = PRESETS[args.preset or "test"]
    if args.profile:
        pw, ph = PROFILES[args.profile]
    if args.profile == "text" and args.fill == "crop":
        ph = 830  # crop draws the full width, so frames need the width, not the height
    if args.quality is None:
        args.quality = 85 if args.profile == "text" else 65
    if args.width is None:
        args.width = pw
    if args.height is None:
        args.height = ph
    if args.fps is None:
        args.fps = pf

    cmd = resolve_capture(args)
    if args.print_source:
        print(shlex.join(cmd))
        return 0

    from http.server import ThreadingHTTPServer
    import socketserver

    class LocalThreadingHTTPServer(ThreadingHTTPServer):
        # HTTPServer.server_bind calls getfqdn(), which can block on an offline
        # host; the server never needs a reverse-DNS name.
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
    secret = args.click_secret or os.environ.get("SWITCHDISPLAY_SECRET") or ""
    clicker = None
    if secret:
        size, origin = args.click_display_size, args.click_display_origin
        screen = _mac_screen(args) if args.preset in ("mac-mirror", "mac-extended") else None
        if screen is not None and size is None:
            match = [d for d in mac_displays() if d["screen"] == screen]
            if match:
                size = match[0]["size"]
                origin = match[0]["origin"] if origin is None else origin
                print(f"[click] screen {screen}: {size[0]}x{size[1]} points at "
                      f"{origin[0]},{origin[1]} (auto-detected)", flush=True)
            else:
                print(f"[click] warning: screen {screen} not found; see --list-displays",
                      flush=True)
        clicker = Clicker(
            secret=secret,
            display_size=size,
            origin=origin or (0, 0),
            dry_run=args.click_dry_run,
        )
    injector = None
    if args.text and not args.keys:
        raise SystemExit("--text needs --keys")
    if args.keys:
        if not clicker:
            raise SystemExit("--keys needs --click-secret (or SWITCHDISPLAY_SECRET)")
        injector = KeyInjector(parse_keymap(args.keymap), dry_run=args.click_dry_run,
                               bounds=(*clicker.origin, *clicker.display_size)
                               if clicker.display_size else None,
                               text_enabled=args.text)
    lan_ip = _lan_ip()
    if clicker and args.bind == "0.0.0.0":
        args.bind = lan_ip
        print(f"[http] click enabled; binding to LAN IP {lan_ip} instead of 0.0.0.0", flush=True)
    allowed_hosts = _allowed_hosts(args.bind, lan_ip, args.allow_host)
    dns_sock = None
    if args.dns_port:
        answer = args.dns_answer or lan_ip
        dns_bind = args.dns_bind or answer
        dns_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dns_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        dns_sock.bind((dns_bind, args.dns_port))
    hub = Hub(cmd)
    server = LocalThreadingHTTPServer((args.bind, args.port),
                                 make_handler(hub, clicker, allowed_hosts, set(args.click_allow),
                                              injector, args.fill))
    server.daemon_threads = True
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", "0"))
        try:
            os.setgroups([])
        except OSError:
            pass
        os.setgid(gid)
        os.setuid(uid)
        print(f"[priv] dropped privileges to uid={uid} gid={gid}", flush=True)
    hub.start()
    if dns_sock:
        threading.Thread(target=dns_responder,
                         args=(dns_sock, answer,
                               ["conntest.nintendowifi.net", "ctest.cdn.nintendo.net"]),
                         daemon=True).start()
    shown = args.bind
    print(f"[http] http://{shown}:{args.port}/  (viewer, /ws, /mjpeg, /click, /health)", flush=True)
    print(f"[src ] {shlex.join(cmd)}", flush=True)
    if clicker:
        size = clicker.display_size or "frame size"
        if clicker.display_size is None:
            print("[click] warning: --click-display-size omitted; mapping to frame size", flush=True)
        print(f"[click] tap-to-click ON{' (DRY RUN)' if clicker.dry_run else ''} "
              f"secret=set display={size} origin={clicker.origin}", flush=True)
        if not clicker.dry_run and accessibility_trusted(prompt=True) is False:
            print(f"[click] warning: taps will be refused until you {ACCESSIBILITY_HELP}. "
                  "macOS has been asked to show its permission prompt.", flush=True)
    else:
        print("[click] tap-to-click OFF (no --click-secret / SWITCHDISPLAY_SECRET)", flush=True)
    if injector:
        shown = ("up", "left", "down", "right", "btn-a", "btn-b", "btn-x", "btn-y",
                 "btn-l", "btn-r", "btn-zl", "btn-zr", "btn-rstick", "btn-plus", "btn-minus",
                 "enter")
        print(f"[keys] Joy-Con/touch input ON{' (DRY RUN)' if args.click_dry_run else ''}: " +
              " ".join(f"{n}={injector.keymap[n]}" for n in shown if n in injector.keymap
                       and not (args.text and n == "btn-minus")),  # the viewer keeps - for the keyboard
              flush=True)
        if args.text:
            print("[text] Switch keyboard ON: - or kbd opens it; any text can be typed on this "
                  "Mac, sent over plain HTTP", flush=True)
    def _terminate(signum, frame):
        raise KeyboardInterrupt  # run the finally below: ffmpeg is in its own session
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if injector:
            injector.close()
        hub.stop()
    return 0


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _allowed_hosts(bind: str, lan_ip: str, allow_hosts: list[str]) -> set[str]:
    """Build Host values, replacing wildcard binds with the effective LAN IP."""
    hosts = {"localhost", "127.0.0.1", "conntest.nintendowifi.net",
             "ctest.cdn.nintendo.net", lan_ip.lower()}
    if bind not in ("0.0.0.0", "::"):
        hosts.add(bind.lower())
    hosts.update(h.lower().split(":", 1)[0] for h in allow_hosts)
    return hosts


if __name__ == "__main__":
    sys.exit(main())
