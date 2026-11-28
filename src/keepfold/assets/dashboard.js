/* Keepfold dashboard, Night Platform. Every note-derived string goes
   in with textContent (or d3 .text); nothing here parses HTML. */
(async () => {
  'use strict';
  const T0 = performance.now();
  // The snapshot (Export snapshot) carries its data; the app's live home fetches it and keeps
  // it current from /api/home/delta.
  const BLOB = document.getElementById('data'), LIVE = !BLOB;
  let KEY = '';
  let cold = null;  // the 90-second check's { root, timer } while its panel is open
  if (LIVE) {  // the app's key, as app.js: from the link once, then this tab's sessionStorage
    KEY = new URLSearchParams(location.search).get('t') || '';
    try { if (KEY) sessionStorage.setItem('keepfold.key', KEY); else KEY = sessionStorage.getItem('keepfold.key') || ''; } catch (e) { /* storage off */ }
    history.replaceState(null, '', '/' + location.hash);
  }
  // GET, or POST with a JSON body; a refused key (401, 403) throws an error marked `key`
  async function api(path, body) {
    const r = await fetch(path, body === undefined ? { headers: { 'X-Keepfold-Token': KEY }, cache: 'no-store' } : {
      method: 'POST', headers: { 'X-Keepfold-Token': KEY, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) {  // the app's own words when it gives them (409: "Wait: Importing is still running.")
      let d = {};
      try { d = await r.json(); } catch (e) { /* no body */ }
      const msg = typeof d.error === 'string' && d.error ? d.error : `The app answered ${r.status}.`;
      throw Object.assign(new Error(msg), { key: r.status === 401 || r.status === 403, status: r.status });
    }
    return r.json();
  }
  function offline(err) {  // the header chip: the app is not answering, or no longer takes this page's key
    const s = document.getElementById('stale');
    s.textContent = err && err.key ? '· This page’s key expired. Open Keepfold from the tray or Start menu.'
      : '· Keepfold is not answering. Reopen it from the tray or Start menu.';
    s.hidden = !err;
  }
  async function fetchHome() {
    // the app builds the home once after it starts (a minute on a big vault): say so, never a blank page
    const s = document.getElementById('stale');
    s.textContent = '· Loading your notes (up to a minute after Keepfold starts)';
    s.hidden = false;
    for (;;) {
      try { const d = await api('/api/home'); offline(null); return d; } catch (e) { offline(e); }
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
  let D = BLOB ? JSON.parse(BLOB.textContent) : await fetchHome();
  const STATES = ['solid', 'learning', 'needs_refresh', 'unverified', 'not_started'];
  const GLYPH = { solid: '●', learning: '◐', needs_refresh: '↻', unverified: '◌', not_started: '○' };
  const LABEL = { solid: 'Solid', learning: 'Learning', needs_refresh: 'Needs refresh',
    unverified: 'Unverified', not_started: 'Not started' };
  const VERDICT = { correct: '✓', partial: '½', wrong: '✗' };
  const OUTCOME = { pass: '✓ pass', fail: '✗ fail', pending: '… pending', taken: '◷ waiting for result', unknown: '? unknown' };
  const GROUPS = [['unchecked', 'Taught, not yet checked'], ['asked', 'Asked, not graded'],
    ['confident_miss', 'Confident miss'], ['track', 'Plan steps'], ['warmup', 'Warm-up'],
    ['refresh', 'Refresh'], ['new', 'Ready to learn']];
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const $ = (id) => document.getElementById(id);
  let TODAY = D.day || D.as_of.slice(0, 10);  // the 04:00 Keepfold day, as `keepfold today`
  let NAME = D.owner && D.owner.name ? D.owner.name : '';
  const SVGNS = d3.namespaces.svg;
  const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');
  const motion = (ms) => (REDUCED.matches ? 0 : ms);  // a d3 transition's length: instant under reduced motion

  /* A segmented control or tab list: when its pressed (or selected) button changes, the highlight (the
     button's ::before, the tabs' underline ::after; dashboard.css) slides in from the button that lost
     it, 200 ms ease-out, transform only. One observer, so no call site changes; off under reduced motion. */
  const GLIDE = '.seg, .viewswitch, .presets, .scrub-span, .tabs';
  const lit = (b) => b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-selected') === 'true';
  new MutationObserver((recs) => {
    if (REDUCED.matches) return;
    const lost = recs.filter((r) => r.oldValue === 'true' && !lit(r.target)).map((r) => r.target);
    for (const r of recs) {
      const b = r.target, g = lit(b) && r.oldValue !== 'true' && b.closest(GLIDE);
      const from = g && lost.find((o) => o !== b && g.contains(o));
      if (!from) continue;  // a multi-select toggle, or a first draw: no glide
      const a = from.getBoundingClientRect(), z = b.getBoundingClientRect();
      if (!a.width || !z.width || !b.animate) continue;
      b.animate([{ transform: `translate(${a.left + a.width / 2 - z.left - z.width / 2}px, ${a.top + a.height / 2 - z.top - z.height / 2}px) `
        + `scale(${a.width / z.width}, ${a.height / z.height})` }, { transform: 'none' }],
      { duration: 200, easing: 'cubic-bezier(.2,.7,.2,1)', pseudoElement: g.matches('.tabs') ? '::after' : '::before' });
    }
  }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['aria-pressed', 'aria-selected'], attributeOldValue: true });

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }
  function svgEl(tag, attrs) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    return e;
  }
  function button(cls, text, onClick, label) {
    const b = el('button', cls, text);
    b.type = 'button';
    if (label) { b.setAttribute('aria-label', label); b.title = label; }
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }
  const isDay = (s) => typeof s === 'string' && /^\d{4}-\d{2}-\d{2}/.test(s);
  const dayNum = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) / 864e5;
  const between = (a, b) => Math.round(dayNum(b) - dayNum(a));
  const fmtDay = (s) => (isDay(s) ? `${+s.slice(8, 10)} ${MON[+s.slice(5, 7) - 1]}` : (s || ''));
  const fmtMonth = (s) => `${MON[+s.slice(5, 7) - 1]} ${s.slice(0, 4)}`;
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const glyph = (state) => el('span', 'glyph g-' + state, GLYPH[state] || '▫');
  const stateWord = (state) => {
    const s = el('span', 'state');
    s.append(glyph(state), ' ' + (LABEL[state] || ''));
    return s;
  };
  function nextText(due) {
    if (!isDay(due)) return 'no refresh scheduled';
    const n = between(TODAY, due);
    return n > 0 ? `refresh in ${plural(n, 'day')}` : n === 0 ? 'refresh due today'
      : `due since ${plural(-n, 'day')}`;
  }
  function greeting() {
    const h = new Date().getHours();
    const g = h >= 5 && h < 12 ? 'Good morning' : h >= 12 && h < 17 ? 'Good afternoon'
      : h >= 17 && h < 22 ? 'Good evening' : 'Late night';
    return NAME ? `${g}, ${NAME}` : g;
  }

  /* ---- dot matrix: an authored 5x7 font drawn as lit dots over the unlit cell ---- */
  const FONT = {
    0: [14, 17, 19, 21, 25, 17, 14], 1: [4, 12, 4, 4, 4, 4, 14], 2: [14, 17, 1, 2, 4, 8, 31],
    3: [31, 2, 4, 2, 1, 17, 14], 4: [2, 6, 10, 18, 31, 2, 2], 5: [31, 16, 30, 1, 1, 17, 14],
    6: [6, 8, 16, 30, 17, 17, 14], 7: [31, 1, 2, 4, 8, 8, 8], 8: [14, 17, 17, 14, 17, 17, 14],
    9: [14, 17, 17, 15, 1, 2, 12], A: [14, 17, 17, 17, 31, 17, 17], B: [30, 17, 17, 30, 17, 17, 30],
    C: [14, 17, 16, 16, 16, 17, 14], D: [28, 18, 17, 17, 17, 18, 28], E: [31, 16, 16, 30, 16, 16, 31],
    F: [31, 16, 16, 30, 16, 16, 16], G: [14, 17, 16, 23, 17, 17, 15], H: [17, 17, 17, 31, 17, 17, 17],
    I: [14, 4, 4, 4, 4, 4, 14], J: [7, 2, 2, 2, 2, 18, 12], K: [17, 18, 20, 24, 20, 18, 17],
    L: [16, 16, 16, 16, 16, 16, 31], M: [17, 27, 21, 21, 17, 17, 17], N: [17, 17, 25, 21, 19, 17, 17],
    O: [14, 17, 17, 17, 17, 17, 14], P: [30, 17, 17, 30, 16, 16, 16], Q: [14, 17, 17, 17, 21, 18, 13],
    R: [30, 17, 17, 30, 20, 18, 17], S: [15, 16, 16, 14, 1, 1, 30], T: [31, 4, 4, 4, 4, 4, 4],
    U: [17, 17, 17, 17, 17, 17, 14], V: [17, 17, 17, 17, 17, 10, 4], W: [17, 17, 17, 21, 21, 21, 10],
    X: [17, 17, 10, 4, 10, 17, 17], Y: [17, 17, 17, 10, 4, 4, 4], Z: [31, 1, 2, 4, 8, 16, 31],
    ':': [0, 12, 12, 0, 12, 12, 0], '.': [0, 0, 0, 0, 0, 12, 12], '-': [0, 0, 0, 31, 0, 0, 0],
    '/': [0, 1, 2, 4, 8, 16, 0], '+': [0, 4, 4, 31, 4, 4, 0], '%': [24, 25, 2, 4, 8, 19, 3],
    ' ': [0, 0, 0, 0, 0, 0, 0],
  };
  const SCRAMBLE = 'ABCDEFGHKMNPRSTXZ0123456789';
  function dmPaths(text, size) {
    const pitch = size + 1, r = size / 2;
    let on = '', off = '';
    [...text].forEach((ch, ci) => {
      const rows = FONT[ch] || FONT[' '];
      for (let y = 0; y < 7; y++) {
        for (let x = 0; x < 5; x++) {
          const cx = (ci * 6 + x) * pitch + pitch / 2, cy = y * pitch + pitch / 2;
          const d = `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${size} 0a${r} ${r} 0 1 0 ${-size} 0`;
          if (rows[y] & (16 >> x)) on += d; else off += d;
        }
      }
    });
    return [on, off];
  }
  // A dot-matrix display of `text` (upper-cased) with `size` px dots.
  function dm(text, size, label) {
    const t = String(text).toUpperCase();
    const pitch = size + 1, w = Math.max(1, t.length * 6 - 1) * pitch, h = 7 * pitch;
    const s = svgEl('svg', { class: 'dm', viewBox: `0 0 ${w} ${h}`, width: w, height: h, role: 'img',
      'aria-label': label || t });
    const [on, off] = dmPaths(t, size);
    s.append(svgEl('path', { class: 'off', d: off }), svgEl('path', { class: 'on', d: on }));
    s.dataset.text = t;
    s.dataset.size = size;
    return s;
  }
  function dmSet(s, text) {  // redraw in place (the split-flap flick)
    const [on, off] = dmPaths(text, +s.dataset.size);
    s.lastChild.setAttribute('d', on);
    s.firstChild.setAttribute('d', off);
  }

  /* ---- icons: one 1.5 px stroke, round caps, currentColor ---- */
  const ICONS = {
    copy: ['M9 9h10v10H9z', 'M5 15V5h10'],
    obsidian: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v5H5V6h5'],
    focus: ['M4 9V4h5', 'M15 4h5v5', 'M20 15v5h-5', 'M9 20H4v-5', 'M12 11a1 1 0 1 0 0.01 0'],
    target: ['M12 3v3', 'M12 18v3', 'M3 12h3', 'M18 12h3', 'M12 7a5 5 0 1 0 0.01 0', 'M12 11a1 1 0 1 0 0.01 0'],
    search: ['M10.5 4a6.5 6.5 0 1 0 0.01 0', 'M15.5 15.5L20 20'],
    sun: ['M12 8a4 4 0 1 0 0.01 0', 'M12 2v2', 'M12 20v2', 'M2 12h2', 'M20 12h2', 'M4.9 4.9l1.4 1.4',
      'M17.7 17.7l1.4 1.4', 'M4.9 19.1l1.4-1.4', 'M17.7 6.3l1.4-1.4'],
    moon: ['M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z'],
    close: ['M6 6l12 12', 'M18 6L6 18'],
    chevron: ['M9 6l6 6-6 6'],
    check: ['M5 12.5l4.5 4.5L19 7.5'],
    help: ['M12 3a9 9 0 1 0 0.01 0', 'M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.7', 'M12 17h.01'],
    gear: ['M12 5.5a6.5 6.5 0 1 0 0.01 0', 'M12 9.5a2.5 2.5 0 1 0 0.01 0', 'M12 3v2.5', 'M12 18.5V21', 'M3 12h2.5',
      'M18.5 12H21', 'M5.6 5.6l1.8 1.8', 'M16.6 16.6l1.8 1.8', 'M5.6 18.4l1.8-1.8', 'M16.6 7.4l1.8-1.8'],
    lock: ['M6 11h12v9H6z', 'M8.5 11V8a3.5 3.5 0 0 1 7 0v3'],
    unlock: ['M6 11h12v9H6z', 'M8.5 11V8a3.5 3.5 0 0 1 6.8-1.2'],
    update: ['M12 4v11', 'M7.5 10.5L12 15l4.5-4.5', 'M5 19h14'],
  };
  function icon(name) {
    const s = svgEl('svg', { class: 'ico', viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    for (const d of ICONS[name]) s.append(svgEl('path', { d }));
    return s;
  }
  function iconBtn(name, label, onClick, cls) {
    const b = button(cls || 'act', null, onClick, label);
    b.append(icon(name));
    return b;
  }

  /* ---- tooltip, toast, copy ---- */
  const tip = $('tip');
  function showTipAt(x, y, parts) {
    tip.replaceChildren(...parts);
    tip.hidden = false;
    tip.style.left = Math.max(8, Math.min(x + 14, window.innerWidth - tip.offsetWidth - 8)) + 'px';
    tip.style.top = Math.min(y + 14, window.innerHeight - tip.offsetHeight - 8) + 'px';
  }
  const showTip = (e, parts) => showTipAt(e.clientX, e.clientY, parts);
  const moveTip = (e) => showTipAt(e.clientX, e.clientY, [...tip.childNodes]);
  const hideTip = () => { tip.hidden = true; };
  const toastEl = $('toast');
  let toastTimer = 0;
  function toast(msg, kind) {  // kind: 'error' (read out as an alert, stays 6 s) or none
    toastEl.textContent = msg;
    toastEl.className = 'toast' + (kind ? ' ' + kind : '');
    toastEl.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, kind === 'error' ? 6000 : 2200);
  }
  // a short confirmation a screen reader hears without a toast (Copied on a labelled button)
  function announce(msg) {
    const a = $('announce');
    if (!a) return;
    a.textContent = msg;
    setTimeout(() => { if (a.textContent === msg) a.textContent = ''; }, 1500);
  }
  function fallbackCopy(text) {
    const ta = el('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }
  // Copy: the button turns to a check (and Copied, when it has words) for 1.5 s, as in the app;
  // an icon-only button also says Copied in the toast. A failure says so in an error toast.
  const copyFaces = new WeakMap(), copyTimers = new WeakMap();
  async function copyText(text, b) {
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; }
    } catch (e) { ok = false; }
    if (!ok) ok = fallbackCopy(text);
    if (!ok) { copyFace(b); toast('Copy failed. Select the text and copy it.', 'error'); return; }
    if (!b) { toast('Copied. Paste it into Claude.'); return; }
    if (!copyFaces.has(b)) copyFaces.set(b, [...b.childNodes]);
    const words = copyFaces.get(b).some((n) => n.textContent.trim());
    clearTimeout(copyTimers.get(b));
    const ck = icon('check');
    ck.classList.add('draw');  // the stroke is drawn (dashboard.css .ico.draw)
    b.replaceChildren(ck, ...(words ? [' Copied'] : []));
    if (words) announce('Copied'); else toast('Copied. Paste it into Claude.');
    copyTimers.set(b, setTimeout(() => copyFace(b), 1500));
  }
  function copyFace(b) {  // the button's own face again
    if (!b || !copyFaces.has(b)) return;
    clearTimeout(copyTimers.get(b));
    b.replaceChildren(...copyFaces.get(b));
    copyFaces.delete(b);
  }
  function copyBtn(prompt, cls) {
    const b = iconBtn('copy', 'Copy: ' + prompt, () => copyText(prompt, b), cls);
    return b;
  }
  // copied text becomes the user's own message, so it carries ids only, never note titles
  const loopPrompt = (id) => `Let's work on the open loop [[${id}]] from Keepfold.`;

  /* ---- pages, lobes, platform codes ---- */
  const PAGES = new Map(D.pages.map((p) => [p.id, p]));
  const GN = new Map(D.graph.nodes.map((n) => [n.id, n]));
  const LOBES = new Map(D.clusters.map((c) => [c.id, c]));
  const WIKI = new Set(['project', 'resource', 'note', 'decision']);
  const KIND_WORD = { concept: 'Concept', domain: 'Domain', area: 'Area', project: 'Project',
    resource: 'Resource', note: 'Note', decision: 'Decision', journal: 'Journal', me: 'About me',
    mistake: 'Mistake', problem: 'Problem', assessment: 'Assessment', application: 'Application',
    track: 'Track', session: 'Session', loop: 'Loop', person: 'Person' };
  const kindWord = (k) => KIND_WORD[k] || k;
  const linkIds = (v) => [].concat(v || []).flatMap((s) => [...String(s).matchAll(/\[\[([^\]|#^]+)/g)])
    .map((m) => m[1].trim());
  const SMALL = new Set(['and', 'of', 'the', 'a', 'an', 'in', 'on', 'for', 'to', 'with']);
  const CODES = new Map();
  function code(title) {  // a station-style code: a short first word, else initials, else 4 letters
    if (!CODES.has(title)) {
      const words = String(title).toUpperCase().split(/[^A-Z0-9]+/).filter(Boolean);
      let c = '';
      if (words.length && words[0].length <= 5) c = words[0];
      else {
        const sig = words.filter((w) => !SMALL.has(w.toLowerCase()));
        c = sig.length > 1 ? sig.map((w) => w[0]).join('').slice(0, 5) : (sig[0] || words[0] || '').slice(0, 4);
      }
      CODES.set(title, c || '—');
    }
    return CODES.get(title);
  }
  function lobeChip(clusterId) {
    const c = LOBES.get(clusterId);
    if (!c) return el('span', 'code', '—');
    const s = el('span', 'code', code(c.title));
    s.title = c.title;
    s.setAttribute('aria-label', 'Lobe: ' + c.title);
    return s;
  }
  const pageLobe = (id) => (PAGES.has(id) ? PAGES.get(id).cluster : null);
  const CHAT = new Set(['Claude', 'Gemini', 'Perplexity']);
  const MEDIA = new Set(['YouTube', 'NotebookLM']);
  function kindGroup(p) {
    if (p.kind === 'concept' || p.kind === 'domain' || p.kind === 'area') return 'concept';
    if (p.kind === 'note' || p.kind === 'project' || p.kind === 'decision' || p.kind === 'person') return p.kind;
    if (p.kind === 'resource') {
      if (CHAT.has(p.source)) return 'chat';
      if (MEDIA.has(p.source) || p.tags.includes('youtube') || p.tags.includes('notebooklm')) return 'youtube';
    }
    return 'other';
  }
  const dateOf = (p) => p.date || p.updated;
  function promptFor(p) {
    if (p.kind === 'project') return `/keepfold project ${p.id}`;
    if (p.kind === 'concept') return `/keepfold learn ${p.id}`;
    if (p.kind === 'loop') return loopPrompt(p.id);
    return `/keepfold about ${p.id}`;
  }

  /* ---- Markdown reader: DOM nodes only ---- */
  let selfId = null;  // the page being drawn: a link to itself is plain text, not a button that does nothing
  function wikilink(id, label) {
    if (!PAGES.has(id)) return el('span', 'wl-missing', (label || id).trim());
    const text = (label || PAGES.get(id).title).trim();
    return id === selfId ? el('span', null, text) : button('wl', text, () => openReader(id));
  }
  // Inline Markdown as DOM nodes: text nodes only, images dropped, [label](url) keeps the label.
  const INLINE = new RegExp(['(`+)([\\s\\S]*?)\\1', '!\\[[^\\]]*\\]\\([^)]*\\)',
    '\\[\\[([^\\]|#]+)(?:#[^\\]|]*)?(?:\\|([^\\]]*))?\\]\\]', '\\[([^\\]]*)\\]\\([^)]*\\)',
    '\\*\\*(.+?)\\*\\*', '(?<!\\w)__(.+?)__(?!\\w)', '\\*(.+?)\\*', '(?<!\\w)_(.+?)_(?!\\w)'].join('|'), 'g');
  function inline(text, into) {
    let i = 0;
    for (const m of text.matchAll(INLINE)) {
      if (m.index > i) into.append(text.slice(i, m.index));
      i = m.index + m[0].length;
      if (m[1]) into.append(el('code', null, m[2]));
      else if (m[3]) into.append(wikilink(m[3].trim(), m[4]));
      else if (m[5] != null) inline(m[5], into);
      else if (m[6] || m[7]) inline(m[6] || m[7], into.appendChild(el('strong')));
      else if (m[8] || m[9]) inline(m[8] || m[9], into.appendChild(el('em')));
    }
    if (i < text.length) into.append(text.slice(i));
    return into;
  }
  // an escaped pipe (\|) is text inside a cell, not a border
  const cells = (l) => l.trim().replace(/^\||(?<!\\)\|$/g, '').split(/(?<!\\)\|/)
    .map((c) => c.trim().replace(/\\\|/g, '|'));
  // Block Markdown: headings, paragraphs, lists, fenced code, blockquotes, tables, rules.
  const QUOTE_DEPTH = 20;  // deeper quotes render as text: untrusted nesting must not blow the stack
  function md(src, depth = 0) {
    const out = document.createDocumentFragment();
    const lines = src.replace(/\r\n?/g, '\n').split('\n');
    let para = [], list = null;
    const flush = () => {
      if (para.length) out.append(inline(para.join(' '), el('p')));
      para = [];
      list = null;
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      let m;
      if ((m = /^\s*(```+|~~~+)/.exec(line))) {
        flush();
        const code = [];
        while (++i < lines.length && !lines[i].trim().startsWith(m[1])) code.push(lines[i]);
        const pre = el('pre');
        pre.append(el('code', null, code.join('\n')));
        out.append(pre);
      } else if (!line.trim()) flush();
      else if ((m = /^(#{1,6})\s+(.*?)\s*#*$/.exec(line))) {
        flush();
        out.append(inline(m[2], el('h' + Math.min(6, m[1].length + 3), 'md-h' + m[1].length)));
      } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.append(el('hr')); }
      else if (line.startsWith('>')) {
        flush();
        const q = [];
        for (; i < lines.length && lines[i].startsWith('>'); i++) q.push(lines[i].replace(/^>\s?/, ''));
        i--;
        const bq = el('blockquote');
        bq.append(depth < QUOTE_DEPTH ? md(q.join('\n'), depth + 1) : el('p', null, q.join('\n')));
        out.append(bq);
      } else if (line.trim().startsWith('|') && /^\s*\|?[\s:|-]*-[\s:|-]*$/.test(lines[i + 1] || '')) {
        flush();
        const tr = el('tr'), thead = el('thead'), tbody = el('tbody'), table = el('table');
        for (const c of cells(line)) tr.append(inline(c, el('th')));
        thead.append(tr);
        for (i += 2; i < lines.length && lines[i].trim().startsWith('|'); i++) {
          const r = el('tr');
          for (const c of cells(lines[i])) r.append(inline(c, el('td')));
          tbody.append(r);
        }
        i--;
        table.append(thead, tbody);
        const wrap = el('div', 'md-table');
        wrap.append(table);
        out.append(wrap);
      } else if ((m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line))) {
        if (para.length) flush();
        const tag = /\d/.test(m[2]) ? 'OL' : 'UL';
        if (!list || list.tagName !== tag) { list = el(tag); out.append(list); }
        const li = el('li');
        const depth = Math.floor(m[1].replace(/\t/g, '  ').length / 2);
        if (depth) li.style.marginLeft = (16 * depth) + 'px';
        let t = m[3];
        const box = /^\[([ xX])\]\s+/.exec(t);
        if (box) { li.append(box[1] === ' ' ? '☐ ' : '☑ '); t = t.slice(box[0].length); }
        list.append(inline(t, li));
      } else if (list && /^\s+\S/.test(line)) inline(' ' + line.trim(), list.lastChild);
      else { list = null; para.push(line.trim()); }
    }
    flush();
    return out;
  }

  let BACK = null;
  function backlinks(id) {
    if (!BACK) {
      BACK = new Map();
      for (const p of D.pages) {
        for (const t of p.links) {
          if (!BACK.has(t)) BACK.set(t, []);
          BACK.get(t).push(p);
        }
      }
    }
    const marks = ['[[' + id + ']]', '[[' + id + '|', '[[' + id + '#'];
    return (BACK.get(id) || []).map((p) => ({ p, line: (p.body.split('\n')
      .find((l) => marks.some((k) => l.includes(k))) || '').replace(/^[\s>#*+-]+/, '').slice(0, 240) }));
  }

  // shortcut: a linear scan over a precomputed lowercase index; ~5k pages scan in tens of ms.
  let HAY = null;
  function search(q, tag) {
    if (!HAY) {
      HAY = D.pages.map((p) => ({ p, t: p.title.toLowerCase(),
        a: [].concat(p.props.aliases || []).join(' ').toLowerCase(),
        s: (p.summary || '').toLowerCase(), b: p.body.toLowerCase() }));
    }
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = [];
    for (const h of HAY) {
      if (tag && !h.p.tags.includes(tag)) continue;
      let score = 0;
      for (const w of words) {
        const s = h.t.includes(w) ? 4 : h.a.includes(w) ? 3 : h.s.includes(w) ? 2 : h.b.includes(w) ? 1 : 0;
        if (!s) { score = -1; break; }
        score += s;
      }
      if (score >= 0) hits.push({ h, words, score });
    }
    return hits.sort((x, y) => y.score - x.score || x.h.p.title.localeCompare(y.h.p.title));
  }
  function snippet({ h, words }) {
    const i = Math.min(...words.map((w) => h.b.indexOf(w)).filter((j) => j >= 0));
    if (!isFinite(i)) return h.p.summary || '';
    const s = h.p.body.slice(Math.max(0, i - 40), i + 100)
      .replace(/\[\[(?:[^\]|]*\|)?([^\]]*)\]\]/g, '$1').replace(/[#*_`>|]/g, '').replace(/\s+/g, ' ').trim();
    return (i > 40 ? '…' : '') + s + '…';
  }

  /* ---- reader panel: one for every section ---- */
  const reader = $('reader');
  let readerId = null, readerFrom = null;
  function readerSection(title, list) {
    if (!list.length) return [];
    const ul = el('ul');
    for (const li of list) ul.append(li);
    return [el('h4', null, title), ul];
  }
  function chipTo(id) {
    const n = GN.get(id) || PAGES.get(id);
    return button('chip', n ? n.title : id, () => openReader(id));
  }
  function groupCounts(n) {
    const key = n.kind === 'domain' ? 'domain' : 'area';
    const c = Object.fromEntries(STATES.map((s) => [s, 0]));
    for (const x of D.graph.nodes) if (x.kind === 'concept' && x[key] === n.id) c[x.state]++;
    return c;
  }
  function conceptDetails(n) {
    const out = [];
    const dom = GN.get(n.domain), area = GN.get(n.area);
    if (n.kind === 'concept') {
      const m = el('div', 'meta');
      m.append(stateWord(n.state));
      out.push(m, el('div', 'meta muted', [dom && dom.title, area && area.title].filter(Boolean).join(' › ')),
        el('div', 'meta', nextText(n.next_due)));
      const d = D.details[n.id] || {};
      if (d.leech) out.push(el('div', 'meta', d.leech));
      // display only: how long answers took never changes a grade or a schedule
      if (d.answer_secs != null) out.push(el('div', 'meta muted', `Usually answers in about ${d.answer_secs} s`));
      const mist = new Map(D.mistakes.map((x) => [x.id, x]));
      out.push(...readerSection('Open mistakes', (d.mistakes || []).map((id) => {
        const mm = mist.get(id), li = el('li');
        li.append(el('span', 'flag', '⚑ '), mm ? mm.title : id, el('span', 'muted', mm ? ` (${mm.count}×)` : ''));
        return li;
      })));
      out.push(...readerSection('Evidence trail', (d.evidence || []).map((ev) => el('li', null,
        [fmtDay(ev.day), VERDICT[ev.verdict] || ev.verdict, ev.item_type, ev.source].filter(Boolean).join(' · ')))));
      for (const [title, ids] of [['Requires', d.requires || []], ['Required by', d.required_by || []]]) {
        if (!ids.length) continue;
        const box = el('div', 'chip-list');
        for (const id of ids) box.append(chipTo(id));
        out.push(el('h4', null, title), box);
      }
      out.push(...readerSection('Problems', (d.problems || []).map((p) => {
        const li = el('li');
        li.append(p.title, el('span', 'muted', p.status ? ' · ' + p.status : ''));
        return li;
      })));
      if (!d.evidence || !d.evidence.length) out.push(el('p', 'empty', 'No evidence yet. Try /keepfold learn on it.'));
    } else {
      if (n.kind === 'area' && dom) out.push(el('div', 'meta muted', dom.title));
      const c = groupCounts(n), total = STATES.reduce((a, s) => a + c[s], 0);
      out.push(el('div', 'meta', total ? `${Math.round(100 * c.solid / total)}% solid · ${plural(total, 'topic')}`
        : 'No topics yet'));
      out.push(...readerSection('Topics by state', STATES.filter((s) => c[s]).map((s) => {
        const li = el('li');
        li.append(stateWord(s), el('span', 'muted', ' ' + c[s]));
        return li;
      })));
    }
    return out;
  }
  function pageView(p) {
    const out = [];
    selfId = p.id;
    if (p.summary) out.push(el('p', 'summary', p.summary));
    if (p.tags.length) {
      const box = el('div', 'chip-list');
      for (const t of p.tags) box.append(button('chip', '#' + t, () => setTag(t), 'Show pages tagged ' + t + ' in the Library'));
      out.push(box);
    }
    const keys = Object.keys(p.props);
    if (keys.length) {
      const table = el('table', 'props'), tbody = el('tbody');
      for (const k of keys) {
        const tr = el('tr'), th = el('th', null, k), td = el('td');
        th.scope = 'row';
        [].concat(p.props[k]).forEach((v, i) => { if (i) td.append(', '); inline(String(v), td); });
        tr.append(th, td);
        tbody.append(tr);
      }
      table.append(tbody);
      out.push(el('h4', null, 'Properties'), table);
    }
    const body = el('div', 'md');
    body.append(md(p.body));
    out.push(body);
    if (p.links.length) {
      const box = el('div', 'chip-list');
      for (const id of p.links) box.append(chipTo(id));
      out.push(el('h4', null, 'Links'), box);
    }
    out.push(el('h4', null, 'Backlinks'));
    const back = backlinks(p.id);
    if (!back.length) out.push(el('p', 'empty', 'No backlinks: no other page links here yet.'));
    else {
      const ul = el('ul', 'backlinks');
      for (const { p: from, line } of back) {
        const li = el('li');
        li.append(button('chip', from.title, () => openReader(from.id)));
        if (line) li.append(inline(line, el('div', 'muted small')));
        ul.append(li);
      }
      out.push(ul);
    }
    selfId = null;
    return out;
  }
  // The right-side panel, emptied and titled: the reader, or the brain's compact inspector.
  function panel(id, title, inspector) {
    closeLook(false);  // the inspector and Look share the right side; the newer one wins
    if (reader.hidden) readerFrom = document.activeElement;
    readerId = id;
    hideTip();
    reader.replaceChildren();
    reader.classList.toggle('inspector', !!inspector);
    reader.setAttribute('aria-label', inspector ? 'Brain inspector' : 'Page reader');
    reader.hidden = false;
    reader.scrollTop = 0;
    reader.append(iconBtn('close', `Close the ${inspector ? 'inspector' : 'reader'} (Esc)`, closeReader, 'icon-btn close'),
      el('h2', null, title));
  }
  function openReader(id, keep) {  // keep: a live refresh (no focus moved, the scroll kept)
    const p = PAGES.get(id), n = GN.get(id), top = reader.scrollTop;
    if (!p && !n) return;
    panel(id, p ? p.title : n.title, false);
    const meta = el('div', 'rmeta');
    meta.append(el('span', null, kindWord(p ? p.kind : n.kind)));
    if (p) {
      const lobe = LOBES.get(p.cluster);
      meta.append(lobeChip(p.cluster));
      if (lobe) meta.append(el('span', null, lobe.title));
      if (p.date) meta.append(el('span', null, fmtDay(p.date) + ' ' + p.date.slice(0, 4)));
      if (p.source) meta.append(el('span', null, p.source));
    }
    reader.append(meta);
    const acts = el('div', 'ractions');
    if (p) {
      const sib = button('btn', null, () => showInBrain(id));
      sib.append(icon('target'), ' Show in brain');
      acts.append(obsidianBtn(p), sib, promptBtn(p));
    }
    reader.append(acts);
    if (n && (n.kind === 'concept' || n.kind === 'domain' || n.kind === 'area')) reader.append(...conceptDetails(n));
    if (p) reader.append(...pageView(p));
    G.setCurrent(id);
    if (keep) reader.scrollTop = top;
    else reader.querySelector('.close').focus({ preventScroll: true });
  }
  function obsidianBtn(p) {
    const a = el('a', 'btn', null);
    a.append(icon('obsidian'), ' Open in Obsidian');
    a.href = 'obsidian://open?vault=' + encodeURIComponent(D.vault_name)
      + '&file=' + encodeURIComponent(p.path);
    return a;
  }
  function promptBtn(p) {
    const cp = button('btn', null, () => copyText(promptFor(p), cp));
    cp.append(icon('copy'), ' Copy prompt');
    cp.title = promptFor(p);
    return cp;
  }

  /* ---- the brain inspector: a page's neighbourhood, or a lobe ---- */
  const DEG = new Map();  // undirected link degree, for "top pages" lists
  countDegrees();
  function countDegrees() {
    DEG.clear();
    const seen = new Set();
    for (const p of D.pages) {
      for (const t of p.links) {
        if (t === p.id || !PAGES.has(t)) continue;
        const key = p.id < t ? p.id + '\n' + t : t + '\n' + p.id;
        if (seen.has(key)) continue;
        seen.add(key);
        DEG.set(p.id, (DEG.get(p.id) || 0) + 1);
        DEG.set(t, (DEG.get(t) || 0) + 1);
      }
    }
  }
  const topPagesOf = (lobeId, n) => D.pages.filter((p) => p.cluster === lobeId)
    .sort((a, b) => (DEG.get(b.id) || 0) - (DEG.get(a.id) || 0) || a.title.localeCompare(b.title)).slice(0, n);
  const live = (msg) => { const r = $('brain-live'); r.textContent = ''; setTimeout(() => { r.textContent = msg; }, 30); };
  function ilist(ids, cls) {
    const ul = el('ul', 'ilist');
    for (const id of ids) {
      const p = PAGES.get(id);
      if (!p) continue;
      const li = el('li');
      if (cls) {  // the same solid / dashed marks as the brain's lines
        const mk = el('span', 'mk ' + cls);
        mk.setAttribute('aria-hidden', 'true');
        li.append(mk);
      }
      li.append(button('linkbtn', p.title, () => explorePage(id)));
      ul.append(li);
    }
    return ul;
  }
  // Fly to a page, light its neighbourhood and show it in the inspector.
  function explorePage(id) {
    const info = brain && brain.explore(id);
    if (info) inspectPage(id, info);
    else openReader(id);
  }
  function inspectPage(id, info) {
    const p = PAGES.get(id);
    if (!p) return;
    panel(id, p.title, true);
    const meta = el('div', 'rmeta'), lobe = LOBES.get(p.cluster), st = GN.has(id) ? GN.get(id).state : null;
    meta.append(el('span', null, kindWord(p.kind)), lobeChip(p.cluster));
    if (lobe) meta.append(el('span', null, lobe.title));
    if (p.kind === 'concept' && st) meta.append(stateWord(st));
    if (dateOf(p)) meta.append(el('span', null, fmtDay(dateOf(p)) + ' ' + dateOf(p).slice(0, 4)));
    const acts = el('div', 'ractions'), open = button('btn', 'Open page', () => openReader(id));
    const lib = button('btn', 'Show in Library', () => libraryFind(p.title));
    acts.append(open, obsidianBtn(p), promptBtn(p), lib);
    reader.append(meta, acts);
    const beacons = (info.beacons || []).filter((l) => LOBES.has(l));
    if (beacons.length) {
      const box = el('div', 'file-under'), row = el('div', 'ractions');
      box.append(el('h4', null, 'File this chat under…'));
      for (const l of beacons) {
        const t = LOBES.get(l).title, b = button('btn', null,
          () => copyText(`File the chat [[${p.id}]] under the topic [[${l.replace(/^domain:/, '')}]] in Keepfold.`, b));
        b.append(icon('copy'), ' ' + t);
        b.title = `Copy: file this chat under ${t}`;
        row.append(b);
      }
      box.append(row);
      reader.append(box);
    }
    reader.append(el('h4', null, `Linked (${info.linked.length + info.hiddenLinks})`));
    if (info.linked.length) reader.append(ilist(info.linked, 'linked'));
    else reader.append(el('p', 'empty', 'No links.'));
    if (info.hiddenLinks > 0) reader.append(el('p', 'muted small', `+${info.hiddenLinks} more links (open the page for all of them)`));
    reader.append(el('h4', null, 'Similar meaning'));
    if (info.similar.length) reader.append(ilist(info.similar, 'similar'));
    else reader.append(el('p', 'empty', 'No similar pages beyond the links.'));
    markOutline(id);
    live(`Selected ${p.title}, ${kindWord(p.kind)}${lobe ? ' in ' + lobe.title : ''}: `
      + `${plural(info.linked.length + info.hiddenLinks, 'link')}, ${info.similar.length} similar.`);
  }
  /* ---- a website of the brain's stem: its details from the browsing store (the app's own
     route, never a vault page); a private one only while the Private switch is on ---- */
  const siteWords = (n) => ['Website', String(n.category || 'other').replace(/-/g, ' '), plural(n.visits, 'visit')]
    .concat(n.private ? ['private'] : []);
  function exploreSite(id) {
    const n = brain && brain.node(id);
    if (n && brain.explore(id)) inspectSite(n);
  }
  async function inspectSite(n) {
    const id = n.id, gen = privGen;
    panel(id, n.title, true);
    const meta = el('div', 'rmeta');
    for (const w of siteWords(n)) meta.append(el('span', null, w));
    reader.append(meta);
    live(`Selected ${n.title}, ${siteWords(n).join(', ')}.`);
    markOutline(id);
    let s = null;
    try {
      s = await api('/api/home/site?site=' + encodeURIComponent(n.site) + (PRIVATE ? '&private=1' : ''));
    } catch (e) {
      if (readerId === id && gen === privGen) reader.append(el('p', 'empty', e.status === 404
        ? 'This website is not in the browsing store any more.' : 'Keepfold is not answering.'));
      return;
    }
    if (readerId !== id || gen !== privGen || reader.hidden) return;  // moved on, or the switch moved meanwhile
    const days = s.days || [];
    if (days.length) meta.append(el('span', null, `first ${fmtDay(days[0])} ${days[0].slice(0, 4)}`),
      el('span', null, `last ${fmtDay(days[days.length - 1])} ${days[days.length - 1].slice(0, 4)}`));
    if (s.description) reader.append(el('p', 'summary', s.description));
    const recent = days.slice(-60).reverse().map(fmtDay).join(', ');
    reader.append(el('h4', null, `Visit days (${days.length})`),
      el('p', 'muted small', recent + (days.length > 60 ? `, and ${days.length - 60} earlier` : '')));
    const counted = (rows) => rows.map(([t, c]) => { const li = el('li'); li.append(el('span', null, t), el('span', 'n', `${c}×`)); return li; });
    reader.append(...readerSection('Searched here', counted(s.searches || [])),
      ...readerSection('Searches that led here', counted(s.led || [])),
      ...readerSection('Pages read', (s.titles || []).map((t) => el('li', null, t))));
    const hubs = (s.hubs || []).filter((h) => PAGES.has(h.id));
    if (hubs.length) {
      const box = el('div', 'chip-list');
      for (const h of hubs) box.append(button('chip', h.title, () => explorePage(h.id)));
      reader.append(el('h4', null, 'Topics'), box);
    }
    const rel = (s.related || []).filter((x) => BRAIN_IDS.has('site:' + x));
    if (rel.length) {
      const box = el('div', 'chip-list');
      for (const x of rel) box.append(button('chip', x, () => exploreSite('site:' + x)));
      reader.append(el('h4', null, 'Related websites'), box);
    }
  }
  function inspectLobe(lobeId) {
    const c = LOBES.get(lobeId), k = D.clusters.findIndex((x) => x.id === lobeId);
    if (!c) return;
    panel(null, c.title, true);
    const meta = el('div', 'rmeta');
    meta.append(lobeChip(lobeId), el('span', null, plural(c.count, 'page')));
    const acts = el('div', 'ractions');
    acts.append(button('btn', 'Show in Library', () => { filterLibraryLobe(lobeId); show('library'); }));
    reader.append(meta, acts);
    const tracts = ((D.brain && D.brain.tracts) || []).filter((t) => t.a === k || t.b === k)
      .map((t) => ({ l: D.clusters[t.a === k ? t.b : t.a], n: t.n })).filter((t) => t.l)
      .sort((a, b) => b.n - a.n).slice(0, 5);
    if (tracts.length) {
      const ul = el('ul', 'ilist');
      for (const t of tracts) {
        const li = el('li');
        li.append(el('span', null, 'connected to '), button('linkbtn', t.l.title, () => selectLobe(t.l.id)),
          el('span', 'n', `(${plural(t.n, 'link')})`));
        ul.append(li);
      }
      reader.append(el('h4', null, 'Strongest connections'), ul);
    }
    const top = topPagesOf(lobeId, 20);
    reader.append(el('h4', null, `Pages (top ${top.length} by links)`), ilist(top.map((p) => p.id), null));
    live(`${c.title}: ${plural(c.count, 'page')}.`);
  }
  function closeReader() {
    if (reader.hidden) return;
    reader.hidden = true;
    readerId = null;
    G.setCurrent(null);
    if (readerFrom && readerFrom.isConnected) readerFrom.focus({ preventScroll: true });
  }

  /* ---- sections and routing ---- */
  // Study only for someone who studies (dashboard.studies), decided once per page load
  const STUDIES = D.studies !== false;
  const SECTIONS = ['home', 'brain', 'library', 'you', 'timeline', 'ideas', 'study', 'help']
    .filter((s) => STUDIES || s !== 'study');
  if (!STUDIES) { $('tab-study').remove(); $('view-study').remove(); }
  const TITLES = { home: 'Home', brain: 'Brain', library: 'Library', you: 'You', timeline: 'Timeline',
    ideas: 'Ideas', study: 'Study', help: 'Help' };
  const rendered = new Set(), stale = new Set();  // stale: rendered from older data (live home)
  let current = null;
  const sel = $('section-select');
  for (const s of SECTIONS) { const o = el('option', null, TITLES[s]); o.value = s; sel.append(o); }
  sel.addEventListener('change', () => show(sel.value));
  function show(name, focusTab) {
    if (!SECTIONS.includes(name)) name = 'home';
    hideTip();
    if (name !== current) {
      closeLook(false);
      for (const s of SECTIONS) {
        const t = $('tab-' + s);
        t.setAttribute('aria-selected', String(s === name));
        t.tabIndex = s === name ? 0 : -1;
        $('view-' + s).hidden = s !== name;
      }
      sel.value = name;
      current = name;
      try { history.replaceState(null, '', '#' + name); } catch (e) { /* a file URL may refuse */ }
      if (!rendered.has(name)) { rendered.add(name); RENDER[name](); }
      else if (stale.delete(name)) rerender(name);
      if (name === 'home') placeBrain('home-brain-slot');
      else clearLens();  // the lens is Home's
      if (name === 'brain') setView(view);
      window.scrollTo(0, 0);
    }
    if (focusTab) $('tab-' + name).focus();
  }
  for (const s of SECTIONS) $('tab-' + s).addEventListener('click', () => show(s));
  $('tabs').addEventListener('keydown', (e) => {
    const f = SECTIONS.indexOf(String(e.target.id).replace('tab-', '')), i = f >= 0 ? f : SECTIONS.indexOf(current);
    const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: SECTIONS.length - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    show(SECTIONS[(to + SECTIONS.length) % SECTIONS.length], true);
  });
  window.addEventListener('hashchange', () => show(location.hash.slice(1)));
  const typing = (t) => t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'
    || t.isContentEditable);
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
    if (e.key === 'Escape') {
      if (!$('palette').hidden) { closePalette(); return; }
      if (!look.hidden) { closeLook(true); return; }
      if (!reader.hidden) { closeReader(); return; }
      if (cold) { closeCold(); return; }
      hideTip();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || !$('palette').hidden) return;
    // the brain uses + - 0, arrows and A M G S; the outline uses arrows
    if (e.target && e.target.closest && e.target.closest('.brain-host, .outline')) return;
    const n = +e.key;
    if (n >= 1 && n <= SECTIONS.length) { e.preventDefault(); show(SECTIONS[n - 1]); }
  });
  function setHdr() {
    document.documentElement.style.setProperty('--hdr', $('top').offsetHeight + 'px');
  }
  window.addEventListener('resize', setHdr);

  /* ---- header: wordmark, greeting, clock, theme ---- */
  const phone = () => window.innerWidth <= 760;
  const roomy = () => window.innerWidth > 1360;  // room for "AS OF" beside the tabs (150% laptops are ~1280)
  const snug = () => window.innerWidth < 1300;  // the header's narrow widths: a smaller clock, no greeting
  function renderHeader() {
    $('wordmark').replaceChildren(dm('KEEPFOLD', phone() ? 2 : 3, 'Keepfold'));
    $('greet').textContent = greeting();
    const hm = D.as_of.slice(11, 16);
    $('clock').replaceChildren(dm(roomy() ? 'AS OF ' + hm : hm, snug() ? 2 : 3, `As of ${fmtDay(TODAY)}, ${hm}`));
    $('clock').title = `Built ${fmtDay(TODAY)} ${TODAY.slice(0, 4)}, ${hm}`;
    if (!LIVE) $('stale').hidden = !(Date.now() - Date.parse(D.as_of) > 12 * 36e5);
    $('open-search').replaceChildren(icon('search'));
  }
  $('open-search').addEventListener('click', () => openPalette());
  if (LIVE) {  // the app's other pages: setup, chat, status, connect, local model, browsing, vault
    const a = el('a', 'icon-btn');
    a.href = '/manage';
    a.setAttribute('aria-label', 'Settings');
    a.title = 'Settings';
    a.append(icon('gear'));
    $('theme').before(a);
  }
  /* ---- the Private switch (live home): private items show only after Windows Hello verifies
     the user; the app relocks it after 10 minutes, on minimise and on restart ---- */
  let PRIVATE = false, switching = false, privGen = 0;
  const lockBtn = LIVE ? button('icon-btn', null, () => setPrivate(!PRIVATE)) : null;
  function syncLock() {
    const word = PRIVATE ? 'Hide private items' : 'Show private items';
    lockBtn.replaceChildren(icon(PRIVATE ? 'unlock' : 'lock'));
    lockBtn.setAttribute('aria-label', word);
    lockBtn.title = word;
  }
  async function setPrivate(on) {
    if (switching) return;
    switching = true;
    try {
      const r = await api(on ? '/api/home/unlock' : '/api/home/lock', {});
      if (on && !r.unlocked) { toast(r.message || 'Private items stay hidden.', 'error'); return; }
      const d = await api(on ? '/api/home?private=1' : '/api/home');  // the whole home again, not a delta
      PRIVATE = on && !d.relock;
      privGen++;
      syncLock();
      applyDelta({ ...d, full: true });
    } catch (e) {
      toast(e.key ? 'This page’s key expired. Open Keepfold from the tray or Start menu.' : 'Keepfold is not answering.', 'error');
    } finally { switching = false; }
  }
  // a newer Keepfold the app has already downloaded: Restart to update, applied only on this click
  // (below 1300 px only its icon shows, named by its label and tooltip); a dev install's is a
  // link to Status, where Updates shows its command
  let updBtn = null;
  function showUpdate(u) {
    if (!u) { if (updBtn) updBtn.hidden = true; return; }
    if (updBtn && updBtn.dataset.mode !== u.mode) { updBtn.remove(); updBtn = null; }
    if (!updBtn) {
      if (u.mode === 'installer') {
        updBtn = button('chip upd', null, async () => {
          try {
            await api('/api/update/apply', {});
            toast('Keepfold is closing to install the update. It reopens by itself when it is done.');
          } catch (e) { toast(e.message, 'error'); }
        });
      } else {
        updBtn = el('a', 'chip upd');
        updBtn.href = '/manage#status';
      }
      updBtn.setAttribute('aria-label', 'Restart to update');
      updBtn.append(icon('update'), el('span', 'upd-word', 'Restart to update'));
      updBtn.dataset.mode = u.mode;
      $('open-search').before(updBtn);
    }
    updBtn.title = `Restart to update: Keepfold ${u.version} is ready` + (u.mode === 'installer' ? '' : '. Status shows the command.');
    updBtn.hidden = false;
  }
  if (LIVE) {
    syncLock();
    $('open-search').before(lockBtn);
  }
  const fit = () => `${phone()} ${roomy()} ${snug()}`;
  let wasFit = fit();
  window.addEventListener('resize', () => {
    if (fit() !== wasFit) { wasFit = fit(); renderHeader(); setHdr(); }
  });

  const darkMq = window.matchMedia('(prefers-color-scheme: dark)');
  let theme = 'night', themeChosen = false;
  try {
    // one key with Manage (app.js); 'keepfold-app-theme' was Manage's own key before
    const t = localStorage.getItem('keepfold-theme') || localStorage.getItem('keepfold-app-theme');
    if (t === 'night' || t === 'day') { theme = t; themeChosen = true; }
  } catch (e) { /* storage off: follow the system */ }
  if (!themeChosen) theme = darkMq.matches || !window.matchMedia('(prefers-color-scheme: light)').matches ? 'night' : 'day';
  function applyTheme(t) {
    theme = t;
    document.documentElement.dataset.theme = t;
    const b = $('theme'), next = t === 'night' ? 'day' : 'night';
    b.replaceChildren(icon(t === 'night' ? 'sun' : 'moon'));
    b.setAttribute('aria-label', `Switch to the ${next} theme`);
    b.title = `Switch to the ${next} theme`;
    G.retheme();
  }
  $('theme').addEventListener('click', () => {
    applyTheme(theme === 'night' ? 'day' : 'night');
    try { localStorage.setItem('keepfold-theme', theme); } catch (e) { /* not kept */ }
  });
  darkMq.addEventListener('change', () => { if (!themeChosen) applyTheme(darkMq.matches ? 'night' : 'day'); });

  /* ---- the brain: one instance, moved between Home and Brain ---- */
  // An old blob has no `brain` (layout is Python's now): the Brain section falls back to the 2D map.
  let brain = null, brainHost = null, brainFailed = !(D.brain && D.brain.nodes && window.KeepfoldBrain);
  let view = brainFailed ? '2d' : '3d', archiveOn = true;
  const BRAIN_IDS = new Set(brainFailed ? [] : D.brain.nodes.ids);
  const MODES = [['activity', 'Activity', 'A'], ['mastery', 'Mastery', 'M'], ['growth', 'Growth', 'G']];
  const modeBtns = [], spinBtns = [], aliveBtns = [];
  const brainFilter = { kinds: new Set(['concept', 'note', 'project', 'decision', 'chat', 'youtube', 'person', 'other']),
    states: new Set(STATES) };
  // keep: a remount's view (camera, mode, style) from the brain it replaces
  function ensureBrain(keep) {
    if (brain || brainFailed) return brain;
    const t = performance.now();
    const pages = D.pages.map((p) => ({ id: p.id, title: p.title, kind: p.kind, cluster: p.cluster, links: p.links,
      date: p.date, updated: p.updated, group: kindGroup(p), state: GN.has(p.id) ? GN.get(p.id).state : null,
      private: !!p.private }));  // never in a blob today; Save picture leaves one out all the same
    const first = !brainHost;
    if (first) {
      brainHost = el('div', 'brain-host');
      $('home-brain-slot').append(brainHost);
    }
    try {
      brain = window.KeepfoldBrain.mount(brainHost, {
        brain: D.brain, pages, today: TODAY, camera: keep && keep.camera, mode: keep && keep.mode,
        onHover(n, pos) {
          if (!n || !pos) { hideTip(); return; }
          const r = brainHost.getBoundingClientRect(), c = LOBES.get(n.cluster);
          showTipAt(r.left + pos.x, r.top + pos.y, [el('b', null, n.title), el('br'),
            el('span', 'k', n.kind === 'site' ? siteWords(n).join(' · ')
              : kindWord(n.kind) + (n.state ? ' · ' + LABEL[n.state] : '') + (c ? ' · ' + c.title : ''))]);
        },
        onSelect(n, info) {
          growClicked.add(n.id);  // a chosen page stops its faint growth glow
          if (info) { if (n.kind === 'site') inspectSite(n); else inspectPage(n.id, info); }
        },
        onToday: syncToday,
        onLens() { lensId = null; syncLens(); },  // Esc in the brain, or Today, ended the lens
        onCluster(c) { if (c) { markLobe(c.id); filterLibraryLobe(c.id); } },
        onLobe(c) { if (c) inspectLobe(c.id); },
        onMode: syncMode,
        onSpin: syncSpin,
        onAlive: syncAlive,
        style: HAS_STYLE ? (keep && keep.style) || savedStyle() : undefined,
        keyHelp: HAS_STYLE ? 'L opens Look.' : '',
        onStyle(s) { saveStyle(s); syncLook(); },
        onCrumbs(c) {
          syncStem();
          const words = (c || []).map((x) => (typeof x === 'string' ? x : x && (x.label || x.title))).filter(Boolean);
          if (words.length) live(words.join(' › '));
        },
      });
    } catch (err) {
      if (window.console) console.warn('brain: could not mount, showing the 2D map', err);
      brainHost.remove();
      brain = null;
      brainFailed = true;
      noBrain();
      return null;
    }
    // brain3d.js owns the other keys on the stage; L is the page's
    if (first) {
      brainHost.addEventListener('keydown', (e) => {
        if (HAS_STYLE && !e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 'l' || e.key === 'L')) {
          e.preventDefault();
          openLook();
        }
      });
    }
    document.body.dataset.brainMs = String(Math.round(performance.now() - t));
    document.body.dataset.brainRenderer = brain.renderer;
    requestAnimationFrame(() => { document.body.dataset.brainFrameMs = String(Math.round(performance.now() - T0)); });
    syncMode(brain.mode);
    syncAlive(!!brain.alive);
    syncStem();
    syncToday(null);
    faintToday();
    return brain;
  }
  // No 3D brain: Home says so in one line, the Brain section opens on the 2D map.
  function noBrainNote(tail) {
    const p = el('p', 'empty', (LIVE ? 'The 3D brain did not start in this window. Keepfold builds it from your pages by itself, '
      + 'so reloading the page usually brings it back. ' : 'This snapshot has no 3D brain: Export snapshot in the Keepfold app makes a new one. ') + tail);
    if (LIVE) p.append(' ', button('btn', 'Reload', () => location.reload()));
    return p;
  }
  function noBrain() {
    $('home-brain-slot').replaceChildren(noBrainNote('The 2D map is in Brain.'));
    $('home-brain-tools').hidden = true;
    $('viewswitch').querySelector('[data-view="3d"]').hidden = true;
    if (view === '3d') view = '2d';
  }
  function placeBrain(slotId) {
    const b = ensureBrain(), slot = $(slotId);
    if (!b) return;
    if (brainHost.parentNode !== slot) slot.append(brainHost);
    b.setArchive(slotId === 'brain-slot' && archiveOn);  // the unsorted-chat ring stays off on Home
    b.resize();
    if (slotId === 'home-brain-slot') growCheck();
  }
  /* ---- the growth moment: the pages added since this viewer last saw the Home brain glow where it
     grew (brain3d.js draws it). `keepfold.brain.seenAt` keeps the newest `added` stamp shown; with
     none yet, the first look only sets it. Storage may fail: the page's memory stands in. ---- */
  const SEEN_KEY = 'keepfold.brain.seenAt', GROWTH = (window.KeepfoldBrain || {}).GROWTH;
  const growClicked = new Set();
  let seenMem = null, growWait = false, faintTimer = 0, countRaf = 0;
  const store = () => { try { return window.localStorage; } catch (e) { return null; } };
  function growCheck() {
    if (!brain || !GROWTH || !D.brain || !D.brain.nodes) return null;
    if (current !== 'home' || brainHost.parentNode !== $('home-brain-slot')) return null;  // the Home brain only
    if (document.hidden) { growWait = true; return null; }  // played when the window shows again
    growWait = false;
    const added = D.brain.nodes.added || {}, kept = GROWTH.read(store(), SEEN_KEY);
    const seen = kept && (!seenMem || kept > seenMem) ? kept : seenMem;
    const p = GROWTH.pending(added, seen, (id) => brain.shows(id), new Date().toISOString());
    if (p.newest && p.newest !== seen) { seenMem = p.newest; GROWTH.write(store(), SEEN_KEY, p.newest); }
    const got = p.ids.length ? brain.grow(p.ids) : null;
    if (got) {
      countUp(got.n);
      live(`The brain grew: ${got.labels.join(', ')}.`);
    }
    return got;
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && growWait) growCheck(); });
  // today's new pages keep a faint glow until local midnight, or until chosen
  function faintToday() {
    if (!brain || !GROWTH || !D.brain || !D.brain.nodes) return;
    const mid = new Date();
    mid.setHours(0, 0, 0, 0);
    brain.faint(GROWTH.since(D.brain.nodes.added, mid.getTime(), growClicked));
    clearTimeout(faintTimer);
    faintTimer = setTimeout(faintToday, Math.max(1000, mid.getTime() + 864e5 - Date.now() + 1000));
  }
  // the panel header's page count climbs from before the new pages to now (none under reduced motion)
  function countUp(n) {
    const line = $('lobe-line');
    cancelAnimationFrame(countRaf);
    if (REDUCED.matches || !line) return;
    const to = D.pages.length, from = Math.max(0, to - n), t0 = performance.now();
    const step = (t) => {
      const p = Math.min(1, Math.max(0, (t - t0 - 400) / 1600)), e = p * p * (3 - 2 * p);
      const k = p < 1 ? Math.round(from + (to - from) * e) : D.pages.length;
      line.textContent = line.textContent.replace(/^[\d,]+/, k.toLocaleString('en'));
      if (p < 1) countRaf = requestAnimationFrame(step);
    };
    step(t0);
  }
  // Live home: a new brain blob. brain3d.js has no incremental add, so the brain is mounted
  // again where it was (camera, selection, mode, Growth month, spin, style, filters) and the new
  // pages pop for 3 s. Never under a pointer or a flight, at most once every 2 s.
  let remountIds = null, remountAt = 0, remountTimer = 0, popTimer = 0;
  function remountBrain(added) {
    remountIds = (remountIds || []).concat(added || []);
    clearTimeout(remountTimer);
    if (!brain) { remountIds = null; return; }  // not mounted yet: it mounts the new blob
    const wait = brain.busy ? 250 : remountAt + 2000 - Date.now();
    if (wait > 0) { remountTimer = setTimeout(remountBrain, wait); return; }
    remountAt = Date.now();
    const ids = remountIds, old = brain;
    remountIds = null;
    const keep = { camera: old.camera, mode: old.mode, style: old.style, selected: old.selected,
      spin: old.spin, growth: old.growth, focused: old.focused, atStem: old.atStem, today: old.todayIds,
      lensCam: old.lensCam };
    hideTip();
    old.destroy();
    brain = null;
    if (!ensureBrain(keep)) return;
    brain.setArchive(brainHost.parentNode === $('brain-slot') && archiveOn);
    if (brainUi) applyBrainFilter();
    if (keep.growth) brain.setGrowth(keep.growth);
    if (keep.focused) { brain.refocus(keep.focused); markLobe(brain.focused); }
    if (keep.selected) brain.reselect(keep.selected);
    if (keep.spin) brain.setSpin(true);
    if (keep.atStem) brain.atStem = true;
    if (keep.today) brain.today(keep.today, keep.selected, true);
    if (lensId) { if (BRAIN_IDS.has(lensId)) applyLens(true, keep.lensCam); else { lensId = null; syncLens(); } }
    syncStem();
    syncLook();
    findInBrain();
    // new pages: the growth moment on Home; elsewhere (or nothing new to this viewer) they pop for 3 s
    if (!growCheck() && ids.length) {
      brain.highlight(ids);
      clearTimeout(popTimer);
      popTimer = setTimeout(() => { if (brain) findInBrain(); }, 3000);
    }
  }
  function showInBrain(id) {
    show('brain');
    if (!ensureBrain()) { setView('2d'); G.setCurrent(id); return; }
    setView('3d');
    if (!brain.flyTo(id)) {  // filtered out: show everything, then fly
      resetBrainFilters();
      brain.flyTo(id);
    }
  }
  // Mode buttons: Home and Brain both show them; onMode keeps them in step.
  function modeGroup() {
    const g = el('div', 'seg');
    g.setAttribute('role', 'group');
    g.setAttribute('aria-label', 'Brain mode');
    for (const [m, word, key] of MODES) {
      const b = button(null, word, () => { if (brain) brain.setMode(m); });
      b.title = `${word} mode (${key} in the brain)`;
      b.dataset.mode = m;
      b.setAttribute('aria-pressed', 'false');
      modeBtns.push(b);
      g.append(b);
    }
    return g;
  }
  // Today, and while in it: the previous and next page of today with "k of n" between them
  const todayUis = [];
  function todayBtn(cls) {
    const b = button(cls + ' tbtn', null, playToday);
    b.title = 'Show everything from today in the brain: pages taught, tested, asked about, added or saved; the Left and Right arrow keys step through them; press again to leave Today';
    b.append(icon('target'), ' Today');
    b.setAttribute('aria-pressed', 'false');
    const at = el('span', 'today-at');
    const stepper = el('span', 'today-step');
    stepper.append(button(cls, '‹', () => { if (brain) brain.todayStep(-1); }, 'Previous page from today'), at,
      button(cls, '›', () => { if (brain) brain.todayStep(1); }, 'Next page from today'));
    stepper.hidden = true;
    todayUis.push({ b, at, stepper });
    const g = el('span', 'today-group');
    g.append(b, stepper);
    return g;
  }
  function syncToday(st) {
    for (const u of todayUis) {
      u.b.setAttribute('aria-pressed', String(!!st));
      u.stepper.hidden = !st || st.n < 2;
      u.at.textContent = st ? (st.at >= 0 ? `${st.at + 1} of ${st.n}` : `${st.n} today`) : '';
    }
  }
  // Stem: fly to the browsing stem's websites; pressed again, back to the overview
  const stemBtns = [];
  function stemBtn() {
    const b = button('btn tbtn', 'Stem', () => { if (brain) { const on = brain.stem(); live(on ? 'Showing the websites in the brain stem.' : 'Back to the whole brain.'); } syncStem(); });
    b.title = 'Fly to the websites in the brain stem; press again for the whole brain';
    stemBtns.push(b);
    syncStem();
    return b;
  }
  function syncStem() {
    for (const b of stemBtns) {
      b.hidden = !(brain && brain.hasStem);
      b.setAttribute('aria-pressed', String(!!(brain && brain.atStem)));
    }
  }
  /* ---- Save picture: the brain's shape and light as a 1200 x 675 PNG, safe to post anywhere. Its
     only text is the caption, built from two counts and the day, never from a page field. ---- */
  const PIC_W = 1200, PIC_H = 675;
  function pictureBtn() {
    const b = button('btn tbtn', null, savePicture);
    b.title = 'Save a picture of your brain’s shape: no titles, no names, nothing private';
    b.append(icon('update'), ' Save picture');
    return b;
  }
  function brainPicture() {
    const p = brain && brain.picture && brain.picture(PIC_W, PIC_H);
    if (!p) return null;
    const g = p.canvas.getContext('2d'), n = (k, w) => `${k.toLocaleString('en')} ${w}${k === 1 ? '' : 's'}`;
    g.font = '18px system-ui, "Segoe UI", sans-serif';
    g.textBaseline = 'alphabetic';
    g.textAlign = 'left';
    g.fillStyle = 'rgba(255, 176, 0, 0.85)';  // --lit at 85 %
    g.fillText(`${n(p.pages, 'page')} in ${n(p.areas, 'area')}, kept on this computer`, 24, PIC_H - 24);
    g.textAlign = 'right';
    g.fillStyle = '#958870';  // night --ink-3
    g.fillText(`${fmtDay(TODAY)} ${TODAY.slice(0, 4)}`, PIC_W - 24, PIC_H - 24);
    return p.canvas;
  }
  function downloadBlob(blob, name) {
    const a = el('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  }
  async function savePicture() {
    const name = `keepfold-brain-${TODAY.slice(0, 10)}.png`;
    try {
      const c = brainPicture();
      const blob = c && await new Promise((r) => c.toBlob(r, 'image/png'));
      if (!blob) throw new Error('That picture could not be made.');
      if (!LIVE) { downloadBlob(blob, name); live('Picture saved to your downloads.'); return; }
      const url = await new Promise((ok, no) => {
        const fr = new FileReader();
        fr.onload = () => ok(String(fr.result));
        fr.onerror = () => no(new Error('That picture could not be made.'));
        fr.readAsDataURL(blob);
      });
      const r = await api('/api/brain/picture', { png: url.slice(url.indexOf(',') + 1), day: TODAY.slice(0, 10) });
      if (r.download) { downloadBlob(blob, name); live('Picture saved to your downloads.'); }
      else if (r.saved) live(`Picture saved: ${r.saved}`);
    } catch (e) { live(e.message); }
  }
  function playToday() {
    if (!brain) return;
    if (brain.todayIds) { brain.overview(); live('Left Today.'); return; }
    // all of today's work: the feed's today (taught, tested, asked, added, chats; events, which leave a
    // concept's file alone) and every page the owner saved today (`today_ids`: never a page a Keepfold
    // job rewrote), newest first; the feed and the pages are already privacy-filtered
    const d = (p) => dateOf(p) || '';
    const mine = new Set(D.today_ids || []);
    const saved = D.pages.filter((p) => mine.has(p.id))
      .sort((a, b) => (d(b) > d(a) ? 1 : d(b) < d(a) ? -1 : 0)).map((p) => p.id);
    // a chat the automatic import brought in today counts only when the chat itself is from today (or
    // undated), so a first import of older chats does not flood Today
    const fresh = (i) => i.kind !== 'chat' || !PAGES.has(i.id) || !PAGES.get(i.id).date || PAGES.get(i.id).date === TODAY;
    const ids = [...new Set([...((D.feed && D.feed.today) || []).filter(fresh).map((i) => i.id), ...saved])]
      .filter((id) => BRAIN_IDS.has(id));
    brain.today(ids);
    live(ids.length ? `Showing ${plural(ids.length, 'page')} from today.` : 'Nothing from today yet.');
  }
  function syncMode(m) {
    for (const b of modeBtns) b.setAttribute('aria-pressed', String(b.dataset.mode === m));
    for (const s of scrubs) { s.root.hidden = m !== 'growth'; if (m !== 'growth') s.stop(); }
    if (brainUi && !brainFailed) renderLegend(m);
  }

  /* ---- the Growth scrubber: a bar per month of new pages above the range (decoration; the range is
     the real control), "1 year" and "All" for the span, and Play, which runs the handle through it ---- */
  let growthData = null;
  function growthMonths() {
    if (growthData) return growthData;
    const months = [], counts = {};
    // a typo like 0001-01-01 or 9999-12-31 must not make the slider thousands of months long
    const latest = String(+TODAY.slice(0, 4) + 1) + TODAY.slice(4);
    const dates = D.pages.map((p) => p.date).filter((d) => isDay(d) && d >= '1990' && d <= latest).sort();
    if (dates.length) {
      let y = +dates[0].slice(0, 4), m = +dates[0].slice(5, 7);
      const end = dates[dates.length - 1].slice(0, 7);
      for (;;) {
        const k = `${y}-${String(m).padStart(2, '0')}`;
        months.push(k);
        if (k >= end) break;
        if (++m > 12) { m = 1; y++; }
      }
    }
    for (const d of dates) counts[d.slice(0, 7)] = (counts[d.slice(0, 7)] || 0) + 1;
    return (growthData = { months, counts });
  }
  const scrubs = [];
  const PLAY_MS = 6000;
  function buildScrub() {
    let all = growthMonths().months, counts = growthMonths().counts;
    let span = all, yearOn = false, playing = false, raf = 0;
    const root = el('div', 'scrub'), bars = el('div', 'scrub-bars'), range = el('input'), out = el('output');
    root.hidden = !(brain && brain.mode === 'growth');
    bars.setAttribute('aria-hidden', 'true');
    range.type = 'range';
    range.min = '0';
    range.setAttribute('aria-label', 'Growth: show pages up to this month');
    const yr = button('btn tbtn', '1 year', () => setSpan(true)), whole = button('btn tbtn', 'All', () => setSpan(false));
    const play = button('btn tbtn', 'Play', () => (playing ? stop() : start()));
    play.hidden = REDUCED.matches;
    REDUCED.addEventListener('change', () => { play.hidden = REDUCED.matches; if (REDUCED.matches) stop(); });
    const paint = () => {
      const v = +range.value;
      [...bars.children].forEach((b, k) => b.classList.toggle('on', k <= v));
      const mo = span[v];
      out.textContent = mo ? fmtMonth(mo) : '';
      range.setAttribute('aria-valuetext', out.textContent);
    };
    const draw = () => {
      const peak = Math.max(1, ...span.map((m) => counts[m] || 0));
      range.max = String(Math.max(0, span.length - 1));
      bars.replaceChildren(...span.map((m, k) => {
        const b = el('span', 'sb'), i = el('i'), n = counts[m] || 0;
        i.style.height = n ? Math.max(2, Math.round(32 * n / peak)) + 'px' : '0';
        b.dataset.i = String(k);
        b.append(i);
        return b;
      }));
      yr.setAttribute('aria-pressed', String(yearOn));
      whole.setAttribute('aria-pressed', String(!yearOn));
    };
    const apply = () => {
      paint();
      const mo = span[+range.value] || null;
      if (brain) brain.setGrowth(mo);
      for (const s of scrubs) if (s.root !== root) s.set(mo);
    };
    function setSpan(year) {
      stop();
      yearOn = year;
      span = year ? all.slice(-12) : all;
      range.value = String(span.length - 1);
      draw();
      apply();
    }
    function stop() {
      if (!playing) return;
      playing = false;
      cancelAnimationFrame(raf);
      play.textContent = 'Play';
      document.removeEventListener('keydown', onKey);
    }
    function onKey(e) { if (e.key === 'Escape') stop(); }
    function start() {
      if (span.length < 2) return;
      playing = true;
      play.textContent = 'Pause';
      document.addEventListener('keydown', onKey);
      const t0 = performance.now();
      let last = -1;
      const step = () => {
        if (!playing) return;
        if (!root.offsetParent) { stop(); return; }  // the tab or the mode was left
        const p = Math.min(1, (performance.now() - t0) / PLAY_MS), k = Math.round(p * (span.length - 1));
        if (k !== last) { last = k; range.value = String(k); apply(); }
        if (p >= 1) stop(); else raf = requestAnimationFrame(step);
      };
      step();
    }
    range.addEventListener('input', () => { stop(); apply(); });
    bars.addEventListener('click', (e) => {  // pointer only: the range is the keyboard's way
      const b = e.target.closest && e.target.closest('.sb');
      if (!b) return;
      stop();
      range.value = b.dataset.i;
      apply();
    });
    range.value = String(Math.max(0, span.length - 1));
    draw();
    paint();
    const row = el('div', 'scrub-row'), seg = el('span', 'scrub-span');
    seg.append(yr, whole);
    row.append(el('span', null, 'Up to'), range, out, seg, play);
    root.append(bars, row);
    if (!all.length) { range.disabled = true; for (const b of [yr, whole, play]) b.disabled = true; }
    const me = { root, stop, refresh() {  // new data: the months and counts again, the chosen month kept
      stop();
      const keep = span[+range.value];
      all = growthMonths().months;
      counts = growthMonths().counts;
      span = yearOn ? all.slice(-12) : all;
      const k = span.indexOf(keep);
      range.disabled = !all.length;
      for (const b of [yr, whole, play]) b.disabled = !all.length;
      draw();
      range.value = String(k < 0 ? Math.max(0, span.length - 1) : k);
      paint();
    }, set(month) {  // follow the other scrubber, without moving the brain
      let k = span.indexOf(month);
      if (k < 0) k = month && month < span[0] ? 0 : span.length - 1;
      range.value = String(Math.max(0, k));
      paint();
    } };
    for (let i = scrubs.length; i--;) if (!scrubs[i].root.isConnected) scrubs.splice(i, 1);  // a rerender's old ones
    scrubs.push(me);
    return me;
  }

  /* ---- Focus Lens: a Home row's page and the pages linked with it stay lit, the rest dims as in
     Today and the camera frames them; Clear, Esc, the same row again or Today ends it ---- */
  let lensId = null, lensAdj = null, lensEl = null;
  const LENS_CAP = 40, LENS_FEW = 5;
  function lensNear(id) {  // links both ways, between pages the brain shows
    if (!lensAdj) {
      lensAdj = new Map();
      const add = (a, b) => { if (!lensAdj.has(a)) lensAdj.set(a, new Set()); lensAdj.get(a).add(b); };
      for (const p of D.pages) {
        if (!BRAIN_IDS.has(p.id)) continue;
        for (const t of p.links) if (t !== p.id && BRAIN_IDS.has(t)) { add(p.id, t); add(t, p.id); }
      }
    }
    return lensAdj.get(id) || [];
  }
  function lensBar() {
    lensEl = el('span', 'lens-bar');
    lensEl.hidden = true;
    return lensEl;
  }
  function syncLens() {
    if (lensEl) {
      lensEl.hidden = !lensId;
      if (lensId) {
        const n = lensEl.dataset.n, p = PAGES.get(lensId);
        const note = el('span', 'lens-note', 'Focus: ');
        note.append(el('b', null, p ? p.title : lensId), `, ${plural(+n, 'linked page')}`);
        lensEl.replaceChildren(note, button('band-btn', 'Clear', () => clearLens()));
      }
    }
    for (const b of document.querySelectorAll('[data-lens]')) b.setAttribute('aria-pressed', String(b.dataset.lens === lensId));
  }
  // quiet: a remount puts the same lens on the new brain (no camera move)
  function applyLens(quiet, cam) {
    const ids = window.KeepfoldBrain.LENS.set(lensId, lensNear, LENS_CAP, LENS_FEW), got = brain.lens(ids, quiet, cam);
    if (!got) { lensId = null; syncLens(); return false; }
    lensEl.dataset.n = String(Math.max(0, got - 1));
    syncLens();
    return true;
  }
  function setLens(id) {
    if (!brain || !BRAIN_IDS.has(id)) return;
    lensId = id;
    if (!applyLens(false)) return;
    const slot = $('home-brain-slot');
    if (slot.scrollIntoView) slot.scrollIntoView({ block: 'nearest', behavior: REDUCED.matches ? 'auto' : 'smooth' });
    live(`Focus: ${PAGES.get(id) ? PAGES.get(id).title : id}, ${plural(+lensEl.dataset.n, 'linked page')}.`);
  }
  function clearLens() {
    if (!lensId) return;
    lensId = null;
    if (brain) brain.endLens();
    syncLens();
  }
  const toggleLens = (id) => (lensId === id ? clearLens() : setLens(id));
  // a quiet button at a Home row's right end (rows of pages the brain does not show get none)
  function lensBtn(id, title) {
    if (brainFailed || !BRAIN_IDS.has(id) || !PAGES.has(id)) return null;
    const b = iconBtn('focus', `Show ${title} in the brain`, () => toggleLens(id));
    b.dataset.lens = id;
    b.setAttribute('aria-pressed', String(id === lensId));
    return b;
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && lensId && !e.defaultPrevented) clearLens(); });
  function syncSpin(on) {
    for (const b of spinBtns) b.setAttribute('aria-pressed', String(on));
  }
  function syncAlive(on) {
    for (const b of aliveBtns) b.setAttribute('aria-pressed', String(on));
  }
  if (!brainFailed) {
    $('home-brain-tools').append(modeGroup(), todayBtn('band-btn'), lensBar());
    $('home-brain-slot').before(buildScrub().root);  // under the band, where the panel's own colours are
  } else noBrain();
  $('explore').addEventListener('click', () => { show('brain'); setView('3d'); });

  /* ---- Look: the brain's style, generated from the renderer's schema ---- */
  const CB = window.KeepfoldBrain || {};
  // an older brain3d.js has no schema: no Look button, nothing else changes
  const HAS_STYLE = !brainFailed && Array.isArray(CB.STYLE_SCHEMA) && Array.isArray(CB.PRESETS);
  const SCHEMA = HAS_STYLE ? CB.STYLE_SCHEMA : [];
  const STYLE_KEY = 'keepfold.brain.style';
  const look = $('look'), lookSync = [], lookBtns = [];
  let lookBuilt = false, lookFrom = null, readerWas = false;
  // Only schema keys survive a reload, so a stale or hand-edited entry cannot inject others.
  function savedStyle() {
    const out = Object.assign({}, CB.DEFAULT_STYLE);
    try {
      const s = JSON.parse(localStorage.getItem(STYLE_KEY));
      if (s && typeof s === 'object' && !Array.isArray(s)) for (const f of SCHEMA) if (f.key in s) out[f.key] = s[f.key];
    } catch (e) { /* storage blocked or bad JSON: the default */ }
    return out;
  }
  // Only keys that differ from the default are kept, so a later default change still reaches the user.
  function saveStyle(s) {
    const diff = {};
    for (const f of SCHEMA) if (f.key in s && !same(s[f.key], CB.DEFAULT_STYLE[f.key])) diff[f.key] = s[f.key];
    try {
      if (Object.keys(diff).length) localStorage.setItem(STYLE_KEY, JSON.stringify(diff));
      else localStorage.removeItem(STYLE_KEY);
    } catch (e) { /* not kept */ }
  }
  function setLook(partial) {
    if (!brain) return;
    brain.setStyle(partial);
    syncLook();
  }
  const same = (a, b) => (typeof a === 'number' && typeof b === 'number' ? Math.abs(a - b) < 1e-6 : a === b);
  const fullPreset = (p) => Object.assign({}, CB.DEFAULT_STYLE, p.style);
  function fmtVal(f, v) {
    const dec = String(f.step || 1).split('.')[1];
    return String(+(+v).toFixed(dec ? dec.length : 0));
  }
  function buildLook() {
    lookBuilt = true;
    look.append(iconBtn('close', 'Close Look (Esc)', () => closeLook(true), 'icon-btn close'), el('h2', null, 'Look'));
    const pre = el('div', 'look-presets'), seg = el('div', 'seg'), custom = el('span', 'look-custom', 'Custom');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'Presets');
    for (const p of CB.PRESETS) {
      const b = button(null, p.label, () => setLook(fullPreset(p)));
      b.setAttribute('aria-pressed', 'false');
      seg.append(b);
      lookSync.push((s) => {
        const full = fullPreset(p);
        b.setAttribute('aria-pressed', String(SCHEMA.every((f) => same(s[f.key], full[f.key]))));
      });
    }
    lookSync.push(() => { custom.hidden = !!seg.querySelector('[aria-pressed="true"]'); });
    pre.append(el('h3', null, 'Presets'), seg, custom);
    look.append(pre);
    const groups = [];
    for (const f of SCHEMA) if (!groups.includes(f.group)) groups.push(f.group);
    for (const g of groups) {
      const sec = el('details', 'look-group');
      sec.open = true;
      sec.append(el('summary', null, g));
      for (const f of SCHEMA.filter((x) => x.group === g)) sec.append(lookRow(f));
      look.append(sec);
    }
    look.append(button('btn look-reset', 'Reset to default', () => setLook(Object.assign({}, CB.DEFAULT_STYLE))));
  }
  function lookRow(f) {
    const row = el('div', 'look-row'), id = 'look-' + f.key, helpId = id + '-help';
    const help = f.help ? el('p', 'look-help', f.help) : null;
    if (help) help.id = helpId;
    const describe = (n) => { if (help) n.setAttribute('aria-describedby', helpId); };
    if (f.type === 'toggle') {
      const lab = el('label', 'toggle'), box = el('input');
      box.type = 'checkbox';
      box.id = id;
      describe(box);
      box.addEventListener('change', () => setLook({ [f.key]: box.checked }));
      lab.append(box, ' ' + f.label);
      row.append(lab);
      lookSync.push((s) => { box.checked = !!s[f.key]; });
    } else if (f.type === 'range') {
      const lab = el('label', 'look-label', f.label), r = el('input'), out = el('output'), line = el('div', 'look-range');
      lab.htmlFor = id;
      r.type = 'range';
      r.id = id;
      r.min = String(f.min);
      r.max = String(f.max);
      r.step = String(f.step || 'any');
      out.htmlFor = id;
      describe(r);
      const showVal = (v) => { out.textContent = fmtVal(f, v); r.setAttribute('aria-valuetext', out.textContent); };
      r.addEventListener('input', () => { showVal(r.value); setLook({ [f.key]: +r.value }); });
      line.append(r, out);
      row.append(lab, line);
      lookSync.push((s) => { if (s[f.key] != null) { r.value = String(s[f.key]); showVal(s[f.key]); } });
    } else if (f.type === 'choice') {
      const lab = el('span', 'look-label', f.label), seg = el('div', 'seg');
      lab.id = id;
      seg.setAttribute('role', 'group');
      seg.setAttribute('aria-labelledby', id);
      describe(seg);
      for (const [v, word] of f.options || []) {
        const b = button(null, word, () => setLook({ [f.key]: v }));
        b.setAttribute('aria-pressed', 'false');
        seg.append(b);
        lookSync.push((s) => b.setAttribute('aria-pressed', String(same(s[f.key], v))));
      }
      row.append(lab, seg);
    } else return row;
    if (help) row.append(help);
    return row;
  }
  function syncLook() {
    if (!lookBuilt || !brain) return;
    const s = brain.style || {};
    for (const fn of lookSync) fn(s);
  }
  function openLook() {
    if (!brain || !HAS_STYLE) return;
    if (!lookBuilt) buildLook();
    if (look.hidden) {
      lookFrom = document.activeElement;
      readerWas = !reader.hidden;
      reader.hidden = true;
      hideTip();
    }
    look.hidden = false;
    syncLook();
    for (const b of lookBtns) b.setAttribute('aria-expanded', 'true');
    const first = look.querySelector('.look-presets button');
    if (first) first.focus({ preventScroll: true });
  }
  function closeLook(restore) {
    if (look.hidden) return;
    look.hidden = true;
    for (const b of lookBtns) b.setAttribute('aria-expanded', 'false');
    if (restore && readerWas) reader.hidden = false;
    readerWas = false;
    if (restore && lookFrom && lookFrom.isConnected) lookFrom.focus({ preventScroll: true });
  }
  function lookBtn() {
    const b = button('btn tbtn', 'Look', () => (look.hidden ? openLook() : closeLook(true)));
    b.title = 'Change how the brain looks (L in the brain)';
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('aria-controls', 'look');
    b.setAttribute('aria-keyshortcuts', 'L');
    lookBtns.push(b);
    return b;
  }

  /* ---- boards ---- */
  function board(title, opts) {
    opts = opts || {};
    const root = el('div', 'board' + (opts.cls ? ' ' + opts.cls : ''));
    const band = el('div', 'band');
    band.append(el('h2', null, title));
    if (opts.note) band.append(el('span', 'band-note', opts.note));
    if (opts.action) band.append(opts.action);
    const body = el('div', 'board-body');
    root.append(band, body);
    return { root, band, body };
  }
  // Board -> brain: hovering or focusing a row lights its page in the brain.
  function syncRow(row, id) {
    const on = () => { if (brain) brain.hoverId(id); }, off = () => { if (brain) brain.hoverId(null); };
    row.addEventListener('pointerenter', on);
    row.addEventListener('pointerleave', off);
    row.addEventListener('focusin', on);
    row.addEventListener('focusout', off);
  }
  // One board row: title (opens the reader) over a muted line, a right-hand cell and actions.
  function brow({ id, title, sub, left, right, prompt, brainId }) {
    const r = el('div', 'brow');
    const t = el('div', 't'), l1 = el('div', 'line1');
    if (left) l1.append(left);
    l1.append(id && (PAGES.has(id) || GN.has(id)) ? button('ttl linkbtn', title, () => openReader(id)) : el('span', 'ttl', title));
    t.append(l1);
    if (sub) t.append(el('span', 'sub', sub));
    const acts = el('div', 'acts');
    if (right) acts.append(right);
    if (prompt) acts.append(copyBtn(prompt));
    if (brainId && PAGES.has(brainId)) {
      acts.append(iconBtn('target', 'Show in brain', () => showInBrain(brainId)));
      syncRow(r, brainId);
      const f = homeRows && lensBtn(brainId, title);
      if (f) acts.append(f);
    }
    r.append(t, acts);
    return r;
  }
  // Home's own rows (Exams and deadlines, Open loops, Back on your mind) carry the Focus Lens button
  let homeRows = false;
  const onHome = (fn) => (x) => { homeRows = true; try { return fn(x); } finally { homeRows = false; } };
  function moreFoot(n, where, section) {
    const f = el('div', 'foot');
    f.append(button('linkbtn', `+ ${n} more on ${where}`, () => show(section)));
    return f;
  }
  const LOOP_CODE = { commitment: 'CMT', question: 'Q', idea: 'IDEA' };
  const loopCode = (t) => el('span', 'code', LOOP_CODE[t] || (t ? String(t).slice(0, 4).toUpperCase() : 'LOOP'));
  function loopRow(l) {
    const bits = [];
    if (l.created) bits.push('since ' + fmtDay(l.created));
    if (l.due) bits.push('due ' + fmtDay(l.due));
    if (l.summary) bits.push(l.summary);
    return brow({ id: l.id, title: l.title, sub: bits.join(' · '), left: loopCode(l.loop_type),
      prompt: loopPrompt(l.id), brainId: l.id });
  }
  const fromWhen = (id) => {
    const p = PAGES.get(id), d = p && dateOf(p);
    return isDay(d) ? 'from ' + fmtMonth(d) : '';
  };
  function resurfaceRow(r) {
    return brow({ id: r.id, title: r.title, sub: [fromWhen(r.id), r.summary].filter(Boolean).join(' · '),
      left: el('span', 'kind', kindWord(r.kind)), prompt: `/keepfold about ${r.id}`, brainId: r.id });
  }
  function followRow(f) {
    return brow({ id: f.id, title: f.title, sub: 'revisit on ' + fmtDay(f.review_on),
      left: el('span', 'code', 'DEC'), prompt: loopPrompt(f.id), brainId: f.id });
  }
  function boardList(b, rows, cap, empty, more) {
    if (!rows.length) b.body.append(el('p', 'empty', empty));
    for (const r of rows.slice(0, cap)) b.body.append(r);
    if (more && rows.length > cap) b.root.append(moreFoot(rows.length - cap, more[0], more[1]));
    return b.root;
  }
  // One readiness metric on Study: the weighted Solid share, named as such, beside
  // the plain topic count (the two differ when topics carry different weights).
  function readiness(r) {
    const topics = `${r.solid} of ${plural(r.total, 'topic')}`;
    if (r.share == null) return { pct: Math.round(100 * r.solid / r.total), label: `${topics} solid` };
    const pct = Math.round(100 * r.share);
    return { pct, label: `${pct}% of the test's weight solid · ${topics}` };
  }

  // Nothing planned and nothing ever checked: three first steps instead of "Done for today";
  // for someone who does not study, steps that fit everyone (shown until something is imported)
  function firstRunBoard() {
    const b = board('Start here', { cls: 'first-run' });
    const steps = STUDIES ? [
      ['Add a test date', 'Tell any Claude chat about a test and its date, or add one in assessments/.', null],
      ['Run /keepfold in Claude', 'Your home card, what is due and what is waiting for you.', '/keepfold'],
      ['Import your notes or chats', 'In the Keepfold app: Setup → Add your history. Or in a terminal: keepfold import <file>.', null]] : [
      ['Add your chats', 'In the Keepfold app: Setup → Add your history. Your AI chats, notes and calendar become pages you can ask about.', null],
      ['Connect an AI app', 'In the Keepfold app: Connect AI apps. Claude, ChatGPT, Codex or Cursor then know your preferences.', null],
      ['Ask Chat', "In the Keepfold app: Chat. Ask about anything you've saved.", null]];
    steps.forEach(([title, sub, prompt], i) => b.body.append(brow({ title, sub, prompt,
      left: el('span', 'code', String(i + 1)) })));
    return b.root;
  }

  /* ---- Home ---- */
  // Next up: tests and deadlines in the next 2 days, then each subject's next steps (the most
  // recently studied subject first; the snapshot has today's plan), then due loops and decisions.
  function nextUp() {
    const items = [];
    for (const a of D.exams || D.assessments) {
      if (a.days_left == null || a.days_left < 0 || a.days_left > 2) continue;
      items.push({ time: a.days_left === 0 ? 'TODAY' : `IN ${a.days_left}D`, id: a.id,
        title: [a.company || a.id, a.kind].filter(Boolean).join(' · '),
        why: a.kind === 'deadline' ? 'Deadline' : 'Assessment',
        lobe: pageLobe(a.id), word: 'DUE', prompt: `/keepfold mock ${a.id}` });
    }
    const groupOf = Object.fromEntries(GROUPS);
    let first = true;
    for (const g of D.next_up || [{ subject: null, items: D.today }]) {
      for (const i of g.items) {
        const now = D.next_up ? first : i.kind === 'unchecked' || i.kind === 'asked';
        first = false;
        items.push({ time: now ? 'NOW' : 'NEXT', id: i.id, title: i.title, why: i.why || groupOf[i.kind] || '',
          lobe: g.subject ? g.subject.id : pageLobe(i.id), subject: g.subject, state: i.state,
          prompt: i.kind === 'refresh' || i.kind === 'review' ? '/keepfold review' : i.id ? `/keepfold learn ${i.id}` : '/keepfold learn' });
      }
    }
    const dated = (d) => (d === TODAY ? 'TODAY' : fmtDay(d).toUpperCase());
    for (const l of D.loops) {
      if (!isDay(l.due) || l.due > TODAY) continue;
      items.push({ time: dated(l.due), id: l.id, title: l.title, why: 'Open loop', lobe: pageLobe(l.id),
        word: 'OPEN', prompt: loopPrompt(l.id) });
    }
    for (const f of D.follow_ups) {
      if (!isDay(f.review_on) || f.review_on > TODAY) continue;
      items.push({ time: dated(f.review_on), id: f.id, title: f.title, why: 'Decision to revisit',
        lobe: pageLobe(f.id), word: 'DUE', prompt: loopPrompt(f.id) });
    }
    return items;
  }
  // TOPIC: the subject's lobe chip, else a chip of the subject's own title
  function platChip(it) {
    if (!it.subject || LOBES.has(it.lobe)) return lobeChip(it.lobe);
    const s = el('span', 'code', code(it.subject.title));
    s.title = it.subject.title;
    s.setAttribute('aria-label', 'Subject: ' + it.subject.title);
    return s;
  }
  function examRow(a) {
    const n = a.days_left, name = a.company || a.title || a.id;
    // the kind only when the name does not say it already: "DSA exam", but "Google · interview"
    const kind = a.kind && !name.toLowerCase().includes(a.kind.toLowerCase()) ? a.kind : null;
    return brow({ id: a.id, title: [name, kind].filter(Boolean).join(' · '), sub: fmtDay(a.date),
      right: el('span', 'kind', n === 0 ? 'today' : `${plural(n, 'day')} left`),
      prompt: a.kind === 'deadline' ? null : `/keepfold mock ${a.id}`, brainId: a.id });
  }

  /* the live feed: Today or This week, grouped by subject, newest first */
  const FEED = [['today', 'Today'], ['week', 'This week']];
  const feedKey = (i) => [i.ts, i.kind, i.id, i.title].join('\n');
  let feedRange = 'today', feedSeen = null;
  const hhmm = (ts) => {
    const t = new Date(ts);
    return isNaN(t) ? '' : `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
  };
  function feedRows() {
    const items = (D.feed && D.feed[feedRange]) || [];
    if (!items.length) {
      return [el('p', 'empty', `${feedRange === 'today' ? 'Nothing yet today' : 'Nothing earlier this week'}. `
        + 'What you save, study and ask shows up here.')];
    }
    const out = [];
    let subj = null;
    for (const i of items) {
      const s = i.subject ? i.subject.id : '';
      if (s !== subj) { subj = s; out.push(el('h3', 'feed-subj', i.subject ? i.subject.title : 'Other')); }
      const r = brow({ id: i.id, title: i.title, sub: i.detail,
        left: el('span', 'ftime', [feedRange === 'today' ? '' : fmtDay(i.day), i.timed === false ? '' : hhmm(i.ts)].filter(Boolean).join(' ')) });
      if (feedSeen && !feedSeen.has(feedKey(i))) r.classList.add('fresh');  // new since the last render
      out.push(r);
    }
    return out;
  }
  function feedBoard() {
    const tools = el('div', 'band-tools'), seg = el('div', 'seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'Feed range');
    tools.append(seg);
    const b = board('Live feed', { cls: 'feed', action: tools });
    const fill = () => {
      for (const t of seg.children) t.setAttribute('aria-pressed', String(t.dataset.k === feedRange));
      b.body.replaceChildren(...feedRows());
    };
    for (const [k, word] of FEED) {
      const t = button(null, word, () => { feedRange = k; fill(); });
      t.dataset.k = k;
      seg.append(t);
    }
    fill();
    feedSeen = new Set(D.feed ? [...D.feed.today, ...D.feed.week].map(feedKey) : []);
    return b.root;
  }

  /* quick chat: one question to the app's chat, the answer and its numbered sources */
  let chatBoard = null, chatId = null;
  function quickChat() {
    const full = el('a', 'band-btn', 'Open full chat');
    full.href = '/manage#chat';
    const b = board('Quick chat', { cls: 'quick-chat', action: full });
    const lab = el('label', 'qc-label', 'Ask about your notes'), q = el('input'), row = el('div', 'qc-row');
    q.type = 'text';
    q.id = 'qc-q';
    q.autocomplete = 'off';
    lab.htmlFor = 'qc-q';
    const go = button('btn', 'Ask', () => ask());
    row.append(q, go);
    const need = el('div'), out = el('div', 'qc-out');
    out.setAttribute('aria-live', 'polite');
    b.body.append(lab, row, need, out);
    q.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); ask(); } });
    const setUp = (what, why) => {  // what is missing, with the page that sets it up
      const p = el('p', 'qc-need', why + ' '), a = el('a', null, what === 'llm' ? 'Set up a local model' : 'Finish setup');
      a.href = what === 'llm' ? '/manage#llm' : '/manage#setup';
      p.append(a);
      return p;
    };
    api('/api/chat').then((s) => {
      if ((s.missing || []).includes('llm')) {
        need.replaceChildren(setUp('llm', 'No local model is set up yet, so this chat can only count and list what Keepfold keeps: your notes, saved chats, browsing and study.'));
      }
    }, () => {});
    function sources(src) {
      if (!src || !src.length) return [];
      const box = el('div', 'chip-list');
      for (const s of src) {
        const p = D.pages.find((x) => x.path === s.path), label = `${s.n} ${s.title || s.path}`;
        box.append(p ? button('chip', label, () => openReader(p.id), `Open source ${s.n}: ${p.title}`) : el('span', 'chip', label));
      }
      return [el('h4', null, 'Sources'), box];
    }
    let busy = false;
    async function ask() {
      const text = q.value.trim();
      if (!text || busy) return;
      busy = true;
      go.disabled = true;
      out.setAttribute('aria-busy', 'true');
      out.replaceChildren(el('p', 'muted', 'Thinking…'));
      let answer = '', src = [], failed = null;
      try {
        const r = await fetch('/api/chat', { method: 'POST', body: JSON.stringify({ id: chatId, text }),
          headers: { 'X-Keepfold-Token': KEY, 'Content-Type': 'application/json' } });
        if (!r.ok) {
          let d = {};
          try { d = await r.json(); } catch (e) { /* no body */ }
          throw new Error(d.error || `The app answered ${r.status}.`);
        }
        const rd = r.body.getReader(), dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { value, done } = await rd.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, i).trim();
            buf = buf.slice(i + 1);
            if (!line) continue;
            const ev = JSON.parse(line);
            if (ev.id) chatId = ev.id;
            if (ev.t === 'tok') answer += ev.s;
            else if (ev.t === 'text') answer = ev.s;
            else if (ev.t === 'src') src = ev.sources || [];
            else if (ev.t === 'err') failed = ev;
            if ((ev.t === 'tok' || ev.t === 'text') && answer) out.replaceChildren(md(answer));
          }
        }
      } catch (e) {
        failed = { code: 'app', message: e.name === 'TypeError' ? 'The app is not answering.' : e.message };
      }
      busy = false;
      go.disabled = false;
      out.setAttribute('aria-busy', 'false');
      if (failed) {
        const what = failed.code === 'no_local_model' ? 'llm' : failed.code === 'not_set_up' ? 'setup' : null;
        const why = failed.message || 'No answer.';
        out.replaceChildren(what ? setUp(what, why) : el('p', 'qc-need', why));
        return;
      }
      q.value = '';
      out.replaceChildren(md(answer || 'No answer.'), ...sources(src));
    }
    return b.root;
  }

  /* the 90-second check: up to 3 due concepts, recall, reveal, self-grade; a text countdown */
  const COLD_S = 90;
  const COLD_ANSWERS = [['forgot', 'Forgot'], ['shaky', 'Shaky'], ['knew', 'Knew it']];
  function closeCold() {
    if (!cold) return;
    clearInterval(cold.timer);
    cold.root.remove();
    cold = null;
    const opener = $('cold-open');
    (opener || $('view-home')).focus();
  }
  async function openCold() {
    if (cold) return;
    const close = button('band-btn', 'Close', closeCold, 'Close the 90-second check');
    const b = board('90-second check', { cls: 'cold', action: close });
    const clock = el('p', 'cold-timer');
    clock.setAttribute('role', 'timer');
    b.body.append(clock);
    cold = { root: b.root, timer: null };
    $('departures').prepend(b.root);
    let items = [];
    try { items = (await api('/api/home/check')).items || []; } catch (e) {
      clock.textContent = 'The check could not load. ' + e.message;
      close.focus();
      return;
    }
    if (!cold || cold.root !== b.root) return;  // closed while loading
    if (!items.length) {
      clock.textContent = 'Nothing is due right now.';
      close.focus();
      return;
    }
    const open = new Set(), state = cold;  // answer buttons of items not answered yet; this panel
    let left = COLD_S;
    const tick = () => { clock.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')} left`; };
    const finish = (text) => {
      clearInterval(state.timer);  // the panel may be closed by now
      for (const btn of open) btn.disabled = true;
      open.clear();
      clock.textContent = text;
    };
    for (const it of items) {
      const box = el('div', 'cold-item'), acts = el('div', 'cold-acts'), said = el('p', 'muted');
      said.setAttribute('aria-live', 'polite');
      const sum = el('p', 'cold-sum', it.summary || 'Its page has no summary yet.');
      sum.hidden = true;
      acts.hidden = true;
      let shownAt = 0;  // answer time is shown here only: never sent, never stored
      const reveal = button('btn', 'Reveal', () => {
        shownAt = performance.now();
        sum.hidden = false;
        acts.hidden = false;
        reveal.remove();
        acts.firstChild.focus();
      }, `Reveal what you wrote about ${it.title}`);
      const btns = COLD_ANSWERS.map(([k, word]) => button('btn', word, async () => {
        btns.forEach((x) => { x.disabled = true; open.delete(x); });
        const took = `answered in ${Math.round((performance.now() - shownAt) / 1000)} s`;
        try {
          await api('/api/home/check/answer', { id: it.id, answer: k });
          said.textContent = `Saved: ${word}, ${took}.`;
        } catch (e) {
          said.textContent = `Not saved, ${took}. ` + e.message;
        }
        if (!open.size && left > 0) finish('Done. Your answers are saved.');
      }, `${word}: ${it.title}`));
      btns.forEach((x) => open.add(x));
      acts.append(...btns);
      box.append(el('h3', null, it.title), el('p', null, 'Recall what you know, then reveal.'), reveal, sum, acts, said);
      b.body.append(box);
    }
    tick();
    state.timer = setInterval(() => {
      left -= 1;
      if (left > 0) tick();
      else finish('Time is up. Unanswered ones are not recorded.');
    }, 1000);
    b.body.querySelector('.cold-item .btn').focus();
  }

  /* B2-b: a chat export found in Downloads, else the claude.ai export reminder (/api/import/pickup;
     the server looks at Downloads once a minute at most). One card at a time, above Next up. */
  let pickupEl = null, pickupKey = '';
  function pickupBoard(p) {
    const f = p && p.found;
    if (!f && !(p && p.remind)) return null;
    const b = board(f ? 'Found in Downloads' : 'claude.ai chats', { cls: 'pickup' });
    const what = f && f.source === 'google-activity' ? 'history' : 'chats';  // Search, YouTube, Chrome
    const r = brow(f ? { title: `You downloaded a ${f.name} export on ${fmtDay(f.day)}. Add its ${what} to Keepfold?`, sub: f.host ? `${f.file} · from ${f.host}` : f.file }
      : { title: 'Export your claude.ai chats so Keepfold keeps all of them (2 minutes)' });
    const close = () => { b.root.remove(); pickupEl = null; $('view-home').focus(); };
    const post = (path, body, said) => async () => {
      try { await api(path, body); } catch (e) { toast(e.message, 'error'); return; }
      close();
      if (said) toast(said);
    };
    r.querySelector('.acts').append(...(f
      ? [button('btn', `Add ${what}`, post('/api/import/pickup/add', { path: f.path }, `Adding the ${what}. Setup shows when it is done.`)),
        button('btn', 'Not this one', post('/api/import/pickup/dismiss', { path: f.path }))]
      : [button('btn', 'Show me how', () => location.assign('/manage#import-claude')),
        button('btn', 'Later', post('/api/import/pickup/later', {}))]));
    b.body.append(r);
    return b.root;
  }
  async function loadPickup() {
    if (document.hidden) return;
    let p;
    try { p = await api('/api/import/pickup'); } catch (e) { return; }
    const key = JSON.stringify(p);
    if (key === pickupKey && (pickupEl || !(p.found || p.remind))) return;  // unchanged: focus stays
    pickupKey = key;
    if (pickupEl) pickupEl.remove();
    pickupEl = pickupBoard(p);
    if (pickupEl) $('departures').prepend(pickupEl);
  }
  if (LIVE) {
    loadPickup();
    setInterval(loadPickup, 60000);
  }

  /* R31: Add a date on the Exams and deadlines board (POST /api/home/date, an assessment page as
     an AI app's keepfold_add makes it), and calendar events that look like exams, each added in a
     click. The open form is one element kept across renders, so a live update keeps its text. */
  let dateForm = null;
  async function addDate(body) {
    const r = await api('/api/home/date', body);
    toast(`Added: ${r.title} on ${fmtDay(r.date)}.`);
    poll();  // the board shows the new row from the next data load
  }
  function dateOpener() {
    const f = el('div', 'foot add-date');
    const b = button('linkbtn', 'Add a date', openDateForm);
    b.id = 'date-open';
    f.append(b);
    return f;
  }
  function closeDateForm() {
    if (!dateForm) return;
    const f = dateOpener();
    dateForm.replaceWith(f);
    dateForm = null;
    f.firstChild.focus();
  }
  function openDateForm() {
    const form = el('form', 'date-form');
    form.noValidate = true;  // the server's words, in the toast, for every refusal
    const field = (id, label, input) => {
      const box = el('div', 'df-field'), lab = el('label', 'qc-label', label);
      input.id = id;
      lab.htmlFor = id;
      box.append(lab, input);
      return box;
    };
    const what = el('input'), when = el('input'), kind = el('select');
    what.type = 'text';
    what.required = true;
    what.maxLength = 120;
    what.placeholder = 'DSA exam';
    what.autocomplete = 'off';
    when.type = 'date';
    when.required = true;
    when.min = TODAY;
    for (const [v, t] of [['exam', 'Exam'], ['deadline', 'Deadline']]) {
      const o = el('option', null, t);
      o.value = v;
      kind.append(o);
    }
    const add = el('button', 'btn lit', 'Add'), acts = el('div', 'df-acts');
    add.type = 'submit';
    acts.append(add, button('btn', 'Cancel', closeDateForm));
    const whatBox = field('date-what', 'What', what);
    whatBox.classList.add('df-what');
    form.append(whatBox, field('date-when', 'When', when), field('date-kind', 'Exam or deadline', kind), acts);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (add.disabled) return;
      add.disabled = true;
      try {
        await addDate({ what: what.value, when: when.value, kind: kind.value });
      } catch (err) {
        add.disabled = false;
        toast(err.message, 'error');
        return;
      }
      closeDateForm();
    });
    form.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDateForm(); } });
    const opener = $('date-open');
    if (opener) opener.parentNode.replaceWith(form);
    dateForm = form;
    what.focus();
  }
  // the board's live extras: the suggestions under its rows, then the opener or the open form;
  // returns what puts the focus back once the board is on the page
  function dateExtras(b) {
    const sug = D.exam_suggestions || [];
    if (sug.length) b.body.append(el('h3', 'feed-subj', 'From your calendar'));
    for (const s of sug) {
      const word = s.kind === 'deadline' ? 'deadline' : 'exam';
      const go = button('btn', `Add as ${word}`, async () => {
        go.disabled = true;
        try { await addDate({ what: s.title, when: s.day, kind: word }); } catch (e) { go.disabled = false; toast(e.message, 'error'); }
      }, `Add as ${word}: ${s.title}`);
      b.body.append(brow({ title: s.title, sub: fmtDay(s.day), right: go }));
    }
    if (!dateForm) { b.root.append(dateOpener()); return null; }
    const act = dateForm.contains(document.activeElement) ? document.activeElement : null;
    b.root.append(dateForm);
    return act && (() => act.focus({ preventScroll: true }));
  }

  let flicked = false;
  function renderHome() {
    const items = nextUp(), host = $('departures');
    const coldBtn = LIVE && D.cold_check ? button('band-btn', '90-second check', openCold,
      'Start a 90-second check of what is due') : null;
    if (coldBtn) coldBtn.id = 'cold-open';
    const b = board('Next up', { cls: 'departures', action: coldBtn });
    const times = [];
    // nothing due: only the one-line message below, no header and no idle rows that look like loading
    if (items.length) {
      const head = el('div', 'dep-head');
      for (const h of ['TIME', 'ITEM', 'TOPIC', 'STATUS', '']) head.append(el('span', null, h));
      b.body.append(head);
    }
    for (let k = 0; k < (items.length ? 8 : 0); k++) {
      const it = items[k], r = el('div', 'dep-row' + (it ? '' : ' idle'));
      const tc = el('div', 'tcell'), d = dm(it ? it.time : '     ', 2, it ? it.time : 'empty');
      tc.append(d);
      times.push(it ? d : null);
      if (!it) {
        r.setAttribute('aria-hidden', 'true');
        r.append(tc, el('div'), el('div', 'plat'), el('div', 'status'), el('div', 'acts'));
        b.body.append(r);
        continue;
      }
      const item = el('div', 't');
      item.append(PAGES.has(it.id) || GN.has(it.id) ? button('ttl linkbtn', it.title, () => openReader(it.id))
        : el('span', 'ttl', it.title), el('span', 'sub', it.why));
      const plat = el('div', 'plat');
      plat.append(platChip(it));
      const st = el('div', 'status' + (it.word ? ' word' : ''));
      if (it.word) st.textContent = it.word; else st.append(stateWord(it.state));
      const acts = el('div', 'acts');
      acts.append(copyBtn(it.prompt));
      if (PAGES.has(it.id)) acts.append(iconBtn('target', 'Show in brain', () => showInBrain(it.id)));
      const lb = lensBtn(it.id, it.title);
      if (lb) acts.append(lb);
      r.append(tc, item, plat, st, acts);
      if (PAGES.has(it.id)) syncRow(r, it.id);
      b.body.append(r);
    }
    if (!items.length) b.root.append(el('div', 'foot', STUDIES ? 'Board clear. Nothing due. Run /keepfold in Claude to start something new.'
      : 'Nothing next. Open loops and decisions that come due show here.'));
    else if (items.length > 8) b.root.append(STUDIES ? moreFoot(items.length - 8, 'Study', 'study') : moreFoot(items.length - 8, 'Ideas', 'ideas'));
    else b.root.append(el('div', 'foot', 'That is everything due today.'));
    // how long each subject stays sharp (9 in 10 recalls right), as one plain line (home._keep)
    const keeps = (D.next_up || []).filter((g) => g.subject && g.keep);
    if (keeps.length) {
      const k = el('div', 'foot keep');
      for (const g of keeps) k.append(el('div', null, `${g.subject.title}: ${g.keep.text}`));
      b.root.append(k);
    }
    // an open 90-second check stays where it is (a live update never takes its focus or clock)
    for (const c of [...host.children]) if ((!cold || c !== cold.root) && c !== pickupEl) c.remove();
    // `imported` is computed only for someone who does not study; Get started, when it shows, is
    // the one first-run board
    const started = getStarted();
    host.append(!started && (STUDIES ? D.first_run : !D.imported) && !items.length ? firstRunBoard() : b.root);
    if (!flicked && !REDUCED.matches) {  // the one authored motion: a split-flap flick, on the first render only
      flicked = true;
      times.forEach((d, k) => {
        if (!d) return;
        setTimeout(() => {
          dmSet(d, [...d.dataset.text].map((c) => (c === ' ' ? c : SCRAMBLE[Math.floor(Math.random() * SCRAMBLE.length)])).join(''));
          requestAnimationFrame(() => requestAnimationFrame(() => dmSet(d, d.dataset.text)));
        }, k * 40);
      });
    }

    const lobes = D.clusters.filter((c) => c.count > 0).length;
    $('lobe-line').textContent = `${D.pages.length.toLocaleString('en')} pages in ${plural(lobes, 'lobe')}`;

    const exams = (D.exams || D.assessments.filter((a) => isDay(a.date))).slice().sort((a, c) => a.days_left - c.days_left);
    const eb = STUDIES ? board('Exams and deadlines') : null;
    const examsBoard = eb ? boardList(eb, exams.map(onHome(examRow)), 4,
      'Nothing scheduled. Tell any Claude chat about a test and its date.', ['Study', 'study']) : null;
    const refocus = eb && LIVE ? dateExtras(eb) : null;  // the snapshot cannot add a date
    const loops = boardList(board('Open loops'), D.loops.map(onHome(loopRow)), 5,
      "No open loops. Say 'remind me to…' in any Claude chat.", ['Ideas', 'ideas']);
    const back = boardList(board('Back on your mind'), D.resurface.map(onHome(resurfaceRow)), 3,
      'Nothing coming back today. Old ideas and decisions return here when they are due another look.',
      ['Ideas', 'ideas']);
    const boards = { study: examsBoard, build: loops, research: back };
    const g = homeGroups().map((k) => boards[k]).filter(Boolean);
    const rowA = $('home-row-a'), rowB = $('home-row-b');
    const cols = (n) => 'home-row ' + ['one', 'one', 'two', 'three'][n];
    if (!LIVE) {  // the snapshot has no feed and no chat
      rowA.className = cols(g.length);
      rowA.replaceChildren(...g);
      rowB.replaceChildren();
      return;
    }
    // as Home always was: one group board (or the first of three) between the feed and the
    // chat, the others in the row below
    const mid = g.length % 2 ? g.shift() : null;
    rowA.className = cols(mid ? 3 : 2);
    // the quick chat is built once: a live update never takes its text or focus
    if (!chatBoard) chatBoard = quickChat();
    for (const c of [...rowA.children]) if (c !== chatBoard) c.remove();
    rowA.prepend(...[feedBoard(), mid].filter(Boolean));
    if (!chatBoard.isConnected) rowA.append(chatBoard);
    rowB.className = cols(g.length);
    rowB.hidden = !g.length;
    rowB.replaceChildren(...g);
    if (refocus) refocus();
    pinLink();
  }

  /* UX-E: Home's group boards (Exams and deadlines: study; Open loops: build; Back on your mind:
     research) in the order of how much of the vault serves each (dashboard.signals; a stable
     sort, so ties keep this order), or as pinned (app.home_layout). A group at 0 hides, study
     exactly as the Study tab does; all at 0 is Home as it always was. */
  const HOME_GROUPS = ['study', 'build', 'research'];
  let PIN = Array.isArray(D.home_layout) ? D.home_layout.filter((k) => HOME_GROUPS.includes(k)) : null;
  function homeOrder() {
    const s = D.signals || {};
    return PIN ? [...PIN, ...HOME_GROUPS.filter((k) => !PIN.includes(k))]
      : HOME_GROUPS.slice().sort((a, b) => (s[b] || 0) - (s[a] || 0));
  }
  function homeGroups() {
    const s = D.signals || {}, none = HOME_GROUPS.every((k) => !s[k]);
    return homeOrder().filter((k) => (k === 'study' ? STUDIES : none || s[k] > 0));
  }
  // Pin layout, at Home's end (the live home only): Home stops reordering until Unpin layout
  let pinEl = null;
  function pinLink() {
    if (!pinEl) {
      pinEl = el('div', 'home-pin');
      $('view-home').append(pinEl);
    }
    const b = button('linkbtn', PIN ? 'Unpin layout' : 'Pin layout', async () => {
      b.disabled = true;
      try {
        const r = await api('/api/home/layout', { order: PIN ? null : homeOrder() });
        PIN = r.home_layout || null;
      } catch (e) { b.disabled = false; toast(e.message, 'error'); return; }
      toast(PIN ? 'Home keeps this layout.' : 'Home orders its boards by itself again.');
      renderHome();
      pinEl.firstChild.focus();
    });
    pinEl.replaceChildren(b);
  }

  /* UX-E: Get started on a new vault (dashboard.get_started, the live home only): three steps,
     each ticked from the vault (`connected` from the app), until all three are ticked or Hide
     (app.first_run_hidden). Built once, so a live update keeps the typed line. */
  const GS_STEPS = ['Tell Keepfold one thing about you', 'Connect an AI app', 'Bring in what you already have'];
  let gs = null, gsTicks = null, FIRST_HIDDEN = !!D.first_run_hidden;
  function gsBoard() {
    const note = el('span', 'band-note');
    const b = board('Get started', { cls: 'get-started', action: note });
    const form = el('form', 'gs-form'), input = el('input'), save = el('button', 'btn lit', 'Save');
    input.type = 'text';
    input.id = 'gs-fact';
    input.maxLength = 300;
    input.autocomplete = 'off';
    input.placeholder = 'Where you work or study, or what you are into';
    input.setAttribute('aria-label', 'One thing about you');
    save.type = 'submit';
    form.append(input, save);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (save.disabled) return;
      save.disabled = true;
      try {
        const r = await api('/api/home/fact', { text: input.value });
        input.value = '';
        toast(r.saved && r.saved.length ? 'Saved to your profile.' : 'Your profile has that line already.');
        poll();  // the tick comes with the next data load
      } catch (err) { toast(err.message, 'error'); }
      save.disabled = false;
    });
    const go = (text, href) => { const a = el('a', 'btn', text); a.href = href; return a; };
    const controls = [form, go('Connect', '/manage#connect'), go('Import', '/manage#setup')];
    const codes = GS_STEPS.map((title, i) => {
      const code = el('span', 'code');
      b.body.append(brow({ title, left: code, right: controls[i] }));
      return code;
    });
    const f = el('div', 'foot');
    f.append(button('linkbtn', 'Hide', async () => {
      try { await api('/api/home/layout', { first_run_hidden: true }); } catch (e) { toast(e.message, 'error'); return; }
      FIRST_HIDDEN = true;
      renderHome();
      toast('Get started is hidden. Settings still has Connect AI apps and Setup.');
      $('view-home').focus({ preventScroll: true });
    }, 'Hide Get started'));
    b.root.append(f);
    return { root: b.root, note, codes };
  }
  function getStarted() {
    const st = LIVE && !FIRST_HIDDEN ? D.get_started : null;
    const ticks = st ? [!!st.fact, !!D.connected, !!st.imported] : null;  // D: the latest poll
    if (ticks && gsTicks) ticks.forEach((t, i) => { if (t && !gsTicks[i]) announce(`Step ${i + 1} of 3 done: ${GS_STEPS[i]}.`); });
    gsTicks = ticks;
    if (!ticks || ticks.every(Boolean)) {
      if (gs) { gs.root.remove(); gs = null; }
      return false;
    }
    if (!gs) gs = gsBoard();
    ticks.forEach((t, i) => {
      gs.codes[i].textContent = t ? '✓' : String(i + 1);
      gs.codes[i].setAttribute('aria-label', t ? 'Done' : `Step ${i + 1}`);
    });
    gs.note.textContent = `${ticks.filter(Boolean).length} of 3 done`;
    if (!gs.root.isConnected) $('view-home').prepend(gs.root);
    return true;
  }

  /* ---- Brain section ---- */
  const KGROUPS = [['concept', 'Concepts'], ['note', 'Notes'], ['project', 'Projects'], ['decision', 'Decisions'],
    ['chat', 'Chats'], ['youtube', 'YouTube'], ['person', 'People'], ['other', 'Other']];
  function applyBrainFilter() {
    const k = brainFilter.kinds, s = brainFilter.states;
    const all = k.size === KGROUPS.length && s.size === STATES.length;
    brain.setFilter(all ? null : (n) => n.kind === 'site' || (k.has(n.group) && (n.kind !== 'concept' || s.has(n.state))));
    for (const b of $('bkinds').children) b.setAttribute('aria-pressed', String(k.has(b.dataset.k)));
    for (const b of $('bstates').children) b.setAttribute('aria-pressed', String(s.has(b.dataset.s)));
  }
  function resetBrainFilters() {
    KGROUPS.forEach(([k]) => brainFilter.kinds.add(k));
    STATES.forEach((s) => brainFilter.states.add(s));
    applyBrainFilter();
  }
  function markLobe(id) {
    $('all-lobes').setAttribute('aria-pressed', String(!id));
    for (const li of $('outline').querySelectorAll('.lobe')) li.setAttribute('aria-selected', String(li.dataset.id === id));
  }
  function markOutline(pageId) {
    if (!brainUi || brainFailed) return;
    for (const li of $('outline').querySelectorAll('.page')) li.setAttribute('aria-selected', String(li.dataset.id === pageId));
  }
  function selectLobe(id) {
    if (!brain) return;
    brain.focusCluster(id);
    markLobe(id);
    inspectLobe(id);
  }
  function renderLegend(m) {
    const lg = $('legend');
    lg.replaceChildren(el('p', 'honest', D.brain.legend || ''));
    if (brain && brain.legendNote) lg.append(el('p', null, brain.legendNote));
    if (m === 'mastery') {
      const sw = el('div', 'sw');
      for (const s of STATES) sw.append(stateWord(s));
      lg.append(sw, el('p', null, 'Concepts take their state; study lobes are tinted from Needs refresh to Solid; other lobes are grey.'));
    } else if (m === 'growth') {
      lg.append(el('p', null, 'Pages appear month by month, dated by the source, not by when it was saved.'));
    } else lg.append(el('p', null, 'A halo marks a page saved or changed in the last 7 days.'));
    const st = D.brain.stem;
    if (st && st.shown) {
      lg.append(el('p', null, `The stem below holds ${plural(st.shown, 'website')} from your browsing: size by visits, `
        + 'brightness by the last visit, colour by category' + (st.sites.some((x) => x[5]) ? ', a hollow ring when private' : '') + '.'
        + (st.total > st.shown ? ` It shows the most visited and those of the last 30 days; the other ${st.total - st.shown} stay in the browsing store.` : '')));
      const key = el('div', 'sw');
      for (const c of Object.keys(st.colors || {}).sort()) {
        const col = String(st.colors[c]);
        if (!/^#[0-9a-f]{6}$/i.test(col)) continue;
        const dot = el('span', 'dot'), item = el('span');
        dot.style.background = col;
        item.append(dot, c.replace(/-/g, ' '));
        key.append(item);
      }
      if (key.childElementCount) lg.append(key);
    }
    lg.append(el('p', null, 'drag to turn · scroll to zoom · click a lobe name for its pages · click a point to inspect it · double-click empty space for the overview'));
    lg.append(el('p', null, 'keys: Esc back · [ ] step (Today: ← → too) · Backspace previous · F frame · 0 reset · arrows turn · + − zoom · A M G modes · S spin'
      + (HAS_STYLE ? ' · L look' : '')));
  }

  /* the outline: region > lobe > page as a WAI tree (an equal route to the canvas) */
  const REGIONS = [['topic', 'Topics'], ['domain', 'Study subjects'], ['group', 'Routines, projects and the rest'],
    ['archive', 'Unsorted chats']];
  const regionOf = (l) => (l.archive ? 'archive' : l.kind === 'topic' || l.kind === 'domain' ? l.kind : 'group');
  function treeItem(cls, level, label, parent) {
    const li = el('li', cls);
    li.setAttribute('role', 'treeitem');
    li.setAttribute('aria-level', String(level));
    li.tabIndex = -1;
    const row = el('div', 'ti');
    row.append(...label);
    li.append(row);
    parent.append(li);
    return li;
  }
  function addGroup(li) {
    const ul = el('ul');
    ul.setAttribute('role', 'group');
    ul.hidden = li.getAttribute('aria-expanded') !== 'true';
    li.append(ul);
    return ul;
  }
  function setExpanded(li, on) {
    if (!li.hasAttribute('aria-expanded')) return;
    li.setAttribute('aria-expanded', String(on));
    let ul = li.querySelector(':scope > ul');
    if (on && li.classList.contains('lobe') && !ul.childElementCount) {  // pages are built on first open
      for (const p of topPagesOf(li.dataset.id, 20)) {
        const pi = treeItem('page', 3, [el('span'), el('span', 'nm', p.title)], ul);
        pi.dataset.id = p.id;
        pi.setAttribute('aria-selected', 'false');
      }
    }
    ul.hidden = !on;
  }
  function activate(li) {
    if (li.classList.contains('region')) setExpanded(li, li.getAttribute('aria-expanded') !== 'true');
    else if (li.classList.contains('lobe')) { setExpanded(li, true); selectLobe(li.dataset.id); }
    else if (li.classList.contains('site')) exploreSite(li.dataset.id);
    else explorePage(li.dataset.id);
  }
  // The tree's rows from D (again on a live update: open lobes and closed regions stay so).
  function fillOutline() {
    const tree = $('outline'), gone = tree.nextElementSibling;
    const okey = (li) => li.dataset.id || li.querySelector('.nm').textContent;
    const was = new Map([...tree.querySelectorAll('[aria-expanded]')].map((li) => [okey(li), li.getAttribute('aria-expanded')]));
    tree.replaceChildren();
    if (gone && gone.classList.contains('empty')) gone.remove();
    const lobes = D.brain.lobes.filter((l) => l.count > 0);
    for (const [r, word] of REGIONS) {
      const ls = lobes.filter((l) => regionOf(l) === r);
      if (!ls.length) continue;
      const n = ls.reduce((a, l) => a + l.count, 0);
      const ri = treeItem('region', 1, [icon('chevron'), el('span', 'nm', word), el('span', 'n', n.toLocaleString('en'))], tree);
      ri.setAttribute('aria-expanded', 'true');
      const rg = addGroup(ri);
      for (const l of ls) {
        const li = treeItem('lobe', 2, [icon('chevron'), el('span', 'code', code(l.title)), el('span', 'nm', l.title),
          el('span', 'n', l.count.toLocaleString('en'))], rg);
        li.dataset.id = l.id;
        li.title = `${l.title} · ${l.count.toLocaleString('en')}`;
        li.setAttribute('aria-label', `${l.title}, ${plural(l.count, 'page')}`);
        li.setAttribute('aria-expanded', 'false');
        li.setAttribute('aria-selected', 'false');
        addGroup(li);
      }
    }
    const st = D.brain.stem;
    if (st && st.shown && Array.isArray(st.sites)) {
      const ri = treeItem('region', 1, [icon('chevron'), el('span', 'nm', 'Browsing'), el('span', 'n', st.shown.toLocaleString('en'))], tree);
      ri.dataset.id = 'region:browsing';
      ri.setAttribute('aria-expanded', 'false');
      const rg = addGroup(ri), visits = (x) => +x[2] || 0;
      for (const x of st.sites.filter(Array.isArray).sort((a, b) => visits(b) - visits(a) || String(a[0]).localeCompare(String(b[0]))).slice(0, 20)) {
        const li = treeItem('page site', 2, [el('span'), el('span', 'nm', String(x[0])), el('span', 'n', visits(x).toLocaleString('en'))], rg);
        li.dataset.id = 'site:' + x[0];
        li.title = `${x[0]} · ${plural(visits(x), 'visit')}`;
        li.setAttribute('aria-label', `${x[0]}, website, ${plural(visits(x), 'visit')}`);
        li.setAttribute('aria-selected', 'false');
      }
    }
    if (!lobes.length) tree.after(el('p', 'empty', 'No lobes yet. Pages you save gather into lobes here.'));
    const f = brain && brain.focused;  // a refill keeps the focused lobe marked
    $('all-lobes').setAttribute('aria-pressed', String(!f));
    for (const li of tree.querySelectorAll('.lobe')) li.setAttribute('aria-selected', String(li.dataset.id === f));
    for (const li of tree.querySelectorAll('[aria-expanded]')) {
      const open = was.get(okey(li));
      if (open && open !== li.getAttribute('aria-expanded')) setExpanded(li, open === 'true');
    }
    const first = tree.querySelector('[role="treeitem"]');
    if (first) first.tabIndex = 0;
  }
  function buildOutline() {
    fillOutline();
    const tree = $('outline');
    const visible =() => [...tree.querySelectorAll('[role="treeitem"]')].filter((li) => !li.parentElement.closest('[role="group"][hidden]'));
    const move = (li) => {
      if (!li) return;
      for (const x of tree.querySelectorAll('[role="treeitem"][tabindex="0"]')) x.tabIndex = -1;
      li.tabIndex = 0;
      li.focus();
    };
    tree.addEventListener('click', (e) => {
      const li = e.target.closest('[role="treeitem"]');
      if (!li) return;
      move(li);
      activate(li);
    });
    tree.addEventListener('keydown', (e) => {
      const li = e.target.closest('[role="treeitem"]');
      if (!li || e.ctrlKey || e.metaKey || e.altKey) return;
      const vis = visible(), i = vis.indexOf(li), open = li.getAttribute('aria-expanded');
      if (e.key === 'ArrowDown') move(vis[i + 1]);
      else if (e.key === 'ArrowUp') move(vis[i - 1]);
      else if (e.key === 'Home') move(vis[0]);
      else if (e.key === 'End') move(vis[vis.length - 1]);
      else if (e.key === 'ArrowRight') {
        if (open === 'false') setExpanded(li, true);
        else if (open === 'true') move(li.querySelector(':scope > ul > [role="treeitem"]'));
      } else if (e.key === 'ArrowLeft') {
        if (open === 'true') setExpanded(li, false);
        else move(li.parentElement.closest('[role="treeitem"]'));
      } else if (e.key === 'Enter' || e.key === ' ') activate(li);
      else return;
      e.preventDefault();
    });
  }

  const LEGEND_KEY = 'keepfold.legend';
  function legendToggle() {
    // the first visit: open where it clears the stem (at 1024x768 it covers it); after it, closed unless opened
    let open = innerWidth >= 1300 && innerHeight >= 900;
    try {
      const v = localStorage.getItem(LEGEND_KEY);
      if (v === null) localStorage.setItem(LEGEND_KEY, 'closed'); else open = v === 'open';
    } catch (e) { /* storage off: the first-visit default */ }
    const t = $('legend-toggle'), set = (on) => { t.setAttribute('aria-expanded', String(on)); $('legend').hidden = !on; };
    t.prepend(icon('chevron'));
    set(open);
    t.addEventListener('click', () => {
      const on = t.getAttribute('aria-expanded') !== 'true';
      set(on);
      try { localStorage.setItem(LEGEND_KEY, on ? 'open' : 'closed'); } catch (e) { /* not kept */ }
    });
  }
  function renderBrainSection() {
    for (const b of $('viewswitch').children) b.addEventListener('click', () => setView(b.dataset.view));
    legendToggle();
    if (brainFailed) {
      $('b2d').prepend(noBrainNote('Showing the 2D map.'));
      return;
    }
    // The toolbar: modes, spin, Today, the unsorted-chat ring, and the Growth scrubber.
    const tools = $('brain-tools');
    const spin = button('btn tbtn', null, () => { if (brain) brain.setSpin(spin.getAttribute('aria-pressed') !== 'true'); });
    spin.textContent = 'Spin';
    spin.title = 'Turn the brain slowly when idle (S in the brain)';
    spin.setAttribute('aria-pressed', 'false');
    spinBtns.push(spin);
    const alive = button('btn tbtn', null, () => { if (brain) brain.setAlive(alive.getAttribute('aria-pressed') !== 'true'); });
    alive.textContent = 'Alive';
    alive.title = 'Neurons fire now and then and waves drift through the topics; sleeps after 2 minutes idle';
    alive.setAttribute('aria-pressed', String(!!(brain && brain.alive)));
    aliveBtns.push(alive);
    const arch = button('btn tbtn', 'Unsorted chats', () => {
      archiveOn = !archiveOn;
      arch.setAttribute('aria-pressed', String(archiveOn));
      if (brain) brain.setArchive(archiveOn);
    });
    arch.title = 'Show the unsorted chats as a faint ring under the brain';
    arch.setAttribute('aria-pressed', String(archiveOn));
    const scrub = buildScrub().root;
    scrub.id = 'scrub';
    tools.append(modeGroup(), spin, alive, todayBtn('btn'), stemBtn(), pictureBtn(), arch);
    tools.addEventListener('click', syncStem);  // another flight (Today) leaves the stem
    if (HAS_STYLE) tools.append(lookBtn());
    tools.append(scrub);
    buildOutline();
    $('all-lobes').addEventListener('click', () => { if (brain) brain.focusCluster(null); markLobe(null); });
    $('outline-toggle').addEventListener('click', () => {
      const on = $('outline-toggle').getAttribute('aria-expanded') !== 'true';
      $('outline-toggle').setAttribute('aria-expanded', String(on));
      $('outline-box').hidden = !on;
    });
    if (!ensureBrain()) return;
    for (const [k, word] of KGROUPS) {
      const b = button(null, word, () => {
        if (brainFilter.kinds.has(k)) brainFilter.kinds.delete(k); else brainFilter.kinds.add(k);
        applyBrainFilter();
      });
      b.dataset.k = k;
      $('bkinds').append(b);
    }
    for (const s of STATES) {
      const b = button(null, null, () => {
        if (brainFilter.states.has(s)) brainFilter.states.delete(s); else brainFilter.states.add(s);
        applyBrainFilter();
      });
      b.dataset.s = s;
      b.append(glyph(s), ' ' + LABEL[s]);
      $('bstates').append(b);
    }
    applyBrainFilter();
    markLobe(null);
    let timer = 0;
    $('bq').addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const msg = findInBrain();
        $('bq-note').textContent = msg;
        if (msg) live(msg);
      }, 160);
    });
    $('rail-toggle').addEventListener('click', () => {
      const open = $('rail').classList.toggle('closed');
      $('rail-toggle').setAttribute('aria-expanded', String(!open));
      $('rail-body').hidden = open;
    });
    if (phone()) $('rail-toggle').click();
    syncMode(brain.mode);
  }
  // the Find box's matches lit in the brain (an empty box: none); its message
  function findInBrain() {
    const q = $('bq').value.trim();
    if (!q) { brain.highlight(null); return ''; }
    const hits = search(q);
    brain.highlight(hits.map((h) => h.h.p.id));
    return hits.length ? `${plural(hits.length, 'page')} lit` : 'No pages match.';
  }
  let brainUi = false;
  function ensureBrainUi() { if (!brainUi) { brainUi = true; renderBrainSection(); } }
  function setView(v) {
    ensureBrainUi();
    if (v === '3d' && brainFailed) v = '2d';
    view = v;
    for (const b of $('viewswitch').children) b.setAttribute('aria-pressed', String(b.dataset.view === v));
    $('b3d').hidden = v !== '3d';
    $('b2d').hidden = v === '3d';
    hideTip();
    if (current !== 'brain') return;
    if (v === '3d') placeBrain('brain-slot');
    if (v === '3d' && brainFailed) { setView('2d'); return; }  // the mount just failed
    if (v !== '3d') { closeLook(false); G.init(); G.setList(v === 'list'); }
  }

  /* ---- Library ---- */
  const PRESETS = [['all', 'All'], ['project', 'Projects'], ['note', 'Notes'], ['decision', 'Decisions'],
    ['reading', 'Reading'], ['chat', 'Chats'], ['youtube', 'YouTube'], ['person', 'People'], ['topic', 'Topics']];
  const LIB = { q: '', preset: 'all', lobe: '', tag: '', sort: 'date', shown: 100, hits: null };
  function inPreset(p) {
    const k = LIB.preset;
    if (k === 'all') return true;
    if (k === 'topic') return p.tags.includes('topic');
    if (k === 'reading') return p.kind === 'resource' && kindGroup(p) === 'other';
    if (k === 'chat' || k === 'youtube') return kindGroup(p) === k;
    return p.kind === k;
  }
  // Brain -> Library: a lobe clicked in the brain filters the Library (a chip clears it).
  function filterLibraryLobe(id) {
    LIB.lobe = id || '';
    LIB.shown = 100;
    $('llobe').value = LIB.lobe;
    renderLibraryRows();
  }
  function libraryFind(text) {
    $('lq').value = text;
    LIB.q = text;
    LIB.hits = new Set(search(text).map((h) => h.h.p.id));
    LIB.shown = 100;
    show('library');
    renderLibraryRows();
  }
  function setTag(t) {
    LIB.tag = t;
    LIB.shown = 100;
    show('library');
    renderLibraryRows();
  }
  function renderLibrary() {
    const pr = $('lpresets');
    for (const [k, word] of PRESETS) {
      const b = button(null, word, () => { LIB.preset = k; LIB.shown = 100; renderLibraryRows(); });
      b.dataset.k = k;
      pr.append(b);
    }
    const lob = $('llobe');
    fillLobes();
    lob.addEventListener('change', () => { LIB.lobe = lob.value; LIB.shown = 100; renderLibraryRows(); });
    $('lsort').addEventListener('change', () => { LIB.sort = $('lsort').value; renderLibraryRows(); });
    let timer = 0;
    $('lq').addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        LIB.q = $('lq').value.trim();
        LIB.hits = LIB.q ? new Set(search(LIB.q).map((h) => h.h.p.id)) : null;
        LIB.shown = 100;
        renderLibraryRows();
      }, 160);
    });
    $('lmore').addEventListener('click', () => { LIB.shown += 100; renderLibraryRows(); });
    renderLibraryRows();
  }
  function fillLobes() {
    const lob = $('llobe');
    lob.replaceChildren(lob.options[0]);  // All lobes
    for (const c of D.clusters) {
      if (!c.count) continue;
      const o = el('option', null, `${code(c.title)} · ${c.title} (${c.count})`);
      o.value = c.id;
      lob.append(o);
    }
    lob.disabled = lob.options.length < 2;  // no lobes yet: nothing to choose
  }
  function renderLibraryRows() {
    if (!rendered.has('library')) return;
    for (const b of $('lpresets').children) b.setAttribute('aria-pressed', String(b.dataset.k === LIB.preset));
    const tagBox = $('ltag');
    tagBox.hidden = !LIB.tag;
    tagBox.replaceChildren();
    if (LIB.tag) {
      tagBox.append(el('span', null, 'Tagged'), el('span', 'chip', '#' + LIB.tag),
        button('linkbtn', 'Clear tag', () => { LIB.tag = ''; renderLibraryRows(); }));
    }
    const chip = $('llobe-chip'), lobe = LOBES.get(LIB.lobe);
    $('llobe').value = LIB.lobe;
    chip.hidden = !lobe;
    chip.replaceChildren();
    if (lobe) {
      const x = button('chip', null, () => filterLibraryLobe(''), 'Clear the lobe filter');
      x.append('Filtered by ' + lobe.title + ' ', icon('close'));
      chip.append(x);
    }
    const rows = D.pages.filter((p) => inPreset(p) && (!LIB.lobe || p.cluster === LIB.lobe)
      && (!LIB.tag || p.tags.includes(LIB.tag)) && (!LIB.hits || LIB.hits.has(p.id)));
    const key = LIB.sort === 'title' ? null : LIB.sort === 'updated' ? (p) => p.updated : dateOf;
    rows.sort(key ? (a, b) => { const x = key(a) || '', y = key(b) || ''; return x < y ? 1 : x > y ? -1 : a.title.localeCompare(b.title); }
      : (a, b) => a.title.localeCompare(b.title));
    $('lcount').textContent = `${rows.length.toLocaleString('en')} of ${D.pages.length.toLocaleString('en')} pages`
      + (rows.length > LIB.shown ? `, showing the first ${LIB.shown.toLocaleString('en')}` : '');
    if (!rows.length) {
      $('ltable').replaceChildren(el('p', 'empty', D.pages.length
        ? 'No pages match. Clear the search, preset, lobe or tag to see more.'
        : 'No pages yet. Import your chats or notes, or save something from a Claude chat.'));
      $('lmore').hidden = true;
      return;
    }
    const table = el('table'), thead = el('thead'), htr = el('tr'), tbody = el('tbody');
    for (const h of ['DATE', 'TITLE', 'KIND', 'TOPIC', 'SOURCE', 'UPDATED']) {
      const th = el('th', null, h);
      th.scope = 'col';
      htr.append(th);
    }
    thead.append(htr);
    for (const p of rows.slice(0, LIB.shown)) {
      const tr = el('tr'), tc = el('td', 'ttlc');
      tc.append(button(null, p.title, (e) => { e.stopPropagation(); openReader(p.id); }));
      if (p.summary) tc.append(el('span', 'sum', p.summary));
      const pc = el('td');
      pc.append(lobeChip(p.cluster));
      tr.append(el('td', 'nowrap', dateOf(p) ? fmtDay(dateOf(p)) + ' ' + dateOf(p).slice(0, 4) : ''), tc,
        el('td', 'nowrap', kindWord(p.kind)), pc, el('td', 'nowrap', p.source || ''), el('td', 'nowrap', fmtDay(p.updated)));
      tr.addEventListener('click', () => openReader(p.id));
      syncRow(tr, p.id);
      tbody.append(tr);
    }
    table.append(thead, tbody);
    $('ltable').replaceChildren(table);
    $('lmore').hidden = rows.length <= LIB.shown;
  }

  /* ---- You ---- */
  const TIER_ROWS = [['you confirmed', ['confirmed', 'typed', 'legacy'], false],
    ['a chat relayed (ask)', ['agent'], true], ['a chat saved (ask)', ['exposed'], true]];
  /* Mind Mirror (R27a): each line about you or decision and what replaced it, one lane per
     chain on a month axis. The list view is its table, and the narrow screen's default. */
  const MM = { all: false, list: null, SHOWN: 8, LANE: 36, AXIS: 22, END: 56, MIN_TEXT: 48 };
  const fullDay = (s) => `${fmtDay(s)} ${s.slice(0, 4)}`;
  const mmRange = (v) => `${v.open_start ? 'an unknown day' : fullDay(v.from)} to ${v.until ? fullDay(v.until) : 'now'}`;
  const mmTitle = (id) => (PAGES.has(id) ? PAGES.get(id).title : id);
  const utcDay = (s) => new Date(dayNum(s) * 864e5);
  function mindMirror() {
    const chains = (D.you && D.you.changes) || [];
    if (MM.list === null) MM.list = window.matchMedia('(max-width: 559px)').matches;
    const toggle = chains.length ? button('band-btn', '', () => { hideTip(); MM.list = !MM.list; draw(); }) : null;
    const b = board('Lines and decisions you replaced',
      { note: chains.length ? plural(chains.length, 'change') : '', action: toggle });
    if (!chains.length) {
      b.body.append(el('p', 'empty', 'Nothing has changed yet. When you revise a line about you or replace a decision, both versions show here.'));
      return b.root;
    }
    const foot = el('div', 'foot');
    b.root.append(foot);
    let width = 0;
    function draw(expanded) {
      toggle.textContent = MM.list ? 'Show as a timeline' : 'Show as a list';
      const shown = MM.all ? chains : chains.slice(0, MM.SHOWN);
      b.body.replaceChildren(MM.list ? mmList(shown) : mmLanes(shown, chains, width));
      foot.replaceChildren();
      if (shown.length < chains.length) {
        foot.append(button('linkbtn', `Show all ${chains.length}`, () => { MM.all = true; draw(true); }));
      }
      foot.hidden = !foot.childNodes.length;
      const next = expanded && b.body.querySelector('.mm-first');
      if (next) next.focus();  // the button that had focus is gone: the first row it showed takes it
    }
    draw();
    new ResizeObserver(() => {
      const w = b.body.clientWidth;
      if (w && w !== width) { width = w; if (!MM.list) draw(); }
    }).observe(b.body);
    return b.root;
  }
  function mmList(shown) {
    const ul = el('ul', 'mm-list');
    shown.forEach((c, i) => {
      const li = el('li'), first = i === MM.SHOWN ? ' mm-first' : '';
      li.append(PAGES.has(c.page) ? button('linkbtn' + first, mmTitle(c.page), () => openReader(c.page))
        : el('span', null, mmTitle(c.page)));
      li.append(el('div', 'mm-chain', c.versions.map((v) =>
        `${v.text} (${mmRange(v)}${v.status === 'retired' ? ', retired' : ''})`).join(' → ')));
      ul.append(li);
    });
    return ul;
  }
  function mmLanes(shown, all, W) {
    const box = el('div', 'mm');
    box.style.height = (MM.AXIS + shown.length * MM.LANE) + 'px';
    const x0 = 4, x1 = W - 24 - MM.END;  // .mm's side margins
    if (x1 - x0 < 40) return box;  // not laid out yet: the resize observer draws it
    const hi = utcDay(TODAY), first = Math.min(...all.flatMap((c) => c.versions.map((v) => dayNum(v.from))));
    const lo = d3.utcMonth.floor(new Date(Math.min(first * 864e5, +d3.utcMonth.offset(hi, -1))));
    const x = d3.scaleUtc().domain([lo, hi]).range([x0, x1]).clamp(true);
    const months = Math.max(1, d3.utcMonth.count(lo, hi));
    const step = [1, 2, 3, 4, 6, 12].find((k) => (x1 - x0) * k / months >= 56) || 12;
    x.ticks(d3.utcMonth.every(step)).forEach((t, i) => {
      const tick = el('div', 'mm-tick');
      tick.style.left = x(t) + 'px';
      tick.append(el('span', null, MON[t.getUTCMonth()] + (i === 0 || t.getUTCMonth() === 0 ? ' ' + t.getUTCFullYear() : '')));
      tick.setAttribute('aria-hidden', 'true');
      box.append(tick);
    });
    shown.forEach((c, li) => {
      const lane = el('div', 'mm-lane');
      lane.style.top = (MM.AXIS + li * MM.LANE) + 'px';
      let end = -Infinity;
      c.versions.forEach((v, vi) => {
        const a = Math.max(x(utcDay(v.from)), end + 2), z = Math.max(v.until ? x(utcDay(v.until)) : x1, a + 4);
        const bar = el('div', 'mm-bar ' + (v.status === 'live' ? 'live' : 'past') + (v.open_start ? ' open' : '')
          + (li === MM.SHOWN && vi === 0 ? ' mm-first' : ''));
        Object.assign(bar.style, { left: a + 'px', width: (z - a) + 'px' });
        bar.tabIndex = 0;
        bar.setAttribute('role', 'button');
        const span = `from ${mmRange(v)}${v.status === 'retired' ? ', then retired' : ''}`;
        bar.setAttribute('aria-label', `${v.text}, ${span}, on ${mmTitle(v.page)}`);
        const tipParts = () => [el('div', null, v.text), el('div', 'k', span), el('div', 'k', mmTitle(v.page))];
        bar.addEventListener('mouseenter', (e) => showTip(e, tipParts()));
        bar.addEventListener('mousemove', moveTip);
        bar.addEventListener('mouseleave', hideTip);
        bar.addEventListener('focus', () => { const r = bar.getBoundingClientRect(); showTipAt(r.left, r.bottom, tipParts()); });
        bar.addEventListener('blur', hideTip);
        bar.addEventListener('click', () => { hideTip(); openReader(v.page); });
        bar.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter' && e.key !== ' ') return;
          e.preventDefault();
          hideTip();
          openReader(v.page);
        });
        lane.append(bar);
        if (z - a >= MM.MIN_TEXT) {
          const t = el('div', 'mm-text', v.text);
          Object.assign(t.style, { left: a + 'px', width: (z - a) + 'px' });
          t.setAttribute('aria-hidden', 'true');
          lane.append(t);
        }
        end = z;
      });
      const last = c.versions[c.versions.length - 1].status;
      if (last === 'retired') {
        const cap = el('div', 'mm-cap');
        cap.style.left = (end + 1) + 'px';
        lane.append(cap);
      }
      if (last === 'live' || last === 'retired') {
        const tag = el('span', 'mm-end', last === 'live' ? 'now' : 'retired');
        tag.style.left = (end + 6) + 'px';
        tag.setAttribute('aria-hidden', 'true');
        lane.append(tag);
      }
      box.append(lane);
    });
    return box;
  }
  function renderYou() {
    const host = $('view-you'), know = D.you.know || { pages: [], total: { tiers: {} } };
    host.append(el('h1', null, NAME || 'You'));
    const tiers = (t) => TIER_ROWS.map(([, keys]) => keys.reduce((a, k) => a + ((t || {})[k] || 0), 0));
    const pagesWith = know.pages.filter((p) => !p.private && p.live).length;
    const lead = el('p', 'muted');
    lead.textContent = `Keepfold has ${plural(tiers(know.total.tiers)[0], 'confirmed line')} about you across ${plural(pagesWith, 'page')}.`;
    host.append(lead);

    const grid = el('div', 'you-grid');
    const wait = board('Waiting for your yes', { action: copyBtn('/keepfold reflect', 'band-btn') });
    const un = D.you.unconfirmed || [];
    if (!un.length) wait.body.append(el('p', 'empty', 'Nothing waiting. Lines a chat proposes about you wait here for your yes.'));
    for (const u of un) {
      const r = el('div', 'brow');
      const t = el('div', 't');
      t.append(el('div', null, u.text), el('span', 'sub', u.since ? 'proposed ' + fmtDay(u.since) : 'in inbox.md'));
      r.append(t, el('div'));
      wait.body.append(r);
    }
    wait.root.append(el('div', 'foot', 'Copy /keepfold reflect and paste it into Claude to go through them.'));

    const kb = board('How well Keepfold knows you');
    const rows = know.pages.filter((p) => !p.private);
    const max = Math.max(1, ...rows.map((p) => Math.max(...tiers(p.tiers))));
    const box = el('div', 'tiers');
    for (const p of know.pages) {
      const r = el('div', 'trow2');
      const title = PAGES.has(p.page) ? button('linkbtn', PAGES.get(p.page).title, () => openReader(p.page)) : el('span', null, p.page);
      r.append(title);
      const bars = el('div');
      if (p.private) bars.append(el('span', 'sub', 'A private page; not shown here.'));
      else {
        tiers(p.tiers).forEach((v, k) => {
          const b = el('div', 'tbar'), tr = el('div', 'track' + (TIER_ROWS[k][2] ? ' ask' : '')), f = el('span');
          f.style.setProperty('--f', String(v / max));  // the fill is scaleX(--f)
          tr.append(f);
          tr.setAttribute('role', 'img');
          tr.setAttribute('aria-label', `${v} lines ${TIER_ROWS[k][0]}`);
          b.append(el('span', null, TIER_ROWS[k][0]), tr, el('span', 'v', v));
          bars.append(b);
        });
      }
      r.append(bars);
      box.append(r);
    }
    if (!know.pages.length) box.append(el('p', 'empty', 'No pages about you yet. Tell any Claude chat about yourself.'));
    kb.body.append(box);
    grid.append(wait.root, kb.root);
    host.append(grid);

    host.append(el('h2', null, 'How your mind changed'), mindMirror());

    host.append(el('h2', null, 'Your pages'));
    const g2 = el('div', 'you-grid');
    const me = D.pages.filter((p) => p.kind === 'me').sort((a, b) => (a.id === 'profile' ? -1 : b.id === 'profile' ? 1 : a.title.localeCompare(b.title)));
    const lb = board('About you', { note: plural(me.length, 'page') });
    const view = board('Profile');
    const peeks = [];
    const showMe = (p) => {
      for (const [b, id] of peeks) b.setAttribute('aria-pressed', String(id === p.id));  // which page is beside
      view.band.firstChild.textContent = p.title;
      const body = el('div', 'md me-view');
      body.append(md(p.body));
      view.body.replaceChildren(body);
    };
    if (!me.length) lb.body.append(el('p', 'empty', 'No me/ pages yet. Tell any Claude chat about yourself.'));
    for (const p of me) {
      const r = brow({ id: p.id, title: p.title, sub: p.summary || '', prompt: `/keepfold about ${p.id}`, brainId: p.id });
      const peek = iconBtn('chevron', 'Show beside the list', () => showMe(p));
      peeks.push([peek, p.id]);
      r.lastChild.prepend(peek);
      lb.body.append(r);
    }
    const prof = me.find((p) => p.id === 'profile') || me[0];
    if (prof) showMe(prof); else view.body.append(el('p', 'empty', 'No profile yet. Tell any Claude chat about yourself.'));
    g2.append(lb.root, view.root);
    host.append(g2);

    host.append(el('h2', null, 'Patterns'));
    const pats = D.pages.filter((p) => p.tags.includes('patterns'));
    host.append(boardList(board('Patterns Keepfold has noticed'), pats.map((p) => brow({ id: p.id, title: p.title,
      sub: p.summary || '', brainId: p.id })), 50,
    'No pattern notes yet. Ask a Claude chat to look for patterns in your imports.'));
  }

  /* ---- Timeline ---- */
  const STEP = (n) => (n >= 8 ? 3 : n >= 3 ? 2 : n >= 1 ? 1 : 0);  // fixed thresholds: 1, 3, 8+
  function renderTimeline() {
    const host = $('view-timeline');
    host.append(el('h1', null, 'Timeline'));
    const byDay = new Map();
    for (const p of D.pages) {
      if (!isDay(p.date)) continue;
      const d = p.date.slice(0, 10);
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d).push(p);
    }
    if (!byDay.size) {
      host.append(el('p', 'empty', 'No dated pages yet. Import your chats or keep a journal and they appear here.'));
      return;
    }
    const days = [...byDay.keys()].sort();
    const last = days[days.length - 1] > TODAY ? TODAY : days[days.length - 1];
    let y = +last.slice(0, 4), m = +last.slice(5, 7);
    const first = days[0].slice(0, 7), months = [];
    while (months.length < 48) {
      const key = `${y}-${String(m).padStart(2, '0')}`;
      months.push(key);
      if (key <= first) break;
      if (--m === 0) { m = 12; y--; }
    }
    host.append(el('p', 'muted', `Pages by the day they were written, newest month first${months.length === 48 ? ' (the last 48 months)' : ''}. Every month uses the same scale.`));
    const LW = 116, CW = 14, RH = 25, TW = 48;
    const W = LW + 31 * CW + TW, H = months.length * RH;
    const svg = svgEl('svg', { class: 'tl-grid', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'group',
      'aria-label': 'Pages per day, one row per month' });
    const label = (day, n) => `${+day.slice(8, 10)} ${MON[+day.slice(5, 7) - 1]} ${day.slice(0, 4)} · ${plural(n, 'page')}`;
    months.forEach((key, r) => {
      const g = svgEl('g', { transform: `translate(0 ${r * RH})` });
      const lab = dm(MON[+key.slice(5, 7) - 1] + ' ' + key.slice(2, 4), 2);
      lab.setAttribute('x', 0);
      lab.setAttribute('y', 2);  // 21 px tall in a 25 px row
      g.append(lab);
      const dim = new Date(Date.UTC(+key.slice(0, 4), +key.slice(5, 7), 0)).getUTCDate();
      let total = 0;
      for (let d = 1; d <= dim; d++) {
        const day = `${key}-${String(d).padStart(2, '0')}`, n = (byDay.get(day) || []).length;
        total += n;
        const c = svgEl('circle', { cx: LW + (d - 1) * CW + CW / 2, cy: RH / 2, r: 4.5, class: 'c' + STEP(n) + (n ? ' hit' : '') });
        c.dataset.day = day;
        c.dataset.n = n;
        if (n) {  // a day with pages is a keyboard button too, not mouse-only
          c.setAttribute('tabindex', '0');
          c.setAttribute('role', 'button');
          c.setAttribute('aria-label', label(day, n));
        } else c.setAttribute('aria-hidden', 'true');
        g.append(c);
      }
      const t = svgEl('text', { x: W - 4, y: RH / 2 + 4, 'text-anchor': 'end', class: 'tl-total' });
      t.textContent = String(total);
      g.append(t);
      svg.append(g);
    });
    svg.addEventListener('mousemove', (e) => {
      const c = e.target.closest('circle');
      if (!c) { hideTip(); return; }
      showTip(e, [label(c.dataset.day, +c.dataset.n)]);
    });
    svg.addEventListener('mouseleave', hideTip);
    const list = el('div');
    let selC = null;
    const pick = (e) => {
      const c = e.target.closest('circle');
      if (!c || !+c.dataset.n) return;
      if (e.type === 'keydown') {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
      }
      if (selC) selC.classList.remove('sel');
      selC = c;
      c.classList.add('sel');
      dayList(list, c.dataset.day, byDay.get(c.dataset.day));
    };
    svg.addEventListener('click', pick);
    svg.addEventListener('keydown', pick);
    const wrap = board('Activity', { note: `${plural(byDay.size, 'day')} with dated pages` });
    const sc = el('div', 'tl-wrap');
    sc.append(svg);
    const key = el('div', 'tl-key');
    for (const [s, txt] of [[0, 'none'], [1, '1 to 2 pages'], [2, '3 to 7'], [3, '8 or more']]) {
      const k = el('span'), ks = svgEl('svg', { width: 12, height: 12, class: 'tl-grid', 'aria-hidden': 'true' });
      ks.append(svgEl('circle', { cx: 6, cy: 6, r: 4.5, class: 'c' + s }));
      k.append(ks, ' ' + txt);
      key.append(k);
    }
    key.append(el('span', null, 'Click a day to list its pages.'));
    sc.append(key);
    wrap.body.append(sc);
    host.append(wrap.root, list);

    host.append(el('h2', null, 'Months'));
    const SRC = [['Claude', (p) => p.source === 'Claude'], ['Gemini', (p) => p.source === 'Gemini'],
      ['Perplexity', (p) => p.source === 'Perplexity'], ['YouTube', (p) => p.source === 'YouTube' || p.source === 'NotebookLM'],
      ['notes', (p) => p.kind === 'note'], ['journal', (p) => p.kind === 'journal']];
    const mb = board('Months');
    const ul = el('ul', 'months');
    for (const key of months) {
      const ps = days.filter((d) => d.startsWith(key)).flatMap((d) => byDay.get(d));
      if (!ps.length) continue;
      const li = el('li');
      li.append(el('span', 'm', fmtMonth(key + '-01')));
      for (const [word, f] of SRC) {
        const n = ps.filter(f).length;
        if (n) li.append(el('span', 'muted', `${word} ${n}`));
      }
      const jr = D.pages.find((p) => p.kind === 'journal' && p.id.startsWith(key));
      if (jr) li.append(button('linkbtn', 'Journal', () => openReader(jr.id)));
      if (PAGES.has('youtube-' + key)) li.append(button('linkbtn', 'YouTube month', () => openReader('youtube-' + key)));
      ul.append(li);
    }
    mb.body.append(ul);
    host.append(mb.root);
  }
  function dayList(host, day, ps) {
    const groups = new Map();
    for (const p of ps) {
      const k = p.source || kindWord(p.kind);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(p);
    }
    const b = board(`${+day.slice(8, 10)} ${MON[+day.slice(5, 7) - 1]} ${day.slice(0, 4)}`, { note: plural(ps.length, 'page') });
    for (const [k, xs] of groups) {
      b.body.append(el('h4', 'pad-h', k));
      for (const p of xs) b.body.append(brow({ id: p.id, title: p.title, sub: p.summary || kindWord(p.kind), left: lobeChip(p.cluster), brainId: p.id }));
    }
    b.root.style.marginTop = '16px';
    host.replaceChildren(b.root);
    b.root.scrollIntoView({ block: 'nearest', behavior: REDUCED.matches ? 'auto' : 'smooth' });
  }

  /* ---- Ideas ---- */
  function renderIdeas() {
    const host = $('view-ideas');
    host.append(el('h1', null, 'Ideas and loops'));
    const grid = el('div', 'ideas-grid');
    const lb = board('Open loops', { note: plural(D.loops.length, 'open loop') });
    if (!D.loops.length) lb.body.append(el('p', 'empty', "No open loops. Say 'remind me to…' in any Claude chat."));
    const TYPES = [['commitment', 'Commitments'], ['question', 'Questions'], ['idea', 'Ideas']];
    const known = new Set(TYPES.map((t) => t[0]));
    for (const [t, word] of [...TYPES, ['', 'Other']]) {
      const ls = D.loops.filter((l) => (t ? l.loop_type === t : !known.has(l.loop_type)));
      if (!ls.length) continue;
      lb.body.append(el('h4', 'pad-h', word));
      for (const l of ls) lb.body.append(loopRow(l));
    }
    grid.append(lb.root);
    grid.append(boardList(board('Decisions to revisit'), D.follow_ups.map(followRow), 1e9,
      'No decisions waiting for a second look. A decision with a review date shows up here when it is due.'));
    grid.append(boardList(board('Back on your mind'), D.resurface.map(resurfaceRow), 1e9,
      'Nothing coming back today. Old ideas and decisions return here when they are due another look.'));
    host.append(grid);
    host.append(el('h2', null, 'Recent ideas'));
    const since = new Date(dayNum(TODAY) * 864e5 - 30 * 864e5).toISOString().slice(0, 10);
    const ideas = D.pages.filter((p) => p.kind === 'note' && (p.tags.includes('idea')
      || ['fleeting', 'permanent'].includes(String(p.props.note_type || ''))) && (dateOf(p) || '') >= since)
      .sort((a, b) => (dateOf(b) > dateOf(a) ? 1 : -1));
    host.append(boardList(board('Notes from the last 30 days', { note: plural(ideas.length, 'note') }),
      ideas.map((p) => brow({ id: p.id, title: p.title, sub: `${fmtDay(dateOf(p))} · ${p.summary || kindWord(p.kind)}`,
        left: lobeChip(p.cluster), prompt: `/keepfold about ${p.id}`, brainId: p.id })), 1e9,
      'No new ideas in the last 30 days. Say "note this idea" in any Claude chat.'));
  }

  /* ---- Study (the old Today and History) ---- */
  function renderStudy() {
    // v8: plans have no dates; each shows its next step and how many are done and left
    const box = $('sprint'), ps = D.plans || [];
    if (!ps.length) box.hidden = true;
    for (const p of ps) {
      box.append(el('p', null, `${p.title} · ${p.next.length ? 'next: ' + p.next[0] : 'all steps done'}`
        + ` · ${p.done} done · ${p.left} left`));
    }
    const tiles = $('tiles');
    for (const st of STATES) {
      const b = button(null, null, () => { show('brain'); setView('2d'); G.onlyState(st); });
      b.append(glyph(st), ' ' + LABEL[st], dm(String(D.counts[st] || 0), 4));
      b.setAttribute('aria-label', `${LABEL[st]}: ${D.counts[st] || 0}, show on the 2D map`);
      tiles.append(b);
    }
    const plan = $('plan');
    const leeches = D.leeches || [], leech = new Set(leeches.map((x) => x.id));
    if (leeches.length) {  // concepts forgotten again and again after they were learned
      const n = leeches.length, first = leeches[0].id, line = el('p', 'small');
      const text = n === 1 ? '1 concept keeps slipping: simplify its note.' : `${n} concepts keep slipping: simplify their notes.`;
      line.append(PAGES.has(first) || GN.has(first) ? button('linkbtn', text, () => openReader(first)) : text);
      plan.append(line);
    }
    if (D.first_run) plan.append(firstRunBoard());
    else if (D.plan_cleared) plan.append(el('div', 'board done-card', 'Done for today ✓'));
    else if (!D.today.length) plan.append(el('p', 'empty', 'Nothing planned yet. Run /keepfold in Claude to pick what to learn next.'));
    for (const [kind, head] of GROUPS) {
      const items = D.today.filter((i) => i.kind === kind);
      if (!items.length) continue;
      const g = el('div', 'group');
      const ul = el('ul', 'rows');
      for (const i of items) {
        const li = el('li', 'row');
        const mid = el('span', 'title');
        mid.append(PAGES.has(i.id) || GN.has(i.id) ? button('linkbtn', i.title, () => openReader(i.id)) : i.title);
        if (i.state) mid.append(el('span', 'label muted', LABEL[i.state]));
        if (leech.has(i.id)) {
          const tag = el('span', 'label muted', 'leech');
          tag.title = i.why;
          mid.append(tag);
        }
        li.append(glyph(i.state), mid, el('span', 'why', i.why === head ? '' : i.why));
        ul.append(li);
      }
      g.append(el('h3', null, head), ul);
      plan.append(g);
    }
    const coming = $('coming');
    if (!D.assessments.length) coming.append(el('p', 'empty', 'Nothing scheduled. Tell any Claude chat about a test and its date.'));
    for (const a of D.assessments) {
      const c = el('div', 'board assess');
      c.append(el('h3', null, [a.company || a.id, a.kind].filter(Boolean).join(' · ')));
      const n = a.days_left;
      c.append(el('div', 'when', n == null ? 'date not set' : n === 0 ? 'today' : n === 1 ? 'tomorrow' : `in ${n} days`));
      const r = a.readiness;
      if (r.total) {
        const wrap = el('div', 'ready'), bar = el('div', 'bar');
        bar.setAttribute('role', 'img');
        const { pct, label } = readiness(r);
        bar.setAttribute('aria-label', label);
        const fill = el('span');
        fill.style.setProperty('--f', String(pct / 100));  // the fill is scaleX(--f)
        bar.append(fill);
        wrap.append(bar);
        c.append(wrap, el('div', 'small muted', label));
        if (r.forecast != null) {  // FSRS prediction, never a score; the Solid share stays
          const day = new Date(a.date + 'T12:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
          c.append(el('div', 'small muted', `Forecast recall on ${day}: ${Math.round(100 * r.forecast)}%`));
        } else if (r.calibrating) {  // too few checked forecasts to trust a number yet
          const { n, need } = r.calibrating;  // enough reviews but no skill yet: say so, like render.py
          c.append(el('div', 'small muted', n < need ? `Forecast after ${need} checked reviews (${n} so far)`
            : 'Forecast still calibrating: not yet better than a guess'));
        }
      } else c.append(el('div', 'small muted', 'No tested topics recorded'));
      if (a.domains.length) {
        const chips = el('div', 'chip-list');
        for (const d of a.domains) chips.append(el('span', 'chip', d));
        c.append(chips);
      }
      const acts = el('div', 'chip-list');
      const cp = button('btn', null, () => copyText(`/keepfold mock ${a.id}`, cp));
      cp.append(icon('copy'), ' Copy mock prompt');
      acts.append(cp);
      c.append(acts);
      coming.append(c);
    }
    const om = $('open-mistakes');
    const open = D.mistakes.filter((m) => m.status === 'open').slice(0, 5);
    if (!open.length) om.append(el('p', 'empty', 'No open mistakes. A wrong answer in a graded check shows up here until it is fixed.'));
    for (const m of open) {
      const row = el('div', 'mrow');
      const left = el('span');
      left.append(el('span', 'flag', '⚑ '), PAGES.has(m.id) ? button('linkbtn', m.title, () => openReader(m.id)) : m.title,
        el('span', 'muted', ` (${m.count}×)`));
      row.append(left, el('span', 'muted small', m.last_day ? 'last ' + fmtDay(m.last_day) : ''));
      om.append(row);
    }
    renderHistory();
  }
  function renderHistory() {
    const W = D.history.weeks, host = $('weeks');
    const w = Math.max(320, host.clientWidth - 28 || 640), h = 200;
    const m = { t: 12, r: 6, b: 26, l: 30 };
    const x = d3.scaleBand().domain(W.map((d) => d.start)).range([m.l, w - m.r]);
    const max = d3.max(W, (d) => d.answers) || 1;
    const y = d3.scaleLinear().domain([0, max]).nice().range([h - m.b, m.t]);
    const svg = d3.select(host).append('svg').attr('viewBox', `0 0 ${w} ${h}`)
      .attr('role', 'img').attr('aria-label', 'Graded answers per week, last 8 weeks: '
        + W.map((d) => `${fmtDay(d.start)} ${d.answers}`).join(', '));
    const ticks = y.ticks(4);
    svg.append('g').selectAll('line').data(ticks).join('line').attr('class', 'grid')
      .attr('x1', m.l).attr('x2', w - m.r).attr('y1', y).attr('y2', y);
    svg.append('g').selectAll('text').data(ticks).join('text').attr('class', 'axis')
      .attr('x', m.l - 6).attr('y', (d) => y(d) + 4).attr('text-anchor', 'end').text((d) => d);
    const bw = Math.max(1, x.bandwidth() - 2);
    svg.append('g').selectAll('rect').data(W).join('rect').attr('class', 'wbar')
      .attr('x', (d) => x(d.start) + 1).attr('width', bw)
      .attr('y', (d) => y(d.answers)).attr('height', (d) => Math.max(0, y(0) - y(d.answers)))
      .on('mouseenter', (e, d) => showTip(e, [`Week of ${fmtDay(d.start)}: ` + plural(d.answers, 'answer')]))
      .on('mousemove', moveTip).on('mouseleave', hideTip);
    svg.append('g').selectAll('text').data(W).join('text').attr('class', 'axis')
      .attr('x', (d) => x(d.start) + x.bandwidth() / 2).attr('y', h - 8)
      .attr('text-anchor', 'middle').text((d) => fmtDay(d.start));

    const tl = $('timeline');
    const rows = D.history.assessments;
    if (!rows.length) tl.append(el('p', 'empty', 'No assessments yet. Tell any Claude chat about a test and its date.'));
    for (const a of rows) {
      const r = el('div', 'trow');
      const who = el('span');
      who.append(a.company || a.id, el('span', 'muted', a.kind ? ' · ' + a.kind : ''));
      r.append(el('span', 'muted', a.date ? fmtDay(a.date) + ' ' + a.date.slice(0, 4) : 'date not set'),
        el('span', 'badge', OUTCOME[a.outcome] || a.outcome), who,
        el('span', 'muted', a.score ? 'score ' + a.score : ''));
      tl.append(r);
    }
    const ms = $('mstats');
    for (const [k, word] of [['opened', 'opened'], ['resolved', 'resolved']]) {
      const b = el('div');
      b.append(dm(String(D.history.mistakes[k]), 4), el('span', 'muted', word));
      ms.append(b);
    }
    // display-only; a band under its minimum shows its count, not a share
    const mir = $('mirror'), C = D.confidence || [];
    if (!C.some((r) => r.n)) mir.append(el('p', 'empty', 'No answers with a "how sure" (1-4) yet. Say how sure you are when you answer in /keepfold review.'));
    else {
      mir.append(el('p', 'small muted', 'Each time you answer, you say how sure you are. This is how often you were right at each level, over all your answers.'));
      for (const r of C) {
        mir.append(el('div', 'mrow', r.share == null
          ? `When you said ${r.label}: not enough answers yet (${r.n})`
          : `When you said ${r.label}, you were right ${r.right} of ${r.n} times`));
      }
    }
  }

  /* ---- Help ---- */
  function commandRows() {
    const md0 = D.help && D.help.commands_md;
    if (!md0) return [];
    const out = [];
    for (const line of md0.split('\n')) {
      const m = /^\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|?\s*$/.exec(line);
      if (m && /^\/?keepfold\b/.test(m[1])) out.push({ cmd: m[1], desc: m[2].replace(/[`*]/g, '') });
    }
    return out;
  }
  function renderHelp() {
    const host = $('view-help');
    host.append(el('h1', null, 'Help'));
    const prose = el('div', 'prose');
    prose.append(el('p', null, `${greeting()}. Here is everything Keepfold can do.`),
      el('p', 'muted', 'This page is read-only: buttons copy a command to paste into Claude; Claude does every write.'));
    if (LIVE) {  // the app's step-by-step first-run dialog; a snapshot file has no app behind it
      const wg = el('a', null, 'Show the welcome guide'), wgP = el('p', null, 'New to Keepfold? ');
      wg.href = '/manage#welcome';
      wgP.append(wg);
      prose.append(wgP);
    }
    host.append(prose);
    const h = D.help || {};
    if (!h.commands_md) {
      host.append(el('p', 'empty', 'Run /keepfold help in Claude for every command.'));
    } else {
      host.append(el('h2', null, 'Commands'));
      const src = h.commands_md, cut = src.indexOf('\n---');  // the lines above are for Claude
      const body = el('div', 'md help-cmds');
      body.append(md(cut >= 0 ? src.slice(cut + 4) : src));
      for (const c of body.querySelectorAll('td code')) {
        const t = c.textContent.trim();
        if (!/^\/?keepfold\b/.test(t)) continue;
        const wrap = el('span', 'copycell');
        c.replaceWith(wrap);
        wrap.append(c, copyBtn(t));
      }
      host.append(body);
    }
    host.append(el('h2', null, 'How saving works'));
    const ul = el('ul', 'prose');
    for (const t of ['The first message of a chat primes it with what Keepfold knows about the topic.',
      'Durable things (facts, sources, decisions, insights, results) are saved as they come up, and each save is announced.',
      'Lines about you wait for your yes before a chat relies on them.',
      '/keepfold save is the safety net for very long chats.',
      "Every chat's changes can be undone: keepfold undo <session>."]) ul.append(el('li', null, t));
    host.append(ul);
    host.append(el('h2', null, 'The names'));
    if (h.glossary_md) {
      const g = el('div', 'md');
      g.append(md(h.glossary_md));
      host.append(g);
    } else host.append(el('p', 'empty', 'Run /keepfold help in Claude for every command.'));
    host.append(el('h2', null, 'This dashboard'));
    const SAY = { home: 'what is due today, what is coming, open loops and what changed this week',
      brain: 'every page as a point in a 3D brain, one lobe per topic; also the 2D concept map and its list',
      library: 'every page as a sortable timetable, with presets, lobes and tags',
      you: 'what Keepfold knows about you, lines waiting for your yes, and your me/ pages',
      timeline: 'pages by the day they were written, month by month',
      ideas: 'open loops, decisions to revisit, ideas coming back and recent notes',
      study: 'your study plans, learning states, the plan, readiness for tests, mistakes and weekly answers',
      help: 'this page' };
    const tbl = el('table'), tb = el('tbody');
    SECTIONS.forEach((s, i) => {
      const tr = el('tr'), th = el('th', null, `${i + 1} · ${TITLES[s]}`);
      th.scope = 'row';
      tr.append(th, el('td', null, SAY[s]));
      tb.append(tr);
    });
    tbl.append(tb);
    const tb2 = board('Sections');
    tb2.body.append(tbl);
    host.append(tb2.root);
    host.append(el('h3', null, 'Keyboard'));
    const keys = el('ul', 'prose');
    for (const t of ['Ctrl+K: search pages, sections and commands', `1 to ${SECTIONS.length}: switch sections`,
      'Arrow keys in the section tabs: move between sections', 'Esc: close the reader or the search',
      'In the brain: drag to turn, scroll to zoom, arrows turn (in Today, Left and Right step through today\'s pages), + and − zoom, 0 resets the view',
      'In the brain: Esc goes back one level (page, region, overview); double-click empty space for the overview; double-click a page or region name to fly to it',
      'With a page selected: [ and ] step to the previous or next connection, Backspace returns to the previous page, F frames the selection',
      'With the brain focused: A, M and G switch Activity, Mastery and Growth; S turns spin on or off; L opens Look',
      'In Look: presets, then switches and sliders for each part of the brain; Esc closes it',
      'In the brain outline: arrows move and open, Enter selects']) keys.append(el('li', null, t));
    host.append(keys);
  }

  /* ---- command palette (Ctrl+K) ---- */
  const pal = $('palette'), pq = $('pq'), pres = $('presults');
  let palItems = [], palSel = 0, palFrom = null, CMDS = null;
  function openPalette() {
    if (pal.hidden) palFrom = document.activeElement;
    pal.hidden = false;
    hideTip();
    pq.focus();
    pq.select();
    renderPalette();
  }
  function closePalette() {
    pal.hidden = true;
    if (palFrom && palFrom.isConnected) palFrom.focus({ preventScroll: true });
  }
  function palRow(title, sub, right, run) {
    const b = button('hit', null, () => { closePalette(); run(); });
    b.setAttribute('role', 'option');
    b.tabIndex = -1;
    const t = el('span', 'ttl', title);
    b.append(t, right || el('span'));
    if (sub) b.append(el('span', 'sub', sub));
    palItems.push(b);
    return b;
  }
  function renderPalette() {
    const q = pq.value.trim().toLowerCase();
    palItems = [];
    pres.replaceChildren();
    const secs = SECTIONS.filter((s) => !q || TITLES[s].toLowerCase().includes(q) || s.includes(q));
    if (secs.length) {
      pres.append(el('h4', null, 'Sections'));
      for (const s of secs) pres.append(palRow(TITLES[s], null, el('span', 'kind', 'key ' + (SECTIONS.indexOf(s) + 1)), () => show(s)));
    }
    if (q) {
      if (!CMDS) CMDS = commandRows();
      const cmds = CMDS.filter((c) => c.cmd.toLowerCase().includes(q) || c.desc.toLowerCase().includes(q)).slice(0, 6);
      if (cmds.length) {
        pres.append(el('h4', null, 'Commands (Enter copies)'));
        for (const c of cmds) pres.append(palRow(c.cmd, c.desc, el('span', 'kind', 'copy'), () => copyText(c.cmd)));
      }
      const hits = search(q).slice(0, 40);
      if (hits.length) {
        pres.append(el('h4', null, 'Pages'));
        for (const x of hits) {
          const p = x.h.p, right = el('span', 'line1');
          right.append(el('span', 'kind', kindWord(p.kind)), lobeChip(p.cluster));
          pres.append(palRow(p.title, p.summary || snippet(x), right, () => openReader(p.id)));
        }
      }
      if (!cmds.length && !hits.length && !secs.length) pres.append(el('p', 'empty', 'Nothing matches. Try fewer words.'));
    }
    palSel = 0;
    markPal();
  }
  function markPal() {
    palItems.forEach((b, i) => b.setAttribute('aria-selected', String(i === palSel)));
    if (palItems[palSel]) palItems[palSel].scrollIntoView({ block: 'nearest' });
  }
  let palTimer = 0;
  pq.addEventListener('input', () => { clearTimeout(palTimer); palTimer = setTimeout(renderPalette, 120); });
  pq.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); palSel = Math.min(palSel + 1, palItems.length - 1); markPal(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = Math.max(palSel - 1, 0); markPal(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(palTimer);
      if (pq.value.trim() && !palItems.length) renderPalette();
      if (palItems[palSel]) palItems[palSel].click();
    }
  });
  pal.addEventListener('mousedown', (e) => { if (e.target === pal) closePalette(); });
  // a modal dialog: Tab stays on the search box (results move with the arrows)
  pal.addEventListener('keydown', (e) => { if (e.key === 'Tab') { e.preventDefault(); pq.focus(); } });

  /* ---- 2D map (the old Map tab, behaviour unchanged) ---- */
  const G = (() => {
    const F = { states: new Set(STATES), domain: '', weak: false, list: false, q: '',
      kinds: new Set(['concept', 'project']), local: false };
    const RADIUS = { area: 4.5, you: 9, project: 5, resource: 3.5, note: 2.5, decision: 3.5 };
    const DIST = { domain: 55, area: 28, requires: 70, wiki: 45 };
    const PRETICKS = 80;
    const CHARGE = { you: -300, domain: -300, area: -70, concept: -24, project: -40, resource: -24,
      note: -24, decision: -24 };
    let nodes, links, byId, adj, R, sim, hover = null, k = 1, ready = false, touched = false, built = false;
    let tf = d3.zoomIdentity, ticks = 0, near = null;
    let sortKey = 'title', sortDir = 1, current = null;

    const KINDS = [['concept', '●', 'Concepts'], ['project', '▢', 'Projects'],
      ['resource', '◆', 'Resources'], ['note', '•', 'Notes'], ['decision', '▲', 'Decisions']];
    const kchips = $('kinds'), chips = $('chips'), sel = $('domain');
    // the Domain list from the current data; a chosen domain that is gone goes back to All domains
    function fillDomains() {
      const doms = D.graph.nodes.filter((x) => x.kind === 'domain');
      if (F.domain && !doms.some((n) => n.id === F.domain)) F.domain = '';
      sel.replaceChildren(sel.options[0], ...doms.map((n) => { const o = el('option', null, n.title); o.value = n.id; return o; }));
      sel.value = F.domain;
      sel.disabled = sel.options.length < 2;  // no domains yet: nothing to choose
    }
    function build() {  // the controls, once
      if (built) return;
      built = true;
      for (const [kd, g, word] of KINDS) {
        const b = button(null, null, () => {
          if (F.kinds.has(kd)) F.kinds.delete(kd); else F.kinds.add(kd);
          syncChips();
          apply();
        });
        b.dataset.kind = kd;
        b.append(el('span', 'glyph k-' + kd, g), ' ' + word);
        kchips.append(b);
      }
      for (const s of STATES) {
        const b = button(null, null, () => {
          if (F.states.has(s)) F.states.delete(s); else F.states.add(s);
          syncChips();
          apply();
        });
        b.dataset.state = s;
        b.append(glyph(s), ' ' + LABEL[s]);
        chips.append(b);
      }
      syncChips();
      fillDomains();
      sel.addEventListener('change', () => {
        F.domain = sel.value;
        apply();
        if (ready && F.domain && !F.list) R.fit(nodes.filter((n) => n.domain === F.domain), 600);
      });
      $('weak').addEventListener('change', (e) => { F.weak = e.target.checked; apply(); });
      $('local').addEventListener('change', (e) => { F.local = e.target.checked; relocal(); });
      $('files').addEventListener('change', (e) => {
        $('tree').hidden = !e.target.checked;
        if (e.target.checked && !$('tree').childElementCount) renderTree();
      });
      $('q').addEventListener('input', (e) => { F.q = e.target.value.trim().toLowerCase(); apply(); });
    }
    function setList(on) {
      F.list = on;
      $('graph').hidden = on;
      $('table').hidden = !on;
      apply();
    }
    // Obsidian's local graph: the open note plus 2 hops, never through the You hub.
    function relocal() {
      near = null;
      if (F.local && current && byId && byId.has(current)) {
        near = new Set([current]);
        let edge = [current];
        for (let d = 0; d < 2; d++) {
          const next = [];
          for (const x of edge) {
            for (const y of adj.get(x)) {
              if (!near.has(y) && byId.get(y).kind !== 'you') { near.add(y); next.push(y); }
            }
          }
          edge = next;
        }
      }
      apply();
      if (ready && !F.list) R.fit(nodes.filter((n) => !n._hide), 500);
    }
    function syncChips() {
      for (const b of chips.children) b.setAttribute('aria-pressed', String(F.states.has(b.dataset.state)));
      for (const b of kchips.children) b.setAttribute('aria-pressed', String(F.kinds.has(b.dataset.kind)));
    }

    const weakNode = (n) => n.state === 'learning' || n.state === 'needs_refresh' || n.open_mistakes > 0;
    function visible(n) {
      if (near) { if (!near.has(n.id)) return false; if (n.id === current) return true; }
      if (n.kind === 'you') return true;
      if (F.domain && n.domain !== F.domain) return false;
      if (WIKI.has(n.kind)) return F.kinds.has(n.kind);
      if (n.kind !== 'concept') return true;
      return F.kinds.has('concept') && F.states.has(n.state) && (!F.weak || weakNode(n));
    }
    const matches = (n) => !!F.q && n.title.toLowerCase().includes(F.q);

    function radius(n) {
      if (n.kind === 'concept') return Math.min(6, 3.5 + Math.max(0, n.degree - 1) * 0.5);
      if (n.kind === 'domain') return 7 + Math.sqrt(n.degree) * 1.6;
      return RADIUS[n.kind];
    }

    function init() {
      build();
      if (ready) return;
      ready = true;
      nodes = D.graph.nodes.map((n) => Object.assign({}, n));
      const have = new Set(nodes.map((n) => n.id));
      const wiki = D.pages.filter((p) => WIKI.has(p.kind) && !have.has(p.id));
      for (const p of wiki) {
        nodes.push({ id: p.id, kind: p.kind, title: p.title, state: null, domain: null, area: null,
          degree: 0, open_mistakes: 0, next_due: null });
      }
      byId = new Map(nodes.map((n) => [n.id, n]));
      links = D.graph.links.filter((l) => byId.has(l.s) && byId.has(l.t))
        .map((l) => ({ source: byId.get(l.s), target: byId.get(l.t), type: l.type }));
      const pairs = new Set();
      for (const p of wiki) {
        for (const t of p.links) {
          const key = [p.id, t].sort().join('\n');
          if (!byId.has(t) || pairs.has(key)) continue;
          pairs.add(key);
          links.push({ source: byId.get(p.id), target: byId.get(t), type: 'wiki' });
          byId.get(p.id).degree++;
          if (WIKI.has(byId.get(t).kind)) byId.get(t).degree++;
        }
      }
      adj = new Map(nodes.map((n) => [n.id, new Set()]));
      for (const l of links) { adj.get(l.source.id).add(l.target.id); adj.get(l.target.id).add(l.source.id); }
      // Domains sit on a ring around You; areas and concepts settle outside their hub.
      const doms = nodes.filter((n) => n.kind === 'domain');
      const ring = Math.max(140, 11 * Math.sqrt(nodes.length));
      const RING = { you: 0, domain: ring, area: ring * 1.6, concept: ring * 2.1 };
      const PULL = { you: 0, domain: 0.5, area: 0.12, concept: 0.03 };
      for (const kd of WIKI) { RING[kd] = ring * 2.4; PULL[kd] = 0.02; }
      const angle = new Map(doms.map((n, i) => [n.id, 2 * Math.PI * i / doms.length]));
      for (const n of nodes) {
        n.r = radius(n);
        const a = WIKI.has(n.kind) ? Math.random() * 2 * Math.PI
          : (angle.get(n.domain) || 0) + (n.kind === 'domain' ? 0 : (Math.random() - 0.5) * 0.4);
        n.x = Math.cos(a) * RING[n.kind]; n.y = Math.sin(a) * RING[n.kind];
        if (n.kind === 'you') { n.fx = 0; n.fy = 0; }
      }
      sim = d3.forceSimulation(nodes).alphaMin(0.02)
        .force('link', d3.forceLink(links)
          .distance((l) => (l.type === 'hier' ? DIST[l.source.kind] : l.type === 'you' ? ring : DIST[l.type]))
          .strength((l) => (l.type === 'requires' || l.type === 'area' ? 0.02 : l.type === 'you' ? 0.1
            : 1 / Math.min(l.source.degree || 1, l.target.degree || 1))))
        .force('charge', d3.forceManyBody().strength((n) => CHARGE[n.kind]).distanceMax(500))
        .force('collide', d3.forceCollide((n) => n.r + 2))
        .force('radial', d3.forceRadial((n) => RING[n.kind], 0, 0).strength((n) => PULL[n.kind]));
      apply();  // drop the hidden kinds from the simulation before it settles
      const t0 = performance.now();
      sim.stop().tick(PRETICKS);  // most of the settling happens before the first paint
      R = nodes.length > 1500 ? canvasRenderer() : svgRenderer();
      if (nodes.every((n) => n.kind === 'you')) {
        $('graph').prepend(el('p', 'empty', 'No topics or pages on the map yet. Run /keepfold learn in Claude to start one.'));
      }
      R.tick();
      sim.restart();
      let first = true;
      sim.on('tick', () => { R.tick(); if (++ticks % 5 === 0) relabel(); }).on('end', () => {
        relabel();
        if (!first) return;
        first = false;
        const ms = Math.round(performance.now() - t0);
        document.body.dataset.settleMs = String(ms);
        console.info(`keepfold: graph settled in ${ms} ms (${nodes.length} nodes)`);
        if (!touched) R.fit(nodes.filter((n) => !n._hide), 500);
      });
      apply();
      if (current) relocal();
    }

    function apply() {
      if (!ready) return;
      const nb = hover ? adj.get(hover.id) : null;
      let changed = false;
      for (const n of nodes) {
        const hide = !visible(n);
        changed = changed || hide !== !!n._hide;
        n._hide = hide;
        n._dim = hover ? !(n === hover || nb.has(n.id)) : (F.q ? !matches(n) : false);
        n._force = matches(n) || (hover != null && (n === hover || nb.has(n.id)));
        n._label = !(n.kind === 'concept' || WIKI.has(n.kind)) || k >= 1.2 || n._force;
      }
      for (const l of links) {
        l._hide = l.source._hide || l.target._hide;
        l._dim = hover ? !(l.source === hover || l.target === hover)
          : (F.q ? !(matches(l.source) && matches(l.target)) : false);
      }
      if (changed) {  // hidden nodes leave the force simulation (and rejoin when shown again)
        sim.nodes(nodes.filter((n) => !n._hide));
        sim.force('link').links(links.filter((l) => !l._hide));
        if (R) sim.alpha(Math.max(sim.alpha(), 0.3)).restart();
      }
      relabel();
      if (F.list) renderTable();
    }

    // A label that would overlap a higher-priority one is skipped (the hovered node's never):
    // hovered and matched labels first, then domains, areas and concepts, each by degree.
    const mctx = document.createElement('canvas').getContext('2d');
    const PRI = { you: 0, domain: 1, area: 2, concept: 3, project: 3, resource: 3, note: 3, decision: 3 };
    const fontSize = (n) => (n.kind === 'domain' ? 14.3 : 11);
    const MAX_LABELS = 300;  // on screen, by priority: bounds the overlap scan on big maps
    function labelWidth(n) {
      if (n._w == null) {
        mctx.font = `${n.kind === 'domain' ? 600 : 400} ${fontSize(n)}px system-ui, sans-serif`;
        n._w = mctx.measureText(n.title + (n.open_mistakes ? ' ⚑' : '')).width;
      }
      return n._w;
    }
    function relabel() {
      if (!R) return;  // the renderer's first zoom event fires while it is being built
      const cand = nodes.filter((n) => n._label && !n._hide);
      cand.sort((a, b) => (b._force - a._force) || (PRI[a.kind] - PRI[b.kind]) || (b.degree - a.degree));
      const placed = [], gh = $('graph'), vw = gh.clientWidth || 800, vh = gh.clientHeight || 600;
      for (const n of cand) {
        const x0 = tf.applyX(n.x + n.r + 3), y = tf.applyY(n.y + 3.5), fs = fontSize(n);
        const box = [x0 - 2, y - fs * 0.85, x0 + labelWidth(n) + 2, y + fs * 0.3];
        const off = box[2] < 0 || box[0] > vw || box[3] < 0 || box[1] > vh;  // off screen: no label
        n._shown = n === hover || (!off && placed.length < MAX_LABELS && !placed.some((b) => box[0] < b[2] && b[0] < box[2] && box[1] < b[3] && b[1] < box[3]));
        if (n._shown) placed.push(box);
      }
      for (const n of nodes) if (!n._label || n._hide) n._shown = false;
      R.update();
    }

    function tipFor(e, n) {
      const parts = [el('b', null, n.title)];
      if (n.kind === 'concept') {
        parts.push(el('br'), (GLYPH[n.state] || '') + ' ' + LABEL[n.state]);
        if (n.open_mistakes) parts.push(' · ⚑ ' + n.open_mistakes);
      } else if (n.kind === 'domain' || n.kind === 'area') {
        const c = groupCounts(n), total = STATES.reduce((a, s) => a + c[s], 0);
        parts.push(el('br'), total ? `${Math.round(100 * c.solid / total)}% solid (${c.solid} of ${total})`
          : 'no topics yet');
      } else if (n.kind !== 'you') parts.push(el('br'), kindWord(n.kind));
      showTip(e, parts);
    }
    function hoverOn(e, n) { hover = n; apply(); tipFor(e, n); }
    function hoverOff() { hover = null; apply(); hideTip(); }

    function click(n) {
      if (n.kind === 'domain' || n.kind === 'area') {
        const key = n.kind === 'domain' ? 'domain' : 'area';
        R.fit(nodes.filter((x) => x[key] === n.id && !x._hide), 600);
      }
      if (n.kind !== 'you') openReader(n.id);
    }
    // Bring a page's node into view (showing it if the filters hid it).
    function focus(id) {
      if (!ready) return;
      const n = byId.get(id);
      if (!n) return;
      if (n._hide) {
        F.states = new Set(STATES); F.weak = false; F.domain = ''; F.kinds.add(n.kind);
        syncControls();
        apply();
      }
      if (!F.list) R.center(n);
    }
    function syncControls() {
      syncChips();
      $('weak').checked = F.weak;
      sel.value = F.domain;
    }
    function setCurrent(id) {
      current = id;
      if (!ready) return;
      if (F.local) relocal();
      if (id && !$('b2d').hidden && !$('view-brain').hidden) focus(id);
    }

    /* file tree: domains › areas › concepts from the catalog, every other folder flat */
    function renderTree() {
      const tree = $('tree');
      const kids = new Map();
      for (const l of D.graph.links) {
        if (l.type !== 'hier' || !byId.has(l.t)) continue;
        if (!kids.has(l.s)) kids.set(l.s, []);
        kids.get(l.s).push(byId.get(l.t));
      }
      const byTitle = (a, b) => a.title.localeCompare(b.title);
      const leaf = (id, title) => button('leaf', title, () => openReader(id));
      const folder = (label, children, n) => {
        const d = el('details'), s = el('summary', null, label + ' ');
        s.append(el('span', 'muted', String(children.length)));
        d.append(s);
        if (n && PAGES.has(n.id)) d.append(leaf(n.id, 'Open ' + n.title));
        d.append(...children);
        return d;
      };
      const branch = (n) => (n.kind === 'concept' ? leaf(n.id, n.title)
        : folder(n.title, (kids.get(n.id) || []).sort(byTitle).map(branch), n));
      tree.append(folder('Domains', nodes.filter((n) => n.kind === 'domain').sort(byTitle).map(branch)));
      const folders = new Map();
      for (const p of D.pages) {
        if (p.kind === 'concept' || p.kind === 'domain' || p.kind === 'area') continue;
        const f = p.path.split('/')[0];
        if (!folders.has(f)) folders.set(f, []);
        folders.get(f).push(p);
      }
      for (const [f, ps] of [...folders].sort((a, b) => a[0].localeCompare(b[0]))) {
        tree.append(folder(f, ps.sort(byTitle).map((p) => leaf(p.id, p.title))));
      }
    }

    /* list view */
    const COLS = [['title', 'TITLE'], ['domain', 'DOMAIN'], ['area', 'AREA'], ['state', 'STATE'],
      ['next', 'NEXT REFRESH']];
    function cell(n, key) {
      if (key === 'domain' || key === 'area') { const p = byId.get(n[key]); return p ? p.title : ''; }
      if (key === 'state') return STATES.indexOf(n.state);
      if (key === 'next') return n.next_due || '9999';
      return n.title.toLowerCase();
    }
    function renderTable() {
      const rows = nodes.filter((n) => n.kind === 'concept' && !n._hide && (!F.q || matches(n)));
      rows.sort((a, b) => { const x = cell(a, sortKey), y = cell(b, sortKey); return (x < y ? -1 : x > y ? 1 : 0) * sortDir; });
      const table = el('table');
      table.append(el('caption', null, `${plural(rows.length, 'topic')}`));
      const tr = el('tr');
      for (const [key, name] of COLS) {
        const th = el('th');
        th.scope = 'col';
        if (key === sortKey) th.setAttribute('aria-sort', sortDir > 0 ? 'ascending' : 'descending');
        th.append(button(null, name + (key === sortKey ? (sortDir > 0 ? ' ▲' : ' ▼') : ''), () => {
          sortDir = sortKey === key ? -sortDir : 1;
          sortKey = key;
          renderTable();
        }));
        tr.append(th);
      }
      const thead = el('thead');
      thead.append(tr);
      const tbody = el('tbody');
      for (const n of rows) {
        const r = el('tr');
        const t = el('td');
        t.append(button(null, n.title, () => openReader(n.id)));
        if (n.open_mistakes) t.append(el('span', 'flag', ' ⚑'));
        const s = el('td');
        s.append(stateWord(n.state));
        r.append(t, el('td', null, cell(n, 'domain')), el('td', null, cell(n, 'area')), s,
          el('td', 'muted', n.next_due ? nextText(n.next_due) : '—'));
        tbody.append(r);
      }
      table.append(thead, tbody);
      $('table').replaceChildren(table);
      if (!rows.length) {
        $('table').append(el('p', 'empty', D.graph.nodes.some((n) => n.kind === 'concept')
          ? 'No topics match. Turn a state or kind back on, or clear the search.'
          : 'No topics yet. Run /keepfold learn in Claude to start one.'));
      }
    }

    function onZoom(t) {
      const crossed = (k >= 1.2) !== (t.k >= 1.2);
      k = t.k;
      tf = t;
      if (crossed) apply(); else relabel();
    }

    function fitTransform(set, w, h) {
      if (!set.length) return d3.zoomIdentity.translate(w / 2, h / 2);
      const xs = set.map((n) => n.x), ys = set.map((n) => n.y);
      const x0 = Math.min(...xs) - 30, x1 = Math.max(...xs) + 30;
      const y0 = Math.min(...ys) - 30, y1 = Math.max(...ys) + 30;
      const s = Math.min(4, 0.9 * Math.min(w / (x1 - x0), h / (y1 - y0)));
      return d3.zoomIdentity.translate(w / 2, h / 2).scale(s).translate(-(x0 + x1) / 2, -(y0 + y1) / 2);
    }

    function dragger(subject) {
      const d = d3.drag()
        .on('start', (e) => {
          touched = true;
          if (!e.active) sim.alphaTarget(0.3).restart();
          e.subject.fx = e.subject.x; e.subject.fy = e.subject.y;
        })
        .on('drag', (e) => { e.subject.fx = e.x; e.subject.fy = e.y; })
        .on('end', (e) => {
          if (!e.active) sim.alphaTarget(0);
          e.subject.fx = null; e.subject.fy = null;
        });
      return subject ? d.subject(subject) : d;
    }

    function svgRenderer() {
      const host = $('graph');
      const svg = d3.select(host).append('svg')
        .attr('aria-label', 'Knowledge map. List view shows the same topics as a table.');
      svg.append('defs').append('marker').attr('id', 'arrow').attr('viewBox', '0 0 8 8')
        .attr('refX', 7).attr('refY', 4).attr('markerWidth', 5).attr('markerHeight', 5)
        .attr('orient', 'auto').append('path').attr('d', 'M0,0L8,4L0,8z').attr('class', 'arrowhead');
      const root = svg.append('g');
      const L = root.append('g').selectAll('line').data(links).join('line')
        .attr('class', (l) => 'link l-' + l.type)
        .attr('marker-end', (l) => (l.type === 'requires' ? 'url(#arrow)' : null));
      const N = root.append('g').selectAll('g').data(nodes).join('g').attr('class', 'node')
        .attr('tabindex', (n) => (n.kind === 'you' ? null : 0))
        .attr('role', (n) => (n.kind === 'you' ? null : 'button'))
        .attr('aria-label', (n) => n.title + (n.state ? ', ' + LABEL[n.state] : ''));
      N.filter((n) => n.state === 'needs_refresh').append('circle').attr('class', 'ring')
        .attr('r', (n) => n.r + 2.5);
      N.each(function (n) {
        const p = paint(n), d = shapePath(n);
        const s = d3.select(this).append(d ? 'path' : 'circle').attr(d ? 'd' : 'r', d || n.r).node();
        s.style.fill = p.fill;
        s.style.stroke = p.stroke || 'none';
        if (p.dash) s.style.strokeDasharray = '2 1.6';
      });
      N.on('mouseenter', hoverOn).on('mousemove', moveTip).on('mouseleave', hoverOff)
        .on('click', (e, n) => { if (!e.defaultPrevented) click(n); })
        .on('keydown', (e, n) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); click(n); } })
        .call(dragger());
      const labels = root.append('g').attr('class', 'labels').attr('font-size', 11);
      const T = labels.selectAll('text').data(nodes).join('text')
        .attr('class', (n) => 'lab-' + n.kind).text((n) => n.title);
      T.filter((n) => n.open_mistakes > 0).append('tspan').attr('class', 'flag').text(' ⚑');
      const zoom = d3.zoom().scaleExtent([0.1, 8]).on('zoom', (e) => {
        if (e.sourceEvent) touched = true;
        root.attr('transform', e.transform);
        labels.attr('font-size', 11 / e.transform.k);
        onZoom(e.transform);
      });
      svg.call(zoom).on('dblclick.zoom', null);
      const size = () => [host.clientWidth || 800, host.clientHeight || 600];
      svg.call(zoom.transform, d3.zoomIdentity.translate(size()[0] / 2, size()[1] / 2).scale(0.6));
      return {
        tick() {
          L.each(function (l) {
            let { x: x2, y: y2 } = l.target;
            if (l.type === 'requires') {
              const dx = x2 - l.source.x, dy = y2 - l.source.y, len = Math.hypot(dx, dy) || 1;
              x2 -= dx / len * (l.target.r + 2); y2 -= dy / len * (l.target.r + 2);
            }
            this.setAttribute('x1', l.source.x); this.setAttribute('y1', l.source.y);
            this.setAttribute('x2', x2); this.setAttribute('y2', y2);
          });
          N.attr('transform', (n) => `translate(${n.x},${n.y})`);
          T.attr('x', (n) => n.x + n.r + 3).attr('y', (n) => n.y + 3.5);
        },
        update() {
          N.classed('hide', (n) => n._hide).classed('dim', (n) => n._dim);
          L.classed('hide', (l) => l._hide).classed('dim', (l) => l._dim);
          T.classed('show', (n) => n._shown).classed('dim', (n) => n._dim);
        },
        fit(set, ms) {
          const [w, h] = size();
          svg.transition().duration(motion(ms)).call(zoom.transform, fitTransform(set, w, h));
        },
        center(n) {
          const [w, h] = size();
          svg.transition().duration(motion(500)).call(zoom.transform, d3.zoomIdentity
            .translate(w / 2, h / 2).scale(Math.max(k, 1.6)).translate(-n.x, -n.y));
        },
      };
    }

    // Wiki kinds: project rounded square, resource diamond, decision triangle;
    // a note stays a small dot (a circle, so no path).
    function shapePath(n) {
      const r = n.r;
      if (n.kind === 'project') {
        const c = 1.5;
        return `M${c - r},${-r}H${r - c}Q${r},${-r} ${r},${c - r}V${r - c}Q${r},${r} ${r - c},${r}`
          + `H${c - r}Q${-r},${r} ${-r},${r - c}V${c - r}Q${-r},${-r} ${c - r},${-r}Z`;
      }
      if (n.kind === 'resource') return `M0,${-1.3 * r}L${1.3 * r},0L0,${1.3 * r}L${-1.3 * r},0Z`;
      if (n.kind === 'decision') return `M0,${-1.2 * r}L${1.1 * r},${0.8 * r}L${-1.1 * r},${0.8 * r}Z`;
      return null;
    }

    function paint(n) {
      if (n.kind === 'you') return { fill: 'var(--ink)' };
      if (n.kind === 'domain' || n.kind === 'project') return { fill: 'var(--lit)' };
      if (n.kind === 'area' || WIKI.has(n.kind)) return { fill: 'var(--ink-3)' };
      if (n.state === 'solid') return { fill: 'var(--solid)' };
      if (n.state === 'learning') return { fill: 'var(--learning)' };
      if (n.state === 'needs_refresh') return { fill: 'var(--refresh)', ring: true };
      return { fill: 'var(--panel)', stroke: 'var(--hollow)', dash: n.state === 'unverified' };
    }

    function canvasRenderer() {
      const host = $('graph');
      const canvas = d3.select(host).append('canvas')
        .attr('aria-label', 'Knowledge map. List view shows the same topics as a table.').node();
      const ctx = canvas.getContext('2d');
      let t = d3.zoomIdentity, w = 0, h = 0, raf = 0, css = null;
      const col = (v) => (v.startsWith('var(') ? css.getPropertyValue(v.slice(4, -1)).trim() : v);
      function resize() {
        const dpr = window.devicePixelRatio || 1;
        w = host.clientWidth || 800; h = host.clientHeight || 600;
        canvas.width = w * dpr; canvas.height = h * dpr;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      }
      function draw() {
        raf = 0;
        css = getComputedStyle(document.documentElement);  // the theme can change between frames
        ctx.save();
        ctx.clearRect(0, 0, w, h);
        ctx.translate(t.x, t.y);
        ctx.scale(t.k, t.k);
        for (const l of links) {
          if (l._hide) continue;
          ctx.globalAlpha = (l._dim ? 0.12 : 1) * (l.type === 'you' ? 0.55 : 1);
          ctx.strokeStyle = col(l.type === 'requires' ? 'var(--ink-3)' : 'var(--edge)');
          ctx.lineWidth = (l.type === 'you' || l.type === 'wiki' ? 0.6 : l.type === 'area' ? 0.5 : 1) / t.k;
          ctx.setLineDash(l.type === 'requires' ? [3 / t.k, 3 / t.k] : l.type === 'wiki' ? [1 / t.k, 2 / t.k] : []);
          ctx.beginPath(); ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y); ctx.stroke();
        }
        ctx.setLineDash([]);
        for (const n of nodes) {
          if (n._hide) continue;
          const p = paint(n);
          ctx.globalAlpha = n._dim ? 0.12 : 1;
          ctx.fillStyle = col(p.fill);
          const d = shapePath(n);
          if (d) {
            ctx.save(); ctx.translate(n.x, n.y); ctx.fill(new Path2D(d)); ctx.restore();
            continue;
          }
          ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 2 * Math.PI);
          ctx.fill();
          if (p.stroke) {
            ctx.setLineDash(p.dash ? [2, 1.6] : []);
            ctx.strokeStyle = col(p.stroke); ctx.lineWidth = 1.2; ctx.stroke();
            ctx.setLineDash([]);
          }
          if (p.ring) {
            ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 2.5, 0, 2 * Math.PI);
            ctx.strokeStyle = col('var(--refresh)'); ctx.lineWidth = 1.5; ctx.stroke();
          }
        }
        ctx.lineJoin = 'round';
        for (const n of nodes) {
          if (!n._shown) continue;
          ctx.globalAlpha = n._dim ? 0.12 : 1;
          const fs = (n.kind === 'domain' ? 14.3 : 11) / t.k;
          ctx.font = `${n.kind === 'domain' ? 600 : 400} ${fs}px system-ui, sans-serif`;
          const s = n.title + (n.open_mistakes ? ' ⚑' : '');
          ctx.strokeStyle = col('var(--panel)'); ctx.lineWidth = 3 / t.k;
          ctx.strokeText(s, n.x + n.r + 3, n.y + 3.5);
          ctx.fillStyle = col(n.kind === 'area' ? 'var(--ink-2)' : 'var(--ink)');
          ctx.fillText(s, n.x + n.r + 3, n.y + 3.5);
        }
        ctx.restore();
      }
      const schedule = () => { if (!raf) raf = requestAnimationFrame(draw); };
      const find = (e) => {
        const [x, y] = t.invert(d3.pointer(e, canvas));
        let best = null, bd = Infinity;
        for (const n of nodes) {
          if (n._hide) continue;
          const d = Math.hypot(n.x - x, n.y - y) - n.r;
          if (d < bd) { bd = d; best = n; }
        }
        return bd <= 4 / t.k ? best : null;
      };
      const zoom = d3.zoom().scaleExtent([0.1, 8]).on('zoom', (e) => {
        if (e.sourceEvent) touched = true;
        t = e.transform; onZoom(t); schedule();
      });
      const sel2 = d3.select(canvas);
      sel2.call(dragger((e) => {
        const n = find(e.sourceEvent);
        if (!n) return null;
        const [x, y] = t.apply([n.x, n.y]);
        return { node: n, x, y };
      }).on('drag.canvas', (e) => {
        [e.subject.node.fx, e.subject.node.fy] = t.invert([e.x, e.y]);
      }).on('start.canvas', (e) => {
        e.subject.node.fx = e.subject.node.x; e.subject.node.fy = e.subject.node.y;
      }).on('end.canvas', (e) => { e.subject.node.fx = null; e.subject.node.fy = null; }))
        .call(zoom).on('dblclick.zoom', null);
      sel2.on('mousemove', (e) => {
        const n = find(e);
        if (n !== hover) { if (n) hoverOn(e, n); else hoverOff(); } else if (n) moveTip(e);
      }).on('mouseleave', () => { if (hover) hoverOff(); })
        .on('click', (e) => { const n = find(e); if (n) click(n); });
      resize();
      t = d3.zoomIdentity.translate(w / 2, h / 2).scale(0.4);
      sel2.call(zoom.transform, t);
      new ResizeObserver(() => { if (host.clientWidth) { resize(); schedule(); } }).observe(host);
      return {
        tick: schedule,
        update: schedule,
        fit(set) { sel2.transition().duration(motion(500)).call(zoom.transform, fitTransform(set, w, h)); },
        center(n) {
          sel2.transition().duration(motion(500)).call(zoom.transform, d3.zoomIdentity
            .translate(w / 2, h / 2).scale(Math.max(k, 1.6)).translate(-n.x, -n.y));
        },
      };
    }

    function onlyState(st) {
      F.states = new Set([st]);
      F.weak = false;
      F.domain = '';
      syncControls();
      apply();
    }
    function retheme() { if (R) R.update(); }
    // live home: forget the drawn map, so the next init draws it from the current D. Its
    // controls stay; the Domain list is filled again.
    function reset() {
      if (built) fillDomains();
      if (!ready) return;
      sim.stop();
      ready = false; touched = false; R = null; hover = null;
      $('graph').replaceChildren();
      $('tree').replaceChildren();
      if (!$('tree').hidden) renderTree();
    }

    return { init, onlyState, setList, setCurrent, retheme, reset };
  })();

  const RENDER = { home: renderHome, brain: ensureBrainUi, library: renderLibrary, you: renderYou,
    timeline: renderTimeline, ideas: renderIdeas, study: renderStudy, help: renderHelp };

  /* ---- live home: poll the version every 3 s while shown, apply each delta in place ---- */
  function reindex() {  // every lookup derived from D, refilled in place
    TODAY = D.day || D.as_of.slice(0, 10);
    NAME = D.owner && D.owner.name ? D.owner.name : '';
    PAGES.clear();
    for (const p of D.pages) PAGES.set(p.id, p);
    GN.clear();
    for (const n of D.graph.nodes) GN.set(n.id, n);
    LOBES.clear();
    for (const c of D.clusters) LOBES.set(c.id, c);
    BRAIN_IDS.clear();
    if (D.brain && D.brain.nodes) for (const id of D.brain.nodes.ids) BRAIN_IDS.add(id);
    lensAdj = null;
    growthData = null;
    for (let i = scrubs.length; i--;) { if (scrubs[i].root.isConnected) scrubs[i].refresh(); else scrubs.splice(i, 1); }
    if (lensId && !BRAIN_IDS.has(lensId)) clearLens();  // its page is gone
    countDegrees();
    BACK = HAY = CMDS = null;
  }
  const CLEAR = { you: ['view-you'], timeline: ['view-timeline'], ideas: ['view-ideas'], help: ['view-help'],
    study: ['sprint', 'tiles', 'plan', 'coming', 'open-mistakes', 'weeks', 'timeline', 'mstats', 'mirror'] };
  // A section again from D, in place: Library and Brain keep their controls and filters.
  function rerender(name) {
    if (name === 'library') {
      if (!LOBES.has(LIB.lobe)) LIB.lobe = '';
      fillLobes();
      LIB.hits = LIB.q ? new Set(search(LIB.q).map((h) => h.h.p.id)) : null;
      renderLibraryRows();
    } else if (name === 'brain') {
      if (brainUi && !brainFailed) { fillOutline(); if (brain) renderLegend(brain.mode); }
      G.reset();  // the 2D map is drawn again from the new data: now if it shows, else when it does
      if (view !== '3d') G.init();
    } else {
      for (const id of CLEAR[name] || []) $(id).replaceChildren();
      if (name === 'study') $('sprint').hidden = false;
      RENDER[name]();
    }
  }
  const focusSig = (e) => [e.tagName, e.id, e.getAttribute('aria-label'), e.textContent.trim().slice(0, 80)].join('\n');
  let V = D.v;
  function applyDelta(d) {
    const had = new Set(BRAIN_IDS);
    if (d.full) { delete d.full; delete d.relock; D = d; }
    else {
      Object.assign(D, d.sections);
      if (d.pages) {
        const by = new Map(D.pages.map((p) => [p.id, p]));
        for (const p of d.pages.upsert) by.set(p.id, p);
        for (const id of d.pages.removed) by.delete(id);
        D.pages = d.pages.order.map((id) => by.get(id)).filter(Boolean);
      }
      if (d.brain) D.brain = d.brain.blob;
    }
    V = d.v;
    reindex();
    // what the user is in keeps its place: scroll, focus, the reader, the search overlay
    const y = window.scrollY, act = document.activeElement;
    const sig = act && act !== document.body ? focusSig(act) : null;
    renderHeader();
    for (const s of rendered) if (s !== current) stale.add(s);
    rerender(current);
    if (readerId && !reader.hidden) {
      if (!PAGES.has(readerId) && !GN.has(readerId) && !BRAIN_IDS.has(readerId)) {
        closeReader();
        reader.replaceChildren();  // a private site's details go with it after a relock
      }
      else if (!reader.classList.contains('inspector') && (d.full || (d.pages && d.pages.upsert.some((p) => p.id === readerId))
        || (d.sections && (d.sections.graph || d.sections.details)))) openReader(readerId, true);
    }
    if (!pal.hidden && pq.value.trim()) {
      const at = palSel;
      renderPalette();
      palSel = Math.max(0, Math.min(at, palItems.length - 1));
      markPal();
    }
    if (d.brain || d.full) remountBrain(d.brain ? d.brain.added : [...BRAIN_IDS].filter((id) => !had.has(id)));
    window.scrollTo(0, y);
    if (sig && !act.isConnected) {
      const same = [...document.querySelectorAll(act.tagName)].find((x) => focusSig(x) === sig);
      if (same) same.focus({ preventScroll: true });
    }
  }
  let pollTimer = 0, polling = false;
  async function poll() {
    clearTimeout(pollTimer);
    if (polling || document.hidden) return;  // hidden: no timer; showing the tab polls at once
    polling = true;
    let d = null;
    const gen = privGen;
    try {
      const ver = await api('/api/home/version');
      // the switch relocked (10 minutes, a minimise): the public home whole, no private item kept
      if (PRIVATE && !ver.private) d = { ...(await api('/api/home')), full: true, relock: true };
      else if (ver.v !== V) d = await api('/api/home/delta?since=' + encodeURIComponent(V) + (PRIVATE ? '&private=1' : ''));
      showUpdate(ver.update);
      offline(null);
    } catch (e) { offline(e); }
    polling = false;
    pollTimer = setTimeout(poll, 3000);
    if (!d || gen !== privGen || switching) return;  // the switch moved meanwhile: its own load wins
    if (d.relock && PRIVATE) { PRIVATE = false; privGen++; syncLock(); }
    if (d.full || d.v !== V) applyDelta(d);
  }
  if (LIVE) {
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    pollTimer = setTimeout(poll, 3000);
  }

  /* ---- start ---- */
  applyTheme(theme);
  renderHeader();
  setHdr();
  const start = SECTIONS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'home';
  // The brain's layout is the slow part: let the first paint land before it is built.
  if (start === 'home') {
    rendered.add('home');
    renderHome();
    for (const s of SECTIONS) {
      $('tab-' + s).setAttribute('aria-selected', String(s === 'home'));
      $('tab-' + s).tabIndex = s === 'home' ? 0 : -1;
    }
    current = 'home';
    sel.value = 'home';
    document.body.dataset.readyMs = String(Math.round(performance.now() - T0));
    setTimeout(() => {
      placeBrain('home-brain-slot');
      document.body.dataset.brainReadyMs = String(Math.round(performance.now() - T0));
    }, 50);
  } else {
    show(start);
    document.body.dataset.readyMs = String(Math.round(performance.now() - T0));
  }
})();
