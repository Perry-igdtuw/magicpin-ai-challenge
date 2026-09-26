# Vera — Merchant AI Assistant

This submission implements the magicpin challenge contract as a deterministic, dependency-free Python HTTP service.

## Run

```bash
python3 bot.py
```

The server listens on `http://localhost:8080` (override with `PORT=8081`). The five required endpoints are implemented:

- `POST /v1/context` stores versioned category, merchant, customer, and trigger contexts. Same-version or older pushes return a stale-version response.
- `POST /v1/tick` composes at most one deduplicated action per active trigger.
- `POST /v1/reply` routes explicit acceptance directly to action, detects repeated/canned auto-replies, backs off when asked, and exits on STOP/not-interested language.
- `GET /v1/healthz` reports liveness and loaded context counts.
- `GET /v1/metadata` reports the bot identity and approach.

## Approach and tradeoffs

The composer uses trigger-kind dispatch with reusable helpers for category digest retrieval, active offers, performance deltas, customer preferences, and source citations. This keeps every factual claim traceable to the pushed contexts and makes the service fast enough for the 30-second judge budget without an external API or network data leakage. Customer-facing messages use `merchant_on_behalf`; merchant-facing messages use `vera`.

The tradeoff is that this is a transparent rules-and-templates implementation rather than an LLM call. It sacrifices open-ended linguistic variety, but gains deterministic replay, predictable latency, easy auditing, and safe adaptation to injected context versions. All trigger kinds in the supplied seed dataset have a dedicated route or a safe generic fallback.

`submission.jsonl` contains 30 deterministic sample compositions generated from the supplied trigger set, including customer-facing and merchant-facing cases.
