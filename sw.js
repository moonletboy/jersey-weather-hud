/* Jersey Weather Chat - service worker
 *
 * This small file is what lets phones install the site as an app. It does
 * only two things:
 *   1. keeps a copy of the offline page and the app icons;
 *   2. when a page can't be loaded because there's no connection, shows that
 *      offline page instead of the browser's error.
 *
 * It never stores weather data or the pages themselves, so the app always
 * shows live information and every upload to GitHub reaches people straight
 * away, exactly as the website does.
 *
 * If this file is ever changed, change VERSION too (jwc-v2, jwc-v3 ...) so
 * phones replace the old copy.
 */
const VERSION = 'jwc-v2';   // v2: new JWC icons
const OFFLINE_URL = 'offline.html';
const KEEP = ['offline.html', 'manifest.json', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];

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
