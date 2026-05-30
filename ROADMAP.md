# TARS roadmap

Honest assessment of what's next after V1.0.0. Ranked by my recommendation, not by alphabet.

## Principle before list

**Use V1 for at least a week before building anything else.** Real usage surfaces real needs. Speculative features built without usage data tend to sit unused. The dashboard at `https://tars-prod.tail28626d.ts.net` is your ground truth — what panels do you actually look at? Which Telegram commands do you actually send?

If after a week you have 2-3 concrete observations like "I keep wanting X" or "Y never fires usefully" — those drive the list better than this doc.

---

## Tier 1 — DONE

| Item | Status |
|---|---|
| **Obsidian one-way mirror** | ✅ Shipped V1.1 — vault writer + Syncthing + paired with Windows Obsidian vault. Files at `C:\Users\ido\Obsidian\Ido\tars\`. |

## Tier 2 — V1.1 mostly DONE; 2 items deferred to V2

| Item | Effort | Status |
|---|---|---|
| **`stale_thread_summarize`** (Sun 17:00) | ~30 min | ✅ Shipped V1.1 |
| **`news_sources_refresh`** (hourly) + RSS infrastructure | ~1 hour | ✅ Shipped V1.1 with `/feeds` Telegram management |
| **ElevenLabs voice** (Voice Design, not cloning Bill Irwin) | ~3 hours | ⏳ **Deferred to V2** — marquee feature, TARS reads morning briefing aloud via Telegram voice notes. Cool factor: very high. Practical value: depends if you listen. Cost: +$22/mo Creator plan. |
| **Storage Box as 2nd backup destination** | ~30 min | ⏳ **Deferred to V2** — true account-isolation for backups. B2 alone is enough; two destinations is safer. Cost: +€3.49/mo. |

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

## Tier 4 — Scheduled jobs from the original artifact

The artifact lists 18 jobs. We shipped **13**:

| Job | Status |
|---|---|
| `morning_briefing` (05:00) | ✅ |
| `email_summary` (30m, quiet 22-07) | ✅ |
| `calendar_pull` (15m) | ✅ |
| `brain_reindex` (15m, diff mode) | ✅ |
| `weekly_followup_reconcile` (Sun 18:00) | ✅ |
| `cooldown_clear` (5m) | ✅ |
| `cost_rollup_daily` (00:05) | ✅ |
| `vault_sweep` (10m) | ✅ |
| `news_sources_refresh` (hourly) | ✅ |
| `competitive_intel_scan` (9/13/17) | ✅ |
| `entity_dedup` (02:00) | ✅ |
| `stale_thread_summarize` (Sun 17:00) | ✅ |
| `lab_notebook_digest` (Thu 16:00) | ✅ |
| `voice_quota_check` (hourly) | ⏳ V2 with ElevenLabs |
| `health_self_ping` (every 1m) | ❌ skipped — systemd `Restart=on-failure` covers this |
| `restic_b2_push` (07:00) | ✅ shipped as systemd timer (`tars-backup.timer`) at 06:00 |
| `restic_offsite_push` (08:00) | ⏳ V2 with Storage Box |
| `vault_sweep` two-way ingest | ⏳ V2 if Obsidian editing surfaces conflicts |

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
