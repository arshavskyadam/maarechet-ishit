"""Pop-up notifications to the phones of people who turned them on in the site.

Runs after build.py in the hourly update. It needs two repository secrets:
  PUSH_TOKEN         the "מפתח התראות" value from the private settings sheet (Apps Script)
  VAPID_PRIVATE_KEY  the private half of the key pair whose public half is config.json's vapid_public
Without them it does nothing.

What is sent, per person, at most once each (a hashed log in docs/data/push_log.json):
  - 07:00–07:59: a morning summary of the day (lessons, yard duty, reminders)
  - any run: changes found in this run that land on today
  - any run after the morning: reminders and yard duty that start within the next hour
"""

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import fill, load_json, write_json  # noqa: E402
from scrape import ISRAEL_TZ  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HOURS = {0: "07:25", 1: "08:10", 2: "08:55", 3: "09:55", 4: "10:40", 5: "11:30", 6: "12:15",
         7: "13:40", 8: "14:25", 9: "15:15", 10: "16:00"}
LUNCH = "13:00"


def api(payload):
    req = urllib.request.Request(CONFIG["api_url"], data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mins(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def main():
    token, key = os.environ.get("PUSH_TOKEN", ""), os.environ.get("VAPID_PRIVATE_KEY", "")
    if not (token and key and CONFIG.get("api_url")):
        print("notifications: not set up, skipping")
        return
    from pywebpush import WebPushException, webpush

    now = datetime.now(ISRAEL_TZ)
    today, dow = now.date().isoformat(), (now.weekday() + 1) % 7  # Sunday = 0, as in the site
    feed = api({"action": "push_feed", "token": token})
    if not feed.get("ok"):
        print(f"notifications: feed refused ({feed.get('error')})")
        return
    staff = load_json(DOCS / "data" / "staff.json", {})
    ids = {p["name"]: p["id"] for p in staff.get("people", [])}
    duty = load_json(DOCS / "data" / "duty.json", {}).get("days", {}).get(today, {})
    log_path = DOCS / "data" / "push_log.json"
    log = load_json(log_path, {})
    log = {k: v for k, v in log.items() if v >= (now.date() - timedelta(days=3)).isoformat()}
    morning = now.hour == 7
    window = (now.hour * 60 + now.minute, now.hour * 60 + now.minute + 60)
    sent = dropped = 0

    for entry in feed["subs"]:
        name, sub = entry["name"], entry["sub"]
        data = load_json(DOCS / "data" / f"{ids.get(name, '')}.json", {})
        latest = data.get("latest", {})
        lessons = [l for l in latest.get("lessons", []) if l.get("date") == today and l.get("status") != "cancelled"]
        rems = [r for r in feed["reminders"] if r["name"] == name and (r["date"] == today if r["date"] else r["dow"] == dow)]
        duties = []
        for grade, g in duty.items():
            for s in g["slots"]:
                if name in s["people"]:
                    duties.append({"grade": grade, "hour": s["hour"], "role": s.get("role", "")})
        messages = []
        if morning:
            parts = []
            if lessons:
                first = min(lessons, key=lambda l: l["hour"])
                parts.append(f"{len({l['hour'] for l in lessons})} שעות הוראה, הראשונה ב-{first['start']} ({first['subject']})")
            else:
                parts.append("אין לך שיעורים היום")
            for d in sorted(duties, key=lambda d: d["hour"]):
                parts.append(f"תורנות: {d['role'] or 'תורן חצר'} · שכבה {d['grade']}' · " + (f"בהפסקה ({LUNCH})" if d["role"] else f"שעה {d['hour']}"))
            for r in sorted(rems, key=lambda r: r["hour"]):
                parts.append(f"תזכורת בשעה {r['hour']}: {r['text']}")
            messages.append(("morning", "☀️ היום שלך", "\n".join(parts)))
        else:
            for r in rems:
                t = HOURS.get(r["hour"], "")
                if t and window[0] <= mins(t) < window[1]:
                    messages.append((f"rem|{r['id']}", f"⏰ תזכורת · {t}", r["text"]))
            for d in duties:
                t = LUNCH if d["role"] else HOURS.get(d["hour"], "")
                if t and window[0] <= mins(t) < window[1]:
                    what = d["role"] or "תורן חצר"
                    messages.append((f"duty|{d['grade']}|{d['hour']}|{d['role']}", f"🛡️ {what} · {t}", f"שכבה {d['grade']}'"))
        for c in data.get("changelog", []):
            if c.get("date") == today and c.get("detected_at") == latest.get("scraped_at"):
                messages.append((f"chg|{c.get('kind')}|{c.get('text')}", "⚠️ שינוי במערכת להיום", f"{c.get('kind')}: {fill(c.get('text', ''))}"))

        for tag, title, body in messages:
            k = hashlib.sha1(f"{name}|{today}|{tag}".encode("utf-8")).hexdigest()[:16]
            if k in log:
                continue
            try:
                webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body, "tag": tag[:40], "url": CONFIG.get("site_url", "./")}, ensure_ascii=False),
                        vapid_private_key=key, vapid_claims={"sub": CONFIG.get("site_url") or "https://github.com"}, ttl=3600)
                log[k] = today
                sent += 1
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):  # the phone turned notifications off
                    api({"action": "push_drop", "token": token, "endpoint": sub.get("endpoint", "")})
                    dropped += 1
                    break
                print(f"notifications: one failed ({code})")
    write_json(log_path, log)
    print(f"notifications: {sent} sent to {len(feed['subs'])} phones, {dropped} removed")


if __name__ == "__main__":
    main()
