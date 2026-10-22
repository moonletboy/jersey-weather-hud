/* Jersey Weather Chat - service worker
 *
 * This small file is what lets phones install the site as an app. It does
 * three things:
 *   1. keeps a copy of the offline page and the app icons;
 *   2. when a page can't be loaded because there's no connection, shows that
 *      offline page instead of the browser's error;
 *   3. receives push messages (Jersey Met warning alerts sent by the GitHub
 *      Action) and shows them as notifications, even when the site is closed.
 *
 * It never stores weather data or the pages themselves, so the app always
 * shows live information and every upload to GitHub reaches people straight
 * away, exactly as the website does.
 *
 * If this file is ever changed, change VERSION too (jwc-v3, jwc-v4 ...) so
 * phones replace the old copy.
 */
const VERSION = 'jwc-v4';   // v4: tapping a notification opens the LATEST ALERTS panel
const OFFLINE_URL = 'offline.html';
const KEEP = ['offline.html', 'manifest.json', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];

/* Same Apps Script web app the pages use. Only needed here so that, if a phone quietly
   renews its subscription in the background, the new one gets saved to the sheet. */
const API_URL = 'https://script.google.com/macros/s/AKfycbzqw6EZ01rKNo8FJrP-VHwpNCRe9AWV2FAeVH3AvVzX2wXWpuprFNRQngIgJC0K08FHcg/exec';

self.addEventListener('install', function(event){
  event.waitUntil(caches.open(VERSION).then(function(cache){ return cache.addAll(KEEP); }).then(function(){ return self.skipWaiting(); }));
});

self.addEventListener('activate', function(event){
  event.waitUntil(
    caches.keys()
      .then(function(keys){ return Promise.all(keys.filter(function(k){ return k !== VERSION; }).map(function(k){ return caches.delete(k); })); })
      .then(function(){ return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function(event){
  /* only page loads; data, maps, fonts and everything else go straight to the network untouched */
  if(event.request.mode !== 'navigate') return;
  event.respondWith(
    fetch(event.request).catch(function(){
      return caches.match(OFFLINE_URL).then(function(page){
        return page || new Response('You are offline.', { headers:{ 'Content-Type':'text/plain' } });
      });
    })
  );
});

/* ---------- push notifications ----------
   The GitHub Action sends a small JSON message:
     { "title": "...", "body": "...", "url": "...", "tag": "..." }
   Anything missing falls back to a sensible default. A message with no readable
   JSON still shows a generic alert, so a push is never silently lost. */
self.addEventListener('push', function(event){
  let data = {};
  try{ data = event.data ? event.data.json() : {}; }
  catch(e){ data = { body: event.data ? event.data.text() : '' }; }
  const title = data.title || 'Jersey Weather Chat';
  const options = {
    body: data.body || 'New Jersey Met weather warning. Check gov.je for the official warning.',
    icon: 'icons/icon-192.png',
    badge: 'icons/icon-192.png',
    tag: data.tag || 'jwc-warning',          // same tag = newer alert replaces the older one instead of piling up
    renotify: true,
    data: { url: data.url || './index.html?source=push#alerts' }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

/* Tapping the notification opens the site (or brings an already-open tab/app to the front). */
self.addEventListener('notificationclick', function(event){
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || './index.html?source=push#alerts', self.location.href).href;
  event.waitUntil(
    self.clients.matchAll({ type:'window', includeUncontrolled:true }).then(function(list){
      for(const c of list){
        if('focus' in c){ c.navigate && c.navigate(target); return c.focus(); }
      }
      return self.clients.openWindow(target);
    })
  );
});

/* Browsers occasionally replace a subscription behind the scenes. When that happens,
   subscribe again with the same public key and tell the sheet about the new one. */
self.addEventListener('pushsubscriptionchange', function(event){
  const oldSub = event.oldSubscription;
  const key = (oldSub && oldSub.options && oldSub.options.applicationServerKey) || (event.newSubscription && event.newSubscription.options.applicationServerKey);
  if(!key) return;
  event.waitUntil(
    self.registration.pushManager.subscribe({ userVisibleOnly:true, applicationServerKey:key })
      .then(function(sub){
        const body = JSON.stringify({ action:'subscribe', subscription: sub.toJSON(), replaces: oldSub ? oldSub.endpoint : '', ua: self.navigator.userAgent });
        return fetch(API_URL, { method:'POST', body: body });
      })
      .catch(function(){})
  );
});
