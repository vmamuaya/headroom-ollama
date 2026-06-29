# headroom-ollama

One-shot clone setup for running the [headroom-ai](https://github.com/chopratejas/headroom) compression proxy against [Ollama Cloud](https://ollama.com), with a 2-layer failsafe stack so the user-facing LLM chain self-heals.

## What you get

```
   CLI tool (claude / codex / OPENAI_BASE_URL=…)
                  │
                  ▼
        ┌──────────────────┐
        │  Hermes  (CLI)   │  model.base_url → http://127.0.0.1:8787/v1
        └────────┬─────────┘
                 │
                 ▼
        ┌──────────────────┐
        │ headroom-proxy   │  :8787  (systemd user service)
        │  - /readyz       │  kompress + smart_crusher
        │  - /v1/...       │
        └────────┬─────────┘
                 │
                 ▼
        ┌──────────────────┐
        │ Ollama Cloud     │  https://ollama.com/v1
        └──────────────────┘

  Sidecar 1: headroom-watchdog.timer → every 5 min
    - /readyz check
    - real auth probe (1-token LLM call)
    - on failure: reset systemd, reinstall venv, restart service
    - on persistent failure: increment counter

  Sidecar 2: headroom-failsafe.service → continuous, 30s ticks
    - reads watchdog counter
    - if counter ≥ 3 AND headroom unhealthy:
        flip Hermes to direct Ollama (kill-switch)
    - if direct AND headroom healthy again:
        flip back (auto-recovery)
```

## Quick start

```bash
git clone https://github.com/vmamuaya/headroom-ollama.git
cd headroom-ollama

# Get your Ollama API key from https://ollama.com/settings
# then put it in templates/headroom.env.template (replace ***your-ollama-cloud-api-key-here***)

# Install everything (creates venv, installs deps, registers systemd units)
./scripts/install.sh

# After install, verify
./scripts/verify.sh
```

That's it. After `./scripts/install.sh` exits green, point any OpenAI-compatible client at `http://127.0.0.1:8787/v1`.

## Pointing Hermes at the proxy

The install doesn't change Hermes config automatically. After install:

```bash
hermes config set model.base_url http://127.0.0.1:8787/v1
```

To revert to direct Ollama:

```bash
hermes config set model.base_url https://ollama.com/v1
```

The failsafe daemon does this automatically when headroom is broken for 3+ consecutive watchdog ticks (~15 min).

## What it costs

- 12 LLM calls/hour from the auth probes (1 token each). Negligible.
- ~110M CPU cycles per hour from the watchdog + failsafe daemons. Negligible.
- ~6 GB disk for the headroom-ai[proxy] + any-llm-sdk venv.

## Lifetime savings rollup

```bash
headroom-rollup.py            # full history
headroom-rollup.py --since 7d # last week
headroom-rollup.py --json     # machine-readable
```

## Repo layout

```
headroom-ollama/
├── bin/                          # Scripts installed to ~/.local/bin/
│   ├── headroom-watchdog.py     # 5-min self-heal
│   ├── headroom-failsafe.py     # 30s kill-switch
│   ├── headroom-snapshot.py     # daily journal writer
│   └── headroom-rollup.py       # lifetime rollup CLI
├── systemd/                      # User-level units
│   ├── headroom-proxy.service
│   ├── headroom-watchdog.service
│   ├── headroom-watchdog.timer
│   └── headroom-failsafe.service
├── templates/
│   └── headroom.env.template    # Place your OLLAMA_API_KEY here
├── scripts/
│   ├── install.sh               # bootstrap everything
│   └── verify.sh                # health checks
└── docs/
    └── OPERATIONS.md            # manual ops + troubleshooting
```

## Requirements

- Python 3.14 (headroom-ai 0.27 builds only against this)
- `uv` (https://docs.astral.sh/uv/)
- systemd with user-session support (any modern Linux)
- Hermes CLI installed and configured
- An Ollama Cloud API key (https://ollama.com/settings)

## License

Same as upstream headroom-ai (Apache 2.0).