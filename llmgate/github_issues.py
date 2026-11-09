"""Mirror status-board incidents as GitHub issues.

One open issue per ongoing incident, labelled `incident`. When the incident
resolves, the issue gets a comment with the duration and is closed. People who
watch the repo get notified both times, and the issue list is the public
incident history. Each issue carries a hidden marker naming its provider, so
the sync is idempotent: running it twice does nothing the second time.
"""

import json
import re
import urllib.error
import urllib.request

from . import __version__
from .incidents import describe_kinds, fmt_duration, fmt_time

LABEL = "incident"
MARKER = re.compile(r"<!-- llmgate-incident provider=\"(.+?)\" -->")


class GitHubError(Exception):
    pass


class GitHubClient:
    """Minimal REST client for the four calls the sync needs."""

    def __init__(self, repo, token, api="https://api.github.com", timeout=20):
        self.repo = repo
        self.token = token
        self.api = api.rstrip("/")
        self.timeout = timeout

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.api}/repos/{self.repo}{path}",
                                     data=data, method=method)
        req.add_header("accept", "application/vnd.github+json")
        req.add_header("authorization", f"Bearer {self.token}")
        req.add_header("x-github-api-version", "2022-11-28")
        req.add_header("user-agent", f"llmgate/{__version__}")
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = " ".join(exc.read().decode("utf-8", "replace").split())[:300]
            raise GitHubError(f"{method} {path}: HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError(f"{method} {path}: {exc}")
        return json.loads(raw) if raw else None

    def ensure_label(self):
        try:
            self._call("POST", "/labels", {"name": LABEL, "color": "d03b3b",
                                           "description": "Provider outage seen by the status board"})
        except GitHubError as exc:
            if "HTTP 422" not in str(exc):  # 422 = label already exists
                raise

    def open_issues(self):
        issues = self._call("GET", f"/issues?state=open&labels={LABEL}&per_page=100")
        return [i for i in issues if "pull_request" not in i]

    def create_issue(self, title, body):
        return self._call("POST", "/issues", {"title": title, "body": body,
                                              "labels": [LABEL]})

    def comment(self, number, body):
        self._call("POST", f"/issues/{number}/comments", {"body": body})

    def close(self, number):
        self._call("PATCH", f"/issues/{number}", {"state": "closed",
                                                  "state_reason": "completed"})


def issue_title(incident):
    return f"{incident.provider} is down"


def issue_body(incident, labels, page_url=None):
    lines = [
        f"<!-- llmgate-incident provider=\"{incident.provider}\" -->",
        f"**{incident.provider}** (`{incident.model}`) has failed "
        f"{incident.failed_probes} probes in a row since {fmt_time(incident.start)}.",
        "",
        f"Failures so far: {describe_kinds(incident.kinds, labels)}.",
        "",
        "This issue was opened by the status board and will be closed "
        "automatically once the provider passes two probes in a row.",
    ]
    if page_url:
        lines += ["", f"Live status: {page_url}"]
    return "\n".join(lines)


def resolved_comment(incident, now, labels):
    return (f"Recovered at {fmt_time(incident.end)}. Down for "
            f"{fmt_duration(incident.duration(now))}, {incident.failed_probes} "
            f"failed probes ({describe_kinds(incident.kinds, labels)}).")


def sync(client, incidents, now, labels, page_url=None, dry_run=False, log=print):
    """Open issues for ongoing incidents, close issues whose incident ended.

    Returns a list of (action, provider) tuples, for logging and tests.
    """
    ongoing = {i.provider: i for i in incidents if i.ongoing}
    # latest resolved incident per provider, to explain a close
    resolved = {}
    for i in incidents:
        if not i.ongoing:
            resolved[i.provider] = i

    if not dry_run:
        client.ensure_label()
    issues = {}
    for issue in client.open_issues():
        m = MARKER.search(issue.get("body") or "")
        if m:
            issues[m.group(1)] = issue

    actions = []
    for provider, incident in sorted(ongoing.items()):
        if provider in issues:
            continue
        actions.append(("open", provider))
        log(f"open issue: {issue_title(incident)}")
        if not dry_run:
            client.create_issue(issue_title(incident),
                                issue_body(incident, labels, page_url))

    for provider, issue in sorted(issues.items()):
        if provider in ongoing:
            continue
        actions.append(("close", provider))
        log(f"close issue #{issue['number']}: {provider} recovered")
        if dry_run:
            continue
        if provider in resolved:
            client.comment(issue["number"], resolved_comment(resolved[provider], now, labels))
        else:
            client.comment(issue["number"], "No longer failing (or no longer probed).")
        client.close(issue["number"])
    if not actions:
        log("incident issues already up to date")
    return actions
