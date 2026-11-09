"""Command line: ask, eval, dashboard, demo. Run from the repo root."""

import argparse
import copy
import json
import os
import sys

from . import dashboard as dash
from . import evals
from .gateway import AllProvidersFailed, BudgetExceeded, Gateway
from .providers import ConfigError, build_providers
from .store import Store

DEFAULT_CONFIG = "config.mock.json"
DEFAULT_CASES = "evals/cases.json"
DEFAULT_BASELINE = "evals/baseline.json"
REGRESSED_CONFIG = "config.regressed.json"

# Demo traffic: (feature, tier, prompt). Cheap providers are made flaky in the
# demo so fallbacks and failures show up on the dashboard.
DEMO_TRAFFIC = [
    ("support", "cheap", "Where is my refund?"),
    ("support", "cheap", "How do I reset my password?"),
    ("summarise", "strong", "Summarise in under ten words: The gateway routes "
     "requests to cheap or strong models and falls back on failure."),
    ("extract", "cheap", "Extract the founding year from: Acme was founded in "
     "1998 in Leeds."),
]
DEMO_FAIL_RATE = 0.35
DEMO_BUDGET = 0.08
DEMO_REQUESTS = 60


def load_config(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid JSON: {exc}")


def make_gateway(cfg, store, budget="from-config"):
    providers = build_providers(
        cfg.get("providers", []), log=lambda m: print(m, file=sys.stderr))
    if not providers:
        raise ConfigError("no providers enabled: set at least one API key "
                          "or add a provider without \"optional\": true")
    if budget == "from-config":
        budget = cfg.get("daily_budget")
    return Gateway(providers, store, daily_budget=budget,
                   retries=cfg.get("retries", 1))


def cmd_ask(args):
    cfg = load_config(args.config)
    store = Store(args.db or cfg.get("db", "llmgate.db"))
    try:
        res = make_gateway(cfg, store).ask(args.prompt, tier=args.tier,
                                           feature=args.feature)
    except BudgetExceeded as exc:
        print(f"budget exceeded: {exc}", file=sys.stderr)
        return 3
    except AllProvidersFailed as exc:
        print(exc, file=sys.stderr)
        return 4
    finally:
        store.close()
    print(res.text)
    note = f"[{res.provider} | cost ${res.cost:.6f} | {res.latency_ms:.0f} ms"
    if res.fallback_from:
        note += f" | fell back from {res.fallback_from}"
    print(note + "]", file=sys.stderr)
    return 0


def run_eval_with_config(config_path, cases_path):
    cfg = load_config(config_path)
    store = Store(":memory:")
    try:
        gw = make_gateway(cfg, store)
        return evals.run_eval(gw, evals.load_cases(cases_path))
    finally:
        store.close()


def print_report(report):
    for r in report.results:
        status = "PASS" if r.passed else "FAIL"
        extra = ""
        if r.provider:
            extra += f" [{r.provider}]"
        if not r.passed:
            extra += " " + (r.error or "; ".join(r.failures))
        print(f"  {status}  {r.id}{extra}")
    print(f"  score: {report.score:.2f}")


def cmd_eval(args):
    report = run_eval_with_config(args.config, args.cases)
    print_report(report)
    if args.update_baseline:
        evals.save_baseline(args.baseline, report)
        print(f"baseline written to {args.baseline}")
        return 0
    if not os.path.exists(args.baseline):
        print(f"no baseline at {args.baseline}; run with --update-baseline first",
              file=sys.stderr)
        return 2
    problems = evals.compare(report, evals.load_baseline(args.baseline))
    if problems:
        print("REGRESSION:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK: no regression against baseline")
    return 0


def cmd_dashboard(args):
    store = Store(args.db)
    try:
        out = dash.write_dashboard(store, args.out, budget=args.budget)
    finally:
        store.close()
    print(f"dashboard written to {out}")
    return 0


def cmd_demo(args):
    os.makedirs(args.out_dir, exist_ok=True)
    cfg = load_config(args.config)
    for spec in cfg["providers"]:
        if spec["tier"] == "cheap":
            spec["fail_rate"] = DEMO_FAIL_RATE

    db_path = os.path.join(args.out_dir, "demo.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    store = Store(db_path)
    gw = make_gateway(cfg, store, budget=DEMO_BUDGET)

    print(f"1) Traffic: {DEMO_REQUESTS} requests, cheap providers fail "
          f"{DEMO_FAIL_RATE:.0%} of the time, daily budget ${DEMO_BUDGET}")
    served = failed = 0
    for i in range(DEMO_REQUESTS):
        feature, tier, prompt = DEMO_TRAFFIC[i % len(DEMO_TRAFFIC)]
        try:
            gw.ask(prompt, tier=tier, feature=feature)
            served += 1
        except BudgetExceeded as exc:
            print(f"   request {i + 1}: BudgetExceeded, stopping ({exc})")
            break
        except AllProvidersFailed:
            failed += 1
    print(f"   served {served}, failed {failed}, spent ${store.spend_today():.4f}")

    out = os.path.join(args.out_dir, "dashboard.html")
    dash.write_dashboard(store, out, budget=DEMO_BUDGET)
    store.close()
    print(f"2) Dashboard written to {out}")

    baseline_path = args.baseline
    if not os.path.exists(baseline_path):
        baseline_path = os.path.join(args.out_dir, "baseline.json")
        evals.save_baseline(baseline_path, run_eval_with_config(args.config, args.cases))
    baseline = evals.load_baseline(baseline_path)

    print("3) Eval with the good config")
    good = run_eval_with_config(args.config, args.cases)
    print_report(good)
    print("   ->", "no regression" if not evals.compare(good, baseline)
          else "REGRESSION")

    print("4) Eval with the regressed config (cheap-mock answers wrong)")
    bad = run_eval_with_config(args.regressed_config, args.cases)
    print_report(bad)
    problems = evals.compare(bad, baseline)
    for p in problems:
        print(f"   - {p}")
    print("   -> CI would", "FAIL the build (exit 1)" if problems else "pass")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="llmgate", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("ask", help="send one prompt through the gateway")
    a.add_argument("prompt")
    a.add_argument("--tier", choices=("cheap", "strong"), default="cheap")
    a.add_argument("--feature", default="cli")
    a.add_argument("--config", default=DEFAULT_CONFIG)
    a.add_argument("--db", default=None)
    a.set_defaults(func=cmd_ask)

    e = sub.add_parser("eval", help="run eval cases and compare to the baseline")
    e.add_argument("--config", default=DEFAULT_CONFIG)
    e.add_argument("--cases", default=DEFAULT_CASES)
    e.add_argument("--baseline", default=DEFAULT_BASELINE)
    e.add_argument("--update-baseline", action="store_true")
    e.set_defaults(func=cmd_eval)

    d = sub.add_parser("dashboard", help="build the static HTML dashboard")
    d.add_argument("--db", default="llmgate.db")
    d.add_argument("--out", default="dashboard.html")
    d.add_argument("--budget", type=float, default=None)
    d.set_defaults(func=cmd_dashboard)

    m = sub.add_parser("demo", help="offline demo of everything")
    m.add_argument("--config", default=DEFAULT_CONFIG)
    m.add_argument("--regressed-config", default=REGRESSED_CONFIG)
    m.add_argument("--cases", default=DEFAULT_CASES)
    m.add_argument("--baseline", default=DEFAULT_BASELINE)
    m.add_argument("--out-dir", default="demo_out")
    m.set_defaults(func=cmd_demo)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
