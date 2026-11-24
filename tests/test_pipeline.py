"""Offline tests: no network, no API key, no browser. Run: python -m unittest discover tests"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SAMPLE_JOBS = [
    dict(source="jobspy", site="naukri", title="Full Stack Developer (React/Node)", company="Acme Tech",
         location="Bengaluru, Karnataka", is_remote=0, url="https://naukri.com/job/1",
         description="1-3 years experience with React, Next.js, Node.js, TypeScript and PostgreSQL. NestJS a plus."),
    dict(source="jobspy", site="linkedin", title="Software Engineer - Full Stack", company="Beta AI",
         location="Hyderabad, Telangana", is_remote=0, url="https://linkedin.com/jobs/2",
         description="Build AI products with Next.js, Python, LLM integrations, WebSockets. 2+ years."),
    dict(source="jobspy", site="indeed", title="Senior Full Stack Engineer", company="Gamma",
         location="Bengaluru", is_remote=0, url="https://indeed.com/3", description="React, Node"),
    dict(source="jobspy", site="naukri", title="Full Stack Developer", company="Delta",
         location="Pune, Maharashtra", is_remote=0, url="https://naukri.com/4", description="React, Node, 1-2 years"),
    dict(source="jobspy", site="naukri", title="Full Stack Developer", company="Epsilon",
         location="Kochi, Kerala", is_remote=0, url="https://naukri.com/5",
         description="Minimum 5 years of experience in React and Node"),
    dict(source="greenhouse", site="greenhouse", title="Software Engineer, Frontend", company="Zeta",
         location="Remote - India", is_remote=1, url="https://boards.greenhouse.io/zeta/jobs/6",
         description="React, TypeScript, Next.js, Stripe payments. 0-2 years."),
    dict(source="jobspy", site="naukri", title="Java Developer", company="Eta", location="Hyderabad",
         is_remote=0, url="https://naukri.com/7", description="Spring Boot"),
    dict(source="jobspy", site="naukri", title="Full Stack Developer - Latest Tech", company="Theta",
         location="Bangalore", is_remote=0, url="https://naukri.com/8",
         description="React, Next.js, Node.js, TypeScript, PostgreSQL, NestJS, Prisma, 1-3 years"),
]


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "config.example.yaml", self.tmp / "config.yaml")
        shutil.copy(ROOT / "profile.example.yaml", self.tmp / "profile.yaml")
        shutil.copy(ROOT / "companies.yaml", self.tmp / "companies.yaml")
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        from jobpilot.config import load_settings
        from jobpilot.db import DB
        self.s = load_settings(self.tmp)
        self.db = DB(self.s.path("db"))

    def tearDown(self):
        self.db.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_llm_scoring_path(self):
        from unittest import mock
        from jobpilot import llm, scorer
        reply = '```json\n{"score": 88, "seniority_ok": true, "required_years": 1, "matched_skills": ["react"],' \
                ' "missing_skills": ["graphql"], "red_flags": [], "one_line": "Strong match",' \
                ' "pitch_points": ["Cut voice latency 75%"]}\n```'
        self.db.upsert_job(dict(SAMPLE_JOBS[0]))
        with mock.patch.object(llm, "available", return_value=True), \
             mock.patch.object(llm, "complete", return_value=reply):
            stats = scorer.score_jobs(self.s, self.db)
        row = self.db.by_status("queued")[0]
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(row["score"], 88)
        self.assertEqual(json.loads(row["score_json"])["missing_skills"], ["graphql"])

    def test_red_flags_and_kochi_weight(self):
        from jobpilot import scorer
        kochi = dict(SAMPLE_JOBS[0], location="Kochi, Kerala")
        v = {"score": 90, "seniority_ok": True, "red_flags": []}
        self.assertEqual(scorer.final_score(self.s, kochi, v), 76)   # 90 * 0.85
        v["red_flags"] = ["bond/agreement"]
        self.assertEqual(scorer.final_score(self.s, SAMPLE_JOBS[0], v), 80)
        # experience / stack / location complaints are not red flags and must not cost points
        v["red_flags"] = ["requires 3+ years; candidate has about 1", "wrong stack", "in-person only"]
        self.assertEqual(scorer.final_score(self.s, SAMPLE_JOBS[0], v), 90)
        v["red_flags"] = ["bond/agreement", "night shift", "unpaid"]  # capped at two
        self.assertEqual(scorer.final_score(self.s, SAMPLE_JOBS[0], v), 70)

    def test_seniority_penalty_only_above_max_years(self):
        from jobpilot import scorer
        job = SAMPLE_JOBS[0]
        v = {"score": 70, "seniority_ok": False, "required_years": 2, "red_flags": []}
        self.assertEqual(scorer.final_score(self.s, job, v), 70)     # 2 yrs <= max 3: no extra penalty
        v["required_years"] = 5
        self.assertEqual(scorer.final_score(self.s, job, v), 45)     # above max: -25

    def test_experience_parser(self):
        from jobpilot.filters import required_experience
        self.assertEqual(required_experience("3-5 years in React"), 3)
        self.assertEqual(required_experience("4+ yrs"), 4)
        self.assertEqual(required_experience("minimum of 6 years"), 6)
        self.assertEqual(required_experience("0 to 2 years"), 0)
        self.assertIsNone(required_experience("great team"))

    def test_dedupe(self):
        self.assertTrue(self.db.upsert_job(dict(SAMPLE_JOBS[0])))
        dup = dict(SAMPLE_JOBS[0], site="indeed", url="https://other")
        self.assertFalse(self.db.upsert_job(dup))

    def test_full_pipeline(self):
        from jobpilot.__main__ import cmd_filter, cmd_score, cmd_tailor
        from jobpilot import digest
        for j in SAMPLE_JOBS:
            self.db.upsert_job(dict(j))
        f = cmd_filter(self.s, self.db)
        self.assertEqual(f["filtered_out"], 3)   # senior, Pune, 5 yrs
        rows = {r["company"]: dict(r) for r in self.db.conn.execute("SELECT * FROM jobs")}
        self.assertEqual(rows["Gamma"]["notes"], "title contains 'senior'")
        self.assertIn("not targeted", rows["Delta"]["notes"])
        self.assertIn("5+", rows["Epsilon"]["notes"])
        self.assertEqual(rows["Theta"]["status"], "new")
        self.assertEqual(rows["Eta"]["status"], "new")    # Java roles are in scope now

        stats = cmd_score(self.s, self.db)
        self.assertEqual(stats["errors"], 0)
        rows = {r["company"]: dict(r) for r in self.db.conn.execute("SELECT * FROM jobs")}
        self.assertEqual(rows["Theta"]["status"], "queued")        # strong match
        self.assertFalse(self.db.by_status("auto"))                # auto-apply is gone

        n = cmd_tailor(self.s, self.db)["cover_letters"]
        self.assertGreaterEqual(n, 1)
        cl = self.db.get(rows["Theta"]["id"])["cover_letter"]
        self.assertIn("Asha Rao", cl)
        self.assertIn("Example Labs", cl)
        self.assertIn("immediately", cl)

        out = digest.write_html(self.s, self.db)
        html = out.read_text()
        self.assertIn("Theta", html)
        self.assertNotIn("Gamma", html)

    def test_title_filter_any_tech_role(self):
        from jobpilot.filters import title_check
        ok = lambda t: title_check(self.s, t)[0]
        for t in ["Java Developer", "Data Engineer", "ML Engineer", "AI Engineer", "SDET",
                  "DevOps Engineer", "Android Developer", "QA Automation Engineer", "ReactJS Developer",
                  "Data Scientist", "Software Engineer - Latest Tech"]:
            self.assertTrue(ok(t), t)
        for t in ["Senior Java Developer", "Tech Lead", "HTML Content Writer", "Air Hostess",
                  "Data Entry Operator", "ASIC Verification Engineer", "Sales Engineer",
                  "Technical Support Engineer", "Software Engineering Intern", "Marketing Executive"]:
            self.assertFalse(ok(t), t)

    def test_requeue(self):
        import argparse
        from jobpilot.__main__ import cmd_requeue
        self.db.upsert_job(dict(SAMPLE_JOBS[0]))
        jid = self.db.by_status("new")[0]["id"]
        self.db.set(jid, status="scored", score=40, score_json="{}")
        self.assertEqual(cmd_requeue(self.s, self.db, argparse.Namespace(days=7))["requeued"], 1)
        row = self.db.get(jid)
        self.assertEqual(row["status"], "new")
        self.assertIsNone(row["score"])

    def test_workday_collector(self):
        from unittest import mock
        from jobpilot.collectors import ats_feeds
        from jobpilot.filters import quick_check
        facets = [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationHierarchy1", "values": [
                {"descriptor": "India", "id": "IN1", "count": 9},
                {"descriptor": "United States", "id": "US1", "count": 99}]}]}]
        listing = {"total": 4, "facets": facets, "jobPostings": [
            {"title": "Software Engineer", "externalPath": "/job/Bengaluru/SE_1",
             "locationsText": "India, Bengaluru", "postedOn": "Posted Today"},
            {"title": "Senior Software Engineer", "externalPath": "/job/Bengaluru/SSE_2",
             "locationsText": "India, Bengaluru", "postedOn": "Posted Today"},
            {"title": "Data Engineer", "externalPath": "/job/Pune/DE_3",
             "locationsText": "India, Pune", "postedOn": "Posted Yesterday"},
            {"title": "Backend Engineer", "externalPath": "/job/x/BE_4",
             "locationsText": "India, Hyderabad", "postedOn": "Posted 30+ Days Ago"}]}
        detail = {"jobPostingInfo": {"jobDescription": "<p>Python, 0-2 years</p>",
                                     "location": "India, Bengaluru", "startDate": "2026-11-19",
                                     "externalUrl": "https://acme.wd5.myworkdayjobs.com/Ext/job/Bengaluru/SE_1"}}
        bodies = []

        def post(url, json, **kw):
            bodies.append(json)
            return mock.Mock(json=lambda: listing, raise_for_status=lambda: None)

        get = mock.Mock(return_value=mock.Mock(json=lambda: detail))
        with mock.patch.object(ats_feeds.requests, "post", side_effect=post), \
             mock.patch.object(ats_feeds.requests, "get", get):
            jobs = ats_feeds.workday("https://acme.wd5.myworkdayjobs.com/en-US/Ext", "Acme", 14,
                                     keep=lambda *a: quick_check(self.s, *a))
        self.assertEqual(bodies[1]["appliedFacets"], {"locationHierarchy1": ["IN1"]})
        self.assertEqual([j["title"] for j in jobs], ["Software Engineer"])  # senior, Pune, old skipped
        self.assertEqual(get.call_count, 1)                                   # details only for that one
        self.assertEqual(jobs[0]["company"], "Acme")
        self.assertIn("Python", jobs[0]["description"])
        self.assertEqual(jobs[0]["url"], "https://acme.wd5.myworkdayjobs.com/Ext/job/Bengaluru/SE_1")
        self.assertEqual(len(bodies), 2)   # last page had an old posting, so paging stopped

    def test_ats_location_filter(self):
        from jobpilot.collectors.ats_feeds import relevant_location
        self.assertTrue(relevant_location("Bengaluru, India", False))
        self.assertTrue(relevant_location("Remote", True))
        self.assertFalse(relevant_location("San Francisco, CA", False))
        self.assertFalse(relevant_location("Remote - US", True))

    def test_pick_numbers(self):
        from jobpilot.__main__ import _pick
        self.assertEqual(_pick("1 3 5-7", 10), [1, 3, 5, 6, 7])
        self.assertEqual(_pick("2,2, 9 12", 10), [2, 9])
        self.assertEqual(_pick("x 0", 5), [])

    def test_board_lists_every_job_and_saves_clicks(self):
        import threading
        import urllib.error
        import urllib.request
        from jobpilot import board, digest
        for i in range(55):   # more than the old 40-card cap, all with the same score
            self.db.upsert_job(dict(SAMPLE_JOBS[0], title=f"Engineer {i}", url=f"https://x/{i}"))
        ids = [r["id"] for r in self.db.conn.execute("SELECT id FROM jobs")]
        for jid in ids:
            self.db.set(jid, status="queued", score=72, score_json="{}")
        queue = [r["id"] for r in self.db.by_status("queued")]
        self.assertEqual(queue, sorted(ids))    # ties broken by id, so numbering is stable

        page = digest.build_html(self.s, self.db)
        self.assertEqual(page.count('class="job"'), 55)
        self.assertIn(f'data-id="{queue[54]}" data-status="queued" data-score="72" data-n="55"', page)
        self.assertNotIn('data-to="applied"', page)          # file on disk: read-only

        srv = board.make_server(self.s, self.db, port=0)
        url = f"http://127.0.0.1:{srv.server_address[1]}"

        def post(body, token):
            req = urllib.request.Request(url + "/api/mark", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-JobPilot-Token": token})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None

        def call(fn):   # client in a thread, server handles one request here (sqlite stays on one thread)
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("r", fn()))
            t.start()
            srv.handle_request()
            t.join()
            return out["r"]

        try:
            served = call(lambda: urllib.request.urlopen(url + "/", timeout=5).read().decode())
            self.assertIn('data-to="applied"', served)
            self.assertIn(srv.token, served)
            self.assertEqual(call(lambda: post({"id": queue[0], "status": "applied"}, "wrong"))[0], 403)
            code, res = call(lambda: post({"id": queue[0], "status": "applied"}, srv.token))
            self.assertEqual((code, res["counts"]["applied"], res["counts"]["queued"]), (200, 1, 54))
            self.assertTrue(self.db.get(queue[0])["applied_at"])
            call(lambda: post({"id": queue[1], "status": "skipped"}, srv.token))
            self.assertEqual(self.db.get(queue[1])["status"], "skipped")
            code, _ = call(lambda: post({"id": queue[0], "status": "queued"}, srv.token))   # undo
            self.assertEqual((code, self.db.get(queue[0])["status"]), (200, "queued"))
            self.assertIsNone(self.db.get(queue[0])["applied_at"])
            self.assertEqual(call(lambda: post({"id": queue[2], "status": "deleted"}, srv.token))[0], 400)
        finally:
            srv.server_close()

    def test_lever_parser(self):
        from unittest import mock
        from jobpilot.collectors import ats_feeds
        fake = [{"text": "Full Stack Engineer", "hostedUrl": "https://jobs.lever.co/x/1",
                 "applyUrl": "https://jobs.lever.co/x/1/apply", "categories": {"location": "Bengaluru"},
                 "descriptionPlain": "React", "lists": [{"text": "Req", "content": "<li>Node</li>"}],
                 "createdAt": 1760000000000, "workplaceType": "hybrid"}]
        resp = mock.Mock(json=lambda: fake, raise_for_status=lambda: None)
        with mock.patch.object(ats_feeds.requests, "get", return_value=resp):
            jobs = ats_feeds.lever("x")
        self.assertEqual(jobs[0]["title"], "Full Stack Engineer")
        self.assertIn("Node", jobs[0]["description"])


if __name__ == "__main__":
    unittest.main()
