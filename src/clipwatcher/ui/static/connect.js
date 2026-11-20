/* The Connect place of the companion app. Builds on window.CW (app.js).
   Three connections, one screen at a time: a hub of three cards, and behind each its own walk. Claude's key
   and Telegram's token are pasted, checked and kept by the server (the page never gets one back); a YouTube
   account is signed in by a person in a Chrome window the server opens, while this page follows along. */
(() => {
const { $, $$, api, ic, esc, toast, tilt, phoneTilt, stagger, reduce, when, compact, P } = CW;
Object.assign(P, {
  cnBack: 'M11 5 4 12l7 7 1.4-1.4L7.8 13H20v-2H7.8l4.6-4.6z', cnNext: 'm13 5 7 7-7 7-1.4-1.4 4.6-4.6H4v-2h12.2l-4.6-4.6z',
  cnPlus: 'M11 4h2v7h7v2h-7v7h-2v-7H4v-2h7z', cnLock: 'M12 2a5 5 0 0 1 5 5v3h1a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h1V7a5 5 0 0 1 5-5zm0 2a3 3 0 0 0-3 3v3h6V7a3 3 0 0 0-3-3z',
  cnUser: 'M12 3a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9zm0 11c4.4 0 8 2.2 8 5v2H4v-2c0-2.8 3.6-5 8-5z', cnPost: 'm12 3 6 6-1.4 1.4L13 6.8V16h-2V6.8L7.4 10.4 6 9zM5 18h14v2H5z',
  cnWin: 'M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm0 5v9h16V9zm1.5-3.2a1 1 0 1 0 0 2 1 1 0 0 0 0-2zm3 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2z',
});
const root = $('#v-connect');
const C = { st: null, scr: 'hub', which: null, kstep: 'home', res: null, confirm: '', job: null, astep: 'intro', kind: 'add', acct: null, poll: 0, sig: '' };
const R = n => $(`[data-r="${n}"]`, root);
const wait = ms => new Promise(r => setTimeout(r, reduce ? 0 : ms));
const SVC = {
  claude: { name: 'Claude', icon: 'claude', what: 'Titles and scores', noun: 'key', ph: 'sk-ant-…', title: 'Paste your <span class="gt">Claude key</span>',
    where: 'Claude looks at every moment, scores it and writes its title. The key comes from the Anthropic Console: open API keys, create one and copy it.',
    off: 'Without a key the bot still clips, with plain titles and no scores.', gone: 'The bot goes back to plain titles and no scores.' },
  telegram: { name: 'Telegram', icon: 'telegram', what: 'Approval', noun: 'token', ph: '123456789:AA…', title: 'Paste your <span class="gt">bot token</span>',
    where: 'Your team approves every clip on a card that a bot posts in your Telegram group. The token comes from BotFather in Telegram: send it /newbot, or /token for a bot you already have, and copy what it answers.',
    off: 'Without a bot, clips wait on this machine and nobody is asked to approve them.', gone: 'No more cards are posted for approval.' },
  youtube: { name: 'YouTube', icon: 'youtube', what: 'Posting' },
};
const TITLE = { works: s => `${s.name} is <span class="gt">connected</span>`, rejected: s => s.noun === 'key' ? 'Claude did <span class="gt">not accept</span> this key' : 'Telegram does <span class="gt">not know</span> this token',
  no_model: () => 'The key works, the <span class="gt">model</span> is out of reach', no_group: () => 'The bot works. <span class="gt">One thing</span> is left', offline: s => `${s.name} could <span class="gt">not be reached</span>`,
  busy: s => `${s.name} is <span class="gt">busy</span> right now`, failed: () => 'The check did <span class="gt">not finish</span>', unchecked: s => `Your ${s.noun} is <span class="gt">saved</span>` };
const badge = (st, label) => `<span class="badge${st === 'on' ? ' live' : st === 'warn' ? ' warn' : ''}"><i class="dot${st === 'on' ? '' : st === 'warn' ? ' warn' : ' off'}"></i>${esc(label)}</span>`;
const wiz = (labels, now) => `<ol class="wiz" aria-label="Steps">${labels.map((l, i) => { const n = i + 1, st = n < now ? 'done' : n === now ? 'now' : 'todo'; return `<li data-st="${st}"><button type="button" disabled${n === now ? ' aria-current="step"' : ''}><i>${st === 'done' ? ic('check') : n}</i><span>${l}</span></button></li>`; }).join('')}</ol>`;
const backTo = (label, to) => `<button type="button" class="chip cn-back" data-do="go" data-to="${to}" data-a>${ic('cnBack')}${label}</button>`;
const btn = (act, label, cls = 'btn-glass', more = '') => `<button type="button" class="btn ${cls}" data-do="${act}"${more}>${label}</button>`;
const nameOf = a => a.name || 'Your channel';
const pic = (a, cls = '') => `<span class="cn-pic ${cls}">${a && a.picture ? `<img src="${esc(a.picture)}" alt="">` : `<i>${esc((a && a.name ? a.name : 'Y')[0].toUpperCase())}</i>`}<span class="pfm pf-youtube">${ic('youtube')}</span></span>`;
const subs = a => a.subscribers == null ? '' : compact(a.subscribers) + (a.subscribers === 1 ? ' subscriber' : ' subscribers');
const clock = s => Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');
const stepRow = s => `<li class="step" data-s="${s.state}"><span class="bub">${ic({ run: 'spin', done: 'check', failed: 'cross', skip: 'skip' }[s.state] || 'dotc')}</span><span class="nm">${esc(s.name)}</span><span class="tm num"></span><span class="dt">${esc(s.detail || '')}</span><span class="bar"><i></i></span></li>`;

async function load() { C.st = await api.get('/api/connect'); C.job = C.st.youtube.login; return C.st; }
function go(scr) { C.scr = scr; C.confirm = ''; paint(); scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' }); }
function paint() { if (!C.st) return; ({ hub: paintHub, key: paintKey, yt: paintYt, add: paintAdd })[C.scr](); stagger(root); }

/* ---------- the hub: three cards ---------- */
function keyLine(which) {
  const k = C.st.keys[which], s = SVC[which], c = k.check;
  if (!k.set) return ['off', 'Not connected', s.off];
  if (!c) return ['warn', 'Not checked yet', `A ${s.noun} ending in ${k.hint} is saved. Open it to check that it works.`];
  if (c.ok) return ['on', 'Connected', c.advice || c.headline];
  return ['warn', c.outcome === 'no_group' ? 'One thing left' : 'Needs a look', c.headline + (c.advice ? ' ' + c.advice : '')];
}
function ytLine() {
  const y = C.st.youtube, post = y.accounts.find(a => a.posting);
  if (!post) return ['off', 'Not connected', y.legacy ? 'A sign-in from before is on this machine. Open to bring it into the list.' : 'No account yet. Sign in once and approved Shorts go to your channel.'];
  if (post.state !== 'ready') return ['warn', 'Needs signing in', `${nameOf(post)} has to sign in again before it can post.`];
  return ['on', 'Ready to post', `Approved Shorts go to ${nameOf(post)}${post.handle ? ' (' + post.handle + ')' : ''}.`];
}
function paintHub() {
  const card = (id, n, [st, label, line], extra = '') => { const s = SVC[id]; return `<button type="button" class="choice cn-card" data-do="open" data-id="${id}" data-st="${st}">
    <span class="cn-top"><span class="tile pf-${s.icon}">${ic(s.icon)}</span>${badge(st, label)}</span>
    <span class="eyebrow">${n} · ${s.what}</span><b>${s.name}</b><p>${esc(line)}</p>${extra}<span class="cn-go">${st === 'off' ? 'Set it up' : 'Open'}${ic('cnNext')}</span></button>`; };
  const accts = C.st.youtube.accounts, stack = accts.length ? `<span class="cn-stack">${accts.slice(0, 4).map(a => pic(a, 'sm')).join('')}<small>${accts.length === 1 ? '1 account' : accts.length + ' accounts'}</small></span>` : '';
  root.innerHTML = `<div class="stepbox"><div class="vhead"><div><h1 data-a>What the bot is <span class="gt">connected</span> to</h1><p class="lead" data-a>Three connections close the loop: Claude judges the moment, your team approves it in Telegram, YouTube gets the Short. Open one to set it up or to check it.</p></div></div>
    <div class="choices cn-hub" data-a>${card('claude', 1, keyLine('claude'))}${card('telegram', 2, keyLine('telegram'))}${card('youtube', 3, ytLine(), stack)}</div></div>`;
  $$('.cn-card', root).forEach(el => tilt(el, 6));
}

/* ---------- a key: paste, check, connected ---------- */
function openKey(which) { C.which = which; const k = C.st.keys[which]; C.res = k.check; C.kstep = k.set ? 'home' : 'paste'; go('key'); }
function paintKey() {
  const s = SVC[C.which];
  root.innerHTML = `<div class="stepbox">${backTo('Connect', 'hub')}<div data-r="wiz" data-a></div>
    <div class="vhead"><div><h1 data-a data-r="h1"></h1><p class="lead" data-a data-r="lead"></p></div></div>
    <div class="runwrap cn-wrap"><div class="card cn-stagecard" data-a><div class="stage3d cn-stage"><div class="rings"><i></i><i></i><i></i></div>
      <div class="cn-pass float" data-st="empty"><span class="cn-mark pf-${s.icon}">${ic(s.icon)}</span><span class="cn-mask" data-r="mask"></span><span class="cn-light"><i class="dot off"></i><span data-r="light"></span></span><span class="cn-scan"></span></div></div></div>
      <div class="panel cn-panel" data-a data-r="panel" aria-live="polite"></div></div></div>`;
  phoneTilt($('.cn-stage', root), $('.cn-pass', root));
  paintKeyStep();
}
function paintKeyStep() {
  const s = SVC[C.which], k = C.st.keys[C.which], res = C.res, ok = !!(res && res.ok), pass = $('.cn-pass', root), o = res ? res.outcome : 'unchecked';
  R('wiz').innerHTML = wiz(['Paste', 'Check', 'Connected'], C.kstep === 'paste' ? 1 : ok && C.kstep === 'home' ? 4 : 2);
  R('mask').textContent = k.set && C.kstep !== 'paste' ? '•••• •••• ' + (k.hint || '••••') : '•••• •••• ••••';
  const set = (st, light, h1, lead, html) => { pass.dataset.st = st; R('light').textContent = light; $('.cn-light .dot', root).className = 'dot' + (st === 'ok' ? '' : st === 'bad' || st === 'checking' ? ' warn' : ' off'); R('h1').innerHTML = h1; R('lead').textContent = lead; R('panel').innerHTML = html; };
  if (C.kstep === 'paste') {
    set('empty', `Waiting for your ${s.noun}`, s.title, s.where, `<form class="cn-form" data-form="key"><div class="field"><label class="eyebrow" for="cnSecret">Your ${s.noun}</label>
      <div class="cn-inp"><input type="password" id="cnSecret" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${s.ph}" aria-describedby="cnErr"><button type="button" class="cn-eye" data-do="eye" aria-pressed="false">Show</button></div><p class="cn-err" id="cnErr" role="alert" hidden></p></div>
      <p class="note">${ic('cnLock')}<span>It stays on this machine, in the project's private credentials file. This page never gets it back: only its last four characters, so you can tell which one it is.</span></p>
      <div class="actions">${btn(k.set ? 'khome' : 'go-hub', 'Back', 'btn-ghost btn-lg')}<button type="submit" class="btn btn-primary btn-lg">Save and check</button></div></form>`);
    const inp = $('#cnSecret'); inp.oninput = () => { R('mask').textContent = inp.value ? '•'.repeat(Math.min(12, Math.ceil(inp.value.length / 4))).replace(/(.{4})(?=.)/g, '$1 ') : '•••• •••• ••••'; pass.dataset.st = inp.value ? 'typing' : 'empty'; $('#cnErr').hidden = true; };
    if (!matchMedia('(max-width:760px)').matches) inp.focus({ preventScroll: true });
  } else if (C.kstep === 'check') {
    set('checking', 'Checking', `Checking your <span class="gt">${s.noun}</span>`, 'A check costs nothing and sends no message. It only asks whether the ' + s.noun + ' is known.', `<ol class="steps cn-steps">${stepRow({ name: 'Saved on this machine', state: 'done', detail: "In the project's private credentials file." })}${stepRow({ name: 'Asking ' + s.name, state: 'run', detail: 'One moment.' })}</ol>`);
  } else if (C.confirm === 'key') {
    set(ok ? 'ok' : 'saved', 'Saved', `Remove this <span class="gt">${s.noun}</span>?`, `It is taken out of this machine's credentials file. ${s.gone}`, `<div class="actions">${btn('kcancel', 'Keep it', 'btn-ghost btn-lg')}${btn('kremove', 'Yes, remove it', 'btn-primary btn-lg')}</div>`);
  } else {
    const acts = o === 'works' ? btn('go-hub', 'Done', 'btn-primary') + btn('kcheck', 'Check again') : o === 'rejected' ? btn('kpaste', `Paste another ${s.noun}`, 'btn-primary') : o === 'unchecked' ? btn('kcheck', 'Check it now', 'btn-primary') : btn('kcheck', 'Check again', 'btn-primary');
    set(ok ? 'ok' : res ? 'bad' : 'saved', ok ? 'Connected' : res ? 'Needs a look' : 'Saved, not checked', (TITLE[o] || TITLE.failed)(s), res ? (TITLE[o] ? res.advice || res.headline : res.headline + ' ' + (res.advice || '')) : `A ${s.noun} ending in ${k.hint} is saved on this machine. It has not been checked yet.`,
      `<div class="result">${res && res.steps.length ? `<ol class="steps cn-steps">${res.steps.map(stepRow).join('')}</ol>` : ''}${res && res.at ? `<p class="cap-note">Checked ${when(res.at)} · ${s.noun} ending in ${esc(k.hint)}${ok ? '. Runs you start from here use it at once; a bot or lane that is already running picks it up when it is started again.' : ''}</p>` : ''}
      <div class="row cn-acts">${acts}${o === 'rejected' ? '' : btn('kpaste', `Replace the ${s.noun}`)}${btn('kask', 'Remove')}</div></div>`);
  }
}
async function saveKey(form) {
  const inp = $('#cnSecret'), err = $('#cnErr'), go1 = $('button[type=submit]', form); go1.disabled = true;
  try { C.st.keys[C.which] = await api.post('/api/connect/keys/' + C.which, { value: inp.value }); }
  catch (e) { err.textContent = e.message; err.hidden = false; go1.disabled = false; inp.focus(); return; }
  inp.value = ''; CW.loadOverview(); runCheck();
}
async function runCheck() {
  C.kstep = 'check'; C.res = null; C.confirm = ''; paintKeyStep();
  const t0 = Date.now(); let res;
  try { res = await api.post(`/api/connect/keys/${C.which}/test`); } catch (e) { res = { ok: false, outcome: 'failed', headline: e.message, advice: '', steps: [] }; }
  await wait(Math.max(0, 900 - (Date.now() - t0)));
  if (C.scr !== 'key' || C.kstep !== 'check') return;
  const list = $('.cn-steps', root); list.lastElementChild.remove();  // "Saved" stays ticked, the rest tick off one by one
  for (const st of res.steps.slice(1)) { list.insertAdjacentHTML('beforeend', stepRow({ ...st, state: 'run' })); await wait(480); if (!list.isConnected) return; list.lastElementChild.outerHTML = stepRow(st); }
  await wait(260); C.st.keys[C.which].check = res.steps.length ? res : null; C.res = res; C.kstep = 'home'; if (C.scr === 'key') paintKeyStep();
}

/* ---------- YouTube: the accounts ---------- */
const busyId = () => C.job && C.job.running ? C.job.account : null;
function acctCard(a) {
  const busy = busyId(), mine = busy === a.id, off = busy && !mine ? ' disabled' : '', ready = a.state === 'ready';
  const acts = mine ? `<span class="badge warn shim"><i class="dot warn"></i>Chrome is open: checking</span>${btn('cancel', 'Cancel', 'btn-ghost btn-sm')}`
    : C.confirm === a.id ? `<span class="cn-ask">Remove ${esc(nameOf(a))}? Its sign-in on this machine is deleted.</span>${btn('rm-no', 'Keep it', 'btn-ghost btn-sm')}${btn('rm-yes', 'Yes, remove', 'btn-primary btn-sm', ` data-id="${a.id}"`)}`
    : (ready ? btn('check', 'Check', 'btn-glass btn-sm', ` data-id="${a.id}"${off}`) + btn('signin', 'Sign in again', 'btn-glass btn-sm', ` data-id="${a.id}"${off}`) : btn('signin', 'Sign in again', 'btn-primary btn-sm', ` data-id="${a.id}"${off}`))
      + (a.posting || !ready ? '' : btn('posting', 'Make this the one that posts', 'btn-glass btn-sm', ` data-id="${a.id}"${off}`)) + btn('rm-ask', 'Remove', 'btn-ghost btn-sm', ` data-id="${a.id}"${off}`);
  return `<article class="card cn-acct${a.posting ? ' posts' : ''}" data-id="${a.id}">${pic(a, 'lg')}
    <div class="cn-who"><b>${esc(nameOf(a))}</b><small>${[a.handle, subs(a)].filter(Boolean).map(esc).join(' · ') || 'Channel ' + esc(a.channel_id || '')}</small>
      <span class="row">${a.posting ? `<span class="pill"><span>${ic('cnPost')} Posts the Shorts</span></span>` : ''}${ready ? badge('on', 'Ready to post') : a.state === 'signin' ? badge('warn', 'Needs signing in') : badge('off', 'Not checked yet')}</span>
      <small class="cap-note">${a.added ? 'Added ' + when(a.added) : ''}${a.checked ? ' · checked ' + when(a.checked) : ''}</small></div>
    <div class="row cn-acts">${acts}</div></article>`;
}
function paintYt() {
  const y = C.st.youtube, busy = busyId(), n = y.accounts.length;
  const notes = [y.mode !== 'browser' ? 'This machine is set to upload through YouTube\'s own API, not through Chrome, so the accounts here are not the ones it posts from.' : '', y.pinned ? 'This machine names one Chrome profile for uploads itself (YT_BROWSER_PROFILE). That wins over the list below.' : '', C.st.dry_run ? 'Dry run is on: approving a clip uploads nothing yet. It is switched off on the machine (DRY_RUN=0), not here.' : ''].filter(Boolean);
  const empty = `<div class="card empty cn-empty"><div class="cn-mini"><div class="rings"><i></i><i></i></div><span class="tile pf-youtube">${ic('youtube')}</span></div><b>No account yet</b><span>Sign in once, in a Chrome window that opens just for this. After that, every Short your team approves goes to your channel.</span>${btn('add', ic('cnPlus') + 'Add an account', 'btn-primary btn-lg')}</div>`;
  const old = y.legacy ? `<article class="card cn-acct cn-old"><span class="cn-pic lg"><i>${ic('cnUser')}</i></span><div class="cn-who"><b>A sign-in from before</b><small>This machine already has the Chrome sign-in the bot used until now. Bring it into the list to see its channel here and keep using it.</small></div><div class="row cn-acts">${btn('adopt', 'Bring it in', 'btn-primary btn-sm', busy ? ' disabled' : '')}</div></article>` : '';
  root.innerHTML = `<div class="stepbox">${backTo('Connect', 'hub')}
    <div class="vhead"><div><h1 data-a>Your <span class="gt">YouTube</span> accounts</h1><p class="lead" data-a>A Short goes up when one of you approves it in Telegram, from the account that posts. This app never uploads anything by itself.</p></div>${n ? `<button type="button" class="btn btn-primary btn-lg" data-do="add" data-a${busy ? ' disabled' : ''}>${ic('cnPlus')}Add an account</button>` : ''}</div>
    <div class="cn-accts" data-a>${y.accounts.map(acctCard).join('')}${old}${n ? '' : empty}</div>
    ${notes.map(t => `<p class="note" data-a><i class="dot warn"></i><span>${esc(t)}</span></p>`).join('')}</div>`;
}

/* ---------- YouTube: adding an account, step by step ---------- */
const WALK = ['Open Chrome', 'Sign in', 'Your channel?', 'Ready to post'];
const ENDS = {
  closed: ['The Chrome window was <span class="gt">closed</span>', 'The sign-in was not finished, so nothing was saved.', 'Open Chrome again'],
  cancelled: ['<span class="gt">Cancelled</span>', 'Chrome was closed and nothing was saved.', 'Start again'],
  timeout: ['Time <span class="gt">ran out</span>', 'Ten minutes passed without a sign-in, so Chrome was closed. Nothing was saved.', 'Try again'],
  failed: ['Chrome did <span class="gt">not get there</span>', 'Something stopped the sign-in before it was finished.', 'Try again'],
  signed_out: ['This sign-in has <span class="gt">run out</span>', 'Google asks for the password again before this account can post.', 'Sign in again'],
  duplicate: ['This channel is <span class="gt">already here</span>', 'It is in your list already, so nothing was added.', 'Back to the accounts'],
};
function paintAdd() {
  root.innerHTML = `<div class="stepbox">${backTo('YouTube accounts', 'yt')}<div data-r="wiz" data-a></div>
    <div class="runwrap cn-wrap"><div class="card cn-stagecard" data-a><div class="stage3d cn-stage cn-ytstage"><div class="rings"><i></i><i></i><i></i></div>
      <span class="opill cn-o1" data-o="win">${ic('cnWin')}<span>Its own Chrome window</span></span><span class="opill cn-o2" data-o="lock">${ic('cnLock', 'pf-telegram')}<span>Password stays with Google</span></span><span class="opill cn-o3" data-o="yt">${ic('youtube', 'pf-youtube')}<span>YouTube Studio</span></span>
      <div class="cn-win float" data-ph="intro"><div class="cn-bar"><i></i><i></i><i></i><span class="cn-url">${ic('cnLock')}<span data-r="url">studio.youtube.com</span></span></div>
        <div class="cn-body"><span class="cn-logo pf-youtube">${ic('youtube')}</span>
          <div class="cn-signin"><span class="cn-face">${ic('cnUser')}</span><i></i><i></i><b></b></div>
          <div class="cn-load"><i></i><b></b><b></b></div><div class="cn-chan" data-r="chan"></div><span class="cn-scan"></span></div></div></div></div>
      <div class="panel cn-panel" data-a data-r="panel" aria-live="polite"></div></div></div>`;
  phoneTilt($('.cn-stage', root), $('.cn-win', root)); C.sig = ''; paintWalk();
}
function paintWalk() {
  const j = C.job || {}, ph = C.astep === 'live' ? j.phase : C.astep, ch = j.channel || C.acct || {}, check = j.mode === 'check';
  const sig = [C.astep, ph, j.note || '', C.closing ? 1 : 0].join('|');
  if (sig === C.sig) { const t = R('left'); if (t && j.left_s != null) t.textContent = clock(j.left_s) + ' left'; return; }
  C.sig = sig;
  const win = $('.cn-win', root), busy = ['signed_in', 'reading', 'closing'].includes(ph), end = ENDS[ph];
  const step = ph === 'intro' || ph === 'opening' ? 1 : ph === 'waiting' ? 2 : busy || ph === 'done' || ph === 'confirm' ? 3 : ph === 'ready' ? 5 : end ? (ph === 'duplicate' ? 3 : 2) : 1;
  R('wiz').innerHTML = wiz(WALK, step);
  win.dataset.ph = end ? 'bad' : busy ? 'reading' : ph === 'confirm' || ph === 'ready' ? 'done' : ph;
  R('url').textContent = ph === 'waiting' && !check ? 'accounts.google.com' : 'studio.youtube.com';
  $$('.opill', root).forEach(o => o.classList.toggle('on', o.dataset.o === 'win' ? ph !== 'intro' && !end : o.dataset.o === 'lock' ? ph === 'waiting' || busy : busy || ph === 'confirm' || ph === 'ready'));
  if (ph === 'confirm' || ph === 'ready' || ph === 'duplicate') R('chan').innerHTML = `${pic(ch, 'xl')}<b>${esc(nameOf(ch))}</b><small>${esc(ch.handle || '')}</small>`;
  const cancel = btn('cancel', C.closing ? 'Closing Chrome…' : 'Cancel', 'btn-ghost btn-lg', C.closing ? ' disabled' : ''), head = (h, p, more = '') => `<div class="cn-say"><h2>${h}</h2><p class="lead">${p}</p>${more}</div>`;
  const facts = list => `<ul class="cn-facts">${list.map(([i, t]) => `<li><span class="tile">${ic(i)}</span><span>${t}</span></li>`).join('')}</ul>`;
  let html;
  if (ph === 'intro') html = head(C.kind === 'signin' ? `Sign <span class="gt">${esc(nameOf(C.acct || {}))}</span> in again` : 'Add a <span class="gt">YouTube account</span>', 'Four short steps. You do the signing in, in Chrome, as you always do.',
    facts([['cnWin', 'Chrome opens a window of its own, with a profile kept only for this account.'], ['cnLock', 'You sign in to Google there. Your password never passes through this app.'], ['youtube', 'When YouTube Studio shows your channel, Chrome closes by itself and the sign-in is kept on this machine.']])) + `<div class="actions">${btn('go-yt', 'Back', 'btn-ghost btn-lg')}${btn('start', 'Open Chrome', 'btn-primary btn-lg')}</div>`;
  else if (ph === 'opening') html = head('Opening <span class="gt">Chrome</span>', 'A new Chrome window is on its way. It may open behind this one.', '<div class="total ind"><i></i></div>') + `<div class="actions"><span></span>${cancel}</div>`;
  else if (ph === 'waiting') html = (check ? head('Looking at <span class="gt">YouTube Studio</span>', 'Chrome has opened YouTube Studio with this sign-in. It closes by itself in a moment.', '<div class="total ind"><i></i></div>')
    : head('Sign in, in the <span class="gt">Chrome window</span>', 'Pick the Google account that owns your channel and sign in. This page waits and notices by itself when you are through.', `<div class="cn-waitrow"><span class="cn-pulse"><i></i><i></i><i></i></span><span>Waiting for you</span><span class="num cn-left" data-r="left">${j.left_s != null ? clock(j.left_s) + ' left' : ''}</span></div>${j.note === 'no_channel' ? '<p class="note"><i class="dot warn"></i><span>You are signed in, but this Google account has no YouTube channel yet. Create one in the Chrome window, or switch to another account there.</span></p>' : ''}`)) + `<div class="actions"><span></span>${cancel}</div>`;
  else if (busy) html = head('Signed in. <span class="gt">Reading your channel</span>', 'Chrome is fetching the name and picture of your channel, then it closes by itself so the sign-in is saved.', '<div class="total ind"><i></i></div>') + `<div class="actions"><span></span>${cancel}</div>`;
  else if (ph === 'confirm') html = head('Is this <span class="gt">your channel</span>?', `Chrome has closed and the sign-in is kept on this machine.`, `<div class="cn-found">${pic(ch, 'lg')}<span class="cn-who"><b>${esc(nameOf(ch))}</b><small>${[ch.handle, subs(ch)].filter(Boolean).map(esc).join(' · ') || 'Channel ' + esc(ch.channel_id || '')}</small></span></div>`) + `<div class="actions">${btn('wrong', 'No, another account', 'btn-ghost btn-lg')}${btn('right', 'Yes, this is it', 'btn-primary btn-lg')}</div>`;
  else if (ph === 'ready') { const posts = !!ch.posting; html = head('<span class="gt">Ready</span> to post', `When one of you approves a clip in Telegram, the Short is uploaded to ${esc(nameOf(ch))} through this sign-in. This app never uploads by itself.`,
    facts([[posts ? 'cnPost' : 'cnUser', posts ? `${esc(nameOf(ch))} is the account that posts.` : 'Another account posts right now. You can switch to this one below, or any time from the list.'], ...(C.st.dry_run ? [['cnLock', 'Dry run is on, so nothing is uploaded yet. It is switched off on the machine (DRY_RUN=0), not here.']] : [])])) + `<div class="actions">${posts ? '<span></span>' : btn('posting', 'Make this the one that posts', 'btn-glass btn-lg', ` data-id="${esc(ch.id)}"`)}${btn('finish', 'Done', 'btn-primary btn-lg')}</div>`; }
  else { const [h, p, b] = end || ENDS.failed, again = ph === 'duplicate' ? btn('finish', b, 'btn-primary btn-lg') : btn(ph === 'signed_out' && C.kind === 'adopt' ? 'fresh' : 'start', ph === 'signed_out' && C.kind === 'adopt' ? 'Sign in fresh' : b, 'btn-primary btn-lg');
    html = head(h, ph === 'failed' && j.message ? esc(j.message) : ph === 'duplicate' ? `${esc(nameOf(ch))} is in your list already, so nothing was added.` : p) + `<div class="actions"><span></span>${again}</div>`; }
  R('panel').innerHTML = html;
}
function follow() { clearInterval(C.poll); C.poll = setInterval(tick, 1000); }
async function tick() {
  if (location.hash.slice(1) !== 'connect') return;
  let j; try { j = await api.get('/api/connect/youtube/status'); } catch (e) { return; }
  C.job = j; if (j.running) return C.scr === 'add' ? paintWalk() : null;
  clearInterval(C.poll); C.poll = 0; C.closing = false; await ended(j);
}
async function ended(j) {
  await load().catch(() => {}); C.job = j;
  if (C.scr === 'add' && C.astep === 'live') { if (j.phase === 'done') { C.acct = j.channel; C.astep = j.new ? 'confirm' : 'ready'; } return paintWalk(); }
  api.post('/api/connect/youtube/seen').catch(() => {}); C.job = C.st.youtube.login = { phase: 'idle', running: false };
  toast(j.phase === 'done' ? `${nameOf(j.channel || {})} is signed in and ready to post` : j.phase === 'signed_out' ? 'This account is signed out. Sign in again to post from it.' : j.phase === 'cancelled' ? 'Cancelled. Chrome was closed.' : j.phase === 'closed' ? 'The Chrome window was closed before the check was through.' : j.message || 'The check did not finish. Try again.');
  if (C.scr === 'yt') paint();
}
async function begin(kind, id) {
  C.kind = kind; C.closing = false;
  const j = await api.post(kind === 'add' ? '/api/connect/youtube/add' : kind === 'adopt' ? '/api/connect/youtube/adopt' : `/api/connect/youtube/${id}/${kind}`);
  C.job = j; follow(); return j;
}
const seen = () => { api.post('/api/connect/youtube/seen').catch(() => {}); C.job = { phase: 'idle', running: false }; if (C.st) C.st.youtube.login = C.job; };

/* ---------- what a press does ---------- */
const acct = id => C.st.youtube.accounts.find(a => a.id === id);
const DO = {
  go: b => { if (b.dataset.to === 'yt' && C.scr === 'add') return DO.finish(); go(b.dataset.to); },
  'go-hub': () => go('hub'), 'go-yt': () => go('yt'),
  open: b => b.dataset.id === 'youtube' ? go('yt') : openKey(b.dataset.id),
  eye: b => { const inp = $('#cnSecret'), on = inp.type === 'password'; inp.type = on ? 'text' : 'password'; b.setAttribute('aria-pressed', on); b.textContent = on ? 'Hide' : 'Show'; inp.focus(); },
  kpaste: () => { C.kstep = 'paste'; C.confirm = ''; paintKeyStep(); }, khome: () => { C.kstep = 'home'; paintKeyStep(); }, kcheck: () => runCheck(),
  kask: () => { C.confirm = 'key'; paintKeyStep(); }, kcancel: () => { C.confirm = ''; paintKeyStep(); },
  kremove: async () => { C.st.keys[C.which] = await api.post(`/api/connect/keys/${C.which}/remove`); CW.loadOverview(); toast(`The ${SVC[C.which].noun} was removed from this machine`); go('hub'); },
  add: () => { C.kind = 'add'; C.acct = null; C.astep = 'intro'; C.job = { phase: 'idle' }; go('add'); },
  signin: b => { C.kind = 'signin'; C.acct = acct(b.dataset.id); C.astep = 'intro'; C.job = { phase: 'idle' }; go('add'); },
  adopt: async () => { C.acct = null; C.astep = 'live'; await begin('adopt'); go('add'); },
  start: async b => { b.disabled = true; C.astep = 'live'; C.sig = ''; try { await begin(C.kind === 'signin' ? 'signin' : 'add', C.acct && C.acct.id); } catch (e) { C.astep = 'intro'; C.sig = ''; paintWalk(); throw e; } paintWalk(); },
  fresh: () => { seen(); C.kind = 'add'; return DO.start({}); },
  cancel: async b => { b.disabled = true; C.closing = true; if (C.scr === 'add') paintWalk(); else b.textContent = 'Closing Chrome…'; const j = await api.post('/api/connect/youtube/cancel'); if (!j.running) { clearInterval(C.poll); C.poll = 0; C.closing = false; await ended(j); } },
  right: () => { C.astep = 'ready'; paintWalk(); },
  wrong: async () => { const id = C.acct && C.acct.id; seen(); if (id) await api.post(`/api/connect/youtube/${id}/remove`); await load(); C.kind = 'add'; C.acct = null; C.astep = 'intro'; C.job = { phase: 'idle' }; C.sig = ''; paintWalk(); toast('That sign-in was removed. Open Chrome to sign in with the right account.'); },
  finish: async () => { if (C.job && C.job.running) { await api.post('/api/connect/youtube/cancel'); clearInterval(C.poll); C.poll = 0; } seen(); await load(); go('yt'); },
  check: async b => { b.disabled = true; await begin('check', b.dataset.id); paint(); },
  posting: async b => { C.st.youtube = { ...C.st.youtube, ...(await api.post(`/api/connect/youtube/${b.dataset.id}/posting`)) }; C.job = C.st.youtube.login; if (C.scr === 'add') { C.acct = { ...C.acct, posting: true }; if (C.job.channel) C.job.channel.posting = true; C.sig = ''; paintWalk(); } else paint(); toast(`${nameOf(acct(b.dataset.id) || {})} is now the account that posts`); },
  'rm-ask': b => { C.confirm = b.dataset.id; paint(); }, 'rm-no': () => { C.confirm = ''; paint(); },
  'rm-yes': async b => { const a = acct(b.dataset.id); C.st.youtube = { ...C.st.youtube, ...(await api.post(`/api/connect/youtube/${b.dataset.id}/remove`)) }; C.confirm = ''; paint(); toast(`${nameOf(a || {})} was removed from this machine`); },
};
root.addEventListener('click', e => { const b = e.target.closest('[data-do]'); if (!b || b.disabled) return; const f = DO[b.dataset.do]; if (f) Promise.resolve().then(() => f(b)).catch(err => { toast(err.message); if (b.isConnected) b.disabled = false; }); });
root.addEventListener('submit', e => { e.preventDefault(); if (e.target.dataset.form === 'key') saveKey(e.target); });

CW.views.connect = {
  async show() {
    try { await load(); } catch (e) { root.innerHTML = `<div class="card empty"><b>Connect could not be loaded</b><span>${esc(e.message)}</span></div>`; return; }
    const j = C.job;
    if (j.running) { follow(); if (j.mode === 'check' && !j.new) C.scr = 'yt'; else { C.scr = 'add'; C.astep = 'live'; C.kind = j.mode === 'check' ? 'adopt' : j.new ? 'add' : 'signin'; } }
    else { if (j.phase !== 'idle') seen(); if (C.scr === 'add' && C.astep === 'live') C.scr = 'yt'; if (C.scr === 'key' && C.kstep === 'check') C.kstep = 'home'; }
    paint();
  },
};
})();
