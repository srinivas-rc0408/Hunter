"""Review Center: http://localhost:8765, your saved link for everything the bot did.

  • Review queue: forms the bot filled but didn't submit (it needed you, or it was a dry run).
    Each shows every field, the value it typed, and where that value came from
    (your fact / learned from you / drafted, check it / missing), the screenshot of the
    filled form, the tailored resume (with its ATS before→after score) and the cover letter.
    "Approve & Submit" re-opens the job in your browser, refills it, and submits.
    If something is still missing, the browser waits for you to type it (and learns it).
  • Applied: every submitted application with the exact details that were sent.
  • Jobs, Govt: the full tracker with filters.

Runs only on 127.0.0.1. Every action needs a per-session token embedded in the page,
and the Host header is checked, so other websites can't trigger anything (CSRF/DNS rebinding).
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config
from .db import DB, STATUSES
from .util import iso

TOKEN = secrets.token_urlsafe(24)
TASK: dict = {"proc": None, "label": "", "log": None, "started": None}
LOCK = threading.Lock()


def _start(label: str, args: list[str]) -> tuple[bool, str]:
    with LOCK:
        p = TASK.get("proc")
        if p is not None and p.poll() is None:
            return False, f"busy: {TASK['label']} is still running"
        logs = config.data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        log = logs / f"{label.replace(' ', '_').replace('/', '_')}.log"
        fh = open(log, "w", encoding="utf-8")
        TASK.update(proc=subprocess.Popen([sys.executable, "-m", "jobhunter.cli", *args], cwd=str(config.home()),
                                          stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                          env={**__import__("os").environ, "COLUMNS": "110"}),
                    label=label, log=log, started=iso())
        return True, f"started: {label}"


def _task() -> dict:
    p = TASK.get("proc")
    if p is None:
        return {"running": False}
    tail = ""
    try:
        tail = "\n".join(Path(TASK["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
    except OSError:
        pass
    return {"running": p.poll() is None, "label": TASK["label"], "started": TASK["started"],
            "code": p.poll(), "tail": tail}


def state() -> dict:
    db = DB()
    apps = db.applications(limit=400)
    by_job: dict[str, list] = {}
    for a in apps:
        by_job.setdefault(a["job_id"], []).append(a)
    jobs = db.query(limit=2000, order="dream DESC, score DESC, found_at DESC")
    keep = ("id", "kind", "title", "company", "location", "url", "urls", "score", "status", "status_at", "dream",
            "flags", "sources", "reasons", "deadline", "posted_at", "found_at", "pay_min", "pay_period", "notes")
    out = []
    for j in jobs:
        d = {k: j.get(k) for k in keep}
        d["description"] = (j.get("description") or "")[:2500]
        d["apps"] = by_job.get(j["id"], [])[:5]
        out.append(d)
    stats = {"tracked": len(out), "to review": sum(1 for j in out if j["status"] in ("needs_you",)),
             "applied": db.count("status='applied'"), "interviews": db.count("status='interview'"),
             "offers": db.count("status='offer'"), "govt": db.count("kind='govt'"),
             "applied today": db.applied_today()}
    return {"jobs": out, "stats": stats, "task": _task(), "now": iso(), "statuses": STATUSES}


def _safe_file(p: str) -> Path | None:
    try:
        f = Path(unquote(p)).resolve()
    except (OSError, ValueError):
        return None
    allowed = [config.data_dir().resolve(), config.path("config").resolve(), (config.home() / "hunter").resolve()]
    if f.is_file() and any(str(f).startswith(str(a)) for a in allowed) and f.suffix.lower() in (
            ".png", ".pdf", ".txt", ".md", ".log", ".html"):
        return f
    return None


class H(BaseHTTPRequestHandler):
    server_version = "JobHunterReview/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str, ensure_ascii=False).encode())

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden host", "text/plain")
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._json(state())
        if u.path == "/api/task":
            return self._json(_task())
        if u.path == "/file":
            f = _safe_file(parse_qs(u.query).get("p", [""])[0])
            if not f:
                return self._send(404, b"not found", "text/plain")
            return self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-JH-Token") != TOKEN:
            return self._send(403, b"forbidden", "text/plain")
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        db = DB()
        if len(parts) >= 3 and parts[0] == "api":
            act, jid = parts[1], parts[2]
            j = db.get(jid)
            if not j and act != "scan":
                return self._json({"ok": False, "msg": "no such job"}, 404)
            if act in ("approve", "dry", "fill") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return self._json({"ok": False, "msg": "This service can't see your screen, so it can't open the browser. "
                                   f"Run in a terminal: hunt apply {jid[:8]}{' --approve' if act == 'approve' else ''}"
                                   " (or re-run scripts/install-services.sh from your desktop session)"})
            if act == "approve":
                ok, msg = _start(f"approve {jid[:8]}", ["apply", jid, "--approve"])
                return self._json({"ok": ok, "msg": msg})
            if act == "dry":
                ok, msg = _start(f"preview {jid[:8]}", ["apply", jid, "--dry-run"])
                return self._json({"ok": ok, "msg": msg})
            if act == "fill":
                ok, msg = _start(f"apply {jid[:8]}", ["apply", jid])
                return self._json({"ok": ok, "msg": msg})
            if act == "mark" and len(parts) == 4 and parts[3] in STATUSES:
                db.set_status(j["id"], parts[3], "review center")
                return self._json({"ok": True, "msg": f"marked {parts[3]}"})
        if parts[:2] == ["api", "scan"]:
            ok, msg = _start("scan", ["scan"])
            return self._json({"ok": ok, "msg": msg})
        if parts[:2] == ["api", "queue"]:
            ok, msg = _start("apply queue", ["apply", "--top", "10"])
            return self._json({"ok": ok, "msg": msg})
        return self._json({"ok": False, "msg": "unknown action"}, 400)


def serve(port: int = 8765, open_browser: bool = False) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://localhost:{port}"
    print(f"Review Center: {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Review Center</title>
<style>
:root{--bg:#f6f6f3;--fg:#1a1a1a;--mut:#666;--card:#fff;--line:#e2e2dc;--acc:#1f5fbf;--good:#1a7f37;--goodbg:#e8f5ec;
--warn:#9a5b00;--warnbg:#fff4df;--bad:#b42318;--badbg:#fdecea;--info:#5b3cc4;--infobg:#efeafd}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#ececec;--mut:#9b9b9b;--card:#1b1b1b;--line:#2d2d2d;--acc:#6ea8fe;
--good:#4cc36b;--goodbg:#14301d;--warn:#e0a84a;--warnbg:#33260f;--bad:#ff7b72;--badbg:#3a1614;--info:#b39dff;--infobg:#251d3d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--acc)}header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.wrap{max-width:1180px;margin:auto;padding:12px 16px}h1{font-size:18px;margin:0}
.top{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.stats{display:flex;gap:6px;flex-wrap:wrap}.stat{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:3px 10px;font-size:12px}
.stat b{font-size:13px}.tabs{display:flex;gap:4px;margin-top:10px;overflow-x:auto}
.tab{border:1px solid var(--line);background:var(--card);color:var(--fg);padding:6px 12px;border-radius:8px;cursor:pointer;white-space:nowrap;font:inherit}
.tab.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}input,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
button.b{font:inherit;font-size:13px;padding:6px 11px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.p{background:var(--good);border-color:var(--good);color:#fff;font-weight:600}button.b:disabled{opacity:.5;cursor:wait}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.ch{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}.t{font-weight:650;font-size:15px}.m{color:var(--mut);font-size:12.5px}
.pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:999px;margin:2px 4px 0 0;border:1px solid var(--line);color:var(--mut)}
.pill.dream{color:var(--warn);border-color:var(--warn)}.pill.govt{color:var(--acc);border-color:var(--acc)}
.st{font-weight:700;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em}
.s-applied,.s-offer,.s-interview{color:var(--good)}.s-needs_you{color:var(--warn)}.s-rejected,.s-skipped,.s-filtered,.s-expired{color:var(--mut)}
.grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:14px;margin-top:10px}
@media (max-width:820px){.grid{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11.5px;color:var(--mut);font-weight:600}td.v{word-break:break-word}
.src{font-size:11px;font-weight:600;padding:1px 7px;border-radius:6px;white-space:nowrap}
.src.profile,.src.consent{background:var(--goodbg);color:var(--good)}.src.learned{background:var(--infobg);color:var(--info)}
.src.drafted{background:var(--warnbg);color:var(--warn)}.src.none{background:var(--badbg);color:var(--bad)}.src.opt{color:var(--mut);border:1px solid var(--line)}
tr.miss td{background:var(--badbg)}img.shot{width:100%;border:1px solid var(--line);border-radius:8px;cursor:zoom-in;max-height:420px;object-fit:cover;object-position:top}
.acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}details summary{cursor:pointer;color:var(--mut);font-size:12.5px;margin-top:6px}
pre{white-space:pre-wrap;font-size:12.5px;background:var(--bg);padding:10px;border-radius:8px;border:1px solid var(--line);max-height:320px;overflow:auto}
.why{font-size:13px;margin-top:6px}.ats{font-size:12.5px}.ats b{font-size:14px}
#task{position:fixed;right:12px;bottom:12px;max-width:min(560px,calc(100vw - 24px));background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;display:none;box-shadow:0 6px 24px rgba(0,0,0,.15)}
#task pre{max-height:180px;margin:6px 0 0}#zoom{position:fixed;inset:0;background:rgba(0,0,0,.8);display:none;align-items:flex-start;justify-content:center;overflow:auto;z-index:9;padding:20px}
#zoom img{max-width:min(1100px,100%)}.empty{color:var(--mut);padding:30px;text-align:center}
.jrow{display:grid;grid-template-columns:48px 1fr auto;gap:10px;align-items:start}.sc{font-weight:700;text-align:center;background:var(--bg);border-radius:8px;padding:6px 0}
</style></head><body>
<header><div class="wrap"><div class="top"><h1>Review Center</h1><div class="stats" id="stats"></div>
<div style="display:flex;gap:6px"><button class="b" onclick="act('scan','x')">Scan now</button><button class="b" onclick="act('queue','x')">Apply to queue (top 10)</button></div></div>
<div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap"><div class="tabs" id="tabs" style="margin:0"></div>
<input id="q" placeholder="Search title, company, place…" oninput="Q=this.value;render()" style="flex:1;min-width:180px"></div></div></header>
<main class="wrap" id="main"></main>
<div id="task"><div class="ch"><b id="tl"></b><button class="b" onclick="document.getElementById('task').style.display='none'">×</button></div><pre id="tt"></pre></div>
<div id="zoom" onclick="this.style.display='none'"><img id="zimg"></div>
<script>
const TOKEN="__TOKEN__"; let S=null, TAB=localStorage.getItem('jh_tab')||'review', Q='';
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const SRC={profile:'your fact',consent:'consent',learned:'learned from you',drafted:'drafted, check it',none:'missing',opt:'optional, left empty'};
const TABS=[['review','To review'],['applied','Applied'],['active','Jobs'],['govt','Govt'],['all','Everything']];
async function load(){ S=await (await fetch('/api/state')).json(); render(); }
function file(p){return '/file?p='+encodeURIComponent(p)}
async function act(a,id,extra){ const btns=document.querySelectorAll('button'); btns.forEach(b=>b.disabled=true);
  try{ const r=await fetch(`/api/${a}/${id}${extra?'/'+extra:''}`,{method:'POST',headers:{'X-JH-Token':TOKEN}}); const j=await r.json();
    toast(j.msg||'done'); }finally{ btns.forEach(b=>b.disabled=false); } load(); poll(); }
function toast(m){ $('#task').style.display='block'; $('#tl').textContent=m; }
async function poll(){ const t=await (await fetch('/api/task')).json(); if(!t.label) return;
  $('#task').style.display='block'; $('#tl').textContent=(t.running?'⏳ ':'✓ ')+t.label+(t.running?'':' (finished)'); $('#tt').textContent=t.tail||'';
  $('#tt').scrollTop=1e9; if(t.running) setTimeout(poll,2000); else load(); }
function zoom(src){ $('#zimg').src=src; $('#zoom').style.display='flex'; }
function fieldsTable(fs){ if(!fs||!fs.length) return '<p class="m">No form fields were recorded (one-click apply, or the page had no form).</p>';
  return `<table><tr><th>Field</th><th>What the bot entered</th><th>Source</th></tr>${fs.map(f=>{
    const miss=f.required&&!f.basic&&f.basis!=='drafted'; const v=f.value===true?'✓ ticked':f.value===false?'left unticked':(f.value??'');
    const opt=!f.required&&(f.value===null||f.value===''); const basis=opt?'opt':f.basis;
    return `<tr class="${miss?'miss':''}"><td>${esc(String(f.label||'').replace(/\s*\*\s*$/,''))}${f.required?' <span class=m>*</span>':''}</td>
    <td class="v">${v===''?`<span class=m>${esc(f.note||'(empty)')}</span>`:esc(String(v).slice(0,400))}</td>
    <td><span class="src ${esc(basis)}">${SRC[basis]||esc(basis)}</span></td></tr>`}).join('')}</table>`; }
function appCard(j){ const a=(j.apps||[])[0]; const rr=a&&a.resume_report||{};
  const missing=a?(a.fields||[]).filter(f=>f.required&&!f.basic&&f.basis!=='drafted').length:0;
  return `<div class="card" id="j${j.id.slice(0,8)}"><div class="ch"><div>
    <div class="t">${j.dream?'★ ':''}${esc(j.title)}</div>
    <div class="m">${esc(j.company)} · ${esc(j.location)} · score ${j.score} · <a href="${esc(j.url)}" target="_blank" rel="noopener">job page ↗</a></div>
    ${a?`<div class="why"><b>${a.dry?'Preview (dry run)':a.outcome==='applied'?(a.auto?'Submitted automatically':'Submitted'):'Waiting for you'}</b> · ${esc(a.site)} · ${esc(a.at.slice(0,16).replace('T',' '))}<br><span class="m">${esc(a.why)}</span></div>`:''}
    </div><div class="st s-${j.status}">${esc(j.status.replace('_',' '))}</div></div>
    ${a?`<div class="grid"><div>${fieldsTable(a.fields)}
      ${missing?`<p class="m" style="color:var(--bad)">${missing} required field(s) missing. Approve & Submit opens your browser; type them there and the bot learns them for next time.</p>`:''}</div>
      <div>${a.screenshot?`<img class="shot" loading="lazy" src="${file(a.screenshot)}" onclick="zoom(this.src)" alt="filled form">`:'<p class="m">No screenshot.</p>'}
      ${a.resume?`<p class="ats">Resume: <a href="${file(a.resume)}" target="_blank">${esc(a.resume.split('/').pop())}</a>${rr.after!=null?`<br>ATS (hunter-resume formula): full resume ${rr.before} → one-page tailored <b>${rr.after}</b> · ${esc(rr.how||'')} ${rr.clean===false?'<span style="color:var(--bad)">truth check FAILED</span>':'· truth check clean'}`:''}
        ${rr.missing&&rr.missing.length?`<br><span class=m>JD asks, resume lacks: ${esc(rr.missing.join(', '))}</span>`:''}</p>`:''}
      ${a.cover?`<details><summary>Cover letter used</summary><pre>${esc(a.cover)}</pre></details>`:''}</div></div>`:
      `<p class="m">Not filled yet.</p>`}
    <div class="acts">
      ${['applied','offer','interview','rejected'].includes(j.status)?'':`<button class="b p" onclick="act('approve','${j.id}')" title="Re-opens the job in your browser, refills it with these values and submits. Waits for you if something is missing.">Approve &amp; Submit</button>
      <button class="b" onclick="act('dry','${j.id}')">Fill again (preview)</button>
      <button class="b" onclick="act('mark','${j.id}','applied')">I submitted it myself</button>
      <button class="b" onclick="act('mark','${j.id}','skipped')">Skip</button>`}
      <button class="b" onclick="act('mark','${j.id}','interview')">Got interview</button>
      <button class="b" onclick="act('mark','${j.id}','rejected')">Rejected</button>
      <details style="width:100%"><summary>Why it matched · description</summary><pre>${esc((j.reasons||[]).join('\n'))}\n\n${esc(j.description)}</pre></details></div></div>`; }
function jobRow(j){ return `<div class="card jrow"><div class="sc">${j.score}</div><div>
  <div class="t" style="font-size:14px">${j.dream?'★ ':''}${j.kind==='govt'?'🏛 ':''}<a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></div>
  <div class="m">${esc(j.company)} · ${esc(j.location)}${j.deadline?' · ⏰ last date '+esc(j.deadline):''}${j.posted_at?' · posted '+esc(j.posted_at):''}</div>
  <div>${(j.flags||[]).map(f=>`<span class="pill ${f==='dream'?'dream':''}">${esc(f)}</span>`).join('')}${(j.sources||[]).map(s=>`<span class="pill">${esc(s)}</span>`).join('')}</div>
  <details><summary>${j.kind==='govt'?'Eligibility':'Why'} · details</summary><pre>${esc((j.reasons||[]).join('\n'))}\n\n${esc(j.description)}</pre></details></div>
  <div style="text-align:right"><div class="st s-${j.status}">${esc(j.status.replace('_',' '))}</div>
  ${j.kind==='govt'?`<div class="acts" style="justify-content:flex-end"><button class="b" onclick="act('mark','${j.id}','applied')">Applied</button><button class="b" onclick="act('mark','${j.id}','skipped')">Skip</button></div>`:
   ['new','alerted','queued'].includes(j.status)?`<div class="acts" style="justify-content:flex-end"><button class="b" onclick="act('dry','${j.id}')">Preview</button><button class="b p" onclick="act('fill','${j.id}')">Apply</button><button class="b" onclick="act('mark','${j.id}','skipped')">Skip</button></div>`:''}</div></div>`; }
function render(){ const st=S.stats; $('#stats').innerHTML=Object.entries(st).map(([k,v])=>`<span class="stat"><b>${v}</b> ${esc(k)}</span>`).join('');
  $('#tabs').innerHTML=TABS.map(([k,l])=>`<button class="tab ${k===TAB?'on':''}" onclick="TAB='${k}';localStorage.setItem('jh_tab',TAB);render()">${l}</button>`).join('');
  const q=Q.toLowerCase(), m=j=>!q||(j.title+' '+j.company+' '+j.location).toLowerCase().includes(q);
  let html='<div style="height:12px"></div>';
  const J=S.jobs.filter(m);
  if(TAB==='review'){ const L=J.filter(j=>j.kind!=='govt'&&(j.status==='needs_you'||(j.apps&&j.apps.length&&j.apps[0].dry&&!['applied','skipped','rejected'].includes(j.status))));
    html+=L.length?L.map(appCard).join(''):'<div class="empty">Nothing to review. Forms the bot fills but doesn\'t submit land here.</div>'; }
  else if(TAB==='applied'){ const L=J.filter(j=>['applied','interview','offer','rejected'].includes(j.status)).sort((a,b)=>(b.status_at||'').localeCompare(a.status_at||''));
    html+=L.length?L.map(appCard).join(''):'<div class="empty">No applications yet.</div>'; }
  else if(TAB==='active'){ html+=J.filter(j=>j.kind!=='govt'&&['new','alerted','queued'].includes(j.status)).slice(0,300).map(jobRow).join('')||'<div class="empty">No open jobs. Run a scan.</div>'; }
  else if(TAB==='govt'){ html+=J.filter(j=>j.kind==='govt').sort((a,b)=>(a.deadline||'9').localeCompare(b.deadline||'9')).map(jobRow).join('')||'<div class="empty">No govt notices yet.</div>'; }
  else html+=J.slice(0,500).map(jobRow).join('');
  $('#main').innerHTML=html; if(location.hash){const e=document.getElementById('j'+location.hash.slice(1)); if(e){e.scrollIntoView(); location.hash='';}} }
load(); poll(); setInterval(load,30000);
</script></body></html>"""
