/* Jersey Weather Chat - cookie choice banner + Google Analytics loader
 *
 * What this file does:
 *   - Google Analytics is NOT loaded, and no analytics cookies are set, until a visitor taps ACCEPT.
 *   - DECLINE (or ignoring the banner) means nothing is loaded and nothing is set.
 *   - The choice is remembered on the visitor's own device for 12 months, then they are asked again.
 *   - Any link or button with the attribute  data-cookie-settings  reopens the banner so people can
 *     change their mind (there is one in the privacy note on alerts-help.html).
 *
 * Every page loads this with:  <script src="cookie-consent.js" defer></script>
 * It replaces the old Google Analytics snippet that used to sit at the top of each page.
 * To change the Analytics ID or the wording, edit the lines just below.
 */
(function(){
  'use strict';

  var GA_ID      = 'G-74T6F4C6YH';
  var KEY        = 'jwc-cookie-consent';
  var MONTHS     = 12;                       // how long a choice is remembered
  var PRIVACY    = 'alerts-help.html#privacy';

  var TEXT = 'We&rsquo;d like to use Google Analytics cookies to count visits and see which pages get used. ' +
             'They don&rsquo;t identify you and aren&rsquo;t used for advertising. ' +
             'Nothing is set unless you accept, and you can change your mind any time.';

  /* ---------- remembered choice (kept on this device only) ---------- */
  var memoryChoice = null;                   // fallback if the browser blocks local storage

  function readChoice(){
    var raw = null;
    try{ raw = localStorage.getItem(KEY); }catch(e){}
    if(!raw && memoryChoice) return memoryChoice;
    if(!raw) return null;
    try{
      var o = JSON.parse(raw);
      if(!o || (o.choice !== 'granted' && o.choice !== 'denied')) return null;
      var age = Date.now() - Number(o.ts || 0);
      if(age > MONTHS * 30.5 * 86400000) return null;   // expired - ask again
      return o.choice;
    }catch(e){ return null; }
  }

  function saveChoice(choice){
    memoryChoice = choice;
    try{ localStorage.setItem(KEY, JSON.stringify({ v:1, choice:choice, ts:Date.now() })); }catch(e){}
  }

  /* ---------- Google Analytics: only ever started after ACCEPT ---------- */
  var gaStarted = false;

  function startAnalytics(){
    window['ga-disable-' + GA_ID] = false;
    if(gaStarted) return;
    gaStarted = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = function(){ window.dataLayer.push(arguments); };
    window.gtag('js', new Date());
    window.gtag('config', GA_ID);
    var s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + GA_ID;
    document.head.appendChild(s);
  }

  function stopAnalytics(){
    window['ga-disable-' + GA_ID] = true;     // stops any further hits being sent
    deleteAnalyticsCookies();
  }

  function deleteAnalyticsCookies(){
    var names = [];
    document.cookie.split(';').forEach(function(c){
      var n = c.split('=')[0].replace(/^\s+/, '');
      if(n === '_ga' || n === '_gid' || n.indexOf('_ga_') === 0 || n.indexOf('_gat') === 0) names.push(n);
    });
    if(!names.length) return;
    var host = location.hostname, parts = host.split('.'), domains = [host, '.' + host];
    for(var i = 1; i < parts.length - 1; i++) domains.push('.' + parts.slice(i).join('.'));
    names.forEach(function(n){
      domains.concat(['']).forEach(function(d){
        document.cookie = n + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/' + (d ? '; domain=' + d : '');
      });
    });
  }

  /* ---------- banner ---------- */
  var banner = null;

  function injectStyles(){
    if(document.getElementById('jwc-cc-style')) return;
    var st = document.createElement('style');
    st.id = 'jwc-cc-style';
    st.textContent =
      '#jwc-cc{position:fixed;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom,0px));z-index:2000;' +
        'max-width:760px;margin:0 auto;background:#0b131c;border:1px solid rgba(76,211,255,0.4);border-radius:12px;' +
        'padding:16px 18px;box-shadow:0 14px 40px rgba(0,0,0,0.65);color:#e9f6f5;' +
        'font-family:"JetBrains Mono",monospace;font-size:11.5px;line-height:1.65;text-align:left;}' +
      '#jwc-cc[hidden]{display:none;}' +
      'body:has(#mediaBackdrop.open) #jwc-cc:not(.jwc-cc-force){display:none;}' +
      '#jwc-cc .jwc-cc-title{font-family:"Orbitron",sans-serif;font-size:11px;font-weight:700;letter-spacing:2px;color:#4cd3ff;margin-bottom:6px;}' +
      '#jwc-cc p{margin:0 0 8px;}' +
      '#jwc-cc .jwc-cc-status{color:#5f7c88;font-size:10px;letter-spacing:1px;margin-bottom:10px;}' +
      '#jwc-cc a{color:#4cd3ff;text-decoration:none;font-size:10px;letter-spacing:1px;white-space:nowrap;}' +
      '#jwc-cc a:hover{text-decoration:underline;}' +
      '#jwc-cc .jwc-cc-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px;}' +
      '#jwc-cc button{flex:1 1 120px;min-width:110px;padding:11px 14px;border-radius:7px;cursor:pointer;' +
        'background:transparent;border:1px solid #4cd3ff;color:#4cd3ff;' +
        'font-family:"Orbitron",sans-serif;font-size:11px;font-weight:700;letter-spacing:2px;transition:background .15s ease;}' +
      '#jwc-cc button:hover,#jwc-cc button:focus-visible{background:rgba(76,211,255,0.14);outline:none;}' +
      '#jwc-cc .jwc-cc-link{flex:1 1 100%;text-align:right;}';
    document.head.appendChild(st);
  }

  function buildBanner(){
    injectStyles();
    banner = document.createElement('div');
    banner.id = 'jwc-cc';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-label', 'Cookie choices');
    banner.hidden = true;
    banner.innerHTML =
      '<div class="jwc-cc-title">COOKIES</div>' +
      '<p>' + TEXT + '</p>' +
      '<div class="jwc-cc-status" id="jwc-cc-status"></div>' +
      '<div class="jwc-cc-row">' +
        '<button type="button" id="jwc-cc-accept">ACCEPT</button>' +
        '<button type="button" id="jwc-cc-decline">DECLINE</button>' +
        '<a class="jwc-cc-link" id="jwc-cc-more" href="' + PRIVACY + '">COOKIE DETAILS &rarr;</a>' +
      '</div>';
    document.body.appendChild(banner);

    document.getElementById('jwc-cc-accept').addEventListener('click', function(){ choose('granted'); });
    document.getElementById('jwc-cc-decline').addEventListener('click', function(){ choose('denied'); });

    /* On the help page the privacy note is a pop-up on the same page, so open it directly. */
    document.getElementById('jwc-cc-more').addEventListener('click', function(e){
      var btn = document.getElementById('privacyOpen');
      if(btn){ e.preventDefault(); btn.click(); }
    });
  }

  function showBanner(forced){
    if(!banner) buildBanner();
    var cur = readChoice();
    var status = document.getElementById('jwc-cc-status');
    status.textContent = cur === 'granted' ? 'CURRENT CHOICE: ACCEPTED' :
                         cur === 'denied'  ? 'CURRENT CHOICE: DECLINED' : '';
    status.style.display = status.textContent ? '' : 'none';
    banner.classList.toggle('jwc-cc-force', !!forced);
    banner.hidden = false;
  }

  function hideBanner(){ if(banner){ banner.hidden = true; banner.classList.remove('jwc-cc-force'); } }

  function choose(choice){
    saveChoice(choice);
    if(choice === 'granted') startAnalytics(); else stopAnalytics();
    hideBanner();
  }

  /* ---------- start-up ---------- */
  function init(){
    var existing = readChoice();
    if(existing === 'granted') startAnalytics();
    else if(existing === 'denied') window['ga-disable-' + GA_ID] = true;
    else showBanner(false);

    /* any element marked data-cookie-settings reopens the banner */
    document.addEventListener('click', function(e){
      var t = e.target.closest ? e.target.closest('[data-cookie-settings]') : null;
      if(!t) return;
      e.preventDefault();
      showBanner(true);
    });
  }

  window.jwcCookieConsent = {
    open: function(){ showBanner(true); },
    get:  function(){ return readChoice(); }
  };

  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
