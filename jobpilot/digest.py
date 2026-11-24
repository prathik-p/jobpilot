"""Daily digest: an HTML report on disk, plus optional Telegram and email.

The same page is served by `python -m jobpilot board` (see board.py), where the ✓ / ✗ buttons
save straight to the database. Opened from disk, the page is a read-only snapshot.
"""
from __future__ import annotations

import html
import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests

from .config import Settings, env

log = logging.getLogger(__name__)


def _rows(db, statuses):
    return [dict(r) for r in db.by_status(*statuses)]


def stats(db) -> dict:
    c = db.counts()
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    applied_24h = db.conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='applied' AND applied_at >= ?", (since,)).fetchone()[0]
    return {"queued": c.get("queued", 0), "applied": c.get("applied", 0), "applied_24h": applied_24h,
            "skipped": c.get("skipped", 0), "rejected": c.get("filtered", 0) + c.get("scored", 0)}


_TICK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
_CROSS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>'


def _card(j: dict, n: int | None, interactive: bool) -> str:
    e = html.escape
    v = json.loads(j.get("score_json") or "{}")
    score = j.get("score")
    status = j["status"]
    meta = [j.get("company") or "", j.get("location") or "", j.get("site") or "", v.get("category") or ""]
    meta_html = " · ".join(e(m) for m in meta if m) + f" · <code>{e(j['id'][:8])}</code>"
    when = {"applied": j.get("applied_at"), "skipped": j.get("updated_at")}.get(status) or ""
    gaps = ", ".join(v.get("missing_skills") or [])
    flags = ", ".join(v.get("red_flags") or [])
    cl = j.get("cover_letter") or ""
    search = " ".join([j.get("title") or "", *meta, j["id"][:8]]).lower()
    detail = "".join([
        f"<p>{e(v['one_line'])}</p>" if v.get("one_line") else "",
        f'<p class="mut">Gaps: {e(gaps)}</p>' if gaps else "",
        f'<p class="flag">Red flags: {e(flags)}</p>' if flags else "",
        f'<p class="mut">{e(j["notes"])}</p>' if j.get("notes") else "",
        f"<details><summary>Cover letter</summary><pre>{e(cl)}</pre></details>" if cl else "",
    ])
    actions = ""
    if interactive:
        actions = f"""<div class="actions">
<button class="act ok" data-to="applied" title="Mark as applied" aria-label="Mark as applied">{_TICK}</button>
<button class="act no" data-to="skipped" title="Discard" aria-label="Discard">{_CROSS}</button>
<button class="back" data-to="queued" title="Move back to the apply queue">Back to queue</button></div>"""
    return (
        f'<article class="job" data-id="{e(j["id"])}" data-status="{e(status)}" data-score="{score if score is not None else -1}"'
        f' data-n="{n or ""}" data-search="{e(search)}">'
        f'<span class="n">{n or ""}</span><span class="score s{(score or 0) // 10}">{score if score is not None else "–"}</span>'
        f'<div class="body"><h3><a href="{e(j.get("url") or "#")}" target="_blank" rel="noopener">{e(j["title"])}</a></h3>'
        f'<p class="meta">{meta_html}<span class="when">{e(when[:10])}</span></p>'
        f'<div class="detail">{detail}</div></div>{actions}</article>'
    )


CSS = """
:root{--bg:#f6f6f3;--card:#fff;--ink:#1d1d1b;--mut:#6b6b66;--line:#e2e2dc;--acc:#2f6f4f;
  --ok:#1f8a4c;--ok-bg:#e5f4ea;--no:#c7372f;--no-bg:#fbe9e7;--warn:#9a5b00}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--card:#1f1f1d;--ink:#ecece8;--mut:#9a9a94;
  --line:#34342f;--acc:#7cc39d;--ok:#5fcf8c;--ok-bg:#1d3326;--no:#f07a70;--no-bg:#3a2220;--warn:#e0a650}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:900px;margin:0 auto;padding:24px 16px 96px}
h1{font-size:22px;margin:0}.sub{color:var(--mut);margin:2px 0 18px}
a{color:var(--acc)}code{font-size:12px}
.banner{background:var(--card);border:1px dashed var(--line);border-radius:10px;padding:10px 14px;color:var(--mut);margin:0 0 18px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.stats div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;color:var(--mut);font-size:13px}
.stats b{display:block;font-size:20px;color:var(--ink);font-variant-numeric:tabular-nums}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;margin-bottom:6px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--line)}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:3px}
.tabs button{border:0;background:none;color:var(--mut);font:inherit;font-size:14px;padding:6px 12px;border-radius:7px;cursor:pointer}
.tabs button[aria-selected=true]{background:var(--ink);color:var(--bg)}
.tabs b{font-weight:600;font-variant-numeric:tabular-nums}
#q{flex:1;min-width:160px;font:inherit;font-size:14px;padding:7px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
h2{font-size:15px;margin:22px 0 10px;color:var(--mut);font-weight:600}
.js h2{display:none}
.list:empty::before{content:attr(data-empty);display:block;color:var(--mut);padding:24px 0;text-align:center}
.job{display:grid;grid-template-columns:30px 42px 1fr auto;gap:12px;align-items:start;background:var(--card);
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 10px;transition:opacity .15s,transform .15s}
.job.out{opacity:0;transform:scale(.98)}
.job[hidden]{display:none}
.n{color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums;text-align:right;padding-top:7px}
.score{text-align:center;font-weight:700;border-radius:8px;padding:6px 0;background:var(--line);font-variant-numeric:tabular-nums}
.s8,.s9,.s10{background:var(--acc);color:var(--card)}
.body{min-width:0}
h3{margin:0;font-size:16px;line-height:1.35}
.meta{color:var(--mut);margin:2px 0;font-size:13px}
.when:not(:empty)::before{content:" · "}
.detail p{margin:6px 0 0}.mut{color:var(--mut);font-size:13px}.flag{color:var(--warn);font-size:13px}
details{margin-top:6px}summary{cursor:pointer;color:var(--acc);font-size:14px}
pre{white-space:pre-wrap;font:inherit;font-size:14px;background:var(--bg);padding:10px;border-radius:8px;margin:6px 0 0}
.actions{display:flex;gap:8px}
.act{width:42px;height:42px;border-radius:50%;border:1.5px solid currentColor;display:grid;place-items:center;cursor:pointer;padding:0}
.act svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}
.act.ok{color:var(--ok);background:var(--ok-bg)}.act.no{color:var(--no);background:var(--no-bg)}
.act.ok:hover,.act.ok:focus-visible{background:var(--ok);color:var(--card)}
.act.no:hover,.act.no:focus-visible{background:var(--no);color:var(--card)}
.back{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:8px;padding:6px 10px;cursor:pointer;white-space:nowrap}
.back:hover{border-color:var(--mut)}
button:disabled{opacity:.4;cursor:wait}
.job[data-status=queued] .back,.job:not([data-status=queued]) .act{display:none}
.job:not([data-status=queued]) .detail{display:none}
.job:not([data-status=queued]){padding:10px 14px}
.job[data-status=applied]{border-left:4px solid var(--ok)}
.job[data-status=skipped]{border-left:4px solid var(--no)}
.job[data-status=skipped] h3 a{color:var(--mut)}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--ink);color:var(--bg);
  padding:10px 14px;border-radius:10px;display:flex;gap:14px;align-items:center;max-width:calc(100% - 32px);
  box-shadow:0 6px 24px rgba(0,0,0,.25);font-size:14px}
#toast[hidden]{display:none}
#toast span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#toast button{font:inherit;font-weight:700;background:none;border:0;color:inherit;text-decoration:underline;cursor:pointer;padding:0}
@media (max-width:560px){
  .job{grid-template-columns:22px 40px 1fr;gap:8px}
  .actions{grid-column:1/-1;justify-content:flex-end}
}
"""

JS = r"""
const TOKEN = __TOKEN__;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const lists = {queued: $('#list-queued'), applied: $('#list-applied'), skipped: $('#list-skipped')};
const LABEL = {applied: 'Marked applied', skipped: 'Discarded', queued: 'Moved back to queue'};
document.body.classList.add('js');

function showTab(tab) {
  if (!lists[tab]) tab = 'queued';
  $$('.tabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === tab));
  for (const k in lists) lists[k].closest('section').hidden = k !== tab;
  try { localStorage.setItem('jobpilot-tab', tab); } catch (e) {}
}
$$('.tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
let saved = 'queued';
try { saved = localStorage.getItem('jobpilot-tab') || 'queued'; } catch (e) {}
showTab(saved);

const q = $('#q');
function filter() {
  const t = q.value.trim().toLowerCase();
  $$('.job').forEach(c => c.hidden = !!t && !c.dataset.search.includes(t));
}
q.addEventListener('input', filter);

function setCounts(c) {
  $$('[data-count]').forEach(el => { if (el.dataset.count in c) el.textContent = c[el.dataset.count]; });
}

function place(card, status) {
  card.dataset.status = status;
  card.querySelector('.n').textContent = status === 'queued' ? card.dataset.n : '';
  card.querySelector('.when').textContent = status === 'queued' ? '' : 'today';
  const list = lists[status];
  if (status !== 'queued') return list.prepend(card);
  // back in the queue: same order as the server (score high to low, then id)
  const s = +card.dataset.score, id = card.dataset.id;
  const next = [...list.children].find(o => +o.dataset.score < s || (+o.dataset.score === s && o.dataset.id > id));
  list.insertBefore(card, next || null);
}

let toastTimer;
function toast(msg, undo) {
  const t = $('#toast');
  t.querySelector('span').textContent = msg;
  const b = t.querySelector('button');
  b.hidden = !undo;
  b.onclick = () => { t.hidden = true; undo(); };
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.hidden = true, undo ? 7000 : 9000);
}

async function mark(card, status, isUndo) {
  const prev = card.dataset.status;
  const buttons = $$('button', card);
  buttons.forEach(b => b.disabled = true);
  try {
    const r = await fetch('/api/mark', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-JobPilot-Token': TOKEN},
      body: JSON.stringify({id: card.dataset.id, status}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    const data = await r.json();
    card.classList.add('out');
    await new Promise(res => setTimeout(res, 150));
    place(card, status);
    card.classList.remove('out');
    setCounts(data.counts);
    const title = card.querySelector('h3').textContent;
    toast(isUndo ? `Undone: ${title}` : `${LABEL[status]}: ${title}`, isUndo ? null : () => mark(card, prev, true));
  } catch (e) {
    toast(`Not saved (${e.message}). Is "python -m jobpilot board" still running?`);
  } finally {
    buttons.forEach(b => b.disabled = false);
  }
}

if (TOKEN) document.addEventListener('click', ev => {
  const b = ev.target.closest('button[data-to]');
  if (b) mark(b.closest('.job'), b.dataset.to);
});
"""


def build_html(settings: Settings, db, token: str | None = None) -> str:
    """The digest page. With a token (served by `board`), each job gets working ✓ / ✗ buttons."""
    interactive = token is not None
    st = stats(db)
    queued = _rows(db, ["queued"])
    applied = [dict(r) for r in db.by_status("applied", order="applied_at DESC, id")]
    skipped = [dict(r) for r in db.by_status("skipped", order="updated_at DESC, id")]

    def section(key, title, jobs, numbered, empty):
        cards = "".join(_card(j, i if numbered else None, interactive) for i, j in enumerate(jobs, 1))
        return (f'<section><h2>{title}</h2>'
                f'<div class="list" id="list-{key}" data-empty="{html.escape(empty)}">{cards}</div></section>')

    banner = "" if interactive else (
        '<p class="banner">Read-only snapshot. To tick jobs off right on this page, run '
        '<code>python -m jobpilot board</code>.</p>')

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JobPilot{' board' if interactive else ' digest'}</title>
<style>{CSS}</style></head><body><main>
<h1>JobPilot</h1>
<p class="sub">{datetime.now().strftime('%a %d %b %Y, %H:%M')} · numbers match <code>jobpilot list</code> and <code>done</code></p>
{banner}
<div class="stats">
 <div><b data-count="queued">{st['queued']}</b>to apply</div>
 <div><b data-count="applied_24h">{st['applied_24h']}</b>applied in 24h</div>
 <div><b data-count="applied">{st['applied']}</b>applied total</div>
 <div><b data-count="skipped">{st['skipped']}</b>discarded</div>
 <div><b data-count="rejected">{st['rejected']}</b>auto-rejected</div>
</div>
<div class="bar">
 <div class="tabs" role="tablist">
  <button data-tab="queued">To apply <b data-count="queued">{st['queued']}</b></button>
  <button data-tab="applied">Applied <b data-count="applied">{st['applied']}</b></button>
  <button data-tab="skipped">Discarded <b data-count="skipped">{st['skipped']}</b></button>
 </div>
 <input id="q" type="search" placeholder="Filter by title, company, city, site…" aria-label="Filter jobs">
</div>
{section("queued", "To apply", queued, True, "Nothing left to apply to. Nice.")}
{section("applied", "Applied", applied, False, "No applications yet.")}
{section("skipped", "Discarded", skipped, False, "Nothing discarded.")}
</main>
<div id="toast" role="status" aria-live="polite" hidden><span></span><button type="button">Undo</button></div>
<script>{JS.replace("__TOKEN__", json.dumps(token))}</script>
</body></html>"""
    return page


def write_html(settings: Settings, db, dated: bool = True):
    """Write digest-latest.html, plus a dated copy unless dated=False (used by `done` and `board`
    to refresh the latest digest without creating/overwriting a day's snapshot)."""
    body = build_html(settings, db)
    latest = settings.path("output_dir") / "digest-latest.html"
    latest.write_text(body, encoding="utf-8")
    if not dated:
        return latest
    out = settings.path("output_dir") / f"digest-{datetime.now():%Y%m%d}.html"
    out.write_text(body, encoding="utf-8")
    return out


def text_summary(settings: Settings, db, run_stats: dict | None = None) -> str:
    queued = _rows(db, ["queued"])
    lines = [f"JobPilot - {datetime.now():%d %b %H:%M}"]
    if run_stats:
        lines.append(" | ".join(f"{k}: {v}" for k, v in run_stats.items() if not isinstance(v, dict)))
    lines.append(f"{len(queued)} matches waiting. Top:")
    for j in queued[:8]:
        lines.append(f"• [{j['score']}] {j['title']} @ {j['company']} ({j.get('site')})\n  {j.get('url')}")
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True},
                      timeout=20)
    return r.ok


def send_email(subject: str, html_body: str) -> bool:
    host, user, pw, to = env("SMTP_HOST"), env("SMTP_USER"), env("SMTP_PASSWORD"), env("DIGEST_TO")
    if not (host and user and pw and to):
        return False
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP(host, int(env("SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)
    return True


def send(settings: Settings, db, run_stats: dict | None = None) -> dict:
    path = write_html(settings, db)
    sent = {"html": str(path)}
    n = settings.config.get("notify", {})
    try:
        if n.get("telegram"):
            sent["telegram"] = send_telegram(text_summary(settings, db, run_stats))
        if n.get("email"):
            sent["email"] = send_email("JobPilot digest", path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("notification failed: %s", e)
    return sent
