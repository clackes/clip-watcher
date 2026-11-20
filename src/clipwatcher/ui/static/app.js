/* Companion for Clip Watcher: the page. It asks the local server (clipwatcher.ui) for what the bot has kept,
   starts runs through it and shows what a run reports while it works. Nothing here talks to the internet. */
(() => {
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const TOKEN = $('meta[name="companion-token"]').content;
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
const store = { get(k, d) { try { const v = localStorage.getItem('cw.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem('cw.' + k, JSON.stringify(v)); } catch (e) {} } };
const P = { twitch: 'M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z', kick: 'M1.333 0h8v5.333H12V2.667h2.667V0h8v8H20v2.667h-2.667v2.666H20V16h2.667v8h-8v-2.667H12v-2.666H9.333V24h-8Z', claude: 'm4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z', telegram: 'M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z', youtube: 'M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z',
  run: 'M7 4.5v15l13-7.5z', history: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 2a7 7 0 1 1 0 14 7 7 0 0 1 0-14zm-1 2v6l4.5 2.7 1-1.7-3.5-2.1V7z',
  clips: 'M4 5h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm0 2v10h16V7zm2 3h5v2H6zm7 0h5v2h-5zm-7 3h3v2H6zm5 0h7v2h-7z',
  setup: 'M4 6h8v2H4zm12 0h4v2h-4zm-3-2h2v6h-2zM4 11h3v2H4zm7 0h9v2h-9zM8 9h2v6H8zm-4 7h10v2H4zm14 0h2v2h-2zm-3-2h2v6h-2z',
  insights: 'M4 20V10h3.5v10zm6.25 0V4h3.5v16zm6.25 0v-7H20v7z',
  library: 'M3 5h18v14H3zm2 2v10h14V7zm2 6h4v2H7zm6 0h4v2h-4z', connect: 'M7 7h4v2H7a3 3 0 0 0 0 6h4v2H7A5 5 0 0 1 7 7zm6 0h4a5 5 0 0 1 0 10h-4v-2h4a3 3 0 0 0 0-6h-4zm-5 4h8v2H8z',
  check: 'M9.55 17.6 4.4 12.45l1.4-1.4 3.75 3.75 8.65-8.65 1.4 1.4z', spin: 'M12 3a9 9 0 1 0 9 9h-2.2A6.8 6.8 0 1 1 12 5.2z', skip: 'M5 5.5 13 12l-8 6.5zM14 5.5 22 12l-8 6.5z',
  dotc: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z', cross: 'm6.4 5 5.600 5.600L17.600 5 19 6.400 13.400 12 19 17.600 17.600 19 12 13.400 6.400 19 5 17.600 10.600 12 5 6.400z' };
const ic = (n, c = '') => `<svg class="ic ${c}" viewBox="0 0 24 24" aria-hidden="true"><path d="${P[n]}"/></svg>`;
const PFN = { twitch: 'Twitch', kick: 'Kick', youtube: 'YouTube', telegram: 'Telegram', claude: 'Claude' };
const pf = n => `<span class="pf pf-${n}" title="${PFN[n] || n}">${ic(n)}</span>`;
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const toast = (() => { let t; return m => { const el = $('#toast'); el.textContent = m; el.classList.add('on'); clearTimeout(t); t = setTimeout(() => el.classList.remove('on'), 3200); }; })();
function seg(el, items, val, on) { el.innerHTML = items.map(([v, l]) => `<button type="button" data-v="${v}" aria-pressed="${String(v) === String(val)}">${l}</button>`).join(''); el.onclick = e => { const b = e.target.closest('button'); if (!b) return; $$('button', el).forEach(x => x.setAttribute('aria-pressed', x === b)); on(b.dataset.v); }; }
const fmtS = s => s == null ? '' : s >= 3600 ? Math.floor(s / 3600) + ' h ' + Math.round(s % 3600 / 60) + ' min' : s >= 60 ? Math.floor(s / 60) + ' min ' + Math.round(s % 60) + ' s' : (s < 10 ? (+s).toFixed(2) : (+s).toFixed(1)) + ' s';
const money = v => '$' + (v >= 100 ? v.toFixed(0) : v >= 1 || !v ? (+v).toFixed(2) : v.toFixed(4));
const when = t => { const d = new Date(t * 1000); return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) + ' ' + d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }); };
const workOf = t => Object.entries(t || {}).reduce((a, [k, v]) => a + (k === 'wait' || k === 'total' || k === 'post' ? 0 : +v || 0), 0);
const plural = (n, one, many) => (+n).toLocaleString('en-US') + ' ' + (+n === 1 ? one : many || one + 's');
const numb = v => `<b class="num${String(v).length > 6 ? ' long' : ''}">${v}</b>`;
const clockOf = t => new Date(t * 1000).toLocaleTimeString('en-GB');
const api = {
  async get(path) { const r = await fetch(path); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; },
  async post(path, body) { const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Companion-Token': TOKEN }, body: JSON.stringify(body || {}) }); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; },
};
const S = { ov: null, live: null, clips: [], ins: null, cfg: null };  // what the server told us

/* ---------- who the streamers are: picture, badges, display name ----------
   Asked of the app's server (which asks Twitch and Kick once a day and keeps the pictures), a batch at a
   time, then painted into whatever on the page names that streamer. */
const PROF = {}; let profWant = new Set(), profTimer = 0;
const pkey = (platform, login) => (platform === 'kick' ? 'kick' : 'twitch') + ':' + String(login || '').toLowerCase();
const platformOf = login => { const s = S.ov && S.ov.streamers.find(x => x.login === String(login).toLowerCase()); return s ? s.platform : 'twitch'; };
function touch(k) {
  if (!PROF[k] && /^(twitch|kick):[a-z0-9_]{2,40}$/.test(k)) profWant.add(k);
  if (profTimer) return;
  profTimer = setTimeout(async () => { profTimer = 0; const ks = [...profWant]; profWant = new Set();
    if (ks.length) { try { (await api.get('/api/profiles?who=' + encodeURIComponent(ks.join(',')))).forEach(r => { PROF[pkey(r.platform, r.login)] = r; }); } catch (e) { ks.forEach(k2 => { PROF[k2] = PROF[k2] || { badges: [] }; }); } }
    paintProfiles(); }, 40);
}
function avatar(platform, login, cls = '') { const k = pkey(platform, login); touch(k); return `<span class="av ${cls}" data-av="${esc(k)}"><i>${esc(String(login || '?')[0].toUpperCase())}</i><span class="pfm pf-${platform === 'kick' ? 'kick' : 'twitch'}">${ic(platform === 'kick' ? 'kick' : 'twitch')}</span></span>`; }
function badges(platform, login, max = 2) { const k = pkey(platform, login); touch(k); return `<span class="bdg" data-bd="${esc(k)}" data-max="${max}"></span>`; }
function named(platform, login) { const k = pkey(platform, login); touch(k); return `<span data-nm="${esc(k)}">${esc(PROF[k] && PROF[k].known ? PROF[k].name : login)}</span>`; }
function paintProfiles() {
  $$('[data-av]').forEach(el => { const pr = PROF[el.dataset.av]; if (!pr || !pr.avatar || el.querySelector('img')) return;
    const img = new Image(); img.alt = ''; img.decoding = 'async'; img.onload = () => el.classList.add('has'); img.src = pr.avatar; el.insertBefore(img, el.querySelector('.pfm')); });
  $$('[data-bd]').forEach(el => { const pr = PROF[el.dataset.bd]; if (!pr || el.dataset.done) return; el.dataset.done = '1';
    el.innerHTML = (pr.badges || []).slice(0, +el.dataset.max || 2).map((b, i) => b.image ? `<img src="${esc(b.image)}" alt="${esc(b.title)}" title="${esc(b.title)}" style="--i:${i}">` : `<span class="vf vf-${pr.platform}" role="img" aria-label="${esc(b.title)}" title="${esc(b.title)}" style="--i:${i}">${ic('check')}</span>`).join(''); });
  $$('[data-nm]').forEach(el => { const pr = PROF[el.dataset.nm]; if (pr && pr.known && el.textContent !== pr.name) el.textContent = pr.name; });
  $$('[data-fo]').forEach(el => { const pr = PROF[el.dataset.fo]; if (pr && pr.followers != null) el.textContent = compact(pr.followers) + ' followers' + (pr.verified ? (pr.platform === 'kick' ? ' · verified on Kick' : ' · Twitch partner') : pr.affiliate ? ' · affiliate' : ''); });
}
const compact = n => n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'K' : String(n);

function tilt(el, max = 5) {
  if (reduce) return;
  el.addEventListener('pointermove', e => { const r = el.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height; el.style.setProperty('--ty', ((x - .5) * 2 * max).toFixed(2) + 'deg'); el.style.setProperty('--tx', ((.5 - y) * 2 * max).toFixed(2) + 'deg'); el.style.setProperty('--gx', (x * 100).toFixed(0) + '%'); el.style.setProperty('--gy', (y * 100).toFixed(0) + '%'); });
  el.addEventListener('pointerleave', () => { el.style.setProperty('--tx', '0deg'); el.style.setProperty('--ty', '0deg'); });
}
function phoneTilt(wrap, phone) {
  if (reduce) return;
  wrap.addEventListener('pointermove', e => { const r = wrap.getBoundingClientRect(); phone.classList.remove('float'); phone.style.setProperty('--ry', (((e.clientX - r.left) / r.width - .5) * 44).toFixed(1) + 'deg'); phone.style.setProperty('--rx', ((.5 - (e.clientY - r.top) / r.height) * 22).toFixed(1) + 'deg'); });
  wrap.addEventListener('pointerleave', () => { phone.style.removeProperty('--rx'); phone.style.removeProperty('--ry'); phone.classList.add('float'); });
}

/* ---------- the six places ---------- */
const VIEWS = [['run', 'Run', 'run'], ['clips', 'Clips', 'clips'], ['library', 'Library', 'library'], ['insights', 'Insights', 'insights'], ['connect', 'Connect', 'connect'], ['setup', 'Settings', 'setup']];
$('#nav').innerHTML = $('#navm').innerHTML = VIEWS.map(([k, l, i]) => `<a href="#${k}" data-v="${k}">${ic(i)}<span>${l}</span></a>`).join('');
let current = null;
function stagger(root) { $$('[data-a]', root).forEach((el, i) => el.style.setProperty('--i', Math.min(i, 10))); }
function show(name) {
  if (!VIEWS.some(v => v[0] === name)) name = 'run';
  if (name === current) return;
  const next = $('#v-' + name), prev = current && $('#v-' + current);
  $$('.tabs a').forEach(a => a.dataset.v === name ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current'));
  const enter = () => {
    $$('.view').forEach(v => { if (v !== next) { v.hidden = true; v.classList.remove('leave', 'play'); } });
    next.hidden = false; stagger(next); next.classList.remove('play'); void next.offsetWidth; next.classList.add('play');
    current = name; store.set('view', name); onShow(name);
  };
  if (prev && prev !== next && !reduce) { prev.classList.add('leave'); setTimeout(enter, 200); scrollTo({ top: 0, behavior: 'smooth' }); } else enter();
}
addEventListener('hashchange', () => show(location.hash.slice(1)));

/* ---------- the walk: Choose, Source, Captions, Run, Result ---------- */
const W = { step: 1, mode: store.get('mode', 'live'), streamer: store.get('streamer', ''), minutes: store.get('minutes', 15), platform: store.get('platform', 'twitch'), vodWho: '', vod: '', top: 3 };
const WIZ = ['Choose', 'Source', 'Captions', 'Run', 'Result'];
const paneOf = n => n >= 4 ? 's4' : 's' + n;
const skipped = n => (n === 2 || n === 3) && (W.step >= 4 ? R.kind === 'tests' || R.kind === 'render' : W.mode === 'tests');
function paintWiz() {
  $('#wiz').innerHTML = WIZ.map((l, i) => { const n = i + 1, st = skipped(n) ? 'skip' : n < W.step ? 'done' : n === W.step ? 'now' : 'todo', can = n < W.step && W.step <= 3 && !skipped(n);
    return `<li data-st="${st}"><button type="button" data-go="${n}" ${can ? '' : 'disabled'} ${n === W.step ? 'aria-current="step"' : ''}><i>${st === 'done' ? ic('check') : n}</i><span>${l}</span></button></li>`; }).join('');
}
function goStep(n) {
  const from = $('#' + paneOf(W.step)), to = $('#' + paneOf(n)); W.step = n; paintWiz();
  if (n === 3 && capW) capW.refresh();
  if (n === 2) paintSource();
  if (from === to) return;
  const enter = () => { from.hidden = true; from.classList.remove('out'); to.hidden = false; stagger(to); scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' }); };
  if (reduce) enter(); else { from.classList.add('out'); setTimeout(enter, 190); }
}
const MODES = [['live', 'Live lane', 'Watch a live stream and clip the moment as it happens.', 'run'], ['vod', 'VOD lane', 'Go through a finished broadcast and take its best moments.', 'history'], ['tests', 'Test suite', 'Check that every part of the bot still works.', 'check']];
function paintModes() {
  $('#modeCards').innerHTML = MODES.map(([k, t, d, icn]) => `<button type="button" class="choice" data-k="${k}" aria-pressed="${W.mode === k}"><span class="tick">${ic('check')}</span><span class="tile">${ic(icn)}</span><b>${t}</b><p>${d}</p></button>`).join('');
  $$('#modeCards .choice').forEach(el => tilt(el, 6));
}
const needsTool = () => W.mode === 'vod' && W.platform === 'twitch' && S.ov && S.ov.tools && !S.ov.tools.twitch_vod;
function liveOf(login) { return (S.live || []).find(r => r.login === login); }
function paintSource() {
  const live = W.mode === 'live'; $('#srcLive').hidden = !live; $('#srcVod').hidden = live;
  $('#s2title').innerHTML = live ? 'Which <span class="gt">stream</span>?' : 'Which <span class="gt">broadcast</span>?';
  $('#s2lead').textContent = live ? 'The bot joins, reads the chat from the start of the broadcast and clips what stands out.' : 'Paste the link of a finished broadcast. Its whole chat is read first, then the best moments are cut from the video.';
  const list = S.ov ? S.ov.streamers : [];
  if (live) {
    if (!list.some(s => s.login === W.streamer)) { $('#otherWho').value = W.streamer; }
    if (!list.length) { $('#whoCards').innerHTML = `<div class="empty card" style="grid-column:1 / -1"><span class="tile">${ic('run')}</span><b>No channels of your own yet</b><span>Type a Twitch channel below. To have yours listed here with their live status, copy <code>config/streamers.example.yaml</code> to <code>config/streamers.yaml</code> and put them in.</span></div>`; }
    else {
    const on = s => { const st = liveOf(s.login); return st && st.live ? 1 : 0; };
    $('#whoCards').innerHTML = [...list].sort((a, b) => on(b) - on(a)).map(s => { const st = liveOf(s.login), line = !S.live ? 'Checking...' : !st ? '' : st.error ? 'Could not be checked' : st.live ? `<i class="dot"></i>Live${st.category ? ' · ' + esc(st.category) : ''}` : 'Offline';
      return `<button type="button" class="choice sm${S.live && !on(s) ? ' off' : ''}" data-k="${s.login}" aria-pressed="${W.streamer === s.login}"><span class="tick">${ic('check')}</span><span class="byline">${avatar(s.platform, s.login, on(s) ? 'live' : '')}<span class="who"><b>${named(s.platform, s.login)}${badges(s.platform, s.login)}</b><small>${line}</small></span></span></button>`; }).join(''); }
    seg($('#minSeg'), [[5, '5 minutes'], [15, '15 minutes'], [60, '1 hour'], [0, 'Until I stop it']], W.minutes, v => { W.minutes = +v; store.set('minutes', +v); });
    if (!S.live && list.length) api.get('/api/live').then(rows => { S.live = rows; if (W.step === 2 && W.mode === 'live') paintSource(); }).catch(() => { S.live = []; });
  } else {
    seg($('#platSeg'), [['twitch', ic('twitch', 'pf-twitch') + ' Twitch'], ['kick', ic('kick', 'pf-kick') + ' Kick']], W.platform, v => { W.platform = v; store.set('platform', v); paintSource(); });
    $('#whoList').innerHTML = list.filter(s => s.platform === W.platform).map(s => `<option value="${esc(s.login)}">`).join('');
    $('#vodLink').placeholder = W.platform === 'kick' ? 'https://kick.com/channel/videos/...' : 'https://www.twitch.tv/videos/...';
    seg($('#topSeg'), [[1, '1'], [2, '2'], [3, '3'], [5, '5']], W.top, v => { W.top = +v; });
    $('#vodNeeds').hidden = !needsTool(); $('#vodNeeds').innerHTML = needsTool() ? '<i class="dot warn"></i><span>Twitch broadcasts are fetched with TwitchDownloaderCLI, which is not installed on this machine (put it in <code>bin/</code>, or name it in CLIPWATCHER_TDL). Kick broadcasts need nothing extra.</span>' : '';
  }
}
/* ---------- the caption library ----------
   Every style a Short can get: four built in, the rest animated templates. One is on the stage, playing
   on a stand-in Short; the shelf holds them all; "Fine-tune" opens the template's own settings and each
   change plays on the stage. The clips are made by the server, once each, then kept. */
const PREV = {}, prevQueue = []; let prevBusy = 0;
function previewOf(style, settings) {
  const tuned = settings && Object.keys(settings).length, key = style + (tuned ? JSON.stringify(settings) : '');
  if (!PREV[key]) PREV[key] = new Promise((res, rej) => { prevQueue[tuned ? 'unshift' : 'push']({ style, settings: tuned ? settings : null, res, rej, key }); pumpPrev(); });
  return PREV[key];
}
function pumpPrev() {
  while (prevBusy < 2 && prevQueue.length) {
    const q = prevQueue.shift(); prevBusy++;
    api.post('/api/captions/preview', { style: q.style, settings: q.settings }).then(q.res, err => { delete PREV[q.key]; q.rej(err); }).finally(() => { prevBusy--; pumpPrev(); });
  }
}
const sameV = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const capOf = id => (S.ov ? S.ov.captions : []).find(x => x.id === id);
const capName = id => { const c = capOf(id); return c ? c.name : id || 'none'; };
const decimals = step => step >= 1 ? 0 : step >= .1 ? 1 : 2;
function capLib(root, o) {
  const st = { style: o.style, colors: o.colors || 'auto', settings: { ...(o.settings || {}) }, filter: 'all', fields: null, tune: false, seq: 0, err: '' };
  const lib = () => (S.ov ? S.ov.captions : []), info = () => (S.ov && S.ov.caption_library) || {};
  const cur = () => capOf(st.style) || lib()[0] || { id: 'word', name: 'Word', about: '', kind: 'built-in' };
  const animated = () => cur().kind === 'animated', blocked = c => c.kind === 'animated' && !info().ready;
  root.className = 'caplib' + (o.compact ? ' compact' : '');
  root.innerHTML = `<div class="capstage">
      <div class="capphone"><video muted loop playsinline preload="auto" aria-label="How the captions move"></video><div class="capwait" hidden><i></i><span></span></div></div>
      <div class="capside"><div class="capinfo"><span class="eyebrow" data-r="meta"></span><h2 data-r="name"></h2><p data-r="about"></p>
        <div class="field" data-r="colbox"><span class="eyebrow">Colours</span><div class="seg" data-r="colors"></div><small class="cap-note" data-r="colnote"></small></div>
        <p class="capwarn" data-r="warn" hidden></p><div class="row" data-r="acts"></div></div>
        <div class="captune" data-r="tune" hidden></div>
        <div class="capbar"><div class="seg" data-r="filter"></div><span class="cap-note" data-r="count"></span></div>
        <div class="capshelf" data-r="shelf"></div></div></div>`;
  const R$ = n => $(`[data-r="${n}"]`, root), video = $('.capphone video', root), wait = $('.capwait', root);
  const state = () => ({ style: st.style, colors: st.colors, settings: animated() ? { ...st.settings } : {}, animated: animated(), tuned: Object.keys(st.settings).length > 0 });
  const changed = () => { paintActs(); if (o.onChange) o.onChange(state()); };

  /* the stage */
  function show(url) { if (video.dataset.url === url) return; video.dataset.url = url; video.src = url; if (!reduce) video.play().catch(() => {}); }
  async function play() {
    const mine = ++st.seq, c = cur(); st.err = '';
    if (blocked(c)) { wait.hidden = false; wait.className = 'capwait still'; wait.lastElementChild.textContent = 'Needs its one-time setup'; video.removeAttribute('src'); video.dataset.url = ''; video.load(); return paintWarn([]); }
    const quick = !Object.keys(st.settings).length && c.preview;
    if (quick) { show(c.preview); wait.hidden = true; }
    else { wait.hidden = false; wait.className = 'capwait'; wait.lastElementChild.textContent = video.dataset.url ? 'Rendering your change' : 'Rendering the preview'; }
    try { const r = await previewOf(c.id, animated() ? st.settings : null); if (mine !== st.seq) return; if (!Object.keys(st.settings).length) c.preview = r.url; show(r.url); wait.hidden = true; paintWarn(r.warnings || []); paintTile(c); paintActs(); }
    catch (err) { if (mine !== st.seq) return; wait.hidden = true; st.err = err.message; paintWarn([]); paintActs(); }
  }
  function paintWarn(list) { const el = R$('warn'), all = [...(st.err ? [st.err] : []), ...list]; el.hidden = !all.length; el.textContent = all.join(' '); }
  function paintStage() {
    const c = cur(), lb = info();
    R$('meta').textContent = c.kind === 'animated' ? `Animated · ${c.font}${c.words ? ' · ' + (c.words === 1 ? 'one word' : 'up to ' + c.words + ' words') + ' at a time' : ''}${c.tuned ? ' · with your settings' : ''}` : 'Built in · Anton';
    R$('name').textContent = c.name; R$('about').textContent = c.about;
    R$('colbox').hidden = c.kind !== 'animated' || blocked(c);
    seg(R$('colors'), [['auto', 'Picked for each video'], ['fixed', 'The template\'s own']], st.colors, v => { st.colors = v; paintCol(); changed(); }); paintCol();
    if (blocked(c)) { R$('about').innerHTML = `${esc(c.about)}<br><br><span class="cap-note">The animated templates are rendered with Node. Run this once on this machine, then reload the page:</span><br><code class="cmd">${esc(lb.setup || '')}</code><br><span class="cap-note">${esc(lb.missing || '')}</span>`; }
    paintActs();
  }
  function paintCol() { R$('colnote').textContent = st.colors === 'auto' ? 'For every phrase the video under it is looked at: a highlight that is not already in the picture, always readable (at least 4.5 to 1). The preview shows the template\'s own colours.' : 'The colours you see here, on every video.'; }
  function paintActs() {
    const c = cur(), s = state(), p = o.primary, off = blocked(c) || !!st.err || (p.disabled && p.disabled(s));
    R$('acts').innerHTML = `<button type="button" class="btn btn-primary" data-a="go"${off ? ' disabled' : ''}>${esc(p.label(s))}</button>` + (c.kind === 'animated' && !blocked(c) ? `<button type="button" class="btn btn-glass" data-a="tune" aria-expanded="${st.tune}">${st.tune ? 'Close the settings' : 'Fine-tune'}</button>` : '') + (o.note ? `<small class="cap-note">${esc(o.note(s))}</small>` : '');
  }
  R$('acts').onclick = async e => { const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.a === 'tune') return tune(!st.tune);
    b.disabled = true; try { await o.primary.run(state()); } catch (err) { toast(err.message); } paintActs(); };

  /* the shelf */
  function tileHTML(c) { return `<button type="button" class="captile${blocked(c) ? ' off' : ''}" data-id="${esc(c.id)}" aria-pressed="${c.id === st.style}" title="${esc(c.about)}"><span class="capband">${c.preview ? `<video muted loop playsinline preload="metadata" data-src="${esc(c.preview)}"></video>` : `<i>${esc(c.name)}</i>`}</span><b>${esc(c.name)}</b><small>${c.kind === 'animated' ? esc(c.font) : 'Built in'}</small></button>`; }
  function paintTile(c) { const el = $(`.captile[data-id="${CSS.escape(c.id)}"]`, root); if (!el || !c.preview || $('video', el)) return; $('.capband', el).innerHTML = `<video muted loop playsinline preload="metadata" data-src="${esc(c.preview)}"></video>`; watch(el); }
  const seen = new IntersectionObserver(es => es.forEach(en => { if (!en.isIntersecting) return; const v = $('video', en.target), c = capOf(en.target.dataset.id); seen.unobserve(en.target);
    if (v && !v.src) { v.src = v.dataset.src; v.addEventListener('loadedmetadata', () => { v.currentTime = 1.62; }, { once: true }); }
    else if (!v && c && !blocked(c)) previewOf(c.id).then(r => { c.preview = r.url; paintTile(c); }, () => {}); }), { rootMargin: '120px' });
  function watch(el) { seen.observe(el); }
  function paintShelf() {
    const all = lib(), rows = all.filter(c => st.filter === 'all' || c.kind === st.filter), n = k => all.filter(c => c.kind === k).length;
    seg(R$('filter'), [['all', `All ${all.length}`], ['animated', `Animated ${n('animated')}`], ['built-in', `Built in ${n('built-in')}`]], st.filter, v => { st.filter = v; paintShelf(); });
    R$('count').textContent = info().ready === false && n('animated') ? 'The animated ones need their one-time setup on this machine.' : '';
    R$('shelf').innerHTML = rows.map(tileHTML).join(''); $$('.captile', root).forEach(watch);
  }
  const hover = (e, on) => { const t = e.target.closest && e.target.closest('.captile'), v = t && $('video', t); if (!v || !v.src || reduce || t.contains(e.relatedTarget)) return; if (on) v.play().catch(() => {}); else { v.pause(); v.currentTime = 1.62; } };
  R$('shelf').addEventListener('pointerover', e => hover(e, true)); R$('shelf').addEventListener('pointerout', e => hover(e, false));
  R$('shelf').onclick = e => { const b = e.target.closest('.captile'); if (!b || b.dataset.id === st.style) return; pick(b.dataset.id); };
  function pick(id) { st.style = id; st.settings = {}; st.fields = null; $$('.captile', root).forEach(x => x.setAttribute('aria-pressed', x.dataset.id === id)); paintStage(); play(); if (st.tune) { if (animated() && !blocked(cur())) loadFields(); else tune(false); } changed(); }

  /* the template's own settings */
  const fieldOf = path => { for (const g of st.fields.groups) for (const f of g.fields) if (f.path === path) return f; };
  const valOf = f => f.path in st.settings ? st.settings[f.path] : f.value;
  let later = 0;
  function setVal(f, v) { if (sameV(v, f.value)) delete st.settings[f.path]; else st.settings[f.path] = v; const row = $(`[data-p="${CSS.escape(f.path)}"]`, root); if (row) row.classList.toggle('changed', !sameV(v, f.own)); clearTimeout(later); later = setTimeout(play, 450); paintTuneHead(); changed(); }
  const numRow = (id, label, f, v, help) => { const max = f.max != null ? f.max : f.soft_max, d = decimals(f.step || .01); return `<div class="cfg-row${sameV(v, f.own) ? '' : ' changed'}" data-p="${esc(id)}"><label>${esc(label)}</label><input type="range" min="${f.min != null ? f.min : 0}" max="${Math.max(max, v)}" step="${f.step || .01}" value="${v}" aria-label="${esc(label)}"><output>${(+v).toFixed(d)}</output>${help ? `<small>${esc(help)}</small>` : ''}</div>`; };
  function rowHTML(f) {
    const v = valOf(f), ch = sameV(v, f.own) ? '' : ' changed';
    if (f.kind === 'number') return numRow(f.path, f.label, f, v, f.help);
    if (f.kind === 'select') return `<div class="cfg-row${ch}" data-p="${esc(f.path)}"><label>${esc(f.label)}</label><select aria-label="${esc(f.label)}">${f.options.map(x => `<option value="${esc(x.id)}"${x.id === v ? ' selected' : ''}>${esc(x.name)}</option>`).join('')}</select><output></output>${f.help ? `<small>${esc(f.help)}</small>` : ''}</div>`;
    if (f.kind === 'color') return `<div class="cfg-row${ch}" data-p="${esc(f.path)}"><label>${esc(f.label)}</label><input type="color" value="${esc(String(v).toLowerCase())}" aria-label="${esc(f.label)}"><output>${esc(String(v).toUpperCase())}</output>${f.help ? `<small>${esc(f.help)}</small>` : ''}</div>`;
    if (f.kind === 'colors') return `<div class="cfg-row wide${ch}" data-p="${esc(f.path)}"><label>${esc(f.label)}</label><div class="swatches">${v.map((x, i) => `<input type="color" value="${esc(String(x).toLowerCase())}" data-i="${i}" aria-label="${esc(f.label)} ${i + 1}">`).join('')}${v.length < (f.max || 8) ? '<button type="button" class="chip" data-sw="add">Add</button>' : ''}${v.length > 1 ? '<button type="button" class="chip" data-sw="less">Remove</button>' : ''}</div>${f.help ? `<small>${esc(f.help)}</small>` : ''}</div>`;
    const a = v && f.choices.find(x => x.id === v.name), units = a && a.units.length > 1 ? a.units : [];
    return `<div class="motion${ch}" data-p="${esc(f.path)}"><div class="cfg-row"><label>${esc(f.label)}</label><select data-m="name" aria-label="${esc(f.label)}">${f.optional ? `<option value=""${v ? '' : ' selected'}>Holds still</option>` : ''}${f.choices.map(x => `<option value="${esc(x.id)}"${v && x.id === v.name ? ' selected' : ''}>${esc(x.id[0].toUpperCase() + x.id.slice(1))}</option>`).join('')}</select><output></output><small>${esc(a ? a.about : 'Nothing moves while it waits.')}</small></div>
      ${units.length ? `<div class="cfg-row"><label>Takes its turn</label><select data-m="by" aria-label="${esc(f.label)}: takes its turn">${units.map(u => `<option value="${u}"${u === (v.by || 'page') ? ' selected' : ''}>${esc(st.fields.units[u] || u)}</option>`).join('')}</select><output></output></div>` : ''}
      ${a ? Object.entries(a.params).map(([k, p]) => { const pv = v.params && v.params[k] != null ? v.params[k] : p.default, span = p.max - p.min; return numRow(f.path + '#' + k, p.label || k, { ...p, step: span > 100 ? 1 : span > 30 ? .5 : span > 6 ? .1 : .01, own: p.default }, pv, ''); }).join('') : ''}</div>`;
  }
  function paintTuneHead() { const n = Object.keys(st.settings).length, h = $('.tunehead small', root); if (h) h.textContent = n ? `${n} ${n === 1 ? 'change' : 'changes'}, playing on the preview` : 'Move a setting and watch the preview.'; }
  function paintTune() {
    const box = R$('tune'), f = st.fields; if (!f) { box.innerHTML = '<p class="cap-note">Loading the settings…</p>'; return; }
    box.innerHTML = `<div class="tunehead"><div><b>Settings of ${esc(f.name)}</b><small class="cap-note"></small></div><div class="row"><button type="button" class="btn btn-glass btn-sm" data-t="undo">Undo my changes</button><button type="button" class="btn btn-glass btn-sm" data-t="own">As the template came</button></div></div>
      <div class="accs">${f.groups.map((g, i) => `<details class="acc slim"${i === 0 ? ' open' : ''}><summary><span><b>${esc(g.title)}</b><small>${g.fields.length} ${g.fields.length === 1 ? 'setting' : 'settings'}</small></span></summary><div class="cfg">${g.fields.map(rowHTML).join('')}</div></details>`).join('')}</div>`;
    paintTuneHead();
    $$('.acc', box).forEach(d => d.addEventListener('toggle', () => { if (d.open) $$('.acc', box).forEach(x => { if (x !== d) x.open = false; }); }));
  }
  function redraw(f) { const el = $(`[data-p="${CSS.escape(f.path)}"]`, root); if (el) el.outerHTML = rowHTML(f); }
  R$('tune').addEventListener('input', e => {
    const t = e.target, row = t.closest('[data-p]'); if (!row) return; const path = row.dataset.p, [fp, sub] = path.split('#'), f = fieldOf(fp); if (!f) return;
    if (sub) { const v = JSON.parse(JSON.stringify(valOf(f))); v.params = { ...(v.params || {}), [sub]: +t.value }; row.querySelector('output').textContent = (+t.value).toFixed(decimals(+t.step)); row.classList.add('changed'); return setVal(f, v); }
    if (f.kind === 'number') { row.querySelector('output').textContent = (+t.value).toFixed(decimals(f.step)); return setVal(f, f.whole ? Math.round(+t.value) : +t.value); }
    if (f.kind === 'color') { row.querySelector('output').textContent = t.value.toUpperCase(); return setVal(f, t.value.toUpperCase()); }
    if (f.kind === 'colors' && t.dataset.i != null) { const v = [...valOf(f)]; v[+t.dataset.i] = t.value.toUpperCase(); return setVal(f, v); }
  });
  R$('tune').addEventListener('change', e => {
    const t = e.target, row = t.closest('[data-p]'); if (!row || t.tagName !== 'SELECT') return; const f = fieldOf(row.dataset.p.split('#')[0]); if (!f) return;
    if (f.kind === 'select') return setVal(f, t.value);
    if (f.kind !== 'motion') return;
    const old = valOf(f); let v = null;
    if (t.dataset.m === 'by') v = { ...old, by: t.value };
    else if (t.value) { const a = f.choices.find(x => x.id === t.value), by = a.units.length ? (a.units.includes(old && old.by) ? old.by : a.units[0]) : null; v = { name: a.id, params: Object.fromEntries(Object.entries(a.params).map(([k, p]) => [k, p.default])) }; if (by) v.by = by; }
    setVal(f, v); redraw(f);
  });
  R$('tune').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.sw) { const row = b.closest('[data-p]'), f = fieldOf(row.dataset.p), v = [...valOf(f)]; if (b.dataset.sw === 'add') v.push(v[v.length - 1]); else v.pop(); setVal(f, v); return redraw(f); }
    if (b.dataset.t === 'undo') { st.settings = {}; paintTune(); play(); return changed(); }
    if (b.dataset.t === 'own') { st.settings = {}; st.fields.groups.forEach(g => g.fields.forEach(f => { if (!sameV(f.value, f.own)) st.settings[f.path] = f.own; })); paintTune(); play(); changed(); }
  });
  async function loadFields() { paintTune(); const id = st.style; try { const f = await api.get('/api/captions/fields?style=' + encodeURIComponent(id)); if (id !== st.style) return; st.fields = f; paintTune(); } catch (err) { R$('tune').innerHTML = `<p class="capwarn">${esc(err.message)}</p>`; } }
  function tune(on) { st.tune = on; R$('tune').hidden = !on; root.classList.toggle('tuning', on); paintActs(); if (on) { if (!st.fields) loadFields(); R$('tune').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' }); } }

  paintStage(); paintShelf(); play();
  return {
    state,
    /* the template's settings as its own version would hold them: everything that is not as it came */
    ownVersion() { const out = {}; if (st.fields) st.fields.groups.forEach(g => g.fields.forEach(f => { const v = valOf(f); if (!sameV(v, f.own)) out[f.path] = v; })); return out; },
    loaded: () => !!st.fields,
    saved(fields) { st.fields = fields || null; st.settings = {}; const c = cur(); c.tuned = !!(fields && fields.tuned); c.preview = null; delete PREV[c.id]; paintStage(); if (st.tune) paintTune(); play(); },
    paint() { paintStage(); },
    refresh() { paintStage(); paintShelf(); if (!video.dataset.url) play(); },
  };
}
let capW = null;
const capSaved = s => S.ov && s.style === S.ov.caption_default && (!s.animated || (s.colors === S.ov.caption_library.colors && !s.tuned));
async function useCaptions(quiet) {
  if (!capW) return; const s = capW.state(); if (capSaved(s)) return;
  if (s.animated && s.tuned && capW.loaded()) { const r = await api.post('/api/captions/defaults', { style: s.style, settings: capW.ownVersion() }); capW.saved(r); }
  await api.post('/api/settings', { values: { 'render.captions': s.style, ...(s.animated ? { 'render.rm.colors': s.colors } : {}) }, merge: true });
  S.ov.caption_default = s.style; if (s.animated) S.ov.caption_library.colors = s.colors; S.cfg = null; capW.paint(); if (!quiet) toast(capName(s.style) + ' is the caption style of new clips');
}
function paintTpls() {
  const ov = S.ov; if (!ov) return;
  if (capW) capW.refresh();
  else capW = capLib($('#capLib'), { style: capOf(ov.caption_default) ? ov.caption_default : 'word', colors: ov.caption_library.colors,
    primary: { label: s => capSaved(s) ? 'In use for new clips' : s.tuned ? 'Save and use for new clips' : 'Use for new clips', disabled: capSaved, run: () => useCaptions() },
    note: s => s.animated ? 'An animated template adds about half a clip\'s length to its render. Too slow for a live run on a busy machine: there the built-in ones are the safe pick.' : '' });
  $('#capLead').textContent = 'What the renderer puts on every new Short. ' + (ov.speech ? 'Pick one, watch it, make it yours.' : 'This machine has no speech model installed (faster-whisper), so new clips come out without captions until it is.');
}

/* ---------- a run, as the server reports it ---------- */
const R = { id: null, kind: null, es: null, params: {}, started: 0, clips: [], clip: 0, tick: 0, tests: { seen: 0, failed: [] }, ended: false, replay: false, timers: [] };
const STEP_PHONE = { cut: 'cut', loudness: 'cut', transcribe: 'cut', judge: 'cut', screen: 'cut', layout: 'split', render_short: 'render', render_clean: 'render', stage: 'render' };
function stepName(k) { return (S.ov && S.ov.steps[k]) || k; }
function row(key, name, state, detail, seconds) {
  let li = $(`#steps [data-key="${key}"]`);
  if (!li) { li = document.createElement('li'); li.className = 'step'; li.dataset.key = key; li.innerHTML = '<span class="bub"></span><span class="nm"></span><span class="tm num"></span><span class="dt"></span><span class="bar"><i></i></span>'; $('#steps').appendChild(li); const box = $('#steps'); box.scrollTo({ top: box.scrollHeight, behavior: reduce ? 'auto' : 'smooth' }); }
  li.dataset.s = state; li.querySelector('.nm').textContent = name;
  li.querySelector('.bub').innerHTML = ic(state === 'run' ? 'spin' : state === 'done' ? 'check' : state === 'skip' ? 'skip' : state === 'failed' ? 'cross' : 'dotc');
  if (detail != null) li.querySelector('.dt').innerHTML = detail;
  if (seconds != null) li.querySelector('.tm').textContent = fmtS(seconds);
  return li;
}
function logLine(text, cls, t) { const el = $('#log'), d = document.createElement('div'); d.innerHTML = `<span class="t">${clockOf(t || Date.now() / 1000)}</span><span class="${cls || ''}">${esc(text)}</span>`; el.appendChild(d); if (el.childElementCount > 1500) el.firstChild.remove(); el.scrollTop = el.scrollHeight; }
function pill(o, cls) { const el = $(`.opill[data-o="${o}"]`); if (el) { el.classList.remove('on', 'skip'); if (cls) el.classList.add(cls); } }
function resetRunView(kind, params) {
  R.timers.forEach(clearTimeout); clearInterval(R.tick); if (R.es) { R.es.close(); R.es = null; }
  Object.assign(R, { kind, params, clips: [], clip: 0, tests: { seen: 0, failed: [] }, ended: false, timers: [] });
  const tests = kind === 'tests', name = kind === 'live' ? 'live lane' : kind === 'vod' ? 'VOD lane' : 'test suite';
  $('#runTitle').innerHTML = kind === 'render' ? 'Rendering the Short <span class="gt">again</span>' : `Running the <span class="gt">${name}</span>`;
  $('#runNote').textContent = kind === 'live' ? `${params.streamer}, ${+params.minutes ? 'for ' + params.minutes + ' minutes' : 'until you stop it'}.` : kind === 'vod' ? `${params.streamer} on ${PFN[params.platform] || 'Twitch'}: up to ${plural(params.top || 3, 'clip')}.` : kind === 'render' ? `${params.clip}: ${params.captions ? capName(params.captions) : 'the same'} captions${params.settings && Object.keys(params.settings).length ? ' with settings of their own' : ''}, ${params.layout === 'stack' ? 'the older stack' : 'webcam over gameplay'}.` : 'Every test of the project, as pytest runs them.';
  const rp = kind === 'vod' ? params.platform : platformOf(params.streamer), rw = $('#runWho'); rw.hidden = !(kind === 'live' || kind === 'vod');
  rw.innerHTML = rw.hidden ? '' : `${avatar(rp, params.streamer, 'lg')}<span class="who"><b>${named(rp, params.streamer)}${badges(rp, params.streamer, 4)}</b><small data-fo="${esc(pkey(rp, params.streamer))}">${rp === 'kick' ? 'kick.com/' : 'twitch.tv/'}${esc(params.streamer)}</small></span>`;
  $('#steps').innerHTML = ''; $('#log').innerHTML = ''; $('#stepsBox').hidden = false; $('#resultBox').hidden = true; $('#resultBox').innerHTML = '';
  $('#runCtl').hidden = false; $('#stopBtn').disabled = false; $('#stopBtn').textContent = 'Stop'; $('#forceBtn').hidden = true; $('#forceBtn').disabled = false;
  $('#stagePhone').hidden = tests; $('#stageDots').hidden = !tests; $('#dots').innerHTML = ''; $('#dotsNote').textContent = 'Collecting the tests';
  const ph = $('#phone'); ph.dataset.st = 'wait'; const v = $('#phoneVid'); v.hidden = true; v.removeAttribute('src'); try { v.load(); } catch (e) {}
  $$('.opill').forEach(o => o.classList.remove('on', 'skip'));
  $('#credit').textContent = kind === 'tests' || kind === 'render' ? '' : (params.platform === 'kick' || (S.ov && (S.ov.streamers.find(s => s.login === params.streamer) || {}).platform === 'kick') ? 'kick.com/' : 'twitch.tv/') + (params.streamer || '');
  $('#total').classList.toggle('ind', kind === 'vod' || kind === 'render' || (kind === 'live' && !+params.minutes)); $('#totalBar').style.width = kind === 'tests' || (kind === 'live' && +params.minutes) ? '0' : '';
  $('#runState').textContent = 'Starting'; $('#runClock').textContent = '0 s';
}
function onEvent(e) {
  const k = e.kind;
  if (k === 'log') return logLine(e.line, /error|traceback|failed/i.test(e.line) ? 'w' : '', e.t);
  if (k === 'start') { R.started = e.t; return; }
  if (k === 'recording') { row('join', 'Join the stream', 'done', 'Recording into the rolling window'); pill(e.platform === 'kick' ? 'kick' : 'twitch', 'on'); $('#forceBtn').hidden = R.replay; $('#runState').textContent = 'Recording'; return; }
  if (k === 'baseline') { row('baseline', 'Read the chat so far', e.more ? 'run' : 'done', `<b>${(+e.messages).toLocaleString('en-US')}</b> messages, ${e.hours} h of the broadcast${e.category ? ' · ' + esc(e.category) : ''}${e.more ? '. The rest is being read in the background.' : ''}`); const b = $('#steps [data-key="baseline"]'), w = $('#steps [data-key="watch"]'); if (b && w && (w.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)) $('#steps').insertBefore(b, w); return; }
  if (k === 'watching') { row('watch', 'Watch for a spike', 'run', `<b>${plural(e.messages, 'message')}</b> read${e.per_minute ? ', ' + e.per_minute + ' a minute' : ''}`); $('#runState').textContent = 'Watching the chat'; return; }
  if (k === 'step' && e.step === 'chat') { row('chat', 'Read the chat of the broadcast', e.state === 'start' ? 'run' : e.state === 'done' ? 'done' : 'failed', e.state === 'failed' ? esc(e.error) : null, e.seconds); $('#runState').textContent = 'Reading the chat'; pill(R.params.platform === 'kick' ? 'kick' : 'twitch', 'on'); return; }
  if (k === 'chat') { row('chat', 'Read the chat of the broadcast', 'done', `<b>${(+e.messages).toLocaleString('en-US')}</b> messages`); return; }
  if (k === 'candidates') { row('found', 'Find the moments', 'done', `<b>${e.count}</b> ${e.count === 1 ? 'moment stands' : 'moments stand'} out${e.category ? ' · ' + esc(e.category) : ''}`); return; }
  if (k === 'moment') { R.clip++; const w = $(`#steps [data-key="watch"]`); if (w) w.dataset.s = 'done'; row('m' + R.clip, `Moment ${R.clip}`, 'done', `Chat score <b>${e.chat_score}</b>${e.chatters ? ', ' + e.chatters + ' chatters' : ''}, ${e.seconds} s of footage`); $('#phone').dataset.st = 'cut'; const v = $('#phoneVid'); v.hidden = true; $('#runState').textContent = 'Making a clip'; return; }
  if (k === 'step') { const key = e.step + ':' + R.clip, note = e.state === 'failed' ? esc(e.error) : e.step === 'transcribe' && e.state === 'done' && S.ov && !S.ov.speech ? 'No speech model on this machine, so no captions' : e.step === 'layout' && e.state === 'done' && S.ov && S.ov.tools && !S.ov.tools.face_detector ? 'No face detector on this machine: the layout set for the streamer is used' : null; row(key, stepName(e.step), e.state === 'start' ? 'run' : e.state === 'done' ? 'done' : 'failed', note, e.seconds); if (e.state === 'start' && STEP_PHONE[e.step]) $('#phone').dataset.st = STEP_PHONE[e.step]; if (e.state === 'start') $('#runState').textContent = stepName(e.step); return; }
  if (k === 'judged') { const ai = e.source === 'claude'; row('judge:' + R.clip, 'Judge', ai ? 'done' : 'skip', ai ? `Claude scored it <b>${e.score}</b> of 10` : 'No API key: the fallback wrote the title'); pill('cl', ai ? 'on' : 'skip'); return; }
  if (k === 'staged') { R.clips.push(e); row('staged:' + R.clip, e.again ? 'Rendered again' : 'Clip ready', 'done', `<b>${esc(e.title)}</b>`); const v = $('#phoneVid'); v.src = `/media/${encodeURIComponent(e.clip_id)}/short_9x16.mp4`; v.hidden = false; v.play().catch(() => {}); $('#phone').dataset.st = 'done'; pill('tg', S.ov && S.ov.telegram ? 'on' : 'skip'); pill('yt', 'skip'); if (!R.replay) { toast('Clip staged: ' + e.title); S.clips = []; } return; }
  if (k === 'rejected' || k === 'failed') { row(k + ':' + R.clip + ':' + e.seq, k === 'rejected' ? 'Not staged' : 'Could not be made', 'failed', esc(e.reason || '')); return; }
  if (k === 'forced') { if (!R.replay) toast('Clipping the last ' + e.seconds + ' seconds'); return; }
  if (k === 'test') { R.tests.seen++; const d = document.createElement('i'); d.className = e.outcome === 'PASSED' ? 'ok' : e.outcome === 'SKIPPED' ? '' : 'now'; d.title = `${e.file}::${e.name} ${e.outcome}`; $('#dots').appendChild(d); if (e.outcome === 'FAILED' || e.outcome === 'ERROR') R.tests.failed.push(e.file + '::' + e.name); $('#dotsNote').textContent = `${R.tests.seen} tests run, ${R.tests.failed.length} failed`; $('#totalBar').style.width = e.percent + '%'; $('#runState').textContent = e.file; row('t:' + e.file, e.file.replace('tests/', ''), 'run', null); const prev = $('#steps').children; if (prev.length > 1) { const p = prev[prev.length - 2]; if (p.dataset.s === 'run') { p.dataset.s = R.tests.failed.some(f => f.startsWith('tests/' + p.querySelector('.nm').textContent)) ? 'failed' : 'done'; p.querySelector('.bub').innerHTML = ic(p.dataset.s === 'done' ? 'check' : 'cross'); } } return; }
  if (k === 'stopping') { $('#runState').textContent = 'Stopping: finishing what is in progress'; $('#stopBtn').disabled = true; return; }
  if (k === 'end') return finish(e);
}
function finish(e) {
  if (!R.replay) busy(false);
  R.ended = true; clearInterval(R.tick); if (R.es) { R.es.close(); R.es = null; }
  $('#runCtl').hidden = true; $('#total').classList.remove('ind'); $('#totalBar').style.width = '100%'; $('#runState').textContent = e.state === 'done' ? 'Finished' : e.state === 'stopped' ? 'Stopped' : 'Ended with an error'; $('#runClock').textContent = fmtS(e.seconds);
  $$('#steps .step[data-s="run"]').forEach(li => { li.dataset.s = e.state === 'failed' ? 'failed' : 'done'; li.querySelector('.bub').innerHTML = ic(e.state === 'failed' ? 'cross' : 'check'); });
  W.step = 5; paintWiz();
  let html;
  if (R.kind === 'tests') {
    const t = e.tests || {}, bad = (t.failed || 0) + (t.error || 0), ok = e.state === 'done' && !bad;
    $('#runTitle').innerHTML = ok ? `All ${t.passed || R.tests.seen} tests <span class="gt">passed</span>` : `The tests <span class="gt">did not pass</span>`;
    html = `<span class="badge ${ok ? 'live' : 'warn'}"><i class="dot ${ok ? '' : 'warn'}"></i>${ok ? 'All green' : e.state === 'stopped' ? 'Stopped before the end' : bad + ' failed'}</span><h2>${ok ? 'Every part of the bot still works' : 'Something needs a look'}</h2>
      <div class="grid statrow"><div class="card"><div class="tile"><b class="num">${t.passed || 0}</b><span>passed</span></div></div><div class="card"><div class="tile"><b class="num">${bad}</b><span>failed</span></div></div><div class="card"><div class="tile">${numb(fmtS(t.seconds || e.seconds))}<span>in total</span></div></div></div>
      ${R.tests.failed.length ? `<div class="tbl-wrap"><table><tbody>${R.tests.failed.slice(0, 30).map(f => `<tr><td>${esc(f)}</td></tr>`).join('')}</tbody></table></div>` : ''}`;
  } else if (R.clips.length) {
    const c = R.clips[R.clips.length - 1], n = R.clips.length;
    $('#runTitle').innerHTML = R.kind === 'render' ? 'Rendered <span class="gt">again</span>' : n === 1 ? 'Clip <span class="gt">staged</span>' : `${n} clips <span class="gt">staged</span>`;
    html = `<span class="badge warn"><i class="dot warn"></i>Waiting for approval</span><h2>${esc(c.title)}</h2><p class="lead">${esc(c.hook || '')}</p>
      <div class="grid statrow"><div class="card"><div class="tile"><b class="num">${c.final_score}</b><span>final score</span></div></div><div class="card"><div class="tile"><b class="num">${c.seconds} s</b><span>long</span></div></div><div class="card"><div class="tile">${R.kind === 'render' ? `<b class="num long" style="text-transform:capitalize">${esc(c.captions ? capName(c.captions) : 'none')}</b>` : numb(fmtS(workOf(c.timings)))}<span>${R.kind === 'render' ? 'captions' : 'of work'}</span></div></div></div>
      <p class="cap-note">${S.ov && S.ov.dry_run ? 'Nothing was uploaded: this is a dry run, and only clips a person approves go to YouTube.' : 'It is uploaded once a person approves it.'}</p>`;
  } else if (R.kind === 'render') {
    $('#runTitle').innerHTML = 'Not rendered <span class="gt">again</span>';
    html = `<span class="badge"><i class="dot off"></i>${e.state === 'stopped' ? 'Stopped' : 'Not rendered'}</span><h2>${e.state === 'stopped' ? 'Stopped before it finished' : 'The render did not finish'}</h2><p class="lead">The clip keeps the video it had.${e.state === 'stopped' ? '' : ' The log below says why.'}</p>`;
  } else {
    $('#runTitle').innerHTML = e.state === 'failed' ? 'The run <span class="gt">stopped early</span>' : 'No clip <span class="gt">this time</span>';
    html = `<span class="badge"><i class="dot off"></i>${e.state === 'failed' ? 'Exit code ' + e.code : 'Nothing staged'}</span><h2>${e.state === 'failed' ? 'It ended with an error' : 'No moment stood out'}</h2><p class="lead">${e.state === 'failed' ? 'The last lines of the log say why. Open "Show the log" below.' : R.kind === 'live' ? 'Chat stayed under the bar while the bot was watching. Try a longer run, or a busier stream.' : 'Nothing in this broadcast passed the detector and the judge.'}</p>`;
  }
  html += `<div class="row" style="margin-top:4px"><button class="btn btn-primary btn-lg" id="again">Run something else</button>${R.clips.length ? '<button class="btn btn-glass btn-lg" id="openIt">Open the clip</button>' : ''}<button class="btn btn-ghost btn-lg" id="watchIt">Watch it again</button></div>`;
  const box = $('#resultBox'); box.innerHTML = `<div class="result">${html}</div>`; $('#stepsBox').hidden = true; box.hidden = false;
  if (e.state === 'failed' || (R.kind === 'render' && !R.clips.length && e.state !== 'stopped')) $('.logd').open = true;
  $('#again').onclick = () => { W.step = 4; goStep(1); };
  $('#watchIt').onclick = () => replay(R.id);
  if ($('#openIt')) $('#openIt').onclick = () => { const id = R.clips[R.clips.length - 1].clip_id; location.hash = 'clips'; setTimeout(() => openClip(id), 500); };
  if (!R.replay) { S.ins = null; S.clips = []; }
}
const busy = on => $$('.tabs a[data-v="run"]').forEach(a => a.classList.toggle('busy', on));
function follow(run) {
  busy(true); R.replay = false; R.id = run.id; resetRunView(run.kind, run.params || {}); R.started = run.started || Date.now() / 1000; paintWiz();
  clearInterval(R.tick); R.tick = setInterval(() => { if (R.ended || R.replay) return; const el = Date.now() / 1000 - R.started; $('#runClock').textContent = fmtS(Math.max(0, el)); if (R.kind === 'live' && +R.params.minutes) $('#totalBar').style.width = Math.min(100, 100 * el / (R.params.minutes * 60)) + '%'; }, 1000);
  if (R.es) R.es.close();
  R.es = new EventSource(`/api/runs/${encodeURIComponent(run.id)}/events?after=0`);
  R.es.onmessage = m => { try { onEvent(JSON.parse(m.data)); } catch (err) { console.error(err); } };
  R.es.onerror = () => { if (R.ended && R.es) R.es.close(); };
}
async function replay(id) {
  let run; try { run = await api.get('/api/runs/' + encodeURIComponent(id)); } catch (err) { return toast(err.message); }
  const evs = run.events || []; if (!evs.length) return toast('This run was not kept event by event. Runs from now on are.');
  if (run.state === 'running') return follow(run);
  if (location.hash !== '#run') location.hash = 'run';
  R.replay = true; R.id = run.id; resetRunView(run.kind, run.params || {});
  W.step = 4; paintWiz(); $$('.stepbox').forEach(b => { b.hidden = b.id !== 's4'; }); $('#runCtl').hidden = true; scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
  const t0 = evs[0].t, span = Math.max(1, evs[evs.length - 1].t - t0), play = reduce ? 0 : Math.min(14000, Math.max(5000, evs.length * 120)), speed = Math.max(1, Math.round(span * 1000 / (play || 1)));
  $('#runNote').textContent = `A recording of ${when(run.started)}` + (play ? `, played ${speed} times faster.` : '.'); $('#total').classList.remove('ind');
  evs.forEach(e => { R.timers.push(setTimeout(() => { if (!R.replay || R.id !== run.id) return; $('#runClock').textContent = fmtS(e.t - t0); if (e.kind !== 'end') $('#totalBar').style.width = Math.min(100, 100 * (e.t - t0) / span) + '%'; try { onEvent(e); } catch (err) { console.error(err); } }, play * (e.t - t0) / span)); });
}
async function startRun() {
  const params = W.mode === 'live' ? { streamer: W.streamer, minutes: W.minutes } : W.mode === 'vod' ? { streamer: W.vodWho, platform: W.platform, vod: W.vod, top: W.top } : {};
  try { if (W.mode !== 'tests') await useCaptions(true); const run = await api.post('/api/runs', { kind: W.mode, params }); goStep(4); follow(run); }
  catch (err) { toast(err.message); }
}
function initRun() {
  paintModes(); paintWiz();
  $('#modeCards').onclick = e => { const b = e.target.closest('.choice'); if (!b) return; W.mode = b.dataset.k; store.set('mode', W.mode); $$('#modeCards .choice').forEach(x => x.setAttribute('aria-pressed', x === b)); paintWiz(); };
  $('#whoCards').onclick = e => { const b = e.target.closest('.choice'); if (!b) return; W.streamer = b.dataset.k; store.set('streamer', W.streamer); $('#otherWho').value = ''; $$('#whoCards .choice').forEach(x => x.setAttribute('aria-pressed', x === b)); };
  $('#otherWho').oninput = e => { const v = e.target.value.trim().toLowerCase().replace(/^.*twitch\.tv\//, '').replace(/[^a-z0-9_]/g, ''); W.streamer = v; store.set('streamer', v); $$('#whoCards .choice').forEach(x => x.setAttribute('aria-pressed', x.dataset.k === v)); };
  $('#next1').onclick = () => W.mode === 'tests' ? startRun() : goStep(2);
  $('#back2').onclick = () => goStep(1);
  $('#next2').onclick = () => {
    if (W.mode === 'live' && !W.streamer) return toast('Pick a stream, or type a channel');
    if (needsTool()) return toast('Twitch broadcasts need TwitchDownloaderCLI on this machine');
    if (W.mode === 'vod') { W.vodWho = $('#vodWho').value.trim(); W.vod = $('#vodLink').value.trim(); if (!W.vodWho) return toast('Whose broadcast is it?'); if (!W.vod) return toast('Paste the link of the VOD'); }
    goStep(3);
  };
  $('#back3').onclick = () => goStep(2); $('#next3').onclick = startRun;
  $('#wiz').onclick = e => { const b = e.target.closest('button:not([disabled])'); if (b) goStep(+b.dataset.go); };
  $('#stopBtn').onclick = async () => { try { await api.post(`/api/runs/${encodeURIComponent(R.id)}/stop`); } catch (err) { toast(err.message); } };
  $('#forceBtn').onclick = async () => { $('#forceBtn').disabled = true; setTimeout(() => { $('#forceBtn').disabled = false; }, 60000); try { await api.post(`/api/runs/${encodeURIComponent(R.id)}/force`, { seconds: 45 }); } catch (err) { toast(err.message); $('#forceBtn').disabled = false; } };
  $$('.opill').forEach(o => { const map = { twitch: ['twitch', 'Twitch'], kick: ['kick', 'Kick'], cl: ['claude', 'Judge'], tg: ['telegram', 'Approval'], yt: ['youtube', 'YouTube'] }[o.dataset.o]; o.innerHTML = ic(map[0], 'pf-' + map[0]) + `<span>${map[1]}</span>`; });
  phoneTilt($('#stagePhone'), $('#phone'));
}

/* ---------- clips ---------- */
let histFilter = 'all';
async function loadClips() { if (!S.clips.length) { try { S.clips = await api.get('/api/clips'); } catch (err) { toast(err.message); } } renderClips(); }
function renderClips() {
  const rows = S.clips.filter(c => histFilter === 'all' || c.platform === histFilter), up = S.clips.filter(c => c.state === 'PUBLISHED').length;
  $('#clipSum').textContent = S.clips.length ? `${S.clips.length} ${S.clips.length === 1 ? 'clip' : 'clips'} in the staging folder, ${up} uploaded. A person approves each one in Telegram before it goes anywhere. Pick one to watch it and see why it was chosen.` : 'Nothing is staged yet. Clips appear here as soon as a run makes one.';
  $('#clipList').innerHTML = rows.length ? rows.map((c, i) => `<button type="button" class="clip" data-id="${esc(c.id)}">${avatar(c.platform, c.streamer, 'lg')}<span class="ct"><b>${esc(c.title || c.id)}</b><small>${named(c.platform, c.streamer)}${badges(c.platform, c.streamer, 1)} · ${when(c.created)} · ${c.seconds != null ? c.seconds + ' s' : ''}${c.captions ? ' · ' + esc(c.captions) + ' captions' : ''}</small></span><span class="sc"><b class="num">${c.final_score != null ? (+c.final_score).toFixed(2) : '–'}</b><small>score</small></span>${stateBadge(c.state)}</button>`).join('')
    : `<div class="empty card"><span class="tile">${ic('clips')}</span><b>${S.clips.length ? 'None on this platform' : 'No clips yet'}</b><span>${S.clips.length ? 'Choose All to see every clip.' : 'Start a run and the first clip lands here.'}</span>${S.clips.length ? '' : '<a class="btn btn-primary" href="#run">Start a run</a>'}</div>`;
}
const LAYOUTS = { unsure: 'not sure where the webcam is: the whole frame is shown', 'no-face': 'no webcam seen: the whole frame is shown', fullcam: 'a camera that is the picture, followed full screen', 'auto-new': 'a webcam found in the picture (new for this streamer)', 'multi-cam': 'several webcams: the one that reacts', fixed: 'as set for the streamer (fixed)', default: 'as set for the streamer (no detection ran)', 'default-noface': 'the confirmed scene (face not seen this time)' };
const stateBadge = s => s === 'PUBLISHED' ? '<span class="badge live"><i class="dot"></i>Uploaded</span>' : s === 'APPROVED' ? '<span class="badge live"><i class="dot"></i>Approved</span>' : s === 'REJECTED' ? '<span class="badge"><i class="dot off"></i>Rejected</span>' : s === 'CLAIMED' ? '<span class="badge warn"><i class="dot warn"></i>Being reviewed</span>' : '<span class="badge warn"><i class="dot warn"></i>Waiting</span>';
function bars(pairs, unit) { const max = Math.max(...pairs.map(p => p[1]), 1e-9); return `<div class="bars">${pairs.map(([l, v]) => `<div class="ln"><span>${esc(l)}</span><div class="tr"><i style="width:${Math.max(1.5, 100 * v / max)}%"></i></div><b>${unit === 's' ? fmtS(v) : (+v).toFixed(2)}</b></div>`).join('')}</div>`; }
async function openClip(id) {
  let c; try { c = await api.get('/api/clips/' + encodeURIComponent(id)); } catch (err) { return toast(err.message); }
  const sig = c.signals || {}, w = (S.ov && S.insWeights) || { U: .3, M: .25, E: .3, S: .15 }, bonus = c.final_score != null && c.chat_score != null ? +(c.final_score - c.chat_score).toFixed(2) : 0;
  const made = Object.keys(sig).length ? bars([['Chatters', (sig.zU || 0) * w.U], ['Messages', (sig.zM || 0) * w.M], ['Keywords', (sig.zE || 0) * w.E], ['Short messages', (sig.zS || 0) * w.S]]) : '';
  const tim = Object.entries(c.timings || {}).filter(([k]) => !['wait', 'total', 'post'].includes(k));
  const j = c.judge || {}, flags = Object.entries(j.flags || {}).filter(([, v]) => v).map(([k]) => ({ sp: 'spoiler', mu: 'music risk', gb: 'gambling' }[k] || k));
  const pick = { layout: c.render_layout || 'split' }, lb = (S.ov && S.ov.caption_library) || {};
  const nSet = Object.keys(c.caption_settings || {}).length, capLine = !c.captions ? 'none' : esc(capName(c.captions)) + (c.caption_colors ? (c.caption_colors === 'auto' ? ', colours picked for this video' : ', the template\'s own colours') : '') + (nSet ? `, ${nSet} ${nSet === 1 ? 'setting' : 'settings'} of its own` : '') + (c.captions_error ? `<br><small style="color:var(--muted)">The animated template could not be rendered, so the built-in style was used: ${esc(c.captions_error)}</small>` : '');
  const words = c.recipe ? 'Rendered from the clean cut of this clip, with the words it was first rendered with.' : S.ov && S.ov.speech ? 'Rendered from the clean cut of this clip. Its words are transcribed again first.' : 'This clip was staged without its words and this machine has no speech model (faster-whisper), so it will come out without captions.';
  const again = !c.files['clean_16x9.mp4'] ? '' : c.state !== 'READY' ? `<div class="card"><h3 style="margin-bottom:8px">Settings for this video</h3><p class="cap-note">This clip is already ${esc(c.state.toLowerCase())}, so its video stays as the operator saw it.</p></div>`
    : `<div class="card"><h3 style="margin-bottom:12px">Settings for this video</h3><div class="cfg">
      <div class="field"><span class="eyebrow">Layout</span><div class="row" id="dLay"><button type="button" class="chip" data-v="split" aria-pressed="${pick.layout === 'split'}">Webcam over gameplay</button><button type="button" class="chip" data-v="stack" aria-pressed="${pick.layout === 'stack'}">Older stack</button></div></div>
      <div class="field"><span class="eyebrow">Captions</span><div id="dCapLib"></div></div>
      <p class="cap-note">${words}</p></div></div>`;
  $('#drawer').innerHTML = `<div class="row" style="justify-content:space-between"><span class="pill"><span>${c.lane === 'vod' ? 'VOD lane' : 'Live lane'} · ${when(c.created)}</span></span><button class="btn btn-glass btn-sm" id="dClose">Close</button></div>
    <h2 style="font-size:1.4rem">${esc(c.title || c.id)}</h2>${c.hook ? `<p style="color:var(--muted)">${esc(c.hook)}</p>` : ''}
    <div class="byline">${avatar(c.platform, c.streamer, 'lg')}<span class="who"><b>${named(c.platform, c.streamer)}${badges(c.platform, c.streamer, 4)}</b><small data-fo="${esc(pkey(c.platform, c.streamer))}">${esc(c.credit)}</small></span></div>
    ${c.files['short_9x16.mp4'] ? `<video class="dvideo" controls playsinline preload="metadata" src="/media/${encodeURIComponent(c.id)}/short_9x16.mp4"></video>` : ''}
    <div class="card"><h3 style="margin-bottom:12px">How it scored</h3>
      <dl class="kv"><dt>Final score</dt><dd>${c.final_score != null ? (+c.final_score).toFixed(2) : '–'}</dd><dt>From the chat</dt><dd>${c.chat_score != null ? (+c.chat_score).toFixed(2) : '–'}</dd><dt>Loudness and viewer clips</dt><dd>+${bonus.toFixed(2)}</dd><dt>Chatters in the spike</dt><dd>${c.chatters || '–'}</dd><dt>Rank in the stream</dt><dd>${c.chat_rank != null ? '#' + Math.round(c.chat_rank) : '–'}</dd><dt>A viewer clipped it too</dt><dd>${c.viewer_clip ? 'yes' : 'no'}</dd></dl>
      ${made ? `<p class="eyebrow" style="margin:16px 0 10px">What the chat score was made of</p>${made}` : ''}</div>
    ${again}
    <div class="card"><h3 style="margin-bottom:12px">Title and judge</h3><dl class="kv"><dt>Written by</dt><dd>${j.source === 'claude' ? 'Claude' : j.source === 'heuristic' ? 'the fallback (no API key or a failed call)' : 'the fallback (judge off)'}</dd>${j.score != null ? `<dt>Judge score</dt><dd>${j.score} of 10</dd>` : ''}${j.why ? `<dt>Why</dt><dd>${esc(j.why)}</dd>` : ''}<dt>Flags</dt><dd>${flags.length ? flags.join(', ') : 'none'}</dd><dt>Credit on screen</dt><dd>${esc(c.credit)}</dd><dt>Captions</dt><dd>${capLine}</dd><dt>Webcam layout</dt><dd>${esc(LAYOUTS[c.layout] || (String(c.layout || '').startsWith('scene:') ? 'the confirmed scene "' + c.layout.slice(6) + '"' : c.layout) || '–')}${(c.layout_doubts || []).map(d => '<br><small style="color:var(--muted)">' + esc(d) + '</small>').join('')}</dd></dl></div>
    ${tim.length ? `<div class="card"><h3 style="margin-bottom:12px">Where its ${fmtS(tim.reduce((a, [, v]) => a + v, 0))} of work went</h3>${bars(tim.map(([k, v]) => [stepName(k), v]), 's')}</div>` : ''}
    <div class="card"><h3 style="margin-bottom:12px">Where it stands</h3><dl class="kv"><dt>State</dt><dd>${esc(c.state)}</dd>${c.approved_by ? `<dt>Approved by</dt><dd>${esc(c.approved_by)}</dd>` : ''}<dt>YouTube</dt><dd>${c.youtube_id ? esc(c.youtube_id) : 'not uploaded'}</dd><dt>Files</dt><dd>${Object.entries(c.files).map(([n, b]) => n + ' ' + (b / 1048576).toFixed(1) + ' MB').join('<br>')}</dd><dt>Clip id</dt><dd>${esc(c.id)}</dd></dl></div>`;
  if ($('#dCapLib')) {
    $('#dLay').onclick = ev => { const b = ev.target.closest('button'); if (!b) return; $$('button', $('#dLay')).forEach(x => x.setAttribute('aria-pressed', x === b)); pick.layout = b.dataset.v; };
    const has = capOf(c.captions), start = has ? c.captions : capOf(S.ov.caption_default) ? S.ov.caption_default : 'word';
    capLib($('#dCapLib'), { compact: true, style: start, colors: (has && c.caption_colors) || lb.colors, settings: has ? c.caption_settings : {},
      primary: { label: () => 'Render again with these', run: async s => { const run = await api.post('/api/runs', { kind: 'render', params: { clip: c.id, captions: s.style, layout: pick.layout, ...(s.animated ? { colors: s.colors, settings: s.settings } : {}) } }); closeDrawer(); location.hash = 'run'; R.kind = 'render'; W.step = 4; paintWiz(); $$('.stepbox').forEach(b => { b.hidden = b.id !== 's4'; }); follow(run); } } });
  }
  $('#dClose').onclick = closeDrawer; $('#drawer').classList.add('open'); $('#scrim').classList.add('open'); $('#drawer').setAttribute('aria-hidden', 'false'); $('#drawer').scrollTop = 0; $('#dClose').focus();
}
function closeDrawer() { const v = $('#drawer video'); if (v) v.pause(); $('#drawer').classList.remove('open'); $('#scrim').classList.remove('open'); $('#drawer').setAttribute('aria-hidden', 'true'); }
function initClips() {
  seg($('#histSeg'), [['all', 'All'], ['twitch', ic('twitch', 'pf-twitch') + ' Twitch'], ['kick', ic('kick', 'pf-kick') + ' Kick']], 'all', v => { histFilter = v; renderClips(); });
  $('#clipList').addEventListener('click', e => { const b = e.target.closest('.clip'); if (b) openClip(b.dataset.id); });
  $('#scrim').onclick = closeDrawer; addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
}

/* ---------- insights: one chart at a time ---------- */
const C = { c0: '#8c93a6', c1: '#5590e6', c2: '#e26a66', c3: '#ab4bda', c4: '#22a99a', text: '#edf5ff', muted: 'rgba(237,245,255,.6)', grid: 'rgba(237,245,255,.08)', surface: '#1c1c28' };
const hasChart = !!window.Chart;
const FMT = { str: v => String(v), s: v => fmtS(v), usd: v => money(v) };
if (hasChart) {
  Chart.defaults.font.family = 'Geologica, system-ui, sans-serif'; Chart.defaults.font.size = 12; Chart.defaults.color = C.muted; Chart.defaults.borderColor = C.grid;
  Chart.defaults.animation.duration = reduce ? 0 : 1100; Chart.defaults.animation.easing = 'easeOutQuart'; Chart.defaults.maintainAspectRatio = false;
  Object.assign(Chart.defaults.plugins.tooltip, { backgroundColor: '#08080f', borderColor: 'rgba(255,255,255,.16)', borderWidth: 1, padding: 10, titleColor: '#fff', bodyColor: C.text, cornerRadius: 10, boxPadding: 4 });
  Object.assign(Chart.defaults.plugins.legend.labels, { boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: 'rectRounded', padding: 14 }); Chart.defaults.plugins.legend.position = 'bottom';
  Chart.register({ id: 'tip', afterDatasetsDraw(ch) { const o = ch.options.plugins.tip; if (!o || !o.k) return; const f = FMT[o.k] || String, x = ch.ctx; x.save(); x.font = '600 11px Geologica, sans-serif'; x.fillStyle = C.text;
    ch.data.datasets.forEach((ds, di) => { ch.getDatasetMeta(di).data.forEach((el, i) => { const v = ds.data[i]; if (v == null) return; const t = f(v, i); if (!t) return; if (ch.options.indexAxis === 'y') { x.textAlign = 'left'; x.textBaseline = 'middle'; x.fillText(t, el.x + 6, el.y); } else { x.textAlign = 'center'; x.textBaseline = 'bottom'; x.fillText(t, el.x, el.y - 5); } }); }); x.restore(); } });
}
const bar = (labels, data, colors, o = {}) => ({ type: 'bar', data: { labels, datasets: [{ label: o.label || 'Value', data, backgroundColor: colors, borderRadius: o.h ? { topRight: 4, bottomRight: 4 } : { topLeft: 4, topRight: 4 }, borderSkipped: o.h ? 'left' : 'bottom', maxBarThickness: 24 }] }, options: { plugins: { legend: { display: false }, tip: { k: o.tip || 'str' } }, layout: { padding: { top: 18, right: o.h ? 58 : 4 } }, scales: { x: { grid: { display: !!o.h }, beginAtZero: true, ticks: { maxRotation: o.rot || 0, ...(o.h && !o.tip ? { precision: 0 } : {}) } }, y: { beginAtZero: true, grid: { display: !o.h }, ticks: !o.h && !o.tip ? { precision: 0 } : {} } }, ...(o.h ? { indexAxis: 'y' } : {}) } });
const donut = (labels, data, colors) => ({ type: 'doughnut', data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: C.surface, borderWidth: 2, hoverOffset: 8 }] }, options: { cutout: '62%' } });
const empty = (title, text) => ({ html: `<div class="empty"><span class="tile">${ic('insights')}</span><b>${title}</b><span>${text}</span></div>` });
function tableOf(cfg) {
  const d = cfg.data, pts = d.datasets[0].data[0] && typeof d.datasets[0].data[0] === 'object';
  if (pts) return `<table><thead><tr><th>Name</th><th class="r">x</th><th class="r">y</th></tr></thead><tbody>${d.datasets[0].data.map(p => `<tr><td>${esc(p.n)}</td><td class="r">${p.x}</td><td class="r">${p.y}</td></tr>`).join('')}</tbody></table>`;
  return `<table><thead><tr><th></th>${d.datasets.map(s => `<th class="r">${esc(s.label || 'Value')}</th>`).join('')}</tr></thead><tbody>${d.labels.map((l, i) => `<tr><td>${esc(l)}</td>${d.datasets.map(s => `<td class="r">${s.data[i] == null ? '' : s.data[i]}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const EST = store.get('est', { clips: 60, frames: 12, text: 900, out: 700, cached: true, batch: false, two: false });
function estimate() {
  const c = S.ins.cost, P_ = c.prices || {}, sn = P_[c.judge_model], hk = P_[c.screen_model]; if (!sn) return null;
  const sys = c.assume.system_tokens, frames = EST.frames * c.assume.frame_tokens, mul = EST.batch ? .5 : 1;
  const call = (p, rest, out) => (sys * (EST.cached ? p.cache_read : p.cache_write) + rest * p.input + out * p.output) / 1e6 * mul;
  const judge = call(sn, frames + EST.text, EST.out), per = EST.two && hk ? call(hk, EST.text, 60) + .4 * judge : judge;
  return { per, month: per * EST.clips * 30, parts: [sys * (EST.cached ? sn.cache_read : sn.cache_write), frames * sn.input, EST.text * sn.input, EST.out * sn.output].map(v => v / 1e6 * mul) };
}
function estHTML() {
  const e = estimate(); if (!e) return empty('No price list', 'Add judge.prices to config/global.yaml to estimate the cost.').html;
  const rng = (id, label, min, max, step, hint) => `<div class="cfg-row"><label for="e-${id}">${label}</label><input type="range" id="e-${id}" data-e="${id}" min="${min}" max="${max}" step="${step}" value="${EST[id]}"><output>${(+EST[id]).toLocaleString('en-US')}</output>${hint ? `<small>${hint}</small>` : ''}</div>`;
  const sw = (id, label, hint) => `<div class="cfg-row"><label id="el-${id}">${label}</label><button class="sw-t" role="switch" data-e="${id}" aria-checked="${!!EST[id]}" aria-labelledby="el-${id}"></button><span></span>${hint ? `<small>${hint}</small>` : ''}</div>`;
  return `<div id="est"><div class="grid statrow" style="grid-template-columns:repeat(2,minmax(0,1fr))"><div class="card"><div class="tile"><b class="num" id="estPer">${(e.per * 100).toFixed(2)}¢</b><span>a judged clip</span></div></div><div class="card"><div class="tile"><b class="num" id="estMonth">${money(e.month)}</b><span>a month</span></div></div></div>
    ${rng('clips', 'Clips judged a day', 5, 400, 5, 'The detector keeps at most 8 clips an hour per stream.')}${rng('frames', 'Frames per clip', S.ins.cost.assume.frames, S.ins.cost.assume.frames_max, 1, 'About ' + S.ins.cost.assume.frame_tokens + ' tokens a frame.')}
    ${rng('text', 'Transcript and chat tokens', 300, 3000, 100, 'Assumed until real calls report their usage.')}${rng('out', 'Answer and thinking tokens', 200, 2000, 50, '')}
    ${sw('cached', 'System prompt read from cache', S.ins.cost.assume.system_tokens.toLocaleString('en-US') + ' tokens, identical for every clip.')}${sw('two', 'Screen with the small model first', 'The judge then sees about four clips in ten.')}${sw('batch', 'Batch (half price, not live)', 'Only for the VOD lane.')}</div>`;
}
function slides(tab) {
  const I = S.ins, sp = I.speed, co = I.cost, ju = I.judge, de = I.detector, work = sp.steps.filter(s => s.step !== 'wait' && s.avg_s >= 0.05);
  if (tab === 'speed') return [
    work.length ? { title: 'What each step costs', sub: `Average seconds over ${plural(sp.clips, 'clip')}`, cfg: bar(work.map(s => s.name), work.map(s => s.avg_s), work.map(s => s.step.startsWith('render') ? C.c1 : C.c0), { h: 1, tip: 's', label: 'Seconds' }) } : { title: 'What each step costs', sub: '', ...empty('No timings yet', 'Clips made from now on keep the seconds of every step. Run a lane and they show up here.') },
    sp.per_clip.length ? { title: 'Work against length, clip by clip', sub: 'Seconds of work to make each clip, next to how long the clip is', cfg: { type: 'bar', data: { labels: sp.per_clip.map(p => p.streamer + ' ' + p.id.slice(-6)), datasets: [{ label: 'Work', data: sp.per_clip.map(p => p.work_s), backgroundColor: C.c1, borderRadius: { topLeft: 4, topRight: 4 }, maxBarThickness: 24 }, { label: 'Clip length', data: sp.per_clip.map(p => p.clip_s), backgroundColor: C.c0, borderRadius: { topLeft: 4, topRight: 4 }, maxBarThickness: 24 }] }, options: { layout: { padding: { top: 18 } }, scales: { x: { grid: { display: false } }, y: { beginAtZero: true } } } } } : null,
    sp.runs && sp.runs.length ? { title: 'Every run started from here', sub: 'Newest first', html: `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Run</th><th>What</th><th>How it went</th><th class="r">Took</th><th class="r">Clips</th><th></th></tr></thead><tbody>${sp.runs.map(r => `<tr><td>${when(r.when)}</td><td>${{ live: 'Live lane', vod: 'VOD lane', tests: 'Test suite', render: 'Render again' }[r.kind] || esc(r.kind)}</td><td class="wrapc">${esc(r.what)}</td><td>${{ done: 'finished', stopped: 'stopped', failed: 'error', running: 'running' }[r.state] || esc(r.state)}</td><td class="r">${fmtS(r.seconds)}</td><td class="r">${r.clips}</td><td class="r">${r.replay && r.state !== 'running' ? `<button type="button" class="btn btn-glass btn-sm" data-watch="${esc(r.id)}">Watch</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` } : null,
    sp.tests.length ? { title: 'The test suite, run by run', sub: 'Seconds for the whole suite', cfg: bar(sp.tests.map(t => when(t.when)), sp.tests.map(t => t.seconds), sp.tests.map(t => t.failed ? C.c2 : C.c4), { tip: 's', label: 'Seconds' }) } : null,
  ].filter(Boolean);
  if (tab === 'cost') return [
    co.calls ? { title: 'Spend, day by day', sub: 'US dollars, from the judge ledger', cfg: bar(co.by_day.map(d => d.day.slice(5)), co.by_day.map(d => +d.usd.toFixed(4)), C.c1, { tip: 'usd', label: 'USD' }) } : null,
    co.calls ? { title: 'Where the money goes', sub: 'By kind of token', cfg: donut(['Input', 'Output', 'Cache read', 'Cache write'], ['input', 'output', 'cache_read', 'cache_write'].map(k => +(co.by_kind[k] || 0).toFixed(5)), [C.c1, C.c2, C.c4, C.c3]) } : null,
    co.calls && co.by_model.length > 1 ? { title: 'By model', sub: 'US dollars', cfg: bar(co.by_model.map(m => m.model), co.by_model.map(m => +m.usd.toFixed(4)), C.c3, { tip: 'usd', label: 'USD' }) } : null,
    { title: 'What it would cost', sub: 'An estimate from the price list in config/global.yaml. Move the assumptions.', html: estHTML(), est: true },
  ].filter(Boolean);
  if (tab === 'judge') { const src = ju.sources, n = Object.values(src).reduce((a, b) => a + b, 0); return [
    n ? { title: 'Who wrote the title of each clip', sub: `${n} staged ${n === 1 ? 'clip' : 'clips'}`, cfg: donut(['Claude', 'Fallback, no key or failed call', 'Fallback, judge off'], [src.claude || 0, src.heuristic || 0, src.none || 0], [C.c2, C.c1, C.c0]) } : { title: 'Who wrote the title of each clip', sub: '', ...empty('No clips yet', 'Once clips are staged, this shows how many titles Claude wrote.') },
    Object.keys(ju.scores).length ? { title: 'How the judge scored', sub: 'Calls by score, 1 to 10', cfg: bar([1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(String), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(s => ju.scores[s] || 0), C.c1, { label: 'Calls' }) } : null,
    { title: 'Every decision', sub: ju.decisions.length ? 'Newest first' : '', html: ju.decisions.length ? `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Streamer</th><th>Decision</th><th class="r">Score</th><th>Title or reason</th><th class="r">Cost</th></tr></thead><tbody>${ju.decisions.map(d => `<tr><td>${when(d.t)}</td><td>${esc(d.streamer || '')}</td><td>${d.error ? 'call failed' : d.ok ? 'accepted' : 'rejected'}</td><td class="r">${d.score == null ? '' : d.score}</td><td class="wrapc">${esc(d.error || d.title || d.why || '')}</td><td class="r">${d.usd == null ? '' : money(d.usd)}</td></tr>`).join('')}</tbody></table></div>` : empty('The judge has not been called', S.ov && S.ov.judge_key ? 'Its calls will be listed here.' : 'No API key is set (ANTHROPIC_API_KEY), so the built-in fallback writes every title.').html },
  ].filter(Boolean); }
  return [
    de.clips ? { title: 'Clips by streamer', sub: 'Everything in the staging folder', cfg: bar(de.by_streamer.map(s => s.streamer), de.by_streamer.map(s => s.clips), de.by_streamer.map(s => s.platform === 'kick' ? C.c4 : C.c3), { label: 'Clips' }) } : { title: 'Clips by streamer', sub: '', ...empty('No clips yet', 'The detector\'s numbers come from the clips it stages.') },
    de.signal_clips ? { title: 'What the chat score is made of', sub: `How far above normal each signal stood, averaged over ${de.signal_clips} ${de.signal_clips === 1 ? 'clip' : 'clips'}, times its weight`, cfg: bar(['Chatters', 'Messages', 'Keywords', 'Short messages'], ['U', 'M', 'E', 'S'].map(k => +((de.signals['z' + k] || 0) * (de.weights[k] || 0)).toFixed(2)), [C.c1, C.c4, C.c2, C.c3], { label: 'Part of the score' }) } : null,
    de.scores.length > 1 ? { title: 'Chat score against final score', sub: 'One dot per clip; the gap is loudness and viewer clips', cfg: { type: 'scatter', data: { datasets: [{ label: 'Clip', data: de.scores.map(s => ({ x: +(+s.chat).toFixed(2), y: +(+s.final).toFixed(2), n: s.streamer })), backgroundColor: C.c1, borderColor: C.surface, borderWidth: 2, pointRadius: 6, pointHoverRadius: 9 }] }, options: { plugins: { legend: { display: false }, tooltip: { callbacks: { label: x => ` ${x.raw.n}: chat ${x.raw.x}, final ${x.raw.y}` } } }, scales: { x: { title: { display: true, text: 'chat score' }, beginAtZero: true, grid: { display: false } }, y: { title: { display: true, text: 'final score' }, beginAtZero: true } } } } } : null,
    de.by_day.length > 1 ? { title: 'Clips, day by day', sub: '', cfg: bar(de.by_day.map(d => d.day.slice(5)), de.by_day.map(d => d.clips), C.c1, { label: 'Clips' }) } : null,
  ].filter(Boolean);
}
function headline(tab) {
  const I = S.ins;
  if (tab === 'speed') return I.speed.avg_work_s != null ? [fmtS(I.speed.avg_work_s), `of work to make a clip, on average over ${plural(I.speed.clips, 'clip')}. The budget of the live lane is about a minute a clip.`] : ['No clips timed', 'yet. Each new clip keeps the seconds of every step.'];
  if (tab === 'cost') return [money(I.cost.usd), I.cost.calls ? `spent on ${I.cost.calls} judge ${I.cost.calls === 1 ? 'call' : 'calls'}${I.cost.failed ? ', plus ' + I.cost.failed + ' that failed' : ''}.` : (S.ov && S.ov.judge_key ? 'spent so far. No judge call has been made yet.' : 'spent. No API key is set, so no paid call has been made.')];
  if (tab === 'judge') { const s = I.judge.sources, n = Object.values(s).reduce((a, b) => a + b, 0); return [`${s.claude || 0} of ${n}`, n ? 'titles were written by Claude. The built-in fallback wrote the rest.' : 'No clips are staged yet.']; }
  return [String(I.detector.clips), `${I.detector.clips === 1 ? 'clip' : 'clips'} staged. ${I.detector.viewer_clip} of them viewers clipped too; loudness was measured on ${I.detector.loud}.`];
}
const ins = { tab: store.get('ins', 'speed'), i: 0, table: false }; let slChart = null;
async function loadInsights() { if (!S.ins) { try { S.ins = await api.get('/api/insights'); S.insWeights = S.ins.detector.weights; } catch (err) { return toast(err.message); } } renderIns(false); }
function renderIns(anim) {
  if (!S.ins) return; if (!['speed', 'cost', 'judge', 'detector'].includes(ins.tab)) ins.tab = 'speed';
  const list = slides(ins.tab); ins.i = Math.min(ins.i, list.length - 1); const def = list[ins.i], [big, text] = headline(ins.tab);
  $('#insBig').textContent = big; $('#insText').textContent = text; $('#slTitle').textContent = def.title; $('#slSub').textContent = def.sub || '';
  $('#slCount').textContent = list.length > 1 ? (ins.i + 1) + ' of ' + list.length : ''; $('#slPrev').hidden = $('#slNext').hidden = list.length < 2; $('#slPrev').disabled = ins.i === 0; $('#slNext').textContent = ins.i === list.length - 1 ? 'Back to the first' : 'Next';
  if (slChart) { slChart.destroy(); slChart = null; }
  const box = $('#slBox'), html = $('#slHtml'), tb = $('#slTable');
  if (def.html) { box.hidden = true; html.hidden = false; html.innerHTML = def.html; tb.hidden = true; if (def.est) wireEst(); }
  else if (ins.table || !hasChart) { box.hidden = true; html.hidden = false; html.innerHTML = '<div class="tbl-wrap">' + tableOf(def.cfg) + '</div>'; tb.hidden = !hasChart; tb.textContent = 'Chart'; }
  else { html.hidden = true; box.hidden = false; tb.hidden = false; tb.textContent = 'Table'; $('#slCv').setAttribute('aria-label', def.title); slChart = new Chart($('#slCv'), def.cfg); }
  if (anim && !reduce) { const s = $('#slide'); s.classList.remove('swap'); void s.offsetWidth; s.classList.add('swap'); }
}
function wireEst() {
  const upd = () => { const e = estimate(); store.set('est', EST); if (e) { $('#estPer').textContent = (e.per * 100).toFixed(2) + '¢'; $('#estMonth').textContent = money(e.month); } };
  $$('#est input[data-e]').forEach(i => i.oninput = () => { EST[i.dataset.e] = +i.value; i.nextElementSibling.textContent = (+i.value).toLocaleString('en-US'); upd(); });
  $$('#est .sw-t').forEach(b => b.onclick = () => { EST[b.dataset.e] = !EST[b.dataset.e]; b.setAttribute('aria-checked', EST[b.dataset.e]); upd(); });
}
function initInsights() {
  seg($('#insSeg'), [['speed', 'Speed'], ['cost', 'Cost'], ['judge', 'Judge'], ['detector', 'Detector']], ins.tab, v => { ins.tab = v; ins.i = 0; ins.table = false; store.set('ins', v); renderIns(true); });
  $('#slNext').onclick = () => { ins.i = (ins.i + 1) % slides(ins.tab).length; ins.table = false; renderIns(true); };
  $('#slPrev').onclick = () => { if (ins.i > 0) { ins.i--; ins.table = false; renderIns(true); } };
  $('#slTable').onclick = () => { ins.table = !ins.table; renderIns(true); };
  $('#slHtml').addEventListener('click', e => { const b = e.target.closest('[data-watch]'); if (!b) return; if (!R.ended && R.id && !R.replay) return toast('A run is going. Watch this one when it has ended.'); replay(b.dataset.watch); });
}

/* ---------- settings ---------- */
const E = {};  // what the page holds, key -> value, until Save
const shown = (f, v) => f.unit === 'top' ? ((1 - v) * 100).toFixed(1) + '%' : f.type === 'toggle' ? (v ? 'on' : 'off') : f.type === 'select' ? '' : (Number.isInteger(f.step) && Number.isInteger(f.min) ? v : (+v).toFixed(2)) + (f.unit && f.unit !== 'top' ? ' ' + f.unit : '');
async function loadSettings() { if (!S.cfg) { try { S.cfg = await api.get('/api/settings'); } catch (err) { return toast(err.message); } for (const k in E) delete E[k]; S.cfg.groups.forEach(g => g.fields.forEach(f => { E[f.key] = f.value; })); } renderSettings(); }
function fieldOf(key) { for (const g of S.cfg.groups) for (const f of g.fields) if (f.key === key) return f; }
function paintRow(rowEl) { const f = fieldOf(rowEl.dataset.k), v = E[f.key], inp = rowEl.querySelector('input,select,.sw-t'); if (f.type === 'toggle') inp.setAttribute('aria-checked', !!v); else inp.value = v; rowEl.querySelector('output').textContent = shown(f, v); rowEl.classList.toggle('changed', v !== f.default); }
function changes() { const unsaved = [], off = []; S.cfg.groups.forEach(g => g.fields.forEach(f => { if (E[f.key] !== f.value) unsaved.push(f.key); if (E[f.key] !== f.default) off.push(f.key); })); return { unsaved, off }; }
function paintSave() {
  const { unsaved, off } = changes(); $('#saveBar').hidden = !unsaved.length && !off.length;
  $('#cfgCount').textContent = unsaved.length ? unsaved.length + (unsaved.length === 1 ? ' change not saved' : ' changes not saved') : off.length + (off.length === 1 ? ' setting differs' : ' settings differ') + ' from global.yaml';
  $('#cfgCount').className = 'badge' + (unsaved.length ? ' warn' : ''); $('#cfgWhat').textContent = unsaved.length ? 'Saved to config/local.yaml; used from the next run.' : ''; $('#cfgSave').hidden = !unsaved.length;
  $('#cfgReset').textContent = unsaved.length ? 'Undo my changes' : 'Back to global.yaml';
  const w = ['U', 'M', 'E', 'S'].map(x => +E['detector.weights.' + x] || 0), sum = w.reduce((a, b) => a + b, 0) || 1, wb = $('#wbar');
  if (wb) { wb.innerHTML = w.map((x, i) => `<i style="flex-basis:${100 * x / sum}%;background:var(--c${i + 1})"></i>`).join(''); $('#wsum').textContent = `chat score = ${w[0].toFixed(2)} chatters + ${w[1].toFixed(2)} messages + ${w[2].toFixed(2)} keywords + ${w[3].toFixed(2)} short` + (Math.abs(sum - 1) > .001 ? `  (adds up to ${sum.toFixed(2)}, not 1)` : ''); }
}
function renderSettings() {
  const ov = S.ov || { streamers: [] };
  const rows = g => g.fields.map(f => { const id = 'f-' + f.key.replace(/\./g, '-'); const ctl = f.type === 'range' ? `<input type="range" id="${id}" min="${f.min}" max="${f.max}" step="${f.step}">` : f.type === 'toggle' ? `<button class="sw-t" role="switch" id="${id}" aria-label="${esc(f.label)}"></button>` : `<select id="${id}">${f.options.map(o => `<option value="${esc(o)}">${esc(f.key === 'render.captions' ? capName(o) + (String(o).startsWith('rm:') ? ' (animated)' : '') : o)}</option>`).join('')}</select>`; return `<div class="cfg-row" data-k="${f.key}"><label for="${id}">${esc(f.label)}</label>${ctl}<output></output>${f.hint ? `<small>${esc(f.hint)}</small>` : ''}</div>`; }).join('');
  $('#cfgGrid').innerHTML = S.cfg.groups.map((g, gi) => `<details class="acc" name="acc"${gi === 0 ? ' open' : ''}><summary><i>${gi + 1}</i><span><b>${esc(g.title)}</b><small>${esc(g.about)}</small></span></summary><div class="cfg">${gi === 0 ? '<div class="wbar" id="wbar"></div><p class="cap-note" id="wsum"></p>' : ''}${rows(g)}</div></details>`).join('') +
    `<details class="acc" name="acc"><summary><i>${S.cfg.groups.length + 1}</i><span><b>Sources and destinations</b><small>Who the bot watches and where approved clips go</small></span></summary><div class="cfg"><div class="row">${ov.streamers.length ? ov.streamers.map(s => `<span class="badge src">${avatar(s.platform, s.login, 'xs')}${named(s.platform, s.login)}${badges(s.platform, s.login, 1)}</span>`).join('') : '<span class="cap-note">No streamers in config/streamers.yaml yet.</span>'}</div><div class="row"><span class="badge">${ic('telegram', 'pf-telegram')} Telegram approval${ov.telegram ? '' : ' (no bot token set)'}</span><span class="badge">${ic('youtube', 'pf-youtube')} YouTube Shorts${ov.dry_run ? ' (dry run: no uploads)' : ''}</span><span class="badge">${ic('claude', 'pf-claude')} Judge${ov.judge_key ? '' : ' (no API key: fallback titles)'}</span></div><p class="cap-note">Streamers, keys and tokens are set in the config files and the environment, not here.</p></div></details>`;
  $$('#cfgGrid [data-k]').forEach(rowEl => { const f = fieldOf(rowEl.dataset.k), inp = rowEl.querySelector('input,select,.sw-t'); paintRow(rowEl);
    const set = v => { E[f.key] = v; paintRow(rowEl); paintSave(); };
    if (f.type === 'toggle') inp.onclick = () => set(!E[f.key]); else inp.oninput = () => set(f.type === 'range' || typeof f.options[0] === 'number' ? +inp.value : inp.value); });
  $$('#cfgGrid .acc').forEach(d => d.addEventListener('toggle', () => { if (d.open) $$('#cfgGrid .acc').forEach(o => { if (o !== d) o.open = false; }); }));
  paintSave();
}
function initSettings() {
  $('#cfgSave').onclick = async () => { try { const r = await api.post('/api/settings', { values: E }); S.cfg.groups = r.groups; S.ov = null; loadOverview(); renderSettings(); toast('Saved. Used from the next run.'); } catch (err) { toast(err.message); } };
  $('#cfgReset').onclick = async () => { const { unsaved } = changes();
    if (unsaved.length) { S.cfg.groups.forEach(g => g.fields.forEach(f => { E[f.key] = f.value; })); renderSettings(); return toast('Changes undone'); }
    try { const r = await api.post('/api/settings', { values: {} }); S.cfg.groups = r.groups; r.groups.forEach(g => g.fields.forEach(f => { E[f.key] = f.value; })); S.ov = null; loadOverview(); renderSettings(); toast('Back to the values of global.yaml'); } catch (err) { toast(err.message); } };
}

/* ---------- start ---------- */
async function loadOverview() {
  try { S.ov = await api.get('/api/overview'); } catch (err) { toast('The server does not answer: ' + err.message); return; }
  const ov = S.ov; $('#dryBadge').innerHTML = ov.dry_run ? '<i class="dot warn"></i>Dry run' : '<i class="dot"></i>Uploads on'; $('#dryBadge').className = 'badge dry ' + (ov.dry_run ? 'warn' : 'live'); $('#dryBadge').title = ov.dry_run ? 'DRY_RUN=1: nothing is uploaded or posted' : 'DRY_RUN=0: approved clips are uploaded';
  $('#footNote').textContent = (ov.dry_run ? 'Dry run: nothing is uploaded. ' : '') + 'Staging folder: ' + ov.staging;
  paintTpls(); if (W.step === 2) paintSource();
  Object.values(CW.views).forEach(v => { if (v.overview) v.overview(ov); });
  return ov;
}
function onShow(name) {
  if (name === 'run' && W.step === 3 && capW) capW.refresh();
  if (name === 'clips') loadClips().then(() => stagger($('#v-clips')));
  if (name === 'insights') loadInsights();
  if (name === 'setup') loadSettings();
  const more = CW.views[name]; if (more && more.show) more.show();
}
/* ---------- what the page's other scripts build on ----------
   library.js and connect.js each fill one place: they register { show(), overview(ov) } under CW.views and
   use the helpers below, so every place looks and behaves like the rest. They load after this file and
   before the page starts. */
window.CW = { $, $$, api, ic, P, pf, PFN, esc, toast, seg, tilt, phoneTilt, stagger, store, reduce, S, when, plural, compact, fmtS, capLib, capOf, capName, loadOverview, views: {} };
addEventListener('DOMContentLoaded', () => {
initRun(); initClips(); initInsights(); initSettings();
show(location.hash.slice(1) || store.get('view', 'run'));
loadOverview().then(ov => { if (ov && ov.active) { if (['live', 'vod', 'tests'].includes(ov.active.kind)) W.mode = ov.active.kind; W.step = 4; $$('.stepbox').forEach(b => { b.hidden = b.id !== 's4'; }); follow(ov.active); paintWiz(); if (!location.hash) location.hash = 'run'; } });
});
})();
