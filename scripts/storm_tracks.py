#!/usr/bin/env python3
"""
Storm-centre tracker for the Jersey Weather Chat HUD.

What this does, in plain English
--------------------------------
1. Finds the newest COMPLETE NOAA GFS forecast run on NOAA's free public data
   archive (Amazon "noaa-gfs-bdp-pds" bucket - no account or key needed).
2. Downloads ONLY the mean-sea-level-pressure map for every 6 hours out to
   +240 hours (it uses NOAA's .idx index file to grab just that one field, so
   each download is a few hundred KB instead of a whole multi-hundred-MB file).
3. Finds the centre of every low-pressure system in each map, then links the
   centres together through time into storm tracks.
4. Writes data/storm-tracks.json, which index.html draws on the Storm Watch map
   (a marker every 12 hours, colour-coded by pressure, labelled with the
   forecast minimum pressure at that time).

It is run automatically by .github/workflows/storm-tracks.yml every 6 hours.

How a "centre" is defined
-------------------------
The pressure map is smoothed slightly (about 1 degree) and each local minimum
that is at least 2 hPa lower than the average of a ring 4 degrees around it is a
storm centre. The reported pressure is the lowest raw value within about 1
degree of the centre. Lows over the Greenland ice cap are ignored because the
"sea-level" pressure there is just a calculation artefact.

Optional settings (only used for testing - normally leave alone)
----------------------------------------------------------------
  STORM_BUCKET_URL  use a different archive address
  STORM_NOW         pretend "now" is this UTC time, e.g. 2026-10-08T21:10:00Z
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import requests
from scipy.ndimage import gaussian_filter, map_coordinates, minimum_filter

import eccodes

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
BUCKET = os.environ.get("STORM_BUCKET_URL", "https://noaa-gfs-bdp-pds.s3.amazonaws.com").rstrip("/")

STEP_HOURS = 6                                   # model maps used for tracking
FORECAST_HOURS = list(range(0, 241, STEP_HOURS)) # +0h ... +240h
LAST_HOUR = FORECAST_HOURS[-1]
OUTPUT_EVERY_HOURS = 12                          # markers on the map
MIN_FIELDS_NEEDED = 30                           # of the 41 maps; fewer = give up

JERSEY = (49.1805, -2.1058)

# Search window (degrees)
LAT_MIN, LAT_MAX = 30.0, 75.0
LON_MIN, LON_MAX = -80.0, 40.0
EDGE_DEG = 3.0              # ignore centres this close to the window edge

# Finding centres
SMOOTH_SIGMA = 2.0          # grid points (0.5 deg each) => ~1 degree smoothing
MIN_WINDOW = 15             # a centre must be the lowest within +/- 3.5 degrees
MAX_CENTRE_HPA = 1010.0     # ignore centres higher than this
MIN_DEPTH_HPA = 2.0         # must be this much lower than the ring around it
RING_DEG = 4.0              # radius of that ring

# Linking centres into tracks
GATE_KM_PREDICTED = 450.0   # how far from the predicted position a match may be
GATE_KM_FRESH = 650.0       # same, for a track with only one point so far
MAX_GAP_STEPS = 1           # a track may "miss" one 6-hour map and carry on

# Which tracks to keep
KEEP_MAX_HPA = 1004.0       # must get at least this low at some point
MIN_TRACK_POINTS = 5        # at least 5 maps in a row-ish (~24 hours)
MAX_TRACKS = 16

# Rough outline of the Greenland ice cap (lon, lat) - sea-level pressure over it is meaningless
GREENLAND = [(-50, 60.8), (-44, 60.8), (-40, 63), (-35, 66), (-28, 69), (-19, 72),
             (-19, 84), (-70, 84), (-62, 72), (-54, 65)]


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def point_in_polygon(lon, lat, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    forced = os.environ.get("STORM_NOW")
    if forced:
        return dt.datetime.strptime(forced, "%Y-%m-%dT%H:%M:%SZ")
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------
# Downloading from NOAA
# ----------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "jersey-weather-chat-storm-tracks/1.0"})
    return s


def get_with_retry(session, url, headers=None, tries=4):
    last = None
    for attempt in range(tries):
        try:
            r = session.get(url, headers=headers, timeout=90)
            if r.status_code in (200, 206):
                return r
            if r.status_code == 404:
                raise FileNotFoundError(url)
            last = RuntimeError("HTTP %s for %s" % (r.status_code, url))
        except FileNotFoundError:
            raise
        except requests.RequestException as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise last


def object_key(cycle, fh, suffix=""):
    return "gfs.%s/%s/atmos/gfs.t%sz.pgrb2.0p50.f%03d%s" % (
        cycle.strftime("%Y%m%d"), cycle.strftime("%H"), cycle.strftime("%H"), fh, suffix)


def cycle_is_ready(session, cycle):
    """A run counts as ready once its LAST map (+240h) has been published."""
    try:
        r = session.head("%s/%s" % (BUCKET, object_key(cycle, LAST_HOUR, ".idx")), timeout=30)
        return r.status_code == 200
    except requests.RequestException:
        return False


def candidate_cycles(now, count=6):
    base = now.replace(minute=0, second=0, microsecond=0)
    base -= dt.timedelta(hours=base.hour % 6)
    return [base - dt.timedelta(hours=6 * k) for k in range(count)]


def download_pressure_message(session, cycle, fh):
    """Download just the PRMSL (mean sea level pressure) message for one forecast hour."""
    key = object_key(cycle, fh)
    idx_text = get_with_retry(session, "%s/%s.idx" % (BUCKET, key)).text
    entries = []
    for line in idx_text.splitlines():
        parts = line.strip().split(":")
        if len(parts) >= 5 and parts[1].isdigit():
            entries.append((int(parts[1]), parts[3], parts[4]))
    start = end = None
    for i, (offset, var, level) in enumerate(entries):
        if var == "PRMSL" and level.strip().lower() == "mean sea level":
            start = offset
            end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
            break
    if start is None:
        raise RuntimeError("PRMSL not found in index for f%03d" % fh)
    rng = "bytes=%d-%s" % (start, "" if end is None else end)
    return get_with_retry(session, "%s/%s" % (BUCKET, key), headers={"Range": rng}).content


# ----------------------------------------------------------------------------
# Reading the GRIB data
# ----------------------------------------------------------------------------
def decode_pressure(grib_bytes):
    """Return (lats, lons, pressure_hPa) with latitude and longitude both ascending,
    cut down to the search window."""
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(grib_bytes)
        path = tf.name
    try:
        with open(path, "rb") as f:
            while True:
                gid = eccodes.codes_grib_new_from_file(f)
                if gid is None:
                    break
                try:
                    if eccodes.codes_get(gid, "shortName") not in ("prmsl", "msl"):
                        continue
                    ni = int(eccodes.codes_get(gid, "Ni"))
                    nj = int(eccodes.codes_get(gid, "Nj"))
                    lat1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
                    lat2 = eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees")
                    lon1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
                    lon2 = eccodes.codes_get(gid, "longitudeOfLastGridPointInDegrees")
                    values = eccodes.codes_get_values(gid).reshape(nj, ni) / 100.0  # Pa -> hPa
                    lats = np.linspace(lat1, lat2, nj)
                    lons = np.linspace(lon1, lon2, ni)
                    return cut_to_window(lats, lons, values)
                finally:
                    eccodes.codes_release(gid)
    finally:
        os.unlink(path)
    raise RuntimeError("no PRMSL message found in download")


def cut_to_window(lats, lons, values):
    lons = ((lons + 180.0) % 360.0) - 180.0           # 0..360 -> -180..180
    lat_order = np.argsort(lats)
    lon_order = np.argsort(lons)
    lats, lons = lats[lat_order], lons[lon_order]
    values = values[lat_order][:, lon_order]
    lat_keep = (lats >= LAT_MIN) & (lats <= LAT_MAX)
    lon_keep = (lons >= LON_MIN) & (lons <= LON_MAX)
    return lats[lat_keep], lons[lon_keep], values[np.ix_(lat_keep, lon_keep)]


# ----------------------------------------------------------------------------
# Finding storm centres in one map
# ----------------------------------------------------------------------------
def ring_depth(ps, lats, lons, i, j):
    """How much lower the centre is than the average of a ring RING_DEG around it."""
    dlat = lats[1] - lats[0]
    dlon = lons[1] - lons[0]
    coslat = max(0.2, math.cos(math.radians(lats[i])))
    rows, cols = [], []
    for k in range(8):
        ang = k * math.pi / 4
        rows.append(i + RING_DEG * math.sin(ang) / dlat)
        cols.append(j + RING_DEG * math.cos(ang) / (coslat * dlon))
    ring = map_coordinates(ps, [rows, cols], order=1, mode="nearest")
    return float(ring.mean() - ps[i, j])


def refine(ps, i, j):
    """Sub-grid position of the minimum (parabola through the neighbours)."""
    def off(a, b, c):
        den = a - 2 * b + c
        return 0.0 if abs(den) < 1e-9 else max(-0.5, min(0.5, 0.5 * (a - c) / den))
    di = off(ps[i - 1, j], ps[i, j], ps[i + 1, j]) if 0 < i < ps.shape[0] - 1 else 0.0
    dj = off(ps[i, j - 1], ps[i, j], ps[i, j + 1]) if 0 < j < ps.shape[1] - 1 else 0.0
    return di, dj


def find_lows(p, lats, lons):
    ps = gaussian_filter(p, sigma=SMOOTH_SIGMA, mode="nearest")
    low_env = minimum_filter(ps, size=MIN_WINDOW, mode="nearest")
    found = []
    dlat = lats[1] - lats[0]
    dlon = lons[1] - lons[0]
    for i, j in np.argwhere((ps == low_env) & (ps < MAX_CENTRE_HPA)):
        lat, lon = float(lats[i]), float(lons[j])
        if (lat - LAT_MIN < EDGE_DEG or LAT_MAX - lat < EDGE_DEG or
                lon - LON_MIN < EDGE_DEG or LON_MAX - lon < EDGE_DEG):
            continue
        if point_in_polygon(lon, lat, GREENLAND):
            continue
        depth = ring_depth(ps, lats, lons, int(i), int(j))
        if depth < MIN_DEPTH_HPA:
            continue
        di, dj = refine(ps, int(i), int(j))
        lat += di * dlat
        lon += dj * dlon
        found.append({"lat": lat, "lon": lon, "p": raw_min_near(p, lats, lons, lat, lon, 1.0), "depth": depth})
    return found


def raw_min_near(p, lats, lons, lat, lon, radius_deg):
    """Lowest raw (unsmoothed) pressure within radius_deg of a point."""
    i0 = int(np.searchsorted(lats, lat - radius_deg))
    i1 = int(np.searchsorted(lats, lat + radius_deg, side="right"))
    j0 = int(np.searchsorted(lons, lon - radius_deg))
    j1 = int(np.searchsorted(lons, lon + radius_deg, side="right"))
    i0, j0 = max(i0, 0), max(j0, 0)
    block = p[i0:max(i1, i0 + 1), j0:max(j1, j0 + 1)]
    return float(block.min())


# ----------------------------------------------------------------------------
# Linking centres into tracks
# ----------------------------------------------------------------------------
class Track:
    def __init__(self, step, c):
        self.pts = [dict(c, step=step)]
        self.miss = 0          # maps in a row where this storm was not found

    @property
    def last(self):
        return self.pts[-1]

    def predict(self, step):
        last = self.last
        if len(self.pts) >= 2:
            prev = self.pts[-2]
            span = max(1, last["step"] - prev["step"])
            vlat = (last["lat"] - prev["lat"]) / span
            vlon = (last["lon"] - prev["lon"]) / span
            ahead = step - last["step"]
            return last["lat"] + 0.9 * vlat * ahead, last["lon"] + 0.9 * vlon * ahead
        return last["lat"], last["lon"]


def link_tracks(frames):
    """frames = {step: [centres]} for the maps we actually have (a failed download
    simply leaves a step out, and the prediction step carries the track across it)."""
    tracks, active = [], []
    for step in sorted(frames):
        cands = frames[step]
        active = [t for t in active if t.miss <= MAX_GAP_STEPS]
        pairs = []
        for ti, t in enumerate(active):
            plat, plon = t.predict(step)
            gate = GATE_KM_PREDICTED if len(t.pts) >= 2 else GATE_KM_FRESH
            elapsed = step - t.last["step"]
            if elapsed > 1:
                gate *= min(2.2, 1.0 + 0.6 * (elapsed - 1))
            for ci, c in enumerate(cands):
                d = haversine_km(plat, plon, c["lat"], c["lon"])
                if d <= gate:
                    pairs.append((d, ti, ci))
        pairs.sort()
        used_t, used_c = set(), set()
        for d, ti, ci in pairs:
            if ti in used_t or ci in used_c:
                continue
            active[ti].pts.append(dict(cands[ci], step=step))
            active[ti].miss = 0
            used_t.add(ti)
            used_c.add(ci)
        for ti, t in enumerate(active):
            if ti not in used_t:
                t.miss += 1
        for ci, c in enumerate(cands):
            if ci not in used_c:
                t = Track(step, c)
                tracks.append(t)
                active.append(t)
    return tracks


def path_length_km(pts):
    return sum(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(pts, pts[1:]))


# ----------------------------------------------------------------------------
# Putting it all together
# ----------------------------------------------------------------------------
def choose_cycle(session, now):
    for cycle in candidate_cycles(now):
        if cycle_is_ready(session, cycle):
            return cycle
        log("Run %s is not complete yet" % cycle.strftime("%Y-%m-%d %HZ"))
    raise RuntimeError("no complete GFS run found in the last 36 hours")


def build_tracks(cycle, fields, grid):
    lats, lons = grid
    frames, raw = {}, {}
    for fh, p in fields.items():
        step = fh // STEP_HOURS
        raw[step] = p
        frames[step] = find_lows(p, lats, lons)
    log("Centres found per map: " + " ".join(str(len(frames[s])) for s in sorted(frames)))

    out = []
    for t in link_tracks(frames):
        pts = t.pts
        if len(pts) < MIN_TRACK_POINTS:
            continue
        min_p = min(q["p"] for q in pts)
        if min_p > KEEP_MAX_HPA:
            continue
        if path_length_km(pts) < 250 and min_p > 998:
            continue                      # barely moves and barely deep: not a travelling storm
        out.append(t)

    out.sort(key=lambda t: min(q["p"] for q in t.pts))
    out = out[:MAX_TRACKS]

    tracks = []
    for n, t in enumerate(out, start=1):
        by_step = {q["step"]: q for q in t.pts}
        first, last = t.pts[0]["step"], t.pts[-1]["step"]
        every = OUTPUT_EVERY_HOURS // STEP_HOURS
        path, points = [], []
        for step in range(first, last + 1):
            q = by_step.get(step)
            if q is None:                 # bridge a missed map by interpolating
                a = max((s for s in by_step if s < step))
                b = min((s for s in by_step if s > step))
                f = (step - a) / float(b - a)
                lat = by_step[a]["lat"] + f * (by_step[b]["lat"] - by_step[a]["lat"])
                lon = by_step[a]["lon"] + f * (by_step[b]["lon"] - by_step[a]["lon"])
                if step in raw:
                    pr = raw_min_near(raw[step], lats, lons, lat, lon, 1.5)
                else:                 # no map at all for this step: blend the pressures either side
                    pr = by_step[a]["p"] + f * (by_step[b]["p"] - by_step[a]["p"])
                q = {"lat": lat, "lon": lon, "p": pr, "step": step}
            path.append([round(q["lat"], 2), round(q["lon"], 2)])
            if step % every == 0:
                fh = step * STEP_HOURS
                points.append({
                    "h": fh,
                    "t": iso(cycle + dt.timedelta(hours=fh)),
                    "lat": round(q["lat"], 2),
                    "lon": round(q["lon"], 2),
                    "p": round(q["p"], 1),
                })
        deepest = min(t.pts, key=lambda q: q["p"])
        nearest = min(t.pts, key=lambda q: haversine_km(JERSEY[0], JERSEY[1], q["lat"], q["lon"]))
        tracks.append({
            "id": "L%d" % n,
            "min_hpa": round(deepest["p"], 1),
            "min_hour": deepest["step"] * STEP_HOURS,
            "closest_km": int(round(haversine_km(JERSEY[0], JERSEY[1], nearest["lat"], nearest["lon"]))),
            "closest_hour": nearest["step"] * STEP_HOURS,
            "path_start_hour": first * STEP_HOURS,
            "path": path,
            "points": points,
        })
    return tracks


def main():
    ap = argparse.ArgumentParser(description="Build data/storm-tracks.json from NOAA GFS")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "storm-tracks.json"))
    ap.add_argument("--cycle", help="force a run, e.g. 2026100812 (default: newest complete run)")
    args = ap.parse_args()

    session = make_session()
    now = now_utc()
    if args.cycle:
        cycle = dt.datetime.strptime(args.cycle, "%Y%m%d%H")
    else:
        cycle = choose_cycle(session, now)
    log("Using GFS run %s" % cycle.strftime("%Y-%m-%d %HZ"))

    # Download the maps in parallel, decode them one at a time
    def fetch(fh):
        try:
            return fh, download_pressure_message(make_session(), cycle, fh)
        except Exception as e:                      # noqa: BLE001 - report and carry on
            log("  f%03d: download failed (%s)" % (fh, e))
            return fh, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        downloads = dict(pool.map(fetch, FORECAST_HOURS))

    fields, grid = {}, None
    for fh in FORECAST_HOURS:
        blob = downloads.get(fh)
        if blob is None:
            continue
        try:
            lats, lons, p = decode_pressure(blob)
        except Exception as e:                      # noqa: BLE001
            log("  f%03d: could not read (%s)" % (fh, e))
            continue
        if grid is None:
            grid = (lats, lons)
        elif p.shape != (len(grid[0]), len(grid[1])):
            log("  f%03d: unexpected grid size, skipped" % fh)
            continue
        fields[fh] = p
    log("Got %d of %d pressure maps" % (len(fields), len(FORECAST_HOURS)))
    if len(fields) < MIN_FIELDS_NEEDED:
        log("Too few maps to track storms reliably - leaving the existing file untouched.")
        return 1

    tracks = build_tracks(cycle, fields, grid)
    log("Kept %d storm tracks" % len(tracks))
    for t in tracks:
        log("  %s  deepest %.1f hPa at +%dh, closest to Jersey %d km at +%dh"
            % (t["id"], t["min_hpa"], t["min_hour"], t["closest_km"], t["closest_hour"]))

    result = {
        "generated": iso(now),
        "model": "NOAA GFS 0.5 degree",
        "cycle": iso(cycle),
        "step_hours": OUTPUT_EVERY_HOURS,
        "tracks": tracks,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    log("Wrote %s (%d bytes)" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
