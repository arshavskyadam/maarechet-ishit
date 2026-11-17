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


def role_label(role):
    """The staff call coordinators "ראש צוות", whatever the sheet says."""
    return re.sub(r"^רכז(ת)?\s+", "ראש צוות ", role.strip())


def class_key(class_name):
    """"ט' - 3", "ט'3" and "ט 3" all -> "ט3"."""
    return re.sub(r"[^א-ת0-9]", "", class_name)


def grade_of(class_name):
    """"יא'- 2" -> "יא", "י - 6" -> "י"."""
    return re.sub(r"[^א-ת]", "", class_name.split("-")[0])


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
        code = r.get("קוד", "").strip()
        person = {"name": name, "as_given": typed, "display": name or typed, "code": code,
                  "roles": [role_label(x) for x in r.get("תפקיד", "").split(",") if x.strip()]}
        if homerooms:
            person["homeroom"], person["homeroom_extra"] = homerooms[0], homerooms[1:]
        staff.append(person)
        team = r.get("צוות", "")
        if team:
            t = teams.setdefault(team, {"lead": None, "members": []})
            t["members"].append(person["display"])
            if r.get("ראש צוות") == "כן":
                t["lead"] = person["display"]
    for r in read_sheet_tab(CONFIG.get("teams_tab", "צוותים"), "שם הצוות") or []:
        if r.get("שם הצוות") in teams:
            teams[r["שם הצוות"]]["grades"] = [g.strip(" '\"") for g in r.get("שכבות", "").split(",") if g.strip()]
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
        # Keyed by the set of name words, so "מאי כהן" and "כהן מאי" are the same person.
        by_name.setdefault(frozenset(r["name"].split()), []).append(extra)
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
        keys = {frozenset(display.split()), frozenset(person["as_given"].split())}
        mine = [e for k in keys for e in extras.get(k, [])]
        # Classes typed by hand ("ט'3", "ט 3") are written as the site writes them ("ט' - 3").
        site_class = {class_key(c["name"]): c["name"] for c in raw["classes"]}
        mine = [{**e, "classes": [site_class.get(class_key(c), c) for c in e["classes"]]} for e in mine]
        latest = scrape(name or "\u0000", raw=raw, extra_lessons=mine, homeroom=homerooms[0] if homerooms else None)
        latest["homerooms"] = [homeroom_info(raw, h) for h in homerooms]
        # The page's "end of day" table: the homeroom classes, or for a team lead every class of their grades.
        grades = person.get("lead_grades", [])
        shown = [c["name"] for c in raw["classes"] if grade_of(c["name"]) in grades] if grades else []
        latest["class_end"] = {}
        for hr in latest["homerooms"] + [homeroom_info(raw, c) for c in shown if c not in homerooms]:
            for day, info in hr["days"].items():
                latest["class_end"].setdefault(day, []).append(
                    {"class": hr["class"], "now": info.get("last_lesson"), "regular": info.get("regular_end")})
        for rows in latest["class_end"].values():
            rows.sort(key=lambda r: (len(grade_of(r["class"])), r["class"]))
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
        "teacher": display, "source": CONFIG["site"], "roles": person.get("roles", []), "api": CONFIG.get("api_url", ""),
        "ends_title": ("שעת סיום · שכבה " + ", ".join(person["lead_grades"])) if person.get("lead_grades")
        else ("שעת סיום · " + ("הכיתות שלי" if len(homerooms) > 1 else "הכיתה שלי") if homerooms else ""),
        "summary_sent": summary_sent, "latest": latest, "changelog": changelog,
        "history": [history[k] for k in sorted(history)],
    })
    active = sum(1 for l in latest["lessons"] if l.get("status") != "cancelled")
    print(f"{display}: {active} lessons, {len(found)} changes ({len(today_found)} today)"
          + (f", notification {sent}" if sent else ""))
    entry = {"id": pid, "name": display, "roles": person.get("roles", []), "homerooms": homerooms}
    pin = pin_hash(pid, person["code"]) if person.get("code") else CONFIG.get("pin_hashes", {}).get(display)
    if pin:
        entry["pin"] = pin  # sha256("<id>:<code>"); the page asks for the code before showing the person
    return entry


LUNCH_ROLES = ["לוקח לחד\"א", "נשאר בשכבה"]


def build_duty(raw, now, people, team_defs):
    """Yard duty per grade: one free team member for every period the grade is in class, and after
    period 6 the lunch break with two roles. Kept stable through the day: someone who stays free keeps
    the slot; a slot whose person became busy is handed to the free member with the fewest duties."""
    today = now.date().isoformat()
    path = DOCS / "data" / "duty.json"
    prev = load_json(path, {}).get("days", {})
    busy = {}
    for person in people:
        data = load_json(DOCS / "data" / f"{person['id']}.json", {})
        for l in data.get("latest", {}).get("lessons", []):
            if l.get("status") != "cancelled" and l.get("hour") is not None:
                busy.setdefault(person["name"], set()).add((l["date"], l["hour"]))
    known = {p["name"] for p in people}
    dates = sorted(d for d in raw["days"] if d >= today and datetime.fromisoformat(d).weekday() in (6, 0, 1, 2, 3))
    grade_hours = {}
    for (date, hour), entries in raw["slots"].items():
        if hour is None:
            continue
        for name, cell in entries:
            if cell["lessons"]:
                grade_hours.setdefault((date, grade_of(name)), set()).add(hour)
    count = {}
    days = {d: v for d, v in prev.items() if d < today and d >= (now.date() - timedelta(days=14)).isoformat()}

    def pick(cands, date, key, taken):
        free = [c for c in cands if c not in taken]
        free.sort(key=lambda c: (count.get(c, 0), hashlib.sha1(f"{c}|{date}|{key}".encode()).hexdigest()))
        return free[0] if free else None

    for date in dates:
        day_out, taken_at = {}, {}
        for team, t in team_defs.items():
            members = [m for m in t.get("members", []) if m in known]
            if not members:
                continue
            for grade in t.get("grades", []):
                hours = sorted(grade_hours.get((date, grade), ()))
                if not hours:
                    continue
                old = {(s["hour"], s.get("role", "")): s["people"] for s in prev.get(date, {}).get(grade, {}).get("slots", [])}
                slots = []
                wanted = [(h, "") for h in hours if h >= 1]
                if any(h >= 7 for h in hours):
                    wanted.append((6.5, LUNCH_ROLES[0]))
                    wanted.append((6.5, LUNCH_ROLES[1]))
                for h, role in wanted:
                    taken = taken_at.setdefault(h, set())
                    if role:
                        cands = [m for m in members]  # the whole team is out of class during the break
                    else:
                        cands = [m for m in members if (date, h) not in busy.get(m, set())]
                    keep = [x for x in old.get((h, role), []) if x in cands and x not in taken]
                    chosen = keep[:1] or [x for x in [pick(cands, date, f"{grade}{h}{role}", taken)] if x]
                    for c in chosen:
                        taken.add(c)
                        count[c] = count.get(c, 0) + 1
                    slot = {"hour": h, "people": chosen}
                    if role:
                        slot["role"] = role
                    slots.append(slot)
                day_out[grade] = {"team": team, "slots": sorted(slots, key=lambda x: x["hour"])}
        days[date] = day_out
    write_json(path, {"updated": now.isoformat(timespec="minutes"), "lunch_roles": LUNCH_ROLES, "days": days})
    print(f"duty lists for {len(dates)} days")

def class_id(class_name):
    return hashlib.sha1(class_name.encode("utf-8")).hexdigest()[:8]


def build_classes(raw, now):
    """One small file per class (its lessons, changes and end times) plus an index, for the "classes" tab."""
    days = sorted(raw["days"].values(), key=lambda d: d["date"])
    regular = {}
    for name, cell in raw["regular"]:
        regular[(name, cell["date"], cell["hour"])] = cell
    index = []
    for c in raw["classes"]:
        name = c["name"]
        slots = {}
        for (date, hour), entries in raw["slots"].items():
            for n, cell in entries:
                if n == name and hour is not None:
                    slots[(date, hour)] = {"date": date, "hour": hour, "start": cell["start"], "end": cell["end"],
                                           "lessons": cell["lessons"], "changes": cell["changes"], "removed": []}
        for (n, date, hour), cell in regular.items():
            if n != name or hour is None:
                continue
            cur = slots.get((date, hour))
            kept = {(l["subject"], l["teacher"]) for l in (cur["lessons"] if cur else [])}
            gone = [l for l in cell["lessons"] if (l["subject"], l["teacher"]) not in kept]
            if gone:
                slot = slots.setdefault((date, hour), {"date": date, "hour": hour, "start": cell["start"], "end": cell["end"],
                                                       "lessons": [], "changes": [], "removed": []})
                slot["removed"].extend(gone)
        info = homeroom_info(raw, name)["days"]
        ends = {d: {"now": v.get("last_lesson"), "regular": v.get("regular_end")} for d, v in info.items()}
        cid = class_id(name)
        write_json(DOCS / "data" / "class" / f"{cid}.json", {
            "class": name, "updated": now.isoformat(timespec="minutes"), "days": days, "ends": ends,
            "slots": [slots[k] for k in sorted(slots)],
        })
        index.append({"id": cid, "name": name, "grade": grade_of(name)})
    write_json(DOCS / "data" / "classes.json", {"updated": now.isoformat(timespec="minutes"), "classes": index})
    print(f"built {len(index)} class pages")


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
    for t in team_defs.values():
        for person in staff:
            shown = person.get("display") or person.get("name") or " ".join(reversed(person["as_given"].split(" ", 1)))
            if shown == t.get("lead"):
                person["lead_grades"] = person.get("lead_grades", []) + t.get("grades", [])
    if raw:
        build_classes(raw, now)
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
                   {"updated": now.isoformat(timespec="minutes"), "people": people, "teams": teams, "api": CONFIG.get("api_url", "")})
        if raw:
            build_duty(raw, now, people, team_defs)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text((ROOT / "web" / "index.html").read_text(encoding="utf-8"), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    print(f"built {len(people)} pages at {now.isoformat(timespec='minutes')}")


if __name__ == "__main__":
    main()
