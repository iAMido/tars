# Building Your Own TARS: A Hands-On Replication Guide

**TL;DR**
- Build TARS as a single asyncio process combining aiogram 3.28.2 (Telegram), APScheduler, and FastAPI, all sharing one Agent instance and one SQLite handle with FTS5 plus sqlite-vec for hybrid retrieval; deploy to a Hetzner CX22 VPS at €3.79/month for the simplest path that still works, with Fly.io as the cleanest cloud-PaaS alternative.
- Route LLM traffic through OpenRouter (DeepSeek V3.2 for cron/ingest at $0.26 in / $0.38 out per 1M tokens; gpt-5-mini for interactive at $0.25 in / $2.00 out per 1M tokens), with OpenAI direct as fallback, a frozen byte-stable system prompt as cache anchor, and Voyage `voyage-3-large` embeddings (first 200M tokens free, then $0.18/1M) plus `rerank-2.5` at $0.05/1M for retrieval.
- For the TARS-from-Interstellar voice, do NOT clone Bill Irwin via Instant Voice Clone (ToS and right-of-publicity violation); instead use ElevenLabs Voice Design with a deadpan-baritone prompt and ship audio via `eleven_flash_v2_5` (under 75ms first audio, 0.5 credit per character), encoded to OGG/Opus for Telegram `sendVoice`. Total operating cost: roughly $25-$45/month at personal usage.

---

## 1. Architecture Overview

### Topology

```
                          ┌─────────────────────────────────┐
                          │  Single Python process (asyncio)│
                          │                                 │
   Telegram ──polling──►  │  ┌───────────┐    ┌──────────┐  │
                          │  │ aiogram   │    │ FastAPI  │ ◄──── Tailnet only
                          │  │ Dispatcher│    │ + SSE    │       (tailscale0)
                          │  └─────┬─────┘    └────┬─────┘  │
                          │        │                │       │
                          │        ▼                ▼       │
                          │     ┌────────────────────┐      │
                          │     │   Agent (stateless)│      │
                          │     │   tier-routed LLM  │      │
                          │     └─────────┬──────────┘      │
                          │               │                 │
                          │  ┌────────────┴─────────────┐   │
                          │  │ APScheduler (~18 jobs)   │   │
                          │  └────────────┬─────────────┘   │
                          │               │                 │
                          │       ┌───────▼─────────┐       │
                          │       │   aiosqlite     │       │
                          │       │ tars.db + WAL   │       │
                          │       │ FTS5 + sqlite-vec│      │
                          │       └─────────────────┘       │
                          └────────────┬────────────────────┘
                                       │
            ┌──────────────┬───────────┼──────────┬─────────────────┐
            ▼              ▼           ▼          ▼                 ▼
       OpenRouter      Voyage AI   ElevenLabs   Gmail API    Syncthing→Obsidian
       (LLM router)    (embed+rerank) (TTS)    Calendar API     vault
                                                                   │
                       ┌───────────────────────────────────────────┘
                       ▼
   Backups (3 destinations): on-host snapshot dir + restic → B2 + restic → second offsite
```

### Components and responsibilities

- **aiogram dispatcher**: long-polls Telegram, routes incoming updates to handlers (`/start`, free chat, `note: ...`, `/voice on|off`, `/research <q>`). Calls Agent with thread key `tg:{chat_id}`.
- **Agent (stateless)**: pure function-style class. Receives a thread key, loads history from SQLite, assembles the frozen-prefix prompt, calls the LLM router, persists assistant turn and any tool outputs, returns text.
- **LLM router**: tier-aware (`interactive_fast`, `cron_default`, `ingest`, `web_research`). OpenRouter primary, OpenAI direct fallback. Enforces per-provider daily spend caps and 60-second cooldowns on 5xx/429.
- **Memory layer (SQLite)**: one file, WAL mode, accessed via `aiosqlite`. Tables for `notes`, `conversations`, `messages`, `briefings`, `entities`, `entity_aliases`, `follow_ups`, `jobs`, `cost_ledger`. Two virtual tables: `brain_docs` (FTS5) and `vec_docs` (sqlite-vec).
- **APScheduler `AsyncIOScheduler`**: shares the same event loop as aiogram and FastAPI. Persistent jobstore is the same SQLite file via `SQLAlchemyJobStore`, so missed jobs are picked up after restart.
- **FastAPI dashboard**: read-only views over `cost_ledger`, `conversations`, `brain_docs`. Server-Sent Events for in-flight jobs. Bound to `tailscale0` interface only.
- **Hybrid retrieval**: `voyage-3-large` (1024-dim int8) for embeddings, BM25 via FTS5, Reciprocal Rank Fusion merge, then `rerank-2.5` second pass.
- **Voice layer**: ElevenLabs `eleven_flash_v2_5` synthesizes a designed (not cloned) TARS-style voice, output is converted to OGG/Opus with ffmpeg `libopus`, sent via Telegram `sendVoice`.
- **Vault mirror**: Syncthing watches `~/tars/vault/` and propagates to a desktop Obsidian vault sub-folder.
- **Network**: Tailscale is the only ingress path. No public ports. Optional Tailscale Serve for HTTPS termination over MagicDNS.

### Why each choice

- **One process, one Agent, one SQLite**: shared state without IPC, lock-free coordination via the single event loop, easy backup (one file plus its WAL). Asaf's piece is explicit: "No cron daemon, no IPC, no message queue. Everything that needs state goes through the same object graph."
- **Stateless Agent class**: lets the same Agent serve Telegram, scheduled jobs, and the dashboard chat without history bleed; thread keys (`tg:123`, `job:morning_briefing`, `web:asaf`) namespace conversations.
- **Frozen system prompt**: prefix caching on OpenRouter, OpenAI, DeepSeek, and Anthropic is keyed on byte-identical prefixes. Per OpenRouter docs, "OpenRouter uses provider sticky routing to maximize cache hits… requests that share the same opening messages are routed to the same provider." Drift the prompt and you pay full prefill every turn.
- **SQLite over Postgres**: a single embedded file is durable, fast for under a few million rows, and trivially backed up with the online `.backup` API. FTS5 ships in the standard library; sqlite-vec is one shared library.
- **Tailscale-only ingress**: no auth code, no public TLS, no rate-limiter. Single-user system, single network. As Asaf notes: "If you ever need multi-user, you can refactor. You probably won't."
- **Voyage over OpenAI embeddings**: `voyage-3-large` outperforms `text-embedding-3-large` by 9.74% across 100 retrieval datasets per Voyage's own published benchmarks, with the first 200M tokens free per account. `rerank-2.5` adds an instruction-following second pass.

---

## 2. Hosting Evaluation

| Option | Realistic monthly cost | Persistent SQLite | Long-running asyncio | Scheduler reliability | Tailscale | Deploy/rollback | Verdict |
|---|---|---|---|---|---|---|---|
| **Hetzner CX22 VPS** (2 vCPU, 4GB, 40GB NVMe) | €3.79 (~$4.20) + ~€0.76 backups | Native filesystem, ideal for WAL | systemd unit, no surprises | Native, sub-second drift | First-class | git push + systemctl restart | **Best overall** |
| **Fly.io** (shared-cpu-1x, 1GB) | $2.47/mo machine ($0.88 compute + $1.59 RAM) plus $0.45/mo for a 3GB volume plus bandwidth (Fly's official calculator) | Volume = $0.15/GB-mo | Yes, Firecracker VM stays warm | Yes, but VM restarts after deploys | Works via userspace `tsnet` or sidecar | `fly deploy`, fast rollback | **Best cloud PaaS path** |
| **Railway Hobby** | $5 base + credit usage (~$8-15 typical) | Volumes supported on Hobby plan | Yes | Yes | Workable via userspace `tsnet` | Git-push, one-click rollback | Simple, but credit-based costs creep |
| **Render** | $7 web service + $0.25/GB-mo disk | Disk add-on persistent | Yes | Yes (paid tier) | Userspace only | Git-push, rollback UI | Acceptable, no real cost win |
| **Home server (Mac mini / Pi 5)** | $0 marginal | Local SSD, native | Yes | Native | First-class | Manual rsync or git pull | Cheapest, ISP-dependent uptime |

### Cost details

- **Hetzner CX22**: €3.79/mo for 2 vCPU, 4GB RAM, 40GB NVMe, 20TB traffic, in Germany or Finland (US locations roughly 20% higher, with as low as 1TB traffic instead of 20TB). Backups add roughly 20%.
- **Fly.io**: pricing has been usage-based since October 2024. Per Fly's own calculator, a `shared-cpu-1x@1024MB` machine running 730 hrs/month is $2.47 ($0.88 compute + $1.59 memory). Volumes are $0.15/GB-mo, outbound bandwidth $0.02/GB in NA/EU. Realistic single-region, single-machine TARS deploy lands at ~$4-6/mo.
- **Railway**: Hobby plan starts at $5/mo and bills per-second on CPU/RAM consumed beyond the included credits. Volumes are now supported. The 2025-2026 reliability track record has been mixed; Northflank's blog explicitly notes a December 2025 incident that paused builds across all plan tiers in Railway's EU West region.
- **Render**: starter web service is $7/mo with no free-tier sleep for paid services. No real upside for this workload.
- **Home server**: a refurbished M1 Mac mini (~$400 once) or Raspberry Pi 5 8GB (~$80) gives you a permanently free runtime. Power draw under 10W. Downsides: residential ISP, dynamic IP (mitigated by Tailscale), heat/dust.

### Recommendation

**Hetzner CX22.** It is the simplest path that still works, because nothing about TARS requires the abstractions Railway and Fly add. You get root, systemd, a real filesystem, and an easy `restic` backup story for €4. The hidden cost on PaaS is the time spent fighting opinions: Railway and Fly both want you to pretend the filesystem is ephemeral, which is the opposite of what a SQLite-first agent wants.

If you must use a PaaS, choose **Fly.io** over Railway. Persistent volumes are first-class, `flyctl ssh` gives you a shell that feels like a VPS, and the pricing is transparent (the calculator above lets you predict the bill to the cent). Pin to one region (`primary_region = "fra"`), one machine, never let Fly autoscale to two replicas: SQLite cannot survive that without LiteFS, which is a bigger detour than just running on Hetzner.

**Use a home server only if you already have one running 24/7.** TARS earns its keep at 5 AM, and a dropped ISP connection is exactly when you will find out. With Tailscale, the dynamic IP problem disappears, but the power-blip problem does not.

---

## 3. Prerequisites and Accounts

| Service | Signup | Free tier / starting cost | Notes |
|---|---|---|---|
| Telegram Bot | t.me/BotFather | Free, unlimited | Save the token in `~/.tars/config.toml`. |
| OpenRouter | openrouter.ai | Pay-as-you-go, $5 minimum credit | Provides DeepSeek V3.2 at $0.26/$0.38 per 1M, gpt-5-mini at $0.25/$2.00 per 1M, and sticky-routing prefix caching. |
| OpenAI | platform.openai.com | $5 minimum | Direct fallback for the router. |
| Voyage AI | voyageai.com | 200M free tokens (voyage-3-large), then $0.18/1M; rerank-2.5 also 200M free, then $0.05/1M | Reranker rate-limits hard on the free tier; identity fallback in the code. |
| ElevenLabs | elevenlabs.io | Free 10k credits/mo (no commercial license); Starter $5/mo; Creator $22/mo for 100k credits | Use Voice Design, not IVC, for TARS. Flash v2.5 is 0.5 credit/char. |
| Tailscale | tailscale.com | Free Personal: 3 users, 100 devices | All you need. |
| Syncthing | syncthing.net | Free, self-hosted | Run the daemon on both VPS and desktop; share one folder. |
| Hetzner Cloud | hetzner.com/cloud | None | Card required, EU billing. |
| Google Cloud project | console.cloud.google.com | Free | For Gmail and Calendar OAuth. |
| Domain (optional) | any registrar | $10-15/yr | Only needed for a custom MagicDNS alias; the `*.ts.net` name works. |

---

## 4. Tech Stack and Project Structure

### Versions and libraries

- **Python 3.12** (3.11 minimum; 3.12 has the better asyncio task-group story and a faster `sqlite3` module).
- **uv** for dependency and virtualenv management. `uv` is dramatically faster than pip/poetry and the lockfile is cleaner.

```toml
# pyproject.toml
[project]
name = "tars"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "aiogram>=3.28.2",
  "apscheduler>=3.11",
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "aiosqlite>=0.20",
  "sqlite-vec>=0.1.6",
  "httpx>=0.27",
  "pydantic>=2.9",
  "pydantic-settings>=2.6",
  "tomli>=2.0",
  "structlog>=24.4",
  "voyageai>=0.3",
  "elevenlabs>=1.10",
  "google-api-python-client>=2.150",
  "google-auth-oauthlib>=1.2",
  "feedparser>=6.0",
  "tenacity>=9.0",
  "sse-starlette>=2.1",
]
```

### Directory layout

```
~/dev/tars/
├── pyproject.toml
├── README.md
├── deploy.sh
├── systemd/
│   └── tars.service
├── migrations/
│   ├── 001_initial.sql
│   ├── 002_brain_docs.sql
│   └── ...
├── src/tars/
│   ├── __main__.py            # asyncio.run(main())
│   ├── config.py              # loads ~/.tars/config.toml
│   ├── db.py                  # aiosqlite pool, migration runner
│   ├── agent.py               # stateless Agent
│   ├── prompt.py              # frozen prefix, tool schemas
│   ├── router/
│   │   ├── __init__.py
│   │   ├── tiers.py
│   │   ├── openrouter.py
│   │   └── openai.py
│   ├── memory/
│   │   ├── conversations.py
│   │   ├── notes.py
│   │   ├── entities.py
│   │   ├── follow_ups.py
│   │   ├── search.py          # hybrid retrieval
│   │   └── embed.py           # voyage + rerank
│   ├── bot/
│   │   ├── handlers.py
│   │   └── voice.py
│   ├── scheduler/
│   │   ├── jobs.py
│   │   ├── morning_briefing.py
│   │   ├── followup_reconcile.py
│   │   ├── competitive_intel.py
│   │   ├── brain_reindex.py
│   │   ├── calendar_pull.py
│   │   └── email_summary.py
│   ├── dashboard/
│   │   ├── app.py             # FastAPI
│   │   ├── sse.py
│   │   └── templates/
│   ├── integrations/
│   │   ├── gmail.py
│   │   ├── gcal.py
│   │   ├── elevenlabs.py
│   │   └── news.py
│   └── util/
│       ├── cost.py
│       └── audio.py           # ffmpeg ogg/opus
└── tests/
```

### Single-file entrypoint

```python
# src/tars/__main__.py
import asyncio, signal
from tars.config import load_config
from tars.db import Database
from tars.agent import Agent
from tars.bot.handlers import build_dispatcher
from tars.scheduler.jobs import build_scheduler
from tars.dashboard.app import build_app
import uvicorn

async def main():
    cfg = load_config()
    db = await Database.connect(cfg.db_path)
    await db.migrate()
    agent = Agent(db=db, cfg=cfg)

    dp, bot = build_dispatcher(agent=agent, cfg=cfg)
    sched = build_scheduler(agent=agent, db=db, cfg=cfg)
    sched.start()

    app = build_app(agent=agent, db=db, cfg=cfg)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.dashboard_host, port=cfg.dashboard_port, log_level="info"))

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    polling = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
    web = asyncio.create_task(server.serve())

    await stop.wait()
    polling.cancel(); sched.shutdown(wait=False); server.should_exit = True
    await asyncio.gather(polling, web, return_exceptions=True)
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
```

This is the entire "one process, one Agent, one SQLite" claim made concrete.

---

## 5. Step-by-Step Build (V1 in a Weekend)

### a. Repo setup, secrets, and config

```bash
mkdir ~/dev/tars && cd ~/dev/tars
uv init --package
uv add aiogram apscheduler fastapi uvicorn aiosqlite sqlite-vec httpx \
       pydantic pydantic-settings tomli structlog voyageai elevenlabs \
       google-api-python-client google-auth-oauthlib feedparser tenacity sse-starlette

mkdir -p ~/.tars
touch ~/.tars/config.toml
chmod 700 ~/.tars
chmod 600 ~/.tars/config.toml
```

```toml
# ~/.tars/config.toml
[telegram]
bot_token = "..."
allowed_chat_ids = [123456789]

[openrouter]
api_key = "sk-or-..."
daily_cap_usd = 5.0

[openai]
api_key = "sk-..."
daily_cap_usd = 2.0

[voyage]
api_key = "pa-..."

[elevenlabs]
api_key = "sk-..."
voice_id = "..."   # your designed TARS voice ID
model_id = "eleven_flash_v2_5"

[paths]
db = "/home/tars/tars.db"
vault = "/home/tars/vault"
backups = "/home/tars/backups"

[network]
dashboard_host = "100.x.y.z"   # tailnet IP, set by deploy script
dashboard_port = 8088

[tiers]
interactive_fast = "openai/gpt-5-mini"
cron_default = "deepseek/deepseek-v3.2"
ingest = "deepseek/deepseek-v3.2"
web_research = "openai/gpt-5"
```

`git init`, then a strict `.gitignore`:

```
.venv/
__pycache__/
*.db
*.db-wal
*.db-shm
vault/
backups/
.env
**/secrets*.toml
```

The TOML config never lives in the repo. The `~/.tars/` directory is mode `0700`, the file is `0600`. The systemd unit reads `Environment=TARS_CONFIG=/home/tars/.tars/config.toml`.

### b. SQLite schema and migrations

```sql
-- migrations/001_initial.sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
  thread_key TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  meta JSON
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  thread_key TEXT NOT NULL REFERENCES conversations(thread_key),
  ts INTEGER NOT NULL,
  role TEXT NOT NULL,        -- system|user|assistant|tool
  content TEXT NOT NULL,
  tool_calls JSON,
  cost_usd REAL DEFAULT 0,
  model TEXT,
  tier TEXT
);
CREATE INDEX idx_messages_thread_ts ON messages(thread_key, ts);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,
  created_at INTEGER NOT NULL,
  source TEXT NOT NULL,         -- 'telegram'|'voice'|'briefing'|'manual'
  body TEXT NOT NULL,
  tags JSON DEFAULT '[]',
  entities JSON DEFAULT '[]',
  status TEXT DEFAULT 'note',   -- 'note'|'open'|'closed'
  closes_note_id INTEGER REFERENCES notes(id),
  closed_at INTEGER,
  ext_path TEXT                 -- mirror in vault
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY,
  canonical TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL,           -- 'person'|'org'|'project'|'product'|'domain'
  meta JSON
);

CREATE TABLE IF NOT EXISTS entity_aliases (
  alias TEXT PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES entities(id)
);

CREATE TABLE IF NOT EXISTS follow_ups (
  id INTEGER PRIMARY KEY,
  note_id INTEGER NOT NULL REFERENCES notes(id),
  promised_to TEXT,
  due_at INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  reopened_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS briefings (
  id INTEGER PRIMARY KEY,
  date TEXT UNIQUE NOT NULL,    -- yyyy-mm-dd
  summary TEXT NOT NULL,
  payload JSON
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  last_run INTEGER,
  last_status TEXT,
  last_duration_ms INTEGER,
  next_run INTEGER
);

CREATE TABLE IF NOT EXISTS cost_ledger (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  tier TEXT,
  job_id TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cached_tokens INTEGER,
  cost_usd REAL NOT NULL
);
CREATE INDEX idx_cost_ts ON cost_ledger(ts);
```

```sql
-- migrations/002_brain_docs.sql
CREATE VIRTUAL TABLE IF NOT EXISTS brain_docs USING fts5(
  doc_id UNINDEXED,
  source UNINDEXED,         -- note|message|briefing|vault
  title,
  body,
  tags,
  tokenize = 'porter unicode61'
);

-- sqlite-vec table loaded at startup via extension:
-- CREATE VIRTUAL TABLE vec_docs USING vec0(
--   doc_id INTEGER PRIMARY KEY,
--   embedding FLOAT[1024]
-- );
```

A trivial migration runner keeps things honest:

```python
# src/tars/db.py
import aiosqlite, sqlite_vec
from pathlib import Path

class Database:
    def __init__(self, conn): self.conn = conn

    @classmethod
    async def connect(cls, path):
        conn = await aiosqlite.connect(path)
        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        return cls(conn)

    async def migrate(self):
        await self.conn.execute("CREATE TABLE IF NOT EXISTS schema_versions (v INTEGER PRIMARY KEY, applied_at INTEGER)")
        cur = await self.conn.execute("SELECT COALESCE(MAX(v), 0) FROM schema_versions")
        current = (await cur.fetchone())[0]
        files = sorted(Path("migrations").glob("*.sql"))
        for f in files:
            n = int(f.name.split("_")[0])
            if n <= current: continue
            await self.conn.executescript(f.read_text())
            await self.conn.execute("INSERT INTO schema_versions(v, applied_at) VALUES (?, strftime('%s','now'))", (n,))
            await self.conn.commit()
```

### c. The Agent class

Stateless, takes a thread key, never holds per-conversation state on `self`. The cache anchor is constructed once at import time.

```python
# src/tars/prompt.py
import json

SYSTEM_PROMPT = """You are TARS. Personal automation agent.
Voice: dry, deadpan, military-precise, terse confirmations, occasional understated wit. Never effusive.
Format: short paragraphs. Bullet only when listing items the user asked to list.
Tools available below. Cite memory IDs as [note:123] when referencing prior content.
Never invent dates, citations, or follow-up closures."""

TOOLS = [
  {"type": "function", "function": {"name": "search_memory", "description": "Hybrid search over notes, conversations, briefings, vault.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 8}},
                   "required": ["query"]}}},
  {"type": "function", "function": {"name": "save_note", "description": "Persist a note with tags.",
    "parameters": {"type": "object", "properties": {"body": {"type": "string"}, "tags": {"type": "array", "items": {"type":"string"}}},
                   "required": ["body"]}}},
  {"type": "function", "function": {"name": "open_followup", "description": "Track a promise.",
    "parameters": {"type": "object", "properties": {"note_id": {"type":"integer"}, "due_at_iso": {"type": "string"}, "to": {"type": "string"}},
                   "required": ["note_id"]}}},
  {"type": "function", "function": {"name": "close_followup", "description": "Close a follow-up with citation to a resolving note.",
    "parameters": {"type": "object", "properties": {"followup_id": {"type":"integer"}, "resolving_note_id": {"type":"integer"}},
                   "required": ["followup_id", "resolving_note_id"]}}},
  {"type": "function", "function": {"name": "web_research", "description": "Bounded web research with a tool loop.",
    "parameters": {"type": "object", "properties": {"query": {"type":"string"}, "max_steps": {"type":"integer","default":6}},
                   "required": ["query"]}}},
]

# Serialize once, byte-stable forever.
TOOLS_JSON = json.dumps(TOOLS, sort_keys=True, separators=(",", ":"))
SYSTEM_BLOCK = SYSTEM_PROMPT  # do not mutate at runtime
```

```python
# src/tars/agent.py
from tars.prompt import SYSTEM_BLOCK, TOOLS
from tars.router import call

class Agent:
    def __init__(self, db, cfg):
        self.db = db; self.cfg = cfg

    async def chat(self, thread_key: str, user_text: str, tier: str = "interactive_fast", tool_loop_max: int = 4) -> dict:
        await self._ensure_thread(thread_key)
        history = await self._load_history(thread_key, limit=40)
        messages = [{"role": "system", "content": SYSTEM_BLOCK}] + history + [{"role": "user", "content": user_text}]
        await self._save_turn(thread_key, "user", user_text)

        for _ in range(tool_loop_max):
            resp = await call(messages=messages, tools=TOOLS, tier=tier, cfg=self.cfg, db=self.db, thread_key=thread_key)
            if resp.tool_calls:
                messages.append({"role": "assistant", "tool_calls": resp.tool_calls, "content": ""})
                for tc in resp.tool_calls:
                    result = await self._run_tool(tc, thread_key)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue
            await self._save_turn(thread_key, "assistant", resp.text, model=resp.model, cost=resp.cost_usd, tier=tier)
            return {"text": resp.text, "cache_hit_tokens": resp.cached_tokens}
        return {"text": "Tool loop exhausted.", "cache_hit_tokens": 0}
```

Notes on stability of the prefix:
- `SYSTEM_BLOCK` is a module constant, never f-stringed at call time.
- `TOOLS` is serialized by the SDK in a canonical order; we keep our own canonical JSON for the audit log.
- History sits at the tail, so the prefix (system + tools) is byte-identical across turns.

### d. LLM router with caching, caps, and cooldowns

```python
# src/tars/router/__init__.py
import time, json
from dataclasses import dataclass
import httpx
from tars.util.cost import price_for

@dataclass
class LLMResponse:
    text: str
    tool_calls: list
    cached_tokens: int
    model: str
    cost_usd: float

class CircuitOpen(Exception): ...

_cooldowns = {}             # provider -> until_ts
_daily_spend = {}           # (provider, yyyy-mm-dd) -> usd

def _cap_ok(provider, cfg):
    today = time.strftime("%Y-%m-%d")
    cap = cfg.openrouter.daily_cap_usd if provider == "openrouter" else cfg.openai.daily_cap_usd
    return _daily_spend.get((provider, today), 0.0) < cap

def _on_spend(provider, usd):
    today = time.strftime("%Y-%m-%d")
    _daily_spend[(provider, today)] = _daily_spend.get((provider, today), 0.0) + usd

async def call(messages, tools, tier, cfg, db, thread_key):
    model = getattr(cfg.tiers, tier)
    order = ["openrouter", "openai"]
    last_err = None
    for provider in order:
        if _cooldowns.get(provider, 0) > time.time(): continue
        if not _cap_ok(provider, cfg): continue
        try:
            return await _call_one(provider, model, messages, tools, cfg, db, thread_key, tier)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            _cooldowns[provider] = time.time() + 60
            last_err = e
    raise CircuitOpen(f"All providers tripped: {last_err}")

async def _call_one(provider, model, messages, tools, cfg, db, thread_key, tier):
    if provider == "openrouter":
        url = "https://openrouter.ai/api/v1/chat/completions"
        key = cfg.openrouter.api_key
        body = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto"}
        headers = {"Authorization": f"Bearer {key}", "HTTP-Referer": "https://tars.local", "X-Title": "TARS"}
    else:
        if model.startswith("deepseek/"): model = "gpt-5-mini"
        url = "https://api.openai.com/v1/chat/completions"
        key = cfg.openai.api_key
        body = {"model": model.removeprefix("openai/"), "messages": messages, "tools": tools, "tool_choice": "auto"}
        headers = {"Authorization": f"Bearer {key}"}

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()

    usage = data.get("usage", {})
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
    cost = price_for(model, usage)
    _on_spend(provider, cost)
    await db.conn.execute(
      "INSERT INTO cost_ledger(ts, provider, model, tier, prompt_tokens, completion_tokens, cached_tokens, cost_usd) VALUES (strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?)",
      (provider, model, tier, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), cached, cost))
    await db.conn.commit()

    msg = data["choices"][0]["message"]
    return LLMResponse(
        text=msg.get("content") or "",
        tool_calls=msg.get("tool_calls") or [],
        cached_tokens=cached, model=model, cost_usd=cost,
    )
```

Key points:
- Per OpenRouter's published docs, prompt caching is automatic on supported providers (OpenAI, DeepSeek, Gemini 2.5) and explicit (cache_control breakpoints) on Anthropic and Alibaba; OpenRouter applies provider sticky routing so identical opening messages stay on the same provider endpoint. Watching `usage.prompt_tokens_details.cached_tokens` in responses tells you whether the cache is hitting.
- Daily spend caps and 60-second cooldowns on transport errors implement the per-provider caps and cooldowns described in Asaf's article.
- Tier-to-model mapping comes from `config.toml`, not hard-coded.

### e. Telegram bot with aiogram

aiogram 3.x is the right pick: fully async on aiohttp, native asyncio, and the type-hints are good. Per the official Telegram bot library list, aiogram is described as "a pretty simple and fully asynchronous library for Telegram Bot API written with asyncio and aiohttp." It is the most modern of the major Python options. Current release is 3.28.2 (per PyPI, with 3.28.0 published May 8, 2026).

```python
# src/tars/bot/handlers.py
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from tars.bot.voice import synth_and_send

def build_dispatcher(agent, cfg):
    bot = Bot(token=cfg.telegram.bot_token)
    dp = Dispatcher()

    def authorized(m: Message) -> bool:
        return m.chat.id in cfg.telegram.allowed_chat_ids

    @dp.message(CommandStart())
    async def start(m: Message):
        if not authorized(m): return
        await m.answer("TARS online.")

    @dp.message(Command("voice"))
    async def voice_toggle(m: Message):
        if not authorized(m): return
        new = await agent.toggle_voice(thread_key=f"tg:{m.chat.id}")
        await m.answer(f"Voice {'enabled' if new else 'disabled'} for this chat.")

    @dp.message(Command("research"))
    async def research(m: Message):
        if not authorized(m): return
        q = m.text.removeprefix("/research").strip()
        out = await agent.chat(thread_key=f"tg:{m.chat.id}", user_text=q, tier="web_research")
        await m.answer(out["text"])

    @dp.message(F.text.regexp(r"(?i)^note:\s*(.+)"))
    async def take_note(m: Message):
        if not authorized(m): return
        body = m.text.split(":", 1)[1].strip()
        note_id = await agent.save_note(body=body, source="telegram")
        await m.answer(f"Noted. [note:{note_id}]")

    @dp.message(F.text)
    async def chat(m: Message):
        if not authorized(m): return
        out = await agent.chat(thread_key=f"tg:{m.chat.id}", user_text=m.text)
        await m.answer(out["text"])
        if await agent.voice_enabled(f"tg:{m.chat.id}"):
            await synth_and_send(bot, m.chat.id, out["text"], cfg)

    return dp, bot
```

Long polling is the right default: no public URL, no webhook signing, and Tailscale would block public webhook traffic anyway. Switch to webhooks only if you graduate to a multi-instance setup, which you will not.

### f. APScheduler in the same event loop

APScheduler's `AsyncIOScheduler` runs jobs on the current asyncio event loop and supports native coroutines:

```python
# src/tars/scheduler/jobs.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from tars.scheduler.morning_briefing import morning_briefing
from tars.scheduler.followup_reconcile import weekly_followup_reconcile
from tars.scheduler.competitive_intel import competitive_intel_scan
from tars.scheduler.brain_reindex import brain_docs_reindex
from tars.scheduler.calendar_pull import calendar_pull
from tars.scheduler.email_summary import email_summary

def build_scheduler(agent, db, cfg):
    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{cfg.paths.db}")}
    sched = AsyncIOScheduler(
        jobstores=jobstores,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
        timezone=cfg.timezone,
    )

    sched.add_job(morning_briefing, CronTrigger(hour=5, minute=0), id="morning_briefing", args=[agent, db, cfg], replace_existing=True)
    sched.add_job(email_summary, CronTrigger(minute="*/30"), id="email_summary", args=[agent, db, cfg], replace_existing=True)
    sched.add_job(calendar_pull, CronTrigger(minute="*/15"), id="calendar_pull", args=[agent, db, cfg], replace_existing=True)
    sched.add_job(brain_docs_reindex, IntervalTrigger(minutes=15), id="brain_docs_reindex", args=[db, cfg], replace_existing=True)
    sched.add_job(competitive_intel_scan, CronTrigger(hour="9,13,17", minute=0), id="competitive_intel_scan", args=[agent, db, cfg], replace_existing=True)
    sched.add_job(weekly_followup_reconcile, CronTrigger(day_of_week="sun", hour=18, minute=0), id="weekly_followup_reconcile", args=[agent, db, cfg], replace_existing=True)
    # ... 12 more
    return sched
```

`coalesce=True` and `misfire_grace_time=600` mean that after a restart, missed jobs are not fired multiple times and you have a 10-minute window to catch up.

### g. Concrete scheduled jobs

**`morning_briefing` (05:00 daily)** is the marquee job:

```python
# src/tars/scheduler/morning_briefing.py
async def morning_briefing(agent, db, cfg):
    overnight = await fetch_overnight_emails(db, cfg, since_hours=12)
    cal = await calendar_top_items(db, cfg, n=5)
    open_fu = await db.conn.execute_fetchall("SELECT id, body FROM notes WHERE status='open' ORDER BY created_at DESC LIMIT 5")
    news = await tracked_domain_news(db, cfg)

    payload = {"emails": overnight, "calendar": cal, "open_followups": open_fu, "news": news}
    prompt = f"Compose a 90-second morning briefing in TARS cadence. Source data:\n{json.dumps(payload, default=str)}"
    out = await agent.chat(thread_key="job:morning_briefing", user_text=prompt, tier="cron_default")
    await db.conn.execute("INSERT OR REPLACE INTO briefings(date, summary, payload) VALUES (?, ?, ?)",
                          (date.today().isoformat(), out["text"], json.dumps(payload, default=str)))
    await db.conn.commit()
    await send_telegram(cfg, out["text"])
    if cfg.voice.morning_briefing_voice:
        await send_voice_note(cfg, out["text"])
```

The other 17 jobs follow the same shape: pull source data, ask the Agent at the appropriate tier, persist, notify. Representative set: `email_summary` (every 30 min), `calendar_pull` (every 15 min), `brain_docs_reindex` (every 15 min), `competitive_intel_scan` (9/13/17 daily), `weekly_followup_reconcile` (Sundays), `cost_rollup_daily` (midnight), `vault_sweep` (every 10 min), `news_sources_refresh` (hourly), `entity_dedup` (nightly), `cooldown_clear` (every 5 min), `voice_quota_check` (hourly), `health_self_ping` (every minute), `backup_snapshot` (06:00 daily), `restic_b2_push` (07:00 daily), `restic_offsite_push` (08:00 daily), `stale_thread_summarize` (Mondays), and `lab_notebook_digest` (Fridays).

### h. Hybrid retrieval (FTS5 + sqlite-vec + Voyage)

Install via `pip install sqlite-vec` and load the extension at connection time. The `vec0` virtual table is the storage layer. The standard hybrid query uses Reciprocal Rank Fusion to merge BM25 and cosine results before Voyage reranks the top candidates.

```python
# src/tars/memory/embed.py
import voyageai
class Embedder:
    def __init__(self, key): self.c = voyageai.AsyncClient(api_key=key)
    async def embed(self, texts, input_type="document"):
        r = await self.c.embed(texts=texts, model="voyage-3-large",
                                output_dimension=1024, output_dtype="int8",
                                input_type=input_type)
        return r.embeddings
    async def rerank(self, query, docs, top_k=8):
        try:
            r = await self.c.rerank(query=query, documents=docs, model="rerank-2.5", top_k=top_k)
            return [(item.index, item.relevance_score) for item in r.results]
        except Exception:
            return [(i, 1.0/(i+1)) for i in range(min(len(docs), top_k))]  # identity fallback
```

```python
# src/tars/memory/search.py
import struct

RRF_K = 60
HYBRID_SQL = """
WITH vec_hits AS (
  SELECT doc_id, distance, ROW_NUMBER() OVER(ORDER BY distance) AS rank
  FROM vec_docs
  WHERE embedding MATCH :qvec AND k = 50
),
fts_hits AS (
  SELECT doc_id, ROW_NUMBER() OVER(ORDER BY rank) AS rank
  FROM brain_docs
  WHERE brain_docs MATCH :qtext
  LIMIT 50
)
SELECT b.doc_id, b.source, b.title, b.body,
       COALESCE(1.0/(:rrf_k + v.rank), 0) + COALESCE(1.0/(:rrf_k + f.rank), 0) AS rrf
FROM brain_docs b
LEFT JOIN vec_hits v ON v.doc_id = b.doc_id
LEFT JOIN fts_hits f ON f.doc_id = b.doc_id
WHERE v.doc_id IS NOT NULL OR f.doc_id IS NOT NULL
ORDER BY rrf DESC
LIMIT 25;
"""

def pack_f32(v): return struct.pack(f"{len(v)}f", *v)

async def hybrid_search(db, embedder, query, k=8):
    qvec = (await embedder.embed([query], input_type="query"))[0]
    rows = await db.conn.execute_fetchall(HYBRID_SQL, {"qvec": pack_f32(qvec), "qtext": query, "rrf_k": RRF_K})
    docs = [r["body"] for r in rows]
    reranked = await embedder.rerank(query=query, docs=docs, top_k=k)
    return [rows[i] for i, _ in reranked]
```

This mirrors the canonical pattern documented by Alex Garcia (sqlite-vec's author) in his "Hybrid full-text search and vector search with SQLite" post: a CTE that issues FTS5 and `vec0` queries in parallel, then merges with RRF (he uses `coalesce(1.0 / (rrf_k + rank), 0)` as the rank weight).

A reindex job rebuilds `brain_docs` every 15 minutes across notes, conversations, briefings, and the vault, exactly per the source article.

### i. Entity extraction and alias resolution

After a note is saved, the Agent emits a structured extraction call:

```python
EXTRACT_PROMPT = """From the note below, return JSON {"entities": [{"canonical":"...","kind":"person|org|project|product|domain","aliases":["..."]}]}.
Note: <<<{body}>>>"""
```

The router runs the call at `cron_default` (DeepSeek V3.2). The result is upserted into `entities` and `entity_aliases`, with conflicts resolved by `canonical` uniqueness. Future searches expand the query: if the user searches "OAI," it also matches "OpenAI."

### j. Follow-up lifecycle with citation-gated closure

`open_followup` writes a row. `close_followup` requires both a `followup_id` and a `resolving_note_id`; the application enforces:

```python
async def close_followup(db, followup_id, resolving_note_id):
    fu = await db.fetch_one("SELECT note_id FROM follow_ups WHERE id=? AND status='open'", (followup_id,))
    if not fu: raise ValueError("Follow-up not open")
    note = await db.fetch_one("SELECT id FROM notes WHERE id=?", (resolving_note_id,))
    if not note: raise ValueError("Resolving note missing")
    await db.execute("UPDATE follow_ups SET status='closed' WHERE id=?", (followup_id,))
    await db.execute("UPDATE notes SET status='closed', closes_note_id=?, closed_at=strftime('%s','now') WHERE id=?",
                     (fu["note_id"], resolving_note_id))
```

The weekly Sunday job reopens any `follow_ups` whose `due_at` passed without a matching closed note, incrementing `reopened_count`.

### k. ElevenLabs voice (Voice Design, not cloning)

**Legal posture.** ElevenLabs' Prohibited Use Policy explicitly bars creating or using audio output to "intentionally replicate the voice of another person… without consent or legal right," and the Professional Voice Clone help docs state: "You can only create a Professional Voice Clone of your own voice. Even with their consent, you cannot clone someone else's voice." Cloning Bill Irwin's TARS performance from Interstellar audio violates both the ToS and US right-of-publicity statutes (California Civil Code §3344, the Tennessee ELVIS Act, NY Civil Rights Law §§50-51).

**The compliant path: Voice Design.** Generate a synthetic deadpan male voice from a text prompt. Recommended starter prompt:

> "Studio-quality recording. Male, late 40s, American, neutral General-American accent. Deep, dry, deadpan baritone with a faint synthetic undertone, as if filtered through a clean robotic chassis. Calm, measured military cadence, confident, precise articulation, never raises pitch. Wry, sardonic sense of humor delivered completely straight. Speaks at a steady, deliberate pace with short pauses between clauses."

Iterate 3 to 10 generations until one locks. Save the `voice_id`.

**Voice settings** (per the published shawndei/tars-voice config and ElevenLabs' own guidance that high stability "borders on monotone"):

```python
voice_settings = {
  "stability": 0.78,         # 0.75-0.85 range gives the monotone TARS feel
  "similarity_boost": 0.80,
  "style": 0.40,
  "use_speaker_boost": True,
}
```

The shawndei/tars-voice repo specifically publishes stability 0.75, similarity_boost 0.80, style 0.45, on default voice Adam (`pNInz6obpgDQGcFmaJgB`) as a known-good starting point if you do not want to design a custom voice.

**Model: `eleven_flash_v2_5`.** Per ElevenLabs help docs, Flash v2.5 generates "speech in under 75ms" and costs 0.5 credit per character versus 1.0 for Multilingual v2. Eleven v3 is explicitly "not made for real-time applications like Conversational AI." For Telegram voice notes, Flash is the right tradeoff: deadpan delivery does not need v3's emotional range.

**Synthesis and OGG/Opus conversion.** Telegram's `sendVoice` requires `.ogg` encoded with Opus; per the Telegram Bot API docs: "for this to work, your audio must be in an .OGG file encoded with OPUS… To use sendVoice, the file must have the type audio/ogg and be no more than 1MB in size."

```python
# src/tars/bot/voice.py
import subprocess
from elevenlabs.client import AsyncElevenLabs
from aiogram.types import BufferedInputFile

async def synth_and_send(bot, chat_id, text, cfg):
    client = AsyncElevenLabs(api_key=cfg.elevenlabs.api_key)
    mp3_bytes = b""
    async for chunk in client.text_to_speech.convert(
        voice_id=cfg.elevenlabs.voice_id,
        model_id=cfg.elevenlabs.model_id,
        text=text,
        voice_settings={"stability":0.78, "similarity_boost":0.80, "style":0.40, "use_speaker_boost":True},
        output_format="mp3_44100_64",
    ): mp3_bytes += chunk

    p = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
         "-c:a", "libopus", "-b:a", "48k", "-ac", "1", "-vn",
         "-f", "ogg", "pipe:1"],
        input=mp3_bytes, capture_output=True, check=True)
    ogg = p.stdout
    await bot.send_voice(chat_id=chat_id, voice=BufferedInputFile(ogg, "tars.ogg"))
```

### l. FastAPI dashboard with SSE

```python
# src/tars/dashboard/app.py
from fastapi import FastAPI, Request
from sse_starlette.sse import EventSourceResponse
import asyncio, json

def build_app(agent, db, cfg):
    app = FastAPI(title="TARS")

    @app.get("/api/costs")
    async def costs():
        rows = await db.conn.execute_fetchall(
          "SELECT date(ts,'unixepoch') d, tier, model, SUM(cost_usd) cost, SUM(prompt_tokens) pt, SUM(cached_tokens) ct "
          "FROM cost_ledger GROUP BY d, tier, model ORDER BY d DESC LIMIT 200")
        return [dict(r) for r in rows]

    @app.get("/api/conversations")
    async def conversations(q: str = ""):
        # serve over brain_docs FTS
        ...

    @app.get("/api/jobs/stream")
    async def job_stream(request: Request):
        async def gen():
            while not await request.is_disconnected():
                rows = await db.conn.execute_fetchall("SELECT * FROM jobs ORDER BY next_run ASC")
                yield {"event":"jobs", "data": json.dumps([dict(r) for r in rows], default=str)}
                await asyncio.sleep(1.0)
        return EventSourceResponse(gen())

    return app
```

### m. Obsidian via Syncthing

Install Syncthing on both the VPS and your desktop. Create a folder ID for `~/tars/vault/` and share it. In Obsidian, configure that directory as a sub-folder (not the vault root, to avoid `.obsidian/` config conflicts). Set Syncthing's "Watcher" on and "Versioning: Staggered (1y)" so conflicts move to `.stversions/` instead of corrupting files. When TARS writes a new note, write to a temp file in the same directory, fsync, then `os.replace()` to avoid partial reads on the desktop.

### n. Tailscale binding

Install Tailscale, `tailscale up --ssh`. Get the device's tailnet IP with `tailscale ip -4`. Either bind FastAPI directly to the tailnet IP, or keep it on `127.0.0.1` and front it with `tailscale serve`. Per Tailscale's official docs: "When you use the identity headers to authenticate to a backend service, it's best practice to only have the service listen on localhost. Otherwise, any user that can call your service directly (rather than with the Serve URL) could trivially provide their own values for these HTTP headers."

Bind directly (simplest):
```python
# uvicorn.Config(app, host="100.x.y.z", port=8088)
```

Or `tailscale serve` for HTTPS via MagicDNS:
```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8088
# https://tars.<your-tailnet>.ts.net now works for any tailnet member
```

Tailscale Serve requires HTTPS to be enabled on your tailnet; if it is not, the CLI prompts a consent page. Funnel (public exposure) is the wrong mode here, on purpose.

### o. Email, calendar, news

For Gmail and Calendar, **use OAuth installed-application flow with a personal Google account, not a service account.** Service accounts can only access Workspace-domain mailboxes via domain-wide delegation, which a personal Gmail does not support. The Python recipe is the standard `google-auth-oauthlib` `InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=0)` once on a workstation, then ship the resulting `token.json` to the VPS (stored in `~/.tars/google_token.json`, mode 0600). Auto-refresh handles the rest.

For news, the simplest reliable path is feedparser over RSS feeds for tracked domains, plus optional Hacker News, Bluesky, and Reddit JSON endpoints. Use a single `news_sources` table to manage subscriptions.

### Deploy script

```bash
#!/usr/bin/env bash
# deploy.sh
set -euo pipefail
git push origin main
ssh tars@$HOST <<'EOF'
  set -euo pipefail
  cd ~/tars
  git fetch --all
  git reset --hard origin/main
  uv sync --frozen
  sudo systemctl restart tars
  sleep 3
  systemctl --user status tars --no-pager | head -20
EOF
```

Rollback: `git revert HEAD && ./deploy.sh`. No magic.

### Backup strategy (three destinations)

```bash
# /usr/local/bin/tars-backup.sh
set -euo pipefail
TS=$(date -u +%Y%m%dT%H%M%SZ)
SNAP=/var/lib/tars/snapshots/tars-$TS.db
mkdir -p /var/lib/tars/snapshots
sqlite3 /home/tars/tars.db ".backup '$SNAP'"

# Dest 1: local snapshot dir (kept 14 days, prune older)
find /var/lib/tars/snapshots -name 'tars-*.db' -mtime +14 -delete

# Dest 2: restic to Backblaze B2
RESTIC_REPOSITORY=b2:tars-prod-1 RESTIC_PASSWORD_FILE=/etc/tars/restic.pw \
  restic backup "$SNAP" /home/tars/vault --tag daily

# Dest 3: restic to a second region/provider (Storj, Wasabi, Hetzner Storage Box)
RESTIC_REPOSITORY=sftp:storage-box:/tars RESTIC_PASSWORD_FILE=/etc/tars/restic.pw \
  restic backup "$SNAP" /home/tars/vault --tag daily

restic forget --prune --keep-daily 14 --keep-weekly 8 --keep-monthly 12
```

Per the SQLite docs, the `.backup` command "uses SQLite's Online Backup API, which allows backup of a database without taking it 'offline'… A backup doesn't literally copy all of the contents of the WAL file, but as the backup operation copies the database page by page, the WAL file is taken into account." Restic's deduplicating chunker means two daily snapshots that share 99% of pages cost almost nothing in storage.

---

## 6. Deep-Dive Technical Guidance

### Prefix caching

**The mental model.** Providers maintain a KV cache keyed by a hash of the leading tokens of the request. If the next request has the same leading tokens, the prefill phase skips. Per OpenRouter's response-caching announcement, on cache hits "Cached responses come back in 80-300ms, most of which is serialization and network. The cache lookup itself averages 4ms." You read cache effectiveness from `usage.prompt_tokens_details.cached_tokens` in the response; OpenRouter's docs state: "`cached_tokens`: Number of tokens read from the cache (cache hit). When this is greater than zero, you're benefiting from cached content."

**How to make it hit.**
1. **One module-level system prompt string.** Never f-string user data, timestamps, or "today is Tuesday" into it.
2. **One canonical tool JSON.** Keep the Python list literal stable; do not shuffle keys.
3. **Place history last.** Per OpenRouter, "OpenRouter identifies conversations by hashing the first system (or developer) message and the first non-system message in each request, so requests that share the same opening messages are routed to the same provider."
4. **Anthropic-style explicit caching.** If you switch a tier to Claude through OpenRouter, you must add `cache_control: {"type":"ephemeral"}` to your system block.
5. **Provider sticky routing.** OpenRouter automatically prefers the same provider endpoint as your last cached request, but only when "the provider's cache read pricing is cheaper than regular prompt pricing." On DeepSeek and OpenAI this is automatic.

**Common pitfalls.**
- Including `"Today is YYYY-MM-DD HH:MM"` in the system block: cache hit rate falls to zero.
- Reordering tool definitions in code while iterating: every PR re-warms the cache.
- Streaming with `cache_control: {ttl: "1h"}` on Claude and then sending the same prompt 65 minutes later: full prefill again.
- OpenAI's caching is implicit and requires prompts of at least 1024 tokens to qualify.

### Multi-tier model routing (late 2025/early 2026)

| Tier | Default model | Price per 1M tokens (May 2026) | Use case |
|---|---|---|---|
| `interactive_fast` | `openai/gpt-5-mini` | $0.25 in / $2.00 out (272K ctx; released Aug 7 2025) | Telegram chat |
| `cron_default` | `deepseek/deepseek-v3.2` | $0.26 in / $0.38 out (163,840 ctx; released Dec 1 2025) | Briefings, summarization, periodic jobs |
| `ingest` | `deepseek/deepseek-v3.2` | $0.26 in / $0.38 out | Bulk reindex, entity extraction |
| `web_research` | `openai/gpt-5` | $0.625 in / $5.00 out (400K ctx) | Bounded deep-research tool loop |

The roughly 8-to-13x cost gap between DeepSeek V3.2 and gpt-5-mini on the input side, and ~25x on output, justifies tiered routing for cron work: a 90,000-token morning-briefing prompt costs about $0.023 on DeepSeek V3.2 versus $0.225 on gpt-5-mini. Across 18 jobs, that compounds quickly.

**Latency vs cost.** gpt-5-mini is faster end-to-end on small interactive turns; DeepSeek V3.2 has slightly higher first-token latency but is dramatically cheaper on completion. Reserve gpt-5 for the deep-research tool loop where you call it 5-8 times in one invocation and quality dominates cost.

**Cooldowns and caps.** On a 5xx or 429 response from a provider, set `_cooldowns[provider] = now + 60`. Track `_daily_spend[(provider, today)]` and stop routing to that provider when it exceeds `daily_cap_usd`. When all providers are tripped, the router raises `CircuitOpen` and the calling job logs a `skipped_no_provider` row to the cost ledger.

### sqlite-vec usage

```bash
pip install sqlite-vec
```

```python
import sqlite3, sqlite_vec, struct
conn = sqlite3.connect("tars.db")
conn.enable_load_extension(True)
sqlite_vec.load(conn)
print(conn.execute("select vec_version()").fetchone())

conn.execute("""
  CREATE VIRTUAL TABLE IF NOT EXISTS vec_docs USING vec0(
    doc_id INTEGER PRIMARY KEY,
    embedding FLOAT[1024]
  )""")

def pack_f32(v): return struct.pack(f"{len(v)}f", *v)

conn.execute("INSERT INTO vec_docs(doc_id, embedding) VALUES (?, ?)", (1, pack_f32(vec)))

rows = conn.execute("""
  SELECT doc_id, distance
  FROM vec_docs
  WHERE embedding MATCH ? AND k = 8
  ORDER BY distance""", (pack_f32(qvec),)).fetchall()
```

The hybrid RRF pattern was shown earlier. Note: sqlite-vec currently does full-table KNN scans (not ANN); per the official docs it "currently only supports vector search using full table scans." On int8 quantized 1024-dim vectors this is fine up to about 100k rows. Beyond that, drop to 512 dimensions or move to FAISS.

**Storage math.** `voyage-3-large` at 1024-dim int8 = 1024 bytes per vector + small SQLite overhead. 50,000 notes ≈ 51 MB. Vector storage is not the bottleneck.

### ElevenLabs TARS voice (recap with cost numbers)

- **Voice Design prompt** (above).
- **Voice settings**: stability 0.78, similarity_boost 0.80, style 0.40, use_speaker_boost true.
- **Model `eleven_flash_v2_5`**: 0.5 credit per character, under 75ms first audio.
- **Format**: `mp3_44100_64` from the API, then ffmpeg to OGG/Opus mono 48 kbps for `sendVoice`.
- **Cost example**: a 90-second morning briefing ≈ 220 words ≈ 1,300 characters ≈ 650 credits. On the Creator plan ($22/100,000 credits), that is roughly $0.143 per voice note. Five voice notes a day = $21.45/mo just in audio. Two a day = $8.60/mo. Budget accordingly.

### Telegram bot architecture

- **Long polling** for a single-instance bot. `dp.start_polling(bot)` and you are done.
- **Webhook** only if you need multi-instance load-balancing, which you do not.
- **Voice notes**: must be `.OGG` with Opus codec; up to 1MB to be sent as a voice note (larger files are sent as audio attachments).
- **Voice toggle per chat**: store in `conversations.meta` JSON, default `false`. User types `/voice on` to opt in per chat.

### APScheduler in asyncio

- `AsyncIOScheduler` runs on the current loop. Do not use `BackgroundScheduler` in asyncio code; it spawns a thread and you lose the shared-loop advantage.
- `SQLAlchemyJobStore` pointed at the same SQLite file gives you persistent jobs across restarts. After a crash, `coalesce=True` plus `misfire_grace_time=600` means missed jobs run once within 10 minutes.
- Single-process pattern. The APScheduler maintainer's GitHub discussion warns: "Gunicorn forks N worker processes, each running its own copy of your FastAPI app. If you initialize APScheduler in the app startup, every worker gets its own scheduler instance, so your jobs run N times." Run uvicorn with `workers=1` since the scheduler shares state with the bot.

### Tailscale-only binding

```python
# uvicorn bound to tailnet only
import subprocess
tailnet_ip = subprocess.check_output(["tailscale", "ip", "-4"]).decode().strip().splitlines()[0]
cfg.network.dashboard_host = tailnet_ip
```

`tailscale serve --bg --https=443 http://127.0.0.1:8088` exposes the dashboard at `https://<machine>.<tailnet>.ts.net` for any tailnet member. Funnel is off, on purpose. Funnel only supports ports 443, 8443, and 10000 and requires Tailscale v1.38.3+ with MagicDNS and HTTPS enabled, which is overkill for a personal dashboard.

### Email and calendar

For Gmail and Calendar against a personal account, OAuth Installed App is the right answer. App passwords no longer work for Gmail API (only IMAP); service accounts require domain-wide delegation, which you cannot grant on a personal account. The `google-auth-oauthlib` `InstalledAppFlow.from_client_secrets_file(...).run_local_server(port=0)` flow runs once, persists `token.json`, and the refresh token survives indefinitely until you revoke.

### Backup strategy

`sqlite3 source.db ".backup target.db"` while the application is live is the canonical "hot backup" and uses the Online Backup API; per the SQLite community discussion, "the .backup command uses SQLite's Online Backup API, which allows backup of a database without taking it 'offline'." Three destinations: a 14-day rolling local snapshot folder, restic to Backblaze B2, restic to a second offsite (Hetzner Storage Box or Wasabi). Test restore monthly: `restic restore latest --target /tmp/restore-test && sqlite3 /tmp/restore-test/tars.db "select count(*) from notes"`.

### Secrets and security

- `~/.tars/` is `0700`, `~/.tars/config.toml` is `0600`, owned by the `tars` user.
- `.env` is for development scaffolding only. The TOML is the canonical secret store in production. The systemd unit reads `TARS_CONFIG=/home/tars/.tars/config.toml`.
- `.gitignore` patterns: `.env`, `*.toml` under `secrets/`, `vault/`, `*.db*`, `~/.tars/`.
- Key rotation: each provider key sits in one TOML line; rotate by editing the file and `systemctl restart tars`. No code changes.
- Bot token rotation: BotFather `/revoke` and update the TOML.

---

## 7. Cost Breakdown

Realistic monthly cost at "personal daily-driver" usage (a few hundred Telegram turns, 18 jobs, 1-3 voice notes/day, hybrid retrieval reindex every 15 min):

| Bucket | Light usage | Heavy usage |
|---|---|---|
| Hetzner CX22 + backups | $5 | $7 |
| OpenRouter (cron + ingest, DeepSeek V3.2, ~5M tokens/mo) | $2-3 | $10-15 |
| OpenRouter (interactive, gpt-5-mini, ~1M tokens/mo) | $1-2 | $5-8 |
| OpenRouter (web research, gpt-5, ~200K tokens/mo) | $1 | $5 |
| Voyage embeddings + rerank | $0 (under free tier) | $2-4 |
| ElevenLabs Creator plan ($22) + overage | $22 | $30-45 |
| Telegram | $0 | $0 |
| Tailscale Personal | $0 | $0 |
| **Total** | **~$31-33/mo** | **~$59-84/mo** |

A few notes on the math:
- DeepSeek V3.2 at $0.26 in / $0.38 out is roughly 10x cheaper than gpt-5-mini on input and 5x cheaper on output, which is the dominant cost in a frozen-prefix system.
- Voyage's first 200M tokens per model are free per account; you will likely never pay for embeddings at personal volume.
- The biggest swing factor is ElevenLabs. If you cut voice notes to two a day, you can downgrade to the Starter plan ($5/mo) which has 30,000 credits.
- Asaf's reported "about $22 a month for the box plus a few dollars a day in LLM tokens" is consistent with the "heavy usage" column above; his box is presumably a CX32 (€6.80/mo) and his daily LLM spend likely lands around $3-5 plus voice. Our estimate is in the same neighborhood.

---

## 8. Migration Path from V1 to V2

Asaf wrote the system twice. The first version was a from-scratch Python agent on his workstation. The second was the same agent on a Hetzner VPS with Hermes Agent as the substrate and his original code "acting as a headless library that handles the cron jobs."

What this tells you:

**Keep in V1, refactor in V2.**
- Keep the SQLite schema, the entity store, the follow-up lifecycle, the cron jobs, and the voice persona. These are the agent. They survive substrate swaps.
- Refactor the chat loop and tool-call protocol. This is the substrate. The first version of an agent's tool loop is always too ad hoc; by V2 you will want middleware (rate limiting, cost capture, idempotency keys, observability spans) and you will feel the urge to lift it into a framework.

**When to consider swapping the substrate.**
- If your tool catalog grows beyond 12-15 functions and you start writing your own routing logic for which tool to call when, consider LangGraph (explicit state machines) or Hermes Agent (the substrate Asaf eventually adopted).
- If you keep finding yourself reimplementing retries, parallel tool calls, and structured outputs, consider lifting to the OpenAI Responses API or Vercel AI SDK as a transport layer.
- **Resist switching to LangChain.** The footgun-per-LoC ratio is high, and the abstractions burn cache anchors by injecting timestamps and dynamic context blocks into prompts by default.

**The contract that lets you swap.** Treat your Agent class as a façade: `Agent.chat(thread_key, user_text, tier) -> str`. As long as the substrate respects the thread_key for history and the frozen prefix is byte-stable, you can swap the body of `chat()` for a LangGraph compiled graph or a Hermes invocation without rewriting jobs, dashboard, or memory layer.

---

## 9. Pitfalls and Lessons Learned

- **SQLite write contention.** Put the DB in WAL mode (`PRAGMA journal_mode=WAL`) and set `PRAGMA busy_timeout=5000`. Run a single writer at a time; if multiple jobs need to write, serialize through an `asyncio.Lock` around the writer. SQLite handles concurrent readers fine, but only one writer at a time.
- **asyncio event loop blocking.** Anything CPU-heavy (Voyage local fallback, large JSON parse, sqlite-vec full scan over 200k rows) blocks the loop. Wrap in `asyncio.to_thread(...)` or `run_in_executor`. The APScheduler maintainer is explicit: do not use `AsyncIOExecutor` for blocking jobs; configure a `ThreadPoolExecutor` for them.
- **Prompt cache invalidation.** Cache hits silently fall to zero if you f-string anything into the system block. Add a daily cache-hit report to the dashboard from `cost_ledger.cached_tokens` so you notice immediately.
- **Telegram rate limits.** 30 messages/sec to different users, 1 message/sec per chat. With one user this is rarely an issue, except for voice notes: a 5-minute synthesized briefing exceeds the 1MB voice-note cap and will be sent as a regular audio file (still works, but no waveform UI in Telegram).
- **OpenRouter quirks.** Sticky routing only activates when cache reads are priced cheaper than fresh inputs; for some smaller providers it does nothing. Also, OpenAI returns useful `x-request-id` and `x-ratelimit-*` headers but cache accounting lives in the usage payload, not headers; rely on `usage.prompt_tokens_details.cached_tokens`.
- **ElevenLabs character costs.** Easy to blow past the Creator plan's 100k credits. Track `chars_synthesized_today` in `cost_ledger` and hard-cap voice synthesis after, say, 2,000 characters/day with a Telegram fallback to text-only.
- **Scheduler clock drift.** Hetzner VMs drift on small loads. Install `chrony` or rely on `systemd-timesyncd`; cron jobs at exact :00 minutes will be off by seconds if you do not.
- **Obsidian sync conflicts.** Always write to vault files via temp + `os.replace()`, never with two writers at once. Syncthing's "Staggered Versioning" preserves losing copies. The rule of thumb: TARS appends only to today's daily note; humans edit anything older.
- **Service account vs OAuth for Gmail.** A personal Gmail does not support service-account delegation; use OAuth installed-app flow, period.
- **uvicorn workers > 1 will fork the scheduler.** Set `workers=1` always.
- **DeepSeek cache warmup latency.** OpenRouter's own community notes mention DeepSeek cache construction "is best-effort and can take a few seconds." Do not panic when the first identical request reports `cached_tokens: 0`; verify with a second identical request.

---

## 10. Resources and References

**Source article and adjacent**
- Asaf Saar, "Meet TARS" at asaf.corgimind.com/thinking/meet-tars
- Asaf Saar, "The Morning I Stopped Paying for AI" (same site)

**Code repositories that resemble parts of this architecture**
- aiogram official examples at github.com/aiogram/aiogram (current 3.28.2)
- danirus/async-apscheduler-fastapi for the AsyncIOScheduler-in-FastAPI pattern
- asg017/sqlite-vec and the NBC headlines hybrid-search example
- liamca/sqlite-hybrid-search for a clean Python RRF implementation
- shawndei/tars-voice and latishab/tars-conversation-app for ElevenLabs TARS-voice integration patterns (note: these clone the original voice, which is ToS-violating; use only as architectural reference, not for the cloning step)

**Official docs**
- OpenRouter prompt caching guide and provider sticky routing docs at openrouter.ai/docs/guides/best-practices/prompt-caching
- OpenRouter response caching announcement, openrouter.ai/announcements/response-caching
- Telegram Bot API `sendVoice` reference, core.telegram.org/bots/api
- ElevenLabs Voice Design, Text-to-Speech, Voice Settings docs (elevenlabs.io/docs/...)
- ElevenLabs Prohibited Use Policy at elevenlabs.io/use-policy
- Tailscale Serve docs at tailscale.com/docs/features/tailscale-serve
- APScheduler 3.x docs (AsyncIOScheduler module)
- sqlite-vec docs and Alex Garcia's "Hybrid full-text search and vector search with SQLite" post at alexgarcia.xyz
- Voyage AI docs (docs.voyageai.com) and the `rerank-2.5` blog post (Aug 2025)
- SQLite Online Backup API documentation

**Useful blog posts**
- Simon Willison's link post on hybrid FTS5 + sqlite-vec with RRF
- Oldmoe's "Backup strategies for SQLite in production"
- OpenRouter "Is Implicit Caching Prompt Retention?" announcement (covers how cache reads are billed)

---

## Caveats

- **Pricing and free-tier limits are volatile.** Numbers reflect public pricing as of May 22, 2026 (gpt-5-mini at $0.25/$2.00 per 1M; DeepSeek V3.2 at $0.26/$0.38 per 1M, released Dec 1, 2025; Voyage `voyage-3-large` first 200M tokens free; Voyage `rerank-2.5` at $0.05/1M; ElevenLabs Creator at $22/mo for 100k credits). Re-check before relying on a budget. ElevenLabs in particular has restructured pricing multiple times in 2024-2026.
- **Hetzner CX22 was discontinued in February 2026** in favor of newer CX23-CX53 plans at similar price points. The €3.79 CX22 figure is what was current when Asaf wrote the article; in May 2026 the equivalent SKU is CX23 at around €3.49-€3.79/mo. The architecture choice (2 vCPU, 4GB) is unchanged.
- **OpenRouter "sticky routing" is a best-effort hint, not a guarantee.** If the cheaper-cache provider is unavailable, you fall through to the next provider and lose the cache.
- **sqlite-vec is pre-1.0** and explicitly notes that breaking changes are possible. Pin the version (`sqlite-vec==0.1.6` or whichever you tested) and do not auto-upgrade.
- **The voice persona is a synthetic approximation, not a clone of Bill Irwin's Interstellar performance.** Cloning the actual TARS voice from movie audio violates ElevenLabs ToS and US right-of-publicity law in multiple states. Voice Design gets you roughly 80% of the way; the deadpan cadence is closer to a "stoic AI assistant" archetype than to the exact film performance.
- **Asaf's article is a single-author claim**, not a third-party-verified case study. The 18 scheduled jobs, the cost figures ("about $22 a month for the box plus a few dollars a day in LLM tokens"), the "two weekends to V1," and the cost gap on cron tiers are his self-reported numbers. They are plausible and consistent with public pricing, but treat them as one operator's data point, not a benchmark.
- **Asaf references "DeepSeek V4 Flash" in the article**; as of May 2026 the current DeepSeek model on OpenRouter is V3.2 (released Dec 1, 2025) with Sparse Attention (DSA). Either the article was written ahead of a renaming, or "V4 Flash" is shorthand for V3.2's Flash configuration. Substitute V3.2 in your own build.
- **The "TARS-from-Interstellar voice persona, off by default per chat" feature is opinionated.** Expect to spend more time tuning the voice prompt and settings than implementing any single scheduled job; the audio is what people will react to first.