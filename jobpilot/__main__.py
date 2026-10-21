"""JobPilot CLI.

  python -m jobpilot run              collect -> filter -> score -> cover letters -> auto-apply -> digest
  python -m jobpilot run --no-apply   same, but never opens a browser (good for servers / CI)
  python -m jobpilot collect | filter | score | tailor | apply | digest
  python -m jobpilot review           walk through the queue, forms pre-filled
  python -m jobpilot login            open the browser once to sign in to Naukri
  python -m jobpilot status           counts per status
  python -m jobpilot list [status]    list jobs (default: queued + auto)
  python -m jobpilot show <id>        full details + cover letter for one job
  python -m jobpilot mark <id> <status>
  python -m jobpilot done             numbered list of the review queue; type numbers to mark applied
  python -m jobpilot export           write applied jobs to data/output/jobs.csv (--all: every job)
  python -m jobpilot check-companies  verify Lever/Greenhouse/Ashby slugs
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys

from . import digest, filters, scorer, tailor
from .collectors import ats_feeds, jobspy_source
from .config import load_settings
from .db import DB, now

log = logging.getLogger("jobpilot")


def cmd_collect(s, db, args=None) -> dict:
    jobs = []
    if not getattr(args, "ats_only", False):
        jobs += jobspy_source.collect(s)
    jobs += ats_feeds.collect(s)
    new = sum(db.upsert_job(j) for j in jobs)
    return {"fetched": len(jobs), "new": new}


def cmd_filter(s, db, args=None) -> dict:
    kept = dropped = 0
    for row in db.by_status("new", order="collected_at DESC"):
        ok, reason = filters.check(s, dict(row))
        if ok:
            kept += 1
        else:
            db.set(row["id"], status="filtered", notes=reason)
            dropped += 1
    return {"passed_filters": kept, "filtered_out": dropped}


def cmd_score(s, db, args=None) -> dict:
    return scorer.score_jobs(s, db)


def cmd_tailor(s, db, args=None) -> dict:
    return {"cover_letters": tailor.tailor_pending(s, db)}


def cmd_apply(s, db, args=None) -> dict:
    from .apply import naukri
    return {"naukri": naukri.run(s, db)}


def cmd_digest(s, db, args=None, stats=None) -> dict:
    return digest.send(s, db, stats)


def cmd_run(s, db, args) -> dict:
    started = now()
    stats = {}
    for name, fn in [("collect", cmd_collect), ("filter", cmd_filter), ("score", cmd_score),
                     ("tailor", cmd_tailor)]:
        log.info("--- %s ---", name)
        stats.update(fn(s, db, args))
    if not args.no_apply:
        log.info("--- apply ---")
        try:
            stats.update(cmd_apply(s, db, args))
        except Exception as e:
            log.error("apply step failed: %s", e)
            stats["apply_error"] = str(e)[:120]
    log.info("--- digest ---")
    stats["digest"] = cmd_digest(s, db, args, {k: v for k, v in stats.items() if isinstance(v, int)})
    db.log_run("run", started, stats)
    return stats


def cmd_login(s, db, args):
    from .apply.browser import browser
    with browser(s, headless=False) as ctx:
        page = ctx.new_page()
        page.goto("https://www.naukri.com/nlogin/login")
        input("Sign in to Naukri in the browser window (and any other job sites), then press Enter here...")
    print("Session saved.")


def cmd_review(s, db, args):
    from . import review
    review.run(s, db, limit=args.limit)


def cmd_status(s, db, args):
    c = db.counts()
    for k in ["new", "auto", "queued", "applied", "scored", "filtered", "skipped", "failed"]:
        print(f"{k:>9}: {c.get(k, 0)}")
    print(f"  dry_run: {s.dry_run}")


def cmd_list(s, db, args):
    statuses = args.status or ["auto", "queued"]
    for r in db.by_status(*statuses):
        print(f"{r['id'][:8]}  {str(r['score'] or '-'):>3}  {r['status']:<8} {r['site']:<10} "
              f"{r['title'][:45]:<45} @ {r['company'][:25]}")


def cmd_show(s, db, args):
    r = db.get(args.id)
    if not r:
        sys.exit("not found")
    r = dict(r)
    v = json.loads(r.get("score_json") or "{}")
    print(f"{r['title']} @ {r['company']} ({r['location']})\n{r['url']}\n"
          f"status={r['status']} score={r['score']} site={r['site']}\n")
    print(json.dumps(v, indent=2))
    if r.get("cover_letter"):
        print("\n--- cover letter ---\n" + r["cover_letter"])


def cmd_mark(s, db, args):
    r = db.get(args.id)
    if not r:
        sys.exit("not found")
    fields = {"status": args.status}
    if args.status == "applied":
        fields["applied_at"] = now()
    db.set(r["id"], **fields)
    print(f"{r['title']} -> {args.status}")


def _pick(text: str, n: int) -> list[int]:
    """'1 3 5-7' or '1,3,5-7' -> [1, 3, 5, 6, 7] (only numbers 1..n)."""
    out = []
    for part in text.replace(",", " ").split():
        a, _, b = part.partition("-")
        if a.isdigit() and (not b or b.isdigit()):
            out += range(int(a), int(b or a) + 1)
    return sorted({i for i in out if 1 <= i <= n})


def cmd_done(s, db, args):
    """Mark review-queue jobs as applied (or skipped) by number instead of by id."""
    import webbrowser
    while True:
        jobs = [dict(r) for r in db.by_status("queued", "auto")]
        if not jobs:
            print("Review queue is empty.")
            return
        print()
        for i, r in enumerate(jobs, 1):
            print(f"{i:>3}. [{str(r['score'] or '-'):>3}] {r['site']:<10} {r['title'][:45]:<45} @ {r['company'][:22]}")
        print("\nType numbers you applied to (e.g. 1 3 5-7). 's 2 4' = skip, 'o 3' = open in browser, Enter = done.")
        text = input("> ").strip().lower()
        if not text:
            return
        action = "applied"
        if text[0] in "so" and (len(text) == 1 or not text[1].isdigit()):
            action = {"s": "skipped", "o": "open"}[text[0]]
            text = text[1:]
        picked = [jobs[i - 1] for i in _pick(text, len(jobs))]
        if not picked:
            print("No valid numbers.")
            continue
        for r in picked:
            if action == "open":
                webbrowser.open(r["apply_url"] or r["url"])
                continue
            fields = {"status": action}
            if action == "applied":
                fields["applied_at"] = now()
            db.set(r["id"], **fields)
            print(f"  {action:<8} {r['title'][:50]} @ {r['company'][:25]}")


def cmd_export(s, db, args):
    out = s.path("output_dir") / "jobs.csv"
    cols = ["applied_at", "score", "site", "title", "company", "location", "url", "status", "notes", "id"]
    if args.all:
        where, order = "", "score DESC"
    else:
        where, order = "WHERE status='applied'", "applied_at DESC"
    rows = db.conn.execute(f"SELECT {','.join(cols)} FROM jobs {where} ORDER BY {order}").fetchall()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows([tuple(r) for r in rows])
    print(f"Wrote {len(rows)} {'jobs' if args.all else 'applied jobs'} to {out}")


def cmd_check(s, db, args):
    res = ats_feeds.check_companies(s)
    if not res:
        print("companies.yaml is empty. Add Lever/Greenhouse/Ashby slugs first.")
    for kind, slug, msg in res:
        print(f"{kind:<11} {slug:<28} {msg}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="jobpilot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-apply", action="store_true")
    r.add_argument("--ats-only", action="store_true", help="skip job boards, only company feeds")
    c = sub.add_parser("collect")
    c.add_argument("--ats-only", action="store_true")
    for name in ["filter", "score", "tailor", "apply", "digest", "login", "status",
                 "check-companies", "done"]:
        sub.add_parser(name)
    ex = sub.add_parser("export")
    ex.add_argument("--all", action="store_true", help="every job, not just applied ones")
    rv = sub.add_parser("review")
    rv.add_argument("--limit", type=int, default=20)
    ls = sub.add_parser("list")
    ls.add_argument("status", nargs="*")
    sh = sub.add_parser("show")
    sh.add_argument("id")
    mk = sub.add_parser("mark")
    mk.add_argument("id")
    mk.add_argument("status", choices=["applied", "skipped", "queued", "auto", "failed"])
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ["httpx", "urllib3", "JobSpy"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    s = load_settings()
    db = DB(s.path("db"))
    handlers = {
        "run": cmd_run, "collect": cmd_collect, "filter": cmd_filter, "score": cmd_score,
        "tailor": cmd_tailor, "apply": cmd_apply, "digest": cmd_digest, "login": cmd_login,
        "review": cmd_review, "status": cmd_status, "list": cmd_list, "show": cmd_show,
        "mark": cmd_mark, "done": cmd_done, "export": cmd_export, "check-companies": cmd_check,
    }
    result = handlers[args.cmd](s, db, args)
    if isinstance(result, dict):
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
