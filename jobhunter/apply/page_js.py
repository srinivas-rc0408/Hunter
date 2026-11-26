"""JavaScript run in the page by apply/browser.py: field scan, error read-out, page text, rejected-value check.
browser.py re-exports every name here (B.DEEP_JS, B.SCAN_JS, …)."""


# Sites (LinkedIn's Easy Apply since 2025) can draw forms inside open shadow roots, where
# document.querySelector can't see them. These helpers search the page AND every open shadow root.
DEEP_JS = r"""
  const deepAll = (root, sel) => { const out = [...root.querySelectorAll(sel)];
    const walk = n => { for (const e of n.querySelectorAll('*')) if (e.shadowRoot) {
      out.push(...e.shadowRoot.querySelectorAll(sel)); walk(e.shadowRoot); } };
    walk(root); return out; };
  const deepOne = sel => { try { return deepAll(document, sel)[0] || null; } catch (_) { return null; } };
"""

SCAN_JS = r"""
(rootSel) => {
""" + DEEP_JS + r"""
  const root = rootSel ? deepOne(rootSel) : document;      // asked-for window missing: touch nothing
  if (!root) return [];
  const shown = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const vis = el => {
    if (shown(el)) return true;
    if (el.type === 'radio' || el.type === 'checkbox') {      // styled inputs: the label is what shows
      const l = el.closest('label') || (el.id && el.getRootNode().querySelector && el.getRootNode().querySelector(`label[for="${CSS.escape(el.id)}"]`));
      return shown(l);
    }
    if (el.type === 'file') return shown(el.parentElement) || shown(el.closest('div, label, form'));
    return false;
  };
  const txt = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const R = el => { const r = el.getRootNode(); return r && r.querySelector ? r : document; };
  const byId = (el, i) => (R(el).getElementById ? R(el).getElementById(i) : null) || document.getElementById(i);
  const labelOf = el => {
    let t = '';
    if (el.id) { const l = R(el).querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = txt(l); }
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => txt(byId(el, i))).join(' ');
    const al = el.getAttribute('aria-label') || '';    // LinkedIn 2026 may put the QUESTION in a radio's aria-label
    const q = s => s.replace(/[\s*]+/g, ' ').trim();
    if (!t && al && !(el.type === 'radio' && q(al) === q(groupLabel(el)))) t = al;
    if (!t) { const l = el.closest('label'); if (l) t = txt(l); }
    if (!t && (el.type === 'radio' || el.type === 'checkbox')) {     // LinkedIn 2026: empty <label for>, text beside it
      const w = el.closest('[role="radio"], [role="checkbox"]'); if (w) t = txt(w) || w.getAttribute('aria-label') || '';
    }
    if (!t && el.type === 'radio')     // the option's text beside it: the nearest box holding only this input
      for (let c = el.parentElement, i = 0; c && i < 5 && !t && c.querySelectorAll('input').length === 1; c = c.parentElement, i++)
        if (txt(c).length < 120) t = txt(c);
    if (!t) {   // nearest label-like text ABOVE the field (not the first one in the box: that can be a profile header)
      let c = el.parentElement;
      for (let i = 0; i < 5 && c && !t; i++) {
        const cands = [...c.querySelectorAll('label, legend, [class*="label"], [class*="question"], [class*="title"]')]
          .filter(x => !x.contains(el) && !x.querySelector('input, select, textarea') && txt(x) && txt(x).length < 300 &&
                       (x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
        if (cands.length) t = txt(cands[cands.length - 1]);
        c = c.parentElement;
      }
    }
    return (t || el.placeholder || el.name || '').slice(0, 300);
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (fs) {
      const lg = fs.querySelector('legend'); if (lg && txt(lg)) return txt(lg);
      if (fs.getAttribute('aria-label')) return fs.getAttribute('aria-label');
      const lb = fs.getAttribute('aria-labelledby');
      if (lb) return lb.split(/\s+/).map(i => txt(byId(el, i))).join(' ');
      for (let n = fs, i = 0; i < 3 && n; i++, n = n.parentElement) {   // LinkedIn 2026: <p>question*</p><fieldset>
        const prev = n.previousElementSibling;
        if (prev && txt(prev)) { const q = prev.matches('p, label, legend, h3, h4') ? prev : prev.querySelector('p, label, legend, h3, h4');
                                 return txt(q || prev).slice(0, 300); }
      }
    }
    let c = el.parentElement;
    for (let i = 0; i < 6 && c; i++) {
      const radios = c.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`);
      if (radios.length > 1) {
        const cands = [...c.querySelectorAll('legend, label, p, span, div, h3, h4, h5')].filter(x =>
          !x.querySelector('input') && !(x.closest('label') && x.closest('label').querySelector('input')) && txt(x));
        if (cands.length) return txt(cands[0]).slice(0, 300);
      }
      c = c.parentElement;
    }
    return el.name || '';
  };
  const out = []; const groups = {}; window.__jhN = window.__jhN || 0;
  const els = deepAll(root, 'input, select, textarea, [contenteditable="true"]');
  for (const el of els) {
    const type = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA') ? 'textarea'
               : el.isContentEditable && el.tagName !== 'INPUT' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (['hidden','submit','button','image','reset','search'].includes(type)) continue;
    if (el.disabled || el.readOnly && type !== 'file') continue;
    if (!vis(el)) continue;
    const jh = el.getAttribute('data-jh') || String(window.__jhN++);
    el.setAttribute('data-jh', jh);
    const required = !!(el.required || el.getAttribute('aria-required') === 'true');
    if (type === 'radio') {
      const g = el.name || jh;
      if (!groups[g]) { groups[g] = {jh: 'g:' + g, tag: 'radio', type: 'radio', name: g, label: groupLabel(el),
                        required: false, options: [], value: null, placeholder: ''}; out.push(groups[g]); }
      const o = {jh, text: labelOf(el) || el.value, value: el.value, checked: el.checked};
      groups[g].options.push(o); groups[g].required = groups[g].required || required || /\*\s*$/.test(groups[g].label);
      if (el.checked) groups[g].value = o.text;
      continue;
    }
    const f = {jh, tag: el.tagName.toLowerCase(), type, name: el.name || '', label: labelOf(el),
               ac: el.getAttribute('autocomplete') || '', eid: el.id || '',
               required, placeholder: el.placeholder || '', value: null, options: [],
               combo: el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete') ||
                      /select__input|autocomplete|typeahead/i.test(el.className || ''),
               maxlength: el.maxLength > 0 ? el.maxLength : null};
    const help = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean).map(i => txt(byId(el, i))).join(' ');
    if (!f.maxlength) { const m = help.match(/\d+\s*(?:of|\/)\s*(\d+)\s*char/i); if (m) f.maxlength = +m[1]; }
    // LinkedIn number questions are type="text": tell them by inputmode, a "numeric" id, or the helper text
    if (type === 'text' && (/numeric|decimal/.test(el.inputMode) || /numeric/i.test(el.id + ' ' + el.name) ||
                            /whole number|decimal number|enter a number/i.test(help))) f.type = 'number';
    if (type === 'select') { f.options = [...el.options].map(o => ({text: o.text.trim(), value: o.value}));
                             f.value = el.selectedIndex > 0 ? el.options[el.selectedIndex].text.trim() : null; }
    else if (type === 'checkbox') f.value = el.checked;
    else if (type === 'file') f.value = el.files && el.files.length ? el.files[0].name : null;
    else f.value = el.isContentEditable && el.tagName !== 'INPUT' ? txt(el) : el.value;
    if (/\*\s*$/.test(f.label)) f.required = true;
    out.push(f);
  }
  return out;
}
"""

ERRORS_JS = r"""
(rootSel) => {
""" + DEEP_JS + r"""
  const root = rootSel ? deepOne(rootSel) : document;      // asked-for window missing: touch nothing
  if (!root) return [];
  const msgs = [];
  const ERR_TEXT = /^(this field is required|invalid input|.{1,40} is required|please (enter|make|select|provide) .{1,60}|enter a (valid|whole|decimal) .{1,40})\.?$/i;
  deepAll(root, '[aria-invalid="true"], .artdeco-inline-feedback--error, [role="alert"], .error, .field-error, .invalid-feedback, [class*="error-message"]')
    .forEach(e => { const r = e.getBoundingClientRect(); const t = (e.innerText || '').trim();
                    if (r.width > 0 && r.height > 0 && t && t.length < 200 && !/\bsuccess/i.test(t)) msgs.push(t); });
  // LinkedIn 2026: errors are bare <p>/<span> with no role or class ("A resume is required")
  deepAll(root, 'p, span, div').forEach(e => { if (e.children.length) return;
    const t = (e.innerText || '').trim(); const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && ERR_TEXT.test(t)) msgs.push(t); });
  return [...new Set(msgs)].slice(0, 10);
}
"""


TEXT_JS = r"""
() => {
""" + DEEP_JS + r"""
  let t = document.body ? document.body.innerText : '';
  for (const e of deepAll(document, '*')) if (e.shadowRoot) t += '\n' + [...e.shadowRoot.children].map(c => c.innerText || c.textContent || '').join('\n');
  return t;
}
"""


REJECTED_JS = r"""e => { if (e.getAttribute('aria-invalid') === 'true') return true;
  const r = e.getRootNode(); const t = (e.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)
    .map(i => (r.getElementById ? r.getElementById(i) : document.getElementById(i))?.innerText || '').join(' ');
  return /invalid input|enter a (valid|whole|decimal)/i.test(t); }"""
