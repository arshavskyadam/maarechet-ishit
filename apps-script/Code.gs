/**
 * "המערכת האישית שלי" — personal reminders, personal codes and the HQ view.
 *
 * A standalone Apps Script project (script.google.com, signed in as the unit), deployed as a web app:
 *   Deploy → New deployment → Web app → Execute as: Me → Who has access: Anyone.
 * The first run creates a separate, private spreadsheet "המערכת האישית — קודים ותזכורות"
 * in the owner's Drive. Codes and reminders live only there, never in the public site.
 */

var SHEET_ID = '11AgQeq267joV3MHYxot3YrSCvdH2UfEf4WvLx5S_Kt4';  // the management sheet
var STAFF_TAB = 'אנשי צוות';
var COL_NAME = 'שם (כמו בשחף: משפחה ואז פרטי)';
var MAX_TEXT = 120;

function doGet() {
  return out({ ok: true, service: 'maarechet-ishit' });
}

function doPost(e) {
  var req;
  try { req = JSON.parse(e.postData.contents); } catch (err) { return out({ ok: false, error: 'bad request' }); }
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    return out(handle(req));
  } catch (err) {
    return out({ ok: false, error: String(err && err.message || err) });
  } finally {
    lock.releaseLock();
  }
}

function handle(req) {
  var a = req.action;
  if (a === 'hq') return hq(req);
  if (a === 'cmd_team' || a === 'cmd_subscribe') return commander(req);
  if (a === 'push_feed' || a === 'push_drop') return pushFeed(req);
  if (a === 'duty_get') return dutyGet(req);
  if (a === 'duty_set' || a === 'duty_clear') return dutySet(req);
  var name = clean(req.name);
  var staff = staffList();
  var me = staff.filter(function (p) { return p.name === name; })[0];
  if (!me) return { ok: false, error: 'unknown name' };
  // Each person is linked to one phone: the first phone that links gets a device key, and only that
  // key opens the person's reminders. To move someone to a new phone, delete their row in "מכשירים".
  var dev = ensureSheet('מכשירים', ['שם', 'מפתח מכשיר', 'קושר']);
  var row = findRow(dev, 1, name);
  if (a === 'status') return { ok: true, bound: row > 0 };
  if (a === 'bind') {
    if (row > 0) {
      if (req.token && String(dev.getRange(row, 2).getValue()) === String(req.token)) return { ok: true, token: req.token };
      return { ok: false, error: 'bound elsewhere' };
    }
    var token = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
    dev.appendRow([name, token, new Date()]);
    return { ok: true, token: token };
  }
  if (row < 1 || String(dev.getRange(row, 2).getValue()) !== String(req.token || '')) {
    Utilities.sleep(800);
    return { ok: false, error: 'not this phone' };
  }
  var rem = sheet('תזכורות');
  if (a === 'list') return { ok: true, reminders: reminders(rem, function (r) { return r.name === name; }) };
  if (a === 'add') {
    var r = req.reminder || {};
    var text = clean(r.text).slice(0, MAX_TEXT);
    var hour = Number(r.hour);
    if (!text || !(hour >= 0 && hour <= 10)) return { ok: false, error: 'bad reminder' };
    var date = /^\d{4}-\d{2}-\d{2}$/.test(r.date || '') ? r.date : '';
    var dow = date ? '' : Number(r.dow);
    if (!date && !(dow >= 0 && dow <= 6)) return { ok: false, error: 'bad reminder' };
    var id = Utilities.getUuid().slice(0, 12);
    rem.appendRow([id, name, me.team, "'" + date, dow, hour, text, new Date()]);
    return { ok: true, id: id };
  }
  if (a === 'delete') {
    var rr = findRow(rem, 1, String(req.id || ''));
    if (rr > 0 && rem.getRange(rr, 2).getValue() === name) rem.deleteRow(rr);
    return { ok: true };
  }
  if (a === 'subscribe') {
    var sub = req.sub || {};
    if (!sub.endpoint || !/^https:\/\//.test(sub.endpoint)) return { ok: false, error: 'bad subscription' };
    var subs = sheet('מנויים');
    var at = findRow(subs, 2, sub.endpoint);
    if (at > 0) subs.deleteRow(at);
    subs.appendRow([name, sub.endpoint, JSON.stringify(sub), new Date()]);
    return { ok: true };
  }
  if (a === 'team') {
    if (!me.lead) return { ok: false, error: 'not a team lead' };
    return { ok: true, team: me.team, reminders: reminders(rem, function (r) { return r.team === me.team && r.name !== name; }) };
  }
  return { ok: false, error: 'unknown action' };
}

/** For the hourly update only: who wants notifications, and the reminders to send. Needs the notification key. */
function pushFeed(req) {
  var key = String(setting('מפתח התראות') || '');
  if (!key || String(req.token || '') !== key) { Utilities.sleep(800); return { ok: false, error: 'wrong token' }; }
  var subs = sheet('מנויים');
  if (req.action === 'push_drop') {
    var at = findRow(subs, 2, String(req.endpoint || ''));
    if (at > 0) subs.deleteRow(at);
    return { ok: true };
  }
  var rows = subs.getDataRange().getValues().slice(1).filter(function (r) { return r[0] && r[2]; });
  return { ok: true, subs: rows.map(function (r) { return { name: r[0], sub: JSON.parse(r[2]) }; }),
           reminders: reminders(sheet('תזכורות'), function () { return true; }) };
}

/** Administration staff: the code only opens every page; personal reminders stay private. */
function hq(req) {
  var want = String(setting('קוד אנשי מנהלה') || '');
  if (!want || String(req.code || '').trim() !== want) {
    Utilities.sleep(800);
    return { ok: false, error: 'wrong code' };
  }
  return { ok: true };
}

/** A team's commander (above the team lead): the team's reminders, and notifications for the team. */
function commander(req) {
  var want = String(setting('קוד מפקדים') || '');
  if (!want || String(req.code || '').trim() !== want) {
    Utilities.sleep(800);
    return { ok: false, error: 'wrong code' };
  }
  var team = clean(req.team);
  if (!team || !staffList().some(function (p) { return p.team === team; })) return { ok: false, error: 'unknown team' };
  if (req.action === 'cmd_team') {
    return { ok: true, reminders: reminders(sheet('תזכורות'), function (r) { return r.team === team; }) };
  }
  var sub = req.sub || {};
  if (!sub.endpoint || !/^https:\/\//.test(sub.endpoint)) return { ok: false, error: 'bad subscription' };
  var subs = sheet('מנויים');
  var at = findRow(subs, 2, sub.endpoint);
  if (at > 0) subs.deleteRow(at);
  subs.appendRow(['מפקד:' + team, sub.endpoint, JSON.stringify(sub), new Date()]);
  return { ok: true };
}

/** Yard duty edited by hand (team leads, commanders, administration): who, and a short note. */
var DUTY_HEAD = ['תאריך', 'שכבה', 'שעה', 'תפקיד', 'מדריך', 'הערה', 'נערך על ידי', 'מתי'];
var GRADES = ['ט', 'י', 'יא', 'יב', 'יג', 'יד', 'מכללה'];  // 13 and 14 share one list, "מכללה"

function dutyGet(req) {
  var date = String(req.date || '');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return { ok: false, error: 'bad date' };
  var rows = ensureSheet('תורנויות ידניות', DUTY_HEAD).getDataRange().getDisplayValues().slice(1);
  return { ok: true, edits: rows.filter(function (r) { return r[0] === date; }).map(function (r) {
    return { grade: r[1], hour: Number(r[2]), role: r[3], person: r[4], note: r[5], by: r[6] };
  }) };
}

function dutySet(req) {
  var date = String(req.date || ''), grade = clean(req.grade), hour = Number(req.hour), role = clean(req.role).slice(0, 30);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || GRADES.indexOf(grade) < 0 || !(hour >= 0 && hour <= 12)) return { ok: false, error: 'bad slot' };
  var staff = staffList();
  // Team leads, commanders and administration use their code; any other staff member edits from the
  // phone linked to them, and only the grades of their own team.
  var code = String(req.code || '').trim();
  var ok = code && [setting('קוד מפקדים'), setting('קוד אנשי מנהלה')].some(function (w) { return w && code === String(w); });
  if (!ok && !code && req.name && req.token) {
    var name = clean(req.name), me = staff.filter(function (p) { return p.name === name; })[0];
    var dev = ensureSheet('מכשירים', ['שם', 'מפתח מכשיר', 'קושר']), row = findRow(dev, 1, name);
    if (!me || row < 1 || String(dev.getRange(row, 2).getValue()) !== String(req.token)) { Utilities.sleep(800); return { ok: false, error: 'not this phone' }; }
    if (teamGrades(me.team).indexOf(grade) < 0) return { ok: false, error: 'not your team' };
    ok = true;
  }
  if (!ok) { Utilities.sleep(800); return { ok: false, error: 'wrong code' }; }
  // one name, or two joined by "|" (the lunch-break roles can take two people)
  var names = String(req.person || '').split('|').map(clean).filter(function (x) { return x; }).slice(0, 2);
  if (names.some(function (n) { return !staff.some(function (p) { return p.name === n; }); })) return { ok: false, error: 'unknown name' };
  var person = names.join('|');
  var tab = ensureSheet('תורנויות ידניות', DUTY_HEAD);
  var rows = tab.getDataRange().getDisplayValues();
  for (var i = rows.length - 1; i >= 1; i--) {
    if (rows[i][0] === date && rows[i][1] === grade && Number(rows[i][2]) === hour && rows[i][3] === role) tab.deleteRow(i + 1);
  }
  if (req.action === 'duty_set') {
    tab.appendRow(["'" + date, grade, "'" + hour, role, person, clean(req.note).slice(0, 40), clean(req.by).slice(0, 40), new Date()]);
  }
  return { ok: true };
}

// ---- data

function teamGrades(team) {
  var tab = SpreadsheetApp.openById(SHEET_ID).getSheetByName('צוותים');
  if (!tab || !team) return [];
  var rows = tab.getDataRange().getValues(), head = rows.shift();
  var n = head.indexOf('שם הצוות'), g = head.indexOf('שכבות');
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][n]).trim() === team) {
      var list = String(rows[i][g] || '').replace(/['"׳״]/g, '').split(/[,\s]+/).filter(function (x) { return x; });
      if (list.indexOf('יג') >= 0 || list.indexOf('יד') >= 0) list.push('מכללה');
      return list;
    }
  }
  return [];
}

function staffList() {
  var tab = SpreadsheetApp.openById(SHEET_ID).getSheetByName(STAFF_TAB);
  var rows = tab.getDataRange().getValues();
  var head = rows.shift();
  var col = function (h) { return head.indexOf(h); };
  var n = col(COL_NAME), t = col('צוות'), l = col('ראש צוות'), act = col('פעיל');
  return rows.filter(function (r) { return r[n] && (act < 0 || String(r[act]).trim() !== 'לא'); }).map(function (r) {
    return { name: clean(r[n]), team: t >= 0 ? String(r[t]).trim() : '', lead: l >= 0 && String(r[l]).trim() === 'כן' };
  });
}

function reminders(rem, keep) {
  var rows = rem.getDataRange().getValues().slice(1);
  return rows.map(function (r) {
    return { id: String(r[0]), name: r[1], team: r[2], date: String(r[3] || ''), dow: r[4] === '' ? null : Number(r[4]), hour: Number(r[5]), text: r[6] };
  }).filter(function (r) { return r.id && keep(r); });
}

function store() {
  var props = PropertiesService.getScriptProperties();
  var id = props.getProperty('STORE_ID');
  if (id) return SpreadsheetApp.openById(id);
  var ss = SpreadsheetApp.create('המערכת האישית — קודים ותזכורות (פרטי)');
  ss.getSheets()[0].setName('מכשירים').appendRow(['שם', 'מפתח מכשיר', 'קושר']);
  ss.insertSheet('תזכורות').appendRow(['מזהה', 'שם', 'צוות', 'תאריך', 'יום בשבוע', 'שעה', 'תזכורת', 'נוצר']);
  ss.insertSheet('הגדרות').appendRow(['קוד אנשי מנהלה', '']);
  ss.getSheetByName('הגדרות').appendRow(['מפתח התראות', Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '')]);
  ss.insertSheet('מנויים').appendRow(['שם', 'כתובת', 'מנוי', 'נוצר']);
  ss.getSheetByName('הגדרות').getRange('A4').setValue('כותבים את קוד אנשי המנהלה בתא B1. רק מי שיודע אותו רואה את כל התזכורות של היחידה.');
  props.setProperty('STORE_ID', ss.getId());
  return ss;
}

function sheet(name) { return store().getSheetByName(name); }

function ensureSheet(name, head) {
  var ss = store(), tab = ss.getSheetByName(name);
  if (!tab) { tab = ss.insertSheet(name); tab.appendRow(head); }
  return tab;
}

function setting(key) {
  var tab = sheet('הגדרות');
  var rows = tab.getDataRange().getValues();
  for (var i = 0; i < rows.length; i++) if (rows[i][0] === key) return rows[i][1];
  tab.appendRow([key, '']);  // a new setting shows up as an empty row, ready to fill in
  return '';
}

function findRow(tab, col, value) {
  var vals = tab.getRange(1, col, Math.max(tab.getLastRow(), 1), 1).getValues();
  for (var i = 1; i < vals.length; i++) if (String(vals[i][0]) === value) return i + 1;
  return -1;
}

function clean(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }

function out(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Run once from the editor (▶ setup) to create the private spreadsheet and approve access. */
function setup() {
  var ss = store();
  Logger.log('הגיליון הפרטי מוכן: ' + ss.getUrl());
}
