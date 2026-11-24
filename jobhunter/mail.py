"""Email replies to your applications: read from Gmail, sorted, matched, recorded. Read-only.

`hunt mail` (also every 30 min with the sync timer, when set up):
  1. reads new messages in your Gmail inbox over IMAP (BODY.PEEK: they stay unread);
  2. keeps the ones about job applications (from ATS / job-board senders, or with application
     words, or from a company you applied to);
  3. sorts each one: offer · interview · assessment · rejected · viewed · received · other;
  4. matches it to the application (company, then role) and moves that job's status forward
     (applied → interview → offer; rejected), never backwards, never on a weak match;
  5. puts a Gmail label on it (JobHunter/Interview, JobHunter/Rejected, …) so your inbox is sorted;
  6. Telegram: interview, assessment and offer replies at once; /mail lists all of them;
  7. refreshes the Excel tracker.

Never sends, deletes, archives or marks anything read. Credentials stay in .env:
  GMAIL_USER=you@gmail.com   GMAIL_APP_PASSWORD=16-char app password (Google Account → Security →
  2-Step Verification → App passwords). SMTP_USER / SMTP_PASS are used if these aren't set.
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import re
from dataclasses import dataclass, field

from . import notify
from .config import env
from .db import DB
from .util import iso, norm

HOST = "imap.gmail.com"
LABEL_ROOT = "JobHunter"

# Applicant-tracking platforms: their mail is about applications (or tests) you started.
ATS_SENDERS = re.compile(
    r"greenhouse|lever\.co|ashbyhq|myworkday|workday|smartrecruiters|icims|successfactors|darwinbox|keka|"
    r"zohorecruit|recruitee|workable|jobvite|taleo|oraclecloud|phenom|eightfold|freshteam|peoplestrong|"
    r"hackerrank|hackerearth|codility|mettl|imocha|testgorilla", re.I)
# Job boards also send ads, alerts and social notifications: only their application mails count.
BOARD_SENDERS = re.compile(r"linkedin\.com|naukri|foundit|indeed|internshala|unstop|wellfound|cutshort|instahyre|"
                           r"hirist|glassdoor|apna|shine\.com|superset", re.I)
BOARD_APP_SUBJECT = re.compile(r"\byour application\b|application (?:sent|viewed|submitted|"
                               r"received|update|status)|you applied|applied (?:to|for)|recruiter (?:viewed|replied|message)|"
                               r"(?:interview|assessment|shortlisted)", re.I)
APP_WORDS = re.compile(
    r"\b(?:your application|applying|applied|application (?:for|to|received|status|update)|candidature|"
    r"interview|assessment|shortlist|next steps?|offer letter|job id|requisition)\b", re.I)
NOT_REPLIES = re.compile(
    r"job alert|jobs? (?:for you|you may like|recommended)|new jobs? matching|\d+\+? new jobs|(?:is|are) hiring|"
    r"people are hiring|weekly digest|newsletter|webinar|reacted to|new invitation|invitations? to connect|invited you to (?:connect|join)|you may know|new connection|"
    r"new opportunit|profile is a good fit|good fit for|stop applying|tips|course|certif|% off|\bsale\b|discount|"
    r"premium|build, deploy|deploy|free trial|upgrade|top (?:picks|jobs)|trending|viewed your profile|"
    r"appeared in \d+ searches|who'?s viewed|post|comment|mentioned you|endorse|congratulate|birthday|work anniversary|"
    r"complete your application|finish your application|receive these benefits|incomplete application|"
    r"don'?t miss|last chance|reminder to apply|apply now",
    re.I)
# order matters: the first kind that matches wins
KINDS: list[tuple[str, re.Pattern]] = [
    ("offer", re.compile(r"offer letter|pleased to (?:extend|offer)|(?:job|internship) offer|we(?:'re| are) delighted to offer|"
                         r"letter of (?:offer|intent)", re.I)),
    ("rejected", re.compile(r"\bregret\b|unfortunately|not (?:been )?accepted|has been declined|decided not to|"
                            r"not to (?:proceed|move forward)|not (?:be )?(?:moving|proceeding) forward|decided to (?:move|proceed) "
                            r"(?:forward )?with other|other candidates|not been (?:selected|shortlisted)|not selected|"
                            r"(?:position|role) has been filled|will not be (?:moving|progressing)|not a (?:fit|match)|"
                            r"unable to (?:offer|move forward)|no longer (?:under consideration|considering)", re.I)),
    ("interview", re.compile(r"\binterview\b|schedule (?:a |your )?(?:call|time|meeting|chat)|next round|technical round|"
                             r"\bhr round\b|calendly|shortlisted|move(?:d)? (?:you )?(?:forward|ahead) to|"
                             r"invite you to (?:a|an|the)", re.I)),
    ("assessment", re.compile(r"assessment|online test|coding (?:test|challenge|round)|hackerrank|hackerearth|codility|"
                              r"mettl|imocha|testgorilla|take[- ]home|assignment", re.I)),
    ("viewed", re.compile(r"application was viewed|viewed your application|your application has been viewed", re.I)),
    ("received", re.compile(r"thank(?:s| you) for (?:applying|your application|your interest)|received your application|"
                            r"application (?:has been )?(?:received|submitted)|we(?:'ve| have) received|"
                            r"application confirmation|successfully applied|your application was sent", re.I)),
]
LABEL = {"offer": "Offer", "interview": "Interview", "assessment": "Assessment", "rejected": "Rejected",
         "viewed": "Viewed", "received": "Received", "other": "Other"}
URGENT = {"offer", "interview", "assessment"}
STATUS_FOR = {"offer": "offer", "interview": "interview", "assessment": "interview", "rejected": "rejected"}
FORWARD = ["applied", "interview", "offer"]          # a status only moves forward; rejected is terminal


@dataclass
class Msg:
    uid: str
    msgid: str
    at: str
    sender: str
    sender_name: str
    subject: str
    body: str
    kind: str = "other"
    job_id: str | None = None
    match: str = ""
    why: list[str] = field(default_factory=list)


# "SrinivaS, your application was sent to Codemonk" · "Your application to ML Engineer at Zoho"
SENT_TO = [re.compile(r"your application was sent to (?P<co>.+?)\s*$", re.I),
           re.compile(r"your application (?:to|for) (?P<role>.+?) (?:at|with) (?P<co>.+?)\s*$", re.I),
           re.compile(r"(?:successfully )?applied (?:to|for) (?P<role>.+?) (?:at|with) (?P<co>.+?)\s*$", re.I)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail (
  msgid TEXT PRIMARY KEY, uid TEXT, at TEXT, sender TEXT, sender_name TEXT, subject TEXT, snippet TEXT,
  kind TEXT, job_id TEXT, match TEXT, seen_at TEXT
);
CREATE INDEX IF NOT EXISTS mail_at ON mail(at);
CREATE TABLE IF NOT EXISTS mail_state (k TEXT PRIMARY KEY, v TEXT);
"""


def ensure(db: DB) -> None:
    db.c.executescript(SCHEMA)
    db.c.commit()


def creds() -> tuple[str | None, str | None]:
    return (env("GMAIL_USER") or env("SMTP_USER"),
            (env("GMAIL_APP_PASSWORD") or env("SMTP_PASS") or "").replace(" ", "") or None)


def _dec(v) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(v or "")))
    except Exception:
        return str(v or "")


def _text(m: email.message.Message) -> str:
    """Plain text of the message (HTML stripped when there's no plain part)."""
    plain, htm = [], []
    for part in m.walk() if m.is_multipart() else [m]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            txt = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (plain if part.get_content_type() == "text/plain" else htm if part.get_content_type() == "text/html" else []).append(txt)
    if plain:
        return "\n".join(plain)
    from bs4 import BeautifulSoup
    return "\n".join(BeautifulSoup(h, "html.parser").get_text(" ") for h in htm)


def parse(uid: str, raw: bytes) -> Msg:
    m = email.message_from_bytes(raw)
    name, addr = email.utils.parseaddr(_dec(m.get("From")))
    try:
        at = iso(email.utils.parsedate_to_datetime(m.get("Date")))
    except Exception:
        at = iso()
    body = re.sub(r"[ \t\r\f\v]+", " ", _text(m))
    return Msg(uid=uid, msgid=(m.get("Message-ID") or f"uid-{uid}").strip(), at=at, sender=addr.lower(),
               sender_name=name, subject=_dec(m.get("Subject")), body=body[:20000])


# "If you are shortlisted, we will contact you for an interview" is a receipt, not an invite: drop such sentences.
CONDITIONAL = re.compile(r"[^.\n]*\b(?:if|should|once|in case)\b[^.\n]*\b(?:shortlisted|selected|match(?:es)?|"
                         r"suitable|qualif(?:y|ied)|move forward|next steps?)\b[^.\n]*[.\n]", re.I)


def classify(msg: Msg) -> str:
    """offer · rejected · interview · assessment · viewed · received · other. Subject first, then body."""
    head = msg.subject
    text = CONDITIONAL.sub(" ", msg.subject + "\n" + msg.body[:6000])
    for kind, rx in KINDS:
        if rx.search(head):
            return kind
    for kind, rx in KINDS:
        if rx.search(text):
            return kind
    return "other"


def is_job_mail(msg: Msg, companies: list[str]) -> bool:
    """Only mail about YOUR applications: never ads, alerts, newsletters or social notifications."""
    blob = f"{msg.sender} {msg.sender_name}"
    if NOT_REPLIES.search(msg.subject):
        return False
    if BOARD_SENDERS.search(blob):
        return bool(BOARD_APP_SUBJECT.search(msg.subject))
    if ATS_SENDERS.search(blob):
        return True
    low = norm(f"{blob} {msg.subject} {msg.body[:3000]}")
    if any(c and f" {c} " in f" {low} " for c in companies) and APP_WORDS.search(msg.subject + " " + msg.body[:3000]):
        return True
    return bool(re.search(r"\byour application\b|\bapplication (?:for|to|received|status|update)\b|"
                          r"\binterview (?:invitation|invite|schedul)|\boffer letter\b", msg.subject, re.I))


def _domain_name(addr: str) -> str:
    host = addr.split("@")[-1].lower()
    parts = [p for p in host.split(".") if p not in ("mail", "email", "careers", "jobs", "hr", "talent", "recruiting",
                                                     "notifications", "noreply", "com", "in", "co", "io", "ai", "net", "org")]
    return parts[-1] if parts else ""


def match(db: DB, msg: Msg) -> tuple[str | None, str]:
    """(job id, how) for the application this reply is about, or (None, reason). Strong matches only:
    the company name must appear in the sender, subject or body; the role breaks ties."""
    apps = db.c.execute("""SELECT id, title, company FROM jobs WHERE status IN
                           ('applied','interview','offer','rejected','needs_you','filled')""").fetchall()
    if not apps:
        return None, "you have no applications yet"
    head = norm(f"{msg.sender_name} {msg.subject}")
    body = norm(msg.body[:8000])
    dom = _domain_name(msg.sender)
    cands = []
    for jid, title, company in apps:
        c = norm(company)
        if not c:
            continue
        core = re.sub(r"\b(?:private|pvt|limited|ltd|llp|inc|corp|corporation|company|co|technologies|technology|solutions|services|india|labs?|"
                      r"software|systems|global|group)\b", " ", c).split()
        key = " ".join(core) or c
        where = ("sender" if dom and (dom in key.replace(" ", "") or key.replace(" ", "") in dom) else
                 "subject" if f" {key} " in f" {head} " else "body" if f" {key} " in f" {body} " else "")
        if not where:
            continue
        t = norm(title)
        role = 2 if t and (t in head or t in body) else 1 if any(w in body or w in head for w in t.split() if len(w) > 3) else 0
        cands.append(({"sender": 3, "subject": 2, "body": 1}[where], role, jid, f"{company} ({where}" +
                      (", role" if role == 2 else "") + ")"))
    if not cands:
        return None, "no company you applied to is named in it"
    cands.sort(reverse=True)
    best = cands[0]
    if len(cands) > 1 and cands[1][:2] == best[:2] and cands[1][2] != best[2]:
        # same company, several roles, no role named: attach to the most recent application
        same = [c for c in cands if c[:2] == best[:2]]
        recent = db.c.execute(f"""SELECT job_id FROM events WHERE kind='applied' AND job_id IN
                                  ({','.join('?' * len(same))}) ORDER BY at DESC LIMIT 1""",
                              [c[2] for c in same]).fetchone()
        if recent:
            return recent[0], best[3] + ", most recent of several roles"
    return best[2], best[3]


def applied_outside(db: DB, msg: Msg) -> tuple[str | None, str]:
    """You applied yourself (LinkedIn / Naukri app): record it, so the sheet has every application.
    Uses the job we already track at that company (best title match), or adds it."""
    for rx in SENT_TO:
        m = rx.search(msg.subject)
        if m:
            break
    else:
        return None, ""
    co = m.group("co").strip(" .·-|")
    role = (m.groupdict().get("role") or "").strip(" .·-|")
    if not co or len(co) > 80:
        return None, ""
    key = norm(co)
    best = None
    for jid, title, company, status in db.c.execute("SELECT id, title, company, status FROM jobs"):
        if norm(company) != key and not norm(company).startswith(key + " "):
            continue
        sc = 2 if role and norm(role) == norm(title) else 1 if role and norm(role) in norm(title) else 0
        if best is None or sc > best[0]:
            best = (sc, jid, status)
    if best and (best[0] > 0 or not role):
        jid, status = best[1], best[2]
    else:
        from .db import Job
        j = Job(role or "(role: see the LinkedIn email)", co, "", url="", source="email")
        db.upsert(j)
        jid, status = j.id, "new"
    if status not in ("applied", "interview", "offer", "rejected"):
        db.c.execute("UPDATE jobs SET status='applied', status_at=? WHERE id=?", (msg.at, jid))
        db.c.execute("INSERT INTO events VALUES (?,?,?,?)", (jid, msg.at, "applied", "email: " + msg.subject[:150]))
        db.c.commit()
    return jid, f"{co} (you applied: recorded from the email)"


def _advance(db: DB, jid: str, kind: str, msg: Msg) -> str | None:
    new = STATUS_FOR.get(kind)
    j = db.get(jid)
    if not new or not j:
        return None
    cur = j["status"]
    if cur in ("rejected", "offer"):
        return None                                   # final: an older mail never undoes it
    if new == "rejected" or cur not in FORWARD or FORWARD.index(new) > FORWARD.index(cur):
        db.set_status(jid, new, f"email: {msg.subject[:120]}")
        return new
    return None


def _label(imap, uid: str, kind: str) -> None:
    lab = f"{LABEL_ROOT}/{LABEL[kind]}"
    try:
        imap.create(f'"{lab}"')
    except Exception:
        pass
    try:
        imap.uid("STORE", uid, "+X-GM-LABELS", f'("{lab}")')
    except Exception:
        pass


def sync(days: int = 30, label: bool = True, quiet: bool = False, imap_factory=None) -> dict:
    """Read new mail, sort and record it. Returns counts per kind."""
    user, pw = creds()
    if not (user and pw) and imap_factory is None:
        return {"error": "set GMAIL_USER and GMAIL_APP_PASSWORD in .env (hunt mail --help)"}
    db = DB()
    ensure(db)
    known = {r[0] for r in db.c.execute("SELECT msgid FROM mail")}
    companies = sorted({norm(r[0]) for r in db.c.execute(
        "SELECT company FROM jobs WHERE status IN ('applied','interview','offer','rejected','needs_you','filled')")} - {""})
    imap = imap_factory() if imap_factory else imaplib.IMAP4_SSL(HOST)
    if imap_factory is None:
        try:
            imap.login(user, pw)
        except imaplib.IMAP4.error as exc:
            if "application-specific" in str(exc).lower() or "invalid credentials" in str(exc).lower():
                return {"error": "Gmail refused the password: GMAIL_APP_PASSWORD must be a 16-letter app password "
                                 "(myaccount.google.com/apppasswords), not your normal password"}
            return {"error": f"Gmail login failed: {exc}"}
    imap.select("INBOX", readonly=not label)
    import datetime as _dt
    since = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%d-%b-%Y")
    last = int((db.c.execute("SELECT v FROM mail_state WHERE k='last_uid'").fetchone() or ["0"])[0] or 0)
    typ, data = imap.uid("SEARCH", None, f"(SINCE {since})")
    uids = [int(u) for u in ((data[0] or b"").split() if typ == "OK" else [])]
    uids = [u for u in uids if u > last][-2000:]          # only mail not looked at before (fast after the first run)
    counts: dict[str, int] = {}
    urgent: list[Msg] = []
    # 1) headers only, 200 at a time: sender + subject decide if the body is worth downloading
    cand: list[int] = []
    for i in range(0, len(uids), 200):
        part = ",".join(str(u) for u in uids[i:i + 200])
        typ, d = imap.uid("FETCH", part, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
        if typ != "OK":
            continue
        for item in d or []:
            if not isinstance(item, tuple):
                continue
            mu = re.search(rb"UID (\d+)", item[0])
            if not mu:
                continue
            h = parse(mu.group(1).decode(), item[1])
            if h.msgid not in known and is_job_mail(h, companies):
                cand.append(int(mu.group(1)))
            elif h.msgid not in known and companies and any(
                    c and c.split()[0] in norm(h.sender + " " + h.sender_name + " " + h.subject) for c in companies):
                cand.append(int(mu.group(1)))      # names a company you applied to: read the body to decide
    # 2) full message only for candidates
    for uid_i in cand:
        uid = str(uid_i)
        typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not d or not isinstance(d[0], tuple):
            continue
        msg = parse(uid, d[0][1])
        if msg.msgid in known or not is_job_mail(msg, companies):
            continue
        msg.kind = classify(msg)
        msg.job_id, msg.match = match(db, msg)
        if not msg.job_id:
            msg.job_id, msg.match = applied_outside(db, msg)
            if msg.job_id and msg.kind == "other":
                msg.kind = "received"
        if not msg.job_id:
            sub = classify(Msg(uid, msg.msgid, msg.at, msg.sender, msg.sender_name, msg.subject, ""))
            msg.kind = sub if sub in ("offer", "interview", "assessment", "rejected") else "other"
            if msg.kind == "other":
                known.add(msg.msgid)
                continue                          # not about an application of yours: leave it alone
        moved = _advance(db, msg.job_id, msg.kind, msg) if msg.job_id else None
        db.c.execute("INSERT OR IGNORE INTO mail VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (msg.msgid, uid, msg.at, msg.sender, msg.sender_name, msg.subject,
                      " ".join(msg.body.split())[:400], msg.kind, msg.job_id, msg.match, iso()))
        if msg.job_id:
            db.event(msg.job_id, f"mail_{msg.kind}", msg.subject[:200])
        db.c.commit()
        known.add(msg.msgid)
        counts[msg.kind] = counts.get(msg.kind, 0) + 1
        if label:
            _label(imap, uid, msg.kind)
        if msg.kind in URGENT:
            urgent.append(msg)
        if not quiet:
            print(f"{LABEL[msg.kind]:<10} {msg.at[:16]}  {msg.subject[:70]}  → {msg.match or 'not matched'}"
                  + (f"  [status → {moved}]" if moved else ""))
    if uids:
        db.c.execute("INSERT OR REPLACE INTO mail_state VALUES ('last_uid', ?)", (str(max(uids)),))
        db.c.commit()
    try:
        imap.logout()
    except Exception:
        pass
    for m in urgent:
        notify.telegram(alert_text(db, m))
    if counts:
        from .tracker import refresh
        refresh(db)
    return counts


def alert_text(db: DB, m: Msg) -> str:
    icon = {"offer": "🎉", "interview": "📅", "assessment": "📝"}[m.kind]
    j = db.get(m.job_id) if m.job_id else None
    head = f"{icon} <b>{LABEL[m.kind]}</b>: " + (f"{notify._esc(j['title'])} · {notify._esc(j['company'])}" if j
                                                else notify._esc(m.sender_name or m.sender))
    tail = (f"\n/interview_{j['id'][:8]} for the prep pack" if j and m.kind in ("interview", "assessment") else "")
    return (f"{head}\n<i>{notify._esc(m.subject[:200])}</i>\nfrom {notify._esc(m.sender)} · {m.at[:16].replace('T', ' ')}\n"
            f"{notify._esc(' '.join(m.body.split())[:300])}…" + tail + "\nReply from Gmail: the bot never answers for you.")


def replies(db: DB, jid: str | None = None, limit: int = 500) -> list[dict]:
    ensure(db)
    sql, args = "SELECT * FROM mail", []
    if jid:
        sql += " WHERE job_id=?"
        args.append(jid)
    sql += " ORDER BY at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db.c.execute(sql, args)]


def summary_text(db: DB, days: int = 7) -> str:
    """For Telegram /mail: replies this week, by kind, with the job each belongs to."""
    import datetime as _dt
    ensure(db)
    since = iso(_dt.datetime.now() - _dt.timedelta(days=days))
    rows = [dict(r) for r in db.c.execute("SELECT * FROM mail WHERE at >= ? ORDER BY at DESC", (since,))]
    if not rows:
        return f"No application emails in the last {days} days." + (
            "" if all(creds()) else "\nEmail reading isn't set up: add GMAIL_USER and GMAIL_APP_PASSWORD to .env, then hunt mail.")
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r)
    out = [f"<b>📬 Application emails, last {days} days: {len(rows)}</b>"]
    for kind in ("offer", "interview", "assessment", "rejected", "viewed", "received", "other"):
        rs = by.get(kind) or []
        if not rs:
            continue
        out.append(f"\n<b>{LABEL[kind]} ({len(rs)})</b>")
        for r in rs[:8]:
            j = db.get(r["job_id"]) if r["job_id"] else None
            who = f"{j['company']} · {j['title']}" if j else (r["sender_name"] or r["sender"])
            out.append(f"• {r['at'][5:10]} {notify._esc(who[:70])}: <i>{notify._esc(r['subject'][:80])}</i>")
        if len(rs) > 8:
            out.append(f"  … {len(rs) - 8} more in the Excel tracker")
    return "\n".join(out)
