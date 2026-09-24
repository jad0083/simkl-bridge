"""Soak test: the real bridge process under sustained faults and list churn.

Runs `python -m simkl_bridge serve` against tests/support/fake_simkl.py for a
fixed duration, while:

  * Simkl misbehaves: 429s, 503s, dropped connections and slow responses on a
    share of requests, small pages, and access tokens that expire every
    ~40 seconds, so refresh runs constantly;
  * an editor keeps adding and removing titles on a TV and a movie list;
  * a poller keeps reading both feeds, as a user pressing Test would;
  * two stand-in apps behave as Sonarr and Radarr do: they list their import
    lists (including a disabled one), and fetch from the bridge whenever it
    requests a sync.

It fails, with a non-zero exit, if any invariant breaks:

  1. every 200 answer is exactly some real version of the list -- never
     partial, never mixed across an edit, never empty;
  2. every edit is delivered to the app within DELIVERY_BOUND seconds;
  3. the disabled list is never synced;
  4. memory and thread count don't grow past their limits;
  5. no traceback appears in the bridge's output.

    python tests/soak/soak.py --minutes 5
"""
import argparse
import json
import os
import pathlib
import random
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "support"))
import fake_simkl

TV, MOVIES = 501, 502
DELIVERY_BOUND = 240          # watch interval 30s + retries under a 15% fault rate
RSS_GROWTH_LIMIT_KB = 30_000
THREAD_LIMIT = 60


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method, url, body=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def item(kind, n):
    ids = {"simkl_id": n, "slug": f"{kind}{n}", "imdb": f"tt{n:07d}"}
    ids["tvdb" if kind == "tv" else "tmdb"] = str(10_000 + n)
    return {"title": f"{kind} {n}", "year": 2000, "type": "tv" if kind == "tv" else "movie", "ids": ids}


class Versions:
    """Every state each list has been in, with the time it began."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_list = {TV: [], MOVIES: []}

    def record(self, list_id, ids):
        with self.lock:
            self.by_list[list_id].append((time.time(), frozenset(ids)))

    def known(self, list_id, ids):
        with self.lock:
            return frozenset(ids) in {v for _, v in self.by_list[list_id]}

    def index_of(self, list_id, ids):
        with self.lock:
            for i in range(len(self.by_list[list_id]) - 1, -1, -1):
                if self.by_list[list_id][i][1] == frozenset(ids):
                    return i
        return -1


class FakeArr:
    """Enough of Sonarr/Radarr for the watcher: import lists, and sync-on-command."""

    def __init__(self, name, bridge, list_id, versions, failures):
        self.name, self.bridge, self.list_id = name, bridge, list_id
        self.versions, self.failures = versions, failures
        self.fetches = []                    # (time, version index) of each successful fetch
        self.disabled_synced = 0
        self.lock = threading.Lock()
        field = "baseUrl" if name == "sonarr" else "url"
        enabled = "enableAutomaticAdd" if name == "sonarr" else "enabled"
        self.lists = [
            {"id": 1, enabled: True, "fields": [{"name": field, "value": f"http://bridge/{name}/{list_id}"}]},
            {"id": 2, enabled: False, "fields": [{"name": field, "value": f"http://bridge/{name}/{list_id}"}]},
        ]

    def fetch(self):
        status, body = http("GET", f"{self.bridge}/{self.name}/{self.list_id}")
        if status != 200:
            return
        key = "tvdbId" if self.name == "sonarr" else "id"
        ids = {e[key] - 10_000 for e in body}
        idx = self.versions.index_of(self.list_id, ids)
        if idx < 0:
            self.failures.append(f"{self.name} fetched a list that never existed: {sorted(ids)}")
        with self.lock:
            self.fetches.append((time.time(), idx))

    def handler(self):
        arr = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get("X-Api-Key") != "soak-key":
                    return self._send(401, {"message": "Unauthorized"})
                if self.path == "/api/v3/importlist":
                    return self._send(200, arr.lists)
                return self._send(404, {})

            def do_POST(self):
                if self.headers.get("X-Api-Key") != "soak-key":
                    return self._send(401, {"message": "Unauthorized"})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/v3/command" and body.get("name") == "ImportListSync":
                    if body.get("definitionId") == 2:
                        arr.disabled_synced += 1
                    threading.Thread(target=arr.fetch, daemon=True).start()
                    return self._send(201, {"id": 1, "name": "ImportListSync"})
                return self._send(404, {})

        return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--fault-rate", type=float, default=0.15)
    ap.add_argument("--edit-every", type=float, default=20)
    a = ap.parse_args()
    random.seed(os.environ.get("SOAK_SEED", "simkl-bridge"))
    failures, versions = [], Versions()

    simkl_port, bridge_port = free_port(), free_port()
    srv, state = fake_simkl.serve(simkl_port, "soak-refresh", 86400 + 40, host="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    admin = f"http://127.0.0.1:{simkl_port}/_admin"
    bridge = f"http://127.0.0.1:{bridge_port}"

    lists = {TV: {1, 2, 3}, MOVIES: {1, 2}}
    http("POST", f"{admin}/list/{TV}", {"media_type": "tv", "items": [item("tv", n) for n in sorted(lists[TV])]})
    http("POST", f"{admin}/list/{MOVIES}", {"media_type": "movies",
                                            "items": [item("movie", n) for n in sorted(lists[MOVIES])]})
    for lid, ids in lists.items():
        versions.record(lid, ids)

    arrs = {}
    for name, lid in (("sonarr", TV), ("radarr", MOVIES)):
        arr = FakeArr(name, bridge, lid, versions, failures)
        port = free_port()
        s = ThreadingHTTPServer(("127.0.0.1", port), arr.handler())
        threading.Thread(target=s.serve_forever, daemon=True).start()
        arrs[name] = (arr, f"http://127.0.0.1:{port}")

    data = pathlib.Path(os.environ.get("SOAK_DATA", f"/tmp/simkl-bridge-soak-{os.getpid()}"))
    env = dict(os.environ, SIMKL_CLIENT_ID="soak-client", SIMKL_REFRESH_TOKEN="soak-refresh",
               SIMKL_API_BASE=f"http://127.0.0.1:{simkl_port}", BRIDGE_PORT=str(bridge_port),
               BRIDGE_DATA_DIR=str(data), BRIDGE_MIN_REFRESH="5", BRIDGE_WATCH_INTERVAL="30",
               BRIDGE_FULL_CHECK="120", PYTHONPATH=str(ROOT / "src"),
               SONARR_URL=arrs["sonarr"][1], SONARR_API_KEY="soak-key",
               RADARR_URL=arrs["radarr"][1], RADARR_API_KEY="soak-key")
    log_path = data.parent / f"soak-bridge-{os.getpid()}.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "simkl_bridge", "serve"], env=env,
                                stdout=log, stderr=subprocess.STDOUT)
    for _ in range(200):
        try:
            if http("GET", f"{bridge}/healthz", timeout=2)[0] == 200:
                break
        except OSError:
            time.sleep(0.05)
    http("POST", f"{admin}/faults", {"rate": a.fault_rate, "kinds": ["429", "503", "drop", "slow"],
                                     "page_max": 2})

    stop = threading.Event()
    edits = []                                   # (time, list id, version index)
    polls = {"whole": 0, "error": 0}

    def editor():
        n = 100
        while not stop.wait(a.edit_every * random.uniform(0.5, 1.5)):
            lid = random.choice([TV, MOVIES])
            kind = "tv" if lid == TV else "movie"
            if len(lists[lid]) > 1 and random.random() < 0.4:
                gone = random.choice(sorted(lists[lid]))
                lists[lid].discard(gone)
                versions.record(lid, lists[lid])
                http("POST", f"{admin}/list/{lid}/remove", {"simkl_id": gone})
            else:
                n += 1
                lists[lid].add(n)
                versions.record(lid, lists[lid])
                http("POST", f"{admin}/list/{lid}/add", item(kind, n))
            edits.append((time.time(), lid, versions.index_of(lid, lists[lid])))

    def poller():
        while not stop.is_set():
            for name, lid, key in (("sonarr", TV, "tvdbId"), ("radarr", MOVIES, "id")):
                status, body = http("GET", f"{bridge}/{name}/{lid}")
                if status == 200:
                    ids = {e[key] - 10_000 for e in body}
                    if not ids:
                        failures.append(f"empty list served for {lid}")
                    elif not versions.known(lid, ids):
                        failures.append(f"{name} {lid}: served a list that never existed ({len(ids)} ids)")
                    polls["whole"] += 1
                else:
                    if not (isinstance(body, dict) and body.get("error")):
                        failures.append(f"{name} {lid}: non-200 without an error body: {status} {body}")
                    polls["error"] += 1
            time.sleep(1)

    samples = []

    def sampler():
        while not stop.wait(10):
            try:
                st = pathlib.Path(f"/proc/{proc.pid}/status").read_text()
                rss = int(re.search(r"VmRSS:\s+(\d+)", st).group(1))
                threads = int(re.search(r"Threads:\s+(\d+)", st).group(1))
                samples.append((time.time(), rss, threads))
            except (OSError, AttributeError):
                pass

    workers = [threading.Thread(target=f, daemon=True) for f in (editor, poller, sampler)]
    for w in workers:
        w.start()
    started = time.time()
    time.sleep(a.minutes * 60)
    stop.set()
    # Let edits made near the end settle, without the faults that slow delivery.
    # Long enough for a sync whose fetch failed to be requested again: one watch
    # interval (30s) plus the watcher's REDELIVER_AFTER (120s), with margin.
    http("POST", f"{admin}/faults", {"rate": 0.0})
    time.sleep(210)
    proc.terminate()
    proc.wait(10)
    output = log_path.read_text()

    # 2. delivery: each edit must be followed by an app fetch of that version or a later one.
    latencies = []
    for t, lid, idx in edits:
        arr = arrs["sonarr" if lid == TV else "radarr"][0]
        after = [ft for ft, fidx in arr.fetches if ft >= t and fidx >= idx]
        if not after:
            failures.append(f"edit at +{t - started:.0f}s on list {lid} was never delivered")
            continue
        latencies.append(min(after) - t)
        if latencies[-1] > DELIVERY_BOUND:
            failures.append(f"edit at +{t - started:.0f}s on list {lid} took {latencies[-1]:.0f}s")
    # 3. disabled definitions
    for name, (arr, _) in arrs.items():
        if arr.disabled_synced:
            failures.append(f"{name}: disabled list synced {arr.disabled_synced} times")
    # 4. resources: compare the last quarter against the first after warm-up
    if len(samples) >= 8:
        q = len(samples) // 4
        early = statistics.median(s[1] for s in samples[1:q + 1])
        late = statistics.median(s[1] for s in samples[-q:])
        if late - early > RSS_GROWTH_LIMIT_KB:
            failures.append(f"RSS grew {late - early} kB ({early} -> {late})")
        if max(s[2] for s in samples) > THREAD_LIMIT:
            failures.append(f"thread count reached {max(s[2] for s in samples)}")
    # 5. crashes
    if "Traceback" in output:
        failures.append("traceback in bridge output")

    report = {
        "minutes": a.minutes, "fault_rate": a.fault_rate, "edits": len(edits),
        "delivered": len(latencies),
        "latency_s": {"p50": round(statistics.median(latencies), 1) if latencies else None,
                      "max": round(max(latencies), 1) if latencies else None},
        "polls": polls, "simkl": {k: state.stats[k] for k in ("requests", "faults", "refreshes", "unauthorised")},
        "rss_kb": {"first": samples[0][1] if samples else None, "last": samples[-1][1] if samples else None},
        "threads_max": max((s[2] for s in samples), default=None),
        "watch_log_lines": sum(1 for line in output.splitlines() if line.startswith("watch:")),
        "redeliveries": sum(1 for line in output.splitlines() if "asking again" in line),
        "failures": failures[:50],
    }
    print(json.dumps(report, indent=2))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
