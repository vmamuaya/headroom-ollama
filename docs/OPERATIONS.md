# Operations & troubleshooting

## Manual health checks

```bash
# Service state
systemctl --user status headroom-proxy.service
systemctl --user status headroom-watchdog.timer
systemctl --user status headroom-failsafe.service

# Watchdog log (5-min tick)
tail -50 ~/.headroom/watchdog.log

# Failsafe log (30s tick)
tail -50 ~/.headroom/failsafe.log

# Proxy log (everything that flows through)
tail -50 /tmp/headroom-proxy.log

# Real-time proxy
journalctl --user -u headroom-proxy.service -f

# Check current routing
hermes config show | grep base_url
```

## Operator mode override (learn-mode / crawl-mode)

Sometimes you want to bypass headroom entirely — every prompt is unique, or
compression overhead exceeds savings. The failsafe supports two pinned-bypass
modes: `learn` and `crawl`. Both have identical behavior (route direct to
Ollama Cloud, no compression, no headroom) but are tracked separately in
audit logs so you can tell why a session was unpinned.

### Toggle modes

```bash
# Bypass headroom immediately for the current session (and all sessions
# until you toggle back). Effect is immediate:
python3 ~/.local/bin/headroom-failsafe.py --mode learn     # for analysis / agent training
python3 ~/.local/bin/headroom-failsafe.py --mode crawl     # for bulk scraping / corpus ingestion

# Toggle back to automatic failsafe behavior (returns to headroom if healthy):
python3 ~/.local/bin/headroom-failsafe.py --mode auto

# Inspect current state:
python3 ~/.local/bin/headroom-failsafe.py --status
```

### What happens under the hood

1. The mode string is written to `~/.headroom/failsafe-mode` (mode 0644).
2. The CLI process immediately invokes `hermes config set model.base_url https://ollama.com/v1`.
3. The running failsafe daemon (if active) reads the mode file on its next
   30-second tick and re-applies the pin to defend against a Hermes restart
   or another process overwriting the config.
4. While mode is `learn` or `crawl`, the failsafe skips its normal
   healthy/unhealthy decision matrix entirely. Routing stays direct.

### State fields added

The failsafe state file (`~/.headroom/failsafe-state.json`) gains two new
fields so you can audit how often pinned modes were used:

```json
{
  "mode": "learn",
  "mode_pinned_flips": 3,
  ...
}
```

### When to use what

| Use case | Mode | Why |
|---|---|---|
| Single-turn LLM lookups for analysis | `learn` | compression overhead > savings when context is always new |
| Crawling/scraping with identical prompts | `crawl` | high cache hit rate already, compression redundant |
| Bulk document summarization | `crawl` | unique inputs each turn, compression rate low |
| Multi-turn agent with persistent state | `auto` | compression wins big here |
| Conversation-heavy chat workloads | `auto` | prior-turn compression = biggest savings category |

### Important caveats

- The CLI runs in the foreground; you don't need sudo.
- The toggle persists across reboots (mode file is on disk, not in tmpfs).
- Toggling to `auto` triggers an immediate tick — if headroom is healthy,
  you flip back within ~2 seconds. If headroom is broken, you stay on
  direct until headroom recovers (standard failsafe behavior).
- Both pinned modes bypass the watchdog's self-heal counter. Headroom can
  be entirely down while learn/crawl is active and nothing else changes.

## Manual failover (don't wait for failsafe)

```bash
# Flip to direct Ollama
hermes config set model.base_url https://ollama.com/v1

# Flip back to headroom
hermes config set model.base_url http://127.0.0.1:8787/v1
```

## Reset the failsafe counter

If the watchdog counter climbed to 3 and failsafe flipped routing, but you've manually fixed headroom and want to force-flip back:

```bash
# Confirm headroom is actually healthy first
curl -s http://127.0.0.1:8787/readyz | python3 -m json.tool | head -10

# Run a single watchdog tick — if successful, counter resets to 0
/usr/bin/python3 ~/.local/bin/headroom-watchdog.py

# Then either wait 30s for failsafe to auto-flip back, or:
hermes config set model.base_url http://127.0.0.1:8787/v1
```

## Common failure modes

### `/readyz` green but every request errors with 401

The OLLAMA_API_KEY is bogus. /readyz doesn't validate the key (headroom-ai only checks it on real requests).

Fix:
```bash
# Edit ~/.headroom/headroom.env and put the real key
nano ~/.headroom/headroom.env
systemctl --user restart headroom-proxy.service
```

### Service won't start: "bad interpreter"

The venv shebang is broken (built under /tmp, moved, or stale python path).

Fix:
```bash
# Patch shebang
sed -i "1c\\#!/usr/bin/env python3" ~/.local/venvs/headroom/bin/headroom

# Or rebuild the venv
rm -rf ~/.local/venvs/headroom
uv venv ~/.local/venvs/headroom --python 3.14
uv pip install --python ~/.local/venvs/headroom/bin/python 'headroom-ai[proxy]' 'any-llm-sdk[all]' --force-reinstall
systemctl --user restart headroom-proxy.service
```

### Watchdog keeps reinstalling the venv

Logs say `HEAL: venv unhealthy — attempting uv pip reinstall` repeatedly.

Usually means the venv path was deleted or moved. Check:
```bash
ls -la ~/.local/venvs/headroom/bin/headroom
head -1 ~/.local/venvs/headroom/bin/headroom
```

If shebang is wrong (pointing at `/tmp/...` or missing), fix per above. The watchdog will reinstall on its own if needed but it's faster to just patch the shebang.

### `requests.failed` is 0 even on auth errors

That's not a bug — it's the upstream model returning a 200 OK with an `{"error":...}` envelope. Headroom doesn't count those as failed requests. Use the auth probe (in the watchdog) for that signal.

### Ollama Cloud returns 401 with valid-looking key

Ollama Cloud keys sometimes need to be re-issued at https://ollama.com/settings. Old keys can be silently revoked if the account had a billing issue.

### Counter keeps climbing even after restart

Means headroom keeps failing self-heal. Check:
1. `tail -50 ~/.headroom/watchdog.log` — what does each heal attempt say?
2. `systemctl --user status headroom-proxy.service` — is the service actually starting?
3. `/tmp/headroom-proxy.log` — startup errors?

If you've fixed the underlying issue, manually reset the counter:
```bash
/usr/bin/python3 -c "
import json
p = '/home/victor/.headroom/watchdog-state.json'
with open(p) as f: s = json.load(f)
s['consecutive_self_heal_failures'] = 0
s['last_self_heal_result'] = 'manually reset'
with open(p, 'w') as f: json.dump(s, f, indent=2)
print('counter reset')
"
```

## Architecture decisions

### Why two layers?

A single daemon doing everything (heal + flip) would either:
- Flip too aggressively (every tick where proxy is briefly down → flip → flip back → flip → oscillation)
- Or flip too slowly (waiting minutes before flipping because it's busy trying to heal)

Two layers solve this:
- **Watchdog** = the surgeon. Aggressive about healing. Doesn't touch config.
- **Failsafe** = the circuit breaker. Patient (waits for 3-strike signal from watchdog). Only touches config when watchdog has clearly given up.

### Why a real LLM call for health check?

Because `/readyz` lies when the API key is broken. Cost is negligible (12 calls/hour × 1 token).

Alternative: implement a synthetic auth probe that doesn't make a real call. Could use the OpenAI SDK's `models.list()` endpoint which only needs an auth check (no model invocation). Future optimization.

### Why 5-min watchdog cadence?

- Tight enough to catch outages within 5 min of headroom dying
- Loose enough that 12 LLM probe calls/hour doesn't drown out real user traffic
- Tunable via `OnUnitActiveSec=` in `systemd/headroom-watchdog.timer`

### Why 30s failsafe cadence?

- Fast enough that once watchdog declares defeat, flip happens within 30s
- Slow enough to keep config-write noise down
- Tunable via `time.sleep(30)` in `bin/headroom-failsafe.py`

## Files you should back up

```bash
~/.headroom/headroom.env         # API key + backend config
~/.local/venvs/headroom/         # 6 GB, rebuildable from bin/
~/.config/systemd/user/headroom-*  # 5 files, all in this repo
```

If you lose everything except `~/.headroom/headroom.env`, `./scripts/install.sh` recovers the rest.