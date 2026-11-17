// Shows the notifications the hourly update sends, and opens the site when one is tapped.
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "המערכת האישית שלי", {
    body: d.body || "", icon: "icon-192.png", badge: "icon-192.png", dir: "rtl", lang: "he",
    tag: d.tag || undefined, data: { url: d.url || "./" }
  }));
});

self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (wins) {
    for (var i = 0; i < wins.length; i++) if ("focus" in wins[i]) return wins[i].focus();
    return self.clients.openWindow((e.notification.data && e.notification.data.url) || "./");
  }));
});
