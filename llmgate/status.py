"""Status board: probe each provider on a schedule and publish a static page.

A probe is one tiny request to one provider: no retries, no fallback, no
circuit breaker. It measures the provider, not the gateway. Results are
appended to a JSONL file, one line per probe, so the history is plain text
that anyone can download and check.
"""

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone

import os

from . import incidents as inc
from .dashboard import CSS as DASHBOARD_CSS
from .dashboard import percentile
from .providers import ProviderError

# The expected answer must be something a server could not return by
# accident. An earlier "Reply with exactly one word: OK" probe was passed by a
# plain-text "OK" that arrived in under 100 ms: possibly a server, not a model.
PROBE_PROMPT = "What is 17 + 25? Reply with only the number."
EXPECTED = re.compile(r"(?<!\d)42(?!\d)")

# Status colours (good / warning / critical) always ship with a symbol and a
# label, never colour alone.
STATES = {
    "up": ("#0ca30c", "✓", "Up"),
    "limited": ("#fab219", "!", "Rate limited"),
    "down": ("#d03b3b", "✕", "Down"),
    "none": ("#c3c2b7", "·", "No data"),
}

KIND_LABELS = {
    "rate_limited": "rate limited (429)",
    "server_error": "server error (5xx)",
    "timeout": "timeout",
    "network": "network error",
    "auth": "auth / forbidden",
    "client_error": "client error (4xx)",
    "bad_response": "bad response",
    "wrong_answer": "wrong answer",
    "config": "config",
    "error": "error",
}


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_one(provider, clock=time.time):
    """Send the probe prompt to one provider and return a record dict."""
    ts = clock()
    record = {"ts": _iso(ts), "t": ts, "provider": provider.name,
              "model": getattr(provider, "model", "mock"), "ok": False,
              "kind": None, "status": None, "latency_ms": None,
              "tokens_in": 0, "tokens_out": 0, "correct": None, "error": None,
              "prompt": PROBE_PROMPT}
    try:
        resp = provider.complete(PROBE_PROMPT)
    except ProviderError as exc:
        record.update(kind=getattr(exc, "kind", "error"),
                      status=getattr(exc, "status", None),
                      latency_ms=exc.latency_ms, error=str(exc)[:200])
        return record
    text = (resp.text or "").strip()
    correct = bool(EXPECTED.search(text))
    record.update(latency_ms=resp.latency_ms, tokens_in=resp.tokens_in,
                  tokens_out=resp.tokens_out, correct=correct,
                  answer=" ".join(text.split())[:80])
    if correct:
        record["ok"] = True
    else:
        # Something answered, but not the question: not a working model.
        record.update(ok=False, kind="wrong_answer",
                      error=f"wrong answer: {' '.join(text.split())[:80]!r}")
    return record


def probe_all(providers, out_path, clock=time.time):
    """Probe every provider once and append the results to out_path."""
    records = [probe_one(p, clock) for p in providers]
    with open(out_path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return records


def load_records(path):
    records = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{n}: not valid JSON ({exc})")
    return records


def state_of(records):
    """Collapse the probes in one slot into a single state."""
    if not records:
        return "none"
    if all(r["ok"] for r in records):
        return "up"
    if all(r["ok"] or r["kind"] == "rate_limited" for r in records):
        return "limited"
    return "down"


def uptime(records, since):
    window = [r for r in records if r["t"] >= since]
    if not window:
        return None
    return sum(1 for r in window if r["ok"]) / len(window)


def provider_summary(records, now):
    """Numbers for one provider's card."""
    week = [r for r in records if r["t"] >= now - 7 * 86400]
    lat = [r["latency_ms"] for r in week if r["ok"] and r["latency_ms"] is not None]
    kinds = {}
    for r in week:
        if not r["ok"]:
            kinds[r["kind"] or "error"] = kinds.get(r["kind"] or "error", 0) + 1
    first = records[0]["t"] if records else now
    return {
        "days_of_data": (now - first) / 86400,
        "latest": records[-1] if records else None,
        "up_24h": uptime(records, now - 86400),
        "up_7d": uptime(records, now - 7 * 86400),
        "up_30d": uptime(records, now - 30 * 86400),
        "p50": percentile(lat, 50),
        "p95": percentile(lat, 95),
        "probes_7d": len(week),
        "kinds_7d": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
    }


def strip_slots(records, now, slot_hours=2, days=7):
    """Bucket the last `days` into fixed slots, oldest first: [(start, records)]."""
    slot = slot_hours * 3600
    n = days * 24 // slot_hours
    last_start = now - (now % slot)
    starts = [last_start - (n - 1 - i) * slot for i in range(n)]
    buckets = {s: [] for s in starts}
    for r in records:
        start = r["t"] - (r["t"] % slot)
        if start in buckets:
            buckets[start].append(r)
    return [(s, buckets[s]) for s in starts]


def _pct(v):
    return "n/a" if v is None else f"{v:.1%}"


def _ms(v):
    return "n/a" if v is None else f"{v:,.0f} ms"


def _short_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %b %H:%M UTC")


def coverage(days):
    """'3 days', '5 hours', 'under an hour': how much history a window really has."""
    hours = days * 24
    if hours < 1:
        return "under an hour"
    if days < 1:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''}"
    n = int(days)
    return f"{n} day{'s' if n != 1 else ''}"


def _uptime_cell(label, value, window_days, days_of_data):
    note = ""
    if value is not None and days_of_data < window_days * 0.95:
        note = f'<div class="note">{coverage(days_of_data)} of data</div>'
    return (f'<div class="num"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{html.escape(_pct(value))}</div>{note}</div>')


def headline(summaries):
    """One sentence ranking providers by 7-day uptime, or '' if too little data.

    Ties are named together, so four models at 100% aren't reduced to the
    one that happens to sort first.
    """
    ranked = [(s["up_7d"], name) for name, s in summaries.items()
              if s["up_7d"] is not None and s["probes_7d"] >= 6]
    if len(ranked) < 2:
        return ""
    best = max(u for u, _ in ranked)
    worst = min(u for u, _ in ranked)
    if best == worst:
        return f"All {len(ranked)} providers at {best:.1%} uptime over the last 7 days."

    def names(value):
        group = sorted(n for u, n in ranked if u == value)
        if len(group) <= 3:
            return ", ".join(group)
        return f"{len(group)} providers"
    return (f"Most reliable over the last 7 days: {names(best)} ({best:.1%}). "
            f"Least reliable: {names(worst)} ({worst:.1%}).")


def _badge(state, detail="", label=None):
    colour, symbol, default = STATES[state]
    text = (label or default) + (f": {detail}" if detail else "")
    return (f'<span class="badge"><span class="dot" style="background:{colour}">'
            f'{symbol}</span>{html.escape(text)}</span>')


def _cell_title(start, slot_hours, recs):
    a = datetime.fromtimestamp(start, tz=timezone.utc)
    b = a + timedelta(hours=slot_hours)
    when = f"{a:%a %d %b %H:%M} to {b:%H:%M} UTC"
    if not recs:
        return f"{when}: no data"
    parts = []
    for r in recs:
        if r["ok"]:
            parts.append(f"OK, {_ms(r['latency_ms'])}")
        else:
            parts.append(KIND_LABELS.get(r["kind"], r["kind"] or "error"))
    return f"{when}: " + "; ".join(parts)


STATUS_CSS = """
.cards{display:grid;gap:16px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;
padding:16px 18px}
.card-head{display:flex;justify-content:space-between;align-items:baseline;
gap:12px;flex-wrap:wrap}
.card h3{margin:0;font-size:16px}
.model{color:var(--muted);font-size:13px;font-family:ui-monospace,monospace}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:13px;
color:var(--ink2)}
.dot{display:inline-grid;place-items:center;width:16px;height:16px;
border-radius:50%;color:#fff;font-size:11px;font-weight:700;line-height:1}
.nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
gap:8px;margin:14px 0}
.num .label{font-size:12px;color:var(--ink2)}
.num .value{font-size:18px;font-weight:600}
.strip{display:flex;gap:2px;margin-top:6px}
.cell{flex:1;height:26px;border-radius:2px;min-width:2px}
.cell:hover{outline:2px solid var(--ink);outline-offset:1px}
.axis{display:flex;justify-content:space-between;font-size:12px;
color:var(--muted);margin-top:4px}
.kinds{font-size:13px;color:var(--ink2);margin-top:10px}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 0}
.method{color:var(--ink2);font-size:14px}
.method li{margin:4px 0}
a{color:inherit}
.note{font-size:12px;color:var(--muted)}
.headline{font-size:17px;font-weight:600;margin:4px 0 14px}
table.text th,table.text td{text-align:left}
table.text td.err{color:var(--ink2);word-break:break-word}
.snippets code{font-size:12px;word-break:break-all}
"""


def render(records, now=None, title="Free LLM API status", repo_url=None,
           slot_hours=2, days=7, incidents=15, site_url=None):
    now = time.time() if now is None else now
    found = inc.find_incidents(records)
    by_provider = {}
    for r in sorted(records, key=lambda r: r["t"]):
        by_provider.setdefault(r["provider"], []).append(r)

    cards = []
    summaries = {}
    for name in sorted(by_provider):
        recs = by_provider[name]
        s = provider_summary(recs, now)
        latest = s["latest"]
        latest_state = state_of([latest])
        detail = "" if latest["ok"] else KIND_LABELS.get(latest["kind"], "")
        slots = strip_slots(recs, now, slot_hours, days)
        cells = "".join(
            f'<div class="cell" style="background:{STATES[state_of(rs)][0]}" '
            f'title="{html.escape(_cell_title(start, slot_hours, rs))}"></div>'
            for start, rs in slots)
        kinds = ", ".join(f"{KIND_LABELS.get(k, k)} {n}"
                          for k, n in s["kinds_7d"].items()) or "none"
        summaries[name] = s
        d = s["days_of_data"]
        nums = (_uptime_cell("Uptime 24h", s["up_24h"], 1, d)
                + _uptime_cell("Uptime 7d", s["up_7d"], 7, d)
                + _uptime_cell("Uptime 30d", s["up_30d"], 30, d)
                + "".join(
                    f'<div class="num"><div class="label">{html.escape(lbl)}</div>'
                    f'<div class="value">{html.escape(val)}</div></div>'
                    for lbl, val in (("p50 latency 7d", _ms(s["p50"])),
                                     ("p95 latency 7d", _ms(s["p95"])),
                                     ("Probes 7d", f"{s['probes_7d']:,}"))))
        cards.append(
            '<section class="card"><div class="card-head">'
            f'<div><h3>{html.escape(name)}</h3>'
            f'<div class="model">{html.escape(str(latest["model"]))}</div></div>'
            f'<div>Last check {html.escape(_short_time(latest["t"]))}: '
            f'{_badge(latest_state, detail)}</div></div>'
            f'<div class="nums">{nums}</div>'
            f'<div class="strip" role="img" aria-label="Checks for {html.escape(name)}'
            f' over the last {days} days">{cells}</div>'
            f'<div class="axis"><span>{days} days ago</span><span>now</span></div>'
            f'<div class="kinds">Failures in the last 7 days: {html.escape(kinds)}.'
            '</div></section>')
    if not cards:
        cards.append('<p class="muted">No probes recorded yet.</p>')

    legend = "".join(f"<span>{_badge(k)}</span>" for k in ("up", "limited", "down", "none"))

    failures = [r for r in sorted(records, key=lambda r: r["t"]) if not r["ok"]]
    failures = failures[-incidents:][::-1]
    rows = "".join(
        f"<tr><td>{html.escape(_short_time(r['t']))}</td><td>{html.escape(r['provider'])}</td>"
        f"<td>{html.escape(KIND_LABELS.get(r['kind'], r['kind'] or 'error'))}</td>"
        f'<td class="err">{html.escape(r["error"] or "")}</td></tr>'
        for r in failures) or '<tr><td colspan="4" class="muted">No failures recorded.</td></tr>'

    repo = (f' Source and raw data: <a href="{html.escape(repo_url)}">'
            f'{html.escape(repo_url)}</a>.' if repo_url else "")
    updated = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        f"<style>{DASHBOARD_CSS}{STATUS_CSS}</style></head><body><main>"
        f"<h1>{html.escape(title)}</h1>"
        f"<p class=\"sub\">Measured by llmgate. Updated {updated}. "
        f"{len(records):,} probes recorded.{repo}</p>"
        + (f'<p class="headline">{html.escape(headline(summaries))}</p>'
           if headline(summaries) else "")
        + f"<div class=\"legend\">{legend}</div>"
        "<p class=\"sub\" style=\"margin-top:6px\">Each bar is a "
        f"{slot_hours}-hour slot. Hover a bar for the result.</p>"
        f"<div class=\"cards\">{''.join(cards)}</div>"
        + _incidents_section(found, now, repo_url)
        + _follow_section(summaries, site_url, repo_url)
        + "<h2>Recent failures</h2><table class=\"text\"><tr><th>Time</th>"
        "<th>Provider</th><th>Kind</th><th>Error</th></tr>"
        f"{rows}</table>"
        "<h2>How this is measured</h2><ul class=\"method\">"
        f"<li>Every {slot_hours} hours a scheduled GitHub Actions job sends one "
        f"request to each provider: “{html.escape(PROBE_PROMPT)}”.</li>"
        "<li>One attempt per provider, no retries, no fallback. Uptime is "
        "successful probes divided by all probes in the window.</li>"
        "<li>All providers are on their free tier (GitHub Models: its free "
        "usage allowance). Free tiers have lower rate limits and may get lower "
        "priority than paid traffic, so these numbers say nothing about paid "
        "tiers.</li>"
        "<li>Gemma 4 31B is probed twice: direct from Google and through "
        "OpenRouter. Same model, two routes, so the difference is what the "
        "middleman adds.</li>"
        "<li>Probes run from GitHub-hosted runners, so network location is "
        "whatever GitHub assigns. Scheduled runs can be delayed or skipped by "
        "GitHub; a grey bar means no probe ran.</li>"
        "<li>Rate limited (HTTP 429) is shown separately from down, but still "
        "counts against uptime: the request was not served.</li>"
        "<li>Latency is time to a complete response for a one-word answer, "
        "successful probes only.</li>"
        "</ul></main></body></html>")


def _incidents_section(found, now, repo_url, limit=20):
    rows = []
    for i in found[::-1][:limit]:
        state = (_badge("down", label="Ongoing") if i.ongoing
                 else _badge("up", label="Resolved"))
        ended = "" if i.ongoing else inc.fmt_time(i.end)
        rows.append(
            f"<tr><td>{html.escape(i.provider)}</td><td>{state}</td>"
            f"<td>{html.escape(inc.fmt_time(i.start))}</td><td>{html.escape(ended)}</td>"
            f"<td>{html.escape(inc.fmt_duration(i.duration(now)))}</td>"
            f"<td>{html.escape(inc.describe_kinds(i.kinds, KIND_LABELS))}</td></tr>")
    if not rows:
        rows.append('<tr><td colspan="6" class="muted">No incidents yet.</td></tr>')
    issues = (f' Each one is also a <a href="{html.escape(repo_url)}/issues?q=label%3Aincident">'
              "GitHub issue</a>: watch the repo to get notified." if repo_url else "")
    return ("<h2>Incidents</h2>"
            f"<p class=\"sub\">An incident opens after {inc.OPEN_AFTER} failed probes "
            f"in a row and closes after {inc.CLOSE_AFTER} successful ones.{issues}</p>"
            "<table class=\"text\"><tr><th>Provider</th><th>State</th><th>Started</th>"
            "<th>Ended</th><th>Duration</th><th>Failures</th></tr>"
            + "".join(rows) + "</table>")


def _follow_section(summaries, site_url, repo_url):
    base = (site_url or "").rstrip("/") + "/" if site_url else ""
    links = [f'<a href="{html.escape(base)}feed.xml">RSS / Atom feed</a> of incidents',
             f'<a href="{html.escape(base)}status.json">status.json</a> for scripts',
             f'<a href="{html.escape(base)}probes.jsonl">probes.jsonl</a>, every probe ever run']
    snippets = []
    if base:
        for name in sorted(summaries):
            url = f"{base}badges/{inc.slug(name)}.json"
            md = (f"![{name}](https://img.shields.io/endpoint?url={url})")
            snippets.append(f"<li><code>{html.escape(md)}</code></li>")
    badge_html = ("<p class=\"method\">Embed a live uptime badge (Markdown):</p>"
                  f"<ul class=\"method snippets\">{''.join(snippets)}</ul>"
                  if snippets else "")
    return ("<h2>Follow</h2><ul class=\"method\">"
            + "".join(f"<li>{l}</li>" for l in links) + "</ul>" + badge_html)


def write_site(data_path, out_dir, title="Free LLM API status", repo_url=None,
               site_url=None, slot_hours=2, now=None):
    """Write index.html, status.json, feed.xml, badges/*.json and probes.jsonl."""
    now = time.time() if now is None else now
    records = load_records(data_path) if os.path.exists(data_path) else []
    os.makedirs(os.path.join(out_dir, "badges"), exist_ok=True)
    page = render(records, now=now, title=title, repo_url=repo_url,
                  slot_hours=slot_hours, site_url=site_url)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)

    by_provider = {}
    for r in sorted(records, key=lambda r: r["t"]):
        by_provider.setdefault(r["provider"], []).append(r)
    summaries = {n: provider_summary(rs, now) for n, rs in by_provider.items()}
    found = inc.find_incidents(records)
    inc.write_json(os.path.join(out_dir, "status.json"),
                   inc.status_json(summaries, found, now))
    for name, s in summaries.items():
        inc.write_json(os.path.join(out_dir, "badges", f"{inc.slug(name)}.json"),
                       inc.badge(name, s["up_7d"]))
    with open(os.path.join(out_dir, "feed.xml"), "w", encoding="utf-8") as fh:
        fh.write(inc.atom_feed(found, now, site_url, title, KIND_LABELS))
    src = os.path.abspath(data_path)
    dst = os.path.abspath(os.path.join(out_dir, "probes.jsonl"))
    if records and src != dst:
        with open(src, encoding="utf-8") as a, open(dst, "w", encoding="utf-8") as b:
            b.write(a.read())
    return len(records)


def write_page(data_path, out_path, **kw):
    records = load_records(data_path)
    page = render(records, **kw)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(records)
