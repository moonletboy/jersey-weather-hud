#!/usr/bin/env python3
"""
Jersey Met warning alerts - push notification sender for the Jersey Weather Chat HUD.

MODE=check (default) - run by .github/workflows/warning-alerts.yml every ~10 minutes:
  1. publishes data/push-config.json (the public VAPID key the ALERT ME bar needs);
  2. reads gov.je's own warning data feed (JerseyWarningStatusXML.json - the file the
     gov.je warnings page itself loads; free, no ZenRows). If that feed can't be read,
     it falls back to ?action=warnings from the Apps Script (the old page-reading method);
  3. takes EACH warning separately (colour, area, type, issue time, message)
     and compares with data/warnings-state.json;
  4. sends a push to every subscribed device ONLY for a warning that is NEW or UPGRADED
     (a land warning and a shipping-area warning are tracked separately, so a new
     yellow land warning still alerts while an orange at-sea warning is in force);
  5. removes subscriptions the push services report as dead.

  Safeguard: a warning only counts as cancelled or lowered once TWO separate gov.je
  fetches agree. One bad fetch that briefly shows "no warnings" can therefore never
  make the next fetch look like a brand-new warning and alert everyone twice.

MODE=test    - send-test-alert.yml: TEST_MESSAGE to the owner's device(s) only.
MODE=special - send-special-alert.yml: SPECIAL_TITLE / SPECIAL_BODY to everyone
               (AUDIENCE=all) or to the owner only (AUDIENCE=owner). LOG_ID, if
               given, is the Storm Watch suggestion this send answers.

Every send is also reported to the Apps Script (action "log") for the AlertLog sheet;
if that isn't set up yet the report is simply ignored and nothing else is affected.

Settings come from environment variables (GitHub secrets):
  VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT, JWC_ADMIN_KEY
"""
import json, os, re, sys, datetime, hashlib
import requests
from pywebpush import webpush, WebPushException

FEED_URL = "https://prodgojweatherstorage.blob.core.windows.net/data/JerseyWarningStatusXML.json"
# (flag, data, hazard word) for each warning type in the feed. ThunderstormWarningALD is left out:
# it appears to be Alderney's thunderstorm warning, not Jersey's (it is still printed in the log).
FEED_TYPES = [("WindWarning", "WindWarningData", "wind"), ("ThunderWarning", "ThunderWarningData", "thunderstorm"),
              ("TideWarning", "TideWarningData", "tide"), ("SnowWarning", "SnowWarningData", "snow"),
              ("SnowAlert", "SnowAlertData", "snow"), ("IceWarning", "IceWarningData", "ice")]
API_URL = "https://script.google.com/macros/s/AKfycbzqw6EZ01rKNo8FJrP-VHwpNCRe9AWV2FAeVH3AvVzX2wXWpuprFNRQngIgJC0K08FHcg/exec"
SITE_URL = "https://www.jerseyweatherchat.com/index.html?source=push"
STATE_FILE = "data/warnings-state.json"
CONFIG_FILE = "data/push-config.json"
LEVEL = {"none": 0, "yellow": 1, "orange": 2, "amber": 2, "red": 3}
COLOUR_WORD = {"yellow": "YELLOW", "orange": "ORANGE", "amber": "ORANGE", "red": "RED"}   # Jersey Met says Orange, not amber
FOOTER = "Automated alert. The warning is officially issued by Jersey Met: see gov.je."
PUSH_TTL = 6 * 3600                 # keep trying to deliver for 6 hours if a phone is off
PUSH_HEADERS = {"Urgency": "high"}  # tells Android to wake the phone now rather than batch it later
FETCHES_TO_CONFIRM_DROP = 2         # separate gov.je fetches that must agree before a warning counts as lowered/cancelled

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        print(f"ERROR: the {name} secret is missing or empty.")
        sys.exit(1)
    return v

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

# ---------- reading the gov.je text ----------
# The page text looks like:
#   "There is currently 1 warning in force Orange - Channel Islands Shipping Area Wind Warning
#    Issued at 03:07 UTC Wednesday 21 October 2026 W to NW ..."
WARN_RE = re.compile(
    r"\b(Yellow|Orange|Amber|Red)\s*[-\u2013\u2014:]\s*"          # colour and dash
    r"([A-Z][^.]{1,80}?)\s+[Ww]arning\s+"                          # name, e.g. "Channel Islands Shipping Area Wind"
    r"[Ii]ssued\s+(?:at\s+)?(\d{1,2}[:.]\d{2})\s*(UTC|GMT|BST)?"   # issue time
    r"\s*(?:on\s+)?((?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+\d{1,2}\s+[A-Z][a-z]+)?")
HAZARD_RE = re.compile(r"(snow and ice|coastal flooding|thunderstorms?|thunder|wind|rain|snow|ice|fog|heat|cold|lightning|hail|gale|flood(?:ing)?|tidal|tide|coastal|storm|weather)$", re.I)
# different words for the same kind of warning, so a renamed warning is still recognised as the same one
HAZARD_SAME = {"thunder": "thunderstorm", "thunderstorms": "thunderstorm", "gale": "wind", "tidal": "tide",
               "coastal": "tide", "coastal flooding": "tide", "flooding": "flood", "snow and ice": "snow"}

def hazard_of(name, default="weather"):
    m = HAZARD_RE.search(name)
    h = m.group(1).lower() if m else default
    return HAZARD_SAME.get(h, h), m

def clean_issued(text):
    t = re.sub(r"^\s*issued\s+(?:at\s+)?", "", str(text or ""), flags=re.I)
    t = re.sub(r"[\s.]+$", "", t)            # trailing full stop
    t = re.sub(r"\s+\d{4}$", "", t)          # trailing year
    return t.strip()
SEA_RE = re.compile(r"shipping|sea area|marine|coastal waters|at sea", re.I)

def parse_warnings(text):
    out, seen = [], set()
    t = " ".join((text or "").split())
    for m in WARN_RE.finditer(t):
        colour = m.group(1).lower()
        if colour == "amber":
            colour = "orange"
        name = re.sub(r"\s*[-\u2013\u2014]\s*$", "", m.group(2).strip())
        key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        hazard, hm = hazard_of(name)
        area = name[:hm.start()].strip(" -\u2013") if hm else name
        issued = " ".join(x for x in (m.group(3).replace(".", ":"), m.group(4) or "", m.group(5) or "") if x)
        out.append({"key": key, "colour": colour, "level": LEVEL[colour], "name": name,
                    "area": area or "Jersey", "hazard": hazard, "sea": bool(SEA_RE.search(name)), "issued": issued})
    return out

def build_message(w, kind):
    colour = COLOUR_WORD.get(w["colour"], w["colour"].upper())
    title = f"Jersey Met: {colour} {w['hazard']} warning" + (" (at sea)" if w["sea"] else "") + (" UPGRADED" if kind == "upgraded" else "")
    where = (w["area"] + " - at sea, not a land warning") if w["sea"] else ("For " + w["area"])
    msg = w.get("message", "").strip()
    if len(msg) > 140:
        msg = msg[:137].rsplit(" ", 1)[0] + "..."
    body = where + "." + (" " + msg if msg else "") + (f" Issued {w['issued']}." if w["issued"] else "") + " " + FOOTER
    tag = "jwc-w-" + hashlib.md5(w["key"].encode()).hexdigest()[:8]   # an upgrade replaces that warning's earlier alert
    return {"title": title, "body": body, "url": SITE_URL, "tag": tag}

def build_generic_message(severity, kind):
    colour = COLOUR_WORD.get(severity, severity.upper())
    return {"title": f"Jersey Met: {colour} warning" + (" UPGRADED" if kind == "upgraded" else ""),
            "body": "A Jersey Met weather warning has been issued. " + FOOTER, "url": SITE_URL, "tag": "jwc-w-generic"}

# ---------- talking to the Apps Script ----------
def fetch_feed():
    """gov.je's own warning data. Returns the same shape as the Apps Script result plus a ready-made
    'parsed' list, or raises if the feed can't be used (the caller then falls back)."""
    r = requests.get(FEED_URL, params={"t": int(datetime.datetime.now().timestamp() // 60)}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("warningEnabled") is False:
        raise ValueError("feed says warningEnabled=false")
    if "WindWarning" not in d or "lastChecked" not in d:
        raise ValueError("feed layout not recognised")
    if d.get("ThunderstormWarningALD"):
        print("  (feed also has an Alderney thunderstorm warning - not alerted:",
              json.dumps(d.get("ThunderstormWarningALDData"), ensure_ascii=False)[:200], ")")
    parsed, seen = [], set()
    for flag, data_key, hazard_word in FEED_TYPES:
        w = d.get(data_key) if d.get(flag) else None
        if not w:
            continue
        colour = str(w.get("Colour", "")).strip().lower()
        colour = "orange" if colour in ("amber", "") or colour not in LEVEL else colour
        name = " ".join(str(w.get("WarningType") or (hazard_word.title())).split())
        key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
        if key in seen:
            key += " " + flag.lower()
        seen.add(key)
        hazard, hm = hazard_of(name, hazard_word)
        area = name[:hm.start()].strip(" -\u2013") if hm else name
        issued = clean_issued(w.get("Issued", ""))
        parsed.append({"key": key, "colour": colour, "level": LEVEL[colour], "name": name,
                       "area": area or "Jersey", "hazard": hazard, "sea": bool(SEA_RE.search(name)),
                       "issued": issued, "message": " ".join(str(w.get("WarningMessage") or "").split())})
    top = max([p["level"] for p in parsed] + [0])
    text = " ".join(f"{p['colour'].title()} - {p['name']} Warning Issued at {p['issued']} {p['message']}" for p in parsed)
    return {"ok": True, "source": "gov.je data feed", "severity": next(k for k, v in LEVEL.items() if v == top),
            "text": text, "hash": hashlib.md5(text.encode()).hexdigest()[:12],
            "checkedAt": d.get("lastChecked", ""), "parsed": parsed}

def fetch_warnings():
    r = requests.get(API_URL, params={"action": "warnings"}, timeout=60)
    r.raise_for_status()
    return r.json()

def fetch_subscribers(admin_key, owner_only=False):
    params = {"action": "subscribers", "key": admin_key}
    if owner_only:
        params["owner"] = "1"
    r = requests.get(API_URL, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        print("ERROR: subscribers request refused:", data.get("message"))
        sys.exit(1)
    return data.get("subscriptions", [])

def prune(admin_key, endpoints):
    if not endpoints:
        return
    r = requests.post(API_URL, data=json.dumps({"action": "prune", "key": admin_key, "endpoints": endpoints}), timeout=60)
    print("Pruned dead subscriptions:", r.text[:200])

def log_event(admin_key, event):
    """Report to the AlertLog sheet. Never allowed to break a send."""
    try:
        body = dict(event, action="log", key=admin_key, at=now_iso())
        r = requests.post(API_URL, data=json.dumps(body), timeout=30)
        print("Logged:", r.text[:120])
    except Exception as ex:
        print("Log skipped:", str(ex)[:120])

# ---------- sending ----------
def send_all(subs, message, vapid_private, vapid_subject, admin_key):
    sent, dead, failed = 0, [], 0
    payload = json.dumps(message)
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": s["keys"]}
        try:
            webpush(subscription_info=info, data=payload, vapid_private_key=vapid_private,
                    vapid_claims={"sub": vapid_subject}, ttl=PUSH_TTL, headers=dict(PUSH_HEADERS))
            sent += 1
        except WebPushException as ex:
            status = getattr(ex.response, "status_code", None)
            if status in (404, 410):
                dead.append(s["endpoint"])
            else:
                failed += 1
                print(f"  push failed ({status}): {str(ex)[:120]}")
        except Exception as ex:
            failed += 1
            print("  push error:", str(ex)[:120])
    print(f"Sent {sent}, failed {failed}, dead {len(dead)} of {len(subs)} subscriptions.")
    prune(admin_key, dead)
    return {"sent": sent, "failed": failed, "dead": len(dead), "of": len(subs)}

# ---------- the warning check ----------
def check(vapid_private, vapid_subject, admin_key):
    try:
        cur = fetch_feed()
    except Exception as ex:
        print("gov.je data feed not usable this run (" + str(ex)[:120] + ") - falling back to the Apps Script page check.")
        try:
            cur = fetch_warnings()
            cur["source"] = "Apps Script page check"
        except Exception as ex2:
            print("Could not fetch warnings this run either (leaving state unchanged):", ex2)
            return
    if not cur.get("ok") or cur.get("severity") == "unknown":
        print("Warnings check reported a problem (leaving state unchanged):", cur.get("error"))
        return

    prev = load_json(STATE_FILE, {"severity": "none", "hash": ""})
    sev = cur.get("severity", "none")
    fetch_id = cur.get("checkedAt", "")
    new_fetch = fetch_id != prev.get("lastFetch")
    migrating = "warnings" not in prev      # first run since per-warning tracking was added
    prev_level = LEVEL.get(prev.get("severity", "none"), 0)
    warnings = {k: dict(v) for k, v in (prev.get("warnings") or {}).items()}
    parsed = cur["parsed"] if "parsed" in cur else (parse_warnings(cur.get("text", "")) if sev != "none" else [])
    alerts = []   # (kind, message, description)
    fallback = 0
    # warnings at or below this level are recorded without an alert: on the first run of this version
    # (they were already in force), and on the first readable fetch after an "unreadable layout" alert
    quiet_level = prev_level if migrating else prev.get("fallback", 0)
    # when the data source changes (page check <-> gov.je feed) the same warning may carry a slightly
    # different name: match it to a recorded warning of the same type, sea/land and colour instead of re-alerting
    source = cur.get("source", "Apps Script page check")
    source_changed = source != prev.get("source", "Apps Script page check")

    print(f"Source: {cur.get('source', '?')}")
    print(f"Fetch {fetch_id} ({'new' if new_fetch else 'same copy as last run'}), overall {sev}, "
          f"{len(parsed)} warning(s) read: " + "; ".join(f"{w['colour']} {w['name']}" for w in parsed))

    if sev != "none" and not parsed:
        # The page shows a warning but its layout wasn't recognised: fall back to the overall colour,
        # alerting only if it is higher than anything already recorded, and leave the records alone.
        known = max([w["level"] for w in warnings.values()] + [prev.get("fallback", 0)] + ([prev_level] if migrating else []))
        fallback = max(known, LEVEL.get(sev, 0))   # remembered, so an unreadable page doesn't re-alert every fetch
        print("WARNING: could not read the individual warnings - page layout may have changed. Using the overall colour.")
        if LEVEL.get(sev, 0) > known:
            kind = "new" if known == 0 else "upgraded"
            alerts.append((kind, build_generic_message(sev, kind), f"{sev} (unreadable layout)"))
    else:
        seen = set()
        for w in parsed:
            seen.add(w["key"])
            old = warnings.get(w["key"])
            if old is None:
                def same_kind(o):
                    oh = HAZARD_SAME.get(o.get("hazard", ""), o.get("hazard", ""))
                    if oh == "weather":     # an older record whose type wasn't recognised: judge it by its name
                        oh = hazard_of(o.get("name", ""), "weather")[0]
                    return oh == w["hazard"] and bool(o.get("sea")) == w["sea"]
                def time_of(o):
                    t = re.search(r"\d{1,2}[:.]\d{2}", o.get("issued", ""))
                    return t.group(0).replace(".", ":") if t else None
                twin = any(same_kind(o) and (
                               (source_changed and o.get("level", 0) >= w["level"])
                               or (o.get("level") == w["level"] and time_of(o) and time_of(o) == time_of(w)))
                           for o in warnings.values())
                if w["level"] <= quiet_level:
                    print(f"  {w['name']}: already alerted/in force before - recorded, no alert")
                elif twin:
                    print(f"  {w['name']}: same warning as one already recorded (renamed or new data source) - recorded, no alert")
                else:
                    alerts.append(("new", build_message(w, "new"), f"{w['colour']} {w['name']}"))
                warnings[w["key"]] = dict(w, missing=0, lower=0)
            elif w["level"] > old["level"]:
                alerts.append(("upgraded", build_message(w, "upgraded"), f"{old['colour']} -> {w['colour']} {w['name']}"))
                warnings[w["key"]] = dict(w, missing=0, lower=0)
            elif w["level"] < old["level"]:
                old["lower"] = old.get("lower", 0) + (1 if new_fetch else 0)
                old["missing"] = 0
                if old["lower"] >= FETCHES_TO_CONFIRM_DROP:
                    print(f"  {w['name']}: lowered to {w['colour']} (confirmed by {old['lower']} fetches)")
                    warnings[w["key"]] = dict(w, missing=0, lower=0)
                else:
                    print(f"  {w['name']}: shows {w['colour']}, waiting for a second fetch before accepting the lower level")
            else:
                old.update(issued=w["issued"], missing=0, lower=0)
        for key in list(warnings):
            if key in seen:
                continue
            warnings[key]["missing"] = warnings[key].get("missing", 0) + (1 if new_fetch else 0)
            if warnings[key]["missing"] >= FETCHES_TO_CONFIRM_DROP:
                print(f"  {warnings[key]['name']}: gone from gov.je (confirmed by {warnings[key]['missing']} fetches) - removed")
                del warnings[key]
            else:
                print(f"  {warnings[key]['name']}: not on this fetch - keeping it until a second fetch agrees")

    if alerts:
        subs = fetch_subscribers(admin_key)
        for kind, msg, desc in alerts:
            print("Alert:", json.dumps(msg, ensure_ascii=False))
            res = send_all(subs, msg, vapid_private, vapid_subject, admin_key)
            log_event(admin_key, {"source": "jersey-met", "rule": "Jersey Met warning " + kind,
                                  "reason": desc, "title": msg["title"], "body": msg["body"],
                                  "status": "sent automatically", "sentCount": res["sent"], "deviceCount": res["of"]})
    else:
        print("No new or upgraded warnings - no alert.")

    overall = max([w["level"] for w in warnings.values()] + [fallback])
    state = {
        "severity": next((k for k, v in LEVEL.items() if v == overall and k != "amber"), "none"),
        "hash": cur.get("hash", ""),
        "warnings": warnings,
        "source": source,
        "fallback": fallback,
        "lastFetch": fetch_id,
        "snippet": " ".join((cur.get("text") or "").split())[:160],
        "checkedAt": fetch_id or now_iso(),
        "lastAlertAt": now_iso() if alerts else prev.get("lastAlertAt", ""),
        "lastAlertKind": alerts[-1][0] if alerts else prev.get("lastAlertKind", ""),
    }
    # Only save when something about the warnings changed. Saving makes GitHub rebuild the website, so
    # saving just because gov.je refreshed its feed (new timestamp, same warnings) would rebuild it every
    # few minutes. The fetch timestamp is still saved whenever a real change is saved, which is all the
    # "two fetches must agree" safeguard needs.
    volatile = ("lastFetch", "checkedAt", "snippet", "hash")
    core = lambda x: {k: v for k, v in x.items() if k not in volatile}
    if core(state) != core(prev) or not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, state)
        print("State updated.")
    else:
        print("Nothing changed - not saving (avoids rebuilding the website).")

def main():
    mode = os.environ.get("MODE", "check").strip() or "check"
    vapid_public = env("VAPID_PUBLIC_KEY")
    vapid_private = env("VAPID_PRIVATE_KEY")
    vapid_subject = env("VAPID_SUBJECT")
    admin_key = env("JWC_ADMIN_KEY")
    if not vapid_subject.startswith("mailto:"):
        print("ERROR: VAPID_SUBJECT must start with mailto: (e.g. mailto:you@example.com)")
        sys.exit(1)

    # public key for the website (only rewritten when it changes)
    cfg = {"publicKey": vapid_public}
    if load_json(CONFIG_FILE, {}) != cfg:
        save_json(CONFIG_FILE, cfg)
        print("Published", CONFIG_FILE)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%S")

    if mode == "test":
        text = os.environ.get("TEST_MESSAGE", "").strip() or "This is a test of the Jersey Weather Chat warning alerts."
        subs = fetch_subscribers(admin_key, owner_only=True)
        if not subs:
            print("No device is marked as the owner's. Open the site with #admin on the end and tap ALERT ME first.")
            sys.exit(1)
        # each test gets its own tag, so every test pops up fresh instead of quietly replacing the previous one
        msg = {"title": "Jersey Weather Chat: TEST", "body": text, "url": SITE_URL, "tag": "jwc-test-" + stamp}
        send_all(subs, msg, vapid_private, vapid_subject, admin_key)
        return

    if mode == "special":
        title = " ".join(os.environ.get("SPECIAL_TITLE", "").split())[:80] or "Jersey Weather Chat"
        body = " ".join(os.environ.get("SPECIAL_BODY", "").split())[:400]
        audience = os.environ.get("AUDIENCE", "owner").strip().lower()
        log_id = os.environ.get("LOG_ID", "").strip()
        if not body:
            print("No message was typed, so nothing was sent.")
            sys.exit(1)
        to_all = audience in ("all", "everyone")
        subs = fetch_subscribers(admin_key, owner_only=not to_all)
        if not subs:
            print("No devices to send to." + ("" if to_all else " (Owner only: open the site with #admin and tap ALERT ME.)"))
            sys.exit(1)
        msg = {"title": title, "body": body, "url": SITE_URL, "tag": "jwc-special-" + stamp}
        print(("Sending to EVERYONE: " if to_all else "Sending to the owner only: ") + json.dumps(msg, ensure_ascii=False))
        res = send_all(subs, msg, vapid_private, vapid_subject, admin_key)
        log_event(admin_key, {"source": "special", "id": log_id, "rule": "Special alert" + ("" if to_all else " (owner test)"),
                              "title": title, "body": body,
                              "status": ("sent" if to_all else "sent to owner only"), "sentCount": res["sent"], "deviceCount": res["of"]})
        return

    check(vapid_private, vapid_subject, admin_key)

if __name__ == "__main__":
    main()
