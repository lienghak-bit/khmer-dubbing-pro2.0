// Service Worker - AI Dubbing Pro
// Only cache GET requests for static files; NEVER intercept POST/API requests
self.addEventListener('install', function(e) { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(clients.claim()); });
self.addEventListener('fetch', function(e) {
    const url = new URL(e.request.url);
    // Never intercept API calls or non-GET requests - let them go directly to server
    if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) {
        return; // Let browser handle it natively (no SW interception)
    }
    // For static GET requests, pass through normally
    e.respondWith(fetch(e.request));
});
