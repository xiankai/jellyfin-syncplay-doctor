#!/usr/bin/env python3
"""
Jellyfin SyncPlay telemetry + live dashboard (Python port).

Polls the Jellyfin /Sessions API and shows, per viewer: play method
(DirectPlay / DirectStream / Transcode), status (Playing / Paused / STUCK),
bitrate, position/length, full title, client version, and transcode reason.
Serves a live auto-refreshing dashboard over HTTP (standard library only) and
writes CSV/JSON logs.

Usage:
  python jellyfin_telemetry.py --dashboard          # poll + serve live dashboard
  python jellyfin_telemetry.py --once               # one snapshot, then exit
  python jellyfin_telemetry.py -h                   # full options
"""

import argparse
import atexit
import base64
import csv
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

DEFAULT_SERVER = "http://127.0.0.1:8096"

STALL_BUFFER = re.compile(r"buffer went from 100% -> 0%", re.I)
STALL_HTTP = re.compile(r"Will reconnect.*error=Error number", re.I)
STALL_SEEK = re.compile(r"Seek failed|Failed to seek", re.I)

STATE = {}
STOP = threading.Event()


def log(msg):
    print(msg, flush=True)


def load_config(config_path, server, api_key):
    cfg = {}
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            log(f"WARN: could not parse {config_path}: {e}")
    api_key = api_key or cfg.get("apiKey") or os.environ.get("JELLYFIN_API_KEY", "")
    if server == DEFAULT_SERVER and cfg.get("serverUrl"):
        server = cfg["serverUrl"]
    client_log = (cfg.get("clientLogPath") or "").strip()
    return server.rstrip("/"), api_key, client_log


def fetch_sessions(server, api_key):
    url = f"{server}/Sessions?api_key={api_key}"
    req = Request(url, headers={"X-Emby-Token": api_key})
    with urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data if isinstance(data, list) else [data]


def client_version(session):
    """Prefer the real version embedded in DeviceId (base64); some clients
    (e.g. Jellyfin Desktop) misreport ApplicationVersion as '1.0.0'."""
    did = session.get("DeviceId") or ""
    if did:
        try:
            plain = base64.b64decode(did).decode("utf-8", "replace")
            m = re.search(r"(\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z][a-zA-Z0-9]*)?)", plain)
            if m:
                return m.group(1)
        except Exception:
            pass
    av = session.get("ApplicationVersion") or ""
    return "" if av == "1.0.0" else av


def full_title(item):
    if not item:
        return ""
    name = item.get("Name") or ""
    series = item.get("SeriesName") or ""
    season = item.get("ParentIndexNumber")
    episode = item.get("IndexNumber")
    if series:
        if season and episode:
            return f"{series} S{int(season):02d}E{int(episode):02d} - {name}"
        return f"{series} - {name}"
    return name


def fmt_duration(seconds):
    s = int(float(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def bitrate_mbps(playstate, item):
    bps = playstate.get("Bitrate")
    if not bps and item:
        bps = 0
        for ms in item.get("MediaStreams") or []:
            if ms.get("Type") in ("Video", "Audio") and ms.get("BitRate"):
                bps += int(ms["BitRate"])
    return f"{bps / 1e6:.1f}" if bps else ""


def tailscale_row(server_ip):
    row = {"available": False, "directOrRelay": "", "relay": "",
           "derpRegion": "", "curAddr": "", "latencyMs": ""}
    ok = False
    try:
        p = subprocess.run(["tailscale", "status", "--json"],
                           capture_output=True, text=True, timeout=15)
        if p.returncode == 0 and p.stdout.strip():
            ts = json.loads(p.stdout)
            peer = None
            for pv in (ts.get("Peer") or {}).values():
                if server_ip in (pv.get("TailscaleIPs") or []):
                    peer = pv
                    break
            if peer:
                ok = True
                row["directOrRelay"] = "relay" if peer.get("Relay") else "direct"
                row["relay"] = peer.get("Relay") or ""
                row["derpRegion"] = peer.get("DERPRegion") or ""
                row["curAddr"] = peer.get("CurAddr") or ""
        p2 = subprocess.run(["tailscale", "ping", "-c", "3", server_ip],
                            capture_output=True, text=True, timeout=20)
        if p2.returncode == 0:
            ok = True
            out = p2.stdout + p2.stderr
            m = re.search(r"in ([\d.]+)ms", out)
            if m:
                row["latencyMs"] = m.group(1)
            if "via DERP" in out:
                row["directOrRelay"] = "relay"
    except Exception:
        pass
    row["available"] = ok
    return row


def csv_field(v):
    s = "" if v is None else str(v)
    if re.search(r'[,"\r\n]', s):
        return '"' + s.replace('"', '""') + '"'
    return s


def resolve_client_log(explicit):
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


ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect x="16" y="16" width="480" height="480" rx="112" fill="#000b25" stroke="#2a3a5c" stroke-width="4"/>'
            '<path d="M172 150 L372 256 L172 362 Z" fill="#5ee08a"/>'
            '<rect x="222" y="212" width="44" height="88" rx="10" fill="#ffffff"/>'
            '<rect x="200" y="234" width="88" height="44" rx="10" fill="#ffffff"/></svg>')
FAVICON_URI = "data:image/svg+xml;base64," + base64.b64encode(ICON_SVG.encode("utf-8")).decode("ascii")

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
<title>Jellyfin SyncPlay Doctor</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, "Segoe UI", sans-serif; margin: 0; background: #0f1115; color: #e6e6e6; }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 20px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #9aa; font-size: 13px; margin-bottom: 18px; }
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
  .topbar { display: flex; justify-content: space-between; align-items: center; }
  #stopBtn { background: #3a1215; color: #ff9aa2; border: 1px solid #5a1a1f; border-radius: 8px; padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer; }
  #stopBtn:hover { background: #4a171b; }
</style>
</head>
<body>
<div class="wrap">
<div class="topbar">
<h1><img src="{FAVICON}" width="22" height="22" style="vertical-align:-4px;border-radius:6px;margin-right:8px" alt="">Jellyfin SyncPlay Doctor</h1>
<button id="stopBtn" onclick="stopRecorder()" title="Stop the recorder (the dashboard will go offline)">&#9632; Stop</button>
</div>
<div class="sub" id="sub">connecting…</div>
<div id="alert"></div>
<div class="card"><h2>Now playing</h2><div id="nowplaying"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Clients (connected)</h2><div id="clients"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Network (Tailscale)</h2><div id="net"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Events</h2><div id="events"><span class="empty">waiting for data…</span></div></div>
<div class="card"><h2>Legend</h2><div class="legend"><span class="pill dp">DirectPlay</span> native playback, no re-encode<br><span class="pill ds">DirectStream</span> container remux only (cheap)<br><span class="pill tr">Transcode</span> server re-encoding (expensive — usual cause of stalls)<br><span class="badge">RELAY (DERP)</span> not direct peer-to-peer — relaying via a Tailscale server (slower)<br><span class="stuck">● STUCK</span> position not advancing (buffering/stalled)</div></div>
</div>
<script>
function esc(x){ return String(x==null?'':x).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function pill(cls, label, tip){ return '<span class="pill '+cls+'"'+(tip?' title="'+tip+'"':'')+'>'+esc(label)+'</span>'; }
function mCls(m){ return m==='DirectPlay'?'dp':(m==='DirectStream'?'ds':(m==='Transcode'?'tr':'na')); }
async function refresh(){
  try {
    const r = await fetch('/state', {cache:'no-store'});
    const s = await r.json();
    render(s);
  } catch(e) { document.getElementById('sub').textContent = 'offline / not reachable'; }
}
function render(s){
  document.getElementById('sub').innerHTML = 'server <span class="ts">'+esc(s.serverUrl)+'</span> · updated '+esc(s.lastPoll)+' · poll #'+s.pollCount;
  const a = document.getElementById('alert');
  a.innerHTML = s.networkAlert
    ? '<div class="alert">⚠ Network stall (kj) — '+esc(s.networkAlert)+' ('+esc(s.networkAlertTime)+')</div>'
    : '';
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
    const path = t.directOrRelay==='relay'
      ? '<span class="badge" title="Not a direct peer-to-peer link — traffic is relaying through a Tailscale server (slower)">RELAY (DERP)</span>'
      : '<span class="pill dp" title="Direct peer-to-peer connection">direct</span>';
    net.innerHTML = 'path: '+path+' · latency: <span class="ts">'+esc(t.latencyMs||'-')+' ms</span>'+(t.relay?' · relay: '+esc(t.relay):'')+(t.derpRegion?' · region: '+esc(t.derpRegion):'');
  } else {
    net.innerHTML = '<span class="empty">Tailscale info unavailable (needs local-API access)</span>';
  }
  const ev = document.getElementById('events');
  if (!s.events || !s.events.length) { ev.innerHTML = '<span class="empty">no events yet</span>'; }
  else {
    let h = '<ul class="events">';
    for (const e of s.events){ h += '<li><span class="t">'+esc(e.time)+'</span>'+esc(e.msg)+'</li>'; }
    ev.innerHTML = h + '</ul>';
  }
}
async function stopRecorder(){
  if (!confirm('Stop the recorder? The dashboard will go offline.')) return;
  try { await fetch('/stop'); } catch(e) {}
  document.body.innerHTML = '<div class="wrap"><h1>Jellyfin SyncPlay Doctor</h1><div class="card"><h2>Stopped</h2><p>Recorder stopped. Launch it again to resume (Start Menu shortcut / Start-Dashboard).</p></div></div>';
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
""".replace("{FAVICON}", FAVICON_URI)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/state":
            body = json.dumps(STATE).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif self.path == "/stop":
            STOP.set()
            body = b"stopping"
            ctype = "text/plain"
        else:
            body = DASHBOARD_HTML.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        log(f"WARN: no free port in {port}-{port + 19}; dashboard disabled, continuing headless")
        return None, None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def main():
    ap = argparse.ArgumentParser(description="Jellyfin SyncPlay telemetry + live dashboard")
    ap.add_argument("--config", default="telemetry.config.json")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--duration-min", type=int, default=0)
    ap.add_argument("--stuck-polls", type=int, default=3)
    ap.add_argument("--out-dir", default="telemetry")
    ap.add_argument("--port", type=int, default=8124)
    ap.add_argument("--client-log-path", default="")
    ap.add_argument("--dashboard", action="store_true", help="serve the live dashboard over HTTP")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--headless", action="store_true", help="poll and write files only (no HTTP server)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-tailscale", action="store_true")
    ap.add_argument("--no-client-log", action="store_true")
    args = ap.parse_args()

    server, api_key, cfg_client_log = load_config(args.config, args.server, args.api_key)
    if not api_key:
        print("No API key found. Put it in telemetry.config.json, set JELLYFIN_API_KEY, or pass --api-key.")
        sys.exit(1)
    m = re.match(r"https?://([^/:]+)", server)
    server_ip = m.group(1) if m else ""

    os.makedirs(args.out_dir, exist_ok=True)
    pid_file = os.path.join(args.out_dir, "recorder.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
    def _cleanup_pid():
        try:
            os.remove(pid_file)
        except OSError:
            pass
    atexit.register(_cleanup_pid)
    csv_path = os.path.join(args.out_dir, "sessions.csv")
    jsonl_path = os.path.join(args.out_dir, "sessions.jsonl")
    state_path = os.path.join(args.out_dir, "state.json")
    ts_path = os.path.join(args.out_dir, "tailscale.csv")
    client_log_path = os.path.join(args.out_dir, "client.log")

    if not os.path.exists(csv_path):
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("timestamp,userName,deviceName,client,itemName,playMethod,isPaused,"
                    "positionSec,runtimeSec,bitrateMbps,transcodeReason,transcodeVideoCodec,"
                    "transcodeAudioCodec,transcodeCompletionPct,stuck\n")
    if not os.path.exists(ts_path):
        with open(ts_path, "w", encoding="utf-8") as f:
            f.write("timestamp,directOrRelay,relay,derpRegion,curAddr,latencyMs\n")

    STATE.update({
        "serverUrl": server, "startedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "lastPoll": "", "pollCount": 0, "sessions": [], "roster": [],
        "tailscale": {"available": False, "directOrRelay": "", "relay": "", "derpRegion": "", "curAddr": "", "latencyMs": ""},
        "events": [], "networkAlert": "", "networkAlertTime": "",
    })

    jf_log = resolve_client_log(args.client_log_path or cfg_client_log)
    client_offset = 0
    if not args.no_client_log and os.path.exists(jf_log):
        client_offset = os.path.getsize(jf_log)

    if args.dashboard and not args.headless:
        srv, actual_port = serve(args.port)
        if srv:
            with open(os.path.join(args.out_dir, "port.txt"), "w") as f:
                f.write(str(actual_port))
            log(f"Dashboard: http://127.0.0.1:{actual_port}/  (Ctrl+C or the Stop button to quit)")
            if not args.no_browser:
                webbrowser.open(f"http://127.0.0.1:{actual_port}/")
        else:
            log("Dashboard disabled (no free port). Recording continues headless.")

    last_pos = {}
    no_advance = {}
    seen_transcode = set()
    seen_stuck = set()
    events = []
    ts_warned = False

    started = time.time()
    poll = 0

    while not STOP.is_set():
        poll += 1
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        rows = []

        try:
            sessions = fetch_sessions(server, api_key)
            STATE["roster"] = [{
                "user": s.get("UserName"), "client": s.get("Client"),
                "device": s.get("DeviceName"), "lastActive": s.get("LastActivityDate"),
                "version": client_version(s),
            } for s in sessions]

            video = [s for s in sessions if s.get("NowPlayingItem") and s["NowPlayingItem"].get("MediaType") == "Video"]
            if not video:
                log(f"[{ts}] No active video sessions.")

            for s in video:
                ps = s.get("PlayState") or {}
                item = s.get("NowPlayingItem") or {}
                sid = str(s.get("Id") or "")
                pos = ps.get("PositionTicks") or 0
                pos_s = pos / 1e7 if pos else 0.0

                delta = 0.0
                if sid in last_pos:
                    delta = pos_s - last_pos[sid]
                last_pos[sid] = pos_s

                stuck = ""
                if not ps.get("IsPaused"):
                    no_advance[sid] = no_advance.get(sid, 0) + 1 if delta < 0.2 else 0
                    if no_advance[sid] >= args.stuck_polls:
                        stuck = "STUCK"
                else:
                    no_advance[sid] = 0

                tc = ps.get("TranscodeInfo") or {}
                reason = "; ".join(tc.get("TranscodeReasons") or [])
                bps = ps.get("Bitrate")
                if not bps:
                    bps = 0
                    for ms in item.get("MediaStreams") or []:
                        if ms.get("Type") in ("Video", "Audio") and ms.get("BitRate"):
                            bps += int(ms["BitRate"])
                bm = f"{bps / 1e6:.1f}" if bps else ""
                rt = item.get("RunTimeTicks")

                csv_row = [ts, s.get("UserName"), s.get("DeviceName"), s.get("Client"),
                           full_title(item), ps.get("PlayMethod"), bool(ps.get("IsPaused")),
                           f"{pos_s:.1f}", str(int(rt / 1e7)) if rt else "",
                           bm, reason, tc.get("VideoCodec") or "", tc.get("AudioCodec") or "",
                           f"{tc['CompletionPercentage']:.1f}" if tc.get("CompletionPercentage") is not None else "",
                           stuck]
                with open(csv_path, "a", newline="", encoding="utf-8") as f:
                    f.write(",".join(csv_field(v) for v in csv_row) + "\n")

                log(f"  {s.get('UserName'):<12} {s.get('DeviceName') or '':<20} "
                    f"{ps.get('PlayMethod') or '':<13} {bm:>6}  "
                    f"pos {fmt_duration(pos_s)}  {full_title(item)[:40]}")

                rows.append({
                    "user": s.get("UserName"), "device": s.get("DeviceName"),
                    "client": s.get("Client"), "version": client_version(s),
                    "item": full_title(item), "method": ps.get("PlayMethod"),
                    "paused": bool(ps.get("IsPaused")), "stuck": stuck == "STUCK",
                    "status": "STUCK" if stuck == "STUCK" else ("Paused" if ps.get("IsPaused") else "Playing"),
                    "position": fmt_duration(pos_s),
                    "runtime": fmt_duration(rt / 1e7) if rt else "",
                    "bitrate": (bm + " Mbps") if bm else "",
                    "reason": reason,
                })

                if ps.get("PlayMethod") == "Transcode" and sid not in seen_transcode:
                    seen_transcode.add(sid)
                    events.insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                      "msg": f"Transcode: {s.get('UserName')} — {reason}"})
                elif ps.get("PlayMethod") != "Transcode":
                    seen_transcode.discard(sid)

                if stuck == "STUCK" and sid not in seen_stuck:
                    seen_stuck.add(sid)
                    events.insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                      "msg": f"STUCK: {s.get('UserName')} — position not advancing"})
                elif stuck != "STUCK":
                    seen_stuck.discard(sid)

        except Exception as e:
            log(f"WARN poll {poll} failed: {e}")

        STATE["sessions"] = rows
        STATE["lastPoll"] = ts
        STATE["pollCount"] = poll
        events = events[:30]
        STATE["events"] = events

        if not args.no_tailscale:
            tr = tailscale_row(server_ip)
            if tr["available"]:
                STATE["tailscale"] = tr
                with open(ts_path, "a", encoding="utf-8") as f:
                    f.write(",".join([ts, tr["directOrRelay"], tr["relay"], tr["derpRegion"],
                                      tr["curAddr"], tr["latencyMs"]]) + "\n")
            else:
                STATE["tailscale"]["available"] = False
                if not ts_warned:
                    log("  (note: Tailscale info skipped -- needs local-API access (admin/sudo))")
                    ts_warned = True

        # client log tail + network-stall detection
        if not args.no_client_log and os.path.exists(jf_log):
            try:
                size = os.path.getsize(jf_log)
                if size > client_offset:
                    with open(jf_log, "rb") as f:
                        f.seek(client_offset)
                        chunk = f.read(size - client_offset).decode("utf-8", "replace")
                    client_offset = size
                    for line in chunk.splitlines():
                        stall = ""
                        if STALL_BUFFER.search(line):
                            stall = "player buffer drained (100% -> 0%) — rebuffering"
                        elif STALL_HTTP.search(line):
                            stall = "HTTP read timeout — reconnecting"
                        elif STALL_SEEK.search(line):
                            stall = "seek failed (stream interrupted)"
                        if stall:
                            events.insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                              "msg": f"kj: {stall}"})
                            events = events[:30]
                            STATE["events"] = events
                            STATE["networkAlert"] = stall
                            STATE["networkAlertTime"] = datetime.now().strftime("%H:%M:%S")
                        if re.search(r"(?i)(playing url|entering state|syncplay/(join|ready|pause|unpause|leave|stop)|opening done|master\.m3u8|stream\.mkv|buffering|cached range|error|failed)", line):
                            with open(client_log_path, "a", encoding="utf-8") as f:
                                f.write(f"[{ts}] {line}\n")
            except Exception:
                pass

        if rows:
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "poll": poll, "sessions": rows,
                                    "tailscale": STATE["tailscale"]}) + "\n")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)

        if args.once:
            break
        if args.duration_min and (time.time() - started) >= args.duration_min * 60:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
