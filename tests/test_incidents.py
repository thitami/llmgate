import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from llmgate import github_issues, incidents, status

T0 = 1_800_000_000.0
STEP = 7200.0  # one probe every 2 hours


def series(provider, pattern, start=T0):
    """'..xx.' -> ok, ok, fail, fail, ok (one probe per STEP)."""
    out = []
    for n, ch in enumerate(pattern):
        ok = ch == "."
        out.append({"ts": status._iso(start + n * STEP), "t": start + n * STEP,
                    "provider": provider, "model": "m", "ok": ok,
                    "kind": None if ok else ("rate_limited" if ch == "r" else "server_error"),
                    "status": None, "latency_ms": 100.0, "tokens_in": 1, "tokens_out": 1,
                    "correct": True if ok else None,
                    "error": None if ok else "boom"})
    return out


class FindIncidentTests(unittest.TestCase):
    def test_single_blip_is_not_an_incident(self):
        self.assertEqual(incidents.find_incidents(series("p", "..x..x..")), [])

    def test_opens_after_two_fails_and_closes_after_two_oks(self):
        found = incidents.find_incidents(series("p", "..xxx.x..."))
        self.assertEqual(len(found), 1)
        i = found[0]
        self.assertEqual(i.start, T0 + 2 * STEP)    # first failure of the streak
        self.assertEqual(i.end, T0 + 7 * STEP)      # first ok of the 2-ok recovery
        self.assertEqual(i.failed_probes, 4)        # the lone fail mid-recovery counts
        self.assertEqual(i.kinds, {"server_error": 4})
        self.assertEqual(i.duration(T0 + 100 * STEP), 5 * STEP)

    def test_one_ok_does_not_close(self):
        found = incidents.find_incidents(series("p", "xx.x"))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].ongoing)

    def test_ongoing_and_kinds_mixed(self):
        found = incidents.find_incidents(series("p", ".xrx"))
        self.assertTrue(found[0].ongoing)
        self.assertEqual(found[0].kinds, {"server_error": 2, "rate_limited": 1})

    def test_two_incidents_and_two_providers(self):
        recs = series("a", "xx..xx..") + series("b", "....xxx")
        found = incidents.find_incidents(recs)
        self.assertEqual([(i.provider, i.ongoing) for i in found],
                         [("a", False), ("a", False), ("b", True)])

    def test_order_of_records_does_not_matter(self):
        recs = series("p", "..xx..")
        self.assertEqual(len(incidents.find_incidents(recs[::-1])), 1)


class OutputTests(unittest.TestCase):
    def test_coverage_wording(self):
        self.assertEqual(status.coverage(0.01), "under an hour")
        self.assertEqual(status.coverage(5 / 24), "5 hours")
        self.assertEqual(status.coverage(1 / 24), "1 hour")
        self.assertEqual(status.coverage(1.2), "1 day")
        self.assertEqual(status.coverage(6.5), "6 days")

    def test_badge(self):
        self.assertEqual(incidents.slug("gemini / 3.8-flash"), "gemini-3-8-flash")
        b = incidents.badge("groq / x", 0.988)
        self.assertEqual(b["schemaVersion"], 1)
        self.assertEqual(b["message"], "98.8% uptime 7d")
        self.assertEqual(b["color"], "green")
        self.assertEqual(incidents.badge("x", None)["color"], "lightgrey")
        self.assertEqual(incidents.badge("x", 0.5)["color"], "red")

    def test_feed_is_valid_atom(self):
        found = incidents.find_incidents(series("a & b", "xx..") + series("c", "..xx"))
        xml = incidents.atom_feed(found, T0 + 10 * STEP, "https://e.x/s", "T",
                                  status.KIND_LABELS)
        root = ET.fromstring(xml)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        titles = [e.find("a:title", ns).text for e in root.findall("a:entry", ns)]
        self.assertEqual(titles[0], "Down: c since " + incidents.fmt_time(T0 + 2 * STEP))
        self.assertTrue(titles[1].startswith("Resolved: a & b was down for 4h"))
        ids = [e.find("a:id", ns).text for e in root.findall("a:entry", ns)]
        self.assertEqual(len(set(ids)), 2)

    def test_site_files(self):
        recs = series("groq / x", "." * 10) + series("gemini / y", "...xx")
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "probes.jsonl")
            with open(data, "w") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in recs)
            out = os.path.join(tmp, "site")
            status.write_site(data, out, repo_url="https://github.com/o/r",
                              site_url="https://o.github.io/r/", now=T0 + 10 * STEP)
            with open(os.path.join(out, "index.html")) as fh:
                page = fh.read()
            with open(os.path.join(out, "badges", "gemini-y.json")) as fh:
                badge = json.load(fh)
            with open(os.path.join(out, "status.json")) as fh:
                st = json.load(fh)
        self.assertIn("<h2>Incidents</h2>", page)
        self.assertIn("Ongoing", page)
        self.assertNotIn("Up (resolved)", page)
        self.assertIn("https://github.com/o/r/issues?q=label%3Aincident", page)
        self.assertIn("https://img.shields.io/endpoint?url=https://o.github.io/r/badges/groq-x.json",
                      page)
        self.assertIn("https://o.github.io/r/feed.xml", page)
        self.assertEqual(badge["message"], "60.0% uptime 7d")
        self.assertTrue(st["providers"]["gemini / y"]["open_incident"])
        self.assertFalse(st["providers"]["groq / x"]["open_incident"])
        self.assertTrue(st["incidents"][0]["ongoing"])
        self.assertNotIn("—", page)


class FakeGitHub:
    def __init__(self, issues=()):
        self.issues = list(issues)
        self.created, self.comments, self.closed = [], [], []
        self.next_number = 100

    def ensure_label(self):
        pass

    def open_issues(self):
        return [i for i in self.issues if i["number"] not in self.closed]

    def create_issue(self, title, body):
        self.next_number += 1
        issue = {"number": self.next_number, "title": title, "body": body}
        self.issues.append(issue)
        self.created.append(issue)
        return issue

    def comment(self, number, body):
        self.comments.append((number, body))

    def close(self, number):
        self.closed.append(number)


class SyncTests(unittest.TestCase):
    def run_sync(self, gh, recs, now=T0 + 50 * STEP):
        found = incidents.find_incidents(recs)
        return github_issues.sync(gh, found, now, status.KIND_LABELS,
                                  page_url="https://o.github.io/r/", log=lambda m: None)

    def test_opens_one_issue_per_ongoing_incident(self):
        gh = FakeGitHub()
        actions = self.run_sync(gh, series("down", "..xx") + series("fine", "...."))
        self.assertEqual(actions, [("open", "down")])
        body = gh.created[0]["body"]
        self.assertEqual(gh.created[0]["title"], "down is down")
        self.assertIn('<!-- llmgate-incident provider="down" -->', body)
        self.assertIn("https://o.github.io/r/", body)

    def test_second_run_does_nothing(self):
        gh = FakeGitHub()
        recs = series("down", "..xx")
        self.run_sync(gh, recs)
        self.assertEqual(self.run_sync(gh, recs + series("down", "x", T0 + 4 * STEP)), [])
        self.assertEqual(len(gh.created), 1)

    def test_closes_with_duration_when_recovered(self):
        gh = FakeGitHub()
        self.run_sync(gh, series("p", "xx"))
        number = gh.created[0]["number"]
        actions = self.run_sync(gh, series("p", "xxx.."))
        self.assertEqual(actions, [("close", "p")])
        self.assertEqual(gh.closed, [number])
        self.assertIn("Down for 6h 0m, 3 failed probes", gh.comments[0][1])

    def test_ignores_issues_without_marker(self):
        gh = FakeGitHub([{"number": 1, "title": "unrelated", "body": "hi"}])
        self.assertEqual(self.run_sync(gh, series("p", "....")), [])
        self.assertEqual(gh.closed, [])

    def test_dry_run_calls_nothing(self):
        gh = FakeGitHub()
        found = incidents.find_incidents(series("p", "xx"))
        actions = github_issues.sync(gh, found, T0, status.KIND_LABELS,
                                     dry_run=True, log=lambda m: None)
        self.assertEqual(actions, [("open", "p")])
        self.assertEqual(gh.created, [])


if __name__ == "__main__":
    unittest.main()


class GitHubClientHTTPTests(unittest.TestCase):
    """The real client against a local server that records every request."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.seen = seen = []
        state = {"label_exists": True}

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n)) if n else None
                seen.append((self.command, self.path, body,
                             self.headers.get("authorization"),
                             self.headers.get("user-agent")))
                return body

            def do_GET(self):
                self._record()
                self._reply(200, [
                    {"number": 7, "body": '<!-- llmgate-incident provider="p" -->'},
                    {"number": 8, "body": "x", "pull_request": {}}])

            def do_POST(self):
                self._record()
                if self.path.endswith("/labels") and state["label_exists"]:
                    self._reply(422, {"message": "already_exists"})
                else:
                    self._reply(201, {"number": 9})

            def do_PATCH(self):
                self._record()
                self._reply(200, {"number": 7})

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = github_issues.GitHubClient(
            "o/r", "tok", api=f"http://127.0.0.1:{self.server.server_port}")

    def test_requests_on_the_wire(self):
        c = self.client
        c.ensure_label()  # 422 already exists is fine
        issues = c.open_issues()
        c.create_issue("t", "b")
        c.comment(7, "hello")
        c.close(7)
        self.assertEqual([i["number"] for i in issues], [7])  # PR filtered out
        calls = [(m, p, b) for m, p, b, _, _ in self.seen]
        self.assertEqual(calls[1], ("GET", "/repos/o/r/issues?state=open&labels=incident&per_page=100", None))
        self.assertEqual(calls[2], ("POST", "/repos/o/r/issues",
                                    {"title": "t", "body": "b", "labels": ["incident"]}))
        self.assertEqual(calls[3], ("POST", "/repos/o/r/issues/7/comments", {"body": "hello"}))
        self.assertEqual(calls[4], ("PATCH", "/repos/o/r/issues/7",
                                    {"state": "closed", "state_reason": "completed"}))
        self.assertTrue(all(a == "Bearer tok" for _, _, _, a, _ in self.seen))
        self.assertTrue(all(ua.startswith("llmgate/") for _, _, _, _, ua in self.seen))
