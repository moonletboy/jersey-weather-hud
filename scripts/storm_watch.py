#!/usr/bin/env python3
"""
Storm Watch - Jersey Weather Chat HUD

Run by .github/workflows/storm-watch.yml every ~10 minutes. It checks live readings
against the thresholds below. When one is met it does NOT send anything to the
public: it asks the Apps Script to EMAIL Matthew a suggested special alert, which he
approves (sends), tests, or dismisses from the email. Every suggestion and every
decision is recorded in the AlertLog tab of the Google Sheet.

Mode (GitHub > Settings > Secrets and variables > Actions > Variables > STORM_WATCH):
  dry  (default if not set) - checks and records what WOULD have been suggested in
                              AlertLog as "dry run", but sends no email
  live                      - emails a suggestion when a rule is met
  off                       - does nothing
A run started by hand with "Send me a test email" ticked always emails a TEST
suggestion built from the current readings, whatever the mode.

Secrets: JWC_ADMIN_KEY (required), WU_API_KEY (optional - without it the
personal-weather-station rule is skipped).
"""
import json, os, re, sys, datetime, statistics
import requests
try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("Europe/Jersey")
except Exception:
    LOCAL = datetime.timezone.utc

# ======================= THRESHOLDS - change the numbers here =======================
GUST_MPH = 70                    # observed or forecast gust that prompts a suggestion
PWS_MULTI_MPH = 70               # this many mph ...
PWS_MULTI_COUNT = 3              # ... at this many WU stations at once
PWS_SINGLE_MPH = 80              # or any ONE WU station this high (single stations spike, so higher bar)
FORECAST_HOURS_AHEAD = 3         # look this far ahead in the forecast
FORECAST_MIN_MODELS = 3          # this many of the five models must reach GUST_MPH
PRESSURE_FALL_HPA = 6            # pressure fall in 3 hours at Jersey Airport ("falling very rapidly")
COOLDOWN_HOURS = 3               # a rule stays quiet this long after suggesting ...
ESCALATE_MPH = 10                # ... unless the gust climbs this much higher
ESCALATE_HPA = 3                 # ... or the pressure fall deepens by this much
BREWING_MPH = 50                 # weather stations are only checked when something is brewing:
                                 #   a land warning in force, or gusts this strong observed/forecast
PWS_EVERY_MINUTES = 30           # and then at most this often (Weather Underground daily call limit)
# ====================================================================================

API_URL = "https://script.google.com/macros/s/AKfycbzqw6EZ01rKNo8FJrP-VHwpNCRe9AWV2FAeVH3AvVzX2wXWpuprFNRQngIgJC0K08FHcg/exec"
STATE_FILE = "data/storm-watch-state.json"
WARNINGS_STATE = "data/warnings-state.json"
JERSEY = (49.21, -2.13)
AIRPORTS = {"EGJJ": "Jersey Airport", "EGJB": "Guernsey Airport", "EGJA": "Alderney Airport"}
MODELS = {"gfs_seamless": "GFS", "ecmwf_ifs025": "ECMWF", "icon_seamless": "ICON",
          "ukmo_seamless": "UK Met Office", "meteofrance_seamless": "Meteo-France"}
STATIONS = [
    ("ITRINITY2", "Trinity"),
    ("ISAINT6315", "Saint Ouen"),
    ("ITRINI32", "Trinity"),
    ("IJERSEY24", "Jersey"),
    ("ISAINT5096", "Saint Mary"),
    ("ISAINT7628", "Saint John"),
    ("ISAINT7711", "Saint Saviour"),
    ("ISAINT5503", "Saint Helier"),
    ("ISTPET54", "St Peter's"),
    ("ISAINT7013", "Saint Martin"),
    ("ISAINTLA156", "Saint Lawrence"),
    ("ISAINT9517", "Saint Martin"),
    ("ISAINT10791", "Saint Martin"),
    ("ISTPET53", "St Peters"),
    ("ISAINT10594", "Saint Saviour"),
    ("IJERSE26", "Jersey"),
    ("ISAINT9683", "Saint Brelade"),
    ("ISAINT8998", "Saint Brelade"),
    ("ISAINT11214", "Saint Saviour"),
    ("ISTSAV1", "St. Saviour"),
    ("ISAINT8993", "Saint Brelade"),
    ("ISAINT9928", "Saint Helier"),
    ("ISAINT11417", "Saint Lawrence"),
    ("IGROUVIL2", "Gorey"),
    ("ICHANNEL3", "Jersey"),
    ("ISAINT1019", "Saint Helier"),
    ("ISAINT9664", "Saint Helier"),
    ("IMAISO24", "Maison St.Louis"),
    ("IGROUV4", "Grouville"),
    ("ISAINT5592", "Saint Brelade"),
    ("IJERSE35", "Jersey"),
    ("ISAINT4222", "Saint Helier"),
    ("ISAINT7913", "Saint Saviour"),
    ("ISAINT10533", "Saint Helier"),
    ("ISAINT11726", "Saint Helier"),
    ("ISAINT7380", "Saint Clement"),
    ("ISAINT5415", "Saint Brelade"),
    ("ISAINT6802", "Saint Clement"),
    ("ISAINT6768", "Saint Clement"),
    ("IGROUV2", "Grouville"),
    ("ISAINT947", "Saint Clement")
]
KT_TO_MPH = 1.15078
FOOTER = "Check gov.je for official Jersey Met warnings."

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")

def local_hm(t):
    return t.astimezone(LOCAL).strftime("%H:%M")

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")

# ---------------- readings ----------------
METAR_WIND = re.compile(r"\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\b")
METAR_QNH = re.compile(r"\bQ(\d{4})\b")

def fetch_metars():
    """Latest observation per airport plus Jersey's pressure history (last ~4 hours)."""
    r = requests.get("https://aviationweather.gov/api/data/metar",
                     params={"ids": ",".join(AIRPORTS), "format": "json", "hours": 4},
                     headers={"User-Agent": "JerseyWeatherChat-StormWatch/1.0 (jwcliveops@gmail.com)"}, timeout=40)
    r.raise_for_status()
    obs = {}
    for m in r.json() or []:
        raw = m.get("rawOb") or ""
        t = datetime.datetime.fromtimestamp(int(m.get("obsTime") or 0), datetime.timezone.utc)
        w = METAR_WIND.search(raw)
        mult = KT_TO_MPH if (w and w.group(4) == "KT") else 2.23694
        speed = round(int(w.group(2)) * mult) if w else None
        gust = round(int(w.group(3)) * mult) if (w and w.group(3)) else None
        q = METAR_QNH.search(raw)
        obs.setdefault(m.get("icaoId"), []).append({
            "t": t, "dir": w.group(1) if w else "", "speed": speed, "gust": gust,
            "qnh": int(q.group(1)) if q else None, "raw": raw})
    for k in obs:
        obs[k].sort(key=lambda o: o["t"])
    return obs

def pressure_fall(history):
    """hPa fallen over ~3 hours (positive = falling), from the Q group of Jersey's METARs."""
    pts = [o for o in history if o["qnh"]]
    if len(pts) < 2:
        return None
    last = pts[-1]
    target = last["t"] - datetime.timedelta(hours=3)
    old = min(pts, key=lambda o: abs((o["t"] - target).total_seconds()))
    if abs((old["t"] - target).total_seconds()) > 40 * 60:
        return None
    return old["qnh"] - last["qnh"]

def fetch_forecast():
    """Max gust per model at Jersey over the next FORECAST_HOURS_AHEAD hours, and when."""
    r = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": JERSEY[0], "longitude": JERSEY[1], "hourly": "wind_gusts_10m",
        "models": ",".join(MODELS), "wind_speed_unit": "mph", "timezone": "GMT", "forecast_days": 2}, timeout=40)
    r.raise_for_status()
    h = r.json().get("hourly", {})
    times = [datetime.datetime.fromisoformat(t).replace(tzinfo=datetime.timezone.utc) for t in h.get("time", [])]
    start, end = now_utc() - datetime.timedelta(minutes=30), now_utc() + datetime.timedelta(hours=FORECAST_HOURS_AHEAD)
    out = {}
    for key, label in MODELS.items():
        vals = h.get("wind_gusts_10m_" + key) or []
        best = None
        for t, v in zip(times, vals):
            if v is not None and start <= t <= end and (best is None or v > best[0]):
                best = (round(v), t)
        if best:
            out[label] = {"gust": best[0], "t": best[1]}
    return out

def fetch_pws(key):
    out, cutoff = [], now_utc() - datetime.timedelta(minutes=30)
    for sid, name in STATIONS:
        try:
            r = requests.get("https://api.weather.com/v2/pws/observations/current",
                             params={"stationId": sid, "format": "json", "units": "e", "apiKey": key}, timeout=20)
            if r.status_code == 429:
                print("  Weather Underground says too many requests - stopping the station check for this run.")
                break
            if r.status_code != 200 or not r.text.strip():
                continue
            o = (r.json().get("observations") or [{}])[0]
            t = datetime.datetime.fromisoformat((o.get("obsTimeUtc") or "").replace("Z", "+00:00"))
            gust = (o.get("imperial") or {}).get("windGust")
            if gust is None or t < cutoff:
                continue
            out.append({"id": sid, "name": name, "gust": round(gust), "t": iso(t)})
        except Exception:
            continue
    return out

# ---------------- rules ----------------
def evaluate(metars, fc, pws, fall):
    trig = []
    fresh = now_utc() - datetime.timedelta(minutes=90)
    jj = (metars.get("EGJJ") or [None])[-1]
    if jj and jj["t"] >= fresh and (jj["gust"] or 0) >= GUST_MPH:
        trig.append({"rule": "jersey-airport-gust", "value": jj["gust"], "unit": "mph",
                     "text": f"Jersey Airport gust {jj['gust']} mph at {local_hm(jj['t'])} (threshold {GUST_MPH})",
                     "title": f"Jersey Weather Chat: gusts {jj['gust']} mph at Jersey Airport",
                     "body": f"Gusts of {jj['gust']} mph recorded at Jersey Airport at {local_hm(jj['t'])}. Take care outdoors. {FOOTER}"})
    if pws:
        top = sorted(pws, key=lambda s: -s["gust"])
        many = [s for s in top if s["gust"] >= PWS_MULTI_MPH]
        if top[0]["gust"] >= PWS_SINGLE_MPH or len(many) >= PWS_MULTI_COUNT:
            names = ", ".join(f"{s['name']} {s['gust']}" for s in top[:3])
            trig.append({"rule": "weather-station-gust", "value": top[0]["gust"], "unit": "mph",
                         "text": f"Weather stations: highest {top[0]['gust']} mph ({top[0]['id']} {top[0]['name']}); "
                                 f"{len(many)} station(s) at {PWS_MULTI_MPH}+ (thresholds: 1 at {PWS_SINGLE_MPH} or {PWS_MULTI_COUNT} at {PWS_MULTI_MPH})",
                         "title": f"Jersey Weather Chat: gusts up to {top[0]['gust']} mph locally",
                         "body": f"Local weather stations are recording gusts up to {top[0]['gust']} mph ({names} mph). Take care outdoors. {FOOTER}"})
    for icao in ("EGJB", "EGJA"):
        o = (metars.get(icao) or [None])[-1]
        if o and o["t"] >= fresh and (o["gust"] or 0) >= GUST_MPH:
            trig.append({"rule": "upstream-gust", "value": o["gust"], "unit": "mph",
                         "text": f"{AIRPORTS[icao]} gust {o['gust']} mph at {local_hm(o['t'])} (threshold {GUST_MPH}) - strongest winds may be approaching Jersey",
                         "title": "Jersey Weather Chat: strongest winds approaching",
                         "body": f"{AIRPORTS[icao]} recorded gusts of {o['gust']} mph at {local_hm(o['t'])}. The strongest winds may reach Jersey soon. {FOOTER}"})
            break
    hits = {k: v for k, v in fc.items() if v["gust"] >= GUST_MPH}
    if len(hits) >= FORECAST_MIN_MODELS:
        peak = max(hits.values(), key=lambda v: v["gust"])
        lo = min(v["gust"] for v in hits.values())
        trig.append({"rule": "forecast-gust", "value": peak["gust"], "unit": "mph",
                     "text": f"Forecast: {len(hits)} of {len(fc)} models give gusts {GUST_MPH}+ mph in the next {FORECAST_HOURS_AHEAD} h "
                             f"(" + ", ".join(f"{k} {v['gust']}" for k, v in hits.items()) + ")",
                     "title": "Jersey Weather Chat: damaging gusts expected soon",
                     "body": f"{len(hits)} of {len(fc)} weather models show gusts of {lo}-{peak['gust']} mph in Jersey within the next "
                             f"{FORECAST_HOURS_AHEAD} hours. Take care outdoors. {FOOTER}"})
    if fall is not None and fall >= PRESSURE_FALL_HPA:
        trig.append({"rule": "pressure-fall", "value": fall, "unit": "hPa",
                     "text": f"Pressure at Jersey Airport fell {fall} hPa in 3 hours (threshold {PRESSURE_FALL_HPA})",
                     "title": "Jersey Weather Chat: pressure falling very rapidly",
                     "body": f"Pressure at Jersey Airport has fallen {fall} hPa in 3 hours, a sign of a deepening storm nearby. {FOOTER}"})
    return trig

def readings_text(metars, fc, pws, fall, pws_note):
    lines = []
    for icao, name in AIRPORTS.items():
        o = (metars.get(icao) or [None])[-1]
        if not o:
            lines.append(f"{name} ({icao}): no report")
            continue
        g = f" gusting {o['gust']}" if o["gust"] else ""
        q = f", QNH {o['qnh']} hPa" if o["qnh"] else ""
        d = o["dir"] + ("\u00b0" if o["dir"].isdigit() else "")
        lines.append(f"{name} ({icao}) {local_hm(o['t'])}: wind {d} {o['speed']} mph{g}{q}")
    if fall is not None:
        lines.append(f"Jersey pressure change over 3 h: {'-' if fall > 0 else '+'}{abs(fall)} hPa")
    if fc:
        lines.append(f"Forecast max gust next {FORECAST_HOURS_AHEAD} h: " + ", ".join(f"{k} {v['gust']}" for k, v in fc.items()) + " mph")
    if pws:
        top = sorted(pws, key=lambda s: -s["gust"])[:6]
        lines.append(f"Weather stations ({len(pws)} reporting, {pws_note}): " + ", ".join(f"{s['name']} ({s['id']}) {s['gust']}" for s in top) + " mph")
    else:
        lines.append(f"Weather stations: {pws_note}")
    return "\n".join(lines)

def post(payload):
    r = requests.post(API_URL, data=json.dumps(payload), timeout=60)
    print("Apps Script replied:", r.text[:200])
    try:
        return r.json()
    except Exception:
        return {}

def main():
    mode = (os.environ.get("STORM_WATCH") or "dry").strip().lower()
    test_email = (os.environ.get("TEST_EMAIL") or "").strip().lower() == "true"
    admin_key = os.environ.get("JWC_ADMIN_KEY", "").strip()
    wu_key = os.environ.get("WU_API_KEY", "").strip()
    if not admin_key:
        print("ERROR: the JWC_ADMIN_KEY secret is missing.")
        sys.exit(1)
    if mode == "off" and not test_email:
        print("Storm Watch is switched OFF (repository variable STORM_WATCH=off). Nothing checked.")
        return
    print(f"Storm Watch mode: {mode.upper()}" + (" + TEST EMAIL requested" if test_email else ""))

    state = load_json(STATE_FILE, {"lastFired": {}, "pws": {}})
    before = json.dumps(state, sort_keys=True)
    now = now_utc()

    try:
        metars = fetch_metars()
    except Exception as ex:
        print("METAR fetch failed:", str(ex)[:150]); metars = {}
    try:
        fc = fetch_forecast()
    except Exception as ex:
        print("Forecast fetch failed:", str(ex)[:150]); fc = {}
    fall = pressure_fall(metars.get("EGJJ") or [])

    # weather stations only when something is brewing, and not too often
    wstate = load_json(WARNINGS_STATE, {})
    land_warning = any(not w.get("sea") for w in (wstate.get("warnings") or {}).values())
    jj = (metars.get("EGJJ") or [None])[-1]
    fc_med = statistics.median([v["gust"] for v in fc.values()]) if fc else 0
    brewing = land_warning or fc_med >= BREWING_MPH or (jj and (jj["gust"] or 0) >= BREWING_MPH)
    pws, pws_note = [], ""
    last_pws = state.get("pws") or {}
    last_pws_t = datetime.datetime.fromisoformat(last_pws["at"].replace("Z", "+00:00")) if last_pws.get("at") else None
    if not wu_key:
        pws_note = "not checked (no WU_API_KEY secret)"
    elif not (brewing or test_email):
        pws_note = f"not checked (nothing brewing: no land warning, gusts under {BREWING_MPH} mph)"
    elif last_pws_t and now - last_pws_t < datetime.timedelta(minutes=PWS_EVERY_MINUTES) and not test_email:
        pws = last_pws.get("readings") or []
        pws_note = f"from the check at {local_hm(last_pws_t)}"
    else:
        pws = fetch_pws(wu_key)
        state["pws"] = {"at": iso(now), "readings": pws}
        pws_note = "checked now"

    readings = readings_text(metars, fc, pws, fall, pws_note)
    print("---- readings ----\n" + readings + "\n------------------")

    triggers = evaluate(metars, fc, pws, fall)
    for t in triggers:
        print("  RULE MET:", t["text"])
    # cooldown / escalation
    fresh, cooling = [], []
    for t in triggers:
        last = (state.get("lastFired") or {}).get(t["rule"])
        if last:
            age = now - datetime.datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
            step = ESCALATE_HPA if t["unit"] == "hPa" else ESCALATE_MPH
            if age < datetime.timedelta(hours=COOLDOWN_HOURS) and t["value"] < last["value"] + step:
                print(f"  {t['rule']}: already suggested {int(age.total_seconds() // 60)} min ago - cooling down")
                cooling.append(t["text"] + f" [already suggested {local_hm(datetime.datetime.fromisoformat(last['at'].replace('Z', '+00:00')))}]")
                continue
        fresh.append(t)

    if fresh or test_email:
        headline = fresh[0] if fresh else {"title": "Jersey Weather Chat: TEST alert",
                                           "body": "This is a test of the Storm Watch approval email. " + FOOTER, "rule": "test", "text": "Test requested by hand"}
        payload = {"action": "suggest", "key": admin_key, "test": test_email,
                   "rules": [t["rule"] for t in fresh] or ["test"],
                   "reasons": [t["text"] for t in fresh] or ["Test email requested by hand - no rule needed"],
                   "readings": readings + ("\nAlso met, still cooling down: " + " | ".join(cooling) if cooling else ""),
                   "title": headline["title"], "body": headline["body"]}
        if test_email:
            print("Sending a TEST suggestion email.")
            post(payload)
        elif mode == "live":
            print("Emailing a suggestion:", headline["title"])
            res = post(payload)
            if res.get("status") != "ok":
                print("WARNING: the suggestion was not accepted - nothing recorded as fired, will retry next run.")
                fresh = []
        else:
            print("DRY RUN - would have emailed:", headline["title"], "| recording it in AlertLog as a dry run.")
            payload.update(action="log", source="storm-watch (dry run)", rule=", ".join(payload["rules"]),
                           reason=" | ".join(payload["reasons"]), status="dry run - no email sent")
            post(payload)
        if not test_email:
            for t in fresh:
                state.setdefault("lastFired", {})[t["rule"]] = {"at": iso(now), "value": t["value"]}
    else:
        print("No rule met - nothing to suggest.")

    if json.dumps(state, sort_keys=True) != before:
        save_json(STATE_FILE, state)
        print("State updated.")

if __name__ == "__main__":
    main()
