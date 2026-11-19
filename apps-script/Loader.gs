/**
 * "המערכת האישית שלי" — the engine loader. This is the ONLY code pasted into script.google.com.
 *
 * Every request loads the current engine (apps-script/Code.gs) from the project's GitHub repository,
 * so a change to the engine needs no pasting: it takes effect within about 5 minutes. If GitHub can't
 * be reached, the last engine that loaded is used (kept for up to 6 hours).
 *
 * After pasting: run "setup" once (▶ at the top) and approve the access it asks for, then
 * Deploy → Manage deployments → ✏️ → Version: New version → Deploy.
 */

var ENGINE_URL = 'https://raw.githubusercontent.com/techni-madrichim/maarechet-ishit/main/apps-script/Code.gs';

function doGet(e) { return engine_().doGet(e); }
function doPost(e) { return engine_().doPost(e); }
function setup() { return engine_().setup(); }

function engine_() {
  var cache = CacheService.getScriptCache();
  var code = cache.get('engine:fresh');
  if (!code) {
    try {
      var r = UrlFetchApp.fetch(ENGINE_URL + '?t=' + Date.now(), { muteHttpExceptions: true });
      if (r.getResponseCode() === 200 && r.getContentText().indexOf('function handle(') >= 0) {
        code = r.getContentText();
        cache.put('engine:fresh', code, 300);    // check GitHub again after 5 minutes
        cache.put('engine:backup', code, 21600); // fallback for 6 hours
      }
    } catch (err) {}
    if (!code) code = cache.get('engine:backup');
    if (!code) throw new Error('engine not reachable');
  }
  return new Function(code + '\n;return { doGet: doGet, doPost: doPost, setup: setup };')();
}

// Never called: it only lists the Google services the engine uses, so that Apps Script asks for
// permission to use them when "setup" is run.
function permissions_() {
  SpreadsheetApp.openById('x'); SpreadsheetApp.create('x');
  PropertiesService.getScriptProperties(); LockService.getScriptLock(); CacheService.getScriptCache();
  ContentService.createTextOutput(''); Utilities.getUuid(); UrlFetchApp.fetch('https://x');
}
