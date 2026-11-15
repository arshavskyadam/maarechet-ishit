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
import urllib.parse
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
WEEKDAYS = {"ראשון": 0, "שני": 1, "שלישי": 2, "רביעי": 3, "חמישי": 4, "שישי": 5,
            "א": 0, "ב": 1, "ג": 2, "ד": 3, "ה": 4, "ו": 5}


def person_id(person):
    return hashlib.sha1((person.get("name") or person["as_given"]).encode("utf-8")).hexdigest()[:10]


def pin_hash(pid, pin):
    return hashlib.sha256(f"{pid}:{pin}".encode()).hexdigest()


def ntfy_topic(person):
    """Per-person topic derived from a secret, so it can't be guessed from the public repository."""
    salt = os.environ.get("NTFY_SALT", "")
    if not salt:
        return None
    digest = hmac.new(salt.encode(), (person.get("name") or person["as_given"]).encode("utf-8"), hashlib.sha256)
    return "techni-" + digest.hexdigest()[:20]


HEADERS = {"name": "שם המדריך", "type": "סוג", "day": "יום קבוע", "date": "או תאריך חד-פעמי",
           "from": "משעה", "to": "עד שעה", "subject": "נושא / תלמיד", "class": "כיתה", "room": "חדר",
           "notes": "הערות"}


def sheet_csv_url(sheet_id, tab):
    return (f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet="
            + urllib.parse.quote(tab))


def read_sheet_tab(tab, must_have):
    """Rows of one tab of the management sheet as dicts keyed by the header row, or None if unreachable.

    Tries the main sheet, then the older one. A tab is accepted only if its header has `must_have`:
    Google returns the first tab when a tab name doesn't exist, which must not be read as data.
    """
    for sheet in [CONFIG.get("sheet_id"), CONFIG.get("fallback_sheet_id")]:
        if not sheet:
            continue
        try:
            req = urllib.request.Request(sheet_csv_url(sheet, tab), headers={"User-Agent": "Mozilla/5.0 (lesson-tracker)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                table = list(csv.reader(io.StringIO(resp.read().decode("utf-8-sig"))))
        except Exception as e:  # a broken sheet must not stop the timetable update
            print(f"sheet {sheet[:8]}… tab '{tab}' not loaded: {e}")
            continue
        header = [h.strip() for h in table[0]] if table else []
        if must_have not in header:
            print(f"sheet {sheet[:8]}… has no tab '{tab}'")
            continue
        return [{h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header) if h} for row in table[1:]]
    return None


def site_teachers(raw):
    names = set()
    for _, cell in [e for entries in raw["slots"].values() for e in entries] + raw["regular"]:
        for l in cell["lessons"]:
            names.update(t.strip() for t in l["teacher"].split(",") if t.strip())
    return names


def load_staff(raw):
    """People and teams from the sheet's staff tab, falling back to staff.json."""
    local = json.loads((ROOT / "staff.json").read_text(encoding="utf-8"))
    rows = read_sheet_tab(CONFIG.get("staff_tab", "אנשי צוות"), "שם (כמו בשחף: משפחה ואז פרטי)")
    if not rows:
        return local["staff"], local.get("teams", {})
    known = site_teachers(raw) if raw else set()
    by_tokens = {frozenset(t.split()): t for t in known}
    staff, teams = [], {}
    for r in rows:
        typed = " ".join(r.get("שם (כמו בשחף: משפחה ואז פרטי)", "").split())
        if not typed or r.get("פעיל", "כן") == "לא":
            continue
        # Exact name as on the site, or the same words in another order (e.g. first name first).
        name = typed if typed in known else by_tokens.get(frozenset(typed.split()))
        if raw and not name:
            print(f"note: '{typed}' has no lessons on the school site")
        homerooms = [h for h in [r.get("כיתת חינוך", ""), r.get("כיתת חינוך נוספת", "")] if h]
        person = {"name": name, "as_given": typed, "display": name or typed,
                  "roles": [x.strip() for x in r.get("תפקיד", "").split(",") if x.strip()]}
        if homerooms:
            person["homeroom"], person["homeroom_extra"] = homerooms[0], homerooms[1:]
        staff.append(person)
        team = r.get("צוות", "")
        if team:
            t = teams.setdefault(team, {"lead": None, "members": []})
            t["members"].append(person["display"])
            if r.get("ראש צוות") == "כן":
                t["lead"] = person["display"]
    print(f"staff from sheet: {len(staff)} people, {len(teams)} teams")
    return staff, teams


def load_extras():
    """Extra lessons the school site doesn't list (tutoring, mentoring...), from the Google Sheet and/or extras.json."""
    rows = list(load_json(ROOT / "extras.json", []))
    for r in read_sheet_tab(CONFIG.get("extras_sheet_tab", "תוספות"), HEADERS["name"]) or []:
        row = {k: r.get(v, "") for k, v in HEADERS.items()}
        if row["name"]:
            rows.append(row)
    by_name = {}
    for r in rows:
        nums = [int(n) for n in re.findall(r"\d+", f"{r.get('from', '')} {r.get('to', '')} {r.get('hours', '')}")]
        kind, subject = r.get("type", ""), r.get("subject", "")
        what = subject if kind and subject.startswith(kind) else " · ".join(x for x in [kind, subject] if x)
        if not nums or not what or "דוגמה" in (r.get("notes") or ""):  # example rows stay in the sheet only
            continue
        extra = {"hours": list(range(min(nums), max(nums) + 1)), "subject": what,
                 "classes": [r["class"]] if r.get("class") else [], "rooms": [r["room"]] if r.get("room") else []}
        m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", r.get("date", "") or "")
        if m:
            year = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
            extra["date"] = f"{year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        elif (r.get("day") or "").replace("יום", "").strip(" '\"") in WEEKDAYS:
            extra["weekday"] = WEEKDAYS[r["day"].replace("יום", "").strip(" '\"")]
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
    display = person.get("display") or name or " ".join(reversed(person["as_given"].split(" ", 1)))
    homerooms = [h for h in [person.get("homeroom")] + person.get("homeroom_extra", []) if h]

    if offline:
        if not previous:
            return None
        latest, found = previous, []
    else:
        mine = extras.get(display, []) + (extras.get(person["as_given"], []) if person["as_given"] != display else [])
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
        title = "☀️ היום שלך"
    elif today_found:
        title, text = "⚠️ שינוי במערכת להיום", "\n".join(f"• {f['kind']}: {fill(f['text'])}" for f in today_found[:12])
    topic = ntfy_topic(person)
    sent = ""
    if text and topic:
        sent = "sent" if notify_ntfy(topic, title, text, CONFIG.get("site_url", "")) else "FAILED"
        if sent == "sent" and title.startswith("☀️"):
            summary_sent = today  # only once a day, and only once it really went out

    write_json(path, {
        "teacher": display, "source": CONFIG["site"], "roles": person.get("roles", []),
        "ends_title": "שעת סיום · " + ("כיתות החינוך" if len(homerooms) > 1 else "כיתת החינוך") if homerooms else "",
        "summary_sent": summary_sent, "latest": latest, "changelog": changelog,
        "history": [history[k] for k in sorted(history)],
    })
    active = sum(1 for l in latest["lessons"] if l.get("status") != "cancelled")
    print(f"{display}: {active} lessons, {len(found)} changes ({len(today_found)} today)"
          + (f", notification {sent}" if sent else ""))
    entry = {"id": pid, "name": display, "roles": person.get("roles", []), "homerooms": homerooms}
    pin = CONFIG.get("pin_hashes", {}).get(display)
    if pin:
        entry["pin"] = pin  # sha256("<id>:<code>"); the page asks for the code before showing the person
    return entry


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
    staff, team_defs = load_staff(raw)
    morning = args.morning or 5 <= now.hour < 8
    people = [p for p in (build_person(s, raw, extras, now, args.offline, morning) for s in staff) if p]
    if people:
        by_name = {p["name"]: p["id"] for p in people}
        teams = []
        for team_name, t in team_defs.items():
            members = [{"id": by_name[m], "name": m} for m in t["members"] if m in by_name]
            if t.get("lead") in by_name:
                teams.append({"name": team_name, "lead": by_name[t["lead"]], "members": members})
        write_json(DOCS / "data" / "staff.json",
                   {"updated": now.isoformat(timespec="minutes"), "people": people, "teams": teams})
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text((ROOT / "web" / "index.html").read_text(encoding="utf-8"), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    print(f"built {len(people)} pages at {now.isoformat(timespec='minutes')}")


if __name__ == "__main__":
    main()
