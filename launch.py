#!/usr/bin/env python3
"""Launcher: open the running dashboard, or start the recorder."""
import os
import subprocess
import sys
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT_FILE = os.path.join(ROOT, "telemetry-py", "port.txt")


def running_url():
    if not os.path.exists(PORT_FILE):
        return None
    try:
        port = open(PORT_FILE).read().strip()
        url = f"http://127.0.0.1:{port}/"
        urllib.request.urlopen(url + "state", timeout=1)
        return url
    except Exception:
        return None


def main():
    url = running_url()
    if url:
        webbrowser.open(url)
        return
    subprocess.run([sys.executable, os.path.join(ROOT, "controller.py")], cwd=ROOT)


if __name__ == "__main__":
    main()
