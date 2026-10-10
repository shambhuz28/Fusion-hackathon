"""Serves the simulation (app/) and screens conjunctions on demand for any date in the clock range.

    GET /api/screen?start=<ISO 8601 UTC>&hours=<1..168>[&dataset=history/<event>]
        -> the same JSON as app/data/conjunctions.json, for the window starting at the hour of `start`.
    GET /api/datasets
        -> the live data and every historical replay built by src/history.py.
    GET /api/maneuver?n1=<NORAD>&n2=<NORAD>&tca_ms=<ms>&not_before_ms=<ms>[&dataset=...]
        -> avoidance manoeuvre plans for that conjunction (src/maneuver.py).
    GET /api/status
        -> the live screening's window and whether the daily update is running.

Daily update: the live data always covers today 00:00 UTC to 7 days ahead. At start-up and every
15 minutes the server checks the live screening; when it does not start today (e.g. after midnight UTC),
src/daily_update.py runs in the background (fresh elements, screening, ML ranking) and swaps the new files
in when it is done; the page then offers to reload. --no-daily-update turns this off.

Screening uses src/screen_conjunctions.py on a persistent worker pool (24 h takes ~15-30 s). Results
are cached in data/cache/, keyed by the loaded objects, the start hour and the window length, so
returning to a date is instant. One screening runs at a time; other requests wait for it.

Usage:
    python src/server.py [--port 8765] [--workers 6]
"""

import argparse
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import _venv  # noqa: F401  (re-runs with .venv/bin/python if packages are missing)
import daily_update
import maneuver
import screen_conjunctions as sc
import telemetry

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
AUTONOMY_DIR = ROOT / "autonomy"   # the crosslink simulation, served at /autonomy/ (it loads /app/js and /app/data)
CACHE_DIR = ROOT / "data" / "cache"
MAX_HOURS = 168
MAX_CACHED_PLANS = 64


class ScreeningError(ValueError):
    """A request the server refuses (bad parameters or outside the data's range)."""


DATASET_RE = re.compile(r"^(history/[a-z0-9-]+)?$")


class Screener:
    """Screens any dataset ("" = live, "history/<event>"); one worker pool, rebuilt when the dataset changes."""

    def __init__(self, workers):
        self.workers = workers
        self.pool = None
        self.pool_dataset = None
        self.lock = threading.Lock()
        self.objects = {}            # dataset -> (mtime, objs, sats, validity) for manoeuvre planning and red zones
        self.objects_lock = threading.Lock()
        self.plans = {}              # request -> manoeuvre plans (JSON bytes), oldest dropped first

    def loaded(self, dataset, path):
        """(objs, sats, validity) of a dataset, reloaded when its leo_objects.json changes."""
        with self.objects_lock:
            cached = self.objects.get(dataset)
            if cached is None or cached[0] != path.stat().st_mtime:
                objs, sats = sc.load_objects(path)
                valid_from, valid_to, _ = sc.load_validity(path)
                cached = (path.stat().st_mtime, objs, sats, (valid_from, valid_to))
                self.objects[dataset] = cached
        return cached[1:]

    def plan_maneuver(self, dataset, query):
        path, _ = self.meta(dataset)
        try:
            n1, n2 = int(query["n1"][0]), int(query["n2"][0])
            tca_ms, not_before_ms = float(query["tca_ms"][0]), float(query.get("not_before_ms", ["0"])[0])
        except (KeyError, ValueError):
            raise ScreeningError("Use n1=<NORAD>&n2=<NORAD>&tca_ms=<ms>&not_before_ms=<ms>.") from None
        objs, sats, valid = self.loaded(dataset, path)
        # Plans are requested each time a conjunction is analysed, so keep the latest ones.
        key = (dataset, path.stat().st_mtime, n1, n2, tca_ms, not_before_ms)
        if key not in self.plans:
            try:
                result = maneuver.plan_conjunction(objs, sats, valid, n1, n2, tca_ms, not_before_ms)
            except ValueError as err:
                raise ScreeningError(str(err)) from None
            if len(self.plans) >= MAX_CACHED_PLANS:
                self.plans.pop(next(iter(self.plans)))
            self.plans[key] = json.dumps(result).encode()
        return self.plans[key]

    @staticmethod
    def meta(dataset):
        if not DATASET_RE.match(dataset):
            raise ScreeningError(f"Unknown dataset {dataset!r}.")
        path = sc.dataset_dirs(dataset)[0] / "leo_objects.json"
        if not path.exists():
            raise ScreeningError(f"Dataset {dataset or 'live'} has not been built (run ./run.sh or src/history.py).")
        objects = json.loads(path.read_text())
        return path, objects

    def datasets(self):
        out = []
        for path in [sc.APP_DATA / "leo_objects.json", *sorted((sc.APP_DATA / "history").glob("*/leo_objects.json"))]:
            objects = json.loads(path.read_text())
            dataset = "" if path.parent == sc.APP_DATA else f"history/{path.parent.name}"
            ev = objects.get("event")
            out.append({"dataset": dataset,
                        "label": f"{ev['name']} ({ev['start'][:4]})" if ev else "Live (CelesTrak, current)",
                        "event": ev})
        return out

    def screen(self, dataset, start_text, hours_text):
        objects_path, objects = self.meta(dataset)
        try:
            start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            hours = float(hours_text)
        except (AttributeError, TypeError, ValueError):
            raise ScreeningError("Use start=<ISO 8601 UTC time>&hours=<1-168>.") from None
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if not math.isfinite(hours) or not 1 <= hours <= MAX_HOURS:
            raise ScreeningError(f"hours must be between 1 and {MAX_HOURS}.")
        start = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        lo, hi = objects["clock_range_ms"]
        if not lo <= start.timestamp() * 1000 <= hi:
            fmt = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
            raise ScreeningError(f"{start:%Y-%m-%d %H:%M} UTC is outside the loaded orbital data's range "
                                 f"({fmt(lo)} - {fmt(hi)} UTC).")
        # Never screen past the end of the clock range.
        hours = min(hours, (hi - start.timestamp() * 1000) / 3600000)
        hours = max(1.0, math.floor(hours))

        stamp = objects["generated_utc"].replace(":", "").replace("-", "")
        # "z1": cache entries written before the red zones existed are not reused.
        path = CACHE_DIR / (dataset or "live") / f"{stamp}_{start:%Y%m%dT%H}Z_{hours:g}h_z1.json"
        if path.exists():
            return path.read_bytes()
        with self.lock:
            if path.exists():          # screened by the request we were waiting for
                return path.read_bytes()
            if self.pool is None or self.pool_dataset != dataset:
                if self.pool is not None:
                    self.pool.terminate()
                self.pool = sc.make_pool(self.workers, objects_path)
                self.pool_dataset = dataset
            began = time.time()
            with telemetry.span("On-demand screening: {hours} h from {start}", hours=hours, start=start.isoformat(),
                                dataset=dataset or "live"):
                _, trusted, counts = sc.run_screening(self.pool, self.workers, start, hours)
                objs, sats, valid = self.loaded(dataset, objects_path)
                zones = sc.zones_payload(trusted, objs, sats, start, valid)
            body = json.dumps(sc.app_payload(start, hours, trusted, counts, len(objects["objects"]), zones=zones),
                              separators=(",", ":")).encode()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
            print(f"Screened {hours:g} h from {start:%Y-%m-%d %H:%M} UTC in {time.time() - began:.0f} s "
                  f"(red {counts['red']}, yellow {counts['yellow']})", flush=True)
            return body

    def close(self):
        if self.pool is not None:
            self.pool.terminate()


class DailyUpdater:
    """Keeps the live screening on today's 7 days by running src/daily_update.py when it is out of date."""

    CHECK_S = 15 * 60
    RETRY_AFTER_FAILURE_S = 60 * 60

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.started = None
        self.failed_at = None
        self.last_error = None
        threading.Thread(target=self._loop, name="daily-update", daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.check()
            except Exception:  # never let the checker thread die
                traceback.print_exc()
            time.sleep(self.CHECK_S)

    def _finished(self):
        """Record the result of a finished update run (call with the lock held)."""
        if self.proc is not None and self.proc.poll() is not None:
            ok = self.proc.returncode == 0
            self.last_error = None if ok else f"the daily update failed (exit {self.proc.returncode}); see the server log"
            self.failed_at = None if ok else time.time()
            self.proc = None

    def check(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._finished()
            if daily_update.is_current():
                return
            if self.failed_at and time.time() - self.failed_at < self.RETRY_AFTER_FAILURE_S:
                return
            print("The live screening does not start today: running src/daily_update.py in the background", flush=True)
            self.proc = subprocess.Popen([sys.executable, str(Path(daily_update.__file__))], cwd=ROOT)
            self.started = time.time()

    def status(self):
        with self.lock:
            self._finished()
            running = self.proc is not None
            start = daily_update.live_window_start()
            return {
                "window_start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ") if start else None,
                "today_utc": daily_update.today_utc().strftime("%Y-%m-%d"),
                "current": daily_update.is_current(),
                "updating": running,
                "update_started_utc": datetime.fromtimestamp(self.started, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if running else None,
                "error": self.last_error,
            }


class Handler(SimpleHTTPRequestHandler):
    screener = None
    updater = None

    def translate_path(self, path):
        """/autonomy/... and /app/... map to those folders; everything else to app/ (the main simulation).
        The standard resolution (which drops '..' segments) runs inside the chosen folder."""
        for prefix, base in (("/autonomy/", AUTONOMY_DIR), ("/app/", APP_DIR)):
            if path.startswith(prefix):
                main_dir, self.directory = self.directory, str(base)
                try:
                    return super().translate_path("/" + path[len(prefix):])
                finally:
                    self.directory = main_dir
        return super().translate_path(path)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/autonomy":
            self.send_response(301)
            self.send_header("Location", "/autonomy/")
            self.end_headers()
            return
        if url.path not in ("/api/screen", "/api/datasets", "/api/maneuver", "/api/status"):
            return super().do_GET()
        query = parse_qs(url.query)
        try:
            if url.path == "/api/datasets":
                body = json.dumps(self.screener.datasets()).encode()
            elif url.path == "/api/status":
                body = json.dumps(self.updater.status() if self.updater else
                                  {"current": daily_update.is_current(), "updating": False, "disabled": True}).encode()
            elif url.path == "/api/maneuver":
                body = self.screener.plan_maneuver(query.get("dataset", [""])[0], query)
            else:
                body = self.screener.screen(query.get("dataset", [""])[0], query.get("start", [None])[0],
                                            query.get("hours", ["24"])[0])
            self._send(200, body)
        except ScreeningError as err:
            self._send(400, json.dumps({"error": str(err)}).encode())
        except Exception:  # keep serving after an unexpected failure, and say what happened
            traceback.print_exc()
            self._send(500, json.dumps({"error": "Screening failed; see the server log."}).encode())

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Always serve fresh data files after a re-run of the pipeline.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/status") and args and str(args[1]).startswith("2"):
            return   # polled every minute by each open page
        if "/api/" in self.path or (args and str(args[1])[:1] in "45"):
            super().log_message(fmt, *args)


def bind(port, tries=20):
    """First free port from `port` upwards (e.g. when the simulation is already running)."""
    for candidate in range(port, port + tries):
        try:
            with socket.socket() as s:
                # Same option the HTTP server uses: a port left in TIME_WAIT by a just-stopped server is free.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", candidate))
            return candidate
        except OSError:
            continue
    raise SystemExit(f"No free port between {port} and {port + tries - 1}; pass --port to choose one.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--no-daily-update", action="store_true",
                        help="do not refresh the live data when its screening does not start today")
    args = parser.parse_args()

    if not sc.OBJECTS_PATH.exists():
        raise SystemExit(f"{sc.OBJECTS_PATH.relative_to(ROOT)} is missing; run ./run.sh first.")
    port = bind(args.port)
    if port != args.port:
        print(f"Port {args.port} is already in use (is the simulation already running there?); using {port}.")
    telemetry.configure("simulation-server", system_metrics=True)
    Handler.screener = Screener(args.workers)
    Handler.updater = None if args.no_daily_update else DailyUpdater()
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(APP_DIR)))
    print(f"Simulation running at http://127.0.0.1:{port} (Ctrl+C to stop)", flush=True)
    # Treat `kill` like Ctrl+C so the screening workers are always shut down with the server.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.screener.close()


if __name__ == "__main__":
    main()
