"""Collect real Brussels Airport (EBBR) flight tracks from the OpenSky Network
and turn them into daily overflight grids.

Runs once a day in GitHub Actions (see .github/workflows/update-data.yml), or by
hand:  OPENSKY_CLIENT_ID=... OPENSKY_CLIENT_SECRET=... python3 scripts/fetch_opensky.py

What it does
1. Asks OpenSky for every departure from and arrival at EBBR on each day that is
   not yet in data/daily/ (OpenSky publishes flight lists the next night, so the
   newest day processed is yesterday; tracks are only kept for ~30 days, so we
   backfill at most that far).
2. Downloads the track of each flight, keeps the part below 3,000 m inside the
   map area, and counts each flight once per ~500 m cell, split by altitude band
   and day/night.
3. Writes data/daily/<date>.json, then rebuilds the rolling summaries
   data/observed_30.json, observed_90.json and observed_365.json (average flights
   per day), plus data/stats.json.

OpenSky data is free for non-commercial use; see https://opensky-network.org/about/terms-of-use
Requests are rate limited with daily credits, so the script stops cleanly when it
runs out and picks up the remaining flights on the next run.
"""
import glob
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from grid import Grid, rasterize_path, is_night, LAT0, LAT1, LON0, LON1

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
DAILY = os.path.join(DATA, "daily")
PENDING = os.path.join(DATA, "pending.json")

API = "https://opensky-network.org/api"
TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
             "/protocol/openid-connect/token")
AIRPORT = "EBBR"
MAX_BACKFILL_DAYS = int(os.environ.get("MAX_BACKFILL_DAYS", "25"))
MAX_TRACKS = int(os.environ.get("MAX_TRACKS", "1500"))   # per run, protects credits
MAX_RUNTIME_S = int(os.environ.get("MAX_RUNTIME_S", str(5 * 3600)))
UA = "belgium-flight-heatmap (non-commercial; github.com)"


class OutOfCredits(Exception):
    pass


class Client:
    def __init__(self):
        self.cid = os.environ.get("OPENSKY_CLIENT_ID")
        self.secret = os.environ.get("OPENSKY_CLIENT_SECRET")
        self.token = None
        self.token_exp = 0

    def _auth(self):
        if not self.cid:
            return {}
        if time.time() > self.token_exp - 60:
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self.cid,
                "client_secret": self.secret,
            }).encode()
            req = urllib.request.Request(TOKEN_URL, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                tok = json.load(r)
            self.token = tok["access_token"]
            self.token_exp = time.time() + tok.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path, **params):
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(4):
            req = urllib.request.Request(url, headers={"User-Agent": UA, **self._auth()})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 429:
                    raise OutOfCredits(e.headers.get("X-Rate-Limit-Retry-After-Seconds", "?"))
                if e.code in (500, 502, 503, 504):
                    time.sleep(2 ** attempt * 3)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** attempt * 3)
        return None


def day_bounds(d):
    start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return start, start + 86400


def load_pending():
    if os.path.exists(PENDING):
        with open(PENDING) as f:
            return json.load(f)
    return {}


def save_pending(p):
    with open(PENDING, "w") as f:
        json.dump(p, f)


def flight_list(client, d):
    begin, end = day_bounds(d)
    flights = []
    for kind in ("departure", "arrival"):
        res = client.get(f"/flights/{kind}", airport=AIRPORT, begin=begin, end=end) or []
        for fl in res:
            t = fl.get("firstSeen") if kind == "departure" else fl.get("lastSeen")
            flights.append({"icao24": fl["icao24"], "t": t, "kind": kind,
                            "callsign": (fl.get("callsign") or "").strip()})
    return flights


def track_points(trk):
    pts = []
    for p in trk.get("path") or []:
        t, lat, lon, alt, _hdg, on_ground = p[:6]
        if lat is None or lon is None:
            continue
        if not (LAT0 <= lat <= LAT1 and LON0 <= lon <= LON1):
            continue
        if on_ground:
            continue
        pts.append((lat, lon, alt, t))
    return pts


def runway_guess(pts, kind):
    """Very rough: which way the aircraft was heading near the airport."""
    if len(pts) < 2:
        return None
    a, b = (pts[0], pts[min(3, len(pts) - 1)]) if kind == "departure" else (pts[max(-4, -len(pts))], pts[-1])
    dlat, dlon = b[0] - a[0], (b[1] - a[1]) * 0.63
    hdg = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360
    if 200 <= hdg < 290:
        return "25"
    if 20 <= hdg < 110:
        return "07"
    if hdg >= 330 or hdg < 20:
        return "01"
    if 160 <= hdg < 200:
        return "19"
    return None


def process_day(client, d, pending, deadline, budget):
    key = d.isoformat()
    state = pending.get(key)
    if state is None:
        flights = flight_list(client, d)
        state = {"flights": flights, "done": 0, "cells": {}, "stats": {}}
        pending[key] = state
    g = Grid()
    for k, v in state["cells"].items():
        i, j = map(int, k.split(","))
        g.cells[(i, j)] = v
    stats = state["stats"]
    flights = state["flights"]
    try:
        while state["done"] < len(flights):
            if time.time() > deadline or budget[0] <= 0:
                break
            fl = flights[state["done"]]
            trk = client.get("/tracks/all", icao24=fl["icao24"], time=fl["t"])
            budget[0] -= 1
            state["done"] += 1
            if not trk:
                stats["missing"] = stats.get("missing", 0) + 1
                continue
            pts = track_points(trk)
            if len(pts) < 2:
                stats["missing"] = stats.get("missing", 0) + 1
                continue
            rasterize_path(pts, g, 1.0, is_night)
            night = is_night(fl["t"])
            stats[fl["kind"]] = stats.get(fl["kind"], 0) + 1
            if night:
                stats["night_" + fl["kind"]] = stats.get("night_" + fl["kind"], 0) + 1
            rw = runway_guess(pts, fl["kind"])
            if rw:
                rk = f"{fl['kind'][:3]}_{rw}"
                stats[rk] = stats.get(rk, 0) + 1
    finally:
        state["cells"] = {f"{i},{j}": v for (i, j), v in g.cells.items()}
    if state["done"] >= len(flights):
        os.makedirs(DAILY, exist_ok=True)
        g.to_json({"source": "opensky", "date": key, "days": 1, "stats": stats,
                   "flights": len(flights)}, os.path.join(DAILY, f"{key}.json"), min_value=0)
        del pending[key]
        print(f"{key}: complete, {len(flights)} flights, {len(g.cells)} cells")
        return True
    print(f"{key}: {state['done']}/{len(flights)} tracks so far, continuing next run")
    return False


def rebuild_summaries():
    files = sorted(glob.glob(os.path.join(DAILY, "*.json")))
    today = date.today()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    per_day = []
    for window in (30, 90, 365):
        g = Grid()
        n = 0
        first = None
        for f in files:
            d = date.fromisoformat(os.path.basename(f)[:10])
            if (today - d).days > window:
                continue
            dg, meta = Grid.from_json(f)
            g.merge(dg)
            n += 1
            first = first or d.isoformat()
            if window == 365:
                per_day.append({"date": d.isoformat(), **meta.get("stats", {}),
                                "flights": meta.get("flights")})
        if n == 0:
            continue
        g.scaled(1.0 / n).to_json(
            {"source": "opensky", "scenario": f"observed_{window}",
             "label": f"Observed, last {window} days", "days": n,
             "from": first, "to": os.path.basename(files[-1])[:10], "generated": now},
            os.path.join(DATA, f"observed_{window}.json"))
    with open(os.path.join(DATA, "stats.json"), "w") as f:
        json.dump({"generated": now, "days": per_day}, f, separators=(",", ":"))
    # drop daily files older than a year to keep the repo small
    for f in files:
        d = date.fromisoformat(os.path.basename(f)[:10])
        if (today - d).days > 400:
            os.remove(f)


def main():
    client = Client()
    if not client.cid:
        print("WARNING: no OPENSKY_CLIENT_ID set; anonymous access has very low limits "
              "and no flight/track history.", file=sys.stderr)
    deadline = time.time() + MAX_RUNTIME_S
    budget = [MAX_TRACKS]
    pending = load_pending()
    yesterday = date.today() - timedelta(days=1)
    todo = sorted(set(pending) | {
        (yesterday - timedelta(days=k)).isoformat() for k in range(MAX_BACKFILL_DAYS)
        if not os.path.exists(os.path.join(DAILY, f"{(yesterday - timedelta(days=k)).isoformat()}.json"))
    }, reverse=True)
    try:
        for key in todo:
            if time.time() > deadline or budget[0] <= 0:
                break
            if not process_day(client, date.fromisoformat(key), pending, deadline, budget):
                break
    except OutOfCredits as e:
        print(f"OpenSky credits used up for today (retry after {e}s); saving progress.")
    finally:
        # forget pending days that are too old to have tracks
        cutoff = (yesterday - timedelta(days=29)).isoformat()
        for k in [k for k in pending if k < cutoff]:
            del pending[k]
        save_pending(pending)
        rebuild_summaries()


if __name__ == "__main__":
    main()
