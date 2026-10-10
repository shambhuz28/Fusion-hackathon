"""Serves the project root so autonomy/index.html can load ../app/js modules and ../app/data, at
http://127.0.0.1:<port>/autonomy/ (default port 8770; the next free one if it is taken).

Usage:
    python autonomy/serve.py [--port 8770]
"""

import argparse
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):   # the main simulation lives in app/ when served from the project root
            self.send_response(302)
            self.send_header("Location", "/app/")
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")   # always serve the latest run.json and code
        super().end_headers()

    def log_message(self, fmt, *args):
        if args and str(args[1])[:1] in "45":
            super().log_message(fmt, *args)


def free_port(port, tries=20):
    for candidate in range(port, port + tries):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"No free port between {port} and {port + tries - 1}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    port = free_port(args.port)
    if port != args.port:
        print(f"Port {args.port} is in use; using {port}.")
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(ROOT)))
    print(f"Crosslink simulation at http://127.0.0.1:{port}/autonomy/ (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
