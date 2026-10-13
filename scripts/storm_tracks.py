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

5. For every storm that comes within about 700 miles of Jersey it ALSO downloads the
   10 m wind and gust maps and records the strongest wind in each of 8 compass
   sectors (N, NE, E ...) within 400 km of the centre, every 6 hours. The page draws
   this as a "wind rose" around the storm (the strongest winds are usually on the
   south-west side of a Atlantic low).
6. Keeps a small registry (data/storm-registry.json) so each storm keeps the SAME
   number from one 6-hourly run to the next, and so that when a storm is given an
   official name (UK/Ireland/Netherlands list, France/Spain/Portugal list, or a
   German one) the name replaces the number automatically. A name can also be set or
   changed by hand: GitHub > Actions > "Storm tracks" > "Run workflow".

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
import email.utils
import json
import math
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
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

# Wind around the storm centre
MILES_TO_KM = 1.609344
RADIUS_500MI_KM = 500 * MILES_TO_KM        # the dashed circle on the map (804.7 km)
WIND_NEAR_KM = 1100.0       # work out the wind rose for centres this close to Jersey
WIND_RADIUS_KM = 400.0      # look this far out from each centre for the strongest wind
MS_TO_MPH = 2.236936
SECTOR_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

# Storm identities and names
REGISTRY_KEEP_DAYS = 5      # forget a storm this long after it was last seen
MATCH_MEAN_KM = 450.0       # same storm in two runs if its positions agree this closely
MATCH_MIN_OVERLAP = 3       # ...over at least this many shared 6-hour times
NAME_MAX_AGE_HOURS = 72     # only news from the last 3 days can name a storm
ELIGIBLE_KM = 1500.0        # a storm can only be auto-named if it comes this close to Jersey...
ELIGIBLE_HOURS = 96         # ...within this many hours...
ELIGIBLE_HPA = 995.0        # ...and gets at least this deep
# Official lists for the 2026-27 season, in the order they are used (checked against
# several published lists, Sept 2026). Update these each September.
NAME_LISTS = {
    "UK / IRELAND / NETHERLANDS": [
        "Austen", "Boelo", "Chloe", "Dano", "Evelyn", "Finn", "Gwendolyn", "Heather", "Ivy",
        "Jade", "Kilian", "Laoise", "Miriam", "Nicola", "Olaf", "Phelim", "Ruban", "Shaun",
        "Theo", "Vesna", "Wende"],
    "FRANCE / SPAIN / PORTUGAL": [
        "Adam", "Bruna", "Charles", "Diane", "Eric", "Flora", "Gabriel", "H\u00e9lo\u00efse", "Igor",
        "Julia", "K\u00e9vin", "Lola", "Michel", "Nathalie", "Oscar", "Paola", "Rafael", "Sandra",
        "Thomas", "Vera", "Walter"],
}
GERMAN_GROUP = "GERMANY (DWD / FU BERLIN)"
GERMAN_STOP = {"Wetter", "Warnung", "Lage", "Gefahr", "Welle", "Wind", "Sturm", "Deutschland", "Tief",
               "Unwetter", "Orkan", "Schnee", "Regen", "Nordsee", "Ostsee", "Dienst", "Wetterdienst",
               "Vorhersage", "Bahn", "Fl\u00fcge", "Fluege", "Verkehr", "Schaden", "Sch\u00e4den", "Alarm"}
CONTEXT_YES = re.compile(r"warning|warn|named|names storm|batter|brings|bring|gales|gale|wind|vigilance|alerte|"
                         r"vigilancia|aviso|warnung|expected|flood|travel|disruption|orange|amber|yellow|red", re.I)
CONTEXT_NO = re.compile(r"\blist\b|names? (?:for|of|revealed|announced)|revealed|public vote|voted|picked|"
                        r"liste|noms des|namensliste|nombres|season's names|every storm name", re.I)
NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=%22named+storm%22+OR+%22Storm%22+%22Met+Office%22+OR+%22Met+%C3%89ireann%22+OR+%22KNMI%22+when:3d&hl=en-GB&gl=GB&ceid=GB:en",
    "https://news.google.com/rss/search?q=temp%C3%AAte+nomm%C3%A9e+M%C3%A9t%C3%A9o-France+vigilance+when:3d&hl=fr&gl=FR&ceid=FR:fr",
    "https://news.google.com/rss/search?q=borrasca+AEMET+nombre+when:3d&hl=es&gl=ES&ceid=ES:es",
    "https://news.google.com/rss/search?q=Sturmtief+DWD+Orkan+when:3d&hl=de&gl=DE&ceid=DE:de",
]

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


def download_message(session, cycle, fh, var, level):
    """Download just ONE field (e.g. PRMSL / mean sea level) for one forecast hour,
    using NOAA's .idx index to find its byte range."""
    key = object_key(cycle, fh)
    idx_text = get_with_retry(session, "%s/%s.idx" % (BUCKET, key)).text
    entries = []
    for line in idx_text.splitlines():
        parts = line.strip().split(":")
        if len(parts) >= 5 and parts[1].isdigit():
            entries.append((int(parts[1]), parts[3], parts[4]))
    start = end = None
    for i, (offset, v, lv) in enumerate(entries):
        if v == var and lv.strip().lower() == level.lower():
            start = offset
            end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
            break
    if start is None:
        raise RuntimeError("%s (%s) not found in index for f%03d" % (var, level, fh))
    rng = "bytes=%d-%s" % (start, "" if end is None else end)
    return get_with_retry(session, "%s/%s" % (BUCKET, key), headers={"Range": rng}).content


def download_pressure_message(session, cycle, fh):
    """Mean sea level pressure for one forecast hour."""
    return download_message(session, cycle, fh, "PRMSL", "mean sea level")


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


def decode_field(grib_bytes):
    """Like decode_pressure, but for any single-field download (wind, gust): returns
    (lats, lons, values) in the field's own units, cut to the search window."""
    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(grib_bytes)
        path = tf.name
    try:
        with open(path, "rb") as f:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                raise RuntimeError("no message in download")
            try:
                ni = int(eccodes.codes_get(gid, "Ni"))
                nj = int(eccodes.codes_get(gid, "Nj"))
                lat1 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
                lat2 = eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees")
                lon1 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
                lon2 = eccodes.codes_get(gid, "longitudeOfLastGridPointInDegrees")
                values = eccodes.codes_get_values(gid).reshape(nj, ni)
                lats = np.linspace(lat1, lat2, nj)
                lons = np.linspace(lon1, lon2, ni)
                return cut_to_window(lats, lons, values)
            finally:
                eccodes.codes_release(gid)
    finally:
        os.unlink(path)


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


# ----------------------------------------------------------------------------
# Wind around each storm centre ("wind rose")
# ----------------------------------------------------------------------------
def bearing_deg(lat1, lon1, lat2, lon2):
    """Compass bearing (0 = north, 90 = east) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def make_geo(lats, lons):
    la, lo = np.meshgrid(lats, lons, indexing="ij")
    return np.radians(la), np.radians(lo)


def sector_winds(geo, speed_ms, gust_ms, lat, lon):
    """Strongest 10 m wind and gust (mph) in each of 8 compass sectors (centred on
    N, NE, E, SE, S, SW, W, NW) within WIND_RADIUS_KM of a storm centre."""
    la, lo = geo
    p1, l1 = math.radians(lat), math.radians(lon)
    dphi, dlam = la - p1, lo - l1
    a = np.sin(dphi / 2) ** 2 + math.cos(p1) * np.cos(la) * np.sin(dlam / 2) ** 2
    dist = 6371.0 * 2 * np.arcsin(np.minimum(1.0, np.sqrt(a)))
    y = np.sin(dlam) * np.cos(la)
    x = math.cos(p1) * np.sin(la) - math.sin(p1) * np.cos(la) * np.cos(dlam)
    brg = (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0
    inside = dist <= WIND_RADIUS_KM
    sec = (((brg + 22.5) % 360.0) // 45.0).astype(int)
    s_out, g_out = [], []
    for k in range(8):
        m = inside & (sec == k)
        if not m.any():
            s_out.append(0)
            g_out.append(0)
            continue
        sp = float(speed_ms[m].max()) * MS_TO_MPH
        gu = float(gust_ms[m].max()) * MS_TO_MPH if gust_ms is not None else sp
        s_out.append(int(round(sp)))
        g_out.append(int(round(max(gu, sp))))
    return s_out, g_out


def fetch_wind_fields(cycle, hours, grid_shape):
    """Download 10 m u/v wind and gust for the given forecast hours (best effort)."""
    jobs = []
    for fh in hours:
        jobs += [(fh, "u", "UGRD", "10 m above ground"), (fh, "v", "VGRD", "10 m above ground"),
                 (fh, "g", "GUST", "surface")]

    def one(job):
        fh, tag, var, level = job
        try:
            return job, download_message(make_session(), cycle, fh, var, level)
        except Exception as e:                           # noqa: BLE001
            log("  wind f%03d %s: download failed (%s)" % (fh, var, e))
            return job, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        blobs = list(pool.map(one, jobs))
    out = {}
    for (fh, tag, var, level), blob in blobs:
        if blob is None:
            continue
        try:
            _, _, arr = decode_field(blob)
        except Exception as e:                           # noqa: BLE001
            log("  wind f%03d %s: could not read (%s)" % (fh, var, e))
            continue
        if arr.shape != grid_shape:
            continue
        out.setdefault(fh, {})[tag] = arr
    return out


def add_winds(cycle, tracks, grid):
    """Adds "within_500mi" and (where possible) "winds" to every exported track."""
    needed = set()
    for t in tracks:
        t["within_500mi"] = None
        inside = []
        for i, (la, lo) in enumerate(t["path"]):
            h = t["path_start_hour"] + 6 * i
            d = haversine_km(JERSEY[0], JERSEY[1], la, lo)
            if d <= RADIUS_500MI_KM:
                inside.append(h)
            if d <= WIND_NEAR_KM:
                needed.add(h)
        if inside:
            t["within_500mi"] = {"from_h": min(inside), "to_h": max(inside)}
    if not needed:
        log("No storm centre comes within %d km of Jersey - no wind roses needed." % WIND_NEAR_KM)
        return
    log("Wind roses needed for %d forecast times" % len(needed))
    shape = (len(grid[0]), len(grid[1]))
    fields = fetch_wind_fields(cycle, sorted(needed), shape)
    geo = make_geo(grid[0], grid[1])
    for t in tracks:
        winds = []
        for i, (la, lo) in enumerate(t["path"]):
            h = t["path_start_hour"] + 6 * i
            f = fields.get(h)
            d = haversine_km(JERSEY[0], JERSEY[1], la, lo)
            if d > WIND_NEAR_KM or not f or "u" not in f or "v" not in f:
                continue
            speed = np.hypot(f["u"], f["v"])
            s, g = sector_winds(geo, speed, f.get("g"), la, lo)
            winds.append({"h": h, "d": int(round(d)),
                          "b": int(round(bearing_deg(la, lo, JERSEY[0], JERSEY[1]))),
                          "s": s, "g": g})
        if winds:
            t["winds"] = winds


# ----------------------------------------------------------------------------
# Stable storm numbers (the registry)
# ----------------------------------------------------------------------------
EPOCH = dt.datetime(2000, 1, 1)


def hours_since_epoch(d):
    return int((d - EPOCH).total_seconds() // 3600)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def track_valid_points(cycle, t):
    """{valid-hour: (lat, lon)} for every 6-hourly point of an exported track."""
    base = hours_since_epoch(cycle)
    return {base + t["path_start_hour"] + 6 * i: (la, lo) for i, (la, lo) in enumerate(t["path"])}


def match_to_registry(cycle, tracks, reg):
    """Give every track the registry number of the storm it continues from the last
    run (same positions at the same clock times), or a brand-new number."""
    pairs = []
    cur = [track_valid_points(cycle, t) for t in tracks]
    for ti, pts in enumerate(cur):
        for rec in reg["storms"]:
            old = {int(v[0]): (v[1], v[2]) for v in rec.get("pts", [])}
            shared = [k for k in pts if k in old]
            if len(shared) < MATCH_MIN_OVERLAP:
                continue
            mean = sum(haversine_km(pts[k][0], pts[k][1], old[k][0], old[k][1]) for k in shared) / len(shared)
            if mean <= MATCH_MEAN_KM:
                pairs.append((mean, ti, rec["num"]))
    pairs.sort()
    used_t, used_n = {}, set()
    for mean, ti, num in pairs:
        if ti in used_t or num in used_n:
            continue
        used_t[ti] = num
        used_n.add(num)
    by_num = {r["num"]: r for r in reg["storms"]}
    now_iso = iso(cycle)
    for ti, t in enumerate(tracks):
        if ti in used_t:
            rec = by_num[used_t[ti]]
        else:
            rec = {"num": reg["next_num"], "first_seen": now_iso, "names": []}
            reg["next_num"] += 1
            reg["storms"].append(rec)
            by_num[rec["num"]] = rec
            log("  new storm #%d" % rec["num"])
        rec["last_seen"] = now_iso
        rec["pts"] = [[k, round(v[0], 2), round(v[1], 2)] for k, v in sorted(cur[ti].items())]
        t["num"] = rec["num"]
    cutoff = cycle - dt.timedelta(days=REGISTRY_KEEP_DAYS)
    reg["storms"] = [r for r in reg["storms"]
                     if dt.datetime.strptime(r.get("last_seen", now_iso), "%Y-%m-%dT%H:%M:%SZ") >= cutoff]


# ----------------------------------------------------------------------------
# Names
# ----------------------------------------------------------------------------
def fetch_news_items(session, now):
    """[(published-time, text)] from the news searches (best effort, never fatal)."""
    items = []
    for url in NEWS_FEEDS:
        try:
            r = session.get(url, timeout=40)
            if r.status_code != 200:
                log("  news feed HTTP %s" % r.status_code)
                continue
            root = ET.fromstring(r.content)
        except Exception as e:                           # noqa: BLE001
            log("  news feed failed (%s)" % e)
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "")
            desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
            try:
                when = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
                when = when.astimezone(dt.timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError):
                continue
            if (now - when).total_seconds() / 3600.0 > NAME_MAX_AGE_HOURS:
                continue
            items.append((when, title + " . " + desc))
    log("News items in the last %d hours: %d" % (NAME_MAX_AGE_HOURS, len(items)))
    return items


def norm_name(n):
    return n.strip().lower()


def detect_names(items, reg):
    """Names that look newly in use. Official lists: only names not used yet, only in
    news that reads like a storm report (not a 'here are the names' article).
    Germany has no fixed list, so any 'Sturmtief <Name>' seen in 2+ stories counts."""
    used = set(reg["used_names"])
    found = {}
    for group, names in NAME_LISTS.items():
        ptr = reg["pointers"].get(group, 0)
        for idx, nm in enumerate(names):
            if idx < ptr or norm_name(nm) in used:
                continue
            pat = re.compile(r"\b(?:storm|temp[eê]te|borrasca|sturm(?:tief)?|orkantief)\s+" + re.escape(nm) + r"\b", re.I)
            hits = [w for w, text in items if pat.search(text) and CONTEXT_YES.search(text) and not CONTEXT_NO.search(text)]
            if hits:
                found[norm_name(nm)] = {"name": nm, "by": group, "when": min(hits), "hits": len(hits), "idx": idx, "official": True}
    german = re.compile(r"\b(?:Sturmtief|Orkantief|Sturm)\s+([A-ZÄÖÜ][a-zäöüß]{2,11})\b")
    official = {norm_name(n) for names in NAME_LISTS.values() for n in names}
    seen = {}
    for w, text in items:
        for m in set(german.findall(text)):
            if m in GERMAN_STOP or norm_name(m) in official or norm_name(m) in used:
                continue
            seen.setdefault(m, []).append(w)
    for m, ws in seen.items():
        if len(ws) >= 2:
            found[norm_name(m)] = {"name": m, "by": GERMAN_GROUP, "when": min(ws), "hits": len(ws), "idx": None, "official": False}
    return sorted(found.values(), key=lambda f: f["when"])


def is_eligible(t):
    """Could this storm plausibly be an officially named one (close to Jersey/western
    Europe soon and reasonably deep)?"""
    close = False
    for i, (la, lo) in enumerate(t["path"]):
        h = t["path_start_hour"] + 6 * i
        if h <= ELIGIBLE_HOURS and haversine_km(JERSEY[0], JERSEY[1], la, lo) <= ELIGIBLE_KM:
            close = True
    deep = min([q["p"] for q in t["points"] if q["h"] <= ELIGIBLE_HOURS] or [9999]) <= ELIGIBLE_HPA
    return close and deep


def rec_for(reg, num):
    for r in reg["storms"]:
        if r["num"] == num:
            return r
    return None


def give_name(reg, rec, f_name, by, when_iso, src):
    for other in reg["storms"]:                         # a name belongs to one storm only
        other["names"] = [n for n in other["names"] if norm_name(n["name"]) != norm_name(f_name)]
    rec["names"].append({"name": f_name, "by": by, "when": when_iso, "src": src})
    reg["used_names"] = sorted(set(reg["used_names"]) | {norm_name(f_name)})
    for group, names in NAME_LISTS.items():
        for idx, nm in enumerate(names):
            if norm_name(nm) == norm_name(f_name):
                reg["pointers"][group] = max(reg["pointers"].get(group, 0), idx + 1)


def auto_name(tracks, reg, found, now):
    """Put each newly found name on the one storm it can safely be matched to."""
    pending = []
    for f in found:
        eligible = [t for t in tracks if is_eligible(t) and not rec_for(reg, t["num"])["names"]]
        named_eligible = [t for t in tracks if is_eligible(t) and rec_for(reg, t["num"])["names"]]
        safe = len(eligible) == 1 and (f["official"] or not named_eligible)
        if safe:
            t = eligible[0]
            give_name(reg, rec_for(reg, t["num"]), f["name"], f["by"], iso(f["when"]), "auto")
            log("  NAME %s (%s) -> storm #%d" % (f["name"], f["by"], t["num"]))
        else:
            log("  name %s (%s) seen but not matched: %d unnamed candidate storm(s)" % (f["name"], f["by"], len(eligible)))
            pending.append({"name": f["name"], "by": f["by"], "when": iso(f["when"])})
    return pending


def apply_manual(reg, now):
    """Hand-set names from the 'Run workflow' form (environment variables)."""
    num = (os.environ.get("STORM_SET_NUM") or "").strip().lstrip("#")
    name = (os.environ.get("STORM_SET_NAME") or "").strip()
    mode = (os.environ.get("STORM_SET_MODE") or "set").strip().lower()
    by = (os.environ.get("STORM_SET_GROUP") or "").strip() or "MANUAL"
    if not num:
        return
    if not num.isdigit() or rec_for(reg, int(num)) is None:
        log("MANUAL NAME IGNORED: there is no current storm number %r" % num)
        return
    rec = rec_for(reg, int(num))
    if mode == "clear":
        rec["names"] = []
        log("  manual: cleared the name of storm #%s" % num)
        return
    if not name:
        log("MANUAL NAME IGNORED: no name typed")
        return
    name = name[:1].upper() + name[1:]
    if mode == "set":
        rec["names"] = []
    give_name(reg, rec, name, by, iso(now), "manual")
    log("  manual: storm #%s %s %s" % (num, "is now" if mode == "set" else "is also known as", name))


def run_names(session, cycle, now, tracks, reg):
    items = fetch_news_items(session, now)
    found = detect_names(items, reg)
    log("Possible new names: " + (", ".join("%s (%s, %d stories)" % (f["name"], f["by"], f["hits"]) for f in found) or "none"))
    pending = auto_name(tracks, reg, found, now)
    apply_manual(reg, now)
    live = {norm_name(n["name"]) for r in reg["storms"] for n in r["names"]}
    pending = [p for p in pending if norm_name(p["name"]) not in live]
    for t in tracks:
        names = rec_for(reg, t["num"])["names"]
        t["name"] = names[0]["name"] if names else None
        t["name_by"] = names[0]["by"] if names else None
        t["name_src"] = names[0]["src"] if names else None
        t["aka"] = [{"name": n["name"], "by": n["by"]} for n in names[1:]]
    return pending


def load_registry(path):
    reg = load_json(path, {})
    reg.setdefault("next_num", 1)
    reg.setdefault("storms", [])
    reg.setdefault("pointers", {})
    reg.setdefault("used_names", [])
    return reg


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

    # Wind around storms near Jersey (best effort: the map still works without it)
    try:
        add_winds(cycle, tracks, grid)
    except Exception as e:                              # noqa: BLE001
        log("Wind roses skipped (%s)" % e)

    # Stable numbers and names (best effort too)
    out = Path(args.out)
    reg_path = out.with_name("storm-registry.json")
    pending = []
    try:
        reg = load_registry(reg_path)
        match_to_registry(cycle, tracks, reg)
        pending = run_names(session, cycle, now, tracks, reg)
        reg_path.write_text(json.dumps(reg, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        log("Wrote %s" % reg_path)
    except Exception as e:                              # noqa: BLE001
        log("Storm numbering/naming skipped (%s)" % e)
        for n, t in enumerate(tracks, start=1):
            t.setdefault("num", n)
            t.setdefault("name", None)

    result = {
        "generated": iso(now),
        "model": "NOAA GFS 0.5 degree",
        "cycle": iso(cycle),
        "step_hours": OUTPUT_EVERY_HOURS,
        "radius_500mi_km": round(RADIUS_500MI_KM, 1),
        "pending_names": pending,
        "tracks": tracks,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    log("Wrote %s (%d bytes)" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
