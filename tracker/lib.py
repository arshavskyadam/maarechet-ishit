"""Comparison, summary and notification helpers shared by build.py."""

import json
import time
import urllib.error
import urllib.request


def notify_ntfy(topic, title, message, click):
    """Send a phone notification through ntfy.sh. The free tier is rate limited per IP, so retry."""
    body = json.dumps({"topic": topic, "title": title, "message": message, "click": click, "tags": ["school"]})
    for attempt in range(6):
        try:
            req = urllib.request.Request("https://ntfy.sh/", data=body.encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20):
                return True
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"ntfy attempt {attempt + 1} failed: {e}")
            time.sleep(15 * (attempt + 1))
    return False


def lesson_key(l):
    return f"{l['date']}|{l['hour']}|{l['subject']}"


def describe(l):
    return f"{l['date']} שעה {l['hour']} · {l['subject']} · {', '.join(l['classes'])}"


def reasons(l):
    texts = sorted({c["text"] for c in l["changes"]})
    return f" — {', '.join(texts)}" if texts else ""


def fill(text):
    """The site writes a fill-in as "original <- substitute"; say it the way people do."""
    left, sep, right = text.partition("<-")
    return f"{right.strip()} במקום {left.strip()}" if sep else text


def hours_label(hours):
    """[1, 2, 3, 5] -> "1–3, 5"."""
    hours, parts, i = sorted(set(hours)), [], 0
    while i < len(hours):
        j = i
        while j + 1 < len(hours) and hours[j + 1] == hours[j] + 1:
            j += 1
        parts.append(str(hours[i]) if i == j else f"{hours[i]}–{hours[j]}")
        i = j + 1
    return ", ".join(parts)


EVENT_TYPES = ("מבחן", "אירוע", "טיול", "פעילות")


def is_event(change):
    return any(k in (change.get("type") or "") for k in EVENT_TYPES)


def cancel_kind(l):
    """Why a lesson is off the timetable: an exam or event in its place, a stand-in, or really cancelled.
    An exam or an event must never read as a cancellation."""
    for c in l.get("changes", []):
        if is_event(c):
            return f"{c['type']} במקום השיעור"
    for c in l.get("changes", []):
        if "מילוי מקום" in (c.get("type") or ""):
            return "מילוי מקום"
    return "לא מתקיים"


def dm(iso):
    return f"{iso[8:10]}.{iso[5:7]}"


def homeroom_line(hr, day):
    info = (hr or {}).get("days", {}).get(day)
    if not info:
        return []
    now, reg = info.get("last_lesson", info.get("end")), info.get("regular_end")
    end = f"מסיימת {now['end']}" if now else "אין שיעורים היום"
    if reg and (not now or now["hour"] != reg["hour"]):
        end += f" (בדרך כלל {reg['end']})"
    lines = [f"🏫 {hr['class']}: {end}"]
    reasons = sorted({fill(c["text"]) for c in info["changes"] if c["type"] != "מילוי מקום"})
    covered = {c["hour"] for c in info["changes"] if is_event(c) or c["type"] == "מילוי מקום"}
    off = [r["hour"] for r in info["removed"] if r["hour"] not in covered]
    if off:
        lines.append(f"   לא מתקיים: שעות {hours_label(off)}")
    if reasons:
        lines.append(f"   {', '.join(reasons)}")
    fills = sorted({fill(c["text"]) for c in info["changes"] if c["type"] == "מילוי מקום"})
    if fills:
        lines.append(f"   מילוי מקום: {', '.join(fills)}")
    return lines


def morning_summary(latest, today):
    """Short text of today's lessons, or None when there is nothing on today."""
    day = next((d for d in latest["days"] if d["date"] == today), None)
    todays = [l for l in latest["lessons"] if l["date"] == today]
    hr_lines = homeroom_line(latest.get("homeroom"), today)
    if not day or (not todays and not hr_lines):
        return None
    blocks = []
    for l in todays:
        b = blocks[-1] if blocks else None
        if b and b["subject"] == l["subject"] and b["classes"] == l["classes"] and b["status"] == l.get("status") \
                and b["last"] + 1 == l["hour"]:
            b["last"], b["end"] = l["hour"], l["end"]
            b["changes"] += [c for c in l["changes"] if c not in b["changes"]]
            continue
        blocks.append({"subject": l["subject"], "classes": l["classes"], "status": l.get("status"), "last": l["hour"],
                       "start": l["start"], "end": l["end"], "changes": list(l["changes"])})
    active = [b for b in blocks if b["status"] != "cancelled"]
    count = sum(1 for l in todays if l.get("status") != "cancelled")
    head = f"📅 {day['weekday']} {dm(today)}"
    if active:
        head += f" · {'שעה אחת' if count == 1 else f'{count} שעות'} · סיום {active[-1]['end']}"
    else:
        head += " · אין לך שיעורים"
    lines = [head] + [f"⚠️ {h}" for h in day.get("holidays", [])]
    for b in blocks:
        where = (", ".join(b["classes"]) + " · ") if b["classes"] else ""
        notes = sorted({fill(c["text"]) for c in b["changes"]})
        if b["status"] == "cancelled":
            kind = cancel_kind(b)
            mark = "📝" if kind.endswith("במקום השיעור") else "🔄" if kind == "מילוי מקום" else "❌"
            lines.append(f"{mark} {b['start']} {where}{b['subject']} — {kind}" + (f" ({', '.join(notes)})" if notes else ""))
        else:
            lines.append(f"{b['start']}–{b['end']} {where}{b['subject']}" + (f" ({', '.join(notes)})" if notes else ""))
    # Exams in the teacher's classes, and events that fall on one of the teacher's lessons.
    for a in [a for a in latest.get("agenda", []) if a["date"] == today and (a["kind"] == "מבחן" or a["my_lessons"])][:3]:
        lines.append(f"📝 {a['kind']}: {a['title']} ({', '.join(a['my_classes'])})")
    return "\n".join(lines + hr_lines)


def todays_changes(found, today):
    """Only changes that land on today are worth a notification; the rest wait on the page."""
    out = []
    for f in found:
        if f["kind"] == "שעת סיום":  # grade-wide end times; the homeroom class is reported on its own
            continue
        if f["date"] == today or (not f["date"] and dm(today) + "." + today[:4] in f["text"]):
            out.append(f)
    return out


def names(items):
    return [i["name"] if isinstance(i, dict) else i for i in items]


def diff(old, new, today):
    """List what changed in upcoming lessons between two runs."""
    # Lessons added by hand in the config aren't on the site, so they are never reported as changes.
    old_by = {lesson_key(l): l for l in old.get("lessons", []) if l["date"] >= today and not l.get("manual")}
    new_by = {lesson_key(l): l for l in new["lessons"] if l["date"] >= today and not l.get("manual")}
    # Only compare dates both runs could see, so the week rolling forward isn't reported as "new lessons".
    old_dates = {d["date"] for d in old.get("days", [])}
    new_dates = {d["date"] for d in new["days"]}
    shared = old_dates & new_dates
    found = []
    for k, l in new_by.items():
        if l["date"] not in shared:
            continue
        status = l.get("status", "regular")
        if k not in old_by:
            found.append({"kind": cancel_kind(l) if status == "cancelled" else "נוסף", "date": l["date"],
                          "text": describe(l) + reasons(l)})
            continue
        before = old_by[k]
        if before.get("status", "regular") != status:
            found.append({"kind": cancel_kind(l) if status == "cancelled" else "חזר למערכת", "date": l["date"],
                          "text": describe(l) + reasons(l)})
        for field, label in (("classes", "כיתות"), ("rooms", "חדר"), ("co_teachers", "מורים שותפים")):
            was, now = names(before.get(field, [])), names(l[field])
            if was != now:
                found.append({"kind": "עודכן", "date": l["date"],
                              "text": f"{describe(l)} — {label}: {', '.join(was) or '—'} ← {', '.join(now) or '—'}"})
        old_changes = {(c["type"], c["text"]) for c in before["changes"]}
        for c in l["changes"]:
            if (c["type"], c["text"]) not in old_changes:
                found.append({"kind": c["type"], "date": l["date"], "text": f"{describe(l)} — {c['text']}"})
    for k, l in old_by.items():
        if l["date"] in shared and k not in new_by:
            found.append({"kind": "הוסר", "date": l["date"], "text": describe(l)})
    old_tab = {(c["class"], c["date_label"], c["text"]) for c in old.get("class_changes", [])}
    for c in new["class_changes"]:
        if (c["class"], c["date_label"], c["text"]) not in old_tab:
            found.append({"kind": "שינוי בדף השינויים", "date": "", "text": f"{c['class']}: {c['date_label']} {c['text']}".strip()})
    if "class_end" in old:
        for day, rows in new.get("class_end", {}).items():
            if day < today or day not in shared:
                continue
            before = {r["class"]: r["now"] for r in old["class_end"].get(day, [])}
            for r in rows:
                was, now = before.get(r["class"]), r["now"]
                if r["class"] in before and was != now:
                    found.append({"kind": "שעת סיום", "date": day,
                                  "text": f"{day} · {r['class']} מסיים {now['end'] if now else 'ללא לימודים'} (במקום {was['end'] if was else 'ללא לימודים'})"})
    if old.get("homeroom") and new.get("homeroom") and old["homeroom"]["class"] == new["homeroom"]["class"]:
        cls = new["homeroom"]["class"]
        for day, info in new["homeroom"]["days"].items():
            before = old["homeroom"]["days"].get(day)
            if day < today or day not in shared or before is None:
                continue
            was, now = before.get("last_lesson", before.get("end")), info.get("last_lesson")
            if (was or {}).get("hour") != (now or {}).get("hour"):
                found.append({"kind": "כיתת חינוך", "date": day,
                              "text": f"{cls} מסיימת {now['end'] if now else 'ללא לימודים'} (קודם {was['end'] if was else 'ללא לימודים'})"})
            old_removed = {(r["hour"], r["subject"], r["teacher"]) for r in before.get("removed", [])}
            gone = [r for r in info["removed"] if (r["hour"], r["subject"], r["teacher"]) not in old_removed]
            events = {c["hour"]: c for c in info["changes"] if is_event(c)}
            subbed = {c["hour"] for c in info["changes"] if c["type"] == "מילוי מקום"}
            really = [r for r in gone if r["hour"] not in events and r["hour"] not in subbed]
            instead = [r for r in gone if r["hour"] in events]
            if really:
                found.append({"kind": "כיתת חינוך", "date": day,
                              "text": f"{cls} · לא מתקיים: " + ", ".join(f"שעה {r['hour']} {r['subject']}" for r in really[:6])})
            if instead:
                found.append({"kind": "כיתת חינוך", "date": day,
                              "text": f"{cls} · " + ", ".join(f"שעה {r['hour']}: {events[r['hour']]['type']} במקום {r['subject']}" for r in instead[:6])})
            old_changes = {(c["hour"], c["text"]) for c in before.get("changes", [])}
            # Fill-ins don't move the end of the day, so they wait for the morning summary.
            new_changes = [c for c in info["changes"] if (c["hour"], c["text"]) not in old_changes and c["type"] != "מילוי מקום"]
            if new_changes and not gone:
                found.append({"kind": "כיתת חינוך", "date": day,
                              "text": f"{cls} · " + ", ".join(sorted({fill(c['text']) for c in new_changes}))})
    if "agenda" in old:  # the first run with agenda support would otherwise report every item as new
        old_agenda = {(a["kind"], a["date"], a["title"]) for a in old["agenda"]}
        for a in new.get("agenda", []):
            if (a["kind"], a["date"], a["title"]) not in old_agenda:
                found.append({"kind": f"{a['kind']} חדש", "date": a["date"],
                              "text": f"{a['date']} · {a['title']} · {', '.join(a['my_classes'])}"})
    return found


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
