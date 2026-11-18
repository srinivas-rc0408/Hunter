"""`hunt dashboard`: one self-contained HTML file (data/dashboard.html), no server.

Filter by status/kind/score, search, and copy the `hunt apply <id>` command.
Works offline; safe to open from GitHub Actions artifacts too.
"""
from __future__ import annotations

import html
import json

from . import config
from .db import DB
from .util import iso

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JobHunter</title>
<style>
:root{--bg:#f7f7f5;--fg:#1b1b1b;--mut:#666;--card:#fff;--line:#e4e4e0;--acc:#1f6feb;--good:#1a7f37;--warn:#b35900;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#ececec;--mut:#9a9a9a;--card:#1c1c1c;--line:#2c2c2c;--acc:#58a6ff;--good:#3fb950;--warn:#d29922;--bad:#f85149}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:18px 16px 8px;max-width:1200px;margin:auto}h1{font-size:20px;margin:0}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.stat b{font-size:18px;display:block}.bar{display:flex;gap:8px;flex-wrap:wrap;max-width:1200px;margin:auto;padding:0 16px 10px}
input,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
main{max-width:1200px;margin:auto;padding:0 16px 40px;display:grid;gap:8px}
.job{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;display:grid;grid-template-columns:52px 1fr auto;gap:10px;align-items:start}
.sc{font-weight:700;font-size:18px;text-align:center;border-radius:8px;padding:6px 0;background:var(--bg)}
.t{font-weight:600}.t a{color:var(--fg);text-decoration:none}.t a:hover{color:var(--acc)}.m{color:var(--mut);font-size:13px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);margin:2px 4px 0 0;color:var(--mut)}
.dream{color:var(--warn);border-color:var(--warn)}.govt{color:var(--acc);border-color:var(--acc)}
.st{font-size:12px;font-weight:600;text-transform:uppercase}.applied{color:var(--good)}.needs_you{color:var(--warn)}.rejected,.skipped,.filtered{color:var(--mut)}
details{grid-column:2/4}details summary{cursor:pointer;color:var(--mut);font-size:12px}pre{white-space:pre-wrap;font-size:12px;color:var(--mut);margin:6px 0 0}
button{font:inherit;font-size:12px;padding:5px 9px;border-radius:7px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
@media (max-width:640px){.job{grid-template-columns:44px 1fr}.job>div:last-child{grid-column:2}}
</style></head><body>
<header><h1>JobHunter · Srinivas</h1><div class="m">updated __UPDATED__</div><div class="stats">__STATS__</div></header>
<div class="bar"><input id="q" placeholder="Search title, company, place…" size="28">
<select id="kind"><option value="">All</option><option value="private">Private</option><option value="govt">Govt</option></select>
<select id="st"><option value="active">Active</option><option value="">Any status</option><option>new</option><option>alerted</option><option>needs_you</option><option>applied</option><option>interview</option><option>offer</option><option>rejected</option><option>filtered</option></select>
<input id="min" type="number" value="40" min="0" max="100" style="width:80px" title="min score">
<label class="m"><input type="checkbox" id="dream"> dream only</label></div>
<main id="list"></main>
<script>
const JOBS = __DATA__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function render(){
  const q = $('q').value.toLowerCase(), k = $('kind').value, st = $('st').value, mn = +$('min').value || 0, dr = $('dream').checked;
  const act = ['new','alerted','queued','needs_you','filled','interview','offer'];
  const rows = JOBS.filter(j => (!k || j.kind === k) && (st === '' || (st === 'active' ? act.includes(j.status) : j.status === st))
     && (j.kind === 'govt' || j.score >= mn) && (!dr || j.dream)
     && (!q || (j.title + ' ' + j.company + ' ' + (j.location||'')).toLowerCase().includes(q)));
  $('list').innerHTML = rows.slice(0, 400).map(j => `<div class="job">
    <div class="sc">${j.score}</div>
    <div><div class="t"><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></div>
      <div class="m">${esc(j.company)} · ${esc(j.location)}${j.deadline ? ' · ⏰ ' + esc(j.deadline) : ''}${j.posted_at ? ' · posted ' + esc(j.posted_at) : ''}</div>
      <div>${j.dream ? '<span class="tag dream">★ dream</span>' : ''}${j.kind === 'govt' ? '<span class="tag govt">govt</span>' : ''}
      ${(j.flags||[]).filter(f => f !== 'dream').map(f => `<span class="tag">${esc(f)}</span>`).join('')}
      ${(j.sources||[]).map(s => `<span class="tag">${esc(s)}</span>`).join('')}</div></div>
    <div style="text-align:right"><div class="st ${j.status}">${j.status.replace('_',' ')}</div>
      <button onclick="navigator.clipboard.writeText('hunt apply ${j.id.slice(0,8)}')">copy apply cmd</button></div>
    <details><summary>why · details</summary><pre>${esc((j.reasons||[]).join('\\n'))}\\n\\n${esc((j.description||'').slice(0,1500))}</pre></details>
  </div>`).join('') || '<p class="m">Nothing matches.</p>';
}
['q','kind','st','min','dream'].forEach(i => $(i).addEventListener('input', render)); render();
</script></body></html>"""


def build() -> str:
    db = DB()
    jobs = db.query(limit=3000, order="kind='govt' DESC, dream DESC, score DESC, found_at DESC")
    keep = ("id", "kind", "title", "company", "location", "url", "score", "status", "dream", "flags", "sources",
            "reasons", "deadline", "posted_at", "description")
    data = [{k: j.get(k) for k in keep} for j in jobs]
    for d in data:
        d["description"] = (d["description"] or "")[:1500]
    stats = {
        "jobs tracked": len(data),
        "new / alerted": db.count("status IN ('new','alerted')"),
        "waiting for you": db.count("status='needs_you'"),
        "applied": db.count("status='applied'"),
        "interviews": db.count("status='interview'"),
        "govt notices": db.count("kind='govt'"),
    }
    st_html = "".join(f'<div class="stat"><b>{v}</b>{html.escape(k)}</div>' for k, v in stats.items())
    out = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")) \
              .replace("__STATS__", st_html).replace("__UPDATED__", iso())
    f = config.data_dir() / "dashboard.html"
    f.write_text(out, encoding="utf-8")
    return str(f)
