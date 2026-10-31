"""Build the *modelled* (expected / future) overflight layer for Brussels Airport.

There is no free source of future flight tracks, so this script models them from
how the airport is operated:

* the runway layout (25R/07L, 25L/07R, 01/19),
* the preferential runway system: which runways are used for take-off and landing
  depending on wind and on weekday/weekend and day/night,
* simplified departure routes (SIDs) and straight-in final approaches,
* typical climb and descent gradients,
* a lateral spread that grows with distance, because real aircraft never fly
  exactly on the nominal line.

Everything is APPROXIMATE and tunable in the CONFIG section below. Once real
OpenSky data has been collected (see fetch_opensky.py) the "observed" layer is the
one to trust; the modelled layer is for "what should I expect here in general".

Output: data/modelled_<scenario>.json  (same format as observed data)
"""
import math
import os
from datetime import datetime, timezone

from grid import Grid, rasterize_path, km_per_deg

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data")
FIELD_ELEV = 56.0  # m

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

# Runways: (midpoint lat, lon, true heading of the lower-numbered direction, length km)
RUNWAYS = {
    "07L/25R": (50.8968, 4.4783, 245.0 - 180.0, 3.64),
    "07R/25L": (50.8890, 4.4996, 245.0 - 180.0, 3.21),
    "01/19":   (50.8985, 4.5262, 13.0, 2.99),
}

MOVEMENTS_PER_DAY = 290   # departures per day; arrivals assumed equal
NIGHT_SHARE = 0.07        # share of movements between 23:00 and 06:00

# Wind / operating configurations and the share of the year each is in use.
# Each config lists departure runway-directions and arrival runway-directions,
# with the share of departures / arrivals on each.
CONFIGS = {
    "west": {
        "label": "Westerly wind (most common)",
        "share": 0.68,
        "dep": {"25R": 1.0},
        "arr": {"25L": 0.75, "25R": 0.25},
    },
    "east": {
        "label": "Easterly wind",
        "share": 0.22,
        "dep": {"07R": 0.85, "07L": 0.15},
        "arr": {"01": 0.65, "07L": 0.35},
    },
    "weekend": {
        "label": "Weekend / night scheme",
        "share": 0.10,
        "dep": {"25R": 0.55, "19": 0.45},
        "arr": {"25L": 0.80, "25R": 0.20},
    },
}

# Departure route fans per take-off direction:
# (share, straight_km_before_turn, new_heading_deg)
# new_heading None = keep straight.
DEP_ROUTES = {
    "25R": [(0.45, 7.0, 200.0),   # left turn towards the south-west (canal / ring)
            (0.25, 0.0, None),    # straight out to the west
            (0.30, 4.5, 330.0)],  # right turn towards the north
    "25L": [(0.6, 6.0, 210.0), (0.4, 0.0, None)],
    "07R": [(0.40, 8.0, 20.0),    # left turn towards the north-east
            (0.40, 8.0, 130.0),   # right turn towards the south-east
            (0.20, 0.0, None)],
    "07L": [(0.5, 8.0, 20.0), (0.5, 0.0, None)],
    "19":  [(0.50, 6.0, 120.0),   # left turn towards the east
            (0.50, 6.0, 250.0)],  # right turn towards the west
    "01":  [(0.5, 6.0, 330.0), (0.5, 6.0, 60.0)],
}

CLIMB_M_PER_KM = 85.0     # ~5% average climb gradient for a loaded jet
GLIDE_M_PER_KM = 52.4     # 3 degree glide path
FINAL_LEN_KM = 28.0

# --------------------------------------------------------------------------- #


def dest(lat, lon, hdg, km):
    ky, kx = km_per_deg(lat)
    r = math.radians(hdg)
    return lat + km * math.cos(r) / ky, lon + km * math.sin(r) / kx


def thresholds(rwy_dir):
    """Return (threshold lat/lon, departure-end lat/lon, heading) for e.g. '25R'."""
    for name, (lat, lon, hdg_low, length) in RUNWAYS.items():
        a, b = name.split("/")
        if rwy_dir in (a, b):
            hdg = hdg_low if rwy_dir == a else (hdg_low + 180) % 360
            start = dest(lat, lon, hdg + 180, length / 2)
            end = dest(lat, lon, hdg, length / 2)
            return start, end, hdg
    raise KeyError(rwy_dir)


def departure_path(rwy_dir, straight_km, new_hdg, total_km=45.0):
    start, end, hdg = thresholds(rwy_dir)
    # lift-off at ~60% of the runway
    lat = start[0] + (end[0] - start[0]) * 0.6
    lon = start[1] + (end[1] - start[1]) * 0.6
    pts = [(lat, lon, FIELD_ELEV + 1)]
    d = 0.0
    step = 0.5
    cur = hdg
    while d < total_km:
        if new_hdg is not None and d >= straight_km:
            diff = (new_hdg - cur + 540) % 360 - 180
            if abs(diff) > 1:
                cur += max(-12, min(12, diff))  # ~ standard-rate-ish turn per 0.5 km
        lat, lon = dest(lat, lon, cur, step)
        d += step
        pts.append((lat, lon, FIELD_ELEV + CLIMB_M_PER_KM * d))
    return pts


def arrival_path(rwy_dir):
    thr, _, hdg = thresholds(rwy_dir)
    pts = []
    d = FINAL_LEN_KM
    while d >= 0:
        lat, lon = dest(thr[0], thr[1], hdg + 180, d)
        pts.append((lat, lon, FIELD_ELEV + 15 + GLIDE_M_PER_KM * d))
        d -= 0.5
    return pts


def dep_spread(d):
    return 0.15 + 0.07 * d


def arr_spread(d):
    # d is distance from the far end of the final (start of path); closer to the
    # runway the spread is small
    from_thr = max(0.0, FINAL_LEN_KM - d)
    return 0.08 + 0.03 * from_thr


def build_config(cfg):
    """Grid of flights/day for one operating configuration (100% of the time)."""
    g = Grid()
    for night in (False, True):
        frac = NIGHT_SHARE if night else 1 - NIGHT_SHARE
        for rwy, share in cfg["dep"].items():
            for r_share, straight, hdg in DEP_ROUTES[rwy]:
                w = MOVEMENTS_PER_DAY * frac * share * r_share
                rasterize_path([p + (None,) for p in departure_path(rwy, straight, hdg)],
                               g, w, night, spread_fn=dep_spread)
        for rwy, share in cfg["arr"].items():
            w = MOVEMENTS_PER_DAY * frac * share
            rasterize_path([p + (None,) for p in arrival_path(rwy)],
                           g, w, night, spread_fn=arr_spread)
    return g


def main():
    os.makedirs(OUT, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    year = Grid()
    for key, cfg in CONFIGS.items():
        g = build_config(cfg)
        n = g.to_json({"source": "modelled", "scenario": key, "label": cfg["label"],
                       "generated": now, "days": 1}, os.path.join(OUT, f"modelled_{key}.json"))
        print(f"{key}: {n} cells")
        year.merge(g, cfg["share"])
    n = year.to_json({"source": "modelled", "scenario": "year",
                      "label": "Typical year (all wind directions)",
                      "generated": now, "days": 1}, os.path.join(OUT, "modelled_year.json"))
    print(f"year: {n} cells")


if __name__ == "__main__":
    main()
