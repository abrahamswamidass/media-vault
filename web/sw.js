// Service worker — exists ONLY to receive Web Push events and show a
// notification; this app has no offline caching story, so nothing else is
// registered here (no fetch handler, no cache). Must live at the site root:
// a service worker's scope is its own path's directory and everything below
// it, so registering it from a subfolder would leave it unable to control
// (or receive push events meant for) the whole app.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "Media Vault";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    icon: "icons/icon-192.png",
    data: { url: data.url || "./" },
  }));
});

// Tapping the notification focuses an already-open tab rather than always
// opening a new one — most of the time this app is already open somewhere
// (it's a PWA someone added to their home screen), and stacking duplicate
// tabs every time a notification arrives would be worse than just bringing
// the existing one forward.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "./";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
      return undefined;
    }),
  );
});
