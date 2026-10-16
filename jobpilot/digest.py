"""Daily digest: an HTML report on disk, plus optional Telegram and email."""
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


def build_html(settings: Settings, db) -> str:
    counts = db.counts()
    queued = _rows(db, ["auto", "queued"])
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    applied = [dict(r) for r in db.since(since, "applied_at") if r["status"] == "applied"]

    def card(j):
        v = json.loads(j.get("score_json") or "{}")
        cl = html.escape(j.get("cover_letter") or "")
        gaps = ", ".join(v.get("missing_skills") or [])
        return f"""
<article>
  <div class="top"><span class="score s{(j['score'] or 0)//10}">{j['score']}</span>
  <div><h3><a href="{html.escape(j.get('url') or '#')}" target="_blank">{html.escape(j['title'])}</a></h3>
  <p class="meta">{html.escape(j['company'])} · {html.escape(j.get('location') or '')} · {j.get('site')}
  · <code>{j['id'][:8]}</code>{' · <b>auto-apply</b>' if j['status']=='auto' else ''}</p></div></div>
  <p>{html.escape(v.get('one_line',''))}</p>
  {f'<p class="gaps">Gaps: {html.escape(gaps)}</p>' if gaps else ''}
  {f'<p class="note">{html.escape(j["notes"])}</p>' if j.get('notes') else ''}
  {f'<details><summary>Cover letter</summary><pre>{cl}</pre></details>' if cl else ''}
</article>"""

    applied_li = "".join(
        f"<li>{html.escape(j['title'])} @ {html.escape(j['company'])} ({j.get('site')})</li>" for j in applied
    ) or "<li>None in the last 24h</li>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JobPilot digest</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mut:#6b6b66;--line:#e4e4df;--acc:#2f6f4f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--card:#1f1f1d;--ink:#ecece8;--mut:#9a9a94;--line:#33332f;--acc:#7cc39d}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:var(--mut);margin:0 0 20px}}
.stats{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px}}
.stats div{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}}
.stats b{{display:block;font-size:20px}}
article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px}}
.top{{display:flex;gap:12px;align-items:flex-start}} h3{{margin:0;font-size:16px}}
a{{color:var(--acc)}} .meta,.gaps,.note{{color:var(--mut);margin:2px 0;font-size:13px}}
.score{{min-width:40px;text-align:center;font-weight:700;border-radius:8px;padding:6px 0;background:var(--line)}}
.s8,.s9,.s10{{background:var(--acc);color:var(--card)}}
pre{{white-space:pre-wrap;font:inherit;background:var(--bg);padding:10px;border-radius:8px}}
</style></head><body><main>
<h1>JobPilot digest</h1>
<p class="sub">{datetime.now().strftime('%a %d %b %Y, %H:%M')} · {'DRY RUN mode' if settings.dry_run else 'live mode'}</p>
<div class="stats">
 <div><b>{len(queued)}</b>to review / auto</div>
 <div><b>{counts.get('applied',0)}</b>applied total</div>
 <div><b>{len(applied)}</b>applied 24h</div>
 <div><b>{counts.get('filtered',0)+counts.get('scored',0)}</b>rejected</div>
</div>
<h2>Applied in the last 24h</h2><ul>{applied_li}</ul>
<h2>Top matches</h2>
{''.join(card(j) for j in queued[:40]) or '<p>No new matches.</p>'}
</main></body></html>"""


def write_html(settings: Settings, db):
    out = settings.path("output_dir") / f"digest-{datetime.now():%Y%m%d}.html"
    out.write_text(build_html(settings, db), encoding="utf-8")
    (settings.path("output_dir") / "digest-latest.html").write_text(out.read_text(encoding="utf-8"),
                                                                     encoding="utf-8")
    return out


def text_summary(settings: Settings, db, run_stats: dict | None = None) -> str:
    queued = _rows(db, ["auto", "queued"])
    lines = [f"JobPilot {'(dry run) ' if settings.dry_run else ''}- {datetime.now():%d %b %H:%M}"]
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
