"""Eval pipeline: JSON test cases, deterministic checks, baseline comparison.

A case passes only if all of its checks pass. The score is passed / total.
A regression is a case that passed in the baseline and fails now, or a score
below the baseline score. The CLI turns a regression into a non-zero exit.
"""

import json
import re
from dataclasses import dataclass, field

CHECK_TYPES = ("equals", "contains", "not_contains", "regex", "max_words")


def run_check(check, output):
    """Return (passed, detail) for one check dict against the output text."""
    kind = check.get("type")
    value = check.get("value")
    if kind not in CHECK_TYPES:
        raise ValueError(f"unknown check type {kind!r}; use one of {CHECK_TYPES}")
    if value is None:
        raise ValueError(f"check {kind!r} needs a 'value'")
    ignore_case = bool(check.get("ignore_case", False))
    text = output.strip()
    hay = text.lower() if ignore_case else text
    needle = str(value).lower() if ignore_case else str(value)

    if kind == "equals":
        ok = hay == needle.strip()
        return ok, f"equals {value!r}"
    if kind == "contains":
        return needle in hay, f"contains {value!r}"
    if kind == "not_contains":
        return needle not in hay, f"not_contains {value!r}"
    if kind == "regex":
        flags = re.IGNORECASE if ignore_case else 0
        return re.search(str(value), text, flags) is not None, f"regex {value!r}"
    # max_words
    return len(text.split()) <= int(value), f"max_words {value}"


@dataclass
class CaseResult:
    id: str
    passed: bool
    provider: str = ""
    failures: list = field(default_factory=list)
    error: str = ""


@dataclass
class EvalReport:
    results: list

    @property
    def score(self):
        if not self.results:
            return 0.0
        return sum(r.passed for r in self.results) / len(self.results)

    def to_baseline(self):
        return {"score": self.score,
                "cases": {r.id: r.passed for r in self.results}}


def load_cases(path):
    with open(path, encoding="utf-8") as fh:
        cases = json.load(fh)
    seen = set()
    for case in cases:
        for key in ("id", "prompt", "checks"):
            if key not in case:
                raise ValueError(f"eval case missing {key!r}: {case}")
        if case["id"] in seen:
            raise ValueError(f"duplicate eval case id {case['id']!r}")
        seen.add(case["id"])
    return cases


def run_eval(gateway, cases):
    """Run every case through the gateway and apply its checks."""
    from .gateway import AllProvidersFailed, BudgetExceeded

    results = []
    for case in cases:
        try:
            res = gateway.ask(case["prompt"], tier=case.get("tier", "cheap"),
                              feature="eval", system=case.get("system"))
        except (AllProvidersFailed, BudgetExceeded) as exc:
            results.append(CaseResult(case["id"], False, error=str(exc)))
            continue
        failures = []
        for check in case["checks"]:
            ok, detail = run_check(check, res.text)
            if not ok:
                failures.append(detail)
        results.append(CaseResult(case["id"], not failures, provider=res.provider,
                                  failures=failures))
    return EvalReport(results)


def compare(report, baseline):
    """Return a list of human-readable regression messages (empty = no regression)."""
    problems = []
    base_cases = baseline.get("cases", {})
    now = {r.id: r for r in report.results}
    for case_id, was_passing in base_cases.items():
        if not was_passing:
            continue
        if case_id not in now:
            problems.append(f"{case_id}: passed in baseline but was not run")
        elif not now[case_id].passed:
            r = now[case_id]
            why = r.error or "; ".join(r.failures)
            problems.append(f"{case_id}: passed in baseline, now fails ({why})")
    base_score = baseline.get("score", 0.0)
    if report.score < base_score - 1e-9:
        problems.append(f"score dropped {base_score:.2f} -> {report.score:.2f}")
    return problems


def load_baseline(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_baseline(path, report):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_baseline(), fh, indent=2, sort_keys=True)
        fh.write("\n")
