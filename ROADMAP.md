# TARS roadmap

Honest assessment of what's next after V1.0.0. Ranked by my recommendation, not by alphabet.

## Principle before list

**Use V1 for at least a week before building anything else.** Real usage surfaces real needs. Speculative features built without usage data tend to sit unused. The dashboard at `https://tars-prod.tail28626d.ts.net` is your ground truth — what panels do you actually look at? Which Telegram commands do you actually send?

If after a week you have 2-3 concrete observations like "I keep wanting X" or "Y never fires usefully" — those drive the list better than this doc.

---

## Tier 1 — Promised today, queued

| Item | Effort | Status |
|---|---|---|
| **Obsidian one-way mirror** | ~1 hour | Promised after V1 ships. TARS writes notes to `~/vault/YYYY-MM-DD.md`. Syncthing daemon on VPS + laptop. Obsidian on desktop points at the synced folder. No two-way (TARS doesn't read back edits). |

That's it for Tier 1. Everything below is optional.

---

## Tier 2 — V1.1 candidates (deferred from original plan, real value)

| Item | Effort | Value | Cost impact |
|---|---|---|---|
| **ElevenLabs voice** (Voice Design, not cloning Bill Irwin) | ~3 hours | Marquee feature in the artifact. TARS reads morning briefing aloud via Telegram voice notes. Cool factor: very high. Practical value: depends if you listen. | +$22/mo Creator plan |
| **Storage Box as 2nd backup destination** | ~30 min | True account-isolation for backups. B2 alone is enough, but two destinations is safer. | +€3.49/mo |
| **`stale_thread_summarize` (Mondays)** | ~30 min | Roll old conversations into single notes. Memory hygiene as conversations accumulate. | Negligible |
| **`news_sources_refresh` (hourly)** + tracked domains via RSS | ~1 hour | Pull from RSS feeds, populate brain_docs. Lets you ask "what's new with X this week?" | Negligible |

---

## Tier 3 — V1.2 quality-of-life (organic, build when pain appears)

I'd build these only when the corresponding pain shows up in real use.

| Item | Effort | Triggered by... |
|---|---|---|
| Streaming Telegram responses (edit message as tokens arrive) | ~30 min | "the wait feels long" |
| Calendar conflict detection in briefing | ~30 min | Double-booking yourself |
| Image handling (vision models on Telegram photos) | ~2 hours | Wanting to send a photo of a whiteboard / receipt / page |
| Voice input (transcribe incoming voice notes via Whisper) | ~2 hours | Hands-busy moments |
| Hebrew-aware entity extraction tuning | ~1 hour | Entity store missing Hebrew names |
| PWA / mobile-friendly dashboard | ~2 hours | Checking dashboard from phone |
| Auto-suggest follow-up due times based on history | ~3 hours | Tired of always saying "tomorrow at 3pm" |
| Multi-chat support (work + personal Telegram allowlist) | ~1 hour | Wanting work/personal separation |
| Cost panel for Voyage embeddings / reranker tracking | ~30 min | Curious about free-tier burn |

---

## Tier 4 — Remaining scheduled jobs from the original artifact

The artifact lists 18 jobs. We shipped 5. Of the other 13, only a few would actually pay off:

| Job | Worth building? |
|---|---|
| `news_sources_refresh` | **Yes** if you track specific companies/domains (already in Tier 2) |
| `competitive_intel_scan` (9/13/17) | Yes if you have competitors to watch |
| `entity_dedup` (nightly) | Maybe — only after the entity store has >100 entries |
| `vault_sweep` (every 10m) | Required IF you go to Obsidian two-way (Tier 5 work) |
| `voice_quota_check` (hourly) | Required IF you add ElevenLabs |
| `cost_rollup_daily` (midnight) | Cosmetic; the dashboard already shows daily aggregates |
| `lab_notebook_digest` (Fridays) | Nice for personal review |
| `health_self_ping` (every 1m) | Already covered by systemd `Restart=on-failure` |
| `cooldown_clear` (every 5m) | Unnecessary; restart fixes any stuck state |
| `stale_thread_summarize` (Mondays) | **Yes** (already in Tier 2) |

---

## Tier 5 — V2 territory (only if explicitly earned)

Per the original artifact and convictions:

| Item | When to consider |
|---|---|
| Substrate refactor to LangGraph or Hermes Agent | If tool catalog grows past 12-15 functions AND you write your own routing logic 3+ times. **Currently at 7 tools.** Margin to spare. |
| Self-hosted LLM (Ollama, vLLM) | If $0.05/day in tokens ever becomes a problem. Spoiler: it won't. |
| Federated calendar / multi-source emails | If you start mixing work + personal accounts |
| Obsidian **two-way** (read back vault edits) | After one-way mirror has been live for ≥1 month. Needs file watcher + idempotent re-import + conflict handling. ~1 day. |
| Multi-user | **Never.** Single-user is a feature. The Tailscale-only ingress and hardcoded `allowed_chat_ids` are doing real security work. |
| LangChain | **Never.** Footgun-per-LoC is high; abstractions inject timestamps and break the cache anchor. Don't. |

---

## My honest single recommendation

If you want one sentence:

> **Ship Obsidian one-way mirror this weekend, then use TARS for two weeks, then look at this list again.**

You'll be surprised by what graduates from Tier 3 to Tier 1 and what falls off the list entirely.

---

## Anti-roadmap (things explicitly NOT planned)

- LangChain integration. Ever.
- Multi-tenant / multi-user mode.
- Public-facing web UI. Tailnet is the boundary.
- Cloning a real human voice (Bill Irwin or otherwise). Voice Design only.
- "Agent that does X for me autonomously without confirmation." Citation-gating exists for a reason.
- Generic chat-style memory ("ChatGPT but personal"). Notes + entities + follow-ups + retrieval are the model. Stay disciplined.
