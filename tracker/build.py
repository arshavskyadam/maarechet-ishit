"""Scan the school timetable once and build every instructor's personal page data.

Usage: python3 tracker/build.py [--offline] [--morning]

Output (served by GitHub Pages from docs/):
  docs/index.html           the app; the visitor picks their name once
  docs/data/staff.json      who can be picked
  docs/data/<id>.json       one instructor's lessons, changes and journal
"""

import argparse
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (diff, dm, fill, homeroom_line, load_json, morning_summary, notify_ntfy,  # noqa: E402
                 todays_changes, write_json)
from scrape import ISRAEL_TZ, collect, homeroom_info, scrape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
MAX_CHANGELOG = 150
HISTORY_DAYS = 90
WEEKDAYS = {"א": 0, "ב": 1, "ג": 2, "ד": 3, "ה": 4, "ו": 5}


def person_id(person):
    return hashlib.sha1((person.get("name") or person["as_given"]).encode("utf-8")).hexdigest()[:10]


def ntfy_topic(person):
    """Per-person topic derived from a secret, so it can't be guessed from the public repository."""
    salt = os.environ.get("NTFY_SALT", "")
    if not salt:
        return None
    digest = hmac.new(salt.encode(), (person.get("name") or person["as_given"]).encode("utf-8"), hashlib.sha256)
    return "techni-" + digest.hexdigest()[:20]


def load_extras():
    """Extra lessons the school site doesn't list, from a Google Sheet (CSV) and/or extras.json."""
    rows = list(load_json(ROOT / "extras.json", []))
    url = CONFIG.get("extras_csv_url")
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (lesson-tracker)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8-sig")
            for i, row in enumerate(csv.reader(io.StringIO(text))):
                if i == 0 or len(row) < 4 or not row[0].strip():
                    continue
                row = [c.strip() for c in row] + [""] * 7
                rows.append({"name": row[0], "day": row[1], "hours": row[2], "subject": row[3],
                             "class": row[4], "room": row[5], "date": row[6]})
        except Exception as e:  # a broken sheet must not stop the timetable update
            print(f"extras sheet not loaded: {e}")
    by_name = {}
    for r in rows:
        nums = [int(n) for n in re.findall(r"\d+", str(r.get("hours", "")))]
        if not nums or not r.get("subject"):
            continue
        extra = {"hours": list(range(nums[0], nums[-1] + 1)), "subject": r["subject"],
                 "classes": [r["class"]] if r.get("class") else [], "rooms": [r["room"]] if r.get("room") else []}
        m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", r.get("date", "") or "")
        if m:
            extra["date"] = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        elif (r.get("day") or "")[:1] in WEEKDAYS:
            extra["weekday"] = WEEKDAYS[r["day"][:1]]
        else:
            continue
        by_name.setdefault(" ".join(r["name"].split()), []).append(extra)
    return by_name


def build_person(person, raw, extras, now, offline, morning):
    pid = person_id(person)
    path = DOCS / "data" / f"{pid}.json"
    saved = load_json(path, {})
    previous = saved.get("latest", {})
    changelog = saved.get("changelog", [])
    history = {d["date"]: d for d in saved.get("history", [])}
    today = now.date().isoformat()
    name = person.get("name")
    display = name or " ".join(reversed(person["as_given"].split(" ", 1)))
    homerooms = [h for h in [person.get("homeroom")] + person.get("homeroom_extra", []) if h]

    if offline:
        if not previous:
            return None
        latest, found = previous, []
    else:
        mine = extras.get(name or "", []) + extras.get(person["as_given"], [])
        latest = scrape(name or "\u0000", raw=raw, extra_lessons=mine, homeroom=homerooms[0] if homerooms else None)
        latest["homerooms"] = [homeroom_info(raw, h) for h in homerooms]
        # The page's "end of day" table shows the homeroom classes.
        latest["class_end"] = {}
        for hr in latest["homerooms"]:
            for day, info in hr["days"].items():
                latest["class_end"].setdefault(day, []).append(
                    {"class": hr["class"], "now": info.get("last_lesson"), "regular": info.get("regular_end")})
        found = diff(previous, latest, today) if previous else []
        for i, hr in enumerate(latest["homerooms"][1:], start=1):
            old = (previous.get("homerooms") or [None] * (i + 1))[i] if previous else None
            if old:
                found += diff({"lessons": [], "days": previous.get("days", []), "homeroom": old, "class_changes": []},
                              {"lessons": [], "days": latest["days"], "homeroom": hr, "class_changes": []}, today)
        for f in found:
            f["detected_at"] = latest["scraped_at"]
        changelog = (found + changelog)[:MAX_CHANGELOG]
        for d in latest["days"]:
            history[d["date"]] = {**d, "recorded_at": latest["scraped_at"],
                                  "lessons": [l for l in latest["lessons"] if l["date"] == d["date"]]}
        cutoff = (now.date() - timedelta(days=HISTORY_DAYS)).isoformat()
        history = {k: v for k, v in history.items() if k >= cutoff}

    # Notifications: one morning summary a day, later only changes that land on today.
    summary_sent = saved.get("summary_sent", "")
    today_found = todays_changes(found, today)
    title = text = None
    if not offline and morning and summary_sent != today:
        text = morning_summary(latest, today)
        for hr in latest.get("homerooms", [])[1:]:
            if text:
                text += "\n" + "\n".join(homeroom_line(hr, today))
        title, summary_sent = "☀️ היום שלך", today
    elif today_found:
        title, text = "⚠️ שינוי במערכת להיום", "\n".join(f"• {f['kind']}: {fill(f['text'])}" for f in today_found[:12])
    topic = ntfy_topic(person)
    sent = ""
    if text and topic:
        sent = "sent" if notify_ntfy(topic, title, text, CONFIG.get("site_url", "")) else "FAILED"

    write_json(path, {
        "teacher": display, "source": CONFIG["site"], "roles": person.get("roles", []),
        "ends_title": "שעת סיום · " + ("כיתות החינוך" if len(homerooms) > 1 else "כיתת החינוך") if homerooms else "",
        "summary_sent": summary_sent, "latest": latest, "changelog": changelog,
        "history": [history[k] for k in sorted(history)],
    })
    active = sum(1 for l in latest["lessons"] if l.get("status") != "cancelled")
    print(f"{display}: {active} lessons, {len(found)} changes ({len(today_found)} today)"
          + (f", notification {sent}" if sent else ""))
    return {"id": pid, "name": display, "roles": person.get("roles", []), "homerooms": homerooms}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="rebuild the site from saved data without scanning")
    ap.add_argument("--morning", action="store_true", help="send the daily summary (default before 08:00)")
    args = ap.parse_args()

    now = datetime.now(ISRAEL_TZ)
    raw, extras = None, {}
    if not args.offline:
        raw = collect(now=now)
        if not raw["classes"] or not raw["days"]:
            sys.exit("scan returned no classes or days; keeping the previous data")
        extras = load_extras()
    staff = json.loads((ROOT / "staff.json").read_text(encoding="utf-8"))["staff"]
    morning = args.morning or now.hour < 8
    people = [p for p in (build_person(s, raw, extras, now, args.offline, morning) for s in staff) if p]
    if people:
        write_json(DOCS / "data" / "staff.json", {"updated": now.isoformat(timespec="minutes"), "people": people})
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text((ROOT / "web" / "index.html").read_text(encoding="utf-8"), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    print(f"built {len(people)} pages at {now.isoformat(timespec='minutes')}")


if __name__ == "__main__":
    main()
