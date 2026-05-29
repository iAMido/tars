# TARS — Implementation Plan

> Personal AI agent. One Python process. Telegram + scheduled jobs + dashboard. Inspired by Asaf Saar's "Meet TARS"; details derived from [compass_artifact.md](compass_artifact.md).

---

## Dev environment

- **GitHub MCP wired up.** `claude mcp add github -e GITHUB_PERSONAL_ACCESS_TOKEN=... -- npx -y @modelcontextprotocol/server-github`. Registered for project scope `C:\Users\ido\tars` in `~/.claude.json`. Verify with `claude mcp list` — should show `github: ✓ Connected`. PAT is fine-grained, scoped only to `iAMido/tars`, 90-day expiry — regenerate when it expires.
- After future Claude Code sessions restart, MCP tools like `mcp__github__list_issues`, `mcp__github__create_pull_request`, `mcp__github__get_file_contents` become available — agent can read/write the repo directly without copy-paste.

## Outstanding TODOs (revisit before V1 sign-off)

- [ ] **Rotate the 4 leaked API keys** (Telegram bot token, OpenRouter, OpenAI, Voyage). Keys were briefly visible in the deployment chat transcript. Mitigation in place: OpenAI/OpenRouter spend limits. Risk acceptable for now, must fix before V1 sign-off or before any sensitive data is ingested into the agent.
- [ ] Sharpen TARS voice — system prompt still produces chatty "confirm if changed" tails on retrievals; should be terse/deadpan.

## Progress log

- **Phase 0–4 complete (local Windows dev)** — schema, config loader, LLM router with caps + cooldowns, Telegram bot via aiogram, hybrid memory search (FTS5 + vec0 + RRF + Voyage rerank). 31 unit tests green.
- **Phase 9a complete (deploy)** — TARS now running 24/7 on Hetzner CPX22 Nuremberg under systemd (`tars.service`), polling Telegram from the tailnet, memory retrieval verified live. €11/mo all-in.
- Stuck on int8 vec_docs schema mismatch: voyageai 0.3.7 returns float32 regardless of `output_dtype="int8"`. Switched to `float[1024]`, lost the 4× space savings but unblocked indexing. Documented in commit 892a237.
- Known polish item: TARS voice still too chatty (adds "Confirm if that's changed…" tails). Sharpen system prompt later.

Remaining for V1: Phase 5 (entity store + follow-ups), Phase 6 (APScheduler + morning briefing), Phase 7 (Gmail/Cal OAuth), Phase 8 (dashboard), Phase 9b (restic backups), Phase 10 (polish).

---

## 0. Locked decisions (from the planning conversation)

| Decision | Choice | Why |
|---|---|---|
| **Production host** | Hetzner CX23 Intel/AMD, **NBG-1 Nuremberg**, **Primary IPv4** (€4.49/mo, 2 vCPU / 4GB / 40GB NVMe, 20TB traffic) | Cheapest serious x86 VPS; native filesystem; systemd. Primary IPv4 (+€0.50/mo) chosen over IPv6-only to keep an emergency SSH fallback path independent of Tailscale and avoid IPv4-only-API edge cases. |
| **Dev host** | This Windows 11 machine, Python 3.12 via `uv` | Same code path; deploy to VPS once V1 works |
| **Voice (ElevenLabs)** | **Deferred to v1.1** | Saves $22/mo while iterating; removes the biggest tuning rabbit hole from V1 |
| **Storage** | SQLite + WAL + FTS5 + sqlite-vec, single file | One file, one backup, lock-free coordination via shared event loop |
| **LLM router** | OpenRouter primary, OpenAI direct fallback | User has both accounts; sticky routing for prefix caching |
| **Embeddings** | Voyage `voyage-3-large` int8 1024-dim + `rerank-2.5` | 200M free tokens; beats text-embedding-3-large by ~10% on retrieval |
| **Network** | Tailscale-only ingress for dashboard; long-poll Telegram | No public ports, no TLS to manage |
| **Telegram** | aiogram 3.28.x, single-instance long polling | Modern async lib; no webhook signing |
| **Scheduler** | APScheduler `AsyncIOScheduler` with `SQLAlchemyJobStore` → same SQLite file | Survives restarts; same event loop as bot |
| **Editor mirror** | Syncthing → Obsidian sub-folder | Standard, free, conflict-tolerant |
| **Backups** | Local snapshot + restic→B2 + restic→Hetzner Storage Box | Three destinations, deduplicated |

**Timezone:** `Asia/Jerusalem` (the 05:00 morning briefing fires at 05:00 Israel time; APScheduler handles DST automatically via `zoneinfo`).

**Backup destinations (locked):**
- **Dest 1:** Backblaze B2 (~$0.50/mo at TARS data volume)
- **Dest 2:** Hetzner Storage Box BX11 (€3.49/mo, 1TB, SFTP) — provisioned under a **separate Hetzner account** with a different payment method, so a billing/account lockout on the VPS account doesn't take both VPS and backup-2 down at once

**Fixed monthly cost before any LLM/voice usage:** €4.49 VPS (incl. Primary IPv4) + €3.49 Storage Box + ~$0.50 B2 ≈ **€8.50/mo**.

**Still TBD (will ask in Phase 0):** Telegram bot token, Hetzner accounts (×2), Voyage account, B2 + Storage Box credentials, Tailscale install on VPS.

---

## 1. Scope ladder — what ships when

### V1 — "weekend build" (target: ~2 weekends of focused work)
Text-only TARS that you'd actually use daily.

- Single asyncio process: aiogram + APScheduler + FastAPI sharing one Agent + one SQLite handle
- Frozen system prompt + tool-calling (cache-friendly)
- Tier-routed LLM calls (`interactive_fast`, `cron_default`, `ingest`, `web_research`)
- Tool catalog: `search_memory`, `save_note`, `open_followup`, `close_followup`, `web_research`
- Hybrid retrieval: FTS5 + sqlite-vec + Voyage embed + Voyage rerank with RRF fusion
- Entity store with alias resolution (query "OAI" matches "OpenAI")
- Follow-up lifecycle with citation-gated closure
- **6 scheduled jobs at first:** `morning_briefing` (05:00), `email_summary` (every 30m), `calendar_pull` (every 15m), `brain_docs_reindex` (every 15m), `weekly_followup_reconcile` (Sun 18:00), `backup_snapshot` (06:00)
- Read-only FastAPI dashboard with cost ledger + SSE job stream, bound to tailnet only
- Gmail + Calendar via OAuth installed-app flow (one-time auth on workstation, copy `token.json` to VPS)
- Three-destination restic backups
- Deployed to Hetzner CX23 under systemd

### V1.1 — additions after V1 is stable
- **ElevenLabs voice** (Voice Design, not cloning Bill Irwin)
- Remaining ~12 scheduled jobs: `competitive_intel_scan`, `news_sources_refresh`, `entity_dedup`, `cooldown_clear`, `voice_quota_check`, `health_self_ping`, `restic_b2_push`, `restic_offsite_push`, `stale_thread_summarize`, `lab_notebook_digest`, `cost_rollup_daily`, `vault_sweep`
- `/voice on|off` per-chat toggle
- Daily cache-hit-rate report
- Bluesky / HN / Reddit news ingestion via feedparser + JSON endpoints

### V2 — only if/when V1.1 has been running clean for 1+ month
- Substrate refactor: Agent class stays as façade, body lifted to LangGraph or Hermes Agent IF (and only if) tool catalog grows past ~12 functions
- **Resist LangChain.** Footgun-per-LoC ratio is high; abstractions inject timestamps and burn cache anchors.

**Discipline rule:** every "wouldn't it be cool if…" idea below V2 line goes in `notes/wishlist.md`, not the code.

---

## 2. Account / prerequisite checklist (Phase 0)

Open in parallel, before any code:

| Service | Cost | Purpose | Output to capture |
|---|---|---|---|
| Hetzner Cloud (acct 1) | €4.49/mo | VPS host (CX23 Intel/AMD, NBG-1, Primary IPv4) | API token + project |
| Hetzner Cloud (acct 2) | €3.49/mo | Storage Box BX11 for backup dest 2 | SFTP host + SSH key (separate from VPS root key) |
| Telegram BotFather | Free | Bot token + chat ID | Bot token, your numeric chat ID (via `@userinfobot`) |
| OpenRouter | Existing | Primary LLM | API key, $5+ credit |
| OpenAI Platform | Existing | Fallback LLM | API key, $5+ credit |
| Voyage AI | Free tier | Embeddings + reranker | API key (200M tokens free per model) |
| Google Cloud Console | Existing | Gmail + Calendar OAuth | `client_secret.json` (Desktop app), then `token.json` after first auth |
| Tailscale | Free Personal | Dashboard ingress | Tailnet up; `tailscale ip -4` available on VPS |
| Backblaze B2 | ~$0.50/mo | Backup destination 1 | `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY`, bucket name |
| Syncthing | Free | Vault mirror to Obsidian | Device IDs on VPS and desktop, folder ID |

Total fixed cost before any LLM/voice usage: **~€8.50/mo** (€4.49 VPS + €3.49 Storage Box + ~$0.50 B2).

---

## 3. Architecture (recap, locked)

```
                    Single Python 3.12 asyncio process (systemd: tars.service)

  Telegram ──long-poll──► aiogram Dispatcher ─┐
                                              ├──► Agent (stateless) ──► LLM Router ──► OpenRouter / OpenAI
                          APScheduler ────────┤         │
                          AsyncIOScheduler    │         ▼
                          (~6 jobs in V1)     │     aiosqlite (tars.db, WAL)
                                              │     ├── tables (notes, messages, ...)
  Dashboard ──HTTPS via   FastAPI + SSE ──────┘     ├── FTS5: brain_docs
  tailscale serve         (bound 127.0.0.1)         └── vec0:  vec_docs   (sqlite-vec ext)

  External: Voyage AI (embed+rerank), Gmail/Calendar (OAuth), Syncthing→Obsidian, restic→B2 + Storage Box
```

**Invariants — never violate without a written ADR:**
1. One process. One Agent. One SQLite handle.
2. `uvicorn workers=1`. Forking the scheduler = jobs run N times.
3. Frozen byte-identical system prompt + tool JSON. Never f-string today's date into it.
4. History appended last in the messages array; prefix lives at index 0.
5. Single writer to SQLite (serialize via `asyncio.Lock` if jobs and bot both write). Many readers OK.
6. No public ports. Dashboard bound to `tailscale0` IP or `127.0.0.1` + `tailscale serve`.

---

## 4. Project layout (final V1 shape)

```
~/dev/tars/                       # mirrored at C:\Users\ido\tars on dev box
├── pyproject.toml
├── uv.lock
├── README.md
├── PLAN.md                       # this file
├── compass_artifact.md           # source-of-truth research notes
├── .gitignore
├── deploy.sh                     # WSL/bash script that ssh+pulls+restarts on VPS
├── systemd/
│   └── tars.service
├── scripts/
│   ├── bootstrap_vps.sh          # one-shot VPS install (user, dirs, deps, tailscale)
│   ├── tars-backup.sh            # 3-destination backup script run by APScheduler shell-out
│   └── google_oauth_bootstrap.py # run ONCE on a workstation to mint token.json
├── migrations/
│   ├── 001_initial.sql
│   ├── 002_brain_docs.sql
│   └── 003_vec_docs.sql          # creates vec0 virtual table at runtime (Python-side)
└── src/tars/
    ├── __init__.py
    ├── __main__.py               # asyncio.run(main())
    ├── config.py                 # pydantic-settings reading ~/.tars/config.toml
    ├── db.py                     # aiosqlite, sqlite_vec, migration runner, write lock
    ├── agent.py                  # stateless Agent class
    ├── prompt.py                 # frozen SYSTEM_BLOCK + TOOLS
    ├── router/
    │   ├── __init__.py           # call() dispatcher with caps + cooldowns
    │   ├── tiers.py              # tier->model resolution
    │   ├── openrouter.py
    │   └── openai_provider.py
    ├── memory/
    │   ├── conversations.py
    │   ├── notes.py
    │   ├── entities.py
    │   ├── follow_ups.py
    │   ├── search.py             # hybrid_search() with RRF
    │   └── embed.py              # Voyage embed + rerank with identity fallback
    ├── bot/
    │   ├── handlers.py           # build_dispatcher()
    │   └── (voice.py — V1.1)
    ├── scheduler/
    │   ├── jobs.py               # build_scheduler() — only V1 jobs at first
    │   ├── morning_briefing.py
    │   ├── email_summary.py
    │   ├── calendar_pull.py
    │   ├── brain_reindex.py
    │   ├── followup_reconcile.py
    │   └── backup_snapshot.py
    ├── dashboard/
    │   ├── app.py                # FastAPI
    │   ├── sse.py
    │   └── templates/
    ├── integrations/
    │   ├── gmail.py
    │   ├── gcal.py
    │   └── (news.py, elevenlabs.py — V1.1)
    └── util/
        ├── cost.py               # price_for(model, usage)
        ├── time.py               # tz-aware now, formatters
        └── (audio.py — V1.1)
└── tests/
    ├── unit/
    │   ├── test_router_caps.py
    │   ├── test_followup_lifecycle.py
    │   ├── test_hybrid_search_rrf.py
    │   └── test_prompt_byte_stability.py    # CRITICAL: locks the cache anchor
    └── integration/
        ├── test_agent_smoke.py
        └── test_scheduler_persistence.py
```

---

## 5. Phased build — concrete, ordered, with acceptance criteria

Each phase ends with a green-light checklist. Don't move on until all boxes check.

### Phase 0 — Bootstrap & accounts (½ day)
**Deliverables:** all accounts in §2 opened; secrets captured in a password manager; `~/.tars/config.toml` exists on dev box (0600); git repo initialized (already done); first commit with `PLAN.md`, `compass_artifact.md`, `.gitignore`, empty `src/tars/`.

**Done when:**
- [ ] `uv --version` works on Windows
- [ ] `python -c "import sqlite3; print(sqlite3.sqlite_version)"` ≥ 3.42 (FTS5 + JSON1 ship by default in modern Python builds; check with `import sqlite3; sqlite3.connect(':memory:').execute("PRAGMA compile_options").fetchall()` and confirm `ENABLE_FTS5` is present — if not, install Python via the official installer rather than MS Store)
- [ ] Telegram bot responds to `/start` (with a one-liner test handler)
- [ ] OpenRouter `curl` smoke test returns a chat completion
- [ ] Tailscale installed on dev box, can ping the (future) VPS once provisioned

### Phase 1 — Skeleton: config + db + migrations (½ day)
**Files:** `pyproject.toml`, `src/tars/{__main__,config,db}.py`, `migrations/001_initial.sql`, `migrations/002_brain_docs.sql`.

**Implements:**
- `pydantic-settings` config loader reading `~/.tars/config.toml` (or `$TARS_CONFIG`)
- `Database` class: aiosqlite + sqlite-vec extension load + WAL pragmas + busy_timeout 5000 + migration runner via `schema_versions` table
- Migrations 001 (core tables) + 002 (FTS5 `brain_docs`)
- Programmatic create of `vec0` virtual table at startup (cannot be in static .sql because extension load must precede CREATE)
- Global `asyncio.Lock` exposed as `db.writer_lock` (used by anything that writes)

**Done when:**
- [ ] `python -m tars` boots, prints "DB migrated to version N", exits clean
- [ ] `sqlite3 tars.db ".tables"` shows all 9 tables + `brain_docs` + `vec_docs`
- [ ] Second boot is idempotent (no re-run of migrations)
- [ ] `pytest tests/unit/test_db_migrations.py` — runs migrations twice on a tmp DB, asserts row counts unchanged

### Phase 2 — Frozen prompt + Agent skeleton + LLM router (1 day)
**Files:** `prompt.py`, `agent.py`, `router/{__init__,tiers,openrouter,openai_provider}.py`, `util/cost.py`.

**Implements:**
- `SYSTEM_BLOCK` and `TOOLS` as module constants. JSON serialized with `sort_keys=True, separators=(",", ":")` for the audit log.
- `Agent.chat(thread_key, user_text, tier="interactive_fast", tool_loop_max=4)` — loads history, builds messages = `[system] + history + [user]`, calls router, runs the tool loop, persists every turn.
- Tier table (from config, not hard-coded):
  - `interactive_fast` → `openai/gpt-5-mini`
  - `cron_default` → `deepseek/deepseek-v3.2`
  - `ingest` → `deepseek/deepseek-v3.2`
  - `web_research` → `openai/gpt-5`
- Router: OpenRouter primary, OpenAI fallback. Per-provider daily-spend cap, 60s cooldown on HTTP 5xx/429, `CircuitOpen` exception when all tripped.
- `cost_ledger` row written on every call, with `cached_tokens` from `usage.prompt_tokens_details.cached_tokens`.
- Tool implementations: only `save_note` + `search_memory` stubs in this phase (real `search_memory` arrives in Phase 4).

**Critical test — DO NOT SKIP:**
```python
# tests/unit/test_prompt_byte_stability.py
def test_system_block_is_module_constant():
    from tars.prompt import SYSTEM_BLOCK, TOOLS_JSON
    expected_hash = "<sha256 captured the first time>"
    actual = hashlib.sha256((SYSTEM_BLOCK + TOOLS_JSON).encode()).hexdigest()
    assert actual == expected_hash, "Cache anchor changed — every PR re-warms the prompt cache"
```
Update the hash deliberately when you intend to change the prompt; let CI fail otherwise.

**Done when:**
- [ ] `Agent.chat("test:1", "say 'pong'")` returns text + writes 1 row to `cost_ledger`
- [ ] Second identical call reports `cached_tokens > 0` (may take a couple seconds for DeepSeek to warm; OK on 3rd try)
- [ ] Router unit tests pass: caps trip after fake spend exceeds cap; cooldown blocks for 60s after a fake 503; `CircuitOpen` raised when both providers tripped
- [ ] Byte-stability test passes

### Phase 3 — Telegram bot (½ day)
**Files:** `bot/handlers.py`, wire into `__main__.py`.

**Implements:**
- Authorization: `m.chat.id in cfg.telegram.allowed_chat_ids` (hard-coded list; no rate limiter needed for a 1-user system)
- Handlers: `/start`, `/research <q>` (tier=`web_research`), `note: <body>` regex (calls `save_note` directly without LLM), free chat (tier=`interactive_fast`)
- Long polling via `dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())`

**Done when:**
- [ ] Sending "hello" to the bot returns an LLM response in <5s
- [ ] `note: bought milk` returns `Noted. [note:N]` and the row is in `notes`
- [ ] Sending from an unauthorized chat returns silence (not an error)

### Phase 4 — Hybrid retrieval + memory layer (1 day)
**Files:** `memory/{search,embed,notes,conversations}.py`, `migrations/003_vec_docs.sql` (Python-side bootstrap).

**Implements:**
- `Embedder` wrapping Voyage `voyage-3-large` (int8, 1024-dim) and `rerank-2.5` with identity fallback (rate-limit safety)
- `brain_docs` reindexer that iterates `notes`, `messages` (assistant turns ≥ 200 chars), `briefings` and upserts (doc_id, source, title, body, tags) into FTS5
- Vector upsert: pack int8 list into bytes with `struct.pack(f"{len(v)}b", *v)` — note the `b` for int8, not `f`
- `hybrid_search(query, k=8)`: CTE with FTS5 + vec0, RRF merge (`rrf_k=60`), then Voyage rerank top-25 → top-`k`
- Wire `search_memory` tool to `hybrid_search` so the LLM can actually use it

**Done when:**
- [ ] Add 20 sample notes via `note: ...`. `Agent.chat("test:2", "find the note about X")` retrieves correctly via tool call
- [ ] `pytest tests/unit/test_hybrid_search_rrf.py` — golden test with synthetic docs verifies RRF ordering matches hand-computed expected output
- [ ] Reranker fallback fires when API key is intentionally bad (test isolates this)

### Phase 5 — Follow-ups + entity store (½ day)
**Files:** `memory/{follow_ups,entities}.py`, wire `open_followup`/`close_followup` tools, post-note entity extraction call at `cron_default` tier.

**Implements:**
- `open_followup(note_id, due_at_iso, to=None)` — inserts row
- `close_followup(followup_id, resolving_note_id)` — **citation-gated**: refuses without a valid resolving note
- After every `save_note`, async-fire an entity-extraction call; upsert into `entities` + `entity_aliases`; conflict on `canonical` is a no-op
- Query expansion: when `search_memory` runs, if any token in the query matches an alias, also OR the canonical form into the FTS5 query

**Done when:**
- [ ] `Agent.chat(..., "remind me to ping Alice next Tuesday")` → opens a follow-up
- [ ] Closing without a resolving note raises ValueError surfaced as a tool error
- [ ] Search for "OAI" returns notes mentioning "OpenAI" (after a manual alias seed)

### Phase 6 — Scheduled jobs (V1 set: 6 jobs) (1 day)
**Files:** `scheduler/jobs.py` + one file per job.

**Implements (V1 only):**
- `morning_briefing` — Cron 05:00 — pulls last-12h Gmail, next-5 calendar items, top-5 open follow-ups, news (V1.1: tracked-domain RSS, V1: empty list); composes with `cron_default`; persists briefing; sends to Telegram
- `email_summary` — every 30m — pulls unread Gmail since last run, summarizes if ≥3 new threads
- `calendar_pull` — every 15m — caches next 50 events into `cal_events` table (add this in migration 004)
- `brain_docs_reindex` — every 15m — diff-mode reindex (only since last run); writes a tombstone for deleted docs
- `weekly_followup_reconcile` — Sun 18:00 — reopen any follow-up whose `due_at` passed without a closed note; ping Telegram with the list
- `backup_snapshot` — 06:00 — shell-out to `scripts/tars-backup.sh`; V1 keeps local snapshots only; V1.1 enables B2 + Storage Box

**Scheduler config (LOCKED):**
```python
AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{cfg.paths.db}")},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
    timezone=cfg.timezone,
)
```

**Done when:**
- [ ] All 6 jobs registered, visible via `sched.get_jobs()`
- [ ] Restart the process — jobs persist (read `apscheduler_jobs` table)
- [ ] Force-run morning briefing via dashboard endpoint → message arrives in Telegram, `briefings` row inserted, `cost_ledger` row inserted with tier=`cron_default`
- [ ] `pytest tests/integration/test_scheduler_persistence.py` — fakes a missed window, asserts the job runs once (coalesce) within grace time

### Phase 7 — Gmail + Calendar integration (½ day)
**Files:** `integrations/{gmail,gcal}.py`, `scripts/google_oauth_bootstrap.py`.

**Implements:**
- One-time OAuth flow on workstation → `token.json` (chmod 600, copy to VPS `~/.tars/google_token.json`)
- `gmail.fetch_unread_since(ts)` returns simplified `[{from, subject, snippet, body, ts}]`
- `gcal.fetch_upcoming(n=50)` returns `[{title, start, end, attendees, location}]`
- Token refresh handled by `google-auth` — no manual reauth needed

**Done when:**
- [ ] `python -m tars.scripts.gmail_test` lists last 5 unread subjects
- [ ] `python -m tars.scripts.gcal_test` lists next 5 events
- [ ] Auto-refresh works after token expires (test by hand-editing `token.json` expiry to past)

### Phase 8 — FastAPI dashboard (½ day)
**Files:** `dashboard/app.py`, `dashboard/sse.py`, `dashboard/templates/index.html` (single page, vanilla JS — no build step).

**Endpoints:**
- `GET /` — HTML page: cost-per-day chart (last 30d), open follow-ups, today's briefing
- `GET /api/costs` — costs grouped by date + tier + model + cached_tokens %
- `GET /api/jobs/stream` — SSE stream of `jobs` table state every 1s
- `GET /api/search?q=...` — proxy to `hybrid_search` (read-only)
- `POST /api/force_run/{job_id}` — manually trigger a scheduled job (auth via Tailscale identity headers, see §9)

**Done when:**
- [ ] `curl http://100.x.y.z:8088/api/costs` returns JSON
- [ ] Dashboard loads in browser via Tailscale; cost chart renders
- [ ] Force-run button triggers the job and the SSE stream updates within 2s

### Phase 9 — Production deploy to Hetzner CX23 (1 day)
**Files:** `systemd/tars.service`, `scripts/bootstrap_vps.sh`, `deploy.sh`.

**`bootstrap_vps.sh` (run once as root on a fresh CX23):**
1. `apt update && apt upgrade -y`
2. Create `tars` user with no shell login, only ssh key
3. Install deps: `python3.12 python3.12-venv ffmpeg sqlite3 restic chrony git curl ufw`
4. UFW: deny incoming, allow only ssh + tailscale interface
5. Install Tailscale, `tailscale up --ssh --advertise-tags=tag:tars-vps`
6. Install Syncthing as `tars` user, systemd-user enable
7. Clone repo to `/home/tars/tars`, `uv sync --frozen`
8. Drop `tars.service` into `/etc/systemd/system/`, enable + start
9. Drop backup cron via systemd timer (not APScheduler) for the restic push — APScheduler shells out only to the local-snapshot script

**`tars.service`:**
```ini
[Unit]
Description=TARS agent
After=network-online.target syncthing@tars.service
Wants=network-online.target

[Service]
Type=simple
User=tars
Group=tars
WorkingDirectory=/home/tars/tars
Environment=TARS_CONFIG=/home/tars/.tars/config.toml
ExecStart=/home/tars/.local/bin/uv run python -m tars
Restart=on-failure
RestartSec=5
StandardOutput=append:/home/tars/logs/tars.log
StandardError=append:/home/tars/logs/tars.err
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/home/tars
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

**`deploy.sh` (run from dev box):**
```bash
#!/usr/bin/env bash
set -euo pipefail
git push origin main
ssh tars@tars-vps <<'EOF'
  set -euo pipefail
  cd ~/tars
  git fetch --all && git reset --hard origin/main
  uv sync --frozen
  sudo systemctl restart tars
  sleep 3
  systemctl status tars --no-pager | head -20
EOF
```

**Done when:**
- [ ] `systemctl status tars` → active (running)
- [ ] Telegram bot responds when running on VPS
- [ ] `tailscale serve --bg --https=443 http://127.0.0.1:8088` → dashboard reachable at `https://tars.<tailnet>.ts.net` from your laptop
- [ ] Reboot the VPS — `tars.service` comes back up automatically; missed-window jobs coalesce-fire once within 10 min
- [ ] Three-destination backups: `restic snapshots` lists at least one snapshot in B2 and one in Storage Box

### Phase 10 — Polish & V1 sign-off (½ day)
- Add structlog with JSON output to file
- Add `/health` endpoint returning `{"db_ok": ..., "last_briefing_ts": ..., "jobs_overdue": N}`
- Document one-page runbook in README.md: how to restart, where logs live, how to restore from backup, how to rotate keys

**V1 ship criteria:**
- [ ] Has been running on the VPS for **7 consecutive days** with no crash
- [ ] Morning briefing delivered 7/7 days
- [ ] At least one successful restore test from B2 to `/tmp/restore-test`
- [ ] Daily LLM spend ≤ $1.50/day for a normal-use week
- [ ] Cache hit rate ≥ 60% on the interactive tier (visible in dashboard)

---

## 6. Schema (final V1 — locked unless a test forces a change)

See `migrations/001_initial.sql` and `migrations/002_brain_docs.sql` in [compass_artifact.md](compass_artifact.md). Additions for V1:

```sql
-- migrations/004_cal_events.sql
CREATE TABLE IF NOT EXISTS cal_events (
  ical_uid TEXT PRIMARY KEY,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  title TEXT NOT NULL,
  attendees JSON,
  location TEXT,
  payload JSON,
  fetched_at INTEGER NOT NULL
);
CREATE INDEX idx_cal_start ON cal_events(start_ts);

-- migrations/005_news_sources.sql (V1.1 — placeholder)
-- (Not in V1 since we defer competitive intel)
```

**Indexes earned, not assumed:** add only the two indexes above + the ones already in 001 (`idx_messages_thread_ts`, `idx_cost_ts`). Add more only when EXPLAIN QUERY PLAN proves they're needed.

---

## 7. Risk register & mitigations (the things that bite)

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Prompt cache silently goes to 0% (someone f-strings a date in) | High | Medium | Byte-stability unit test + daily dashboard panel showing cached-token % |
| SQLite write contention between scheduler + bot | Medium | High | Single `asyncio.Lock` on the `Database` object guards every write; many readers OK |
| Event loop blocked by Voyage local fallback or large JSON parse | Medium | Medium | Wrap CPU-heavy work in `asyncio.to_thread`; APScheduler `ThreadPoolExecutor` for any blocking job |
| OAuth `token.json` revoked (e.g., 6-month inactivity, password change) | Medium | High | Health endpoint pings Gmail every hour; alert via Telegram if 401; one-time re-auth on workstation regenerates token |
| Hetzner VM clock drift | Medium | Low | Install `chrony` in `bootstrap_vps.sh` |
| `sqlite-vec` breaking change on pre-1.0 release | Low | High | Pin `sqlite-vec==0.1.6` (or whichever version V1 tests against); never auto-upgrade |
| `uvicorn workers > 1` accidentally introduced | Low | Catastrophic (jobs run N times) | systemd unit hard-codes `workers=1`; comment in code; integration test asserts only one scheduler instance |
| Telegram voice notes > 1MB once V1.1 adds voice | High (V1.1) | Low | Truncate briefing TTS to 1500 chars; split into multiple voice messages if needed |
| ElevenLabs ToS violation if I'm tempted to clone Bill Irwin (V1.1) | Low | Catastrophic (account ban + legal) | **Use Voice Design only.** Voice prompt locked in `compass_artifact.md` §5.k |
| Restic backup script fails silently | Medium | Catastrophic | Backup script exits non-zero on any restic error; systemd `OnFailure=` emails me; weekly restore-test job |
| OpenRouter sticky routing drops me to a non-cached provider | Medium | Medium (cost spike, not outage) | Daily cost alarm at 2× expected; manual fallback via `cfg.tiers` edit |
| LLM hallucinates a closed follow-up | High | Medium | Citation-gated `close_followup` (refuses without resolving_note_id); also reopen-tracker job double-checks weekly |
| I get curious and bolt on LangChain | Medium | High | Hard rule in §1: V2 only, and even then resist. Re-read this risk row before adding any framework. |

---

## 8. Cost target (V1, no voice)

| Bucket | Expected | Hard cap (alert) |
|---|---|---|
| Hetzner CX23 (Primary IPv4) + Storage Box BX11 | €7.98/mo | n/a (fixed) |
| Backblaze B2 (~5GB after 6 months) | $0.50/mo | n/a (fixed) |
| OpenRouter cron (DeepSeek V3.2, ~5M tokens/mo) | $2-3 | `daily_cap_usd=5` in router |
| OpenRouter interactive (gpt-5-mini, ~1M tokens/mo) | $1-2 | included in cap |
| OpenRouter web research (gpt-5, ~200K tokens/mo) | $1 | separate per-tier cap (TODO: enforce in router) |
| Voyage embed + rerank | $0 (under free tier) | n/a |
| OpenAI direct fallback | <$1 | `daily_cap_usd=2` |
| **Total V1** | **~$13-15/mo** | hard alarm at $25/mo |

V1.1 adds ~$22/mo for ElevenLabs Creator + ~$5-15 voice overage if I'm prolific.

---

## 9. Security posture (single-user, but still)

- `~/.tars/` → 0700; `~/.tars/config.toml` → 0600; owner `tars`
- `token.json` (Google OAuth) → 0600
- `restic.pw` → 0600, owned by `root`, read by backup script via sudoers entry
- Bot only responds to `cfg.telegram.allowed_chat_ids` — my numeric chat ID, hard-coded
- Dashboard: bind to `127.0.0.1:8088`, expose via `tailscale serve --bg --https=443 http://127.0.0.1:8088`. Use Tailscale identity headers (`Tailscale-User-Login`) for the force-run endpoint; reject if header is absent or doesn't match my login
- UFW: default deny incoming; allow only `tailscale0` interface and outbound; ssh restricted to Tailscale IP range
- Secret rotation: edit `config.toml`, `systemctl restart tars`. No code changes.
- `.gitignore` already covers: `.env`, `*.db*`, `vault/`, `backups/`, `.tars/`, `token.json`, `client_secret*.json`

---

## 10. Self-review of this plan

I read back through this plan against the artifact and against my own assumptions. Where it could break:

1. **`workers=1` invariant.** The plan asserts this in §3, the systemd unit, and the risk register. Triple-mention is intentional — single biggest footgun.
2. **Sqlite-vec int8 packing.** I corrected the artifact's example: it uses `struct.pack(f"{len(v)}f", *v)` (float32), but Voyage int8 output should be `b` (signed char). Verify with `vec_version()` and a tiny round-trip test in Phase 4. **Action item: confirm sqlite-vec's expected byte layout for FLOAT[1024] vs INT8[1024] virtual-table column types — may need to declare `INT8[1024]` instead. Check sqlite-vec docs in Phase 4 before writing the migration.**
3. **APScheduler `SQLAlchemyJobStore` writing to the same SQLite file.** This means APScheduler holds its own connection alongside aiosqlite. With WAL + busy_timeout this is generally fine, but contention is possible. Falls under risk-register row "write contention". Mitigation: APScheduler in V1 fires 6 jobs/day at most — contention near zero. If V1.1 traffic grows, move APScheduler's jobstore to a separate `tars_sched.db` file.
4. **The frozen prompt invariant vs. tool list growth.** Every time I add a tool, the prefix changes and cache warms from scratch. Acceptable, but means I should batch tool-catalog changes into single deploys, not drip them in.
5. **OAuth token.json portability.** Step 7 says generate on workstation, copy to VPS. Google's docs warn that some OAuth flows tie tokens to the originating IP; tested on personal accounts this isn't an issue, but if Phase 7 surfaces an "invalid_grant" the fallback is to run the OAuth flow via X forwarding on the VPS once.
6. **Tailscale Serve HTTPS** requires HTTPS-enabled tailnet. Free Personal tailnets support this, but it must be enabled in admin console; bootstrap_vps.sh should print a reminder, not assume it's on.
7. **Telegram chat-ID allowlist.** Hard-coding my chat ID means if I switch phones/Telegram accounts I'm locked out. Acceptable for V1. Add a `/whoami` debug handler that prints `m.chat.id` so I can recover.
8. **The artifact says "DeepSeek V4 Flash" but the current model is V3.2.** Plan uses V3.2 (per artifact's own §10 caveat). Re-check OpenRouter model catalog in Phase 2 — pricing or model names may have shifted between artifact's date (May 2026) and now.
9. **V1 scope honestly assessed.** I could ship V1 in two focused weekends if no surprises. Realistic estimate is 3-4 weekends including OAuth setup, debugging cache hits, and one round of VPS reinstall after a config mistake. Don't promise myself "weekend project" beyond that.
10. **What's missing that might matter:**
    - **Log rotation.** Added `StandardOutput=append:` to systemd unit but no logrotate config. Add `/etc/logrotate.d/tars` in `bootstrap_vps.sh`.
    - **Health check / dead-man switch.** `health_self_ping` job is V1.1, but a totally crashed process won't send the alert. Hook up an external uptime ping (Healthchecks.io free tier) called by `morning_briefing` — if the daily ping is missed, I get an email.
    - **DB write lock pattern.** I assert `asyncio.Lock` but should verify aiosqlite already serializes on a single connection — it does, since one connection = one writer thread. Multiple connections (APScheduler's separate one!) is the actual risk. Re-validate Phase 6.

These ten items are the residual list. None block starting Phase 0; items 2, 5, 8, 10a, and 10b need verification *during their respective phase*, not before.

---

## 11. What I need from you to start Phase 0

- Confirm Hetzner CX23 region preference (Falkenstein DE = lowest latency for EU; Helsinki FI; Hillsboro / Ashburn for US)
- Confirm timezone for the morning briefing (`Asia/Jerusalem`? `Europe/Berlin`? `America/New_York`?)
- Decide: will you use the Hetzner Storage Box for backup destination 2, or a different second-region target (Wasabi, Storj)?

Reply with those three and I'll move to Phase 0 implementation.
