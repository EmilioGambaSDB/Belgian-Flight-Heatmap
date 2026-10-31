"""Shared grid definition and helpers for the flight exposure heatmap.

The map area around Brussels Airport is cut into cells of roughly 500 m x 500 m.
For every cell we store the average number of flights per day that passed over it,
split by altitude band and by day/night:

    [i, j, day_low, day_mid, day_high, night_low, night_mid, night_high]

Altitude bands (above mean sea level; the airport is at ~56 m):
    low  : below 600 m   (~2,000 ft)  loud
    mid  : 600 - 1,500 m (~2,000-5,000 ft)
    high : 1,500 - 3,000 m (~5,000-10,000 ft)  usually faint
Anything above 3,000 m is ignored.

Night = 23:00-06:00 Brussels local time (the airport's night regime).
"""
import json
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LAT0, LAT1 = 50.55, 51.30
LON0, LON1 = 3.90, 5.15
DLAT = 0.0045   # ~500 m
DLON = 0.0071   # ~500 m at this latitude
NROWS = int(round((LAT1 - LAT0) / DLAT))
NCOLS = int(round((LON1 - LON0) / DLON))

BANDS = (600.0, 1500.0, 3000.0)
TZ = ZoneInfo("Europe/Brussels")

EBBR = (50.9014, 4.4844)


def cell_of(lat, lon):
    i = int((lat - LAT0) / DLAT)
    j = int((lon - LON0) / DLON)
    if 0 <= i < NROWS and 0 <= j < NCOLS:
        return i, j
    return None


def band_of(alt_m):
    if alt_m is None:
        return None
    for b, top in enumerate(BANDS):
        if alt_m < top:
            return b
    return None


def is_night(ts):
    h = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ).hour
    return h >= 23 or h < 6


def km_per_deg(lat):
    return 111.32, 111.32 * math.cos(math.radians(lat))


class Grid:
    """Sparse accumulator: (i, j) -> 6 floats."""

    def __init__(self):
        self.cells = {}

    def add(self, i, j, slot, w):
        v = self.cells.get((i, j))
        if v is None:
            v = self.cells[(i, j)] = [0.0] * 6
        v[slot] += w

    def merge(self, other, scale=1.0):
        for k, vals in other.cells.items():
            v = self.cells.setdefault(k, [0.0] * 6)
            for s in range(6):
                v[s] += vals[s] * scale

    def scaled(self, f):
        g = Grid()
        for k, vals in self.cells.items():
            g.cells[k] = [x * f for x in vals]
        return g

    def to_json(self, meta, path, min_value=0.01):
        rows = []
        for (i, j), v in sorted(self.cells.items()):
            if sum(v) < min_value:
                continue
            rows.append([i, j] + [round(x, 2) for x in v])
        out = {
            "meta": dict(meta, lat0=LAT0, lon0=LON0, dlat=DLAT, dlon=DLON,
                         nrows=NROWS, ncols=NCOLS,
                         columns=["i", "j", "day_low", "day_mid", "day_high",
                                  "night_low", "night_mid", "night_high"]),
            "cells": rows,
        }
        with open(path, "w") as f:
            json.dump(out, f, separators=(",", ":"))
        return len(rows)

    @classmethod
    def from_json(cls, path):
        g = cls()
        with open(path) as f:
            data = json.load(f)
        for row in data["cells"]:
            g.cells[(row[0], row[1])] = list(row[2:8])
        return g, data["meta"]


def rasterize_path(points, grid, weight, night_fn, spread_fn=None, step_km=0.15):
    """Add one path to the grid, counting each cell at most once per path.

    points: list of (lat, lon, alt_m, ts_or_None)
    night_fn: callable(ts) -> bool, or a constant bool
    spread_fn: optional callable(dist_along_km) -> sigma_km for lateral spread
               (used by the modelled routes to mimic track dispersion).
    """
    seen = {}  # cell -> (lowest band, night, weight)
    along = 0.0
    for a, b in zip(points, points[1:]):
        ky, kx = km_per_deg((a[0] + b[0]) / 2)
        dy = (b[0] - a[0]) * ky
        dx = (b[1] - a[1]) * kx
        seg = math.hypot(dx, dy)
        n = max(1, int(seg / step_km))
        for s in range(n):
            t = s / n
            lat = a[0] + (b[0] - a[0]) * t
            lon = a[1] + (b[1] - a[1]) * t
            alt = None if a[2] is None or b[2] is None else a[2] + (b[2] - a[2]) * t
            ts = a[3] if a[3] is not None else None
            band = band_of(alt)
            if band is None:
                continue
            night = night_fn(ts) if callable(night_fn) else night_fn
            if spread_fn is None:
                c = cell_of(lat, lon)
                if c is not None:
                    _mark(seen, c, band, night, 1.0)
            else:
                sigma = spread_fn(along + seg * t)
                # perpendicular unit vector (in km space)
                if seg > 0:
                    px, py = -dy / seg, dx / seg
                else:
                    px, py = 0.0, 0.0
                # Probability that a flight of this route is over a given 500 m cell.
                peak = min(1.0, 0.5 / (sigma * 2.5066))
                nsteps = max(1, int(2.0 * sigma / 0.25))
                for k in range(-nsteps, nsteps + 1):
                    off_km = k * 2.0 * sigma / nsteps
                    w = peak * math.exp(-0.5 * (off_km / sigma) ** 2)
                    olat = lat + (py * off_km) / ky
                    olon = lon + (px * off_km) / kx
                    c = cell_of(olat, olon)
                    if c is not None:
                        _mark(seen, c, band, night, w)
        along += seg
    for (i, j), (band, night, w) in seen.items():
        grid.add(i, j, band + (3 if night else 0), w * weight)


def _mark(seen, cell, band, night, w):
    """Count a flight once per cell, at the lowest (loudest) altitude it had there."""
    prev = seen.get(cell)
    if prev is None:
        seen[cell] = (band, night, w)
    else:
        seen[cell] = (min(prev[0], band), prev[1], max(prev[2], w))
