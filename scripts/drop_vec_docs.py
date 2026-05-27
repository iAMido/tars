"""One-off: drop the vec_docs virtual table so the next db.migrate recreates
it with the current schema (float32 instead of int8).

Safe — vec_docs has zero rows from the schema mismatch, no data to lose.
Notes/messages/briefings are untouched and will be re-indexed on next reindex.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tars.config import load_config

cfg = load_config()
print(f"Opening {cfg.paths.db}")
conn = sqlite3.connect(cfg.paths.db)
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.execute("DROP TABLE IF EXISTS vec_docs")
conn.commit()
print("vec_docs dropped (next bot start will recreate it as float[1024])")
