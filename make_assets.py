#!/usr/bin/env python3
"""Generate the icon and demo dashboard screenshots for Jellyfin SyncPlay Doctor."""
import base64
import html
import os
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(ROOT, "assets")
SHOTS = os.path.join(ROOT, "screenshots")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect x="16" y="16" width="480" height="480" rx="112" fill="#000b25" stroke="#2a3a5c" stroke-width="4"/>'
            '<path d="M172 150 L372 256 L172 362 Z" fill="#5ee08a"/>'
            '<rect x="222" y="212" width="44" height="88" rx="10" fill="#ffffff"/>'
            '<rect x="200" y="234" width="88" height="44" rx="10" fill="#ffffff"/></svg>')
FAVICON_URI = "data:image/svg+xml;base64," + base64.b64encode(ICON_SVG.encode("utf-8")).decode("ascii")

CSS = """
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
"""

LEGEND = ('<span class="pill dp">DirectPlay</span> native playback, no re-encode<br>'
          '<span class="pill ds">DirectStream</span> container remux only (cheap)<br>'
          '<span class="pill tr">Transcode</span> server re-encoding (expensive — usual cause of stalls)<br>'
          '<span class="badge">RELAY (DERP)</span> not direct peer-to-peer — relaying via a Tailscale server (slower)<br>'
          '<span class="stuck">● STUCK</span> position not advancing (buffering/stalled)')


def esc(s):
    return html.escape("" if s is None else str(s))


def pill(cls, label):
    return f'<span class="pill {cls}">{esc(label)}</span>'


def render(state):
    if not state.get("sessions"):
        now = '<tr><td colspan="8" class="empty">no video playing right now</td></tr>'
    else:
        rows = []
        for x in state["sessions"]:
            st_cls = "tr" if x.get("stuck") else ("na" if x.get("paused") else "dp")
            m_cls = {"DirectPlay": "dp", "DirectStream": "ds", "Transcode": "tr"}.get(x.get("method"), "na")
            ver = f' <span class="ts">v{esc(x.get("version"))}</span>' if x.get("version") else ""
            rows.append(f'<tr><td>{esc(x.get("user"))}</td><td>{esc(x.get("device"))}{ver}</td>'
                        f'<td>{pill(st_cls, x.get("status", "Playing"))}</td>'
                        f'<td>{pill(m_cls, x.get("method"))}</td>'
                        f'<td>{esc(x.get("bitrate"))}</td>'
                        f'<td>{esc(x.get("position"))} / {esc(x.get("runtime"))}</td>'
                        f'<td>{esc(x.get("item"))}</td><td>{esc(x.get("reason"))}</td></tr>')
        now = "".join(rows)

    clients = ""
    for r in state.get("roster", []):
        ver = f' <span class="ts">v{esc(r.get("version"))}</span>' if r.get("version") else ""
        la = (r.get("lastActive") or "").replace("T", " ")[:19]
        clients += f'<tr><td>{esc(r.get("user"))}</td><td>{esc(r.get("client"))}{ver}</td><td>{esc(r.get("device"))}</td><td class="ts">{esc(la)}</td></tr>'

    t = state.get("tailscale") or {}
    if t.get("available"):
        path = '<span class="badge">RELAY (DERP)</span>' if t.get("directOrRelay") == "relay" else '<span class="pill dp">direct</span>'
        net = f'path: {path} · latency: <span class="ts">{esc(t.get("latencyMs", "-"))} ms</span>'
        if t.get("relay"):
            net += f' · relay: {esc(t["relay"])}'
        if t.get("derpRegion"):
            net += f' · region: {esc(t["derpRegion"])}'
    else:
        net = '<span class="empty">Tailscale info unavailable</span>'

    if state.get("events"):
        ev = '<ul class="events">' + "".join(f'<li><span class="t">{esc(e["time"])}</span>{esc(e["msg"])}</li>' for e in state["events"]) + '</ul>'
    else:
        ev = '<span class="empty">no events yet</span>'

    alert = ""
    if state.get("networkAlert"):
        alert = (f'<div class="alert">&#9888; Network stall (Bob) — {esc(state["networkAlert"])} '
                 f'({esc(state.get("networkAlertTime", ""))})</div>')

    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            '<link rel="icon" type="image/svg+xml" href="' + FAVICON_URI + '">'
            "<title>Jellyfin SyncPlay Doctor</title><style>" + CSS + "</style></head><body><div class=\"wrap\">"
            '<h1><img src="' + FAVICON_URI + '" width="22" height="22" style="vertical-align:-4px;border-radius:6px;margin-right:8px" alt="">Jellyfin SyncPlay Doctor</h1>'
            f'<div class="sub">server <span class="ts">{esc(state.get("serverUrl"))}</span> · updated {esc(state.get("lastPoll"))} · poll #{esc(state.get("pollCount"))}</div>'
            f'{alert}'
            '<div class="card"><h2>Now playing</h2><table><tr><th>User</th><th>Device</th><th>Status</th><th>Method</th><th>Bitrate</th><th>Time</th><th>Title</th><th>Reason</th></tr>' + now + '</table></div>'
            '<div class="card"><h2>Clients (connected)</h2><table><tr><th>User</th><th>Client</th><th>Device</th><th>Last active</th></tr>' + clients + '</table></div>'
            f'<div class="card"><h2>Network (Tailscale)</h2>{net}</div>'
            f'<div class="card"><h2>Events</h2>{ev}</div>'
            f'<div class="card"><h2>Legend</h2><div class="legend">{LEGEND}</div></div>'
            "</div></body></html>")


def make_icon():
    from PIL import Image, ImageDraw
    os.makedirs(ASSETS, exist_ok=True)
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([16, 16, 496, 496], radius=112, fill="#000b25", outline="#2a3a5c", width=4)
    d.polygon([(172, 150), (372, 256), (172, 362)], fill="#5ee08a")
    d.rounded_rectangle([222, 212, 266, 300], radius=10, fill="#ffffff")   # vertical bar
    d.rounded_rectangle([200, 234, 288, 278], radius=10, fill="#ffffff")   # horizontal bar
    img.save(os.path.join(ASSETS, "icon.png"))
    img.save(os.path.join(ASSETS, "icon.ico"), sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
           '<rect x="16" y="16" width="480" height="480" rx="112" fill="#000b25" stroke="#2a3a5c" stroke-width="4"/>'
           '<path d="M172 150 L372 256 L172 362 Z" fill="#5ee08a"/>'
           '<rect x="222" y="212" width="44" height="88" rx="10" fill="#ffffff"/>'
           '<rect x="200" y="234" width="88" height="44" rx="10" fill="#ffffff"/>'
           '</svg>')
    with open(os.path.join(ASSETS, "icon.svg"), "w", encoding="utf-8") as f:
        f.write(svg)
    print("icon: assets/icon.svg + icon.png + icon.ico")


def screenshot(html_path, png_path):
    url = "file:///" + html_path.replace("\\", "/")
    subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--window-size=1080,1500", f"--screenshot={png_path}", url],
                   check=True, timeout=60, capture_output=True)


def make_screenshots():
    os.makedirs(SHOTS, exist_ok=True)
    item = "Demo Movie (2024)"
    roster = [
        {"user": "Alice", "client": "Jellyfin Media Player", "device": "Alice-Laptop", "version": "1.12.0", "lastActive": "2026-09-26T22:50:00"},
        {"user": "Bob", "client": "Jellyfin Media Player", "device": "Bob-Desktop", "version": "1.12.0", "lastActive": "2026-09-26T22:50:00"},
        {"user": "Carol", "client": "Jellyfin Desktop", "device": "Jellyfin Desktop", "version": "3.0.0-dev", "lastActive": "2026-09-26T22:50:00"},
    ]
    tail_direct = {"available": True, "directOrRelay": "direct", "relay": "", "derpRegion": "", "latencyMs": "12"}
    tail_relay = {"available": True, "directOrRelay": "relay", "relay": "sin", "derpRegion": "sin", "latencyMs": "135"}

    def session(user, device, version, method, status, bitrate, reason="", stuck=False, paused=False, pos="42:55"):
        return {"user": user, "device": device, "version": version, "method": method,
                "status": status, "stuck": stuck, "paused": paused, "bitrate": bitrate,
                "position": pos, "runtime": "55:11", "item": item, "reason": reason}

    states = {
        "all-fine": {
            "serverUrl": "http://your-jellyfin:8096", "lastPoll": "2026-09-26T22:50:10", "pollCount": 1012,
            "sessions": [
                session("Alice", "Alice-Laptop", "1.12.0", "DirectPlay", "Playing", "4.5 Mbps"),
                session("Bob", "Bob-Desktop", "1.12.0", "DirectPlay", "Playing", "4.5 Mbps"),
                session("Carol", "Jellyfin Desktop", "3.0.0-dev", "DirectPlay", "Playing", "4.5 Mbps"),
            ],
            "roster": roster, "tailscale": tail_direct, "events": [], "networkAlert": "",
        },
        "transcoding": {
            "serverUrl": "http://your-jellyfin:8096", "lastPoll": "2026-09-26T22:51:30", "pollCount": 1028,
            "sessions": [
                session("Alice", "Alice-Laptop", "1.12.0", "Transcode", "Playing", "3.6 Mbps", reason="VideoCodecNotSupported"),
                session("Bob", "Bob-Desktop", "1.12.0", "DirectPlay", "Playing", "4.5 Mbps"),
                session("Carol", "Jellyfin Desktop", "3.0.0-dev", "DirectPlay", "Playing", "4.5 Mbps"),
            ],
            "roster": roster, "tailscale": tail_direct,
            "events": [{"time": "22:51:30", "msg": "Transcode: Alice — VideoCodecNotSupported"}], "networkAlert": "",
        },
        "stuck": {
            "serverUrl": "http://your-jellyfin:8096", "lastPoll": "2026-09-26T22:52:45", "pollCount": 1043,
            "sessions": [
                session("Alice", "Alice-Laptop", "1.12.0", "DirectPlay", "Playing", "4.5 Mbps"),
                session("Bob", "Bob-Desktop", "1.12.0", "DirectPlay", "STUCK", "4.5 Mbps", stuck=True, pos="42:55"),
                session("Carol", "Jellyfin Desktop", "3.0.0-dev", "DirectPlay", "Playing", "4.5 Mbps"),
            ],
            "roster": roster, "tailscale": tail_relay,
            "events": [
                {"time": "22:52:45", "msg": "Bob: player buffer drained (100% -> 0%) — rebuffering"},
                {"time": "22:52:40", "msg": "Bob: HTTP read timeout — reconnecting"},
                {"time": "22:51:30", "msg": "Transcode: Alice — VideoCodecNotSupported"},
            ],
            "networkAlert": "player buffer drained (100% -> 0%) — rebuffering", "networkAlertTime": "22:52:45",
        },
    }

    for name, state in states.items():
        html_path = os.path.join(SHOTS, f"_tmp_{name}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(render(state))
        png_path = os.path.join(SHOTS, f"{name}.png")
        screenshot(html_path, png_path)
        os.remove(html_path)
        print(f"screenshot: screenshots/{name}.png")


def make_social_preview():
    from PIL import Image, ImageDraw, ImageFont
    W, H = 1280, 640
    img = Image.new("RGB", (W, H), "#000b25")
    d = ImageDraw.Draw(img)
    icon = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    di = ImageDraw.Draw(icon)
    di.rounded_rectangle([8, 8, 248, 248], radius=56, fill="#0a1a3a", outline="#2a3a5c", width=3)
    di.polygon([(86, 75), (186, 128), (86, 181)], fill="#5ee08a")
    di.rounded_rectangle([111, 106, 133, 150], radius=5, fill="#ffffff")
    di.rounded_rectangle([100, 117, 144, 139], radius=5, fill="#ffffff")
    img.paste(icon, (88, 192), icon)
    try:
        big = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 72)
        small = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 30)
    except Exception:
        big = ImageFont.load_default()
        small = ImageFont.load_default()
    d.text((400, 220), "Jellyfin SyncPlay Doctor", font=big, fill="#ffffff")
    d.text((400, 330), "Diagnose your watch party", font=small, fill="#5ee08a")
    img.save(os.path.join(ASSETS, "social-preview.png"))
    print("social preview: assets/social-preview.png")


if __name__ == "__main__":
    make_icon()
    make_screenshots()
    make_social_preview()
    print("done")
