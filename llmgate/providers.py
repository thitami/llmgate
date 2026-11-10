"""Providers: the things the gateway can call.

Stdlib only. MockProvider runs fully offline. AnthropicProvider and
OpenAICompatProvider talk to real APIs through urllib and have NOT been
tested against live endpoints yet.

Prices are per 1,000,000 tokens. For real providers you must fill them in
yourself from the provider's pricing page; the loader refuses to guess.
"""

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import __version__

TIERS = ("cheap", "strong")
USER_AGENT = f"llmgate/{__version__} (+https://github.com/thitami/llmgate)"


ERROR_KINDS = ("rate_limited", "server_error", "timeout", "network", "auth",
               "client_error", "bad_response", "config", "error")


class ProviderError(Exception):
    """A provider call failed. latency_ms is set when the time is known.

    retryable=False tells the gateway not to retry this provider and to move
    straight to the next one (rate limits, timeouts, missing keys, bad requests).
    kind is one of ERROR_KINDS, used by the status board to group failures.
    status is the HTTP status code when there was one.
    """

    def __init__(self, message, latency_ms=None, retryable=True, kind="error",
                 status=None):
        super().__init__(message)
        self.latency_ms = latency_ms
        self.retryable = retryable
        self.kind = kind
        self.status = status


def http_error_kind(code):
    if code == 429:
        return "rate_limited"
    if code in (401, 403):
        return "auth"
    if code >= 500:
        return "server_error"
    return "client_error"


def estimate_tokens(text):
    """Rough token estimate for the mock provider (about 1.3 tokens per word)."""
    return max(1, int(len(text.split()) * 1.3))


@dataclass
class Response:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: float


class Provider:
    """Base class. Subclasses implement complete()."""

    def __init__(self, name, tier, price_in, price_out, priority=0):
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")
        self.name = name
        self.tier = tier
        self.price_in = price_in  # per 1M input tokens
        self.price_out = price_out  # per 1M output tokens
        self.priority = priority  # lower is tried first within a tier

    def cost(self, tokens_in, tokens_out):
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000

    def complete(self, prompt, system=None):
        raise NotImplementedError


class MockProvider(Provider):
    """Offline provider with canned answers, seeded failures and fake latency.

    answers: {substring: reply}. The first key found in the prompt
    (case-insensitive) wins; otherwise `default` is returned.
    Latency is simulated (reported, not slept) so runs stay fast.
    """

    def __init__(self, name, tier, price_in, price_out, answers=None,
                 default="I don't know.", fail_rate=0.0, seed=0, latency_ms=100,
                 priority=0):
        super().__init__(name, tier, price_in, price_out, priority)
        self.answers = dict(answers or {})
        self.default = default
        self.fail_rate = fail_rate
        self.latency_ms = latency_ms
        self._rng = random.Random(seed)

    def complete(self, prompt, system=None):
        failed = self._rng.random() < self.fail_rate
        latency = self.latency_ms * (0.8 + 0.4 * self._rng.random())
        if failed:
            raise ProviderError("simulated outage", latency_ms=latency,
                                kind="server_error")
        lowered = prompt.lower()
        text = self.default
        for key, reply in self.answers.items():
            if key.lower() in lowered:
                text = reply
                break
        return Response(text, estimate_tokens(prompt), estimate_tokens(text), latency)


PLAIN_TEXT = "__plain_text__"


def _post_json(url, headers, body, timeout, allow_plain_text=False):
    """POST JSON, return (parsed_json, latency_ms). Raises ProviderError.

    allow_plain_text: a 2xx text/plain body is returned as {PLAIN_TEXT: body}
    instead of failing. GitHub Models has answered that way in production.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "application/json")
    # Some providers sit behind Cloudflare, which rejects urllib's default
    # "Python-urllib/x.y" agent with HTTP 403 (error code 1010).
    req.add_header("user-agent", USER_AGENT)
    for key, value in headers.items():
        req.add_header(key, value)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            ctype = resp.headers.get("content-type", "")
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        latency = (time.perf_counter() - start) * 1000
        detail = " ".join(exc.read().decode("utf-8", "replace").split())[:200]
        # 429 = rate limited: retrying immediately just burns more quota.
        # Other 4xx are our fault (bad model id, bad request): retrying won't help.
        # 5xx are worth one retry.
        retryable = exc.code >= 500
        label = "rate limited (HTTP 429)" if exc.code == 429 else f"HTTP {exc.code}"
        raise ProviderError(f"{label}: {detail}", latency_ms=latency,
                            retryable=retryable, kind=http_error_kind(exc.code),
                            status=exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        latency = (time.perf_counter() - start) * 1000
        # A timeout has already used the provider's whole time budget; retrying
        # would double the caller's wait, so go straight to the next provider.
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        raise ProviderError(f"{type(exc).__name__}: {exc}", latency_ms=latency,
                            retryable=not timed_out,
                            kind="timeout" if timed_out else "network")
    latency = (time.perf_counter() - start) * 1000
    if (allow_plain_text and ctype.lower().startswith("text/plain")
            and raw.strip()):
        return {PLAIN_TEXT: raw.decode("utf-8", "replace").strip()}, latency
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        # A 2xx that isn't JSON: say exactly what came back, so the cause
        # (a redirect to a web page, an empty body, a proxy page) is visible.
        # urllib turns a redirected POST into a GET, which often lands on HTML.
        snippet = " ".join(raw[:150].decode("utf-8", "replace").split()) or "(empty body)"
        moved = f", redirected to {final_url}" if final_url != url else ""
        raise ProviderError(
            f"HTTP {status} but not JSON (content-type {ctype or 'none'}{moved}): {snippet}",
            latency_ms=latency, retryable=False, kind="bad_response", status=status)
    return payload, latency


class AnthropicProvider(Provider):
    """Anthropic Messages API. Untested against the live API."""

    def __init__(self, name, tier, price_in, price_out, model, api_key_env,
                 max_tokens=1024, timeout=30, priority=0,
                 base_url="https://api.anthropic.com"):
        super().__init__(name, tier, price_in, price_out, priority)
        self.model = model
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    def complete(self, prompt, system=None):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderError(f"env var {self.api_key_env} is not set",
                                retryable=False, kind="config")
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        payload, latency = _post_json(f"{self.base_url}/v1/messages", headers,
                                      body, self.timeout)
        try:
            text = "".join(b.get("text", "") for b in payload["content"]
                           if b.get("type") == "text")
            usage = payload["usage"]
            return Response(text, int(usage["input_tokens"]),
                            int(usage["output_tokens"]), latency)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}",
                                latency_ms=latency, kind="bad_response")


class OpenAICompatProvider(Provider):
    """Any OpenAI-style /chat/completions endpoint (OpenAI, Groq, Gemini's
    compatibility endpoint, GitHub Models, OpenRouter, Ollama)."""

    def __init__(self, name, tier, price_in, price_out, model, api_key_env,
                 base_url="https://api.openai.com/v1", max_tokens=1024, timeout=30, priority=0):
        super().__init__(name, tier, price_in, price_out, priority)
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout

    def complete(self, prompt, system=None):
        headers = {}
        if self.api_key_env:  # local servers like Ollama need no key
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ProviderError(
                    f"env var {self.api_key_env} is not set",
                    retryable=False, kind="config")
            headers["authorization"] = f"Bearer {key}"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": self.model, "messages": messages,
                "max_tokens": self.max_tokens}
        payload, latency = _post_json(f"{self.base_url}/chat/completions", headers,
                                      body, self.timeout, allow_plain_text=True)
        try:
            if PLAIN_TEXT in payload:  # answer sent as text/plain: no usage block
                text = payload[PLAIN_TEXT]
                return Response(text, estimate_tokens(prompt), estimate_tokens(text),
                                latency)
            text = payload["choices"][0]["message"]["content"] or ""
            usage = payload["usage"]
            return Response(text, int(usage["prompt_tokens"]),
                            int(usage["completion_tokens"]), latency)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(f"unexpected response shape: {exc}",
                                latency_ms=latency, kind="bad_response")


class ConfigError(Exception):
    pass


def is_skipped(spec, environ=None):
    """True if an optional provider has no API key set, so it should be left out.

    Lets you keep paid providers in a config without paying for them yet:
    set "optional": true and they switch on once the key exists.
    """
    environ = os.environ if environ is None else environ
    key_env = spec.get("api_key_env")
    return bool(spec.get("optional")) and bool(key_env) and not environ.get(key_env)


def build_providers(specs, environ=None, log=None):
    """Build all providers, leaving out optional ones whose key is missing."""
    built = []
    for spec in specs:
        if is_skipped(spec, environ):
            if log:
                log(f"note: skipping optional provider {spec.get('name')!r} "
                    f"({spec.get('api_key_env')} not set)")
            continue
        built.append(build_provider(spec))
    return built


def build_provider(spec):
    """Build a provider from a config dict."""
    kind = spec.get("type")
    common = {}
    for field in ("name", "tier", "price_in", "price_out"):
        if spec.get(field) is None:
            raise ConfigError(
                f"provider {spec.get('name', '?')!r}: {field!r} is required"
                + (" (fill it in from the provider's pricing page)"
                   if field.startswith("price") else ""))
        common[field] = spec[field]
    common["priority"] = spec.get("priority", 0)
    if kind == "mock":
        return MockProvider(
            **common, answers=spec.get("answers"),
            default=spec.get("default", "I don't know."),
            fail_rate=spec.get("fail_rate", 0.0), seed=spec.get("seed", 0),
            latency_ms=spec.get("latency_ms", 100))
    if kind in ("anthropic", "openai"):
        required = ("model", "api_key_env") if kind == "anthropic" else ("model",)
        for field in required:
            if not spec.get(field):
                raise ConfigError(f"provider {spec['name']!r}: {field!r} is required")
        extra = {k: spec[k] for k in ("max_tokens", "timeout", "base_url") if k in spec}
        cls = AnthropicProvider if kind == "anthropic" else OpenAICompatProvider
        return cls(**common, model=spec["model"], api_key_env=spec.get("api_key_env"),
                   **extra)
    raise ConfigError(f"unknown provider type {kind!r}")
