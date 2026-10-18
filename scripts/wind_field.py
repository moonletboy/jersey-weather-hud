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
   It also downloads the 10 m wind (east-west and north-south parts) so it can
   work out the average (sustained) wind speed and the direction it blows from.
3. It cuts them down to the Storm Tracker's map window (Ireland and the Western
   Approaches across to the Low Countries, Biscay up to southern Norway) and
   writes data/wind-field.json. Since October 2026 it uses NOAA's finer
   0.25 degree files (about 15 miles between points) instead of 0.5 degree,
   because the map concentrates on this smaller area.
   To keep the file small every number is written as TWO characters
   (0-9 then a-z, like a base-36 number: "00" = 0, "0a" = 10, "1k" = 56):
     gust  = wind gust in mph
     wspd  = average wind speed in mph
     mslp  = (pressure in hPa - 900) x 2     (so 1000 hPa is stored as 200)
     wdir  = direction the wind blows FROM, ONE character per grid point in
             steps of 10 degrees ("0" = north, "9" = east, "i" = south,
             "r" = west)
   The file says enc = "b36x2" so index.html knows to decode it; the page can
   still read the older plain-number files too.
4. index.html reads that file and paints it on the map.

It is run automatically by .github/workflows/wind-field.yml every 6 hours,
about 20 minutes after the storm tracks job. The workflow publishes the file on
the separate "data" branch (one commit, replaced each run) so the main branch
does not grow four times a day. If NOAA is not available it leaves the old
file untouched.
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

# The area saved (degrees) - the Storm Tracker's map window. index.html locks
# its map to the same box, so these four numbers and ST_WINDOW in index.html
# must agree.
LAT_MIN, LAT_MAX = 43.0, 60.0
LON_MIN, LON_MAX = -16.0, 12.0
RESOLUTION = "0p25"             # NOAA file set: "0p25" (15 mi) or "0p50" (30 mi)
MS_TO_MPH = 2.236936
MSLP_BASE = 900.0
MSLP_SCALE = 2.0
MIN_HOURS_NEEDED = 30           # of the 41 pressure maps; fewer = give up


def log(msg):
    print(msg, flush=True)


def object_key(cycle, fh, suffix=""):
    """Same as storm_tracks.object_key but for the file set chosen above. The storm
    tracks keep using 0.5 degree (fine for finding centres over the whole Atlantic);
    this script swaps the helper in for its own downloads only."""
    return "gfs.%s/%s/atmos/gfs.t%sz.pgrb2.%s.f%03d%s" % (
        cycle.strftime("%Y%m%d"), cycle.strftime("%H"), cycle.strftime("%H"), RESOLUTION, fh, suffix)


st.object_key = object_key   # download_message / cycle_is_ready now use the finer files


def crop(lats, lons, arr):
    """Cut a decoded map (latitude and longitude both ascending) to the saved area."""
    li = np.where((lats >= LAT_MIN - 1e-6) & (lats <= LAT_MAX + 1e-6))[0]
    lj = np.where((lons >= LON_MIN - 1e-6) & (lons <= LON_MAX + 1e-6))[0]
    return lats[li], lons[lj], arr[np.ix_(li, lj)]


DIR_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"
B36 = np.array(list(DIR_CHARS))


def encode_b36x2(ints):
    """Whole numbers 0-1295 as two base-36 characters each, joined into one string."""
    v = np.clip(np.asarray(ints, dtype=int), 0, 1295).ravel()
    hi, lo = v // 36, v % 36
    return "".join(np.char.add(B36[hi], B36[lo]).tolist())


def encode_dir(u, v):
    """Wind direction (the way it blows FROM, degrees) in 10-degree steps, as a text string."""
    deg = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    steps = np.rint(deg / 10.0).astype(int) % 36
    return "".join(DIR_CHARS[k] for k in steps.ravel())


def encode(arr, kind):
    if kind in ("wspd", "gust"):
        v = np.clip(np.rint(arr * MS_TO_MPH), 0, 250)
    else:
        v = np.clip(np.rint((arr - MSLP_BASE) * MSLP_SCALE), 0, 500)
    return encode_b36x2(v)


def build(cycle, hours):
    def fetch(job):
        fh, kind = job
        try:
            if kind == "mslp":
                return job, st.download_message(st.make_session(), cycle, fh, "PRMSL", "mean sea level")
            if kind == "u":
                return job, st.download_message(st.make_session(), cycle, fh, "UGRD", "10 m above ground")
            if kind == "v":
                return job, st.download_message(st.make_session(), cycle, fh, "VGRD", "10 m above ground")
            return job, st.download_message(st.make_session(), cycle, fh, "GUST", "surface")
        except Exception as e:                       # noqa: BLE001 - report and carry on
            log("  f%03d %s: download failed (%s)" % (fh, kind, e))
            return job, None

    jobs = [(fh, k) for fh in hours for k in ("mslp", "gust", "u", "v")]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        blobs = dict(pool.map(fetch, jobs))

    grid = None
    out = {"mslp": {}, "gust": {}, "u": {}, "v": {}}
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


def simulate(cycle, hours):
    """A made-up deep low crossing the window, for testing without NOAA."""
    step = 0.25 if RESOLUTION == "0p25" else 0.5
    lats = np.arange(LAT_MIN, LAT_MAX + 1e-6, step)
    lons = np.arange(LON_MIN, LON_MAX + 1e-6, step)
    LA, LO = np.meshgrid(lats, lons, indexing="ij")
    out = {"mslp": {}, "gust": {}, "u": {}, "v": {}}
    for h in hours:
        f = h / float(hours[-1])
        clat, clon = 47.0 + 12.0 * f, -16.0 + 30.0 * f
        depth = 36.0 * np.exp(-((f - 0.45) / 0.2) ** 2)
        r2 = ((LA - clat) / 3.0) ** 2 + ((LO - clon) / 5.0) ** 2
        p = 1016.0 - depth * np.exp(-r2) + 2.0 * np.sin(LA / 4.0)
        gy, gx = np.gradient(p, step, step)
        u, v = -gy * 2.2, gx * 2.2 * np.cos(np.radians(LA))
        spd = np.hypot(u, v)
        out["mslp"][h], out["u"][h], out["v"][h] = p, u, v
        out["gust"][h] = spd * 1.4 + 2.0
    return (lats, lons), out


def main():
    ap = argparse.ArgumentParser(description="Build data/wind-field.json from NOAA GFS")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "wind-field.json"))
    ap.add_argument("--cycle", help="force a run, e.g. 2026100812 (default: newest complete run)")
    ap.add_argument("--simulate", action="store_true", help="no NOAA: write a made-up storm for testing the page")
    args = ap.parse_args()

    session = st.make_session()
    now = st.now_utc()
    if args.cycle:
        cycle = dt.datetime.strptime(args.cycle, "%Y%m%d%H")
    elif args.simulate:
        cycle = st.candidate_cycles(now)[1]
    else:
        cycle = st.choose_cycle(session, now)
    log("Using GFS run %s" % cycle.strftime("%Y-%m-%d %HZ"))

    hours = list(st.FORECAST_HOURS)
    grid, data = simulate(cycle, hours) if args.simulate else build(cycle, hours)
    got = len(data["mslp"])
    log("Got %d pressure maps and %d gust maps" % (got, len(data["gust"])))
    if grid is None or got < MIN_HOURS_NEEDED:
        log("Too few maps - leaving the existing file untouched.")
        return 1

    # Average wind speed and direction from the two wind components
    wspd, wdir = {}, {}
    for h in hours:
        if h in data["u"] and h in data["v"]:
            wspd[h] = np.hypot(data["u"][h], data["v"][h])
            wdir[h] = encode_dir(data["u"][h], data["v"][h])
    log("Got %d wind-speed maps" % len(wspd))

    # Gust is not always published for the very first hour: borrow the next one
    if hours[0] not in data["gust"] and hours[1] in data["gust"]:
        data["gust"][hours[0]] = data["gust"][hours[1]]
        log("No gust map for +0h - using +%dh instead" % hours[1])

    lats, lons = grid
    result = {
        "generated": st.iso(now),
        "cycle": st.iso(cycle),
        "model": "NOAA GFS %s degree" % ("0.25" if RESOLUTION == "0p25" else "0.5"),
        "enc": "b36x2",
        "step_hours": st.STEP_HOURS,
        "hours": hours,
        "lat0": round(float(lats[0]), 3), "lat1": round(float(lats[-1]), 3),
        "lon0": round(float(lons[0]), 3), "lon1": round(float(lons[-1]), 3),
        "nlat": int(len(lats)), "nlon": int(len(lons)),
        "mslp_base": MSLP_BASE, "mslp_scale": MSLP_SCALE,
        "gust": [encode(data["gust"][h], "gust") if h in data["gust"] else None for h in hours],
        "mslp": [encode(data["mslp"][h], "mslp") if h in data["mslp"] else None for h in hours],
        "wspd": [encode(wspd[h], "wspd") if h in wspd else None for h in hours],
        "wdir": [wdir[h] if h in wdir else None for h in hours],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    log("Wrote %s (%d bytes, grid %d x %d)" % (out, out.stat().st_size, len(lats), len(lons)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
