import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from llmgate.gateway import AllProvidersFailed, BudgetExceeded, Gateway
from llmgate.providers import ConfigError, build_provider, build_providers
from llmgate.store import Store

from .helpers import ScriptedProvider, boom, mock
from llmgate.providers import ProviderError


class _FakeAPI(BaseHTTPRequestHandler):
    """Tiny local OpenAI-style server. Behaviour set by the class attribute."""
    status = 200
    delay = 0.0
    seen_auth = []
    seen_agent = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        type(self).seen_auth.append(self.headers.get("authorization"))
        type(self).seen_agent.append(self.headers.get("user-agent"))
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.status != 200:
            self.send_response(self.status)
            self.end_headers()
            self.wfile.write(b'{\n  "error": {\n    "message": "nope"\n  }\n}')
            return
        if self.path.endswith("/v1/messages"):  # Anthropic shape
            type(self).seen_auth[-1] = self.headers.get("x-api-key")
            body = {"content": [{"type": "text", "text": "pong"}],
                    "usage": {"input_tokens": 3, "output_tokens": 1}}
        else:  # OpenAI shape
            body = {"choices": [{"message": {"content": "pong"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client gave up first (timeout test); not an error


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_routes_by_tier_cheapest_first(self):
        cheap_a = ScriptedProvider("cheap-a", "cheap", ["a"], price_in=5, price_out=5)
        cheap_b = ScriptedProvider("cheap-b", "cheap", ["b"], price_in=1, price_out=1)
        strong = ScriptedProvider("strong", "strong", ["s"])
        gw = Gateway([strong, cheap_a, cheap_b], self.store)
        self.assertEqual([p.name for p in gw.order("cheap")],
                         ["cheap-b", "cheap-a", "strong"])
        self.assertEqual(gw.ask("hi", tier="cheap").provider, "cheap-b")
        self.assertEqual(gw.ask("hi", tier="strong").provider, "strong")

    def test_unknown_tier_rejected(self):
        gw = Gateway([ScriptedProvider("c", "cheap", [])], self.store)
        with self.assertRaises(ValueError):
            gw.ask("hi", tier="medium")

    def test_retries_once_on_same_provider(self):
        flaky = ScriptedProvider("flaky", "cheap", [boom(), "recovered"])
        strong = ScriptedProvider("strong", "strong", ["never"])
        res = Gateway([flaky, strong], self.store).ask("hi")
        self.assertEqual(res.text, "recovered")
        self.assertEqual(res.provider, "flaky")
        self.assertEqual(flaky.calls, 2)
        self.assertEqual(strong.calls, 0)
        self.assertIsNone(res.fallback_from)

    def test_falls_back_after_retry_fails(self):
        cheap = ScriptedProvider("cheap", "cheap", [boom(), boom()])
        strong = ScriptedProvider("strong", "strong", ["from strong"])
        res = Gateway([cheap, strong], self.store).ask("hi")
        self.assertEqual(res.provider, "strong")
        self.assertEqual(res.fallback_from, "cheap")
        self.assertEqual(cheap.calls, 2)
        rows = self.store.all_calls()
        self.assertEqual([(r["provider"], r["ok"]) for r in rows],
                         [("cheap", 0), ("cheap", 0), ("strong", 1)])
        self.assertEqual(rows[2]["fallback_from"], "cheap")
        self.assertEqual(rows[0]["error"], "boom")

    def test_all_providers_fail(self):
        cheap = ScriptedProvider("cheap", "cheap", [boom("x1"), boom("x2")])
        strong = ScriptedProvider("strong", "strong", [boom("y1"), boom("y2")])
        with self.assertRaises(AllProvidersFailed) as ctx:
            Gateway([cheap, strong], self.store).ask("hi")
        self.assertEqual(len(ctx.exception.errors), 4)
        self.assertEqual(len(self.store.all_calls()), 4)
        self.assertTrue(all(r["ok"] == 0 for r in self.store.all_calls()))

    def test_every_call_is_logged_with_cost_and_feature(self):
        p = ScriptedProvider("c", "cheap", ["fine"], price_in=1000, price_out=1000)
        res = Gateway([p], self.store).ask("hi", feature="support")
        row = self.store.all_calls()[0]
        self.assertEqual(row["feature"], "support")
        self.assertEqual(row["tokens_in"], 100)
        self.assertAlmostEqual(row["cost"], 0.2)  # 200 tokens * 1000 / 1M
        self.assertAlmostEqual(res.cost, 0.2)

    def test_budget_enforced(self):
        # each call costs 0.2; budget 0.3 allows two calls (soft cap), blocks the third
        p = ScriptedProvider("c", "cheap", [], price_in=1000, price_out=1000)
        gw = Gateway([p], self.store, daily_budget=0.3)
        gw.ask("one")
        gw.ask("two")
        self.assertAlmostEqual(self.store.spend_today(), 0.4)
        with self.assertRaises(BudgetExceeded):
            gw.ask("three")
        self.assertEqual(p.calls, 2)

    def test_budget_none_means_unlimited(self):
        p = ScriptedProvider("c", "cheap", [], price_in=1000, price_out=1000)
        gw = Gateway([p], self.store, daily_budget=None)
        for _ in range(5):
            gw.ask("x")
        self.assertEqual(p.calls, 5)

    def test_spend_resets_on_new_day(self):
        now = [1_700_000_000.0]
        store = Store(":memory:", clock=lambda: now[0])
        p = ScriptedProvider("c", "cheap", [], price_in=1000, price_out=1000)
        gw = Gateway([p], store, daily_budget=0.1)
        gw.ask("x")
        with self.assertRaises(BudgetExceeded):
            gw.ask("y")
        now[0] += 86_400
        gw.ask("z")  # new UTC day, budget available again

    def test_mock_provider_seeded_failures_are_repeatable(self):
        def outcomes(seed):
            m = mock("m", "cheap", fail_rate=0.5, seed=seed)
            out = []
            for _ in range(20):
                try:
                    m.complete("hi")
                    out.append(True)
                except Exception:
                    out.append(False)
            return out
        self.assertEqual(outcomes(3), outcomes(3))
        self.assertIn(False, outcomes(3))
        self.assertIn(True, outcomes(3))

    def test_real_provider_requires_prices(self):
        spec = {"type": "anthropic", "name": "a", "tier": "cheap", "model": "m",
                "api_key_env": "X", "price_in": None, "price_out": None}
        with self.assertRaises(ConfigError):
            build_provider(spec)

    def test_real_provider_without_key_fails_cleanly(self):
        spec = {"type": "anthropic", "name": "a", "tier": "cheap", "model": "m",
                "api_key_env": "LLMGATE_TEST_UNSET_KEY", "price_in": 1, "price_out": 1}
        strong = ScriptedProvider("strong", "strong", ["ok"])
        res = Gateway([build_provider(spec), strong], self.store).ask("hi")
        self.assertEqual(res.provider, "strong")

    def test_non_retryable_error_skips_retry(self):
        limited = ScriptedProvider(
            "limited", "cheap", [ProviderError("rate limited (HTTP 429)", retryable=False)])
        backup = ScriptedProvider("backup", "cheap", ["ok"], price_in=50, price_out=50)
        res = Gateway([limited, backup], self.store, retries=1).ask("hi")
        self.assertEqual(limited.calls, 1)  # not retried
        self.assertEqual(res.provider, "backup")
        self.assertEqual(res.fallback_from, "limited")

    def test_priority_beats_price_within_tier(self):
        local = ScriptedProvider("local", "cheap", ["l"], price_in=0, price_out=0)
        local.priority = 10
        cloud = ScriptedProvider("cloud", "cheap", ["c"], price_in=5, price_out=5)
        gw = Gateway([local, cloud], self.store)
        self.assertEqual([p.name for p in gw.order("cheap")], ["cloud", "local"])


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    def gateway(self, flaky, backup, **kw):
        kw.setdefault("breaker_threshold", 3)
        kw.setdefault("breaker_cooldown", 60)
        return Gateway([flaky, backup], self.store, retries=1, **kw)

    def test_opens_after_threshold_and_skips_provider(self):
        flaky = ScriptedProvider("flaky", "cheap", [boom()] * 10)
        backup = ScriptedProvider("backup", "strong", ["ok"] * 10)
        gw = self.gateway(flaky, backup)
        gw.ask("1")  # flaky fails twice (attempt + retry)
        gw.ask("2")  # third consecutive failure -> breaker now open
        calls_before = flaky.calls
        res = gw.ask("3")
        self.assertEqual(flaky.calls, calls_before)  # not called at all
        self.assertEqual(res.skipped, ["flaky"])
        self.assertEqual(res.provider, "backup")
        self.assertEqual(res.fallback_from, "flaky")

    def test_half_open_after_cooldown_then_closes_on_success(self):
        flaky = ScriptedProvider("flaky", "cheap", [boom()] * 4 + ["back"])
        backup = ScriptedProvider("backup", "strong", ["ok"] * 10)
        gw = self.gateway(flaky, backup, breaker_threshold=3)
        gw.ask("1")
        gw.ask("2")
        self.assertTrue(gw.breaker_open(flaky))
        self.clock.t += 61
        self.assertFalse(gw.breaker_open(flaky))  # trial allowed
        res = gw.ask("3")
        self.assertEqual(res.provider, "flaky")
        self.assertFalse(gw.breaker_open(flaky))

    def test_trial_failure_reopens(self):
        flaky = ScriptedProvider("flaky", "cheap", [boom()] * 10)
        backup = ScriptedProvider("backup", "strong", ["ok"] * 10)
        gw = self.gateway(flaky, backup)
        gw.ask("1")
        gw.ask("2")
        self.clock.t += 61
        gw.ask("3")  # trial fails
        self.assertTrue(gw.breaker_open(flaky))

    def test_state_survives_a_new_gateway(self):
        # every `llmgate ask` is a new process; the log carries the state
        flaky = ScriptedProvider("flaky", "cheap", [boom()] * 10)
        backup = ScriptedProvider("backup", "strong", ["ok"] * 10)
        self.gateway(flaky, backup).ask("1")
        self.gateway(flaky, backup).ask("2")
        fresh = self.gateway(flaky, backup)
        self.assertTrue(fresh.breaker_open(flaky))

    def test_all_open_tries_everything_anyway(self):
        a = ScriptedProvider("a", "cheap", [boom()] * 3 + ["a-ok"])
        b = ScriptedProvider("b", "strong", [boom()] * 3 + ["b-ok"])
        gw = Gateway([a, b], self.store, retries=1, breaker_threshold=2)
        with self.assertRaises(AllProvidersFailed):
            gw.ask("1")
        self.assertTrue(gw.breaker_open(a) and gw.breaker_open(b))
        res = gw.ask("2")
        self.assertEqual(res.provider, "a")
        self.assertEqual(res.skipped, [])

    def test_disabled(self):
        flaky = ScriptedProvider("flaky", "cheap", [boom()] * 10)
        backup = ScriptedProvider("backup", "strong", ["ok"] * 10)
        gw = self.gateway(flaky, backup, breaker_threshold=None)
        for i in range(4):
            gw.ask(str(i))
        self.assertEqual(flaky.calls, 8)

    def test_attempts_share_a_request_id(self):
        flaky = ScriptedProvider("flaky", "cheap", [boom(), boom()])
        backup = ScriptedProvider("backup", "strong", ["ok"])
        res = self.gateway(flaky, backup).ask("1")
        ids = {r["request_id"] for r in self.store.all_calls()}
        self.assertEqual(ids, {res.request_id})


class StoreMigrationTests(unittest.TestCase):
    def test_old_database_gets_request_id_column(self):
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL, day TEXT NOT NULL, provider TEXT NOT NULL,"
                " ok INTEGER NOT NULL, error TEXT, tokens_in INTEGER NOT NULL DEFAULT 0,"
                " tokens_out INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,"
                " latency_ms REAL, feature TEXT NOT NULL DEFAULT 'default',"
                " fallback_from TEXT);"
                "INSERT INTO calls (ts, day, provider, ok) VALUES (1, '1970-01-01', 'p', 1);")
            conn.commit()
            conn.close()
            store = Store(path)
            store.record("p", ok=True, request_id="abc")
            rows = store.all_calls()
            store.close()
        self.assertIsNone(rows[0]["request_id"])
        self.assertEqual(rows[1]["request_id"], "abc")


class OptionalProviderTests(unittest.TestCase):
    SPEC = {"type": "openai", "name": "paid", "tier": "strong", "model": "m",
            "api_key_env": "PAID_KEY", "price_in": 1, "price_out": 1,
            "optional": True}

    def test_optional_without_key_is_skipped(self):
        notes = []
        built = build_providers([self.SPEC], environ={}, log=notes.append)
        self.assertEqual(built, [])
        self.assertIn("PAID_KEY not set", notes[0])

    def test_optional_with_key_is_built(self):
        built = build_providers([self.SPEC], environ={"PAID_KEY": "x"})
        self.assertEqual([p.name for p in built], ["paid"])

    def test_non_optional_without_key_is_still_built(self):
        spec = dict(self.SPEC, optional=False)
        self.assertEqual(len(build_providers([spec], environ={})), 1)


class OpenAICompatHTTPTests(unittest.TestCase):
    """Exercises the real urllib adapter against a local fake server."""

    def setUp(self):
        _FakeAPI.status = 200
        _FakeAPI.delay = 0.0
        _FakeAPI.seen_auth = []
        _FakeAPI.seen_agent = []
        self.server = HTTPServer(("127.0.0.1", 0), _FakeAPI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def provider(self, **extra):
        spec = {"type": "openai", "name": "fake", "tier": "cheap", "model": "m",
                "base_url": self.url, "price_in": 1, "price_out": 1}
        spec.update(extra)
        return build_provider(spec)

    def test_success_without_key(self):
        resp = self.provider().complete("ping")
        self.assertEqual(resp.text, "pong")
        self.assertEqual((resp.tokens_in, resp.tokens_out), (3, 1))
        self.assertEqual(_FakeAPI.seen_auth, [None])

    def test_sends_llmgate_user_agent(self):
        self.provider().complete("ping")
        self.assertTrue(_FakeAPI.seen_agent[0].startswith("llmgate/"))

    def test_error_body_is_one_line(self):
        _FakeAPI.status = 503
        with self.assertRaises(ProviderError) as ctx:
            self.provider().complete("ping")
        self.assertNotIn("\n", str(ctx.exception))
        self.assertIn('"message": "nope"', str(ctx.exception))

    def test_timeout_is_not_retryable(self):
        _FakeAPI.delay = 0.5
        with self.assertRaises(ProviderError) as ctx:
            self.provider(timeout=0.1).complete("ping")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("Timeout", str(ctx.exception))

    def test_429_is_not_retryable(self):
        _FakeAPI.status = 429
        with self.assertRaises(ProviderError) as ctx:
            self.provider().complete("ping")
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("rate limited", str(ctx.exception))

    def test_500_is_retryable(self):
        _FakeAPI.status = 503
        with self.assertRaises(ProviderError) as ctx:
            self.provider().complete("ping")
        self.assertTrue(ctx.exception.retryable)

    def test_anthropic_adapter_against_fake_server(self):
        os.environ["LLMGATE_TEST_ANTHROPIC_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "LLMGATE_TEST_ANTHROPIC_KEY", None)
        p = build_provider({"type": "anthropic", "name": "a", "tier": "strong",
                            "model": "m", "api_key_env": "LLMGATE_TEST_ANTHROPIC_KEY",
                            "base_url": self.url[:-3], "price_in": 1, "price_out": 1})
        resp = p.complete("ping", system="be brief")
        self.assertEqual(resp.text, "pong")
        self.assertEqual((resp.tokens_in, resp.tokens_out), (3, 1))
        self.assertEqual(_FakeAPI.seen_auth, ["test-key"])

    def test_anthropic_429_is_not_retryable(self):
        _FakeAPI.status = 429
        os.environ["LLMGATE_TEST_ANTHROPIC_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "LLMGATE_TEST_ANTHROPIC_KEY", None)
        p = build_provider({"type": "anthropic", "name": "a", "tier": "strong",
                            "model": "m", "api_key_env": "LLMGATE_TEST_ANTHROPIC_KEY",
                            "base_url": self.url[:-3], "price_in": 1, "price_out": 1})
        with self.assertRaises(ProviderError) as ctx:
            p.complete("ping")
        self.assertFalse(ctx.exception.retryable)

    def test_gateway_falls_back_once_on_429(self):
        _FakeAPI.status = 429
        store = Store(":memory:")
        self.addCleanup(store.close)
        backup = ScriptedProvider("backup", "strong", ["ok"])
        res = Gateway([self.provider(), backup], store, retries=1).ask("hi")
        self.assertEqual(res.provider, "backup")
        self.assertEqual(len(_FakeAPI.seen_auth), 1)  # one attempt, no retry


if __name__ == "__main__":
    unittest.main()
