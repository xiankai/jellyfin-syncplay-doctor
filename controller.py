#!/usr/bin/env python3
"""
Jellyfin SyncPlay Doctor — controller.

An always-on local web server that:
  - serves the dashboard (as a Progressive Web App, cached for offline use)
  - shows Start / Stop buttons for the recorder
  - manages the recorder as a child process (start/stop)
  - exposes /state (latest recorded state) and /status (running/stopped)

The recorder runs headless (jellyfin_telemetry.py --headless): it only polls
Jellyfin and writes state.json; this controller is the single HTTP entry point.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "telemetry-py")
STATE_FILE = os.path.join(OUT_DIR, "state.json")
PORT_FILE = os.path.join(OUT_DIR, "port.txt")
RECORDER = os.path.join(ROOT, "jellyfin_telemetry.py")
ICON_PNG = os.path.join(ROOT, "assets", "icon.png")

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect x="16" y="16" width="480" height="480" rx="112" fill="#000b25" stroke="#2a3a5c" stroke-width="4"/>'
            '<path d="M172 150 L372 256 L172 362 Z" fill="#5ee08a"/>'
            '<rect x="222" y="212" width="44" height="88" rx="10" fill="#ffffff"/>'
            '<rect x="200" y="234" width="88" height="44" rx="10" fill="#ffffff"/></svg>')

APP_VERSION = "0.1.2"

recorder = None
stop_flag = threading.Event()
UPDATE_INFO = {"latest": None, "updateAvailable": False, "releaseUrl": ""}


def load_config():
    cfg = {"serverUrl": "", "apiKey": "", "githubRepo": "", "clientLogPath": ""}
    path = os.path.join(ROOT, "telemetry.config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg.update({k: (data.get(k) or "") for k in cfg})
        except Exception:
            pass
    return cfg


def save_config(cfg):
    path = os.path.join(ROOT, "telemetry.config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_github_repo():
    return (load_config().get("githubRepo") or "").strip()


def resolve_client_log(explicit=""):
    if explicit:
        return explicit
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "JellyfinMediaPlayer", "logs", "JellyfinMediaPlayer.log")
    if sys.platform == "darwin":
        candidates = [
            os.path.join(home, "Library", "Logs", "jellyfinmediaplayer", "JellyfinMediaPlayer.log"),
            os.path.join(home, "Library", "Application Support", "jellyfinmediaplayer", "logs", "JellyfinMediaPlayer.log"),
        ]
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        candidates = [
            os.path.join(xdg, "jellyfinmediaplayer", "logs", "JellyfinMediaPlayer.log"),
            os.path.join(home, ".var", "app", "org.jellyfin.JellyfinMediaPlayer", "data", "jellyfinmediaplayer", "logs", "JellyfinMediaPlayer.log"),
            os.path.join(home, "snap", "jellyfin-media-player", "common", ".local", "share", "jellyfinmediaplayer", "logs", "JellyfinMediaPlayer.log"),
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


def get_paths():
    cfg = load_config()
    return {
        "configFile": os.path.join(ROOT, "telemetry.config.json"),
        "clientLog": resolve_client_log(cfg.get("clientLogPath") or ""),
        "outputDir": OUT_DIR,
    }


def open_path(path):
    if not path or not os.path.exists(path):
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])
        return True
    except Exception:
        return False


def check_update():
    repo = load_github_repo()
    if not repo:
        return
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Jellyfin-SyncPlay-Doctor",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = str(data.get("tag_name") or "").lstrip("v")
        if latest:
            UPDATE_INFO["latest"] = latest
            UPDATE_INFO["updateAvailable"] = latest != APP_VERSION
            UPDATE_INFO["releaseUrl"] = data.get("html_url") or ""
    except Exception:
        pass


def update_loop():
    check_update()
    while True:
        time.sleep(6 * 3600)
        check_update()

MANIFEST = json.dumps({
    "name": "Jellyfin SyncPlay Doctor",
    "short_name": "SyncPlay Doctor",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0f1115",
    "theme_color": "#000b25",
    "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}],
})

SERVICE_WORKER = """\
const CACHE = 'syncplay-doctor-v3';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE)
    .then(c => c.addAll(['/manifest.json', '/icon.svg', '/icon.png']))
    .then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  // purge every older cache so a stale page can never be served again
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('message', (e) => { if (e.data === 'SKIP_WAITING') self.skipWaiting(); });
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const u = new URL(e.request.url);
  if (u.pathname === '/') {
    // app shell: network-first, never serve a stale cached page
    e.respondWith(fetch(e.request).then(res => {
      const cp = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, cp));
      return res;
    }).catch(() => caches.match(e.request).then(r => r || Response.error())));
    return;
  }
  if (u.pathname === '/manifest.json' || u.pathname === '/icon.svg' || u.pathname === '/icon.png') {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
    return;
  }
  // live data + controls always hit the network
  e.respondWith(fetch(e.request).catch(() =>
    new Response('{}', {headers: {'Content-Type': 'application/json'}})));
});
"""

DASHBOARD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.png">
<title>Jellyfin SyncPlay Doctor</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, "Segoe UI", sans-serif; margin: 0; background: #0f1115; color: #e6e6e6; }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 20px; margin: 0; }
  .ver { font-size: 11px; color: #8ab; background: #23262e; border-radius: 999px; padding: 2px 8px; font-weight: 600; vertical-align: 3px; margin-left: 8px; }
  .sub { color: #9aa; font-size: 13px; margin: 6px 0 18px; }
  .card { background: #1a1d24; border: 1px solid #2a2e3a; border-radius: 10px; padding: 14px 16px; margin-bottom: 16px; }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: #8ab; margin: 0 0 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #262a34; }
  th { color: #9aa; font-weight: 600; font-size: 12px; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .dp { background: #123a1f; color: #5ee08a; }
  .ds { background: #3a3412; color: #e0c15e; }
  .tr { background: #3a1215; color: #ff7b82; }
  .na { background: #23262e; color: #9aa; }
  .stuck { animation: blink 1s infinite; font-weight: 700; color: #ff5b62; }
  @keyframes blink { 50% { opacity: .35; } }
  .badge { background:#3a1215; color:#ff7b82; font-weight:700; padding:4px 10px; border-radius:8px; }
  .events { list-style: none; margin: 0; padding: 0; font-size: 13px; }
  .events li { padding: 5px 0; border-bottom: 1px solid #22262e; font-family: ui-monospace, Consolas, monospace; }
  .events .t { color: #6b7; margin-right: 8px; }
  .ts { color:#7ea; font-family: ui-monospace, Consolas, monospace; }
  .empty { color: #777; font-style: italic; }
  .legend { line-height: 2; font-size: 13px; }
  .alert { background: #3a1215; color: #ff9aa2; border: 1px solid #5a1a1f; padding: 10px 14px; border-radius: 8px; font-weight: 600; margin-bottom: 16px; }
  .stopped { background: #23262e; color: #e6e6e6; border: 1px solid #3a3f4a; padding: 10px 14px; border-radius: 8px; font-weight: 600; margin-bottom: 16px; }
  .topbar { display: flex; justify-content: space-between; align-items: center; }
  .btnrow { display: flex; align-items: center; gap: 8px; }
  .ctl { background: #23262e; color: #e6e6e6; border: 1px solid #2a2e3a; border-radius: 8px; padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer; }
  #startBtn:disabled, #stopBtn:disabled { background:#1a1d24; color:#5a5f6a; border-color:#262a34; cursor:default; }
  #stopBtn { background: #3a1215; color: #ff9aa2; border-color: #5a1a1f; }
  #startBtn { background: #123a1f; color: #5ee08a; border-color: #1e5a34; }
  .update { background:#2a2410; color:#e0c15e; border:1px solid #4a3d18; padding:10px 14px; border-radius:8px; font-weight:600; margin-bottom:16px; }
  .update a { color:#5ee08a; }
  .field { margin-bottom: 12px; }
  .field label { display: block; font-size: 12px; color: #9aa; margin-bottom: 4px; }
  .field input { width: 100%; box-sizing: border-box; background: #0f1115; border: 1px solid #2a2e3a; border-radius: 6px; color: #e6e6e6; padding: 8px 10px; font-size: 14px; }
  .field input:focus { outline: none; border-color: #5ee08a; }
  .keywrap { display: flex; align-items: center; gap: 6px; }
  .keywrap input { flex: 1; }
  .eye { background: #23262e; border: 1px solid #2a2e3a; border-radius: 6px; color: #9aa; font-size: 15px; cursor: pointer; padding: 7px 10px; line-height: 1; }
  .eye:hover { color: #e6e6e6; border-color: #3a3f4a; }
  .pathrow { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .plabel { width: 80px; flex-shrink: 0; font-size: 12px; color: #9aa; }
  .pathrow .ts { flex: 1; word-break: break-all; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
<div class="topbar">
  <h1><img src="/icon.svg" width="22" height="22" style="vertical-align:-4px;border-radius:6px;margin-right:8px" alt="">Jellyfin SyncPlay Doctor <span id="appver" class="ver">v__APP_VERSION__</span></h1>
  <div class="btnrow">
    <span id="recstatus" class="pill na">connecting…</span>
    <button id="startBtn" class="ctl" onclick="startRecorder()">&#9654; Start</button>
    <button id="stopBtn" class="ctl" onclick="stopRecorder()">&#9632; Stop</button>
    <button id="settingsBtn" class="ctl" onclick="toggleSettings()" title="Settings">&#9881; Settings</button>
  </div>
</div>
<div class="sub" id="sub">connecting…</div>
<div id="settings" class="card" style="display:none">
  <h2>Settings</h2>
  <div class="field"><label>Server URL</label><input id="cfgServer" placeholder="http://host:8096" autocomplete="off"></div>
  <div class="field"><label>Jellyfin API key</label><div class="keywrap"><input id="cfgKey" type="password" placeholder="paste your API key" autocomplete="off"><button id="eyeBtn" class="eye" onclick="toggleKeyVisibility()" title="Show / hide API key">&#128065;</button></div></div>
  <div class="field"><label>GitHub repo (for update checks)</label><input id="cfgRepo" placeholder="owner/repo" autocomplete="off"></div>
  <div class="field"><label>Client log path (optional — stall detection)</label><input id="cfgLog" placeholder="auto-detected if blank" autocomplete="off"></div>
  <div class="field"><label>File locations</label><div id="paths" class="paths"><span class="empty">loading…</span></div></div>
  <div class="field"><button id="saveBtn" class="ctl" onclick="saveConfig()">Save</button> <span id="cfgMsg"></span></div>
</div>
<div id="update"></div>
<div id="stopped"></div>
<div id="alert"></div>
<div class="card"><h2>Now playing</h2><div id="nowplaying"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Clients (connected)</h2><div id="clients"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Network (Tailscale)</h2><div id="net"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Events</h2><div id="events"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Legend</h2><div class="legend"><span class="pill dp">DirectPlay</span> native playback, no re-encode<br><span class="pill ds">DirectStream</span> container remux only (cheap)<br><span class="pill tr">Transcode</span> server re-encoding (expensive — usual cause of stalls)<br><span class="badge">RELAY (DERP)</span> not direct peer-to-peer — relaying via a Tailscale server (slower)<br><span class="stuck">● STUCK</span> position not advancing (buffering/stalled)</div></div>
</div>
<script>
function esc(x){ return String(x==null?'':x).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function pill(cls,label,tip){ return '<span class="pill '+cls+'"'+(tip?' title="'+tip+'"':'')+'>'+esc(label)+'</span>'; }
function mCls(m){ return m==='DirectPlay'?'dp':(m==='DirectStream'?'ds':(m==='Transcode'?'tr':'na')); }
async function getJSON(p){ const r = await fetch(p, {cache:'no-store'}); return r.json(); }
function render(s){
  document.getElementById('sub').innerHTML = 'server <span class="ts">'+esc(s.serverUrl)+'</span> · updated '+esc(s.lastPoll)+' · poll #'+s.pollCount;
  document.getElementById('alert').innerHTML = s.networkAlert
    ? '<div class="alert">⚠ Network stall (kj) — '+esc(s.networkAlert)+' ('+esc(s.networkAlertTime)+')</div>' : '';
  const np = document.getElementById('nowplaying');
  if (!s.sessions || !s.sessions.length) { np.innerHTML = '<span class="empty">no video playing right now</span>'; }
  else {
    let h = '<table><tr><th>User</th><th>Device</th><th>Status</th><th>Method</th><th>Bitrate</th><th>Time</th><th>Title</th><th>Reason</th></tr>';
    for (const x of s.sessions){
      const stCls = x.stuck ? 'tr' : (x.paused ? 'na' : 'dp');
      const stTip = x.stuck ? 'Playback position has not advanced for 3+ polls (buffering/stalled)' : '';
      const mTip = x.method==='Transcode' ? 'Server is re-encoding this stream (expensive; the usual cause of stalls)' : '';
      const ver = x.version ? ' <span class="ts">v'+esc(x.version)+'</span>' : '';
      h += '<tr><td>'+esc(x.user)+'</td><td>'+esc(x.device)+ver+'</td><td>'+pill(stCls,x.status,stTip)+'</td><td>'+pill(mCls(x.method),x.method,mTip)+'</td><td>'+esc(x.bitrate)+'</td><td>'+esc(x.position)+' / '+esc(x.runtime)+'</td><td>'+esc(x.item)+'</td><td>'+esc(x.reason)+'</td></tr>';
    }
    np.innerHTML = h + '</table>';
  }
  const cl = document.getElementById('clients');
  if (!s.roster || !s.roster.length) { cl.innerHTML = '<span class="empty">no clients connected</span>'; }
  else {
    let h = '<table><tr><th>User</th><th>Client</th><th>Device</th><th>Last active</th></tr>';
    for (const r of s.roster){
      const la = (r.lastActive||'').replace('T',' ').slice(0,19);
      const ver = r.version ? ' <span class="ts">v'+esc(r.version)+'</span>' : '';
      h += '<tr><td>'+esc(r.user)+'</td><td>'+esc(r.client)+ver+'</td><td>'+esc(r.device)+'</td><td class="ts">'+esc(la)+'</td></tr>';
    }
    cl.innerHTML = h + '</table>';
  }
  const net = document.getElementById('net');
  const t = s.tailscale || {};
  if (t.available) {
    const path = t.directOrRelay==='relay' ? '<span class="badge">RELAY (DERP)</span>' : '<span class="pill dp">direct</span>';
    net.innerHTML = 'path: '+path+' · latency: <span class="ts">'+esc(t.latencyMs||'-')+' ms</span>'+(t.relay?' · relay: '+esc(t.relay):'')+(t.derpRegion?' · region: '+esc(t.derpRegion):'');
  } else { net.innerHTML = '<span class="empty">Tailscale info unavailable</span>'; }
  const ev = document.getElementById('events');
  if (!s.events || !s.events.length) { ev.innerHTML = '<span class="empty">no events yet</span>'; }
  else {
    let h = '<ul class="events">';
    for (const e of s.events){ h += '<li><span class="t">'+esc(e.time)+'</span>'+esc(e.msg)+'</li>'; }
    ev.innerHTML = h + '</ul>';
  }
}
async function pollStatus(){
  try {
    const s = await getJSON('/status');
    const run = !!s.running;
    const el = document.getElementById('recstatus');
    el.className = 'pill ' + (run ? 'dp' : 'na');
    el.textContent = run ? 'recorder running' : 'recorder stopped';
    const sb = document.getElementById('stopped');
    if (sb) sb.innerHTML = run ? '' : '<div class="stopped">Recorder stopped — press Start to resume.</div>';
    const ver = document.getElementById('appver');
    if (ver) ver.textContent = s.version ? 'v' + s.version : '';
    const up = document.getElementById('update');
    if (up) up.innerHTML = s.updateAvailable
      ? '<div class="update">Update available: v' + esc(s.latestVersion) + ' — <a href="' + esc(s.releaseUrl) + '" target="_blank" rel="noopener">view release</a></div>'
      : '';
  } catch(e) {
    const el = document.getElementById('recstatus');
    el.className = 'pill na';
    el.textContent = 'status unknown — press Start';
  }
}
async function refresh(){ try { render(await getJSON('/state')); } catch(e) {} }
async function startRecorder(){ await fetch('/start', {method:'POST'}); pollStatus(); setTimeout(refresh, 1500); }
async function stopRecorder(){ if (!confirm('Stop the recorder?')) return; await fetch('/stop', {method:'POST'}); pollStatus(); refresh(); }
async function toggleSettings(){
  const el = document.getElementById('settings');
  el.style.display = el.style.display === 'none' ? '' : 'none';
  if (el.style.display !== 'none') { loadConfig(); loadPaths(); }
}
async function loadConfig(){
  try {
    const c = await getJSON('/config');
    document.getElementById('cfgServer').value = c.serverUrl || '';
    document.getElementById('cfgKey').value = c.apiKey || '';
    document.getElementById('cfgRepo').value = c.githubRepo || '';
    document.getElementById('cfgLog').value = c.clientLogPath || '';
  } catch(e) {}
}
async function saveConfig(){
  const body = {
    serverUrl: document.getElementById('cfgServer').value.trim(),
    apiKey: document.getElementById('cfgKey').value.trim(),
    githubRepo: document.getElementById('cfgRepo').value.trim(),
    clientLogPath: document.getElementById('cfgLog').value.trim()
  };
  const r = await fetch('/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  const msg = document.getElementById('cfgMsg');
  msg.textContent = j.ok ? 'Saved — recorder restarted.' : ('Error: ' + (j.error || ''));
  msg.style.color = j.ok ? '#5ee08a' : '#ff7b82';
  setTimeout(() => { msg.textContent = ''; }, 4000);
  pollStatus(); refresh();
}
function toggleKeyVisibility(){
  const inp = document.getElementById('cfgKey');
  const btn = document.getElementById('eyeBtn');
  const show = inp.type === 'password';
  inp.type = show ? 'text' : 'password';
  btn.innerHTML = show ? '&#128584;' : '&#128065;';
}
async function loadPaths(){
  try {
    const p = await getJSON('/paths');
    const el = document.getElementById('paths');
    el.innerHTML = '';
    const mk = (label, path, target) => {
      const d = document.createElement('div');
      d.className = 'pathrow';
      d.innerHTML = '<span class="plabel"></span><span class="ts"></span><button class="ctl">Open</button>';
      d.querySelector('.plabel').textContent = label;
      d.querySelector('.ts').textContent = path;
      d.querySelector('button').onclick = function(){ openPath(target); };
      el.appendChild(d);
    };
    mk('Config', p.configFile, 'configFile');
    mk('Client log', p.clientLog, 'clientLog');
    mk('Output', p.outputDir, 'outputDir');
  } catch(e) {}
}
async function openPath(target){
  await fetch('/open', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({target})});
}
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(()=>{});
  let swReloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!swReloaded) { swReloaded = true; location.reload(); }
  });
  navigator.serviceWorker.ready.then(reg => {
    if (reg.waiting) reg.waiting.postMessage('SKIP_WAITING');
    reg.addEventListener('updatefound', () => {
      const nw = reg.installing;
      if (nw) nw.addEventListener('statechange', () => {
        if (nw.state === 'installed' && navigator.serviceWorker.controller) nw.postMessage('SKIP_WAITING');
      });
    });
  }).catch(()=>{});
}
pollStatus();
refresh();
setInterval(refresh, 2000);
setInterval(pollStatus, 1000);
</script>
</body>
</html>
"""


def status():
    running = recorder is not None and recorder.poll() is None
    return {"running": running, "pid": recorder.pid if running else None,
            "version": APP_VERSION,
            "latestVersion": UPDATE_INFO.get("latest"),
            "updateAvailable": bool(UPDATE_INFO.get("updateAvailable")),
            "releaseUrl": UPDATE_INFO.get("releaseUrl", "")}


def read_state():
    # Only serve recorded data while the recorder is actually running; otherwise
    # return a clean empty state so "stopped" doesn't show stale info.
    if recorder is not None and recorder.poll() is None:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError:
                pass
    return json.dumps({"serverUrl": "", "lastPoll": "", "pollCount": 0, "sessions": [],
                       "roster": [], "tailscale": {}, "events": [], "networkAlert": ""})


def start_recorder():
    global recorder
    if recorder is not None and recorder.poll() is None:
        return
    recorder = subprocess.Popen(
        [sys.executable, RECORDER, "--out-dir", "telemetry-py", "--headless"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_recorder():
    global recorder
    if recorder is not None:
        if recorder.poll() is None:
            recorder.terminate()
            try:
                recorder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                recorder.kill()
        recorder = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/state":
            self._send(read_state(), "application/json; charset=utf-8")
        elif path == "/status":
            self._send(json.dumps(status()), "application/json; charset=utf-8")
        elif path == "/config":
            self._send(json.dumps(load_config()), "application/json; charset=utf-8")
        elif path == "/paths":
            self._send(json.dumps(get_paths()), "application/json; charset=utf-8")
        elif path == "/manifest.json":
            self._send(MANIFEST, "application/manifest+json")
        elif path == "/sw.js":
            self._send(SERVICE_WORKER, "application/javascript")
        elif path == "/icon.svg":
            self._send(ICON_SVG, "image/svg+xml")
        elif path == "/icon.png":
            try:
                with open(ICON_PNG, "rb") as f:
                    self._send(f.read(), "image/png")
            except OSError:
                self._send(b"", "image/png")
        else:
            self._send(DASHBOARD.replace("__APP_VERSION__", APP_VERSION), "text/html; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/start":
            start_recorder()
        elif path == "/stop":
            stop_recorder()
        elif path == "/config":
            try:
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                cfg = load_config()
                for k in ("serverUrl", "apiKey", "githubRepo", "clientLogPath"):
                    if k in data:
                        cfg[k] = str(data[k] or "").strip()
                save_config(cfg)
                stop_recorder()
                start_recorder()
                self._send(json.dumps({"ok": True}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            return
        elif path == "/open":
            try:
                data = json.loads(self._read_body().decode("utf-8") or "{}")
                p = get_paths().get(data.get("target", ""), "")
                ok = open_path(p)
                self._send(json.dumps({"ok": ok, "path": p}), "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            return
        self._send(json.dumps(status()), "application/json; charset=utf-8")

    def log_message(self, *args):
        pass


def serve(port):
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"no free port in {port}-{port + 19}", file=sys.stderr)
        sys.exit(1)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def main():
    ap = argparse.ArgumentParser(description="Jellyfin SyncPlay Doctor controller")
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-autostart", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    threading.Thread(target=update_loop, daemon=True).start()
    srv, port = serve(args.port)
    with open(PORT_FILE, "w") as f:
        f.write(str(port))

    if not args.no_autostart:
        start_recorder()

    print(f"Jellyfin SyncPlay Doctor — controller: http://127.0.0.1:{port}/  (Ctrl+C to quit)")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")

    try:
        while not stop_flag.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_recorder()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
