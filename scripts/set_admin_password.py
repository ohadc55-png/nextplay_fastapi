"""Reset the admin password.

Prompts for a new password (hidden input), generates a bcrypt hash at
cost factor 12, and updates `ADMIN_PASSWORD_HASH=...` in `.env` in
place. Run from the repo root:

    python scripts/set_admin_password.py

After it finishes, restart uvicorn for the new hash to take effect.
"""

from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

import bcrypt

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def _read_env() -> str:
    if not ENV_PATH.exists():
        sys.exit(f"ERROR: {ENV_PATH} does not exist. Create it first.")
    return ENV_PATH.read_text(encoding="utf-8")


def _replace_or_append(content: str, key: str, value: str) -> str:
    """Replace the line `KEY=...` if present, otherwise append it.
    Multi-line bcrypt hashes don't happen, so a single-line regex is
    fine — bcrypt output is one $-delimited string."""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(content):
        return pattern.sub(line, content, count=1)
    sep = "" if content.endswith("\n") or content == "" else "\n"
    return f"{content}{sep}{line}\n"


def main() -> None:
    pw1 = getpass.getpass("New admin password: ")
    if not pw1:
        sys.exit("ERROR: empty password.")
    if len(pw1) < 8:
        sys.exit("ERROR: password must be at least 8 characters.")
    pw2 = getpass.getpass("Confirm password:   ")
    if pw1 != pw2:
        sys.exit("ERROR: passwords don't match.")

    hashed = bcrypt.hashpw(pw1.encode("utf-8"), bcrypt.gensalt(rounds=12))
    hash_str = hashed.decode("utf-8")

    content = _read_env()
    updated = _replace_or_append(content, "ADMIN_PASSWORD_HASH", hash_str)
    ENV_PATH.write_text(updated, encoding="utf-8")

    print()
    print(f"OK: ADMIN_PASSWORD_HASH updated in {ENV_PATH}")
    print("Restart uvicorn for the change to take effect.")


if __name__ == "__main__":
    main()
