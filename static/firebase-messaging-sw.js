importScripts('https://www.gstatic.com/firebasejs/10.13.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.13.2/firebase-messaging-compat.js');

importScripts('/static/js/firebase-config.js');
firebase.initializeApp(STEP_SAFE_FIREBASE_CONFIG);

const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
  console.log('[STEP-SAFE] Background message received:', payload);

  // FCM automatically displays notification payloads while in the background.
  // Show a notification here only for data-only messages to avoid duplicates.
  if (payload.notification) return;

  const notification = payload.data || {};
  const title = notification.title || 'STEP-SAFE Alert';
  const body = notification.body || 'A new foot-pressure alert has been received.';
  const url = payload.data?.url || '/';

  self.registration.showNotification(title, {
    body: body,
    data: {
      url: url
    }
  });
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const url = event.notification.data?.url || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if ('focus' in client) {
            client.navigate(url);
            return client.focus();
          }
        }

        if (clients.openWindow) {
          return clients.openWindow(url);
        }
      })
  );
});
