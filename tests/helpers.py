import os

from llmgate.providers import MockProvider, Provider, ProviderError, Response

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path(*parts):
    return os.path.join(ROOT, *parts)


class ScriptedProvider(Provider):
    """Returns scripted outcomes in order: a string is a reply, an Exception is raised."""

    def __init__(self, name, tier, outcomes, price_in=10, price_out=10):
        super().__init__(name, tier, price_in, price_out)
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, prompt, system=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome, 100, 100, 50.0)


def boom(msg="boom"):
    return ProviderError(msg, latency_ms=5.0)


def mock(name, tier, **kw):
    return MockProvider(name, tier, kw.pop("price_in", 10), kw.pop("price_out", 10), **kw)
