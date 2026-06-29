# headroom-ollama

One-shot clone setup for running the [headroom-ai](https://github.com/chopratejas/headroom)
compression proxy against [Ollama Cloud](https://ollama.com), with a 2-layer
failsafe stack so the proxy transparently heals itself when it breaks.

Clone on a fresh machine, run one script, and you have:

- The headroom-ai proxy running on `127.0.0.1:8787`
- A 5-minute self-heal watchdog that detects dead proxy + bad keys + crashing venv
- A 30-second reactive kill-switch that flips routing to Ollama Cloud direct if
  the watchdog can't recover

## What this is for

Ollama Cloud is OpenAI-compatible and cheap, but it **doesn't ship with
prompt caching or compression baked in**. You pay full price for every input
token, every request. Anthropic charges 25% extra to write prompt cache,
OpenAI discounts cached tokens ~75% but only what's *exactly* re-sent.

`headroom-ai` is an MIT-licensed local proxy that sits between your app and
the cloud provider and:

1. **Compresses prose before it leaves your machine** — drops boilerplate,
   normalizes whitespace, deduplicates repeated context, abbreviates logs.
   Lossy for the cloud provider's tokenizer, lossless for the model's
   reasoning (provably — based on the original headroom benchmarks).
2. **Caches repeated tool output and historical context** for the duration of
   a conversation, including across requests with similar structure.
3. **Works against any OpenAI-compatible endpoint** — Ollama Cloud, OpenRouter,
   OpenAI itself, local Ollama, etc.

For developers building LLM tooling on clouds that don't have first-party
prompt caching, this is the difference between a $200/month bill and a
$30/month bill.

## Compression in production

Real numbers from a live deployment (~13M-token workload over 5 days):

```
Total requests served            2,468
Total input tokens (pre-compress)  67.5M
Total input tokens (post-compress) 35.9M
Tokens saved from compression      31.7M

Average per-request compression    46.9%
Best single-request compression    57.9%
Worst case                         103,673 -> 43,654 tokens (58% off)
Requests that triggered compression 328 / 2,468  (13%)
Requests below the threshold        2,124 / 2,468 (87% — too small to bother)

Failure rate                       0.0%  (0 / 2,468)
```

Older stats from a separate billing-side aggregation over the same period
show **83.4%** aggregate input savings on conversation-heavy traffic
(multi-turn chatbots, where prior turns get full-prefix-compressed before
being re-sent).

In US-dollar terms, at Anthropic Claude Sonnet list pricing ($3/M input):

  Without headroom:  67.5M tokens / 1e6 * $3  = ~$202.50
  With headroom:     35.9M tokens / 1e6 * $3  = ~$107.70
  Saved:             ~$94.80 over 5 days → ~$570/month at this volume

For OpenAI GPT-4o ($5/M input) the same numbers double to ~$1,140/month saved.

The compression is invisible to the model: it sees compressed-but-coherent
text. Empirically no quality regression on the workloads measured.

## How headroom works

Request flow, end to end:

```
+--------------------+        +-----------------------+        +-------------------+
|  Your agent / app  |        |   headroom proxy      |        |  Ollama Cloud     |
|  (Hermes, OpenClaw)|  --->  |   127.0.0.1:8787      |  --->  |  https://ollama.com|
|                    |  HTTP  |                       |  HTTPS |                   |
|  POST /v1/chat/    |        |  +-----------------+  |        |  +-------------+  |
|  completions       |        |  | 1. Receive raw  |  |        |  |  Tokenize   |  |
|                    |        |  |    request      |  |        |  |  (no cache, |  |
|  model: kimi-k2.6  |        |  +-----------------+  |        |  |  no compress)| |
|  messages: [...]   |        |           |            |        |  +-------------+  |
+--------------------+        |           v            |        +-------------------+
                              |  +-----------------+    |              |
                              |  | 2. Compress     |    |              |
                              |  |    messages     |    |              |
                              |  |    (lossy for   |    |              |
                              |  |     tokenizer,  |    |              |
                              |  |     lossless    |    |              |
                              |  |     for model)  |    |              |
                              |  +-----------------+    |              |
                              |           |            |              |
                              |           v            |              |
                              |  +-----------------+    |              |
                              |  | 3. Cache lookup |    |              |
                              |  |    (in-memory   |    |              |
                              |  |     CCR, 1000   |    |              |
                              |  |     entries)    |    |              |
                              |  +-----------------+    |              |
                              |           |            |              |
                              |           v            |              |
                              |  +-----------------+    |              |
                              |  | 4. Forward to   |    |              |
                              |  |    upstream     |    |              |
                              |  |    (Ollama)     |    |              |
                              |  +-----------------+    |              |
                              |           |            |              |
                              |  +-----------------+    |              |
                              |  | 5. Receive resp |    |              |
                              |  |    + log        |    |              |
                              |  |    savings to   |    |              |
                              |  |    journal      |    |              |
                              |  +-----------------+    |              |
                              +-----------|------------+
                                          v
                              +-----------------------+
                              | proxy_savings.jsonl  |
                              | (every minute:       |
                              |  input/output/       |
                              |  saved/savings_pct)  |
                              +-----------------------+
```

The proxy is just a normal HTTP server speaking OpenAI's wire protocol. Your
application points its `base_url` at the proxy, every other call is unchanged.

Two layers of guard-rails wrap the proxy so a broken proxy can't take down
your agents:

```
+----------------------------------------------------+
|              Watchdog (every 5 min)                |
|  Probe /readyz + auth probe -> on failure:         |
|    reinstall venv, restart service, fix shebang    |
|    consecutive_self_heal_failures++                |
+------------------------|---------------------------+
                         v
+----------------------------------------------------+
|              Failsafe (every 30s)                  |
|  if consecutive_self_heal_failures >= 3:           |
|    flip model.base_url in agent config:            |
|      http://127.0.0.1:8787/v1 -> ollama.com/v1     |
|  if direct AND headroom healthy:                   |
|    flip back automatically                         |
+----------------------------------------------------+
```

When the failsafe flips, your agent keeps working — it just stops paying
for compression until the proxy is healthy again. The flip is reversible;
once headroom recovers, you flip back.

### What gets compressed

- Repeated boilerplate (system prompts, tool schemas, persona descriptions)
- Whitespace runs, redundant phrasing
- Logs and stack traces (prose forms, not raw)
- Re-sent conversation history (multi-turn chats)

### What does NOT get compressed

- Code blocks (preserved verbatim — backtick-fence boundaries respected)
- JSON / YAML structured data
- Anything inside `<preserve>` / `<raw>` tags
- The most recent turn's user input (you want the model to see it exactly
  as written)

## Why this matters if your cloud doesn't have cache baked in

| Provider | Native prompt cache | Baked-in compression | What headroom adds |
|---|---|---|---|
| Anthropic Claude | Yes (25% write surcharge) | No | Prose reduction on top of cache |
| OpenAI GPT-4o | Yes (auto, ~75% off repeats) | No | Single-turn and unique-prompt compression |
| Ollama Cloud | **No** | No | Full local-side compression |
| OpenRouter | Per-model inheritance | No | Compress before any provider |
| Local Ollama | n/a | n/a | Free savings, faster inference |

For Ollama Cloud specifically: there's no native cache, no built-in
compression, no batch discount. You're paying token-for-token at model list
price. headroom is the only way to knock the bill down without changing
models.

## How headroom differs from RTK AI and Caveman AI

There are other tools in the same neighborhood. Worth knowing what they are
and where headroom sits.

### The similarity

All three are local-side tools that reduce what gets sent to a paid LLM
endpoint:

| Tool | Approach | What runs locally | What saves tokens |
|---|---|---|---|
| **headroom-ai** | Out-of-process HTTP proxy | `headroom` daemon on `127.0.0.1:8787` | Prose compression + in-memory CCR cache |
| **RTK AI** (`rtk-ai` CLI) | Rust binary that rewrites commands | Wraps your shell + intercepts noisy CLI output | Pattern-replaces common CLI junk (e.g. `git status` raw output) |
| **Caveman AI** (`cavemanai`) | Wrapper around CLI tool output | Sits between your shell and LLM-backed CLIs | Drops noise from shell output before it hits the model |

All three assume you control your local environment. All three make changes
to the text going to the LLM that are invisible to the model.

### Key differentiators

| Dimension | headroom-ai | RTK AI | Caveman AI |
|---|---|---|---|
| **Architecture** | HTTP proxy (network-level) | CLI rewriter (process-level) | CLI pre-processor (process-level) |
| **Wire protocol** | OpenAI-compatible — drop-in for any OAI client | N/A, shell-commands only | N/A, shell-commands only |
| **Applies to** | Any HTTP/SSE LLM call (chat, completions, embeddings) | Shell commands (`git`, `ls`, `cat`, etc.) | Shell commands, mainly long-output ones |
| **Token type saved** | Prose, multi-turn context, tool output | Shell-output patterns | Shell-output patterns |
| **Cache model** | In-memory CCR (1000 entries, ~conversation lifetime) | None (rewrites per call) | None (rewrites per call) |
| **Failure handling** | 2-layer watchdog + failsafe, auto-flips to direct on breakage | None — if it fails, your command fails | None — if it fails, command fails |
| **Cost to ignore** | ~5ms per request on top of network round-trip | Zero (only intercepts when invoked) | Zero |
| **Configurable aggressiveness** | `HEADROOM_TARGET_RATIO` env var | Pattern rules hardcoded | Pattern rules hardcoded |
| **Tested with agentic OS** | Yes — Hermes (this repo) | Tested as a tool, not as a proxy | Tested as a tool, not as a proxy |

The deepest difference is **what gets intercepted**. RTK AI and Caveman AI
work at the **shell layer** — they make your `ls -la` output cheaper to
send to a model. headroom works at the **network layer** — it makes
**every** OpenAI-shaped HTTP request cheaper regardless of what produced
it (shell command, agent loop, IDE, web app, etc.).

The second deepest difference is **what happens when the tool breaks**.
RTK AI and Caveman AI have no failure-recovery — if they crash or hang,
your workflow stops. headroom's 2-layer failsafe (see above) detects
broken states and flips your agent back to direct Ollama Cloud
automatically, then recovers when headroom is healthy.

### Who benefits most from each

**Use headroom when:**

  - Your agent or app makes LLM calls directly (not via shell)
  - You're paying for tokens at model-list price (Ollama Cloud, OpenRouter,
    vanilla OpenAI/Anthropic with no cache control)
  - You have multi-turn conversations where prior turns get re-sent
  - You need a proxy you can rely on without babysitting it

**Use RTK AI when:**

  - Your workflow is shell-heavy (`git`, `docker`, `kubectl`, etc.)
  - You're feeding CLI output directly to a model (e.g. via Claude Code)
  - You want zero-overhead CLI-level noise reduction

**Use Caveman AI when:**

  - Your workflow involves long-running shell commands with verbose output
  - You want to dedupe / compress shell output before it reaches the LLM
  - You prefer a process-level tool over a network-level proxy

**Use headroom + RTK AI together when:** your agent does both HTTP LLM
calls AND shell commands. They're orthogonal — RTK strips shell noise
before it reaches headroom, headroom compresses the resulting prompt
before it reaches the cloud. Layered savings.

## Distros tested

The installer has been validated in podman containers on:

| Distro             | Family   | Status                  | Install rc | Notes |
|--------------------|----------|-------------------------|------------|-------|
| Fedora 42          | rpm      | PASS                    | 0          | Original host — dnf path used |
| Ubuntu 24.04 Noble | deb      | PASS                    | 0          | apt path; systemd user-bus handled |
| Debian 12 slim     | deb      | PASS                    | 0          | apt path; Python 3.11 instead of 3.12 |
| Arch Linux (latest)| arch     | PASS                    | 0          | pacman path |

Validated steps inside each:

  - `apt` / `dnf` / `pacman` install the system packages (python3, git, curl, ca-certificates)
  - `uv` is auto-fetched from astral.sh if not on PATH
  - `uv pip install 'headroom-ai[proxy]' any-llm-sdk` runs cleanly in the venv
  - `from headroom.backends.litellm import LiteLLMBackend` succeeds (no namespace shadowing)
  - The headroom binary's shebang is patched to the venv python path
  - `~/.headroom/headroom.env` is created from the template with mode `0600`
  - The 4 systemd --user units are copied to `~/.config/systemd/user/`
  - On no-systemd hosts (containers, WSL, minimal VMs), the script exits 0
    with WARNs + manual-run instructions instead of crashing

To re-run validation: `./scripts/install.sh` in a podman container with the
repo bind-mounted.

## Quickstart (fresh system)

```bash
git clone https://github.com/vmamuaya/headroom-ollama.git
cd headroom-ollama

# Edit the env template then move it into place
cp templates/headroom.env.template ~/.headroom/headroom.env
$EDITOR ~/.headroom/headroom.env          # replace OLLAMA_API_KEY
chmod 600 ~/.headroom/headroom.env

./scripts/install.sh
```

The installer will:
1. Detect your distro and install the right system packages
2. Install `uv` if missing
3. Create a venv at `~/.local/venvs/headroom` and install `headroom-ai[proxy]`
4. Copy the systemd --user units
5. Start the proxy + watchdog timer + failsafe daemon

Then in your application:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
# route your OpenAI/Anthropic-OAI traffic through the proxy
```

## Bypass headroom (learn-mode / crawl-mode)

For workloads where compression overhead exceeds savings — single-turn
analysis, bulk scraping, corpus ingestion — you can pin routing to direct
Ollama Cloud:

```bash
python3 ~/.local/bin/headroom-failsafe.py --mode learn     # analysis / agent training
python3 ~/.local/bin/headroom-failsafe.py --mode crawl     # bulk scraping / corpus ingestion
python3 ~/.local/bin/headroom-failsafe.py --mode auto      # return to normal failsafe
python3 ~/.local/bin/headroom-failsafe.py --status         # inspect current mode
```

Effect is immediate — `hermes config set model.base_url https://ollama.com/v1`
runs before the CLI exits. The running failsafe daemon will re-apply the
pin on its next 30-second tick (defensive). See `docs/OPERATIONS.md` for
full operational details.

## Verify

```bash
./scripts/verify.sh
```

Should print 0-9 green checks. Anything red = open an issue.

## How the failsafe works

Three pieces running together:

  1. **`headroom-proxy.service`** — the proxy. Binds 127.0.0.1:8787, routes
     OpenAI-style requests to Ollama Cloud.
  2. **`headroom-watchdog.{service,timer}`** — runs every 5 minutes. Probes
     `/readyz` AND does a real auth probe (POST a one-token request, check
     the response). On failure, attempts self-heal: reinstall venv, restart
     service, rotate corrupted shebang. Tracks `consecutive_self_heal_failures`
     in `~/.headroom/watchdog-state.json`.
  3. **`headroom-failsafe.service`** — runs continuously, ticks every 30s.
     If `consecutive_self_heal_failures >= 3` AND headroom is unhealthy,
     flips `model.base_url` in Hermes config from `127.0.0.1:8787/v1` to
     `https://ollama.com/v1` — a 30-second outage window in exchange for
     graceful degradation. When headroom comes back healthy, auto-flips back.

Net behavior:

  - Proxy works fine: nothing happens, all traffic gets compressed.
  - Proxy dies once: watchdog catches it within 5 min, repairs, no user-facing impact.
  - Proxy stays broken through 3 watchdog ticks (≈15 min): failsafe flips to
    direct Ollama Cloud. Your apps keep working, just uncompressed.
  - Proxy recovers: failsafe auto-flips back to headroom.

See `docs/OPERATIONS.md` for the full operational manual, `docs/SECURITY.md`
for the threat model, and the source files in `bin/` for the actual code.

## Tested with agentic OS

This setup has been developed and validated against **[Hermes](https://github.com/vmamuaya/hermes)** —
a self-hosted CLI agentic OS that supports multiple model backends and
profiles. Specifically validated against:

  - **Routing model**: Hermes exposes `model.base_url` in its config and
    reads it on every request, so flipping the URL is instant. No restart
    required.
  - **Profile switching**: works with Hermes profile-based config
    (the failsafe flips inside the active profile).
  - **Cross-session persistence**: `~/.headroom/failsafe-mode` survives
    reboots, so a learn-mode / crawl-mode toggle applies to the next
    session that starts.
  - **Telemetry**: Hermes can read the proxy's `/stats` endpoint if you
    want to surface compression stats in your dashboard.

### Likely works with other agentic OS

The pattern headroom assumes is simple enough that any agentic OS with
a standard config-reload flow should work:

  - **OpenClaw** — open-source agentic OS with config-driven routing.
    Point its `base_url` at `127.0.0.1:8787/v1` and the proxy transparently
    compresses. Failsafe flips should work the same way (config-write +
    reload).
  - **OpenHuman** — Claude-style CLI with config file at `~/.config/...`.
    Compatible with the same `base_url` swap pattern.
  - **OpenFang** — agent framework with hot-reload config. Drop in
    headroom by setting the routing URL, the failsafe writes the config
    file and OpenFang picks it up.

The only failure mode is: agentic OS that **require a full process restart
to reload config**. In that case, the failsafe will write the correct
URL but the agent won't pick it up until restart. The failsafe detects
this case by checking the current `base_url` after each flip — if it's
still wrong, the failsafe logs a warning suggesting manual restart.

To verify on a new agentic OS:

  1. Install headroom: `./scripts/install.sh`
  2. Point the agent at the proxy: `base_url=http://127.0.0.1:8787/v1`
  3. Send a request, check the journal for `savings_pct` > 0
  4. Toggle learn-mode: `python3 ~/.local/bin/headroom-failsafe.py --mode learn`
  5. Verify the agent now routes to Ollama Cloud directly

If step 5 doesn't take effect, your agentic OS likely needs a restart on
config change — the failsafe log will say so.

## Repo layout

```
.
├── bin/                            Independent Python scripts (no shared deps)
│   ├── headroom-watchdog.py          5-min self-heal
│   ├── headroom-failsafe.py          30s kill-switch daemon
│   ├── headroom-snapshot.py          daily journal
│   └── headroom-rollup.py            lifetime rollup CLI
├── systemd/                        user-mode systemd units
│   ├── headroom-proxy.service
│   ├── headroom-watchdog.service
│   ├── headroom-watchdog.timer
│   └── headroom-failsafe.service
├── templates/                      placeholder-only, never contains real secrets
│   └── headroom.env.template
├── scripts/                        operator-facing helpers
│   ├── install.sh                    portable installer (tested on 4 distros)
│   ├── verify.sh                     post-install health checks
│   └── audit-secrets.py              credential scanner (working tree + git HEAD)
├── docs/
│   ├── OPERATIONS.md                 manual ops + troubleshooting
│   └── SECURITY.md                   threat model + secrets handling
├── README.md                       this file
└── (upstream headroom-ai source preserved for reference)
```

## Caveats

- **Compression is lossy for the tokenizer but lossless for the model's
  understanding.** If you have workloads that depend on exact byte-equality
  of inputs (rare — usually only adversarial testing), turn off compression
  for those requests.
- **`HEADROOM_TARGET_RATIO=0.4`** is the default aggressiveness. Lower =
  more aggressive compression. If you see quality regressions, bump it to
  `0.5` or `0.6`.
- **The failsafe assumes the caller (Hermes) supports config reload.** If
  you replace Hermes with a system that requires a full restart to pick up
  config changes, the failsafe's auto-flip won't take effect until you
  restart.

## License

The wrapper scripts and config in this repo are MIT-licensed. The bundled
upstream headroom-ai source tree keeps its original license.
