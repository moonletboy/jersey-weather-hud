/* Jersey Weather Chat - Settings panel (the gear at the end of the tab bar)
 *
 * What this file does, on every page that loads it:
 *   1. puts a SETTINGS gear at the right-hand end of the tab bar (it stays put while the tabs scroll);
 *   2. builds the Settings panel: What's new, the app's start page, About & disclaimer,
 *      Privacy & cookies, Systems check, step-by-step help, Report a problem and the Facebook group;
 *   3. keeps a quiet note of the data this page fetches, for the Systems check (it never leaves the device);
 *   4. on the Storm Reports page, can remember your name and parish on this device;
 *   5. in the installed app, opens on the start page you picked.
 *
 * Every page loads it near the top of <head>, WITHOUT defer:
 *     <script src="jwc-settings.js"></script>
 * It has to start before the page's own scripts so the Systems check sees every data request,
 * and so the app can switch to your start page before anything is drawn.
 *
 * Settings and saved report details are kept in this browser only (localStorage). Nothing here is sent to us.
 *
 * TO ADD A "WHAT'S NEW" LINE: add a line at the TOP of WHATS_NEW below (newest first, same format)
 * and upload this file. Anyone who hasn't seen it yet gets a small amber dot on the gear.
 *
 * Links that open the panel from anywhere: add #settings, #about or #systems to a page address,
 * or give a button the attribute data-jwc-open="about" (or "settings", "privacy", "systems", "news").
 */
(function(){
  'use strict';

  /* ================= things you may want to edit ================= */
  var WHATS_NEW = [
    ['2026-10-27', 'New Settings panel: tap the gear at the end of the tab bar for About, privacy, help and more.'],
    ['2026-10-27', 'Systems check (Settings \u203A About & help) shows how fresh each piece of data on the page is.'],
    ['2026-10-27', 'Storm Reports can remember your name and parish on this device.'],
    ['2026-10-27', 'Installed app: choose which page it opens on (Settings \u203A App).'],
    ['2026-10-23', 'The Live Ops wind card now uses real Jersey Airport gusts for the last 12 hours.'],
    ['2026-10-22', 'Latest alerts sent: read an alert again after swiping the notification away.'],
    ['2026-10-22', 'Channel Islands Wind Tracker by VeryWeather added to Live Ops.'],
    ['2026-10-21', 'Warning alerts: a notification when Jersey Met issues or upgrades a warning.'],
    ['2026-10-20', 'Storm Scenarios opened to everyone, and the site moved to jerseyweatherchat.com.']
  ];
  var EMAIL = 'jwcliveops@gmail.com';
  var FACEBOOK = 'https://www.facebook.com/groups/jerseywxchat/';
  var PAGES = [
    ['index.html', 'Live Ops'],
    ['jersey-storm-scenarios.html', 'Storm Scenarios'],
    ['jersey-storm-reports.html', 'Storm Reports'],
    ['jersey-space-weather.html', 'Space Weather'],
    ['jersey-sea-tides.html', 'Sea & Tides'],
    ['microclimate-grid.html', 'Microclimate']
  ];

  /* ================= small helpers ================= */
  var K_START = 'jwc-start-page', K_SEEN = 'jwc-news-seen', K_REMEMBER = 'jwc-remember-report', K_DETAILS = 'jwc-report-details';
  function lsGet(k){ try{ return window.localStorage.getItem(k); }catch(e){ return null; } }
  function lsSet(k, v){ try{ window.localStorage.setItem(k, v); }catch(e){} }
  function lsDel(k){ try{ window.localStorage.removeItem(k); }catch(e){} }
  function thisPage(){ var p = location.pathname.split('/').pop(); return p || 'index.html'; }
  function pageName(file){ for(var i = 0; i < PAGES.length; i++){ if(PAGES[i][0] === file) return PAGES[i][1]; } return ''; }
  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c]; }); }
  function ago(ms){
    var m = Math.round((Date.now() - ms) / 60000);
    if(m < 1) return 'just now';
    if(m < 60) return m + ' min ago';
    var h = Math.floor(m / 60); m = m % 60;
    if(h < 48) return h + ' h ' + (m ? m + ' min ' : '') + 'ago';
    return Math.round(h / 24) + ' days ago';
  }
  var MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
  function shortDate(iso){ var p = String(iso).split('-'); return (+p[2]) + ' ' + MONTHS[(+p[1]) - 1]; }

  /* ================= 1. installed app: open on the chosen start page =================
     Only when the app is started from the home screen (index.html?source=app), and only once per launch,
     so the LIVE OPS tab still takes you to Live Ops. */
  (function(){
    try{
      if(!/[?&]source=app(&|$)/.test(location.search) || thisPage() !== 'index.html') return;
      var start = lsGet(K_START);
      if(!start || start === 'index.html' || !pageName(start)) return;
      try{ if(window.sessionStorage.getItem('jwc-start-done')) return; window.sessionStorage.setItem('jwc-start-done', '1'); }catch(e){}
      location.replace(start + '?source=app');
    }catch(e){}
  })();

  /* ================= 2. Systems check: a quiet note of each data request =================
     Wraps the browser's fetch() so every data request the page makes is noted (name, when it last loaded,
     whether it failed). It passes everything through untouched; nothing is stored or sent anywhere. */
  var FEEDS = {}, FEED_ORDER = [], feedListeners = [];
  function classify(url, method){
    if(method && String(method).toUpperCase() !== 'GET') return null;
    var u; try{ u = new URL(url, location.href); }catch(e){ return null; }
    var h = u.hostname, file = u.pathname.split('/').pop(), q = u.searchParams;
    function f(key, name, read, staleMin){ return { key:key, name:name, read:read || '', staleMin:staleMin || 25 }; }
    if(h === 'api.open-meteo.com') return q.get('models') ? f('om-models', 'Wind forecast from several models') : f('om', 'Weather forecast');
    if(h === 'air-quality-api.open-meteo.com') return f('om-aq', 'Air quality');
    if(h === 'marine-api.open-meteo.com') return f('om-marine', 'Sea, waves and sea temperature');
    if(h === 'ensemble-api.open-meteo.com') return f('om-ens', 'Model ensembles');
    if(h === 'api.rainviewer.com') return f('radar', 'Rain radar', 'rainviewer', 30);
    if(h === 'earthquake.usgs.gov') return f('usgs', 'Earthquakes (USGS)', '', 60);
    if(/(^|\.)seismicportal\.eu$/.test(h)) return f('emsc', 'Earthquakes (EMSC)', '', 60);
    if(h === 'api.weather.com') return f('wu', 'Weather Underground stations');
    if(h === 'script.google.com'){
      var a = q.get('action') || 'reports';
      var names = { warnings:'Jersey Met warnings', metar:'Jersey Airport now (METAR)', metar_history:'Jersey Airport, last 13 hours',
                    tides:'Tide times', news:'Weather news', elnino_news:'El Ni\u00F1o news', pws:'Weather Underground stations',
                    reports:'Storm reports', ewx:'Emergencyworx incidents' };
      var reads = { metar:'metar', metar_history:'metarHistory' };
      return f('gas-' + a, names[a] || ('Site data: ' + a), reads[a] || 'status', (a === 'metar' || a === 'metar_history') ? 90 : (a === 'tides' ? 240 : 25));
    }
    if(h === 'services.swpc.noaa.gov'){
      var sw = { 'planetary_k_index_1m.json':['Kp index (NOAA)', 'swpc'], 'rtsw_wind_1m.json':['Solar wind speed (NOAA)', 'swpc'],
                 'rtsw_mag_1m.json':['Solar wind magnetic field (NOAA)', 'swpc'], 'ovation_aurora_latest.json':['Aurora forecast (NOAA)', ''],
                 'alerts.json':['Space weather alerts (NOAA)', ''] };
      var s = sw[file] || ['Space weather data (NOAA)', ''];
      return f('swpc-' + file, s[0], s[1], 60);
    }
    var dataFiles = { 'special-message.json':'Special weather message', 'alerts.json':'Latest alerts sent', 'storm-tracks.json':'Storm Tracker storms',
                      'wind-field.json':'Storm Tracker wind map', 'push-config.json':'Warning alerts set-up', 'sst-anomaly.json':'Sea temperature map' };
    if((h === 'raw.githubusercontent.com' || h === location.hostname) && dataFiles[file]){
      return f('data-' + file, dataFiles[file], file === 'storm-tracks.json' ? 'cycle' : '', file === 'storm-tracks.json' ? 14 * 60 : 25);
    }
    return null;
  }
  function noteFeed(info, patch){
    var r = FEEDS[info.key];
    if(!r){
      r = FEEDS[info.key] = { key:info.key, name:info.name, read:info.read, staleMin:info.staleMin, state:'loading',
                              lastOk:0, lastFail:0, status:0, dataTime:0, problem:'', okN:0, failN:0 };
      FEED_ORDER.push(info.key);
    }
    for(var k in patch){ if(Object.prototype.hasOwnProperty.call(patch, k)) r[k] = patch[k]; }
    for(var i = 0; i < feedListeners.length; i++){ try{ feedListeners[i](); }catch(e){} }
    return r;
  }
  /* reads a time stamp out of the few feeds that carry one, so the check can say how old the data itself is */
  function inspect(r, j){
    var out = { t:0, problem:'' };
    try{
      if(r.read === 'status' || r.read === 'metar' || r.read === 'metarHistory'){
        if(j && typeof j.status === 'string' && /^(error|fail)/i.test(j.status)) out.problem = 'the site\u2019s data service reported an error';
      }
      if(r.read === 'rainviewer'){ var past = j && j.radar && j.radar.past; if(past && past.length) out.t = past[past.length - 1].time * 1000; }
      else if(r.read === 'metar'){ if(j && j.obsTime) out.t = Number(j.obsTime) * 1000; }
      else if(r.read === 'metarHistory'){
        var ob = (j && j.obs) || [], best = 0;
        for(var i = 0; i < ob.length; i++){ var v = Number(ob[i] && ob[i].obsTime) || 0; if(v > best) best = v; }
        out.t = best * 1000;
      }
      else if(r.read === 'cycle'){ var c = j && j.cycle ? new Date(j.cycle).getTime() : 0; if(c && !isNaN(c)) out.t = c; }
      else if(r.read === 'swpc' && Array.isArray(j)){
        var top = 0;
        for(var n = 0; n < j.length; n++){
          var tag = j[n] && j[n].time_tag;
          if(!tag) continue;
          var s = String(tag); if(!/(Z|[+-]\d\d:?\d\d)$/.test(s)) s = s.replace(' ', 'T') + 'Z';
          var ms = Date.parse(s); if(ms > top) top = ms;
        }
        out.t = top;
      }
    }catch(e){}
    return out;
  }
  if(typeof window.fetch === 'function'){
    var pageFetch = window.fetch;
    window.fetch = function(input, init){
      var p = pageFetch.apply(window, arguments);
      try{
        var url = (typeof input === 'string') ? input : (input && input.url) ? input.url : String(input);
        var method = (init && init.method) || (input && typeof input === 'object' && input.method) || 'GET';
        var info = classify(url, method);
        if(info){
          var before = FEEDS[info.key];
          noteFeed(info, { state: before && before.lastOk ? before.state : 'loading' });
          p.then(function(res){
            var r = FEEDS[info.key];
            if(res && res.ok){
              var prevOk = r.lastOk;
              noteFeed(info, { state:'ok', lastOk:Date.now(), status:res.status, okN:r.okN + 1, problem:'' });
              if(r.read){
                try{
                  res.clone().json().then(function(j){
                    var x = inspect(r, j), patch = {};
                    if(x.t) patch.dataTime = x.t;
                    /* the request worked but the data says it failed: count it as a failure, not a success */
                    if(x.problem){ patch.state = 'fail'; patch.problem = x.problem; patch.lastFail = Date.now(); patch.lastOk = prevOk; patch.okN = Math.max(0, r.okN - 1); patch.failN = r.failN + 1; }
                    noteFeed(info, patch);
                  }, function(){});
                }catch(e){}
              }
            } else {
              noteFeed(info, { state:'fail', lastFail:Date.now(), status:res ? res.status : 0, failN:r.failN + 1, problem:'' });
            }
          }, function(){
            var r = FEEDS[info.key];
            noteFeed(info, { state:'fail', lastFail:Date.now(), status:0, failN:r.failN + 1, problem:'' });
          });
        }
      }catch(e){}
      return p;
    };
  }
  /* green = working; amber = the data itself is older than expected (only for feeds that carry a time stamp);
     red = the last try failed. How long ago a feed loaded is shown, but never counted against it, because
     some feeds are only fetched once when the page opens. */
  function feedVerdict(r){
    if(r.state === 'loading' && !r.lastOk) return 'loading';
    if(r.state === 'fail' && (!r.lastOk || r.lastFail >= r.lastOk)) return 'fail';
    if(r.dataTime && (Date.now() - r.dataTime) > r.staleMin * 60000) return 'stale';
    return 'ok';
  }
  function feedDetail(r){
    var v = feedVerdict(r), parts = [];
    if(v === 'loading') return 'Loading\u2026';
    if(v === 'fail'){
      parts.push('Not loading ' + (r.problem ? '(' + r.problem + ')' : (r.status ? '(error ' + r.status + ')' : '(no connection or blocked)')));
      parts.push('last tried ' + ago(r.lastFail));
      if(r.lastOk) parts.push('last worked ' + ago(r.lastOk));
    } else {
      parts.push('Loaded ' + ago(r.lastOk));
      if(r.dataTime){
        var what = r.read === 'rainviewer' ? 'latest radar picture' : r.read === 'cycle' ? 'model run' :
                   (r.read === 'metar' || r.read === 'metarHistory') ? 'latest airport report' : 'latest reading';
        parts.push(what + ' ' + ago(r.dataTime).replace(' ago', ' old'));
      }
    }
    var n = r.okN + r.failN;
    if(n > 1 && r.failN) parts.push(r.okN + ' of ' + n + ' requests worked');
    return parts.join(' \u00B7 ');
  }
  function feedSummary(){
    var ok = 0, stale = 0, fail = 0, loading = 0;
    for(var i = 0; i < FEED_ORDER.length; i++){
      var v = feedVerdict(FEEDS[FEED_ORDER[i]]);
      if(v === 'ok') ok++; else if(v === 'stale') stale++; else if(v === 'fail') fail++; else loading++;
    }
    var total = FEED_ORDER.length;
    if(!total) return { text:'Nothing loaded yet', level:'loading' };
    if(fail) return { text:fail + ' not loading \u00B7 ' + (ok + stale) + ' working', level:'fail' };
    if(stale) return { text:stale + ' running late \u00B7 ' + ok + ' up to date', level:'stale' };
    if(loading) return { text:'Checking ' + total + ' data feeds\u2026', level:'loading' };
    return { text:'All ' + total + ' data feeds working', level:'ok' };
  }

  /* ================= 3. styles (use each page's own colours) ================= */
  var CSS = [
    '.jwcs-navbar{display:flex;align-items:stretch;gap:8px}',
    '.jwcs-navbar>nav.tabs{position:static!important;top:auto!important;flex:1 1 auto;min-width:0;margin:0!important;padding:0!important;background:none!important;border:0!important}',
    '.jwcs-navbar>nav.tabs.jwcs-more{-webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 30px),transparent);mask-image:linear-gradient(90deg,#000 calc(100% - 30px),transparent)}',
    '.jwcs-gear{position:relative;flex:0 0 auto;display:flex;align-items:center;justify-content:center;gap:8px;min-width:44px;padding:0 11px;border:1px solid var(--line,rgba(76,211,255,.14));border-radius:6px;background:transparent;color:var(--dim,#6f8d99);cursor:pointer;font-family:"Orbitron",sans-serif;font-size:10px;letter-spacing:2px;transition:color .2s ease,border-color .2s ease}',
    '.jwcs-gear:hover,.jwcs-gear[aria-expanded="true"]{color:var(--cyan,#4cd3ff);border-color:var(--cyan,#4cd3ff)}',
    '.jwcs-gear svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linejoin:round;transition:transform .5s cubic-bezier(.2,.8,.2,1)}',
    '.jwcs-gear:hover svg{transform:rotate(45deg)}',
    '.jwcs-gear .jwcs-word{display:none}',
    '@media (min-width:900px){.jwcs-gear .jwcs-word{display:inline}}',
    '.jwcs-gear.jwcs-has-news::after{content:"";position:absolute;top:4px;right:4px;width:8px;height:8px;border-radius:50%;background:var(--amber,#ffb020);box-shadow:0 0 0 2px var(--bg,#05080d)}',
    '.jwcs-layer{position:fixed;inset:0;z-index:1500}',
    '.jwcs-layer[hidden]{display:none}',
    '.jwcs-scrim{position:absolute;inset:0;background:rgba(2,6,10,.62);animation:jwcsFade .2s ease}',
    '.jwcs-sheet{position:absolute;top:0;right:0;bottom:0;width:min(440px,100%);display:flex;flex-direction:column;background:linear-gradient(160deg,var(--bg-panel,#0b131c),var(--bg-panel-2,#0e1822));border-left:1px solid var(--line,rgba(76,211,255,.14));box-shadow:-30px 0 60px -30px rgba(0,0,0,.7);color:var(--text,#e9f6f5);font-family:"JetBrains Mono",monospace;text-align:left;animation:jwcsIn .24s cubic-bezier(.2,.8,.2,1);outline:none}',
    '.jwcs-sheet::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:linear-gradient(180deg,var(--cyan,#4cd3ff),transparent 30%,transparent 70%,var(--cyan,#4cd3ff));opacity:.5;pointer-events:none}',
    '@keyframes jwcsIn{from{transform:translateX(28px);opacity:0}to{transform:none;opacity:1}}',
    '@keyframes jwcsUp{from{transform:translateY(28px);opacity:0}to{transform:none;opacity:1}}',
    '@keyframes jwcsFade{from{opacity:0}to{opacity:1}}',
    '@media (max-width:640px){.jwcs-sheet{top:auto;left:0;width:100%;height:92%;border-left:0;border-top:1px solid var(--line,rgba(76,211,255,.14));border-radius:16px 16px 0 0;animation-name:jwcsUp;box-shadow:0 -20px 50px -20px rgba(0,0,0,.6)}.jwcs-sheet::before{display:none}}',
    '@media (prefers-reduced-motion:reduce){.jwcs-sheet,.jwcs-scrim{animation:none}.jwcs-gear svg{transition:none}}',
    '.jwcs-head{display:flex;align-items:center;gap:10px;padding:12px 12px 12px 18px;border-bottom:1px solid var(--line,rgba(76,211,255,.14));min-height:64px;flex:0 0 auto}',
    '.jwcs-head .jwcs-ico{width:20px;height:20px;fill:none;stroke:var(--cyan,#4cd3ff);stroke-width:1.6;stroke-linejoin:round;flex:0 0 auto}',
    '.jwcs-title{font-family:"Orbitron",sans-serif;font-size:14px;letter-spacing:3px;font-weight:700;color:var(--text,#e9f6f5);flex:1;min-width:0;margin:0}',
    '.jwcs-iconbtn{width:44px;height:44px;flex:0 0 auto;display:flex;align-items:center;justify-content:center;border:1px solid var(--line,rgba(76,211,255,.14));border-radius:50%;background:transparent;color:var(--dim,#6f8d99);cursor:pointer;padding:0}',
    '.jwcs-iconbtn:hover{color:var(--text,#e9f6f5);border-color:var(--text,#e9f6f5)}',
    '.jwcs-iconbtn svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}',
    '.jwcs-back{margin-left:-6px;border-color:transparent;border-radius:8px}',
    '.jwcs-ico-wrap{display:flex;flex:0 0 auto}',
    '.jwcs-back[hidden],.jwcs-ico-wrap[hidden]{display:none}',
    '.jwcs-body{flex:1;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;padding:4px 18px calc(28px + env(safe-area-inset-bottom,0px))}',
    '.jwcs-view[hidden]{display:none}',
    '.jwcs-intro{font-size:11px;color:var(--dim,#6f8d99);line-height:1.6;padding:14px 0 2px;margin:0}',
    '.jwcs-group{padding:22px 0 0}',
    '.jwcs-ghead{display:flex;align-items:center;gap:10px;margin-bottom:12px}',
    '.jwcs-ghead h3{font-family:"Orbitron",sans-serif;font-size:11px;letter-spacing:2.5px;font-weight:700;color:var(--cyan,#4cd3ff);white-space:nowrap;margin:0}',
    '.jwcs-ghead .jwcs-bar{flex:1;height:1px;background:linear-gradient(90deg,var(--line,rgba(76,211,255,.14)),transparent)}',
    '.jwcs-label{display:block;font-size:12px;font-weight:700;color:var(--text,#e9f6f5);letter-spacing:.3px}',
    '.jwcs-desc{font-size:11px;color:var(--dim,#6f8d99);line-height:1.6;margin:3px 0 0}',
    '.jwcs-desc a,.jwcs-prose a{color:var(--cyan,#4cd3ff)}',
    '.jwcs-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 0 0;margin-top:14px;border-top:1px solid var(--line,rgba(76,211,255,.14))}',
    '.jwcs-row.first{border-top:0;margin-top:0;padding-top:0}',
    '.jwcs-txt{min-width:0}',
    '.jwcs-select{-webkit-appearance:none;appearance:none;flex:0 0 auto;max-width:190px;min-height:40px;padding:8px 32px 8px 12px;border:1px solid var(--line,rgba(76,211,255,.14));border-radius:8px;background:var(--bg,#05080d) url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 24 24%27%3E%3Cpath d=%27M6 9l6 6 6-6%27 fill=%27none%27 stroke=%27%236f8d99%27 stroke-width=%272%27 stroke-linecap=%27round%27/%3E%3C/svg%3E") no-repeat right 10px center/14px;color:var(--text,#e9f6f5);font-family:"JetBrains Mono",monospace;font-size:11px;font-weight:700;letter-spacing:1px;cursor:pointer}',
    '.jwcs-select:hover,.jwcs-select:focus{border-color:var(--cyan,#4cd3ff)}',
    '.jwcs-select option{background:#0b131c;color:#e9f6f5}',
    '.jwcs-switch{position:relative;flex:0 0 auto;width:52px;height:44px;border:0;background:transparent;cursor:pointer;padding:0}',
    '.jwcs-switch::before{content:"";position:absolute;left:3px;right:3px;top:9px;height:26px;border-radius:13px;background:rgba(255,255,255,.06);border:1px solid var(--line,rgba(76,211,255,.14));transition:background .18s ease,border-color .18s ease}',
    '.jwcs-switch::after{content:"";position:absolute;left:8px;top:14px;width:16px;height:16px;border-radius:50%;background:var(--dim,#6f8d99);transition:transform .18s cubic-bezier(.2,.8,.2,1),background .18s ease}',
    '.jwcs-switch[aria-checked="true"]::before{background:var(--green,#2de1a0);border-color:var(--green,#2de1a0)}',
    '.jwcs-switch[aria-checked="true"]::after{transform:translateX(20px);background:#05080d}',
    '.jwcs-btn{flex:0 0 auto;min-height:40px;padding:8px 14px;font-family:"JetBrains Mono",monospace;font-size:10px;font-weight:800;letter-spacing:1.5px;border:1px solid var(--line,rgba(76,211,255,.14));border-radius:7px;background:transparent;color:var(--cyan,#4cd3ff);cursor:pointer}',
    '.jwcs-btn:hover{border-color:var(--cyan,#4cd3ff);background:rgba(76,211,255,.08)}',
    '.jwcs-btn[hidden]{display:none}',
    '.jwcs-acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}',
    '.jwcs-nav{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:48px;width:100%;padding:9px 0;border:0;border-top:1px solid var(--line,rgba(76,211,255,.14));background:transparent;color:var(--text,#e9f6f5);font-family:"JetBrains Mono",monospace;font-size:12px;text-align:left;text-decoration:none;cursor:pointer}',
    '.jwcs-nav.first{border-top:0}',
    '.jwcs-nav:hover{color:var(--cyan,#4cd3ff)}',
    '.jwcs-nav>span{display:flex;flex-direction:column;gap:2px;min-width:0}',
    '.jwcs-nav b{font-weight:700}',
    '.jwcs-nav small{font-size:10.5px;color:var(--dim,#6f8d99);line-height:1.5}',
    '.jwcs-nav svg{width:16px;height:16px;flex:0 0 auto;fill:none;stroke:var(--dim,#6f8d99);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}',
    '.jwcs-sum{display:inline-flex;align-items:center;gap:6px}',
    '.jwcs-sum i{width:7px;height:7px;border-radius:50%;background:var(--dim,#6f8d99);flex:0 0 auto}',
    '.jwcs-sum.ok i{background:var(--green,#2de1a0)}.jwcs-sum.stale i{background:var(--amber,#ffb020)}.jwcs-sum.fail i{background:var(--red,#ff4b5c)}',
    '.jwcs-news{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}',
    '.jwcs-news li{display:grid;grid-template-columns:58px 1fr;gap:10px;font-size:11px;line-height:1.55;color:var(--text,#e9f6f5);margin:0}',
    '.jwcs-news time{font-size:9.5px;letter-spacing:1.2px;color:var(--dim,#6f8d99);font-weight:800;padding-top:2px;white-space:nowrap}',
    '.jwcs-news li.is-new time{color:var(--amber,#ffb020)}',
    '.jwcs-news li.is-new time::after{content:" \\2022";color:var(--amber,#ffb020)}',
    '.jwcs-more-btn{margin-top:12px}',
    '.jwcs-foot{margin:26px 0 0;padding-top:14px;border-top:1px solid var(--line,rgba(76,211,255,.14));font-size:10px;letter-spacing:1px;color:var(--dim,#6f8d99);line-height:1.7}',
    '.jwcs-foot b{color:var(--text,#e9f6f5)}',
    '.jwcs-prose{font-size:11px;line-height:1.75;letter-spacing:.3px;color:var(--text,#e9f6f5);padding-top:2px}',
    '.jwcs-prose p{margin:0 0 8px}',
    '.jwcs-prose ul{margin:0;padding-left:18px}',
    '.jwcs-prose li{margin:0 0 7px;color:var(--dim,#6f8d99)}',
    '.jwcs-prose b{color:var(--text,#e9f6f5)}',
    '.jwcs-fb{display:flex;align-items:center;gap:10px;background:linear-gradient(90deg,rgba(24,119,242,.12),rgba(24,119,242,.03));border:1px solid rgba(24,119,242,.35);border-radius:10px;padding:10px 14px;margin:16px 0 10px}',
    '.jwcs-fb-ic{flex:0 0 auto;width:26px;height:26px;border-radius:50%;background:#1877F2;display:flex;align-items:center;justify-content:center}',
    '.jwcs-fb-tx{font-size:11.5px;color:var(--text,#e9f6f5);letter-spacing:.3px;line-height:1.5}',
    '.jwcs-fb-tx b{color:var(--cyan,#4cd3ff)}',
    '.jwcs-talk{margin:0 0 14px;padding:8px 10px;border:1px solid rgba(76,211,255,.25);border-radius:8px;background:rgba(76,211,255,.04);font-size:11px;line-height:1.55;color:var(--dim,#6f8d99)}',
    '.jwcs-talk b{color:var(--text,#e9f6f5)}',
    '.jwcs-talk a{color:var(--cyan,#4cd3ff)}',
    '.jwcs-feeds{display:flex;flex-direction:column;margin-top:12px}',
    '.jwcs-feed{display:grid;grid-template-columns:12px 1fr;gap:3px 10px;padding:10px 0;border-top:1px solid var(--line,rgba(76,211,255,.14))}',
    '.jwcs-feed i{width:9px;height:9px;border-radius:50%;margin-top:4px;background:var(--dim,#6f8d99)}',
    '.jwcs-feed.ok i{background:var(--green,#2de1a0)}.jwcs-feed.stale i{background:var(--amber,#ffb020)}.jwcs-feed.fail i{background:var(--red,#ff4b5c)}',
    '.jwcs-feed.loading i{animation:jwcsBlink 1.2s ease-in-out infinite}',
    '@keyframes jwcsBlink{50%{opacity:.3}}',
    '.jwcs-feed b{font-size:11.5px;font-weight:700;color:var(--text,#e9f6f5)}',
    '.jwcs-feed small{grid-column:2;font-size:10.5px;color:var(--dim,#6f8d99);line-height:1.5;letter-spacing:.2px}',
    '.jwcs-feed.fail small{color:var(--red,#ff4b5c)}',
    '.jwcs-feed.stale small{color:var(--amber,#ffb020)}',
    '.jwcs-note{font-size:10.5px;color:var(--dim,#6f8d99);line-height:1.6;margin:14px 0 0;padding:8px 10px;border:1px dashed var(--line,rgba(76,211,255,.14));border-radius:8px}',
    '.jwcs-remember,.field .jwcs-remember{display:flex;align-items:center;gap:8px;margin:0;font-size:10.5px;letter-spacing:.3px;color:var(--dim,#6f8d99);cursor:pointer;line-height:1.4;text-transform:none}',
    '.jwcs-remember input,.field .jwcs-remember input{width:16px;height:16px;min-height:0;flex:0 0 auto;margin:0;padding:0;border:0;background:none;accent-color:var(--cyan,#4cd3ff);cursor:pointer}',
    '.jwcs-sheet :focus-visible,.jwcs-gear:focus-visible{outline:2px solid var(--cyan,#4cd3ff);outline-offset:2px}'
  ].join('\n');
  (function(){
    try{
      var st = document.createElement('style');
      st.id = 'jwcs-style';
      st.textContent = CSS;
      (document.head || document.documentElement).appendChild(st);
    }catch(e){}
  })();

  /* ================= 4. icons ================= */
  var GEAR = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19.76,10.07 L21.86,9.37 L21.86,14.63 L19.76,13.93 L18.85,16.13 L20.83,17.11 L17.11,20.83 L16.13,18.85 L13.93,19.76 L14.63,21.86 L9.37,21.86 L10.07,19.76 L7.87,18.85 L6.89,20.83 L3.17,17.11 L5.15,16.13 L4.24,13.93 L2.14,14.63 L2.14,9.37 L4.24,10.07 L5.15,7.87 L3.17,6.89 L6.89,3.17 L7.87,5.15 L10.07,4.24 L9.37,2.14 L14.63,2.14 L13.93,4.24 L16.13,5.15 L17.11,3.17 L20.83,6.89 L18.85,7.87Z"/><circle cx="12" cy="12" r="3.4"/></svg>';
  var CHEV = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';
  var FB_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="#ffffff" aria-hidden="true"><path d="M22 12.06C22 6.505 17.523 2 12 2S2 6.505 2 12.06c0 5.02 3.657 9.184 8.438 9.94v-7.03H7.898v-2.91h2.54V9.845c0-2.522 1.492-3.915 3.777-3.915 1.094 0 2.238.196 2.238.196v2.475h-1.26c-1.243 0-1.63.775-1.63 1.57v1.888h2.773l-.443 2.91h-2.33V22c4.78-.756 8.437-4.92 8.437-9.94z"/></svg>';

  /* ================= 5. the panel's contents ================= */
  var ABOUT_HTML =
    '<div class="jwcs-fb"><span class="jwcs-fb-ic">' + FB_ICON + '</span><span class="jwcs-fb-tx">Live data and storm reporting for the Facebook <b>Jersey Weather Chat</b> community &middot; run by volunteers &middot; forecasts are computer models, not official warnings</span></div>' +
    '<div class="jwcs-talk">&#128172; <b>Anxious about the weather?</b> You&rsquo;re not alone &mdash; <a href="' + FACEBOOK + '" target="_blank" rel="noopener">chat with others in the Jersey Weather Chat group</a> (open its <b>Chats</b> tab and join any chat).</div>' +
    '<div class="jwcs-prose">' +
    '<p><b>About this site.</b> Jersey Weather Chat is a community run by volunteers who love weather. This site pulls together public data and computer forecast models so islanders can see what&rsquo;s coming and make their own decisions. It&rsquo;s free, it always will be, and nobody&rsquo;s paid to do it.</p>' +
    '<p><b>A few things to know before relying on it</b></p>' +
    '<ul>' +
    '<li><b>Forecasts are predictions, not facts.</b> They change with every update and are often wrong about timing and strength several days out. That&rsquo;s why we show several models side by side &mdash; so you can see how much they agree before you decide.</li>' +
    '<li><b>This is not the official forecast or warning service.</b> The authority for Jersey is <a href="https://www.gov.je/weather" target="_blank" rel="noopener">Jersey Met (gov.je)</a>. Always check their warnings and follow official advice; if we and they disagree, trust Jersey Met.</li>' +
    '<li><b>We post about storms because the models show one &mdash; not to alarm anyone.</b> We know Ciar&aacute;n and Goretti left a mark. A storm that fizzles is good news, and we&rsquo;ll say so. Please don&rsquo;t shoot the messenger.</li>' +
    '<li><b>Use at your own risk.</b> The site, its data and the Facebook group are provided &ldquo;as is&rdquo;. We can&rsquo;t accept responsibility for decisions you make or any loss or damage arising from using them &mdash; boats, travel, outdoor work, events, property or anything else.</li>' +
    '<li><b>The data isn&rsquo;t ours.</b> It comes from NOAA, Open-Meteo, Jersey Met, USGS, EMSC, Weather Underground and others, and can be late, incomplete or wrong. The site itself may go down, usually at the worst moment.</li>' +
    '<li><b>Storm reports are from members</b> and are not verified.</li>' +
    '<li><b>Warning alerts are automated.</b> If you tap ALERT ME, this device gets a notification when our checker spots a new or upgraded Jersey Met warning on gov.je. It can lag behind or miss one, and it never replaces the official warning. We store the anonymous push address your phone gives us, plus the browser type and when you signed up &mdash; no name, email or location &mdash; and TURN OFF removes it.</li>' +
    '<li><b>Special weather messages to your phone are a responsibility we take seriously.</b> A post on this site is something you choose to read; a notification on your phone is not, so we hold ourselves to a higher standard and only send one when we believe it genuinely matters. Our system checks the live data on this site around the clock &mdash; airport observations from Jersey, Guernsey and Alderney, local weather stations, forecast models, air pressure, and tides with wave heights &mdash; against set thresholds, such as gusts of 70&nbsp;mph or more, or big waves arriving at high tide. Reaching a threshold never sends anything on its own: it alerts the site&rsquo;s admin, who checks the readings and decides whether a message is warranted. Every suggestion, the readings behind it, the decision made and the exact message sent are recorded, so we can be held to account. Occasionally we may send a message without a threshold being met &mdash; for example to pass on important official advice &mdash; and those are recorded too. A special message is never an official warning: Jersey Met remains the authority.</li>' +
    '<li><b>Privacy.</b> See <a href="#" data-jwcs-go="privacy">Privacy &amp; cookies</a> for what little we hold and how to see or delete it.</li>' +
    '<li><b>In an emergency call 999.</b></li>' +
    '</ul></div>';

  var PRIVACY_HTML =
    '<div class="jwcs-prose" style="padding-top:16px">' +
    '<p><b>Privacy.</b> Jersey Weather Chat is registered with the Jersey Office of the Information Commissioner (<a href="https://portal.jerseyoic.org/Registrations/RegistrySearch/Index" target="_blank" rel="noopener">registration no. 104194</a>). We hold very little personal data, and only to run this site: the notification address, browser type and sign-up time for alerts (TURN OFF deletes them); the name, report and optional email you give with a storm report; and visit counts through Google Analytics. It is kept in our Google account and passes through GitHub and your browser maker&rsquo;s notification service, so it may be held outside Jersey. We never sell or share it. To see, correct or delete your data, email ' + EMAIL + '; you can also complain to the <a href="https://jerseyoic.org" target="_blank" rel="noopener">Jersey Office of the Information Commissioner</a>.</p>' +
    '<p><b>On this device.</b> Your settings, and your storm report name and parish if you choose to keep them, are saved in this browser only. They are never sent to us.</p>' +
    '</div>' +
    '<div class="jwcs-row"><div class="jwcs-txt"><span class="jwcs-label">Analytics cookies</span><p class="jwcs-desc" id="jwcs-cookie-state">Not chosen yet</p></div><button type="button" class="jwcs-btn" data-cookie-settings>COOKIE CHOICES</button></div>' +
    '<div class="jwcs-row"><div class="jwcs-txt"><span class="jwcs-label" id="jwcs-rem-lbl">Remember my storm report details</span><p class="jwcs-desc">Fills in your name and parish on the Storm Reports form next time.</p></div><button type="button" class="jwcs-switch" role="switch" aria-checked="true" aria-labelledby="jwcs-rem-lbl" id="jwcs-rem"></button></div>' +
    '<div class="jwcs-acts"><button type="button" class="jwcs-btn" id="jwcs-forget" hidden>FORGET SAVED DETAILS</button></div>' +
    '<div style="margin-top:14px">' +
    '<a class="jwcs-nav" href="alerts-help.html#privacy"><span><b>Full privacy note</b><small>Everything we hold, why, and for how long</small></span>' + CHEV + '</a>' +
    '<a class="jwcs-nav" href="https://portal.jerseyoic.org/Registrations/RegistrySearch/Index" target="_blank" rel="noopener"><span><b>Check our registration</b><small>Jersey Office of the Information Commissioner, no. 104194</small></span>' + CHEV + '</a>' +
    '</div>';

  function startOptions(){
    var cur = lsGet(K_START) || 'index.html', h = '';
    for(var i = 0; i < PAGES.length; i++) h += '<option value="' + PAGES[i][0] + '"' + (PAGES[i][0] === cur ? ' selected' : '') + '>' + esc(PAGES[i][1].toUpperCase()) + '</option>';
    return h;
  }
  var MAIN_HTML =
    '<p class="jwcs-intro">Saved on this device only. No account, and nothing is sent to us.</p>' +
    '<div class="jwcs-group"><div class="jwcs-ghead"><h3>WHAT&rsquo;S NEW</h3><span class="jwcs-bar"></span></div>' +
      '<ul class="jwcs-news" id="jwcs-news-top"></ul>' +
      '<button type="button" class="jwcs-btn jwcs-more-btn" data-jwcs-go="news">ALL UPDATES</button></div>' +
    '<div class="jwcs-group"><div class="jwcs-ghead"><h3>APP</h3><span class="jwcs-bar"></span></div>' +
      '<div class="jwcs-row first"><div class="jwcs-txt"><label class="jwcs-label" for="jwcs-start">Start page</label><p class="jwcs-desc">The page the app opens on when you start it from your home screen.</p></div>' +
      '<select class="jwcs-select" id="jwcs-start">' + startOptions() + '</select></div></div>' +
    '<div class="jwcs-group"><div class="jwcs-ghead"><h3>ABOUT &amp; HELP</h3><span class="jwcs-bar"></span></div>' +
      '<button type="button" class="jwcs-nav first" data-jwcs-go="about"><span><b>About &amp; disclaimer</b><small>Volunteer-run &middot; not official warnings &middot; Jersey Met is the authority</small></span>' + CHEV + '</button>' +
      '<button type="button" class="jwcs-nav" data-jwcs-go="privacy"><span><b>Privacy &amp; cookies</b><small>Registered with the JOIC, no. 104194 &middot; your cookie choice &middot; saved details</small></span>' + CHEV + '</button>' +
      '<button type="button" class="jwcs-nav" data-jwcs-go="systems"><span><b>Systems check</b><small class="jwcs-sum" id="jwcs-sys-sum"><i></i><span>How fresh this page&rsquo;s data is</span></small></span>' + CHEV + '</button>' +
      '<a class="jwcs-nav" href="alerts-help.html"><span><b>Step-by-step help</b><small>Warning alerts, installing the app, fixing problems</small></span>' + CHEV + '</a>' +
      '<a class="jwcs-nav" id="jwcs-report" href="mailto:' + EMAIL + '"><span><b>Report a problem</b><small>Email ' + EMAIL + ' &middot; the systems check is added for you</small></span>' + CHEV + '</a>' +
      '<a class="jwcs-nav" href="' + FACEBOOK + '" target="_blank" rel="noopener"><span><b>Jersey Weather Chat on Facebook</b><small>Chat with others, share photos and reports</small></span>' + CHEV + '</a></div>' +
    '<p class="jwcs-foot"><b>In an emergency call 999.</b><br>JERSEY WEATHER CHAT &middot; VOLUNTEER-RUN &middot; SINCE 2011</p>';

  var SYSTEMS_HTML =
    '<p class="jwcs-intro" id="jwcs-sys-intro"></p>' +
    '<div class="jwcs-feeds" id="jwcs-feeds"></div>' +
    '<p class="jwcs-note">Pictures and maps (radar tiles, satellite, lightning, NOAA charts) load separately and aren&rsquo;t listed. Green: working. Amber: running late. Red: not loading right now &mdash; the page keeps trying by itself.</p>' +
    '<div class="jwcs-acts"><button type="button" class="jwcs-btn" id="jwcs-reload">RELOAD PAGE</button><a class="jwcs-btn" id="jwcs-report2" href="mailto:' + EMAIL + '" style="text-decoration:none;display:inline-flex;align-items:center">REPORT A PROBLEM</a></div>';

  var NEWS_HTML = '<ul class="jwcs-news" id="jwcs-news-all" style="padding-top:16px"></ul>';

  var TITLES = { main:'SETTINGS', about:'ABOUT & DISCLAIMER', privacy:'PRIVACY & COOKIES', systems:'SYSTEMS CHECK', news:'WHAT\u2019S NEW' };

  /* ================= 6. building the gear and the panel ================= */
  var layer, sheet, titleEl, backBtn, headIco, gearBtn, views = {}, current = 'main', lastFocus = null, sysTimer = null, newsSeenBefore = '';

  function newsUnseen(){ var seen = lsGet(K_SEEN) || ''; return WHATS_NEW.length && WHATS_NEW[0][0] > seen; }
  function renderNews(listId, max){
    var ul = document.getElementById(listId); if(!ul) return;
    var cutoff = newsSeenBefore || '', h = '';
    var newest = WHATS_NEW.length ? WHATS_NEW[0][0] : '';
    for(var i = 0; i < WHATS_NEW.length && i < max; i++){
      var d = WHATS_NEW[i][0];
      var isNew = cutoff ? d > cutoff : d === newest;      /* first visit: only the latest day counts as new */
      h += '<li class="' + (isNew ? 'is-new' : '') + '"><time datetime="' + d + '">' + shortDate(d) + '</time><span>' + esc(WHATS_NEW[i][1]) + '</span></li>';
    }
    ul.innerHTML = h;
  }
  function cookieState(){
    var el = document.getElementById('jwcs-cookie-state'); if(!el) return;
    var c = null; try{ c = window.jwcCookieConsent && window.jwcCookieConsent.get(); }catch(e){}
    el.textContent = c === 'granted' ? 'Accepted on this device' : c === 'denied' ? 'Declined on this device' : 'Not chosen yet';
  }
  function rememberOn(){ return lsGet(K_REMEMBER) !== 'off'; }
  function readDetails(){ try{ return JSON.parse(lsGet(K_DETAILS) || 'null'); }catch(e){ return null; } }
  function syncRemember(){
    var sw = document.getElementById('jwcs-rem'), fg = document.getElementById('jwcs-forget'), cb = document.getElementById('jwcs-rem-cb');
    if(sw) sw.setAttribute('aria-checked', rememberOn() ? 'true' : 'false');
    if(fg) fg.hidden = !readDetails();
    if(cb) cb.checked = rememberOn();
  }
  function setRemember(on){
    lsSet(K_REMEMBER, on ? 'on' : 'off');
    if(!on) lsDel(K_DETAILS);
    syncRemember();
  }
  function renderSystems(){
    var sum = feedSummary();
    var s = document.getElementById('jwcs-sys-sum');
    if(s){ s.className = 'jwcs-sum ' + sum.level; s.querySelector('span').textContent = sum.text; }
    var intro = document.getElementById('jwcs-sys-intro');
    if(intro) intro.textContent = 'Data this page (' + (pageName(thisPage()) || document.title) + ') has loaded since you opened it. Pages fetch fresh data by themselves every few minutes.';
    var box = document.getElementById('jwcs-feeds');
    if(!box || current !== 'systems') return;
    if(!FEED_ORDER.length){ box.innerHTML = '<div class="jwcs-feed loading"><i></i><b>Nothing loaded yet</b><small>Give the page a few seconds, then look again.</small></div>'; return; }
    var rank = { fail:0, stale:1, loading:2, ok:3 }, keys = FEED_ORDER.slice();
    keys.sort(function(a, b){ return rank[feedVerdict(FEEDS[a])] - rank[feedVerdict(FEEDS[b])]; });
    var h = '';
    for(var i = 0; i < keys.length; i++){
      var r = FEEDS[keys[i]], v = feedVerdict(r);
      h += '<div class="jwcs-feed ' + v + '"><i></i><b>' + esc(r.name) + '</b><small>' + esc(feedDetail(r)) + '</small></div>';
    }
    box.innerHTML = h;
  }
  function mailtoHref(){
    var lines = ['Page: ' + location.href.split('#')[0], 'When: ' + new Date().toString().slice(0, 24), 'Device: ' + navigator.userAgent.slice(0, 160), '', 'Systems check:'];
    for(var i = 0; i < FEED_ORDER.length; i++){ var r = FEEDS[FEED_ORDER[i]]; lines.push('- ' + r.name + ': ' + feedVerdict(r).toUpperCase() + ' - ' + feedDetail(r)); }
    lines.push('', 'What went wrong (please describe):', '');
    var body = lines.join('\n');
    if(body.length > 1600) body = body.slice(0, 1600) + '\n...';
    return 'mailto:' + EMAIL + '?subject=' + encodeURIComponent('Jersey Weather Chat website problem') + '&body=' + encodeURIComponent(body);
  }

  function show(view){
    if(!views[view]) view = 'main';
    current = view;
    for(var k in views){ if(Object.prototype.hasOwnProperty.call(views, k)) views[k].hidden = (k !== view); }
    titleEl.textContent = TITLES[view] || 'SETTINGS';
    backBtn.hidden = (view === 'main');
    headIco.hidden = (view !== 'main');
    if(view === 'privacy'){ cookieState(); syncRemember(); }
    if(view === 'systems') renderSystems();
    if(view === 'news') renderNews('jwcs-news-all', 99);
    var body = sheet.querySelector('.jwcs-body'); if(body) body.scrollTop = 0;
  }
  function open(view){
    if(!layer) return;
    if(layer.hidden){
      lastFocus = document.activeElement;
      newsSeenBefore = lsGet(K_SEEN) || '';
      layer.hidden = false;
      document.body.style.overflow = 'hidden';
      if(gearBtn) gearBtn.setAttribute('aria-expanded', 'true');
      renderNews('jwcs-news-top', 3);
      if(WHATS_NEW.length) lsSet(K_SEEN, WHATS_NEW[0][0]);
      if(gearBtn) gearBtn.classList.remove('jwcs-has-news');
      clearInterval(sysTimer);
      sysTimer = setInterval(renderSystems, 5000);
    }
    show(view || 'main');
    renderSystems();
    /* first visit: leave room at the bottom so the cookie banner doesn't hide the last rows */
    try{
      var cc = document.getElementById('jwc-cc'), body = sheet.querySelector('.jwcs-body');
      body.style.paddingBottom = (cc && !cc.hidden && cc.offsetHeight) ? (cc.offsetHeight + 28) + 'px' : '';
    }catch(e){}
    focusHead();
  }
  /* after opening or switching pages inside the panel, put keyboard focus on Back (or Close) so it isn't lost */
  function focusHead(){
    setTimeout(function(){
      try{ (backBtn && !backBtn.hidden ? backBtn : sheet.querySelector('.jwcs-close')).focus({ preventScroll:true }); }catch(e){}
    }, 30);
  }
  function close(){
    if(!layer || layer.hidden) return;
    layer.hidden = true;
    document.body.style.overflow = '';
    clearInterval(sysTimer);
    if(gearBtn) gearBtn.setAttribute('aria-expanded', 'false');
    if(/^#(settings|about|systems|whats-new)$/.test(location.hash)){ try{ history.replaceState(null, '', location.pathname + location.search); }catch(e){} }
    try{ if(lastFocus && lastFocus.focus) lastFocus.focus(); }catch(e){}
  }

  function buildGear(){
    var nav = document.querySelector('nav.tabs');
    if(!nav || nav.parentNode.classList.contains('jwcs-navbar')) return;
    /* move the tab bar's sticky position, spacing and background onto a new row that holds the tabs and the gear */
    var cs = window.getComputedStyle(nav);
    var bar = document.createElement('div');
    bar.className = 'jwcs-navbar';
    bar.style.position = cs.position;
    bar.style.top = cs.top;
    bar.style.zIndex = cs.zIndex;
    bar.style.margin = cs.marginTop + ' ' + cs.marginRight + ' ' + cs.marginBottom + ' ' + cs.marginLeft;
    bar.style.padding = cs.paddingTop + ' ' + cs.paddingRight + ' ' + cs.paddingBottom + ' ' + cs.paddingLeft;
    bar.style.background = cs.backgroundColor;
    bar.style.borderBottom = cs.borderBottomWidth + ' ' + cs.borderBottomStyle + ' ' + cs.borderBottomColor;
    nav.parentNode.insertBefore(bar, nav);
    bar.appendChild(nav);
    gearBtn = document.createElement('button');
    gearBtn.type = 'button';
    gearBtn.className = 'jwcs-gear' + (newsUnseen() ? ' jwcs-has-news' : '');
    gearBtn.setAttribute('aria-label', 'Settings');
    gearBtn.setAttribute('aria-haspopup', 'dialog');
    gearBtn.setAttribute('aria-expanded', 'false');
    gearBtn.innerHTML = GEAR + '<span class="jwcs-word">SETTINGS</span>';
    gearBtn.addEventListener('click', function(){ open('main'); });
    bar.appendChild(gearBtn);
    /* fade the right-hand end of the tabs while there are more tabs to scroll to */
    function more(){ nav.classList.toggle('jwcs-more', nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 2); }
    nav.addEventListener('scroll', more, { passive:true });
    window.addEventListener('resize', more);
    more();
  }

  function buildSheet(){
    layer = document.createElement('div');
    layer.className = 'jwcs-layer';
    layer.hidden = true;
    layer.innerHTML =
      '<div class="jwcs-scrim"></div>' +
      '<div class="jwcs-sheet" role="dialog" aria-modal="true" aria-labelledby="jwcs-title" tabindex="-1">' +
        '<div class="jwcs-head">' +
          '<button type="button" class="jwcs-iconbtn jwcs-back" aria-label="Back to settings" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>' +
          '<span class="jwcs-ico-wrap">' + GEAR.replace('<svg ', '<svg class="jwcs-ico" ') + '</span>' +
          '<h2 class="jwcs-title" id="jwcs-title">SETTINGS</h2>' +
          '<button type="button" class="jwcs-iconbtn jwcs-close" aria-label="Close settings"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
        '</div>' +
        '<div class="jwcs-body">' +
          '<div class="jwcs-view" data-view="main">' + MAIN_HTML + '</div>' +
          '<div class="jwcs-view" data-view="about" hidden>' + ABOUT_HTML + '</div>' +
          '<div class="jwcs-view" data-view="privacy" hidden>' + PRIVACY_HTML + '</div>' +
          '<div class="jwcs-view" data-view="systems" hidden>' + SYSTEMS_HTML + '</div>' +
          '<div class="jwcs-view" data-view="news" hidden>' + NEWS_HTML + '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(layer);
    sheet = layer.querySelector('.jwcs-sheet');
    titleEl = layer.querySelector('.jwcs-title');
    backBtn = layer.querySelector('.jwcs-back');
    headIco = layer.querySelector('.jwcs-ico-wrap');
    var vs = layer.querySelectorAll('.jwcs-view');
    for(var i = 0; i < vs.length; i++) views[vs[i].getAttribute('data-view')] = vs[i];

    layer.querySelector('.jwcs-scrim').addEventListener('click', close);
    layer.querySelector('.jwcs-close').addEventListener('click', close);
    backBtn.addEventListener('click', function(){ show('main'); focusHead(); });
    layer.addEventListener('click', function(e){
      var go = e.target.closest ? e.target.closest('[data-jwcs-go]') : null;
      if(go){ e.preventDefault(); show(go.getAttribute('data-jwcs-go')); focusHead(); return; }
      if(e.target.closest && e.target.closest('[data-cookie-settings]')) setTimeout(cookieState, 400);
    });
    /* keyboard: Esc closes; Tab stays inside the panel while it is open */
    document.addEventListener('keydown', function(e){
      if(!layer || layer.hidden) return;
      if(e.key === 'Escape'){ e.preventDefault(); close(); return; }
      if(e.key !== 'Tab') return;
      var f = sheet.querySelectorAll('a[href],button:not([hidden]),select,input,[tabindex]:not([tabindex="-1"])');
      var list = [];
      for(var i = 0; i < f.length; i++){ if(f[i].offsetParent !== null) list.push(f[i]); }
      if(!list.length) return;
      var first = list[0], last = list[list.length - 1];
      if(!sheet.contains(document.activeElement)){ e.preventDefault(); first.focus(); }
      else if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
    });
    var sel = document.getElementById('jwcs-start');
    if(sel) sel.addEventListener('change', function(){ if(sel.value === 'index.html') lsDel(K_START); else lsSet(K_START, sel.value); });
    var sw = document.getElementById('jwcs-rem');
    if(sw) sw.addEventListener('click', function(){ setRemember(!rememberOn()); });
    var fg = document.getElementById('jwcs-forget');
    if(fg) fg.addEventListener('click', function(){ lsDel(K_DETAILS); syncRemember(); });
    var rl = document.getElementById('jwcs-reload');
    if(rl) rl.addEventListener('click', function(){ location.reload(); });
    ['jwcs-report', 'jwcs-report2'].forEach(function(id){
      var a = document.getElementById(id);
      if(a) a.addEventListener('click', function(){ a.href = mailtoHref(); });
    });
    feedListeners.push(function(){ if(layer && !layer.hidden) renderSystems(); });
  }

  /* buttons and links anywhere on a page can open the panel: data-jwc-open="about" etc., or #settings / #about in the address */
  function wireOpeners(){
    document.addEventListener('click', function(e){
      var t = e.target.closest ? e.target.closest('[data-jwc-open]') : null;
      if(!t) return;
      e.preventDefault();
      open(t.getAttribute('data-jwc-open') || 'main');
    });
    function fromHash(){
      var h = location.hash;
      if(h === '#settings') open('main');
      else if(h === '#about') open('about');
      else if(h === '#systems') open('systems');
      else if(h === '#whats-new') open('news');
    }
    window.addEventListener('hashchange', fromHash);
    fromHash();
  }

  /* ================= 7. Storm Reports: remember name and parish on this device ================= */
  function wireReportForm(){
    var form = document.getElementById('reportForm');
    var nameEl = document.getElementById('f-name'), parishEl = document.getElementById('f-parish');
    if(!form || !nameEl || !parishEl) return;
    var label = document.createElement('label');
    label.className = 'jwcs-remember';
    label.innerHTML = '<input type="checkbox" id="jwcs-rem-cb"> <span>Remember my name and parish on this device</span>';
    nameEl.parentNode.appendChild(label);
    var cb = label.querySelector('input');
    cb.checked = rememberOn();
    cb.addEventListener('change', function(){ setRemember(cb.checked); });
    function fill(){
      if(!rememberOn()) return;
      var d = readDetails(); if(!d) return;
      if(!nameEl.value && d.name) nameEl.value = d.name;
      if(!parishEl.value && d.parish){
        for(var i = 0; i < parishEl.options.length; i++){ if(parishEl.options[i].value === d.parish){ parishEl.value = d.parish; break; } }
      }
    }
    /* runs just before the page sends the report (the page then clears the form) */
    form.addEventListener('submit', function(){
      if(!rememberOn()) return;
      var n = nameEl.value.trim(), p = parishEl.value;
      if(n || p) lsSet(K_DETAILS, JSON.stringify({ name:n, parish:p }));
    }, true);
    /* after a report is sent the page empties the form; put the name and parish straight back */
    form.addEventListener('reset', function(){ setTimeout(fill, 0); });
    fill();
  }

  /* ================= 8. start-up ================= */
  function init(){
    try{ buildSheet(); }catch(e){ return; }
    try{ buildGear(); }catch(e){}
    try{ wireOpeners(); }catch(e){}
    try{ wireReportForm(); }catch(e){}
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.JWCSettings = { open:open, close:close, feeds:function(){ return FEEDS; } };
})();
