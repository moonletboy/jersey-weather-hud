// Jersey Weather Chat - snapshot logger (Stages 1 and 3)
// ------------------------------------------------
// Every 30 minutes (GitHub Actions: .github/workflows/snapshot-logger.yml) this records what the site was
// showing, and posts it to the "JWC Snapshots" Google Sheet through its own small receiver script, so that
// after Storm Austen the forecasts can be compared with what actually happened.
//
// "Snapshots" tab (one row per run): Live Ops "now" conditions and storm indices, rain since midnight,
// the five-model gust forecast, the Jersey Airport report (METAR) and the Jersey Met warnings.
// "Forecast Snapshots" tab (one row per forecast day): the storm scenarios page's numbers, worked out with
// the page's own code. The receiver also emails Matthew when new model runs change the scenarios.
// Each data source is fetched separately with retries; if one fails the row is still written, with blanks
// for that part and a note in the "error" column.
//
//   node scripts/snapshot-logger.mjs            -> fetch and post one snapshot
//   node scripts/snapshot-logger.mjs --dry-run  -> fetch and print it, post nothing
//
// Settings (GitHub secrets / workflow): SNAPSHOT_URL, SNAPSHOT_TOKEN, SOURCE (schedule | manual | heartbeat)

const DRY = process.argv.includes('--dry-run');
const SOURCE = (process.env.SOURCE || 'manual').trim();
const LAT = 49.1805, LON = -2.1058;                    // Live Ops point (index.html LAT/LON and ST_JERSEY)
const FEED_URL = 'https://prodgojweatherstorage.blob.core.windows.net/data/JerseyWarningStatusXML.json';
const WF_MODELS = [                                    // index.html WF_MODELS
  { id:'gfs_seamless',         key:'gfs' },
  { id:'ecmwf_ifs025',         key:'ecmwf' },
  { id:'icon_seamless',        key:'icon' },
  { id:'ukmo_seamless',        key:'ukmo' },
  { id:'meteofrance_seamless', key:'meteofrance' }
];

// ---------- copied verbatim from index.html ----------
function windShearMph(dir1, speed1, dir2, speed2){
const u1 = -speed1 * Math.sin(dir1 * Math.PI/180), v1 = -speed1 * Math.cos(dir1 * Math.PI/180);
const u2 = -speed2 * Math.sin(dir2 * Math.PI/180), v2 = -speed2 * Math.cos(dir2 * Math.PI/180);
return Math.hypot(u2-u1, v2-v1);
}
const GREEN = 'green', AMBER = 'amber', RED = 'red';   // (colour names only - the page uses these for display)
function classifyStormMode(cape, cin, shearMph){
if(cape < 300 || (cin <= -100 && cape < 1500)){
return { tier:0, label:'STORMS UNLIKELY', color:GREEN };
}
if(shearMph < 23){
return { tier:1, label:'ISOLATED PULSE STORMS POSSIBLE', color: cape>=1500?AMBER:GREEN };
}
if(shearMph < 40){
return { tier:2, label:'MULTICELL CLUSTERS POSSIBLE', color:AMBER };
}
if(shearMph < 58 || cape < 1500){
return { tier:3, label:'ORGANISED MULTICELL / SQUALL LINE POSSIBLE', color:RED };
}
return { tier:4, label:'ISOLATED SUPERCELL POTENTIAL', color:RED };
}
// -----------------------------------------------------

const errors = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function getJson(url, label, tries = 3){
  let last;
  for(let i = 0; i < tries; i++){
    try{
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 30000);
      const r = await fetch(url, { signal: ctl.signal, headers: { 'User-Agent': 'JerseyWeatherChat-SnapshotLogger/1.0 (jwcliveops@gmail.com)' } });
      clearTimeout(t);
      if(!r.ok) throw new Error('HTTP ' + r.status);
      const txt = (await r.text()).replace(/^\uFEFF/, '');
      return JSON.parse(txt);
    }catch(e){ last = e; await sleep(2000 * Math.pow(2, i)); }
  }
  errors.push(label + ': ' + (last && last.message ? last.message : last));
  return null;
}
const r1 = v => (v === null || v === undefined || isNaN(v)) ? '' : Math.round(v * 10) / 10;
const r0 = v => (v === null || v === undefined || isNaN(v)) ? '' : Math.round(v);
const KT = 1.15078;

function localTime(d){
  return new Intl.DateTimeFormat('en-GB', { timeZone:'Europe/London', year:'numeric', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', hour12:false }).format(d).replace(',', '');
}

async function liveOps(row){
  const base = `https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&wind_speed_unit=mph&timezone=auto`;
  const fast = await getJson(base + '&current=temperature_2m,relative_humidity_2m,apparent_temperature,uv_index,weather_code,wind_speed_10m,wind_gusts_10m,wind_direction_10m,pressure_msl,cloud_cover,is_day&forecast_hours=2', 'live ops now');
  const storm = await getJson(base + '&hourly=cape,lifted_index,dew_point_2m,convective_inhibition,wind_speed_500hPa,wind_direction_500hPa&past_hours=12', 'storm indices');
  const cur = fast && fast.current;
  if(cur){
    Object.assign(row, {
      now_time: cur.time, now_temp_c: r1(cur.temperature_2m), now_humidity_pct: r0(cur.relative_humidity_2m),
      now_feels_c: r1(cur.apparent_temperature), now_uv: r1(cur.uv_index), now_weather_code: cur.weather_code,
      now_wind_mph: r1(cur.wind_speed_10m), now_gust_mph: r1(cur.wind_gusts_10m), now_wind_dir: r0(cur.wind_direction_10m),
      now_pressure_hpa: r1(cur.pressure_msl), now_cloud_pct: r0(cur.cloud_cover)
    });
  }
  if(storm && storm.hourly && cur){
    const h = storm.hourly, nowIso = cur.time.slice(0, 13);          // same hour-matching as wxRenderStorm()
    let idx = h.time.findIndex(t => t.slice(0, 13) === nowIso);
    if(idx < 0) idx = 0;
    const cape = h.cape[idx], cin = h.convective_inhibition[idx];
    const sp5 = h.wind_speed_500hPa[idx], dr5 = h.wind_direction_500hPa[idx];
    const shear = windShearMph(cur.wind_direction_10m, cur.wind_speed_10m, dr5, sp5);
    const mode = classifyStormMode(cape, cin, shear);
    Object.assign(row, {
      storm_cape: r0(cape), storm_lifted_index: r1(h.lifted_index[idx]), storm_dewpoint_c: r1(h.dew_point_2m[idx]),
      storm_cin: r0(cin), storm_wind500_mph: r1(sp5), storm_dir500: r0(dr5), storm_shear_mph: r1(shear),
      storm_tier: mode.tier, storm_label: mode.label
    });
  }
}

async function rainfall(row){                            // index.html loadRainfall(), without the browser's display clamp
  const d = await getJson(`https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&current=precipitation&hourly=precipitation&past_days=1&forecast_days=1&timezone=auto&precipitation_unit=mm`, 'rainfall');
  if(!d || !d.current) return;
  const todayDate = d.current.time.slice(0, 10), nowPrefix = d.current.time.slice(0, 13), hT = d.hourly.time;
  let first = hT.findIndex(t => t.slice(0, 10) === todayDate); if(first < 0) first = 0;
  let nowIdx = hT.findIndex(t => t.slice(0, 13) === nowPrefix); if(nowIdx < 0) nowIdx = first;
  let sum = 0;
  for(let i = first; i <= nowIdx; i++){ const v = d.hourly.precipitation[i]; if(v !== null && v !== undefined) sum += v; }
  row.rain_since_midnight_mm = r1(sum);
}

async function fiveModels(row){                          // index.html wfJerseyLoad() request
  const r = await getJson('https://api.open-meteo.com/v1/forecast?latitude=' + LAT + '&longitude=' + LON +
    '&hourly=wind_gusts_10m,wind_speed_10m&wind_speed_unit=mph&timezone=GMT&forecast_days=10&models=' + WF_MODELS.map(m => m.id).join(','), 'five-model gusts');
  if(!r || !r.hourly) return;
  const times = r.hourly.time, idx = {};
  times.forEach((t, i) => { idx[t] = i; });
  const hk = new Date().toISOString().slice(0, 13) + ':00';
  const t0 = idx[hk] || 0;
  WF_MODELS.forEach(m => {
    const g = r.hourly['wind_gusts_10m_' + m.id];
    if(!g || !g.some(v => v != null)) { row[m.key + '_peak48_mph'] = ''; return; }
    // strongest 3-hour average, as wfJerseyPeak(): over the next 48 h, and over the whole run (what the page shows)
    const spell = (limit) => {
      let best = -1, bt = -1;
      for(let t = Math.max(1, t0); t < Math.min(g.length - 1, limit); t++){
        if(g[t-1] == null || g[t] == null || g[t+1] == null) continue;
        const a = (g[t-1] + g[t] + g[t+1]) / 3;
        if(a > best){ best = a; bt = t; }
      }
      return bt >= 0 ? { v: best, t: times[bt] } : { v: null, t: '' };
    };
    const p48 = spell(t0 + 48), pAll = spell(g.length - 1);
    row[m.key + '_peak48_mph'] = r1(p48.v); row[m.key + '_peak48_time_utc'] = p48.t;
    row[m.key + '_peak_all_mph'] = r1(pAll.v); row[m.key + '_peak_all_time_utc'] = pAll.t;
    [6, 12, 24, 48].forEach(h => { const v = g[t0 + h]; row[m.key + '_gust_' + h + 'h_mph'] = (v == null) ? '' : r1(v); });
  });
}

async function metar(row){                               // direct from aviationweather.gov (no browser CORS limits here)
  const arr = await getJson('https://aviationweather.gov/api/data/metar?ids=EGJJ&format=json', 'METAR');
  if(!arr || !arr.length) return;
  const m = arr[0], raw = m.rawOb || '';
  const w = raw.match(/\b(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?KT\b/);
  const q = raw.match(/\bQ(\d{4})\b/);
  const wxs = (m.wxString || '');
  Object.assign(row, {
    metar_obs_utc: m.obsTime ? new Date(m.obsTime * 1000).toISOString() : '', metar_raw: raw,
    metar_temp_c: r1(m.temp), metar_dewp_c: r1(m.dewp), metar_wind_dir: w ? w[1] : (m.wdir ?? ''),
    metar_wind_kt: w ? +w[2] : (m.wspd ?? ''), metar_gust_kt: w && w[3] ? +w[3] : (m.wgst ?? ''),
    metar_wind_mph: w ? Math.round(+w[2] * KT) : '', metar_gust_mph: w && w[3] ? Math.round(+w[3] * KT) : '',
    metar_visibility: m.visib ?? '', metar_qnh_hpa: q ? +q[1] : r0(m.altim), metar_weather: wxs,
    metar_clouds: (m.clouds || []).map(c => c.cover + (c.base != null ? String(Math.round(c.base / 100)).padStart(3, '0') : '')).join(' '),
    metar_flight_cat: m.fltCat || '',
    // thunder only from the weather codes (TS, VCTS, TSRA, +TSRA, RETS...); CB clouds are recorded separately
    metar_thunder: /TS/.test(wxs) || /(^|\s)(\+|-|VC|RE)?TS[A-Z]*(?=\s|$)/.test(raw) ? 'Y' : 'N',
    metar_cb: /\d{3}CB\b|(^|\s)CB(?=\s|$)/.test(raw) ? 'Y' : 'N'
  });
}

async function warnings(row){                            // gov.je's own warning feed (free, no ZenRows)
  const d = await getJson(FEED_URL + '?t=' + Math.floor(Date.now() / 60000), 'warnings feed');
  if(!d) return;
  const types = [['WindWarning','WindWarningData','Wind'],['ThunderWarning','ThunderWarningData','Thunderstorm'],['TideWarning','TideWarningData','Tide'],
                 ['SnowWarning','SnowWarningData','Snow'],['SnowAlert','SnowAlertData','Snow'],['IceWarning','IceWarningData','Ice']];
  const rank = { yellow:1, orange:2, red:3 }, list = [];
  let top = 0;
  types.forEach(([flag, data, name]) => {
    const w = d[flag] ? d[data] : null;
    if(!w) return;
    let c = String(w.Colour || '').toLowerCase(); if(!rank[c]) c = 'orange';
    top = Math.max(top, rank[c]);
    const issued = String(w.Issued || '').replace(/^\s*issued\s+(at\s+)?/i, '').replace(/[\s.]+$/, '');
    list.push(c.toUpperCase() + ' ' + (w.WarningType || name) + ' (' + issued + ')' + (w.WarningMessage ? ': ' + String(w.WarningMessage).trim() : ''));
  });
  row.warn_severity = ['none', 'yellow', 'orange', 'red'][top];
  row.warn_count = list.length;
  row.warn_list = list.join(' | ');
  row.warn_feed_checked_utc = d.lastChecked || '';
}


// ================= STORM SCENARIOS (Stage 3) =================
// Everything inside SCN is copied WORD FOR WORD from jersey-storm-scenarios.html (constants, model lists,
// loadEnsemble, loadDet, dayStats, detPeaks, headText, sigOf...), minus three browser-only lines, so the
// numbers logged and emailed are exactly the ones the page shows. If the page's maths changes, copy it again.
const SCN = (function(){
const LAT = 49.21, LON = -2.13;
const ENS = [
{ id:'ecmwf_ifs025',              lbl:'ECMWF',          col:'#4cd3ff', meta:['ecmwf_ifs025_ensemble'] },
{ id:'gfs_seamless',              lbl:'GFS',            col:'#b48cff', meta:['ncep_gefs025'] },
{ id:'icon_seamless',             lbl:'ICON',           col:'#ffd166', meta:['dwd_icon_eu_eps', 'dwd_icon_eps'] },   // the European ICON ensemble is the one in use (about 5 days ahead)
{ id:'ukmo_global_ensemble_20km', lbl:'UK MET OFFICE',  col:'#ff8fab', nm:'UK Met Office', meta:['ukmo_global_ensemble_20km'] }
];
const DET = [
{ id:'gfs_seamless',         lbl:'GFS' },
{ id:'ecmwf_ifs025',         lbl:'ECMWF' },
{ id:'icon_seamless',        lbl:'ICON' },
{ id:'ukmo_seamless',        lbl:'UK MET OFFICE' },
{ id:'meteofrance_seamless', lbl:'M\u00c9T\u00c9O-FRANCE', sub:'(AROME + ARPEGE)' }
];
/* gust levels, strongest first. hex = the colour used in the charts */
const TIERS = [
{ min:100, hex:'#ffffff', col:'#ffffff',      bg:'rgba(255,255,255,0.14)', head:'EXCEPTIONAL GUSTS &middot; 100+ MPH', body:'Hurricane strength. Danger to life; buildings badly damaged, roofs and power lines down, island-wide disruption. Shelter indoors away from windows and follow official advice.' },
{ min:90,  hex:'#c04dff', col:'#c04dff',      bg:'rgba(192,77,255,0.14)',  head:'SEVERE, DANGEROUS GUSTS', body:'Danger to life from flying debris and falling trees. Serious damage to buildings and long power cuts. Stay indoors and do not travel.' },
{ min:80,  hex:'#ff2fa8', col:'#ff2fa8',      bg:'rgba(255,47,168,0.12)',  head:'VERY DAMAGING GUSTS',     body:'Widespread damage to roofs, trees and power lines; flying debris. Avoid all travel and keep well away from the coast.' },
{ min:70,  hex:'#ff4b5c', col:'var(--red)',   bg:'rgba(255,75,92,0.16)',   head:'DAMAGING GUSTS LIKELY',   body:'Structural damage and power cuts possible. Stay away from the coast and avoid travel if you can.' },
{ min:58,  hex:'#ff4b5c', col:'var(--red)',   bg:'rgba(255,75,92,0.08)',   head:'DAMAGING GUSTS POSSIBLE', body:'Roof tiles, fences and trees at risk. Secure loose items; take care near the coast.' },
{ min:47,  hex:'#ffb020', col:'var(--amber)', bg:'rgba(255,176,32,0.12)',  head:'STRONG GUSTS',            body:'Branches down and debris likely; difficult driving, especially for high-sided vehicles.' },
{ min:39,  hex:'#ffb020', col:'var(--amber)', bg:'rgba(255,176,32,0.06)',  head:'GALE-FORCE GUSTS',        body:'Loose items blow about; hard going on bikes and for high-sided vehicles.' }
];
/* the shaded zones on the charts, and their labels */
const BANDS = [
[39, 47,  'rgba(255,176,32,0.06)',  'GALE'],
[47, 58,  'rgba(255,176,32,0.12)',  'STRONG'],
[58, 70,  'rgba(255,75,92,0.10)',   'DAMAGING'],
[70, 80,  'rgba(255,75,92,0.20)',   '70+'],
[80, 90,  'rgba(255,47,168,0.20)',  '80+'],
[90, 100, 'rgba(192,77,255,0.22)',  '90+'],
[100, 400,'rgba(255,255,255,0.14)', '100+']
];
/* the speeds the chance figures are given for */
const THRESH = [39, 50, 60, 70, 80, 90, 100];
const THCOL = { 39:'#ffb020', 50:'#ffb020', 60:'#ff6a3d', 70:'#ff4b5c', 80:'#ff2fa8', 90:'#c04dff', 100:'#ffffff' };
function tierOf(mph){ for(const t of TIERS) if(mph >= t.min) return t; return null; }
const SC = { ens:[], det:null, days:[], stats:{}, day:null, fetched:null, userPicked:false, W:1000, F:1 };
const DAYF = new Intl.DateTimeFormat('en-GB', { timeZone:'UTC', weekday:'short', day:'numeric', month:'short' });
const DAYS = new Intl.DateTimeFormat('en-GB', { timeZone:'UTC', weekday:'short' });
const FETCHF = new Intl.DateTimeFormat('en-GB', { timeZone:'Europe/London', weekday:'short', day:'numeric', month:'short', hour:'2-digit', minute:'2-digit', hourCycle:'h23' });
function nc(s){ return String(s).replace(/,/g, ''); }
function dayDate(d){ const p = d.split('-').map(Number); return new Date(Date.UTC(p[0], p[1] - 1, p[2], 12)); }
function pct(a, p){ if(!a.length) return null; const s = a.slice().sort((x, y) => x - y); const i = (s.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i); return s[lo] + (s[hi] - s[lo]) * (i - lo); }
function med(a){ return pct(a, 0.5); }
function hh(h){ return String(h).padStart(2, '0') + ':00'; }
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' }[c])); }

async function getJson(url, ms){
const ctl = new AbortController();
const t = setTimeout(() => ctl.abort(), ms || 25000);
try{ const r = await fetch(url, { signal:ctl.signal }); if(!r.ok) throw new Error('HTTP ' + r.status); return await r.json(); }
finally{ clearTimeout(t); }
}
async function loadEnsemble(m){
const url = 'https://ensemble-api.open-meteo.com/v1/ensemble?latitude=' + LAT + '&longitude=' + LON +
'&hourly=wind_gusts_10m,wind_speed_10m&models=' + m.id + '&wind_speed_unit=mph&timezone=Europe%2FLondon&forecast_days=7';
try{
const j = await getJson(url);
const h = j.hourly, runs = [];
Object.keys(h).forEach(function(k){
if(k.indexOf('wind_gusts_10m') !== 0 || !h[k].some(v => v != null)) return;
runs.push({ g:h[k], w:h['wind_speed_10m' + k.slice(14)] || [] });
});
if(!runs.length) return null;
const byDay = {};
h.time.forEach(function(t, i){ (byDay[t.slice(0, 10)] = byDay[t.slice(0, 10)] || []).push(i); });
return { m:m, time:h.time, runs:runs, byDay:byDay };
}catch(e){ console.warn('ensemble ' + m.id + ' not loaded', e); return null; }
}
async function loadDet(){
try{
const j = await getJson('https://api.open-meteo.com/v1/forecast?latitude=' + LAT + '&longitude=' + LON +
'&hourly=wind_gusts_10m&models=' + DET.map(d => d.id).join(',') + '&wind_speed_unit=mph&timezone=Europe%2FLondon&forecast_days=7');
return j.hourly;
}catch(e){ console.warn('main models not loaded', e); return null; }
}

/* one record per run for the day: its peak gust, peak average wind, first/last hour at 60+, and its 24 hourly gusts */
function dayStats(day){
const recs = [];
SC.ens.forEach(function(md){
const idx = md.byDay[day];
if(!idx) return;
md.runs.forEach(function(r){
let pk = -1, wpk = -1, n = 0, f60 = -1, l60 = -1;
const h24 = new Array(24).fill(null);
idx.forEach(function(i){
const v = r.g[i];
if(v == null) return;
n++;
const hr = Number(md.time[i].slice(11, 13));
h24[hr] = v;
if(v > pk) pk = v;
if(r.w[i] != null && r.w[i] > wpk) wpk = r.w[i];
if(v >= 60){ if(f60 < 0) f60 = hr; l60 = hr; }
});
if(n >= 18) recs.push({ model:md.m, pk:pk, wpk:wpk, f60:f60, l60:l60, h24:h24 });
});
});
if(recs.length < 10) return null;
const peaks = recs.map(r => r.pk);
const share = function(x){ return Math.round(100 * peaks.filter(v => v >= x).length / peaks.length); };
const st = { day:day, n:recs.length, recs:recs, c50:share(50), c60:share(60), c70:share(70), ch:{}, models:{} };
THRESH.forEach(function(t){ st.ch[t] = share(t); });
recs.forEach(function(r){ st.models[r.model.lbl] = (st.models[r.model.lbl] || 0) + 1; });
/* percentiles of all the runs' peaks (used for the day tiles, the main-runs chart and "since you last looked") */
st.q10 = pct(peaks, 0.1); st.q50 = pct(peaks, 0.5); st.q90 = pct(peaks, 0.9);
/* The three scenarios are RANGES of peak gust, so each has a real chance: the share of runs in it.
   The two dividing lines are impact levels (39, 47, 58, 70, 80, 90, 100 mph), picked for each day
   so the split reads like a forecast: about a quarter / half / quarter where possible, with at
   least 8% in each outer range, and the middle range spanning no more than two impact levels.
   The MIDDLE range must always be the biggest share (strictly more runs than either outer range),
   so the middle card is always the most common outcome. If no pair of levels gives that (for
   example all runs bunched together), the lines fall on the 25th and 75th percentiles instead,
   rounded to the nearest 5 mph where that keeps the middle the biggest. */
const LV = [39, 47, 58, 70, 80, 90, 100];
const below = function(b){ return peaks.filter(v => v < b).length / peaks.length; };
let cut = null;
for(let i = 0; i < LV.length; i++) for(let j = i + 1; j < LV.length && j <= i + 2; j++){
const lo = below(LV[i]), hi = 1 - below(LV[j]), mid = 1 - lo - hi;
if(lo < 0.08 || hi < 0.08) continue;
const p0 = Math.round(100 * lo), p2 = Math.round(100 * hi), p1 = 100 - p0 - p2;      // the same rounded figures the cards show
if(!(p1 > p0 && p1 > p2)) continue;                                                // the middle range must be the biggest share
const score = Math.abs(lo - 0.25) + Math.abs(mid - 0.5) + Math.abs(hi - 0.25);
if(!cut || score < cut.score) cut = { b1:LV[i], b2:LV[j], score:score, levels:true };
}
if(!cut){
const mk = function(a, b){ return { b1:a, b2:Math.max(a + 1, b), levels:false }; };
const midBig = function(c){
const n = peaks.length, l = peaks.filter(v => v < c.b1).length, h = peaks.filter(v => v >= c.b2).length;
const p0 = Math.round(100 * l / n), p2 = Math.round(100 * h / n);
return (100 - p0 - p2) > p0 && (100 - p0 - p2) > p2;
};
const tries = [];
[[0.25, 0.75], [0.30, 0.70], [0.35, 0.65], [0.40, 0.60]].forEach(function(q){
const a = pct(peaks, q[0]), b = pct(peaks, q[1]);
tries.push(mk(Math.round(a / 5) * 5, Math.round(b / 5) * 5), mk(Math.round(a), Math.round(b)));
});
cut = tries.find(midBig) || null;
if(!cut){                                                          // last resort: try every whole-mph pair and keep the one closest to a quarter / half / quarter
const mn = Math.floor(Math.min.apply(null, peaks)), mx = Math.ceil(Math.max.apply(null, peaks)) + 1;
let bestS = 9;
for(let a = mn; a <= mx; a++) for(let b = a + 1; b <= mx + 1; b++){
const c = { b1:a, b2:b, levels:false };
if(!midBig(c)) continue;
const l = below(a), h = 1 - below(b), sc = Math.abs(l - 0.25) + Math.abs(1 - l - h - 0.5) + Math.abs(h - 0.25);
if(sc < bestS){ bestS = sc; cut = c; }
}
if(!cut) cut = tries[1];                                           // runs so identical that no split can work: the page falls back to "in between" and a star
}
}
st.b1 = cut.b1; st.b2 = cut.b2; st.levelCut = cut.levels;
const groups = [recs.filter(r => r.pk < cut.b1), recs.filter(r => r.pk >= cut.b1 && r.pk < cut.b2), recs.filter(r => r.pk >= cut.b2)];
const pcs = groups.map(g => Math.round(100 * g.length / recs.length));
pcs[1] = 100 - pcs[0] - pcs[2];                                   // so the three always add up to 100%
const biggest = pcs.indexOf(Math.max.apply(null, pcs));
/* The best case is shown as a real range, from the calmest run in it (rounded down to 5 mph) up to just
   under the first dividing line, so "60-69" is never mistaken for "calm": gusts in the high 60s can
   still bring down tiles and branches, and the impact box underneath says so. */
const bestLo = groups[0].length ? Math.floor(Math.min.apply(null, groups[0].map(r => r.pk)) / 5) * 5 : null;
const bestRange = (bestLo !== null && bestLo < cut.b1 - 1) ? bestLo + '\u2013' + (cut.b1 - 1) : 'UNDER ' + cut.b1;
st.sc = [
{ key:'best',   lbl:'BEST CASE',   emo:'\ud83d\udfe2', range:bestRange.replace('\u2013', '&ndash;'), rangeTxt:bestRange.toLowerCase() },
{ key:'likely', lbl:biggest === 1 ? 'MOST COMMON' : 'IN BETWEEN', emo:'\ud83d\udfe0', range:cut.b1 + '&ndash;' + (cut.b2 - 1), rangeTxt:cut.b1 + '\u2013' + (cut.b2 - 1) },
{ key:'worst',  lbl:'WORST CASE',  emo:'\ud83d\udd34', range:cut.b2 + '+', rangeTxt:cut.b2 + '+' }
].map(function(s, i){
const g = groups[i];
s.n = g.length; s.p = pcs[i]; s.top = (i === biggest);
if(!g.length){ s.v = i === 0 ? cut.b1 - 3 : i === 1 ? (cut.b1 + cut.b2) / 2 : cut.b2 + 3; s.wind = 0; s.win = null; s.lean = []; s.leanNm = []; return s; }
s.v = med(g.map(r => r.pk));                                      // the typical peak gust within this range
s.wind = med(g.map(r => r.wpk));
const hit = g.filter(r => r.f60 >= 0);
s.win = hit.length >= g.length / 2 ? [Math.round(med(hit.map(r => r.f60))), Math.max(Math.round(med(hit.map(r => r.f60))) + 1, Math.round(med(hit.map(r => r.l60))) + 1)] : null;   // [start hour, hour it ends]
/* which models are over-represented in this range */
const cnt = {}, nmOf = {};
g.forEach(r => { cnt[r.model.lbl] = (cnt[r.model.lbl] || 0) + 1; nmOf[r.model.lbl] = r.model.nm || r.model.lbl; });
s.lean = Object.keys(cnt).filter(m => cnt[m] >= 2 && cnt[m] / g.length >= 1.4 * st.models[m] / st.n).sort((x, y) => cnt[y] - cnt[x]);
s.leanNm = s.lean.map(m => nmOf[m]);
return s;
});
/* hour by hour spread for the fan chart */
st.fan = [];
for(let h = 0; h < 24; h++){
const v = recs.map(r => r.h24[h]).filter(x => x != null);
st.fan.push(v.length >= 5 ? { h:h, p10:pct(v, 0.1), p25:pct(v, 0.25), p50:pct(v, 0.5), p75:pct(v, 0.75), p90:pct(v, 0.9) } : null);
}
return st;
}
function detPeaks(day){
const h = SC.det;
if(!h) return [];
const idx = [];
h.time.forEach((t, i) => { if(t.slice(0, 10) === day) idx.push(i); });
return DET.map(function(d){
const s = h['wind_gusts_10m_' + d.id];
if(!s) return null;
const v = idx.map(i => s[i]).filter(x => x != null);
return v.length >= 18 ? { lbl:d.lbl, sub:d.sub || '', pk:Math.max.apply(null, v) } : null;
}).filter(Boolean);
}


/* the big headline sentence and its colour (used on the page and in the Facebook picture) */
function headText(st){
/* lead with the highest level that most runs reach (60+ on an ordinary stormy day) */
const base = [100, 90, 80, 70].find(t => st.ch[t] >= 50) || 60;
const cb = st.ch[base], n10 = Math.round(cb / 10);
let h;
if(st.q90 < 39) h = 'NO GALES EXPECTED &mdash; EVEN THE WILDEST RUNS KEEP GUSTS BELOW 39 MPH';
else if(cb < 5) h = '60+ MPH GUSTS VERY UNLIKELY &mdash; MIDDLE RUN PEAKS ABOUT ' + Math.round(st.q50) + ' MPH';
else if(cb < 10) h = 'FEWER THAN 1 IN 10 RUNS GIVE JERSEY GUSTS OF 60+ MPH';
else if(cb >= 95) h = 'ALMOST EVERY RUN GIVES JERSEY GUSTS OF ' + base + '+ MPH';
else h = 'ABOUT ' + n10 + ' IN 10 RUNS GIVE JERSEY GUSTS OF ' + base + '+ MPH';
/* and if a still higher level has at least a 1 in 10 chance, say so too */
const hiT = [100, 90, 80, 70].find(t => t > base && st.ch[t] >= 10);
if(hiT && cb >= 10) h += ' &middot; ABOUT ' + Math.round(st.ch[hiT] / 10) + ' IN 10 GIVE ' + hiT + '+ MPH';
const t = tierOf(st.q50);
return { h:h, col:t ? t.hex : '#2de1a0' };
}

function sigOf(md){
const r = md.runs[0], out = {};
for(let i = 48; i < Math.min(md.time.length, 150); i += 6) if(r.g[i] != null) out[md.time[i]] = r.g[i];
return out;
}
function sigChanged(a, b){
if(!a || !b) return false;
let common = 0;
for(const k in a){ if(k in b){ common++; if(Math.abs(a[k] - b[k]) >= 0.1) return true; } }
return false;
}

return { ENS, DET, THRESH, SC, DAYF, nc, dayDate, med, hh, loadEnsemble, loadDet, dayStats, detPeaks, headText, sigOf };
})();

const plain = t => String(t).replace(/&mdash;/g, '\u2014').replace(/&middot;/g, '\u00b7').replace(/&ndash;/g, '\u2013').replace(/&amp;/g, '&');

async function scenarios(){
  const S = SCN, SC = S.SC;
  const res = await Promise.all(S.ENS.map(S.loadEnsemble).concat([S.loadDet()]));
  SC.det = res.pop();
  SC.ens = res.filter(Boolean);
  const missing = S.ENS.filter(m => !SC.ens.some(md => md.m.id === m.id)).map(m => m.lbl);
  if(missing.length) errors.push('scenarios: no data from ' + missing.join(', '));
  if(!SC.det) errors.push('scenarios: main model runs not loaded');
  if(!SC.ens.length) return null;
  const days = {};
  SC.ens.forEach(md => Object.keys(md.byDay).forEach(d => { days[d] = 1; }));
  SC.days = Object.keys(days).sort().slice(0, 7);
  SC.stats = {};
  SC.days.forEach(d => { SC.stats[d] = S.dayStats(d); });
  /* the stormiest day - the one the page opens on: highest chance of 60+, then highest middle run */
  let focus = null;
  SC.days.forEach(d => { const s = SC.stats[d]; if(s && (!focus || s.c60 > focus.c60 || (s.c60 === focus.c60 && s.q50 > focus.q50))) focus = s; });

  const rows = [], sumDays = [];
  SC.days.forEach(function(day){
    const st = SC.stats[day], label = S.nc(S.DAYF.format(S.dayDate(day))).toUpperCase();
    const row = { forecast_day: day, day_label: label };
    if(!st){ row.runs_total = 0; row.note = 'too few runs for this day'; rows.push(row); return; }
    const det = S.detPeaks(day);
    Object.assign(row, {
      runs_total: st.n, runs_per_model: Object.keys(st.models).map(m => m + ' ' + st.models[m]).join(' \u00b7 '),
      headline: plain(S.headText(st).h), q10_mph: r1(st.q10), q50_mph: r1(st.q50), q90_mph: r1(st.q90)
    });
    S.THRESH.forEach(t => { row['chance_' + t + '_pct'] = st.ch[t]; });
    Object.assign(row, { cut_b1: st.b1, cut_b2: st.b2, cut_on_levels: st.levelCut ? 'Y' : 'N' });
    st.sc.forEach(function(x){
      row[x.key + '_label'] = x.lbl; row[x.key + '_pct'] = x.p; row[x.key + '_runs'] = x.n;
      row[x.key + '_range'] = plain(x.range); row[x.key + '_typical_mph'] = x.n ? r1(x.v) : '';
      row[x.key + '_wind_mph'] = x.n ? r1(x.wind) : '';
      row[x.key + '_60_window'] = x.win ? S.hh(x.win[0]) + '-' + S.hh(x.win[1]) : '';
    });
    S.ENS.forEach(function(m){
      const pk = st.recs.filter(r => r.model.lbl === m.lbl).map(r => r.pk);
      row['median_peak_' + m.lbl.toLowerCase().replace(/[^a-z]+/g, '_')] = pk.length ? r1(S.med(pk)) : '';
    });
    S.DET.forEach(function(d){
      const hit = det.filter(x => x.lbl === d.lbl)[0];
      row['main_run_' + d.id] = hit ? r1(hit.pk) : '';
    });
    row.all_run_peaks = st.recs.map(r => r.pk.toFixed(1)).join(',');
    rows.push(row);
    sumDays.push({ day: day, label: label, n: st.n, nModels: Object.keys(st.models).length, head: plain(S.headText(st).h),
      q50: Math.round(st.q50), ch: { 60: st.ch[60], 70: st.ch[70], 80: st.ch[80], 90: st.ch[90], 100: st.ch[100] },
      sc: st.sc.map(x => ({ k: x.key, lbl: x.lbl, r: plain(x.range), p: x.p, n: x.n, v: Math.round(x.v),
                            win: x.win ? S.hh(x.win[0]) + '-' + S.hh(x.win[1]) : '' })),
      det: det.map(x => ({ lbl: x.lbl, pk: Math.round(x.pk) })) });
  });
  const sig = {};
  SC.ens.forEach(md => { sig[md.m.lbl] = S.sigOf(md); });
  return { rows: rows, watch: { sig: sig, focus: focus ? focus.day : '', days: sumDays } };
}

async function main(){
  const now = new Date();
  // each section fills its own object, then they are joined in a fixed order so the sheet's columns never shuffle
  const parts = [{}, {}, {}, {}, {}];
  let scen = null;
  await Promise.all([liveOps(parts[0]), rainfall(parts[1]), fiveModels(parts[2]), metar(parts[3]), warnings(parts[4]),
                     scenarios().then(r => { scen = r; }).catch(e => { errors.push('scenarios: ' + e.message); })]);
  const stamp = { time_utc: now.toISOString(), time_local: localTime(now), source: SOURCE };
  const ordered = Object.assign({}, stamp, { error: errors.join(' ; ') }, ...parts);
  const fcRows = scen ? scen.rows.map(r => Object.assign({}, stamp, r)) : [];
  const payload = { token: process.env.SNAPSHOT_TOKEN || '', source: SOURCE,
                    tabs: { 'Snapshots': [ordered], 'Forecast Snapshots': fcRows },
                    scenarioWatch: scen ? scen.watch : null,
                    scenarioTestEmail: (process.env.SCENARIO_TEST_EMAIL || '').toLowerCase() === 'true' };
  if(DRY){
    console.log('DRY RUN - nothing posted. Snapshots: ' + Object.keys(ordered).length + ' columns:');
    Object.entries(ordered).forEach(([k, v]) => console.log('  ' + k.padEnd(28) + ' ' + String(v).slice(0, 140)));
    console.log('Forecast Snapshots: ' + fcRows.length + ' rows (one per day), ' + (fcRows[0] ? Object.keys(fcRows[0]).length : 0) + ' columns. Day by day:');
    fcRows.forEach(r => console.log('  ' + r.day_label.padEnd(12) + ' ' + String(r.runs_total).padStart(3) + ' runs  ' + (r.headline || r.note || '') +
      (r.likely_range ? '  | best ' + r.best_range + ' ' + r.best_pct + '%, ' + r.likely_label.toLowerCase() + ' ' + r.likely_range + ' ' + r.likely_pct + '%, worst ' + r.worst_range + ' ' + r.worst_pct + '%' : '')));
    if(scen) console.log('Stormiest day (used for the email): ' + scen.watch.focus + ' \u00b7 models: ' + Object.keys(scen.watch.sig).join(', '));
    return;
  }
  if(!process.env.SNAPSHOT_URL || !process.env.SNAPSHOT_TOKEN){ console.error('ERROR: SNAPSHOT_URL / SNAPSHOT_TOKEN secrets missing.'); process.exit(1); }
  for(let i = 0; i < 3; i++){
    try{
      const r = await fetch(process.env.SNAPSHOT_URL, { method:'POST', body: JSON.stringify(payload), redirect:'follow' });
      const txt = await r.text();
      console.log('Receiver replied: ' + txt.slice(0, 300));
      if(/"status"\s*:\s*"(ok|skipped)"/.test(txt)) return;
      if(/forbidden/.test(txt)){ console.error('Token mismatch - check SNAPSHOT_TOKEN in GitHub and in the receiver script.'); process.exit(1); }
    }catch(e){ console.log('post failed: ' + e.message); }
    await sleep(3000 * Math.pow(2, i));
  }
  console.error('Could not post the snapshot after 3 tries.');
  process.exit(1);
}
main();
