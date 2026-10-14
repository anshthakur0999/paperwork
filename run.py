"""Launch the offline app and open its local browser interface."""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app import create_app


def choose_port(start: int) -> tuple[int, bool]:
    for port in range(start, min(start + 20, 65536)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port, False
            except OSError:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
                        if json.load(response).get("app") == "paperwork-local":
                            return port, True
                except Exception:
                    pass
    raise RuntimeError("No local port is available. Close an existing Paperwork window and try again.")


def main():
    parser = argparse.ArgumentParser(description="Paperwork Local PDF Studio")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65515:
        parser.error("Choose a port between 1024 and 65515.")
    port, already_running = choose_port(args.port)
    url = f"http://127.0.0.1:{port}"
    if already_running:
        if not args.no_browser:
            webbrowser.open(url)
        return
    local_app = create_app()
    server = uvicorn.Server(uvicorn.Config(local_app, host="127.0.0.1", port=port, access_log=False, log_level="warning", workers=1))
    local_app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    if not args.no_browser:
        def open_when_ready():
            for _ in range(100):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(0.1)
        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"Paperwork is running at {url}", flush=True)
    print("Your documents are processed locally. Close the server with Ctrl+C.", flush=True)
    server.run()


if __name__ == "__main__":
    main()
