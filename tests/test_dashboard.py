import os
import tempfile
import unittest

from llmgate import dashboard
from llmgate.gateway import Gateway
from llmgate.store import Store

from .helpers import ScriptedProvider, boom


class DashboardTests(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(dashboard.percentile(values, 50), 50)
        self.assertEqual(dashboard.percentile(values, 95), 95)
        self.assertEqual(dashboard.percentile([7], 95), 7)
        self.assertIsNone(dashboard.percentile([], 50))

    def make_store(self):
        store = Store(":memory:")
        cheap = ScriptedProvider("cheap", "cheap", [boom("<b>down</b>"), boom(), "a", "b"])
        strong = ScriptedProvider("strong", "strong", ["s"])
        gw = Gateway([cheap, strong], store)
        gw.ask("one", feature="support")   # cheap fails twice, strong serves
        gw.ask("two", feature="extract")   # cheap serves
        return store

    def test_summary_numbers(self):
        store = self.make_store()
        rows = store.all_calls()
        store.close()
        s = dashboard.summarise(rows)
        self.assertEqual(s["calls"], 4)
        self.assertEqual(s["failures"], 2)
        self.assertAlmostEqual(s["failure_rate"], 0.5)
        self.assertEqual(s["fallbacks"], 1)
        self.assertEqual(s["tokens"], 400)  # two successful calls x 200 tokens
        self.assertEqual(s["p50"], 5.0)
        by_provider = dashboard.group_by(rows, "provider")
        self.assertEqual(by_provider["cheap"]["calls"], 3)
        self.assertEqual(by_provider["strong"]["calls"], 1)
        self.assertEqual(set(dashboard.group_by(rows, "feature")), {"support", "extract"})

    def test_html_output(self):
        store = self.make_store()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "d.html")
            dashboard.write_dashboard(store, out, budget=1.0)
            with open(out, encoding="utf-8") as fh:
                page = fh.read()
        store.close()
        self.assertIn("llmgate dashboard", page)
        self.assertIn("By provider", page)
        self.assertIn("By feature", page)
        self.assertIn("Recent failures", page)
        self.assertIn("50.0%", page)
        self.assertIn("cheap", page)
        self.assertIn("support", page)
        self.assertIn("&lt;b&gt;down&lt;/b&gt;", page)  # escaped
        self.assertNotIn("<b>down</b>", page)
        self.assertNotIn("<script", page)

    def test_empty_log_renders(self):
        page = dashboard.render([])
        self.assertIn("No calls yet.", page)
        self.assertIn("No failures logged.", page)


if __name__ == "__main__":
    unittest.main()
