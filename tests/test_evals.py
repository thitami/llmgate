import os
import tempfile
import unittest

from llmgate import evals
from llmgate.gateway import Gateway
from llmgate.store import Store

from .helpers import mock


class CheckTests(unittest.TestCase):
    def passes(self, check, text):
        return evals.run_check(check, text)[0]

    def test_equals(self):
        self.assertTrue(self.passes({"type": "equals", "value": "4"}, " 4\n"))
        self.assertFalse(self.passes({"type": "equals", "value": "4"}, "It is 4"))

    def test_contains_and_not_contains(self):
        self.assertTrue(self.passes({"type": "contains", "value": "Paris"}, "In Paris."))
        self.assertFalse(self.passes({"type": "contains", "value": "Paris"}, "paris"))
        self.assertTrue(self.passes(
            {"type": "contains", "value": "Paris", "ignore_case": True}, "paris"))
        self.assertTrue(self.passes({"type": "not_contains", "value": "London"}, "Paris"))
        self.assertFalse(self.passes({"type": "not_contains", "value": "London"}, "London"))

    def test_regex(self):
        self.assertTrue(self.passes({"type": "regex", "value": r"^\d{4}$"}, "1998"))
        self.assertFalse(self.passes({"type": "regex", "value": r"^\d{4}$"}, "in 1998"))

    def test_max_words(self):
        self.assertTrue(self.passes({"type": "max_words", "value": 3}, "one two three"))
        self.assertFalse(self.passes({"type": "max_words", "value": 3}, "one two three four"))

    def test_bad_check_rejected(self):
        with self.assertRaises(ValueError):
            evals.run_check({"type": "vibes", "value": "x"}, "text")
        with self.assertRaises(ValueError):
            evals.run_check({"type": "equals"}, "text")


class PipelineTests(unittest.TestCase):
    CASES = [
        {"id": "math", "prompt": "2+2?", "tier": "cheap",
         "checks": [{"type": "equals", "value": "4"}]},
        {"id": "city", "prompt": "capital of france?", "tier": "cheap",
         "checks": [{"type": "contains", "value": "Paris"}]},
    ]

    def gateway(self, answers):
        store = Store(":memory:")
        self.addCleanup(store.close)
        return Gateway([mock("m", "cheap", answers=answers)], store)

    def test_score_and_baseline_roundtrip(self):
        report = evals.run_eval(self.gateway({"2+2": "4", "france": "Paris"}), self.CASES)
        self.assertEqual(report.score, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "baseline.json")
            evals.save_baseline(p, report)
            baseline = evals.load_baseline(p)
        self.assertEqual(baseline["cases"], {"math": True, "city": True})
        self.assertEqual(evals.compare(report, baseline), [])

    def test_regression_detected(self):
        good = evals.run_eval(self.gateway({"2+2": "4", "france": "Paris"}), self.CASES)
        bad = evals.run_eval(self.gateway({"2+2": "5", "france": "Paris"}), self.CASES)
        self.assertEqual(bad.score, 0.5)
        problems = evals.compare(bad, good.to_baseline())
        self.assertTrue(any("math" in p for p in problems))
        self.assertTrue(any("score dropped 1.00 -> 0.50" in p for p in problems))

    def test_improvement_is_not_a_regression(self):
        bad = evals.run_eval(self.gateway({"2+2": "5", "france": "Paris"}), self.CASES)
        good = evals.run_eval(self.gateway({"2+2": "4", "france": "Paris"}), self.CASES)
        self.assertEqual(evals.compare(good, bad.to_baseline()), [])

    def test_missing_case_counts_as_regression(self):
        good = evals.run_eval(self.gateway({"2+2": "4", "france": "Paris"}), self.CASES)
        fewer = evals.run_eval(self.gateway({"2+2": "4"}), self.CASES[:1])
        problems = evals.compare(fewer, good.to_baseline())
        self.assertTrue(any("city" in p and "not run" in p for p in problems))

    def test_duplicate_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            with open(p, "w") as fh:
                fh.write('[{"id":"a","prompt":"x","checks":[]},'
                         '{"id":"a","prompt":"y","checks":[]}]')
            with self.assertRaises(ValueError):
                evals.load_cases(p)


if __name__ == "__main__":
    unittest.main()
