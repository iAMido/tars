# TARS

Personal AI agent. One Python process. Telegram bot + scheduled jobs + read-only dashboard.
Inspired by [Asaf Saar's "Meet TARS"](https://asaf.corgimind.com/thinking/meet-tars).

## What this is

A single asyncio process running on a €5/mo Hetzner CPX22 that:

- Polls Telegram and answers in a deadpan TARS voice
- Saves notes with citations, retrieves them via hybrid search (FTS5 + sqlite-vec + Voyage rerank)
- Tracks promises with citation-gated follow-up closure
- Resolves entity aliases (asking about "OAI" finds notes mentioning "OpenAI")
- Fires a daily 05:00 morning briefing from Gmail + Calendar + open follow-ups
- Surfaces interim email updates every 30 min (quiet 22:00-07:00)
- Reopens overdue follow-ups every Sunday at 18:00
- Backs itself up daily to Backblaze B2 with `restic`
- Serves a read-only dashboard at `https://tars-prod.<your-tailnet>.ts.net` via Tailscale Serve

State of the world (current model usage):

- `interactive_fast` → `deepseek/deepseek-v3.2` ($0.026/$0.38 per 1M tokens)
- `cron_default` / `ingest` → `deepseek/deepseek-v3.2`
- `web_research` → `openai/gpt-5:online` (Perplexity-style auto web search)
- Embeddings → `voyageai/voyage-3-large` (1024-dim float32)
- Reranker → `voyageai/rerank-2.5` with identity fallback

Typical daily cost: **~$0.01-$0.05 in LLM tokens**.
All-in monthly: **~€10-12** (Hetzner CPX22 + B2 + LLM).

## Architecture

```
                    Single Python 3.13+ asyncio process (systemd: tars.service)

  Telegram ──long-poll──► aiogram Dispatcher ─┐
                                              ├──► Agent (stateless) ──► LLM Router ──► OpenRouter / OpenAI
  Browser ──HTTPS via    FastAPI + SSE ───────┤         │
  tailscale serve        (bound 127.0.0.1)    │         ▼
                          APScheduler ────────┘     aiosqlite (tars.db, WAL)
                          AsyncIOScheduler         ├── tables (notes, messages, ...)
                          (5 jobs)                 ├── FTS5: brain_docs
                                                   └── vec0:  vec_docs   (sqlite-vec ext)

  External: Voyage AI (embed+rerank), Gmail/Calendar (OAuth), restic→B2
```

Invariants (don't violate without an ADR):

1. One process. One Agent. One SQLite handle.
2. `uvicorn workers=1`. Forking the scheduler = jobs run N times.
3. Frozen byte-identical system prompt + tool JSON. Never f-string today's date into it.
4. History appended last in the messages array; prefix lives at index 0.
5. Single writer to SQLite (serialized via `asyncio.Lock`). Many readers fine.
6. No public ports. Dashboard bound to `127.0.0.1`, exposed via Tailscale Serve.

## Daily ops

### Status

From your laptop:

```powershell
# Bot health
ssh tars-vps "systemctl status tars --no-pager | head -8"

# Recent errors
ssh tars-vps "tail -n 30 ~/logs/tars.err"

# Next scheduled job runs
"SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time;" | ssh tars-vps "sqlite3 ~/.tars/tars.db"

# Full inspector (cost + notes + follow-ups + entities)
ssh tars-vps "cd ~/tars && ~/.local/bin/uv run python scripts/inspect_db.py"
```

From your phone (Telegram):

- `/stats` — today's cost + counts + next 5 scheduled jobs
- `/tier` — current tier→model mapping
- `/whoami` — your chat ID (works for unauthorized users too, in case Telegram ever swaps your ID)
- `/clear` — wipe the current chat's history (notes and follow-ups preserved)

### Deploy a change

```powershell
# 1. Edit code locally on Windows
# 2. Run tests
uv run pytest tests/unit -q

# 3. Commit + push
git add -A
git commit -m "your message"
git push

# 4. Pull on VPS, restart
ssh tars-vps "cd ~/tars && git pull && ~/.local/bin/uv sync --frozen && sudo systemctl restart tars && sleep 4 && systemctl status tars --no-pager | head -8"
```

### Update API keys

```powershell
notepad $HOME\.tars\config.toml  # edit the value(s)
uv run python scripts\make_linux_config.py | ssh tars-vps "umask 077 && cat > ~/.tars/config.toml && chmod 600 ~/.tars/config.toml"
ssh tars-vps "sudo systemctl restart tars"
```

The `make_linux_config.py` script rewrites `[paths]` to Linux paths and preserves all secrets verbatim — never written to disk on Windows, never echoed to PowerShell.

### Backup / restore

Backups run automatically at **06:00 IDT daily** via `tars-backup.timer`. They snapshot `tars.db` (online `.backup` API), `~/.tars/config.toml`, `~/.tars/google_token.json`, `~/.tars/client_secret.json`, and `~/vault` (when populated), push to Backblaze B2, and prune to **14 daily / 8 weekly / 12 monthly**.

Trigger a backup manually:

```powershell
ssh tars-vps "sudo systemctl start tars-backup.service && sleep 5 && sudo systemctl status tars-backup.service --no-pager | head -8"
```

List snapshots in B2:

```powershell
ssh tars-vps "sudo bash -c 'set -a; source /etc/tars/restic.env; set +a; restic snapshots'"
```

Restore drill (non-destructive — restores to `/tmp/tars-restore` and verifies):

```powershell
ssh tars-vps "bash ~/tars/scripts/restore_from_backup.sh"
```

**Full disaster recovery** (the box is gone):

1. Provision a fresh CPX22 with the same SSH key (see `scripts/bootstrap_vps.sh`)
2. Run `scripts/bootstrap_vps.sh` as root (installs deps, creates tars user, sets up Tailscale + UFW)
3. Run `scripts/harden_ssh.sh` to lock down SSH
4. Copy `~/.tars/config.toml` from your laptop or password manager
5. Add the deploy key to GitHub if needed (see "Phase 9a" in PLAN.md)
6. `git clone git@github.com:iAMido/tars.git ~/tars && cd ~/tars && uv sync --frozen`
7. Set up the restic env file at `/etc/tars/restic.env` with B2 keys (see `scripts/setup_backups.sh`)
8. Restore the latest snapshot: `bash scripts/restore_from_backup.sh`
9. Move the restored DB into place: `cp /tmp/tars-restore/home/tars/.tars/snapshots/tars-*.db ~/.tars/tars.db`
10. Install systemd units and start: `sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/ && sudo systemctl enable --now tars tars-backup.timer`

Total cold restore time: **~20 minutes**.

## Project layout

```
src/tars/
├── __main__.py          # asyncio entrypoint + CLI (bot, briefing, job, reindex, chat, check)
├── config.py            # pydantic-validated TOML config loader
├── db.py                # aiosqlite + sqlite-vec extension + migrations
├── prompt.py            # FROZEN SYSTEM_BLOCK + TOOLS (cache anchor, SHA256-locked in tests)
├── agent.py             # stateless Agent.chat(thread_key, user_text, tier)
├── router.py            # OpenRouter primary + OpenAI fallback, caps + cooldowns + cost ledger
├── tools.py             # save_note, search_memory, open/close/list_followup, get_current_time, web_research
├── bot/                 # aiogram 3 dispatcher (auth gate + handlers + typing indicator)
├── memory/              # embed (Voyage), index (FTS5+vec0+diff), search (RRF+rerank), entities, follow_ups
├── scheduler/           # APScheduler wiring + 5 jobs (morning_briefing, email_summary, calendar_pull, brain_reindex, weekly_followup_reconcile)
├── integrations/        # google_auth, gmail, gcal
├── dashboard/           # FastAPI app + single-page HTML
└── util/                # cost (per-model pricing)

migrations/              # 005 SQL files: core schema, FTS5, doc_index, cal_events, scheduler_state
scripts/                 # bootstrap_vps, first_deploy, harden_ssh, tars-backup, setup_backups,
                         # restore_from_backup, inspect_db, make_linux_config, google_oauth_bootstrap
systemd/                 # tars.service, tars-backup.service, tars-backup.timer
tests/unit/              # 46 unit tests (prompt byte stability, router caps, RRF, follow-ups, entities, ...)
```

## Cost ledger and prompt caching

Every LLM call writes one row to `cost_ledger` with provider, model, tier, token counts, cached tokens, and computed USD cost. The system prompt + tool JSON is byte-stable (`tests/unit/test_prompt_byte_stability.py` locks the SHA256), so OpenAI / DeepSeek prefix caches hit reliably. Typical cache hit rate after warm-up: **60-80% of prompt tokens** served from cache at ~25% of the fresh-prompt rate.

If you ever need to change the prompt deliberately:

1. Edit `src/tars/prompt.py`
2. Run `python -c "from tars.prompt import ANCHOR_HASH; print(ANCHOR_HASH)"` to get the new hash
3. Update `EXPECTED_HASH` in `tests/unit/test_prompt_byte_stability.py`
4. Commit both changes in one go

## License

Personal use. No license granted.

## Acknowledgments

Architecture borrowed wholesale from Asaf Saar's "Meet TARS" writeup. Implementation is original.
