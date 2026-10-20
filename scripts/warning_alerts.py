#!/usr/bin/env python3
"""
Jersey Met warning alerts - push notification sender for the Jersey Weather Chat HUD.

Run by .github/workflows/warning-alerts.yml every ~10 minutes:
  1. publishes data/push-config.json (the public VAPID key the ALERT ME bar needs);
  2. fetches ?action=warnings from the Apps Script (cached there, so this costs no
     ZenRows credits however often it runs);
  3. compares with data/warnings-state.json and, ONLY for a NEW or UPGRADED warning,
     sends a web push to every subscribed device;
  4. removes subscriptions the push services report as dead.

Also used by send-test-alert.yml (MODE=test): sends one message to the owner's
device(s) only, so the whole chain can be proved without alerting the community.

Settings come from environment variables (GitHub secrets):
  VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT, JWC_ADMIN_KEY
  MODE = check (default) | test        TEST_MESSAGE = text for test mode
"""
import json, os, re, sys, datetime, hashlib
import requests
from pywebpush import webpush, WebPushException

API_URL = "https://script.google.com/macros/s/AKfycbzqw6EZ01rKNo8FJrP-VHwpNCRe9AWV2FAeVH3AvVzX2wXWpuprFNRQngIgJC0K08FHcg/exec"
SITE_URL = "https://www.jerseyweatherchat.com/index.html?source=push"
STATE_FILE = "data/warnings-state.json"
CONFIG_FILE = "data/push-config.json"
LEVEL = {"none": 0, "yellow": 1, "orange": 2, "amber": 2, "red": 3}
COLOUR_WORD = {"yellow": "YELLOW", "orange": "AMBER", "amber": "AMBER", "red": "RED"}
FOOTER = "Automated alert, check gov.je for the official warning."
PUSH_TTL = 6 * 3600   # keep trying to deliver for 6 hours if a phone is off

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

# ---------- working out what to say ----------
def describe(severity, text):
    """Title and issue time pulled out of the flattened gov.je text. The page layout
    changed in Aug 2026, so these are deliberately loose and always have a fallback."""
    t = " ".join((text or "").split())
    m = re.search(r"((?:yellow|amber|orange|red)\s+(?:weather\s+)?warning(?:\s+(?:of|for))?\s+[a-z ,/&-]{2,40}?)(?=[.:;]|\s+(?:issued|valid|from|until|\d)|$)", t, re.I)
    title = m.group(1).strip().rstrip(",") if m else f"{COLOUR_WORD.get(severity, severity).title()} weather warning"
    title = re.sub(r"\s+", " ", title)
    title = title[0].upper() + title[1:]
    m = re.search(r"issued\s*(?:at|on|:)?\s*([0-9]{1,2}[:.][0-9]{2}\s*(?:am|pm)?(?:\s*(?:on\s+)?[A-Za-z]{3,9}\s+\d{1,2}(?:\s+[A-Za-z]{3,9})?)?|[A-Za-z]{3,9}\s+\d{1,2}[^.;]{0,25}?\d{1,2}[:.]\d{2}\s*(?:am|pm)?)", t, re.I)
    issued = m.group(1).strip() if m else ""
    return title, issued

def build_message(severity, text, kind):
    title, issued = describe(severity, text)
    colour = COLOUR_WORD.get(severity, severity.upper())
    head = f"{colour} warning" + (" UPGRADED" if kind == "upgraded" else "")
    body = title + (f". Issued {issued}." if issued else ".") + " " + FOOTER
    return {"title": f"Jersey Met: {head}", "body": body, "url": SITE_URL, "tag": "jwc-warning"}

def decide(prev, cur):
    """Returns 'new', 'upgraded' or '' (no alert)."""
    p, c = LEVEL.get(prev.get("severity", "none"), 0), LEVEL.get(cur.get("severity", "none"), 0)
    if c == 0:
        return ""
    if p == 0:
        return "new"
    if c > p:
        return "upgraded"
    return ""   # same level (even if reworded) or downgraded: no alert

# ---------- talking to the Apps Script ----------
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

# ---------- sending ----------
def send_all(subs, message, vapid_private, vapid_subject, admin_key):
    sent, dead, failed = 0, [], 0
    payload = json.dumps(message)
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": s["keys"]}
        try:
            webpush(subscription_info=info, data=payload, vapid_private_key=vapid_private,
                    vapid_claims={"sub": vapid_subject}, ttl=PUSH_TTL)
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
    return sent

def main():
    mode = os.environ.get("MODE", "check").strip() or "check"
    vapid_public = env("VAPID_PUBLIC_KEY")
    vapid_private = env("VAPID_PRIVATE_KEY")
    vapid_subject = env("VAPID_SUBJECT")
    admin_key = env("JWC_ADMIN_KEY")
    if not vapid_subject.startswith("mailto:"):
        print("ERROR: VAPID_SUBJECT must start with mailto: (e.g. mailto:you@example.com)")
        sys.exit(1)

    # 1. public key for the website (only rewritten when it changes)
    cfg = {"publicKey": vapid_public}
    if load_json(CONFIG_FILE, {}) != cfg:
        save_json(CONFIG_FILE, cfg)
        print("Published", CONFIG_FILE)

    if mode == "test":
        text = os.environ.get("TEST_MESSAGE", "").strip() or "This is a test of the Jersey Weather Chat warning alerts."
        subs = fetch_subscribers(admin_key, owner_only=True)
        if not subs:
            print("No device is marked as the owner's. Open the site with #admin on the end and tap ALERT ME first.")
            sys.exit(1)
        msg = {"title": "Jersey Weather Chat: TEST", "body": text + " " + FOOTER, "url": SITE_URL, "tag": "jwc-test"}
        send_all(subs, msg, vapid_private, vapid_subject, admin_key)
        return

    # 2. current warnings
    try:
        cur = fetch_warnings()
    except Exception as ex:
        print("Could not fetch warnings this run (leaving state unchanged):", ex)
        return
    if not cur.get("ok") or cur.get("severity") == "unknown":
        print("Warnings check reported a problem (leaving state unchanged):", cur.get("error"))
        return
    prev = load_json(STATE_FILE, {"severity": "none", "hash": ""})
    sev, h = cur.get("severity", "none"), cur.get("hash", "")
    kind = decide(prev, cur)
    print(f"Previous: {prev.get('severity')} [{prev.get('hash','')}]  Now: {sev} [{h}]  -> {kind or 'no alert'}")

    # 3. alert if needed
    if kind:
        msg = build_message(sev, cur.get("text", ""), kind)
        print("Alert:", json.dumps(msg, ensure_ascii=False))
        subs = fetch_subscribers(admin_key)
        send_all(subs, msg, vapid_private, vapid_subject, admin_key)

    # 4. remember what we saw (only written when something changed, so the repo stays quiet)
    state = {
        "severity": sev, "hash": h,
        "snippet": " ".join((cur.get("text") or "").split())[:160],
        "checkedAt": cur.get("checkedAt", now_iso()),
        "lastAlertAt": now_iso() if kind else prev.get("lastAlertAt", ""),
        "lastAlertKind": kind or prev.get("lastAlertKind", ""),
    }
    old = dict(prev); old.pop("checkedAt", None)
    new = dict(state); new.pop("checkedAt", None)
    if old != new or not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, state)
        print("State updated.")

if __name__ == "__main__":
    main()
