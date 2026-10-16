#!/usr/bin/env python3
"""
Sea-surface-temperature anomaly for the Jersey Weather Chat HUD.

What this does, in plain English
--------------------------------
Visitors' browsers used to ask NOAA's CoastWatch server for this data directly,
and that server is slow, goes down, and refuses busy callers - so the sea page
kept showing "NOAA UNAVAILABLE". Now this script does the asking instead, a
couple of times a day, from GitHub's servers, and saves the answer as
data/sst-anomaly.json. The sea page reads that file from your own site.

1. Asks CoastWatch for the near-real-time OISST anomaly map (a few days old).
   If that fails, or is more than 7 days old, it asks for the "Final" version.
2. Retries a few times with pauses (the server is often just slow).
3. Saves one small file: the anomaly at Jersey plus the whole map grid.
4. If every attempt fails it leaves the previous file alone, so the page keeps
   showing the last good reading (with its date) instead of going blank.

It is run by .github/workflows/sst-anomaly.yml. You can also run that by hand:
GitHub > Actions > "SST anomaly" > "Run workflow".
"""

import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import requests

BASE = "https://coastwatch.pfeg.noaa.gov/erddap"
DATASETS = [
    ("COASTWATCH NRT", "ncdcOisst21NrtAgg_LonPM180"),
    ("COASTWATCH FINAL", "ncdcOisst21Agg_LonPM180"),
]
LAT0, LAT1 = 42.125, 53.875
LON0, LON1 = -11.875, 7.875
STEP = 0.25
NX, NY = 80, 48
JERSEY = (49.125, -2.125)          # grid cell nearest Jersey
MAX_AGE_DAYS_FOR_NRT = 7           # older than this: also try the Final dataset
PAUSES = [5, 15, 30, 60]           # seconds between retries


def log(msg):
    print(msg, flush=True)


def fetch_grid(session, ds_id):
    url = ("%s/griddap/%s.json?anom%%5B(last)%%5D%%5B(0.0)%%5D%%5B(%s):(%s)%%5D%%5B(%s):(%s)%%5D"
           % (BASE, ds_id, LAT0, LAT1, LON0, LON1))
    last = None
    for attempt in range(len(PAUSES) + 1):
        try:
            r = session.get(url, timeout=120)
            if r.status_code == 200:
                return parse_grid(r.json())
            last = "HTTP %s" % r.status_code
        except Exception as e:                       # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, e)
        log("    attempt %d failed (%s)" % (attempt + 1, last))
        if attempt < len(PAUSES):
            time.sleep(PAUSES[attempt])
    raise RuntimeError(last)


def parse_grid(j):
    rows = j["table"]["rows"]
    grid = [None] * (NX * NY)
    when = None
    ok = 0
    for t, _z, lat, lon, v in rows:
        when = when or t
        i = int(round((lon - LON0) / STEP))
        k = int(round((lat - LAT0) / STEP))
        if 0 <= i < NX and 0 <= k < NY and v is not None and not (isinstance(v, float) and math.isnan(v)):
            grid[k * NX + i] = round(float(v), 2)
            ok += 1
    if not when or ok < 1000:
        raise RuntimeError("too little data (%d sea cells)" % ok)
    ji = int(round((JERSEY[0] - LAT0) / STEP)) * NX + int(round((JERSEY[1] - LON0) / STEP))
    if grid[ji] is None:
        raise RuntimeError("no value at Jersey")
    return {"time": when, "anom": grid[ji], "grid": grid}


def parse_time(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "data" / "sst-anomaly.json"))
    args = ap.parse_args()
    out = Path(args.out)
    session = requests.Session()
    session.headers.update({"User-Agent": "jersey-weather-chat-sst/1.0"})

    best = None
    for tag, ds_id in DATASETS:
        log("Trying %s (%s)" % (tag, ds_id))
        try:
            res = fetch_grid(session, ds_id)
        except Exception as e:                       # noqa: BLE001
            log("  %s failed: %s" % (tag, e))
            continue
        age = (dt.datetime.utcnow() - parse_time(res["time"])).total_seconds() / 86400.0
        log("  %s OK: data for %s (%.1f days old), Jersey %+.2f C" % (tag, res["time"], age, res["anom"]))
        res.update(tag=tag, dataset=ds_id)
        if best is None or parse_time(res["time"]) > parse_time(best["time"]):
            best = res
        if age <= MAX_AGE_DAYS_FOR_NRT:
            break                                    # fresh enough: no need to ask for the Final set

    if best is None:
        prev = None
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if prev:
            log("All sources failed - keeping the previous file (data for %s)." % prev.get("time"))
            return 0
        log("All sources failed and there is no previous file.")
        return 1

    result = {
        "generated": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tag": best["tag"],
        "dataset": best["dataset"],
        "time": best["time"],
        "anom": best["anom"],
        "nx": NX, "ny": NY, "lat0": LAT0, "lon0": LON0, "step": STEP,
        "grid": best["grid"],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    log("Wrote %s (%d bytes)" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
