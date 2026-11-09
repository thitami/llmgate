"""The gateway: tier routing, retry once, fallback chain, daily budget, logging."""

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


class Gateway:
    """Routes a prompt to a provider.

    Order: providers of the requested tier first, then the other tier. Within
    a tier, lower `priority` goes first, then the cheapest. Each provider gets
    1 + retries attempts before the gateway falls through to the next, except
    for non-retryable errors (rate limits, missing keys) which skip straight on. The budget is a soft cap: it is
    checked before every attempt, so one call can overshoot it by its own cost.
    Failed attempts are logged with cost 0 (providers don't bill for errors).
    """

    def __init__(self, providers, store, daily_budget=None, retries=1):
        if not providers:
            raise ValueError("gateway needs at least one provider")
        self.providers = list(providers)
        self.store = store
        self.daily_budget = daily_budget
        self.retries = retries

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

    def ask(self, prompt, tier="cheap", feature="default", system=None):
        errors = []
        previous = None  # last provider that was exhausted
        attempts = 0
        for provider in self.order(tier):
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
                        fallback_from=previous)
                    if not getattr(exc, "retryable", True):
                        break  # rate limited or misconfigured: next provider
                    continue
                cost = provider.cost(resp.tokens_in, resp.tokens_out)
                self.store.record(
                    provider.name, ok=True, tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out, cost=cost,
                    latency_ms=resp.latency_ms, feature=feature,
                    fallback_from=previous)
                return GatewayResult(
                    resp.text, provider.name, provider.tier, resp.tokens_in,
                    resp.tokens_out, cost, resp.latency_ms,
                    fallback_from=previous, attempts=attempts, errors=errors)
            previous = provider.name
        raise AllProvidersFailed(errors)
