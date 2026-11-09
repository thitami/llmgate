import contextlib
import io
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
