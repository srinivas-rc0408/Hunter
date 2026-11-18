"""Review Center: http://localhost:8765, your saved link for everything the bot did.

  • Review queue: forms the bot filled but didn't submit (it needed you, or it was a dry run).
    Each shows every field, the value it typed, and where that value came from
    (your fact / learned from you / drafted, check it / missing), the screenshot of the
    filled form, the tailored resume (with its ATS before→after score) and the cover letter.
    "Open & check" re-opens the job in the bot browser with the check panel; "Submit as shown" sends it as recorded.
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
    from .apply.questions import pending
    stats = {"tracked": len(out), "to review": sum(1 for j in out if j["status"] in ("needs_you",)),
             "questions": len(pending()),
             "applied": db.count("status='applied'"), "interviews": db.count("status='interview'"),
             "offers": db.count("status='offer'"), "govt": db.count("kind='govt'"),
             "applied today": db.applied_today()}
    import datetime as _dt
    week = (_dt.datetime.now() - _dt.timedelta(days=7)).isoformat()
    stats["applied this week"] = sum(1 for j in out if j["status"] in ("applied", "interview", "offer", "rejected")
                                     and (j.get("status_at") or "") >= week)
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
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/companies/suggest":
            from . import companies
            return self._json(companies.suggest(qs.get("q", ""), online=qs.get("online", "1") == "1"))
        if u.path == "/api/company":
            from . import companies
            if not qs.get("name"):
                return self._json({"error": "name required"}, 400)
            info = companies.resolve(qs["name"], qs.get("domain") or None, qs.get("wikidata") or None,
                                     refresh=qs.get("refresh") == "1")
            res = companies.listings(info, boards_search=qs.get("boards") == "1")
            res["links"] = companies.links(info)
            return self._json(res)
        if u.path == "/api/questions":
            from .apply.questions import pending
            return self._json(pending())
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
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if 0 < n < 5_000_000 else {}
        except ValueError:
            body = {}
        if parts == ["api", "answer"]:
            from .apply.questions import answer
            try:
                res = answer(body.get("key", ""), str(body.get("value", "")))
            except (KeyError, ValueError) as exc:
                return self._json({"ok": False, "msg": str(exc)})
            return self._json({"ok": True, "msg": f"saved: {res['label'][:50]} → {res['value']}"
                               + (f" · {len(res['requeued'])} job(s) back in the queue" if res["requeued"] else "")})
        if parts == ["api", "fix"]:
            from .apply.fixes import fix
            try:
                ok, msg = fix(DB(), str(body.get("job", "")), int(body.get("n", 0)), str(body.get("value", "")))
            except (TypeError, ValueError):
                ok, msg = False, "bad request"
            return self._json({"ok": ok, "msg": msg})
        if parts == ["api", "dream"]:
            from . import dreams
            name, add = str(body.get("name", "")).strip(), body.get("action", "add") == "add"
            ok, msg = (dreams.add if add else dreams.remove)(name)
            if ok:
                threading.Thread(target=dreams.after_change, args=(name, add), daemon=True).start()
                msg += " · re-scoring, watching its careers site and pushing in the background"
            return self._json({"ok": ok, "msg": msg})
        if parts == ["api", "company", "watch"]:
            from . import companies
            info = companies.resolve(body.get("name", ""), body.get("domain"), body.get("wikidata"))
            return self._json({"ok": True, "msg": "watching: every scan now includes it" if companies.watch(info)
                               else "already watched, or no platform/careers page found"})
        if parts == ["api", "company", "track"]:
            from . import companies
            rows = [x for x in body.get("rows", []) if isinstance(x, dict) and x.get("title")][:30]
            if not rows:
                return self._json({"ok": False, "msg": "select at least one listing"})
            ids = companies.track(rows)
            how = body.get("action", "track")
            if how in ("preview", "apply"):
                if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                    return self._json({"ok": False, "msg": "Queued. This service can't open a browser: run `hunt apply "
                                       + " ".join(i[:8] for i in ids) + "` in a terminal"})
                ok, msg = _start(f"{how} {len(ids)} job(s)", ["apply", *ids] + (["--dry-run"] if how == "preview" else []))
                return self._json({"ok": ok, "msg": msg})
            return self._json({"ok": True, "msg": f"{len(ids)} job(s) added to your apply queue"})
        db = DB()
        if len(parts) >= 3 and parts[0] == "api":
            act, jid = parts[1], parts[2]
            j = db.get(jid)
            if not j and act != "scan":
                return self._json({"ok": False, "msg": "no such job"}, 404)
            if act in ("approve", "submitnow", "dry", "fill") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return self._json({"ok": False, "msg": "This service can't see your screen, so it can't open the browser. "
                                   f"Run in a terminal: hunt apply {jid[:8]}{' --approve' if act == 'approve' else ''}"
                                   " (or re-run scripts/install-services.sh from your desktop session)"})
            if act == "approve":
                ok, msg = _start(f"check {jid[:8]} in the browser", ["apply", jid, "--approve"])
                return self._json({"ok": ok, "msg": (msg + " · read the form in the bot's browser window and press "
                                                     "Submit on the JobHunter panel") if ok else msg})
            if act == "submitnow":
                ok, msg = _start(f"submit {jid[:8]}", ["apply", jid, "--submit"])
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
        if parts[:2] == ["api", "browser"]:
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return self._json({"ok": False, "msg": "This service can't open windows: run `hunt browser` in a terminal"})
            url = str(body.get("url") or "")
            ok, msg = _start("bot browser", ["browser"] + ([url] if url.startswith("http") else []))
            return self._json({"ok": ok, "msg": msg + (" · press 🎯 Fill this form on any application page" if ok else "")})
        if parts[:2] == ["api", "applyn"]:
            n = max(1, min(20, int(body.get("n") or 5)))
            ok, msg = _start(f"apply to best {n}", ["apply", "--top", str(n)])
            return self._json({"ok": ok, "msg": msg + (" · check each form in the bot's browser and press Submit" if ok else "")})
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
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JobHunter</title>
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
.sugbox{display:none;position:absolute;left:0;right:0;top:100%;z-index:6;background:var(--card);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.15);max-height:360px;overflow:auto}
.sg{padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--line)}.sg:hover{background:var(--bg)}
.pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut);margin:1px 3px 1px 0}
.jrow{display:grid;grid-template-columns:48px 1fr auto;gap:10px;align-items:start}.sc{font-weight:700;text-align:center;background:var(--bg);border-radius:8px;padding:6px 0}
</style></head><body>
<header><div class="wrap"><div class="top"><h1>🎯 JobHunter</h1><div class="stats" id="stats"></div>
<div style="display:flex;gap:6px"><button class="b" onclick="act('scan','x')">Scan now</button><button class="b" onclick="act('queue','x')">Apply to queue (top 10)</button></div></div>
<div style="display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap"><div class="tabs" id="tabs" style="margin:0"></div>
<input id="q" placeholder="Search title, company, place…" oninput="Q=this.value;render()" style="flex:1;min-width:180px"></div></div></header>
<main class="wrap" id="main"></main>
<div id="task"><div class="ch"><b id="tl"></b><button class="b" onclick="document.getElementById('task').style.display='none'">×</button></div><pre id="tt"></pre></div>
<div id="zoom" onclick="this.style.display='none'"><img id="zimg"></div>
<script>
const TOKEN="__TOKEN__"; let S=null, TAB=localStorage.getItem('jh_tab')||'home', Q='';
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const SRC={profile:'your details',consent:'consent',learned:'learned from you',you:'you changed it',drafted:'AI draft: read it',none:'EMPTY',opt:'optional, empty'};
async function fixField(jid,n){ const el=document.getElementById('fx'+jid.slice(0,8)+'_'+n); const v=(el.value||'').trim(); if(!v){toast('type the new value');return;}
  const j=await post('/api/fix',{job:jid,n:n,value:v}); toast(j.msg); if(j.ok) load(); }
function editRow(jid,n){ const c=document.getElementById('ed'+jid.slice(0,8)+'_'+n); c.style.display=c.style.display==='none'?'':'none'; }
async function dream(name,action){ const j=await post('/api/dream',{name,action:action||'add'}); toast(j.msg); load(); }
const TABS=[['home','Dashboard'],['review','To review'],['questions','Questions'],['companies','Companies'],['applied','Applied'],['active','Jobs'],['govt','Govt'],['all','Everything']];
let QS=[], CO=null, CQ='', CSUG=[], CF={ka:true,fit:false,t:''}, CSEL=new Set(), CBUSY='';
async function loadQ(){ QS=await (await fetch('/api/questions')).json(); if(TAB==='questions') render(); }
async function post(path, body){ const r=await fetch(path,{method:'POST',headers:{'X-JH-Token':TOKEN,'Content-Type':'application/json'},body:JSON.stringify(body||{})}); return r.json(); }
async function answerQ(i){ const el=document.getElementById('qa'+i); const v=el.value.trim(); if(!v){toast('type an answer first');return;}
  const j=await post('/api/answer',{key:QS[i].key,value:v}); toast(j.msg); await loadQ(); load(); }
function questionsView(){ if(!QS.length) return '<div class="empty">No open questions. When a form asks something that isn\'t in your files, it appears here. Nothing is ever guessed.</div>';
  return '<p class="m">Answer with true facts only. Each answer is saved once and reused on every form; jobs waiting only on these go back into the apply queue.</p>'+
  QS.map((q,i)=>`<div class="card"><div class="t">${esc(q.label)}</div><div class="m">${q.jobs.length} job(s) waiting · seen on ${esc((q.sites||[]).join(', ')||'?')} · first asked ${esc((q.first_asked||'').slice(0,10))}</div>
   <div class="acts">${q.options&&q.options.length?`<select id="qa${i}"><option value="">choose…</option>${q.options.map(o=>`<option>${esc(o)}</option>`).join('')}</select>`:
   `<input id="qa${i}" placeholder="your answer" style="flex:1;min-width:220px" onkeydown="if(event.key==='Enter')answerQ(${i})">`}
   <button class="b p" onclick="answerQ(${i})">Save answer</button></div></div>`).join(''); }
let sugT=null;
function csInput(v){ CQ=v; clearTimeout(sugT); if(v.trim().length<2){CSUG=[];drawSug();return;}
  sugT=setTimeout(async()=>{ CSUG=await (await fetch('/api/companies/suggest?q='+encodeURIComponent(v))).json(); drawSug(); },250); }
function drawSug(){ const el=$('#sug'); if(!el) return; el.innerHTML=CSUG.map((s,i)=>`<div class="sg" onmousedown="pickCo(${i})"><b>${esc(s.name)}</b> <span class="m">${esc(s.hint||'')} · ${esc(s.source)}</span></div>`).join(''); el.style.display=CSUG.length?'block':'none'; }
async function pickCo(i){ const s=CSUG[i]; CSUG=[]; CQ=s.name; CO=null; CSEL=new Set(); CBUSY='Finding '+s.name+'\'s careers page and hiring platform…'; render();
  const p=new URLSearchParams({name:s.name,domain:s.domain||'',wikidata:s.wikidata||''});
  CO=await (await fetch('/api/company?'+p+'&boards=0')).json(); CBUSY='Adding LinkedIn / Naukri / Indeed results…'; render();
  try{ const full=await (await fetch('/api/company?'+p+'&boards=1')).json(); if(full.listings) CO=full; }catch(e){}
  CBUSY=''; render(); }
function coRows(){ if(!CO||!CO.listings) return []; const t=CF.t.toLowerCase();
  return CO.listings.filter(r=>(!CF.ka||r.karnataka)&&(!CF.fit||r.fit)&&(!t||(r.title+' '+(r.location||'')).toLowerCase().includes(t))); }
async function coAct(action){ const rows=coRows().filter(r=>CSEL.has(r.id)); if(!rows.length){toast('tick at least one listing');return;}
  const j=await post('/api/company/track',{rows,action}); toast(j.msg); CSEL=new Set(); load(); poll(); }
async function coWatch(){ const j=await post('/api/company/watch',{name:CO.company.name,domain:CO.company.domain,wikidata:CO.company.wikidata}); toast(j.msg); }
function companiesView(){ let h=`<div style="position:relative;max-width:640px"><input id="cq" value="${esc(CQ)}" placeholder="Type a company, e.g. Birla, Sarvam, Bosch…" autocomplete="off"
   oninput="csInput(this.value)" onblur="setTimeout(()=>{const e=$('#sug');if(e)e.style.display='none'},150)" style="width:100%;font-size:15px;padding:10px 12px">
   <div id="sug" class="sugbox"></div></div>`;
  if(CBUSY) h+=`<p class="m">⏳ ${esc(CBUSY)}</p>`;
  if(!CO) return h+'<p class="m" style="margin-top:14px">Pick a company to see every open listing: its own careers platform (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Keka, Darwinbox, Zoho…), LinkedIn, Naukri, Indeed, and jobs already in your tracker.</p>';
  const c=CO.company, k=CO.counts, rows=coRows();
  h+=`<div class="card" style="margin-top:12px"><div class="ch"><div><div class="t">${esc(c.name)}</div>
    <div class="m">${[c.description,c.employees?('~'+c.employees+' employees'):'',c.founded?('founded '+c.founded):''].filter(Boolean).map(esc).join(' · ')}</div>
    <div class="m">platform: <b>${(c.boards||[]).map(b=>esc(b.type)).join(', ')||'none found'}</b>${c.careers_url?` · <a href="${esc(c.careers_url)}" target="_blank" rel="noopener">careers page ↗</a>`:''}${c.website?` · <a href="${esc(c.website)}" target="_blank" rel="noopener">website ↗</a>`:''}</div>
    ${(c.notes||[]).map(n=>`<div class="m" style="color:var(--warn)">${esc(n)}</div>`).join('')}</div>
    <div><button class="b" onclick="coWatch()">👁 Watch company</button> <button class="b p" onclick="dream(CO.company.name)">★ Make it a dream company</button></div></div>
    <div class="stats" style="margin-top:8px"><span class="stat"><b>${k.total}</b> listings</span><span class="stat"><b>${k.karnataka}</b> Karnataka/remote</span><span class="stat"><b>${k.fit}</b> fit your rules</span><span class="stat"><b>${k.new_24h}</b> new in 24 h</span></div>
    <details style="margin-top:8px"><summary>Referrals & research: REVA alumni there, recruiters, reviews, salaries</summary>
    <div class="acts">${Object.entries(CO.links||{}).filter(([a,b])=>b).map(([a,b])=>`<a class="b" href="${esc(b)}" target="_blank" rel="noopener" style="text-decoration:none">${esc(a)} ↗</a>`).join('')}</div></details></div>
    <div class="bar"><label class="m"><input type="checkbox" ${CF.ka?'checked':''} onchange="CF.ka=this.checked;render()"> Karnataka / remote-India only</label>
    <label class="m"><input type="checkbox" ${CF.fit?'checked':''} onchange="CF.fit=this.checked;render()"> fits my rules only</label>
    <input placeholder="filter titles…" value="${esc(CF.t)}" onchange="CF.t=this.value;render()">
    <button class="b" onclick="coAct('track')">Add ticked to queue</button><button class="b" onclick="coAct('preview')">Preview ticked</button><button class="b p" onclick="coAct('apply')">Apply to ticked</button></div>
    <div style="overflow-x:auto"><table style="min-width:760px"><tr><th></th><th>Posted</th><th>Role</th><th>Location</th><th>Type</th><th>Via</th><th>Fit</th><th>Status</th><th></th></tr>
    ${rows.slice(0,400).map(r=>`<tr><td><input type="checkbox" ${CSEL.has(r.id)?'checked':''} onchange="this.checked?CSEL.add('${r.id}'):CSEL.delete('${r.id}')"></td>
      <td style="white-space:nowrap">${r.days_ago==null?'?':(r.days_ago===0?'🔥 today':r.days_ago+'d ago')}<div class="m">${esc(r.posted||'')}</div></td>
      <td><b>${esc(r.title)}</b><details><summary>details</summary><pre>${esc(r.why||'')}\n\n${esc(r.description||'')}</pre></details></td>
      <td>${esc(r.location||'')}${r.karnataka?' <span class="pill">KA</span>':''}${r.remote?' <span class="pill">remote</span>':''}</td>
      <td>${esc(r.job_type||'')}</td><td>${(r.sources||[]).map(s=>`<span class="pill">${esc(s)}</span>`).join('')}</td>
      <td>${r.fit?r.score:'<span class="m" title="outside your rules">—</span>'}</td><td class="st s-${esc(r.status||'')}">${esc(r.status||'')}</td>
      <td><a href="${esc(r.apply_url||r.url)}" target="_blank" rel="noopener">open ↗</a></td></tr>`).join('')}</table></div>
    ${rows.length?'':'<div class="empty">No listings match these filters. Untick "Karnataka only" to see all.</div>'}
    ${(CO.errors||[]).length?`<p class="m">some sources failed: ${esc(CO.errors.join('; '))}</p>`:''}`;
  return h; }
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
function fieldsTable(fs,jid){ if(!fs||!fs.length) return '<p class="m">Nothing was filled on this try (the page didn\'t load, or it had no form). Press <b>Fill again (preview)</b>.</p>';
  return `<p class="m">✎ = change a value. Your change is saved as your answer and used on every future form.</p><table><tr><th>#</th><th>Field</th><th>What the bot entered</th><th>From</th><th></th></tr>${fs.map((f,i)=>{
    const n=i+1, miss=f.required&&!f.basic&&!['drafted','you'].includes(f.basis); const v=f.value===true?'✓ ticked':f.value===false?'left unticked':(f.value??'');
    const opt=!f.required&&(f.value===null||f.value===''); const basis=opt?'opt':f.basis; const id=jid.slice(0,8)+'_'+n;
    const input=(f.options&&f.options.length)?`<select id="fx${id}"><option value="">choose…</option>${f.options.map(o=>`<option>${esc(o)}</option>`).join('')}</select>`:
      f.type==='checkbox'?`<select id="fx${id}"><option>Yes</option><option>No</option></select>`:`<input id="fx${id}" value="${esc(typeof v==='string'?v:'')}" style="min-width:200px" onkeydown="if(event.key==='Enter')fixField('${jid}',${n})">`;
    return `<tr class="${miss?'miss':''}"><td class="m">${n}</td><td>${esc(String(f.label||'').replace(/\s*\*\s*$/,''))}${f.required?' <span class=m>*</span>':''}</td>
    <td class="v">${v===''?`<span class=m>${esc(f.note||'(empty)')}</span>`:esc(String(v).slice(0,400))}${basis==='you'&&f.note?`<div class="m">${esc(f.note)}</div>`:''}
      <div id="ed${id}" style="display:none;margin-top:4px">${input} <button class="b p" onclick="fixField('${jid}',${n})">Save</button></div></td>
    <td><span class="src ${esc(basis)}">${SRC[basis]||esc(basis)}</span></td><td>${f.type==='file'?'':`<button class="b" title="change this value" onclick="editRow('${jid}',${n})">✎</button>`}</td></tr>`}).join('')}</table>`; }
function appCard(j){ const a=(j.apps||[])[0]; const rr=a&&a.resume_report||{};
  const missing=a?(a.fields||[]).filter(f=>f.required&&!f.basic&&f.basis!=='drafted').length:0;
  return `<div class="card" id="j${j.id.slice(0,8)}"><div class="ch"><div>
    <div class="t">${j.dream?'★ ':''}${esc(j.title)}</div>
    <div class="m">${esc(j.company)} · ${esc(j.location)} · score ${j.score} · <a href="${esc(j.url)}" target="_blank" rel="noopener">job page ↗</a></div>
    ${a?`<div class="why"><b>${a.dry?'Preview (dry run)':a.outcome==='applied'?(a.auto?'Submitted automatically':'Submitted'):'Waiting for you'}</b> · ${esc(a.site)} · ${esc(a.at.slice(0,16).replace('T',' '))}<br><span class="m">${esc(a.why)}</span></div>`:''}
    </div><div class="st s-${j.status}">${esc(j.status.replace('_',' '))}</div></div>
    ${a?`<div class="grid"><div>${fieldsTable(a.fields,j.id)}
      ${missing?`<p class="m" style="color:var(--bad)">${missing} required field(s) EMPTY. Fill them with ✎ here, or press <b>Open &amp; check</b> and type them on the real form: the bot learns them either way.</p>`:''}</div>
      <div>${a.screenshot?`<img class="shot" loading="lazy" src="${file(a.screenshot)}" onclick="zoom(this.src)" alt="filled form">`:'<p class="m">No screenshot.</p>'}
      ${a.resume?`<p class="ats">Resume: <a href="${file(a.resume)}" target="_blank">${esc(a.resume.split('/').pop())}</a>${rr.after!=null?`<br>ATS (hunter-resume formula): full resume ${rr.before} → one-page tailored <b>${rr.after}</b> · ${esc(rr.how||'')} ${rr.clean===false?'<span style="color:var(--bad)">truth check FAILED</span>':'· truth check clean'}`:''}
        ${rr.missing&&rr.missing.length?`<br><span class=m>JD asks, resume lacks: ${esc(rr.missing.join(', '))}</span>`:''}</p>`:''}
      ${a.cover?`<details><summary>Cover letter used</summary><pre>${esc(a.cover)}</pre></details>`:''}</div></div>`:
      `<p class="m">Not filled yet.</p>`}
    <div class="acts">
      ${['applied','offer','interview','rejected'].includes(j.status)?'':`${a&&a.fields&&a.fields.length?`<button class="b p" onclick="act('approve','${j.id}')" title="Opens the real form in the bot's browser, filled with these values, with the JobHunter panel. You read it and press Submit.">Open &amp; check, then submit</button>
      <button class="b" onclick="if(confirm('Send it with exactly these values, without opening it?'))act('submitnow','${j.id}')" title="Submits with these values if nothing is missing (no panel).">Submit as shown</button>`:''}
      <button class="b" onclick="act('dry','${j.id}')">Fill again (preview)</button>
      <button class="b" onclick="act('mark','${j.id}','applied')">I submitted it myself</button>
      <button class="b" onclick="act('mark','${j.id}','skipped')">Skip</button>`}
      ${j.dream?'':`<button class="b" onclick="dream(${esc(JSON.stringify(j.company))})" title="+15 score, alerts sooner, its careers site watched">★ Dream company</button>`}
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
const DONE=['applied','interview','offer','rejected'];
async function openBrowser(){ const u=prompt('Open which page first? (blank = LinkedIn jobs)','')||''; const j=await post('/api/browser',{url:u}); toast(j.msg); poll(); }
async function applyN(){ const n=prompt('Apply to how many of your best matches? You check each form before it is sent.','5'); if(!n) return;
  const j=await post('/api/applyn',{n:+n}); toast(j.msg); poll(); }
function goCompany(v){ TAB='companies'; localStorage.setItem('jh_tab',TAB); CQ=v; CO=null; render(); if(v) csInput(v); }
function ago(t){ if(!t) return ''; const d=(Date.now()-new Date(t))/864e5; return d<1?'today':d<2?'yesterday':Math.floor(d)+' days ago'; }
function homeView(){ const st=S.stats, q=Q.toLowerCase();
  const big=(n,l,c)=>`<div class="card" style="text-align:center;padding:12px"><div style="font-size:26px;font-weight:750;color:${c||'inherit'}">${n}</div><div class="m">${l}</div></div>`;
  const H=S.jobs.filter(j=>DONE.includes(j.status)&&(!q||(j.title+' '+j.company).toLowerCase().includes(q)))
    .sort((a,b)=>(b.status_at||'').localeCompare(a.status_at||''));
  let h=`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px">
    ${big(st.applied+st.interviews+st.offers,'applied in total')}${big(st['applied this week'],'this week')}${big(st['applied today'],'today')}
    ${big(st.interviews,'interviews','var(--good)')}${big(st.offers,'offers','var(--good)')}${big(st['to review'],'waiting for you','var(--warn)')}${big(st.questions,'questions for you','var(--warn)')}</div>
  <div class="card"><div class="t" style="margin-bottom:8px">Apply</div>
    <div class="acts" style="margin-top:0">
      <input placeholder="Type a company (Birla, Zoho, Sarvam…) and press Enter" style="flex:1;min-width:240px" onkeydown="if(event.key==='Enter')goCompany(this.value)">
      <button class="b" onclick="goCompany('')">🔍 Search a company</button>
      <button class="b p" onclick="applyN()">▶ Apply to my best matches</button>
      <button class="b" onclick="openBrowser()">🎯 Open bot browser</button>
      <button class="b" onclick="act('scan','x')">⟳ Scan for new jobs</button></div>
    <p class="m" style="margin:8px 0 0">The bot fills every form; a JobHunter panel on the page shows what it typed. You read it, fix anything, press Submit. Your fixes are remembered.
    In the bot browser, go to any application page and press <b>🎯 Fill this form</b>.</p></div>
  <div class="card"><div class="ch"><div class="t">Your applications (${H.length})</div><span class="m">search box above filters this list</span></div>`;
  if(!H.length) return h+'<div class="empty">No applications yet. Press ▶ Apply to my best matches.</div></div>';
  h+=`<div style="overflow-x:auto"><table style="min-width:820px"><tr><th>When</th><th>Company</th><th>Role</th><th>Via</th><th>Status</th><th>You fixed</th><th>Files</th><th></th></tr>
  ${H.map(j=>{ const a=(j.apps||[]).find(x=>!x.dry)||(j.apps||[])[0]||{}; const fs=a.fields||[]; const fixed=fs.filter(f=>f.basis==='you').length;
    return `<tr><td style="white-space:nowrap">${esc(ago(j.status_at))}<div class="m">${esc((j.status_at||'').slice(0,10))}</div></td>
    <td>${j.dream?'★ ':''}<b>${esc(j.company)}</b></td><td>${esc(j.title)}<div class="m">${esc(j.location||'')}</div></td>
    <td>${esc(a.site||(j.sources||[]).join(', '))}</td>
    <td><select onchange="act('mark','${j.id}',this.value)">${DONE.map(x=>`<option ${x===j.status?'selected':''}>${x}</option>`).join('')}</select></td>
    <td>${fixed?fixed+' field(s)':'—'}</td>
    <td style="white-space:nowrap"><a href="${esc(j.url)}" target="_blank" rel="noopener">job ↗</a>${a.resume?` · <a href="${file(a.resume)}" target="_blank">resume</a>`:''}${a.screenshot?` · <a href="#" onclick="zoom('${file(a.screenshot)}');return false">form</a>`:''}</td>
    <td><details><summary>what was sent</summary>${fieldsTable(fs,j.id)}${a.cover?`<pre>${esc(a.cover)}</pre>`:''}</details></td></tr>`}).join('')}</table></div></div>`;
  return h; }
function render(){ const st=S.stats; $('#stats').innerHTML=Object.entries(st).map(([k,v])=>`<span class="stat"><b>${v}</b> ${esc(k)}</span>`).join('');
  $('#tabs').innerHTML=TABS.map(([k,l])=>`<button class="tab ${k===TAB?'on':''}" onclick="TAB='${k}';localStorage.setItem('jh_tab',TAB);render()">${l}</button>`).join('');
  const q=Q.toLowerCase(), m=j=>!q||(j.title+' '+j.company+' '+j.location).toLowerCase().includes(q);
  let html='<div style="height:12px"></div>';
  const J=S.jobs.filter(m);
  if(TAB==='home'){ html+=homeView(); $('#main').innerHTML=html; return; }
  if(TAB==='questions'){ html+=questionsView(); $('#main').innerHTML=html; return; }
  if(TAB==='companies'){ html+=companiesView(); $('#main').innerHTML=html; drawSug(); const e=$('#cq'); if(e&&!CO&&!CBUSY) e.focus(); return; }
  if(TAB==='review'){ const L=J.filter(j=>j.kind!=='govt'&&(j.status==='needs_you'||(j.apps&&j.apps.length&&j.apps[0].dry&&!['applied','skipped','rejected'].includes(j.status))));
    html+=(L.length?'<p class="m">Each card shows every field the bot filled and where it came from. <b>Open &amp; check</b> shows the real form with a JobHunter panel: read it, fix anything, press Submit. Any change you make is remembered.</p>'+L.map(appCard).join(''):'<div class="empty">Nothing to review. Forms the bot fills but doesn\'t submit land here.</div>'); }
  else if(TAB==='applied'){ const L=J.filter(j=>['applied','interview','offer','rejected'].includes(j.status)).sort((a,b)=>(b.status_at||'').localeCompare(a.status_at||''));
    html+=L.length?L.map(appCard).join(''):'<div class="empty">No applications yet.</div>'; }
  else if(TAB==='active'){ html+=J.filter(j=>j.kind!=='govt'&&['new','alerted','queued'].includes(j.status)).slice(0,300).map(jobRow).join('')||'<div class="empty">No open jobs. Run a scan.</div>'; }
  else if(TAB==='govt'){ html+=J.filter(j=>j.kind==='govt').sort((a,b)=>(a.deadline||'9').localeCompare(b.deadline||'9')).map(jobRow).join('')||'<div class="empty">No govt notices yet.</div>'; }
  else html+=J.slice(0,500).map(jobRow).join('');
  $('#main').innerHTML=html; if(location.hash){const e=document.getElementById('j'+location.hash.slice(1)); if(e){e.scrollIntoView(); location.hash='';}} }
load(); loadQ(); poll(); setInterval(()=>{ if(!['companies','questions'].includes(TAB)&&!document.querySelector('main details[open], main input:focus, main select:focus')) load(); else loadStatsOnly(); },30000);
async function loadStatsOnly(){ S=await (await fetch('/api/state')).json(); $('#stats').innerHTML=Object.entries(S.stats).map(([k,v])=>`<span class="stat"><b>${v}</b> ${esc(k)}</span>`).join(''); }
</script></body></html>"""
