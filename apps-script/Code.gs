/**
 * "המערכת האישית שלי" — personal reminders, personal codes and the HQ view.
 *
 * Runs inside the management sheet (Extensions → Apps Script), deployed as a web app:
 *   Deploy → New deployment → Web app → Execute as: Me → Who has access: Anyone.
 * The first run creates a separate, private spreadsheet "המערכת האישית — קודים ותזכורות"
 * in the owner's Drive. Codes and reminders live only there, never in the public site.
 */

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
  var name = clean(req.name);
  var staff = staffList();
  var me = staff.filter(function (p) { return p.name === name; })[0];
  if (!me) return { ok: false, error: 'unknown name' };
  var codes = sheet('קודים');
  var row = findRow(codes, 1, name);
  if (a === 'status') return { ok: true, hasCode: row > 0 };
  if (a === 'setCode') {
    if (row > 0) return { ok: false, error: 'code exists' };
    var code = String(req.code || '').trim();
    if (!/^\d{4,8}$/.test(code)) return { ok: false, error: 'code must be 4-8 digits' };
    codes.appendRow([name, "'" + code, new Date()]);
    return { ok: true };
  }
  if (row < 1 || String(codes.getRange(row, 2).getValue()) !== String(req.code || '').trim()) {
    Utilities.sleep(800);
    return { ok: false, error: 'wrong code' };
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
  if (a === 'team') {
    if (!me.lead) return { ok: false, error: 'not a team lead' };
    return { ok: true, team: me.team, reminders: reminders(rem, function (r) { return r.team === me.team && r.name !== name; }) };
  }
  return { ok: false, error: 'unknown action' };
}

function hq(req) {
  var want = String(setting('קוד אנשי מנהלה') || '');
  if (!want || String(req.code || '').trim() !== want) {
    Utilities.sleep(800);
    return { ok: false, error: 'wrong code' };
  }
  return { ok: true, reminders: reminders(sheet('תזכורות'), function () { return true; }) };
}

// ---- data

function staffList() {
  var tab = SpreadsheetApp.getActive().getSheetByName(STAFF_TAB);
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
  ss.getSheets()[0].setName('קודים').appendRow(['שם', 'קוד', 'נוצר']);
  ss.insertSheet('תזכורות').appendRow(['מזהה', 'שם', 'צוות', 'תאריך', 'יום בשבוע', 'שעה', 'תזכורת', 'נוצר']);
  ss.insertSheet('הגדרות').appendRow(['קוד אנשי מנהלה', '']);
  ss.getSheetByName('הגדרות').getRange('A3').setValue('כותבים את קוד אנשי המנהלה בתא B1. רק מי שיודע אותו רואה את כל התזכורות של היחידה.');
  props.setProperty('STORE_ID', ss.getId());
  return ss;
}

function sheet(name) { return store().getSheetByName(name); }

function setting(key) {
  var rows = sheet('הגדרות').getDataRange().getValues();
  for (var i = 0; i < rows.length; i++) if (rows[i][0] === key) return rows[i][1];
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
