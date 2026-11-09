# llmgate

A small LLM gateway with an eval gate and a dashboard. Python standard library only, and it runs fully offline with mock providers, so you can try everything without an API key.

It has three pieces that work together:

1. **Gateway** routes each request to a cheap or strong model, retries once, falls back to the next provider when one fails, stops at a daily budget, and logs every call.
2. **Eval pipeline** runs JSON test cases with deterministic checks and compares the result to a stored baseline. If something that used to pass now fails, it exits non-zero so CI fails the build.
3. **Dashboard** turns the call log into one static HTML page: calls, failure rate, fallbacks, tokens, cost, p50/p95 latency, by provider and by feature, plus recent failures.

## Try it

Needs Python 3.10+. Run everything from the repo root.

```
python -m unittest discover -s tests -t . -v     # 69 tests
python -m llmgate demo                            # offline demo, writes demo_out/dashboard.html
python -m llmgate eval                            # passes, exit 0
python -m llmgate eval --config config.regressed.json   # score 1.0 -> 0.5, exit 1
python -m llmgate ask "What is 2+2? Reply with just the number."
python -m llmgate dashboard --db llmgate.db --out dashboard.html
```

The demo makes the cheap mock provider fail 35% of the time so you can see retries, fallbacks and failures on the dashboard, then hits the daily budget and stops. The last step runs the eval with a deliberately broken cheap model and shows the CI gate failing.

## How each piece works

### Gateway (`llmgate/gateway.py`)

- **Tiers.** Every provider is `cheap` or `strong`. A request asks for a tier. Providers of that tier are tried first, cheapest first (input price plus output price), then the other tier.
- **Retry and fallback.** Each provider gets one retry. If it still fails, the gateway moves to the next provider. Rate limits (429), missing keys and other 4xx errors skip the retry and fall back straight away. If every provider fails you get `AllProvidersFailed`.
- **Timeouts.** A timeout is not retried: the provider already used its whole time budget, and a retry would double the caller's wait. Set `"timeout"` (seconds) per provider; the default is 30.
- **Circuit breaker.** If a provider's last 3 attempts all failed and the latest failure is under 60 seconds old, the gateway skips it without calling it. After the cooldown it gets one trial request: success closes the breaker, failure reopens it. The state is read from the call log, so it works across processes (every `llmgate ask` is a new one). If every provider's breaker is open, they are all tried anyway. Tune it with `"circuit_breaker": {"threshold": 3, "cooldown_seconds": 60}` in the config, or `"threshold": null` to turn it off.
- **Priority.** An optional `priority` per provider (lower first, default 0) beats price within a tier. Useful for a free local model that should only be a last resort.
- **Budget.** Before every attempt the gateway checks today's logged spend (UTC day). At or over the budget it raises `BudgetExceeded`. It is a soft cap: the call that crosses the line still completes, because the cost is only known afterwards.
- **Logging.** Every attempt, including failed ones, is a row in SQLite (`llmgate/store.py`): time, provider, ok, error, tokens, cost, latency, feature, and which provider it fell back from. Failed attempts cost 0.

### Providers (`llmgate/providers.py`)

- `MockProvider`: canned answers, seeded random failures, simulated latency. Used by the tests, the demo and CI.
- `AnthropicProvider` and `OpenAICompatProvider`: plain `urllib` calls. `OpenAICompatProvider` works with any OpenAI-style endpoint (OpenAI, Groq, Gemini's compatibility endpoint, Ollama). The tests run both real adapters against a local fake server, including 429, 5xx and timeout responses. `OpenAICompatProvider` has also served live traffic on Groq; Gemini's endpoint was reachable but only returned 503s during testing. `AnthropicProvider` has not been run against the live API yet.

**Live testing so far is limited** (see above). Check an adapter with a real key before you trust it. Prices are per 1M tokens and you must fill them in yourself from the provider's pricing page (`config.example.json`); the loader refuses to start with a missing price. The prices in `config.mock.json` are arbitrary numbers, not real ones.

### Running on free tiers (no card)

`config.free.json` wires up three providers that cost nothing:

- **Groq** (cheap tier): free tier, OpenAI-compatible endpoint.
- **Gemini Flash** via Google AI Studio (strong tier): free tier, OpenAI-compatible endpoint.
- **Ollama** on your own machine (last resort): a small model on CPU, no key, never rate limited.

```
export GROQ_API_KEY=...
export GEMINI_API_KEY=...
ollama pull <small model> && ollama serve
python -m llmgate ask "What is 2+2?" --config config.free.json
```

OpenAI and Anthropic are in the same file with `"optional": true`. Without their API key they are skipped at startup (one note on stderr), so they cost nothing until you set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. Neither has a lasting free API tier, so expect to pay for real calls once they are on. Any provider can be marked optional this way.

Fill in the model IDs and prices first (see the note in the file). Free tiers return HTTP 429 when you hit their limits. The gateway treats a 429 as non-retryable and moves straight to the next provider, because retrying a rate limit immediately only burns more quota. A missing API key or another 4xx is handled the same way. 5xx errors and timeouts get the normal single retry. Free tiers may use your prompts to improve their models, so only send test data.

### Eval pipeline (`llmgate/evals.py`)

Test cases live in `evals/cases.json`:

```json
{"id": "capital", "prompt": "What is the capital of France?", "tier": "cheap",
 "checks": [{"type": "contains", "value": "Paris"},
            {"type": "not_contains", "value": "London"}]}
```

Checks: `equals`, `contains`, `not_contains`, `regex`, `max_words`. `contains`, `not_contains`, `equals` and `regex` accept `"ignore_case": true`. A case passes when all its checks pass. The score is passed cases divided by total cases.

`evals/baseline.json` stores the last known good result. A run counts as a regression if a case that passed in the baseline now fails, or the score drops. Exit codes: 0 fine, 1 regression, 2 setup problem (missing baseline, bad config).

After an intentional change, accept the new result with:

```
python -m llmgate eval --update-baseline
```

`.github/workflows/eval.yml` runs the unit tests and the eval on every pull request.

**Limits to know about.** Deterministic checks suit narrow tasks (extraction, classification, short answers). They cannot judge open-ended quality. The CI eval uses mock providers, so it tests the gate and the routing logic, not real model quality. To gate on real models you would point `--config` at a real config, which costs money, and live model output can vary between runs.

### Dashboard (`llmgate/dashboard.py`)

One self-contained HTML file with no JavaScript. It shows the same log two ways:

- **Requests**, what the caller experienced. One request is one call to the gateway, however many provider attempts it took. It counts as served if any attempt succeeded. End-to-end latency adds up all its attempts, so time lost on a timeout before a fallback shows up here.
- **Provider attempts**, what each provider did. Every retry and fallback is its own attempt. Provider latency only counts successful attempts, so a provider that fails in 1 ms can't look fast.

Percentiles are nearest-rank. Errors are HTML-escaped.

### Why the breaker exists

The first live run hit a real Gemini outage: every call returned 503 or timed out after 30 seconds. Every request was still served, because Groq took over, but each one first waited for Gemini to fail. With a retry on top, that was 60 seconds per request.

Replaying that outage with mocks (10 strong requests, first provider down):

| Setup | p50 end-to-end | p95 / worst |
|---|---|---|
| 30 s timeout, timeout retried, no breaker | 60.5 s | 60.5 s |
| Timeouts not retried | 30.5 s | 30.5 s |
| + circuit breaker | 0.5 s | 30.5 s |
| + 10 s timeout on the flaky provider | 0.5 s | 10.5 s |

The breaker fixes the typical request. The timeout bounds what the first few requests pay while the breaker is still learning.

## Status board: free LLM APIs, measured

`.github/workflows/status.yml` turns llmgate into a public status page for free-tier LLM APIs. Every 2 hours it sends one tiny request ("Reply with exactly one word: OK") to each provider in `config.status.json`, with no retries and no fallback, so it measures the provider and not the gateway. Results are appended to `probes.jsonl` on a separate `status-data` branch, and the page is published to GitHub Pages.

The page shows, per provider: uptime over 24 hours, 7 days and 30 days, p50/p95 latency of successful probes, a strip of every check over the last week, and failures grouped by kind (rate limited, 5xx, timeout, auth, and so on). Raw data is published next to the page as `probes.jsonl`, so anyone can check the numbers.

Run it locally:

```
python -m llmgate probe --config config.status.json --out status/probes.jsonl
python -m llmgate status-page --data status/probes.jsonl --out site/index.html
```

Set it up on GitHub:

1. Add repo secrets `GROQ_API_KEY` and `GEMINI_API_KEY` (Settings > Secrets and variables > Actions). Use keys made only for the board, so probes don't share quota with your own testing.
2. Settings > Pages > Source: **GitHub Actions**.
3. Actions > status > **Run workflow** for the first probe. After that it runs on its own.

Limits worth knowing: this is free-tier traffic from GitHub-hosted runners, one probe per provider every 2 hours. It says nothing about paid tiers, and GitHub can delay or skip scheduled runs (those show as grey "no data" slots).

## Layout

```
llmgate/            package: providers, store, gateway, evals, dashboard, cli
tests/              unittest suite
evals/              cases.json and baseline.json
config.mock.json    cheap-mock + strong-mock
config.regressed.json   same, but cheap-mock answers two cases wrong
config.example.json     real-provider template (prices and models to fill in)
config.free.json        Groq + Gemini free tiers + local Ollama, OpenAI + Anthropic optional
config.status.json      providers probed by the status board
scripts/status-data.sh  keeps probe history on the status-data branch
.github/workflows/eval.yml      unit tests + eval gate on pull requests
.github/workflows/status.yml    probes every 2 hours, publishes the status page
```

## Phase 2: RAG (not built yet)

Planned: hybrid search (keyword plus embeddings), reranking, and answers with citations, all going through the gateway and tested by the eval pipeline (does the answer cite the right document, does it avoid claims outside the sources). This part needs an embedding model and a vector store, so it will not be stdlib-only.
