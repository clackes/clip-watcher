/* The Library place of the companion app. Builds on window.CW and window.CWcaps (app.js).
   Home: one template on the stage, the shelf of them all under it. "Create your own" walks four steps
   (start from a template, make it yours, name it, saved); a template that exists opens "make it yours"
   directly. What is kept goes to the server (/api/library/...), which checks it and writes this machine's
   config; after every change the overview is asked for again, so the Run walk and a clip's own settings
   show the same shelf. */
(() => {
const { $, $$, api, ic, esc, toast, stagger, reduce, S, capLib, capOf, loadOverview } = CW;
const { capFields, previewOf, capForget, useCaptions, capSaved, sameV } = CWcaps;
const view = $('#v-library'), STEPS = ['Start from', 'Make it yours', 'Name it', 'Saved'];
/* screen: home | make (the four steps) | edit (a template that exists). target: the style being edited;
   base: the style a new one starts from; from: the edit a "save as new" came out of; set: what the
   settings hold that is not in force yet. */
const L = { screen: 'home', step: 0, pane: null, shelf: null, picker: null, target: null, base: null, from: null, fields: null, set: {}, group: null, ed: null, made: null, ask: null, renaming: false, err: '', warns: [], seq: 0, later: 0, pending: null, again: false };

view.innerHTML = `
<div class="libbox" id="libHome">
  <div class="vhead"><div><h1 data-a>Your caption <span class="gt">library</span></h1><p class="lead" data-a id="libLead">Opening the library…</p></div><button type="button" class="btn btn-primary btn-lg" id="libNew" data-a disabled>Create your own</button></div>
  <p class="note" id="libNeeds" data-a hidden></p>
  <div id="libShelf" data-a></div>
</div>
<div class="libbox" id="libMake" hidden>
  <ol class="wiz" id="libWiz" aria-label="Steps to a template of your own"></ol>
  <div class="libbox" id="lm1" hidden>
    <div class="vhead"><div><h1 data-a>Start from a <span class="gt">template</span></h1><p class="lead" data-a>Pick the one closest to what you have in mind. In the next step you can change anything about it.</p></div></div>
    <div id="lmPick" data-a></div>
    <div class="actions" data-a><button type="button" class="btn btn-ghost btn-lg" data-b="home">Back</button><button type="button" class="btn btn-primary btn-lg" data-b="next1">Continue</button></div>
  </div>
  <div class="libbox" id="lm2" hidden>
    <div class="vhead"><div id="lmHead" data-a></div><div class="row" data-a id="lmHeadActs"></div></div>
    <div class="libed" data-a>
      <div class="libslot" data-slot="2"></div>
      <div class="libside">
        <div class="row libstate"><span class="badge" id="lmCount"></span><button type="button" class="btn btn-glass btn-sm" data-b="undo">Undo my changes</button><button type="button" class="btn btn-glass btn-sm" data-b="came">As the template came</button></div>
        <p class="capwarn" id="lmWarn" hidden></p>
        <div class="captune" id="lmRows"></div>
      </div>
    </div>
    <div id="lmBar" data-a></div>
  </div>
  <div class="libbox" id="lm3" hidden>
    <div class="vhead"><div><h1 data-a>Give it a <span class="gt">name</span></h1><p class="lead" data-a>The name it has on the shelf and wherever you pick captions for a clip.</p></div></div>
    <div class="libname" data-a>
      <div class="libslot" data-slot="3"></div>
      <div class="panel libnamebox">
        <div class="field"><label class="eyebrow" for="lmName">Its name</label><input type="text" id="lmName" maxlength="40" autocomplete="off" spellcheck="false"><small class="cap-note" id="lmNameNote"></small></div>
        <p class="capwarn" id="lmNameErr" hidden></p>
        <p class="lead" id="lmNameSum"></p>
      </div>
    </div>
    <div class="actions" data-a><button type="button" class="btn btn-ghost btn-lg" data-b="back3">Back</button><button type="button" class="btn btn-primary btn-lg" data-b="create">Save to my library</button></div>
  </div>
  <div class="libbox" id="lm4" hidden>
    <div class="libname" data-a>
      <div class="libslot" data-slot="4"></div>
      <div class="result" id="lmDone"></div>
    </div>
  </div>
</div>`;

/* ---------- the phone the editor plays on: one, moved to the step that is open ---------- */
const phone = document.createElement('div'); phone.className = 'capphone';
phone.innerHTML = '<video muted loop playsinline preload="auto" aria-label="How the captions move"></video><div class="capwait" hidden><i></i><span></span></div>';
const video = $('video', phone), wait = $('.capwait', phone);
function showClip(url) { if (video.dataset.url !== url) { video.dataset.url = url; video.src = url; } if (!reduce) video.play().catch(() => {}); }
const wholeOf = () => { if (L.ed) return L.ed.whole(); const out = {}; if (L.fields) L.fields.groups.forEach(g => g.fields.forEach(f => { const v = f.path in L.set ? L.set[f.path] : f.value; if (!sameV(v, f.own)) out[f.path] = v; })); return out; };
const dirty = () => Object.keys(L.set).length;
const ownColours = () => Object.keys(wholeOf()).some(k => k.startsWith('colors.'));
/* the look the editor holds now, over the template as it shipped */
async function play() {
  const f = L.fields; if (!f) return; const mine = ++L.seq, row = capOf(L.target || L.base), quick = !dirty() && row && row.preview;
  L.err = ''; L.warns = [];
  if (quick) { showClip(row.preview); wait.hidden = true; }
  else { wait.hidden = false; wait.lastElementChild.textContent = video.dataset.url ? 'Rendering your change' : 'Rendering the preview'; }
  try { const r = await previewOf(f.base, wholeOf(), true); if (mine !== L.seq) return; showClip(r.url); L.warns = r.warnings || []; }
  catch (err) { if (mine !== L.seq) return; L.err = err.message; }
  wait.hidden = true; paintState();
}

/* ---------- home: the stage and the shelf ---------- */
const ready = () => !!(S.ov && S.ov.caption_library.ready);
function paintHome() {
  const ov = S.ov; if (!ov) return;
  const all = ov.captions, own = all.filter(c => c.own).length, lb = ov.caption_library;
  $('#libLead').textContent = `${all.length} caption styles, ${own ? (own === 1 ? 'one of them yours' : own + ' of them yours') : 'none of them yours yet'}. Watch one, change it, or make your own. What you keep here stays on this machine, ready to pick for any run or any clip.`;
  const needs = $('#libNeeds'); needs.hidden = lb.ready;
  if (!lb.ready) needs.innerHTML = `<i class="dot warn"></i><span>The animated templates are not set up on this machine yet, so they cannot play or be changed here. The built-in styles work right away.<details class="libhow"><summary>How to set them up</summary>Run this once in the project folder, then open this page again:<br><code class="cmd">${esc(lb.setup || '')}</code></details></span>`;
  $('#libNew').disabled = !lb.ready; $('#libNew').title = lb.ready ? '' : 'The animated templates need their one-time setup first';
  if (L.shelf) L.shelf.refresh(L.again);
  else {
    L.shelf = capLib($('#libShelf'), { page: true, style: capOf(ov.caption_default) ? ov.caption_default : 'word', colors: lb.colors, tune: false,
      filters: ['all', 'own', 'tuned', 'animated', 'built-in'], always: ['own', 'tuned'],
      primary: { label: s => capSaved(s) ? 'In use for new clips' : 'Use for new clips', disabled: capSaved, run: s => useCaptions(false, s) },
      more: (s, c) => c.kind === 'animated' && ready() ? [{ label: c.own ? 'Change it' : 'Make it yours', run: () => edit(c.id) }] : [],
      note: (s, c) => c.kind === 'built-in' ? 'A built-in style comes as it is: there is nothing to set. Every animated template can be changed and kept as your own.' : '',
      empty: k => k === 'own' ? `<span class="tile">${ic('library')}</span><b>Nothing of your own yet</b><span>Start from any animated template, change what you like and give it a name.</span>${ready() ? '<button type="button" class="btn btn-primary" data-b="new">Create your own</button>' : ''}`
        : k === 'tuned' ? `<span class="tile">${ic('setup')}</span><b>Nothing edited yet</b><span>Open an animated template, move a setting and save it: it is kept here as your version of that template.</span>` : '' });
    tiltPhone($('.capphone', L.shelf.root));
  }
  L.again = false;
}
function tiltPhone(el) {
  if (reduce) return;
  el.addEventListener('pointermove', e => { const r = el.getBoundingClientRect(); el.style.setProperty('--ry', (((e.clientX - r.left) / r.width - .5) * 28).toFixed(1) + 'deg'); el.style.setProperty('--rx', ((.5 - (e.clientY - r.top) / r.height) * 14).toFixed(1) + 'deg'); });
  el.addEventListener('pointerleave', () => { el.style.removeProperty('--rx'); el.style.removeProperty('--ry'); });
}
/* a template changed: its clips are asked for again and every shelf on the page is read again */
async function reread() { capForget(); L.again = true; await loadOverview(); }

/* ---------- moving between the screens ---------- */
function paintWiz() {
  $('#libWiz').innerHTML = STEPS.map((l, i) => { const n = i + 1, st = n < L.step ? 'done' : n === L.step ? 'now' : 'todo', can = n < L.step && L.step < 4 && !(n === 1 && L.from);
    return `<li data-st="${st}"><button type="button" data-go="${n}" ${can ? '' : 'disabled'} ${n === L.step ? 'aria-current="step"' : ''}><i>${st === 'done' ? ic('check') : n}</i><span>${l}</span></button></li>`; }).join('');
}
function go(screen, step) {
  const pane = screen === 'home' ? $('#libHome') : $('#lm' + (screen === 'edit' ? 2 : step)), from = L.pane || $('#libHome');
  Object.assign(L, { screen, step: screen === 'make' ? step : 0, ask: null, renaming: false });
  const enter = () => {
    if (from !== pane) { from.hidden = true; from.classList.remove('out'); }
    $('#libMake').hidden = screen === 'home'; $('#libWiz').hidden = screen !== 'make'; pane.hidden = false; L.pane = pane;
    const slot = $('[data-slot]', pane); if (slot) slot.appendChild(phone);
    paintWiz(); ({ home: paintHome, edit: paintEditor, make: [null, paintPick, paintEditor, paintName, paintDone][step] })[screen]();
    stagger(pane); scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
  };
  if (from !== pane && !reduce && !view.hidden) { from.classList.add('out'); setTimeout(enter, 190); } else enter();
}
async function loadFields(style) { L.fields = await api.get('/api/library/fields?style=' + encodeURIComponent(style)); L.set = {}; L.ed = null; L.group = null; }
/* "make it yours" on a template that exists */
async function edit(style) {
  try { await loadFields(style); } catch (err) { return toast(err.message); }
  Object.assign(L, { target: style, base: style, from: null, made: null }); go('edit');
}
/* back from naming a "save as a new one" to the editor it came out of, its changes as they were */
function reopen() { L.target = L.from; L.from = null; go('edit'); }
/* the four steps to a new one */
function startNew() {
  if (!ready()) return toast('The animated templates need their one-time setup on this machine first.');
  const on = L.shelf && L.shelf.cur(), first = S.ov.captions.find(c => c.kind === 'animated');
  Object.assign(L, { target: null, from: null, made: null, fields: null, set: {}, ed: null, base: on && on.kind === 'animated' ? on.id : first && first.id }); go('make', 1);
}

/* step 1: start from a template */
function paintPick() {
  if (L.picker) { L.picker.pick(L.base); L.picker.refresh(); return; }
  L.picker = capLib($('#lmPick'), { style: L.base, tune: false, colours: false, primary: null, only: c => c.kind === 'animated', filters: ['all', 'own'], onChange: s => { L.base = s.style; } });
}

/* step 2, and the editor of a template that exists */
function paintHead() {
  const f = L.fields, editing = L.screen === 'edit';
  if (!editing) { $('#lmHead').innerHTML = `<h1>Make it <span class="gt">yours</span></h1><p class="lead">You started from ${esc(f.name)}. Change anything, one group at a time: every change plays on the phone.</p>`; $('#lmHeadActs').innerHTML = ''; return; }
  if (L.renaming) {
    $('#lmHead').innerHTML = `<span class="eyebrow">A new name</span><div class="librename"><input type="text" id="lmRename" maxlength="40" autocomplete="off" spellcheck="false" value="${esc(f.name)}" aria-label="The template's name"><button type="button" class="btn btn-primary" data-b="rename-ok">Save the name</button><button type="button" class="btn btn-ghost" data-b="rename-no">Cancel</button></div><p class="capwarn" id="lmRenameErr" hidden></p>`;
    $('#lmHeadActs').innerHTML = ''; const inp = $('#lmRename'); inp.focus(); inp.select(); return;
  }
  $('#lmHead').innerHTML = `<span class="eyebrow">${f.own ? 'Yours · made from ' + esc(f.base_name) : 'Comes with the app' + (f.tuned ? ' · with your changes' : '')}${S.ov && S.ov.caption_default === L.target ? ' · in use for new clips' : ''}</span><h1>${esc(f.name)}</h1><p class="lead">${f.own ? 'Change anything, one group at a time. Every change plays on the phone, and nothing is kept until you save.' : `What you save here is your version of ${esc(f.name)} on this machine. Every change plays on the phone, and you can always put it back to how it came.`}</p>`;
  $('#lmHeadActs').innerHTML = `<button type="button" class="btn btn-glass btn-sm" data-b="home">Back to the library</button>` + (f.own ? '<button type="button" class="btn btn-glass btn-sm" data-b="rename">Rename</button><button type="button" class="btn btn-glass btn-sm" data-b="delete">Delete</button>' : f.tuned ? '<button type="button" class="btn btn-glass btn-sm" data-b="reset">Back to how it came</button>' : '');
}
function paintEditor() {
  const f = L.fields; paintHead();
  const old = $('#lmRows'), rows = old.cloneNode(false); old.replaceWith(rows);  // a fresh box: its listeners go with the old one
  L.ed = capFields(rows, { fields: f, settings: L.set, one: true, group: L.group, onGroup: g => { L.group = g; }, onChange: x => { L.set = x.settings(); clearTimeout(L.later); L.later = setTimeout(play, 450); paintState(); } });
  paintState(); play();
}
function paintState() {
  if (!L.fields || !L.ed) return;
  const editing = L.screen === 'edit', n = dirty(), off = Object.keys(wholeOf()).length, f = L.fields, badge = $('#lmCount');
  badge.textContent = editing ? (n ? `${n} ${n === 1 ? 'change' : 'changes'} not saved` : f.own || f.tuned ? 'Everything is saved' : 'As it came') : off ? `${off} ${off === 1 ? 'setting' : 'settings'} changed` : 'Nothing changed yet';
  badge.className = 'badge' + (editing && n ? ' warn' : '');
  $('[data-b="undo"]', view).disabled = !n; $('[data-b="came"]', view).disabled = !off;
  const w = $('#lmWarn'), all = [...(L.err ? [L.err] : []), ...L.warns]; w.hidden = !all.length; w.textContent = all.join(' ');
  paintBar();
}
function paintBar() {
  const bar = $('#lmBar'), editing = L.screen === 'edit', n = dirty(), bad = !!L.err;
  if (L.ask) { bar.className = 'savebar libbar asking'; bar.innerHTML = `<span class="libask">${esc(L.ask.text)}</span><span class="row"><button type="button" class="btn btn-ghost" data-b="ask-no">${esc(L.ask.no)}</button><button type="button" class="btn btn-primary" data-b="ask-yes">${esc(L.ask.yes)}</button></span>`; return; }
  if (!editing) { bar.className = 'actions'; bar.innerHTML = `<button type="button" class="btn btn-ghost btn-lg" data-b="back2">Back</button><button type="button" class="btn btn-primary btn-lg" data-b="next2"${bad ? ' disabled' : ''}>Continue</button>`; return; }
  const inUse = S.ov && S.ov.caption_default === L.target;
  bar.className = 'savebar libbar';
  bar.innerHTML = `<span class="cap-note">${bad ? 'Fix what the note above says, then save.' : n ? 'Your changes play on the phone. They are kept when you save.' : 'Move a setting to change it.'}</span>
    <span class="row"><button type="button" class="btn btn-glass" data-b="use"${bad || (inUse && !n) ? ' disabled' : ''}>${inUse ? (n ? 'Save for new clips' : 'In use for new clips') : n ? 'Save and use for new clips' : 'Use for new clips'}</button>
    <button type="button" class="btn btn-glass" data-b="as-new"${bad ? ' disabled' : ''}>Save as a new one</button>
    <button type="button" class="btn btn-primary" data-b="save"${bad || !n ? ' disabled' : ''}>Save</button></span>`;
}
function ask(text, yes, no, run) { L.ask = { text, yes, no, run }; paintBar(); $('#lmBar').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' }); }
async function save() {
  const f = await api.post('/api/library/save', { style: L.target, settings: wholeOf() });
  L.fields = f; L.set = {}; await reread(); paintEditor();
}
async function use(style, coloured) {
  await useCaptions(true, { style, animated: true, colors: coloured ? 'fixed' : S.ov.caption_library.colors });
  toast(`${capOf(style).name} is the caption style of new clips` + (coloured ? ', with its own colours on every video' : ''));
}

/* step 3: name it */
function paintName() {
  const f = L.fields, inp = $('#lmName'), off = Object.keys(wholeOf()).length;
  inp.placeholder = 'My ' + f.base_name; $('#lmNameErr').hidden = true;
  $('#lmNameSum').textContent = `Made from ${f.base_name}` + (off ? `, with ${off} ${off === 1 ? 'setting' : 'settings'} of your own.` : ', as it came. You can change it any time.');
  paintNameNote(); play(); setTimeout(() => inp.focus({ preventScroll: true }), 260);
}
function paintNameNote() { const n = $('#lmName').value.trim().length; $('#lmNameNote').textContent = n ? `${n} of 40 characters` : 'Up to 40 characters.'; }
async function create() {
  const inp = $('#lmName'), name = inp.value.trim(), err = $('#lmNameErr');
  if (L.creating) return;  // Enter and the button, or Enter twice: one template
  if (!name) { err.hidden = false; err.textContent = 'Give your template a name.'; return inp.focus(); }
  L.creating = true;
  try { L.made = await api.post('/api/library/create', { from: L.base, name, settings: wholeOf() }); }
  catch (e) { L.creating = false; err.hidden = false; err.textContent = e.message; return inp.focus(); }
  try { inp.value = ''; await reread(); L.made = capOf(L.made.id) || L.made; if (L.shelf) L.shelf.pick(L.made.id); go('make', 4); }
  finally { L.creating = false; }
}

/* step 4: saved */
function paintDone() {
  const c = L.made, inUse = S.ov.caption_default === c.id;
  $('#lmDone').innerHTML = `<span class="badge live"><i class="dot"></i>Saved on this machine</span><h2>${esc(c.name)} is on your shelf</h2>
    <p class="lead">Pick it in the Captions step of any run, or give it to one clip from that clip's own settings. Change it whenever you like: it is under Yours in the library.</p>
    <div class="row" style="margin-top:4px"><button type="button" class="btn btn-primary btn-lg" data-b="use-made"${inUse ? ' disabled' : ''}>${inUse ? 'In use for new clips' : 'Use for new clips'}</button><button type="button" class="btn btn-glass btn-lg" data-b="home">Back to the library</button><button type="button" class="btn btn-ghost btn-lg" data-b="new">Create another</button></div>`;
  if (c.preview) showClip(c.preview);
}

/* ---------- what is pressed ---------- */
const DO = {
  new: startNew,
  home() { if (L.screen === 'edit' && dirty() && !L.ask) return ask(`Leave without saving your ${dirty() === 1 ? 'change' : dirty() + ' changes'}?`, 'Leave', 'Stay', () => go('home')); go('home'); },
  async next1() { if (!L.base) return toast('Pick a template to start from'); if (!L.fields || L.fields.style !== L.base) await loadFields(L.base); go('make', 2); },
  back2() { go('make', 1); },
  next2() { go('make', 3); },
  back3() { if (L.from) reopen(); else go('make', 2); },
  create,
  undo() { L.ed.undo(); }, came() { L.ed.asCame(); },
  async save() { await save(); toast('Saved'); },
  async use() { const style = L.target, coloured = ownColours(); if (dirty()) await save(); await use(style, coloured); paintEditor(); },
  'as-new'() { L.from = L.target; L.base = L.target; L.target = null; go('make', 3); },
  rename() { L.renaming = true; paintHead(); },
  'rename-no'() { L.renaming = false; paintHead(); },
  async 'rename-ok'() {
    const err = $('#lmRenameErr'); let row;
    try { row = await api.post('/api/library/rename', { style: L.target, name: $('#lmRename').value }); } catch (e) { err.hidden = false; err.textContent = e.message; return; }
    L.fields.name = row.name; L.renaming = false; await reread(); paintHead(); toast('It is called ' + row.name + ' now');
  },
  delete() {
    const f = L.fields, inUse = S.ov.caption_default === L.target;
    ask(`Delete ${f.name}? It leaves your library for good. Clips already made with it keep their video.` + (inUse ? ` New clips use it now: they will go back to ${f.base_name}.` : ''), 'Yes, delete it', 'Keep it', async () => {
      const r = await api.post('/api/library/delete', { style: L.target }); await reread(); go('home');
      toast(`${f.name} is deleted.` + (r.moved ? ` New clips use ${f.base_name} again.` : ''));
    });
  },
  reset() {
    const f = L.fields;
    ask(`Put ${f.name} back to how it came? Your changes to it on this machine are dropped.`, 'Yes, put it back', 'Keep my changes', async () => {
      L.fields = await api.post('/api/library/reset', { style: L.target }); L.set = {}; await reread(); paintEditor(); toast(`${f.name} is back to how it came`);
    });
  },
  'ask-no'() { L.ask = null; paintBar(); },
  async 'ask-yes'() { const run = L.ask.run; L.ask = null; await run(); if (L.screen !== 'home') paintBar(); },
  async 'use-made'() { await use(L.made.id, !!L.made.colored); paintDone(); },
};
view.addEventListener('click', async e => {
  const w = e.target.closest('#libWiz button:not([disabled])'); if (w) { const n = +w.dataset.go; return n === 2 && L.from ? reopen() : go('make', n); }
  const b = e.target.closest('button[data-b]'); if (!b || !DO[b.dataset.b] || b.disabled) return;
  b.disabled = true; try { await DO[b.dataset.b](); } catch (err) { toast(err.message); }
  if (b.isConnected && !['undo', 'came', 'use', 'save', 'use-made'].includes(b.dataset.b)) b.disabled = false;
});
$('#libNew').dataset.b = 'new';
view.addEventListener('keydown', e => {
  if (e.target.id === 'lmName' && e.key === 'Enter') { e.preventDefault(); create(); }
  if (e.target.id === 'lmRename' && e.key === 'Enter') { e.preventDefault(); DO['rename-ok'](); }
  if (e.target.id === 'lmRename' && e.key === 'Escape') DO['rename-no']();
});
view.addEventListener('input', e => { if (e.target.id === 'lmName') { $('#lmNameErr').hidden = true; paintNameNote(); } });

/* ---------- the place ---------- */
function arrive() {
  const p = L.pending; if (!p || !S.ov) return; L.pending = null;
  const c = capOf(p.style); if (!c) return go('home');
  if (p.edit && c.kind === 'animated' && ready()) return edit(c.id);
  go('home'); if (L.shelf) L.shelf.pick(c.id);
}
let slow = 0;
CW.views.library = {
  show() {
    if (L.pending) return arrive();
    if (L.screen === 'home') paintHome();
    clearTimeout(slow); if (!S.ov) slow = setTimeout(() => { if (!S.ov) { $('#libLead').innerHTML = 'The library could not be opened: the app does not answer. <button type="button" class="btn btn-glass btn-sm" id="libRetry">Try again</button>'; $('#libRetry').onclick = () => { $('#libLead').textContent = 'Opening the library…'; loadOverview(); CW.views.library.show(); }; } }, 4000);
  },
  /* the overview was read again: the shelves follow, and an editor whose template is gone closes */
  overview() {
    paintHome(); if (L.picker && L.screen === 'make' && L.step === 1) L.picker.refresh();
    if (L.screen === 'edit' && !capOf(L.target)) go('home');
    if (L.pending && !view.hidden) arrive();
  },
  /* from elsewhere on the page: the Library on that style, its settings open when it has any */
  open(style, editIt) { L.pending = { style, edit: !!editIt }; if (view.hidden) location.hash = 'library'; else arrive(); },
};
})();
