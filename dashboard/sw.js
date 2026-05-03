// Service Worker pour Claude Tracker
// - rend l'app installable comme PWA
// - reçoit les Web Push et affiche des notifications
// - clic sur une notif → focus / ouvre le dashboard

const CACHE_NAME = 'claude-tracker-v1';

self.addEventListener('install', (event) => {
  // Activation immédiate sans attendre un rechargement
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // Prend le contrôle des onglets ouverts immédiatement
  event.waitUntil(self.clients.claim());
});

// Pas de cache offline pour l'instant : on passe tout au réseau (le dashboard
// a besoin de données live de toute façon). Le SW est juste là pour rendre
// l'app installable et pour le Web Push.
self.addEventListener('fetch', (event) => {
  // network-first sans cache
});

// Réception d'une Web Push
self.addEventListener('push', (event) => {
  let data = { title: 'Claude Tracker', body: '', url: '/', tag: 'claude-tracker' };
  try {
    if (event.data) {
      const parsed = event.data.json();
      data = { ...data, ...parsed };
    }
  } catch (e) {
    if (event.data) data.body = event.data.text();
  }

  const opts = {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: data.tag,
    renotify: true,
    requireInteraction: data.title && data.title.includes('Question'),
    data: { url: data.url || '/' },
  };

  event.waitUntil(self.registration.showNotification(data.title, opts));
});

// Clic sur une notif : focus l'onglet existant ou en ouvre un nouveau
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      // Cherche un onglet déjà ouvert sur le dashboard, le focus
      for (const w of wins) {
        const u = new URL(w.url);
        if (u.pathname === '/' || u.pathname.startsWith('/index')) {
          return w.focus().then(() => w.navigate(targetUrl)).catch(() => w.focus());
        }
      }
      // Sinon ouvre une nouvelle fenêtre
      return self.clients.openWindow(targetUrl);
    })
  );
});
