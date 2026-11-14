// Runs the viewer's script against a stub browser with a fake clock, to test
// behaviour that a syntax check cannot. Usage: node viewer_test.js <case> <script.js>
// (tests/test_switchdisplay.py extracts the script and calls this).
'use strict';
var fs = require('fs'), vm = require('vm'), assert = require('assert');

function browser(search) {
  var now = 0, timers = [], env = {sockets: [], created: [], pads: []};
  function element(tag) {
    return {
      tag: tag, style: {}, className: '', value: '',
      addEventListener: function () {}, appendChild: function (c) { c.parentNode = this; },
      removeChild: function () {}, getContext: function () { return {drawImage: function () {}}; },
      getAttribute: function () { return null; }, getElementsByTagName: function () { return []; },
      getBoundingClientRect: function () { return {left: 0, top: 0, width: 100, height: 100}; },
      focus: function () {}, blur: function () {}
    };
  }
  var byId = {};
  function WebSocket(url) { this.url = url; this.sent = []; this.readyState = 1; env.sockets.push(this); }
  WebSocket.prototype.send = function (message) { this.sent.push(message); };
  function timer(fn, ms, repeat) { timers.push({fn: fn, ms: ms, at: now + ms, repeat: repeat}); return timers.length; }
  function FakeDate() {}
  FakeDate.prototype.getTime = function () { return now; };
  var window = {
    innerWidth: 1280, innerHeight: 720, WebSocket: WebSocket,
    addEventListener: function () {},
    setInterval: function (fn, ms) { return timer(fn, ms, true); },
    setTimeout: function (fn, ms) { return timer(fn, ms, false); },
    clearTimeout: function (id) { if (id) { timers[id - 1].dead = true; } },
    URL: {createObjectURL: function () { return 'blob:'; }, revokeObjectURL: function () {}}
  };
  env.context = {
    window: window, WebSocket: WebSocket, Date: FakeDate,
    document: {
      body: element('body'), documentElement: element('html'), addEventListener: function () {},
      getElementById: function (id) { return byId[id] || (byId[id] = element(id)); },
      createElement: function (tag) { var e = element(tag); env.created.push(e); return e; }
    },
    navigator: {getGamepads: function () { return env.pads; }},
    location: {protocol: 'http:', host: 'mac', search: search},
    XMLHttpRequest: function () {}, Blob: function () {}, Image: function () {}
  };
  env.advance = function (ms) {  // run due timers in time order, then stop the clock at now + ms
    var end = now + ms, next, i;
    for (;;) {
      next = null;
      for (i = 0; i < timers.length; i++) {
        if (!timers[i].dead && timers[i].at <= end && (!next || timers[i].at < next.at)) { next = timers[i]; }
      }
      if (!next) { break; }
      now = next.at;
      if (next.repeat) { next.at += next.ms; } else { next.dead = true; }
      next.fn();
    }
    now = end;
  };
  env.now = function () { return now; };
  return env;
}

var cases = {
  // A healthy MJPEG stream must not be reconnected; a silent one must be.
  mjpeg: function (script) {
    var env = browser('?mode=mjpeg'), img, first, i;
    vm.runInNewContext(script, env.context);
    img = env.created.filter(function (e) { return e.tag === 'img'; })[0];
    first = img.src;
    assert.ok(/^\/mjpeg\?/.test(first), 'the fallback opens /mjpeg');
    for (i = 0; i < 12; i++) { env.advance(3000); img.onload(); }  // 36 s of frames
    assert.strictEqual(img.src, first, 'a stream that delivers frames is left alone');
    env.advance(9000);
    assert.strictEqual(img.src, first, 'not yet stalled after 9 s');
    env.advance(2000);
    assert.notStrictEqual(img.src, first, 'reconnects after 10 s without a frame');
  },
  // A slow right-stick tilt on one pad must still move the pointer while a
  // second pad rests (each step is under a pixel, so the fractions must add up).
  twopads: function (script) {
    var env = browser(''), input, moved = 0;
    vm.runInNewContext(script, env.context);
    input = env.sockets.filter(function (s) { return /\/input$/.test(s.url); })[0];
    input.onopen();
    input.onmessage({data: 'ok'});
    env.pads = [{buttons: [], axes: [0, 0, 0.16, 0]}, {buttons: [], axes: [0, 0, 0, 0]}];
    env.advance(1000);
    input.sent.forEach(function (m) { if (m.indexOf('m:') === 0) { moved += parseInt(m.substring(2), 10); } });
    assert.ok(moved >= 15 && moved <= 30, 'pointer moved ' + moved + ' px in 1 s, expected about 23');
  }
};

cases[process.argv[2]](fs.readFileSync(process.argv[3], 'utf8'));
