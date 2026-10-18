#!/usr/bin/env python3
"""
Wind-field map data for the Jersey Weather Chat HUD.

What this does, in plain English
--------------------------------
The Storm Tracker map shows a smooth, colour-shaded picture of the forecast
wind gusts with pressure lines (isobars) over it, underneath the storm tracks.
This script builds the data for that picture.

1. It finds the newest COMPLETE NOAA GFS forecast run (the same one the storm
   tracks use - it borrows the helper functions from storm_tracks.py).
2. For every 6 hours out to +240 hours it downloads two maps from NOAA's free
   archive: mean-sea-level pressure and surface wind gust.
3. It cuts them down to the area around the British Isles / Biscay / North
   Atlantic and writes data/wind-field.json as small whole numbers:
     gust  = wind gust in mph
     mslp  = (pressure in hPa - 900) x 2     (so 1000 hPa is stored as 200)
4. index.html reads that file and paints it on the map.

It is run automatically by .github/workflows/wind-field.yml every 6 hours,
about 20 minutes after the storm tracks job. If NOAA is not available it leaves
the old file untouched.
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import storm_tracks as st   # noqa: E402  (re-uses its download and decoding helpers)

# The area saved (degrees). Covers the whole 500-mile circle around Jersey.
LAT_MIN, LAT_MAX = 43.0, 59.0
LON_MIN, LON_MAX = -20.0, 6.0
MS_TO_MPH = 2.236936
MSLP_BASE = 900.0
MSLP_SCALE = 2.0
MIN_HOURS_NEEDED = 30           # of the 41 pressure maps; fewer = give up


def log(msg):
    print(msg, flush=True)


def crop(lats, lons, arr):
    """Cut a decoded map (latitude and longitude both ascending) to the saved area."""
    li = np.where((lats >= LAT_MIN - 1e-6) & (lats <= LAT_MAX + 1e-6))[0]
    lj = np.where((lons >= LON_MIN - 1e-6) & (lons <= LON_MAX + 1e-6))[0]
    return lats[li], lons[lj], arr[np.ix_(li, lj)]


def encode(arr, kind):
    if kind == "gust":
        v = np.rint(arr * MS_TO_MPH)
        v = np.clip(v, 0, 250)
    else:
        v = np.rint((arr - MSLP_BASE) * MSLP_SCALE)
        v = np.clip(v, 0, 500)
    return v.astype(int).ravel().tolist()


def build(cycle, hours):
    def fetch(job):
        fh, kind = job
        try:
            if kind == "mslp":
                return job, st.download_message(st.make_session(), cycle, fh, "PRMSL", "mean sea level")
            return job, st.download_message(st.make_session(), cycle, fh, "GUST", "surface")
        except Exception as e:                       # noqa: BLE001 - report and carry on
            log("  f%03d %s: download failed (%s)" % (fh, kind, e))
            return job, None

    jobs = [(fh, k) for fh in hours for k in ("mslp", "gust")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        blobs = dict(pool.map(fetch, jobs))

    grid = None
    out = {"mslp": {}, "gust": {}}
    for (fh, kind), blob in sorted(blobs.items()):
        if blob is None:
            continue
        try:
            if kind == "mslp":
                lats, lons, a = st.decode_pressure(blob)
                a = a                                   # already hPa
            else:
                lats, lons, a = st.decode_field(blob)   # m/s
            lats, lons, a = crop(lats, lons, a)
        except Exception as e:                       # noqa: BLE001
            log("  f%03d %s: could not read (%s)" % (fh, kind, e))
            continue
        if grid is None:
            grid = (lats, lons)
        elif a.shape != (len(grid[0]), len(grid[1])):
            log("  f%03d %s: unexpected grid size, skipped" % (fh, kind))
            continue
        out[kind][fh] = a
    return grid, out


def main():
    ap = argparse.ArgumentParser(description="Build data/wind-field.json from NOAA GFS")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "wind-field.json"))
    ap.add_argument("--cycle", help="force a run, e.g. 2026100812 (default: newest complete run)")
    args = ap.parse_args()

    session = st.make_session()
    now = st.now_utc()
    cycle = dt.datetime.strptime(args.cycle, "%Y%m%d%H") if args.cycle else st.choose_cycle(session, now)
    log("Using GFS run %s" % cycle.strftime("%Y-%m-%d %HZ"))

    hours = list(st.FORECAST_HOURS)
    grid, data = build(cycle, hours)
    got = len(data["mslp"])
    log("Got %d pressure maps and %d gust maps" % (got, len(data["gust"])))
    if grid is None or got < MIN_HOURS_NEEDED:
        log("Too few maps - leaving the existing file untouched.")
        return 1

    # Gust is not always published for the very first hour: borrow the next one
    if hours[0] not in data["gust"] and hours[1] in data["gust"]:
        data["gust"][hours[0]] = data["gust"][hours[1]]
        log("No gust map for +0h - using +%dh instead" % hours[1])

    lats, lons = grid
    result = {
        "generated": st.iso(now),
        "cycle": st.iso(cycle),
        "model": "NOAA GFS 0.5 degree",
        "step_hours": st.STEP_HOURS,
        "hours": hours,
        "lat0": round(float(lats[0]), 3), "lat1": round(float(lats[-1]), 3),
        "lon0": round(float(lons[0]), 3), "lon1": round(float(lons[-1]), 3),
        "nlat": int(len(lats)), "nlon": int(len(lons)),
        "mslp_base": MSLP_BASE, "mslp_scale": MSLP_SCALE,
        "gust": [encode(data["gust"][h], "gust") if h in data["gust"] else None for h in hours],
        "mslp": [encode(data["mslp"][h], "mslp") if h in data["mslp"] else None for h in hours],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    log("Wrote %s (%d bytes, grid %d x %d)" % (out, out.stat().st_size, len(lats), len(lons)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
