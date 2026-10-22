/* App moved to /osler/. This worker replaces the old one, clears its cache and unregisters itself. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k.startsWith("nasenbluten")).map(k => caches.delete(k))))
    .then(() => self.registration.unregister())
    .then(() => self.clients.matchAll())
    .then(cs => cs.forEach(c => c.navigate("../osler/"))));
});
