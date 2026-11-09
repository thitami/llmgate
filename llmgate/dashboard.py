"""Static HTML dashboard generated from the call log. No JavaScript, no assets.

Every row in the log is one provider attempt, so "calls" counts attempts
(a retry or a fallback is its own call). Latency percentiles use nearest-rank
over all attempts that have a latency, failed ones included.
"""

import html
import math
from datetime import datetime, timezone


def percentile(values, p):
    """Nearest-rank percentile. Returns None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def summarise(rows):
    lat = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    fails = sum(1 for r in rows if not r["ok"])
    return {
        "calls": len(rows),
        "failures": fails,
        "failure_rate": (fails / len(rows)) if rows else 0.0,
        # successful calls that were served by a fallback provider
        "fallbacks": sum(1 for r in rows if r["ok"] and r["fallback_from"]),
        "tokens": sum(r["tokens_in"] + r["tokens_out"] for r in rows),
        "cost": sum(r["cost"] for r in rows),
        "p50": percentile(lat, 50),
        "p95": percentile(lat, 95),
    }


def group_by(rows, key):
    groups = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    return {k: summarise(v) for k, v in sorted(groups.items())}


def _ms(v):
    return "n/a" if v is None else f"{v:,.0f} ms"


def _cost(v):
    return f"${v:,.4f}"


def _tile(label, value):
    return (f'<div class="tile"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{html.escape(value)}</div></div>')


def _table(title, key_header, groups):
    head = ("<tr><th>{}</th><th>Calls</th><th>Failure rate</th><th>Fallbacks</th>"
            "<th>Tokens</th><th>Cost</th><th>p50</th><th>p95</th></tr>"
            ).format(html.escape(key_header))
    body = []
    for name, s in groups.items():
        body.append(
            f"<tr><td>{html.escape(str(name))}</td><td>{s['calls']}</td>"
            f"<td>{s['failure_rate']:.1%}</td><td>{s['fallbacks']}</td>"
            f"<td>{s['tokens']:,}</td><td>{_cost(s['cost'])}</td>"
            f"<td>{_ms(s['p50'])}</td><td>{_ms(s['p95'])}</td></tr>")
    if not body:
        body.append('<tr><td colspan="8" class="muted">No calls yet.</td></tr>')
    return (f"<h2>{html.escape(title)}</h2><table>{head}{''.join(body)}</table>")


CSS = """
:root{--bg:#fafaf9;--surface:#fff;--ink:#1c1917;--ink2:#57534e;--muted:#78716c;
--line:#e7e5e4;--bad:#b42318;}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--surface:#292524;
--ink:#fafaf9;--ink2:#d6d3d1;--muted:#a8a29e;--line:#44403c;--bad:#f97066;}}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:32px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:12px 14px}
.label{font-size:12px;color:var(--ink2)}
.value{font-size:22px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--surface);
border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:14px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{font-weight:600;color:var(--ink2);font-size:12px}
tr:last-child td{border-bottom:0}
.muted{color:var(--muted);text-align:left}
.fail td.err{text-align:left;color:var(--ink2);word-break:break-word}
.tag{color:var(--bad);font-weight:600}
"""


def render(rows, budget=None, spent_today=None, generated_at=None, recent=10):
    total = summarise(rows)
    when = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tiles = [
        _tile("Calls", f"{total['calls']:,}"),
        _tile("Failure rate", f"{total['failure_rate']:.1%}"),
        _tile("Fallbacks served", f"{total['fallbacks']:,}"),
        _tile("Tokens", f"{total['tokens']:,}"),
        _tile("Cost", _cost(total["cost"])),
        _tile("p50 latency", _ms(total["p50"])),
        _tile("p95 latency", _ms(total["p95"])),
    ]
    if budget is not None and spent_today is not None:
        tiles.append(_tile("Spent today / budget",
                           f"{_cost(spent_today)} / {_cost(budget)}"))

    failed = [r for r in rows if not r["ok"]][-recent:][::-1]
    fail_rows = []
    for r in failed:
        ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        fail_rows.append(
            f"<tr class=\"fail\"><td>{ts}</td><td>{html.escape(r['provider'])}</td>"
            f"<td>{html.escape(r['feature'])}</td>"
            f"<td class=\"err\"><span class=\"tag\">Failed</span> "
            f"{html.escape(r['error'] or '')}</td></tr>")
    if not fail_rows:
        fail_rows.append('<tr><td colspan="4" class="muted">No failures logged.</td></tr>')
    fail_table = ("<h2>Recent failures</h2><table><tr><th>Time</th><th>Provider</th>"
                  "<th>Feature</th><th style=\"text-align:left\">Error</th></tr>"
                  + "".join(fail_rows) + "</table>")

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>llmgate dashboard</title><style>{CSS}</style></head><body><main>"
        "<h1>llmgate dashboard</h1>"
        f"<p class=\"sub\">Generated {html.escape(when)}. Each call is one provider "
        "attempt, so retries and fallbacks count as their own calls.</p>"
        f"<div class=\"tiles\">{''.join(tiles)}</div>"
        + _table("By provider", "Provider", group_by(rows, "provider"))
        + _table("By feature", "Feature", group_by(rows, "feature"))
        + fail_table + "</main></body></html>")


def write_dashboard(store, out_path, budget=None):
    rows = store.all_calls()
    page = render(rows, budget=budget,
                  spent_today=store.spend_today() if budget is not None else None)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path
