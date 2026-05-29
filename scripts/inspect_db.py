"""Quick read-only inspector for tars.db. Run with: uv run python scripts/inspect_db.py"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tars.config import load_config

cfg = load_config()
db = sqlite3.connect(cfg.paths.db)
db.row_factory = sqlite3.Row

print(f"DB: {cfg.paths.db}\n")

print("=== cost_ledger (latest 20) ===")
rows = db.execute(
    "SELECT id, datetime(ts,'unixepoch','localtime') AS when_, provider, model, tier, "
    "prompt_tokens AS p, completion_tokens AS c, cached_tokens AS cached, "
    "printf('%.6f', cost_usd) AS usd "
    "FROM cost_ledger ORDER BY id DESC LIMIT 20"
).fetchall()
if not rows:
    print("  (empty)")
for r in rows:
    print(f"  #{r['id']:3d} {r['when_']} {r['provider']:>10s} {r['model']:<35s} "
          f"tier={r['tier']:<16s} p={r['p']:>5d} c={r['c']:>4d} cached={r['cached']:>4d} ${r['usd']}")

print("\n=== conversations ===")
for r in db.execute("SELECT thread_key, datetime(created_at,'unixepoch','localtime') AS created FROM conversations"):
    print(f"  {r['thread_key']:<30s} created={r['created']}")

print("\n=== messages (latest 10) ===")
for r in db.execute(
    "SELECT id, thread_key, role, substr(content,1,80) AS preview, "
    "model, printf('%.6f', cost_usd) AS usd "
    "FROM messages ORDER BY id DESC LIMIT 10"
).fetchall():
    print(f"  #{r['id']:3d} {r['thread_key']:<20s} {r['role']:<10s} ${r['usd']} | {r['preview']}")

print("\n=== notes ===")
for r in db.execute("SELECT id, datetime(created_at,'unixepoch','localtime') AS created, source, body, status FROM notes ORDER BY id DESC LIMIT 10"):
    print(f"  #{r['id']:3d} {r['created']} src={r['source']} status={r['status']:6s} | {r['body'][:80]}")

print("\n=== follow_ups ===")
rows = db.execute(
    "SELECT fu.id, fu.note_id, fu.status, fu.promised_to, "
    "datetime(fu.due_at, 'unixepoch', 'localtime') AS due, "
    "fu.reopened_count, n.body "
    "FROM follow_ups fu JOIN notes n ON n.id = fu.note_id "
    "ORDER BY fu.id DESC LIMIT 20"
).fetchall()
if not rows:
    print("  (none)")
for r in rows:
    print(f"  #{r['id']:3d} note={r['note_id']} {r['status']:7s} due={r['due']} "
          f"to={r['promised_to']} reopens={r['reopened_count']} | {r['body'][:60]}")

print("\n=== entities ===")
ents = db.execute("SELECT id, canonical, kind FROM entities ORDER BY id").fetchall()
if not ents:
    print("  (none)")
for e in ents:
    aliases = db.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (e['id'],)
    ).fetchall()
    alist = ", ".join(a['alias'] for a in aliases)
    print(f"  #{e['id']:3d} [{e['kind']:8s}] {e['canonical']}  aliases=[{alist}]")

print("\n=== totals ===")
total = db.execute("SELECT printf('%.6f', SUM(cost_usd)) AS t, COUNT(*) AS n FROM cost_ledger").fetchone()
print(f"  total LLM cost so far: ${total['t']} across {total['n']} calls")

db.close()
