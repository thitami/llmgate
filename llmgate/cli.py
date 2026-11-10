"""Command line: ask, eval, dashboard, demo. Run from the repo root."""

import argparse
import copy
import json
import os
import sys
import time

from . import dashboard as dash
from . import evals
from . import status
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
    breaker = cfg.get("circuit_breaker", {})
    return Gateway(providers, store, daily_budget=budget,
                   retries=cfg.get("retries", 1),
                   breaker_threshold=breaker.get("threshold", 3),
                   breaker_cooldown=breaker.get("cooldown_seconds", 60))


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
    if res.skipped:
        note += f" | circuit open: {', '.join(res.skipped)}"
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
    existed = os.path.exists(args.db)
    if not existed:
        print(f"warning: {args.db} does not exist; the dashboard will be empty. "
              "Send some requests with `llmgate ask` first.", file=sys.stderr)
    store = Store(args.db)
    try:
        if existed and not store.all_calls():
            print(f"warning: no calls logged in {args.db}; the dashboard will "
                  "be empty.", file=sys.stderr)
        out = dash.write_dashboard(store, args.out, budget=args.budget)
    finally:
        store.close()
    print(f"dashboard written to {out}")
    return 0


def cmd_probe(args):
    cfg = load_config(args.config)
    providers = build_providers(cfg.get("providers", []),
                                log=lambda m: print(m, file=sys.stderr))
    if not providers:
        raise ConfigError("no providers enabled: set at least one API key")
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    for r in status.probe_all(providers, args.out):
        if r["ok"]:
            note = f"ok {r['latency_ms']:.0f} ms, answered {r.get('answer', '')!r}"
        else:
            note = f"FAILED {r['kind']}: {r['error']}"
        print(f"{r['provider']:<24} {note}")
    # Provider failures are data, not errors: exit 0 so the schedule keeps going.
    return 0


def cmd_status_page(args):
    if not os.path.exists(args.data):
        print(f"warning: {args.data} does not exist; writing an empty page",
              file=sys.stderr)
    n = status.write_site(args.data, args.out_dir, title=args.title,
                          repo_url=args.repo_url, site_url=args.site_url,
                          slot_hours=args.slot_hours)
    print(f"status site from {n} probes written to {args.out_dir}/ "
          "(index.html, status.json, feed.xml, badges/)")
    return 0


def cmd_incidents_sync(args):
    from . import github_issues, incidents
    records = status.load_records(args.data) if os.path.exists(args.data) else []
    found = incidents.find_incidents(records)
    if args.dry_run:
        client = _DryRunClient()
    else:
        token = os.environ.get(args.token_env)
        if not token:
            raise ConfigError(f"env var {args.token_env} is not set")
        client = github_issues.GitHubClient(args.repo, token)
    try:
        github_issues.sync(client, found, time.time(), status.KIND_LABELS,
                           page_url=args.page_url, dry_run=args.dry_run)
    except github_issues.GitHubError as exc:
        print(f"github error: {exc}", file=sys.stderr)
        return 1
    return 0


class _DryRunClient:
    """Pretends no incident issues are open, to preview what sync would open."""

    def ensure_label(self):
        pass

    def open_issues(self):
        return []


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

    pr = sub.add_parser("probe", help="probe each provider once, append to a JSONL file")
    pr.add_argument("--config", default="config.status.json")
    pr.add_argument("--out", default="status/probes.jsonl")
    pr.set_defaults(func=cmd_probe)

    sp = sub.add_parser("status-page", help="build the public status site")
    sp.add_argument("--data", default="status/probes.jsonl")
    sp.add_argument("--out-dir", default="site")
    sp.add_argument("--title", default="Free LLM API status")
    sp.add_argument("--repo-url", default=None)
    sp.add_argument("--site-url", default=None,
                    help="public URL of the site, for feed and badge links")
    sp.add_argument("--slot-hours", type=int, default=2)
    sp.set_defaults(func=cmd_status_page)

    ins = sub.add_parser("incidents-sync",
                         help="open/close GitHub issues to match status-board incidents")
    ins.add_argument("--data", default="status/probes.jsonl")
    ins.add_argument("--repo", required=True, help="owner/name")
    ins.add_argument("--page-url", default=None)
    ins.add_argument("--token-env", default="GITHUB_TOKEN")
    ins.add_argument("--dry-run", action="store_true",
                     help="print what would be opened, call nothing")
    ins.set_defaults(func=cmd_incidents_sync)

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
