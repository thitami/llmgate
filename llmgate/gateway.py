"""The gateway: tier routing, retry once, fallback chain, daily budget, logging."""

import uuid
from dataclasses import dataclass, field

from .providers import TIERS, ProviderError


class BudgetExceeded(Exception):
    """Today's logged spend has reached the daily budget."""


class AllProvidersFailed(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("all providers failed: " + "; ".join(errors))


@dataclass
class GatewayResult:
    text: str
    provider: str
    tier: str
    tokens_in: int
    tokens_out: int
    cost: float
    latency_ms: float
    fallback_from: str = None
    attempts: int = 1
    errors: list = field(default_factory=list)
    request_id: str = None
    skipped: list = field(default_factory=list)  # providers with an open breaker


class Gateway:
    """Routes a prompt to a provider.

    Order: providers of the requested tier first, then the other tier. Within
    a tier, lower `priority` goes first, then the cheapest. Each provider gets
    1 + retries attempts before the gateway falls through to the next, except
    for non-retryable errors (rate limits, missing keys) which skip straight on.

    Circuit breaker: if a provider's last `breaker_threshold` attempts all
    failed and the latest failure is less than `breaker_cooldown` seconds old,
    the provider is skipped without being called. Once the cooldown passes it
    gets one trial request: success closes the breaker, failure reopens it.
    The state is read from the call log, so it holds across processes (every
    `llmgate ask` is a new process). Set breaker_threshold=None to disable.
    If every provider's breaker is open, they are all tried anyway, because
    an answer that might fail beats a guaranteed error.

    The budget is a soft cap: it is checked before every attempt, so one call
    can overshoot it by its own cost. Failed attempts are logged with cost 0
    (providers don't bill for errors). All attempts for one ask() share a
    request_id, so the dashboard can report what the caller experienced.
    """

    def __init__(self, providers, store, daily_budget=None, retries=1,
                 breaker_threshold=3, breaker_cooldown=60.0):
        if not providers:
            raise ValueError("gateway needs at least one provider")
        self.providers = list(providers)
        self.store = store
        self.daily_budget = daily_budget
        self.retries = retries
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = breaker_cooldown

    def breaker_open(self, provider):
        if not self.breaker_threshold:
            return False
        recent = self.store.recent_attempts(provider.name, self.breaker_threshold)
        if len(recent) < self.breaker_threshold or any(ok for ok, _ in recent):
            return False
        last_failure_ts = recent[0][1]
        return self.store.clock() - last_failure_ts < self.breaker_cooldown

    def order(self, tier):
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
        tiers = [tier] + [t for t in TIERS if t != tier]
        chain = []
        for t in tiers:
            group = [p for p in self.providers if p.tier == t]
            group.sort(key=lambda p: (p.priority, p.price_in + p.price_out, p.name))
            chain.extend(group)
        return chain

    def _check_budget(self):
        if self.daily_budget is None:
            return
        spent = self.store.spend_today()
        if spent >= self.daily_budget:
            raise BudgetExceeded(
                f"daily budget {self.daily_budget:.4f} reached (spent {spent:.4f})")

    @staticmethod
    def _skipped_before(chain, skipped, served):
        """Last skipped provider ranked ahead of the one that served, if any."""
        ahead = [p.name for p in chain[:chain.index(served)] if p.name in skipped]
        return ahead[-1] if ahead else None

    def ask(self, prompt, tier="cheap", feature="default", system=None):
        request_id = uuid.uuid4().hex
        chain = self.order(tier)
        live = [p for p in chain if not self.breaker_open(p)]
        skipped = [p.name for p in chain if p not in live]
        if not live:
            live, skipped = chain, []
        errors = [f"{name}: circuit open, skipped" for name in skipped]
        previous = None  # last provider that was exhausted
        attempts = 0
        for provider in live:
            for _ in range(1 + self.retries):
                self._check_budget()
                attempts += 1
                try:
                    resp = provider.complete(prompt, system=system)
                except ProviderError as exc:
                    errors.append(f"{provider.name}: {exc}")
                    self.store.record(
                        provider.name, ok=False, error=str(exc),
                        latency_ms=exc.latency_ms, feature=feature,
                        fallback_from=previous, request_id=request_id)
                    if not getattr(exc, "retryable", True):
                        break  # rate limited or misconfigured: next provider
                    continue
                cost = provider.cost(resp.tokens_in, resp.tokens_out)
                fell_back = previous or self._skipped_before(chain, skipped, provider)
                self.store.record(
                    provider.name, ok=True, tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out, cost=cost,
                    latency_ms=resp.latency_ms, feature=feature,
                    fallback_from=fell_back, request_id=request_id)
                return GatewayResult(
                    resp.text, provider.name, provider.tier, resp.tokens_in,
                    resp.tokens_out, cost, resp.latency_ms,
                    fallback_from=fell_back, attempts=attempts, errors=errors, request_id=request_id,
                    skipped=skipped)
            previous = provider.name
        raise AllProvidersFailed(errors)
