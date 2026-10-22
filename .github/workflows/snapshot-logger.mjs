// Jersey Weather Chat - snapshot logger (Stage 1)
// ------------------------------------------------
// Every 30 minutes (GitHub Actions: .github/workflows/snapshot-logger.yml) this records what the site was
// showing, and posts it to the "JWC Snapshots" Google Sheet through its own small receiver script, so that
// after Storm Austen the forecasts can be compared with what actually happened.
//
// Stage 1 columns ("Snapshots" tab): Live Ops "now" conditions and storm indices, rain since midnight,
// the five-model gust forecast, the Jersey Airport report (METAR) and the Jersey Met warnings.
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

async function main(){
  const now = new Date();
  // each section fills its own object, then they are joined in a fixed order so the sheet's columns never shuffle
  const parts = [{}, {}, {}, {}, {}];
  await Promise.all([liveOps(parts[0]), rainfall(parts[1]), fiveModels(parts[2]), metar(parts[3]), warnings(parts[4])]);
  const ordered = Object.assign({ time_utc: now.toISOString(), time_local: localTime(now), source: SOURCE, error: errors.join(' ; ') }, ...parts);
  const payload = { token: process.env.SNAPSHOT_TOKEN || '', source: SOURCE, tabs: { 'Snapshots': [ordered] } };
  if(DRY){
    console.log('DRY RUN - nothing posted. ' + Object.keys(ordered).length + ' columns:');
    Object.entries(ordered).forEach(([k, v]) => console.log('  ' + k.padEnd(28) + ' ' + String(v).slice(0, 140)));
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
