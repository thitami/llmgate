import contextlib
import io
import json
import os
import tempfile
import unittest

from llmgate.cli import main

from .helpers import path

COMMON = ["--cases", path("evals", "cases.json"),
          "--baseline", path("evals", "baseline.json")]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_status_page_writes_the_whole_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "probes.jsonl")
            with open(data, "w") as fh:
                for i, ok in enumerate([True, False, False]):
                    fh.write(json.dumps({"ts": "x", "t": 1_800_000_000 + i * 7200,
                                         "provider": "p one", "model": "m", "ok": ok,
                                         "kind": None if ok else "timeout",
                                         "status": None, "latency_ms": 100.0,
                                         "tokens_in": 1, "tokens_out": 1,
                                         "correct": True if ok else None,
                                         "error": None if ok else "t/o"}) + "\n")
            out = os.path.join(tmp, "new", "site")
            code, _, _ = run(["status-page", "--data", data, "--out-dir", out,
                              "--site-url", "https://e.x/s/"])
            self.assertEqual(code, 0)
            for name in ("index.html", "status.json", "feed.xml", "probes.jsonl",
                         os.path.join("badges", "p-one.json")):
                self.assertTrue(os.path.exists(os.path.join(out, name)), name)
            with open(os.path.join(out, "status.json")) as fh:
                st = json.load(fh)
            self.assertTrue(st["providers"]["p one"]["open_incident"])

    def test_incidents_sync_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "probes.jsonl")
            with open(data, "w") as fh:
                for i in range(2):
                    fh.write(json.dumps({"ts": "x", "t": 1_800_000_000 + i, "provider": "p",
                                         "model": "m", "ok": False, "kind": "timeout",
                                         "error": "t/o"}) + "\n")
            code, out, _ = run(["incidents-sync", "--data", data, "--repo", "o/r",
                                "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("open issue: p is down", out)

    def test_dashboard_warns_on_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run(["dashboard", "--db", os.path.join(tmp, "none.db"),
                                "--out", os.path.join(tmp, "d.html")])
        self.assertEqual(code, 0)
        self.assertIn("does not exist", err)
        self.assertEqual(err.count("warning"), 1)

    def test_eval_passes_with_good_config(self):
        code, out, _ = run(["eval", "--config", path("config.mock.json")] + COMMON)
        self.assertEqual(code, 0)
        self.assertIn("score: 1.00", out)

    def test_eval_fails_ci_with_regressed_config(self):
        code, out, _ = run(["eval", "--config", path("config.regressed.json")] + COMMON)
        self.assertEqual(code, 1)
        self.assertIn("score: 0.50", out)
        self.assertIn("REGRESSION", out)

    def test_eval_without_baseline_is_an_error(self):
        code, _, err = run(["eval", "--config", path("config.mock.json"),
                            "--cases", path("evals", "cases.json"),
                            "--baseline", path("evals", "does_not_exist.json")])
        self.assertEqual(code, 2)
        self.assertIn("update-baseline", err)

    def test_example_config_refuses_to_run_without_prices(self):
        code, _, err = run(["eval", "--config", path("config.example.json")] + COMMON)
        self.assertEqual(code, 2)
        self.assertIn("price", err)

    def test_demo_runs_and_shows_the_gate_failing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(["demo", "--out-dir", tmp,
                                "--config", path("config.mock.json"),
                                "--regressed-config", path("config.regressed.json")]
                               + COMMON)
        self.assertEqual(code, 0)
        self.assertIn("BudgetExceeded", out)
        self.assertIn("CI would FAIL the build", out)


if __name__ == "__main__":
    unittest.main()
