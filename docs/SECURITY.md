# Security

## Threat model

This repo exposes a local LLM-compression proxy (headroom-ai) on `127.0.0.1:8787`,
plus a kill-switch daemon that can reconfigure the caller (Hermes). Risks:

1. **Local network exposure** — anyone who can reach 127.0.0.1:8787 can use your
   Ollama Cloud quota. The proxy binds localhost by default. Don't expose publicly.
2. **API key leakage** — the Ollama Cloud API key unlocks billable inference. It
   must never be committed.
3. **Self-heal auto-restart** — the watchdog will reinstall venv and restart
   the proxy automatically. If the user systemd is compromised, the watchdog
   re-creates the vulnerability.

## What this repo guarantees

- No real API keys in git history (audit: `python3 scripts/audit-secrets.py`).
- `templates/headroom.env.template` ships with only placeholder values.
- `.gitignore` blocks `*.env.local`, `.env`, and `secrets/`.
- `install.sh` creates `~/.headroom/headroom.env` with mode `0600`.
- The proxy listens on `127.0.0.1` only — not `0.0.0.0`.

## What you (the operator) must do

1. **Get your key** at https://ollama.com/settings/keys (or copy from your
   existing Ollama account). Never paste it into chat / code / git messages.
2. **Paste it into the env file** the install script creates, not anywhere else:
   ```bash
   $EDITOR ~/.headroom/headroom.env
   chmod 600 ~/.headroom/headroom.env
   ```
3. **Verify file perms** before running: `stat -c '%a' ~/.headroom/headroom.env`
   should show `600`.
4. **Never commit** `~/.headroom/headroom.env` or any file containing the key.
5. **Rotate the key** if you suspect it's been leaked — Ollama lets you
   regenerate on the dashboard.
6. **Don't expose 8787 publicly**. If you need remote access, use a reverse
   proxy with auth (Caddy, nginx with basic auth, Cloudflare Tunnel, etc.).

## Default network bind

The proxy defaults to `127.0.0.1:8787`. This is enforced by the systemd unit's
`--host` flag (see `systemd/headroom-proxy.service`). To accept external
connections you'd need to edit the unit, set `host=0.0.0.0`, and reload —
but you SHOULD then add auth, as anyone reaching the proxy can spend your
quota.

## Audit commands

The repo ships a secret auditor at `scripts/audit-secrets.py`:

```bash
./scripts/audit-secrets.py
```

It scans the working tree AND full git blob history for known API-key
formats and outputs a JSON report. Run it after any change.

Caveats:
- The auditor does NOT catch every possible secret format.
- Known upstream test fixtures (e.g. `AKIAEXAMPLEAKIDFORTEST`) trip the
  detector — that's intended as a sanity check, but if you fork and add
  real test credentials, the audit will flag them.

## Reporting a vulnerability

Open a GitHub issue at https://github.com/vmamuaya/headroom-ollama/issues
or contact the maintainer directly. Don't include the real key in any
public channel — even if you're asking for help.
