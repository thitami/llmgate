import json
import os
import tempfile
import unittest

from llmgate import status
from llmgate.providers import ProviderError

from .helpers import ScriptedProvider, path

NOW = 1_800_000_000.0  # fixed clock; a multiple of 7200, so slots line up
HOUR = 3600


def rec(provider, t, ok=True, kind=None, latency=500.0, correct=True):
    return {"ts": status._iso(t), "t": t, "provider": provider, "model": "m",
            "ok": ok, "kind": kind, "status": None,
            "latency_ms": latency, "tokens_in": 1, "tokens_out": 1,
            "correct": correct if ok else None,
            "error": None if ok else f"{kind} happened"}


class ProbeTests(unittest.TestCase):
    def test_success_record(self):
        p = ScriptedProvider("p", "cheap", ["OK."])
        r = status.probe_one(p, clock=lambda: NOW)
        self.assertTrue(r["ok"])
        self.assertTrue(r["correct"])
        self.assertEqual(r["ts"], status._iso(NOW))
        self.assertIsNone(r["kind"])

    def test_wrong_answer_is_up_but_flagged(self):
        p = ScriptedProvider("p", "cheap", ["Sure! Here's a poem about tokens."])
        r = status.probe_one(p, clock=lambda: NOW)
        self.assertTrue(r["ok"])
        self.assertFalse(r["correct"])  # "tokens" must not match "ok"

    def test_failure_record_keeps_kind_and_status(self):
        err = ProviderError("rate limited (HTTP 429): slow down", latency_ms=12.0,
                            retryable=False, kind="rate_limited", status=429)
        p = ScriptedProvider("p", "cheap", [err])
        r = status.probe_one(p, clock=lambda: NOW)
        self.assertFalse(r["ok"])
        self.assertEqual((r["kind"], r["status"], r["latency_ms"]),
                         ("rate_limited", 429, 12.0))

    def test_probe_all_appends_jsonl_one_attempt_each(self):
        a = ScriptedProvider("a", "cheap", ["OK"])
        b = ScriptedProvider("b", "strong", [ProviderError("down", kind="server_error")])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "probes.jsonl")
            with open(out, "w") as fh:
                fh.write(json.dumps(rec("old", NOW - HOUR)) + "\n")
            status.probe_all([a, b], out, clock=lambda: NOW)
            records = status.load_records(out)
        self.assertEqual([r["provider"] for r in records], ["old", "a", "b"])
        self.assertEqual((a.calls, b.calls), (1, 1))  # no retries

    def test_bad_line_is_reported_with_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "probes.jsonl")
            with open(out, "w") as fh:
                fh.write(json.dumps(rec("a", NOW)) + "\n{oops\n")
            with self.assertRaisesRegex(ValueError, ":2:"):
                status.load_records(out)


class SummaryTests(unittest.TestCase):
    def test_state_of_slot(self):
        self.assertEqual(status.state_of([]), "none")
        self.assertEqual(status.state_of([rec("a", NOW)]), "up")
        self.assertEqual(status.state_of([rec("a", NOW, False, "rate_limited")]), "limited")
        self.assertEqual(status.state_of([rec("a", NOW, False, "rate_limited"),
                                          rec("a", NOW, False, "timeout")]), "down")

    def test_uptime_windows_and_latency_ok_only(self):
        recs = [rec("a", NOW - 2 * 86400, False, "timeout", latency=30000.0),
                rec("a", NOW - 3 * HOUR, latency=400.0),
                rec("a", NOW - 1 * HOUR, False, "server_error", latency=5.0),
                rec("a", NOW - 0.5 * HOUR, latency=600.0)]
        s = status.provider_summary(recs, NOW)
        self.assertAlmostEqual(s["up_24h"], 2 / 3)
        self.assertAlmostEqual(s["up_7d"], 2 / 4)
        self.assertEqual(s["p50"], 400.0)  # failed attempts excluded
        self.assertEqual(s["p95"], 600.0)
        self.assertEqual(s["kinds_7d"], {"timeout": 1, "server_error": 1})
        self.assertAlmostEqual(s["days_of_data"], 2.0)

    def test_strip_has_fixed_slots_and_places_records(self):
        recs = [rec("a", NOW - 10), rec("a", NOW - 5 * HOUR, False, "timeout")]
        slots = status.strip_slots(recs, NOW - 1, slot_hours=2, days=7)
        self.assertEqual(len(slots), 84)
        states = [status.state_of(rs) for _, rs in slots]
        self.assertEqual(states[-1], "up")
        self.assertEqual(states[-3], "down")
        self.assertEqual(states.count("none"), 82)

    def test_headline_needs_enough_data(self):
        few = {"a": {"up_7d": 1.0, "probes_7d": 2}, "b": {"up_7d": 0.5, "probes_7d": 2}}
        self.assertEqual(status.headline(few), "")
        enough = {"a": {"up_7d": 0.99, "probes_7d": 80},
                  "b": {"up_7d": 0.6, "probes_7d": 80}}
        line = status.headline(enough)
        self.assertIn("Most reliable over the last 7 days: a (99.0%)", line)
        self.assertIn("Least reliable: b (60.0%)", line)


class HeadlineTieTests(unittest.TestCase):
    def test_ties_are_named_together(self):
        s = {n: {"up_7d": u, "probes_7d": 10}
             for n, u in [("a", 1.0), ("b", 1.0), ("c", 0.5)]}
        self.assertIn("Most reliable over the last 7 days: a, b (100.0%)",
                      status.headline(s))

    def test_big_tie_is_counted(self):
        s = {n: {"up_7d": 1.0, "probes_7d": 10} for n in "abcd"}
        s["z"] = {"up_7d": 0.0, "probes_7d": 10}
        line = status.headline(s)
        self.assertIn("4 providers (100.0%)", line)
        self.assertIn("Least reliable: z (0.0%)", line)


class PageTests(unittest.TestCase):
    def week(self):
        recs = []
        for i in range(84):
            t = NOW - (84 - i) * 2 * HOUR + 60
            recs.append(rec("groq / x", t))
            recs.append(rec("gemini / <y>", t, ok=(i % 2 == 0),
                            kind=None if i % 2 == 0 else "server_error"))
        return recs

    def test_page_content(self):
        page = status.render(self.week(), now=NOW, repo_url="https://example.com/r")
        self.assertIn("Free LLM API status", page)
        self.assertIn("Most reliable over the last 7 days: groq / x (100.0%)", page)
        self.assertIn("gemini / &lt;y&gt;", page)  # escaped
        self.assertNotIn("<y>", page)
        self.assertIn("server error (5xx) 42", page)
        self.assertIn("Rate limited", page)  # legend label, not colour alone
        self.assertNotIn("((", page)  # no nested brackets in badges
        self.assertIn("How this is measured", page)
        self.assertIn("https://example.com/r", page)
        self.assertIn("days of data", page)  # 30d window has only 7 days
        self.assertNotIn("<script", page)
        self.assertNotIn("—", page)  # no em dashes

    def test_empty_page(self):
        page = status.render([], now=NOW)
        self.assertIn("No probes recorded yet.", page)
        self.assertIn("No failures recorded.", page)


class StatusConfigTests(unittest.TestCase):
    def test_status_config_loads_and_everything_is_optional(self):
        from llmgate.providers import build_providers
        with open(path("config.status.json")) as fh:
            cfg = json.load(fh)
        self.assertTrue(all(p.get("optional") for p in cfg["providers"]))
        self.assertEqual(build_providers(cfg["providers"], environ={}), [])
        keys = {p["api_key_env"]: "x" for p in cfg["providers"]}
        built = build_providers(cfg["providers"], environ=keys)
        self.assertEqual(len(built), len(cfg["providers"]))
        # GitHub Models runs on the workflow's own token, no extra secret
        self.assertIn("GITHUB_TOKEN", keys)
        # only GitHub Models with the built-in token: 2 providers on, rest skipped
        self.assertEqual(len(build_providers(cfg["providers"],
                                             environ={"GITHUB_TOKEN": "t"})), 2)


if __name__ == "__main__":
    unittest.main()
