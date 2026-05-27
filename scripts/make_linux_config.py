"""Convert the local Windows ~/.tars/config.toml into a Linux-suitable copy.

Just rewrites the [paths] section to point at /home/tars/.tars/... and prints
the result on stdout. Pipe it through ssh to land it on the VPS:

    uv run python scripts/make_linux_config.py | ssh tars-vps \\
        "umask 077 && cat > /home/tars/.tars/config.toml && chmod 600 /home/tars/.tars/config.toml"

All secrets are preserved verbatim — only the file paths change.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


def main() -> int:
    src = Path.home() / ".tars" / "config.toml"
    if not src.exists():
        print(f"ERROR: {src} not found", file=sys.stderr)
        return 1

    text = src.read_text(encoding="utf-8")

    # Validate it parses before mutating.
    tomllib.loads(text)

    # Rewrite the three paths under [paths]. Keep formatting unchanged elsewhere.
    replacements = {
        r'^(\s*db\s*=\s*)"[^"]*"': r'\1"/home/tars/.tars/tars.db"',
        r'^(\s*vault\s*=\s*)"[^"]*"': r'\1"/home/tars/.tars/vault"',
        r'^(\s*backups\s*=\s*)"[^"]*"': r'\1"/home/tars/.tars/backups"',
    }
    for pat, repl in replacements.items():
        text = re.sub(pat, repl, text, flags=re.MULTILINE)

    # Final validation: must still parse, and paths must look Unix-y now.
    parsed = tomllib.loads(text)
    paths = parsed.get("paths", {})
    for key in ("db", "vault", "backups"):
        v = str(paths.get(key, ""))
        assert v.startswith("/home/tars/"), f"path {key} not rewritten correctly: {v!r}"

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
