"""Incidents, badges, feed and status.json for the status board.

An incident opens when a provider fails `open_after` probes in a row and
closes when it then succeeds `close_after` probes in a row. Requiring a streak
both ways keeps one blip, or a provider flapping between up and down, from
opening and closing incidents every two hours.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

OPEN_AFTER = 2
CLOSE_AFTER = 2


@dataclass
class Incident:
    provider: str
    model: str
    start: float            # time of the first failed probe in the streak
    end: float = None        # time of the first successful probe in the recovery streak
    failed_probes: int = 0
    kinds: dict = field(default_factory=dict)

    @property
    def ongoing(self):
        return self.end is None

    def duration(self, now):
        return (self.end if self.end is not None else now) - self.start

    def key(self):
        return f"{self.provider}@{int(self.start)}"


def find_incidents(records, open_after=OPEN_AFTER, close_after=CLOSE_AFTER):
    """Walk each provider's probes in time order and return all incidents."""
    by_provider = {}
    for r in sorted(records, key=lambda r: r["t"]):
        by_provider.setdefault(r["provider"], []).append(r)

    incidents = []
    for provider, recs in sorted(by_provider.items()):
        current = None
        fail_streak = []
        ok_streak = []
        for r in recs:
            if r["ok"]:
                fail_streak = []
                ok_streak.append(r)
                if current and len(ok_streak) >= close_after:
                    current.end = ok_streak[0]["t"]
                    incidents.append(current)
                    current = None
                continue
            ok_streak = []
            fail_streak.append(r)
            kind = r.get("kind") or "error"
            if current:
                current.failed_probes += 1
                current.kinds[kind] = current.kinds.get(kind, 0) + 1
            elif len(fail_streak) >= open_after:
                current = Incident(provider, r.get("model", ""), fail_streak[0]["t"])
                for f in fail_streak:
                    k = f.get("kind") or "error"
                    current.failed_probes += 1
                    current.kinds[k] = current.kinds.get(k, 0) + 1
        if current:
            incidents.append(current)
    incidents.sort(key=lambda i: i.start)
    return incidents


def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%-d %b %H:%M UTC")


def fmt_duration(seconds):
    minutes = int(seconds // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def describe_kinds(kinds, labels):
    return ", ".join(f"{labels.get(k, k)} {n}"
                     for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))


# ---- badges ---------------------------------------------------------------

def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def badge_colour(uptime):
    if uptime is None:
        return "lightgrey"
    if uptime >= 0.99:
        return "brightgreen"
    if uptime >= 0.95:
        return "green"
    if uptime >= 0.90:
        return "yellowgreen"
    if uptime >= 0.75:
        return "orange"
    return "red"


def badge(name, uptime):
    """A shields.io endpoint badge (https://shields.io/badges/endpoint-badge)."""
    message = "no data" if uptime is None else f"{uptime:.1%} uptime 7d"
    return {"schemaVersion": 1, "label": name, "message": message,
            "color": badge_colour(uptime)}


# ---- status.json and feed -------------------------------------------------

def status_json(summaries, incidents, now):
    def iso(ts):
        return None if ts is None else datetime.fromtimestamp(
            ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    providers = {}
    for name, s in summaries.items():
        latest = s["latest"]
        providers[name] = {
            "model": latest.get("model"),
            "last_check": latest["ts"],
            "last_ok": bool(latest["ok"]),
            "uptime_24h": s["up_24h"], "uptime_7d": s["up_7d"],
            "uptime_30d": s["up_30d"],
            "p50_ms": s["p50"], "p95_ms": s["p95"],
            "probes_7d": s["probes_7d"],
            "open_incident": any(i.ongoing and i.provider == name for i in incidents),
        }
    return {
        "updated": iso(now),
        "providers": providers,
        "incidents": [{"provider": i.provider, "model": i.model,
                       "start": iso(i.start), "end": iso(i.end),
                       "ongoing": i.ongoing, "failed_probes": i.failed_probes,
                       "kinds": i.kinds} for i in incidents[::-1]],
    }


def atom_feed(incidents, now, site_url, title, labels, limit=50):
    """Atom feed with one entry per incident, newest first."""
    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = site_url.rstrip("/") + "/" if site_url else ""
    entries = []
    for i in incidents[::-1][:limit]:
        if i.ongoing:
            heading = f"Down: {i.provider} since {fmt_time(i.start)}"
            updated = i.start
        else:
            heading = (f"Resolved: {i.provider} was down for "
                       f"{fmt_duration(i.duration(now))}")
            updated = i.end
        summary = (f"{i.provider} ({i.model}) failed {i.failed_probes} probes in a row "
                   f"from {fmt_time(i.start)}"
                   + ("." if i.ongoing else f" until {fmt_time(i.end)}.")
                   + f" Failures: {describe_kinds(i.kinds, labels)}.")
        entries.append(
            "<entry>"
            f"<id>urn:llmgate:incident:{xml_escape(i.key())}"
            f"{'' if i.ongoing else ':resolved'}</id>"
            f"<title>{xml_escape(heading)}</title>"
            f"<updated>{iso(updated)}</updated>"
            + (f'<link href="{xml_escape(base)}"/>' if base else "")
            + f"<summary>{xml_escape(summary)}</summary>"
            "</entry>")
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            f"<title>{xml_escape(title)}: incidents</title>"
            f"<id>{xml_escape(base or 'urn:llmgate:status')}</id>"
            + (f'<link href="{xml_escape(base)}"/>'
               f'<link rel="self" href="{xml_escape(base)}feed.xml"/>' if base else "")
            + f"<updated>{iso(now)}</updated>"
            + "".join(entries) + "</feed>")


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
