"""Compare live Postgres schema vs SQLAlchemy models.

Reads prod URL from $env:NEXTPLAY_PROD_URL, queries information_schema, and
diffs the result against `Base.metadata` (populated by importing src.models).

Outputs a markdown report on stdout. Read-only: SELECT queries only.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict

import asyncpg

# Importing this side-effect-registers all 47 ORM classes.
from src.core.database import Base  # noqa: E402
import src.models  # noqa: F401, E402

SCHEMA = "public"


def _normalize_pg_type(data_type: str, udt_name: str, char_max: int | None,
                      num_prec: int | None, num_scale: int | None) -> str:
    """Render Postgres column type as a comparable string."""
    if data_type == "USER-DEFINED":
        return udt_name.upper()
    if data_type == "character varying":
        return f"VARCHAR({char_max})" if char_max else "VARCHAR"
    if data_type == "character":
        return f"CHAR({char_max})" if char_max else "CHAR"
    if data_type == "numeric" and num_prec:
        if num_scale:
            return f"NUMERIC({num_prec},{num_scale})"
        return f"NUMERIC({num_prec})"
    return data_type.upper()


def _normalize_sa_type(col) -> str:  # noqa: ANN001
    """Render SQLAlchemy column type as comparable to Postgres."""
    t = col.type
    cls = t.__class__.__name__.upper()
    # Float with precision <= 24 maps to REAL; >24 (or unset) is DOUBLE PRECISION.
    if cls == "FLOAT":
        precision = getattr(t, "precision", None)
        if precision and precision <= 24:
            return "REAL"
        return "DOUBLE PRECISION"
    # Map SQLAlchemy → Postgres-flavored
    mapping = {
        "TEXT": "TEXT",
        "STRING": "TEXT",
        "INTEGER": "INTEGER",
        "BIGINTEGER": "BIGINT",
        "SMALLINTEGER": "SMALLINT",
        "BOOLEAN": "BOOLEAN",
        "DATETIME": "TIMESTAMP WITHOUT TIME ZONE",
        "DATE": "DATE",
        "JSONTEXT": "TEXT",
    }
    return mapping.get(cls, cls)


async def fetch_prod_schema(conn) -> dict:
    """Return {table: {column_name: {type, nullable, default}}}."""
    out: dict[str, dict] = defaultdict(dict)
    rows = await conn.fetch("""
        SELECT table_name, column_name, data_type, udt_name, is_nullable,
               column_default, character_maximum_length, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = $1
        ORDER BY table_name, ordinal_position
    """, SCHEMA)
    for r in rows:
        col_type = _normalize_pg_type(
            r["data_type"], r["udt_name"], r["character_maximum_length"],
            r["numeric_precision"], r["numeric_scale"],
        )
        out[r["table_name"]][r["column_name"]] = {
            "type": col_type,
            "nullable": r["is_nullable"] == "YES",
            "default": (r["column_default"] or "").split("::")[0],
        }
    return dict(out)


def model_schema() -> dict:
    """Render Base.metadata as {table: {column_name: {type, nullable, default}}}."""
    out: dict[str, dict] = {}
    for table_name, table in Base.metadata.tables.items():
        cols = {}
        for col in table.columns:
            default = ""
            if col.server_default is not None:
                default = str(col.server_default.arg).strip("'\"") if hasattr(col.server_default, "arg") else str(col.server_default)
            cols[col.name] = {
                "type": _normalize_sa_type(col),
                "nullable": bool(col.nullable),
                "default": default,
            }
        out[table_name] = cols
    return out


def diff_tables(prod: dict, models: dict) -> str:
    lines = ["# NEXTPLAY schema diff: prod vs models\n"]
    prod_t = set(prod)
    model_t = set(models)
    only_models = sorted(model_t - prod_t)
    only_prod = sorted(prod_t - model_t)
    shared = sorted(prod_t & model_t)

    lines.append(f"**Prod tables:** {len(prod_t)}  ")
    lines.append(f"**Model tables:** {len(model_t)}  ")
    lines.append(f"**Shared:** {len(shared)}  \n")

    lines.append("\n## Tables in models but missing from prod")
    if only_models:
        for t in only_models:
            lines.append(f"- `{t}` ({len(models[t])} cols)")
    else:
        lines.append("(none — everything in models exists in prod)")

    lines.append("\n## Tables in prod but missing from models")
    if only_prod:
        for t in only_prod:
            lines.append(f"- `{t}` ({len(prod[t])} cols)")
    else:
        lines.append("(none — every prod table is modeled)")

    # Per-table column diffs (shared only)
    lines.append("\n## Column-level diffs (shared tables)\n")
    drift_count = 0
    for t in shared:
        p = prod[t]
        m = models[t]
        only_in_model = sorted(set(m) - set(p))
        only_in_prod = sorted(set(p) - set(m))
        type_diffs = []
        nullable_diffs = []
        for col in sorted(set(p) & set(m)):
            if p[col]["type"] != m[col]["type"]:
                type_diffs.append((col, p[col]["type"], m[col]["type"]))
            if p[col]["nullable"] != m[col]["nullable"]:
                nullable_diffs.append((col, p[col]["nullable"], m[col]["nullable"]))
        if not (only_in_model or only_in_prod or type_diffs or nullable_diffs):
            continue
        drift_count += 1
        lines.append(f"### `{t}`")
        if only_in_model:
            lines.append(f"  - **Columns in models, missing from prod:** {', '.join(only_in_model)}")
        if only_in_prod:
            lines.append(f"  - **Columns in prod, missing from models:** {', '.join(only_in_prod)}")
        for col, p_t, m_t in type_diffs:
            lines.append(f"  - **Type differs** for `{col}`: prod=`{p_t}`, model=`{m_t}`")
        for col, p_n, m_n in nullable_diffs:
            lines.append(f"  - **Nullable differs** for `{col}`: prod={p_n}, model={m_n}")
        lines.append("")

    if drift_count == 0 and shared:
        lines.append("(zero column-level drift across all shared tables)")

    lines.append(f"\n---\n\nDrifted tables: **{drift_count} / {len(shared)}**")
    return "\n".join(lines)


async def main() -> None:
    url = os.environ.get("NEXTPLAY_PROD_URL", "")
    if not url:
        print("ERROR: set NEXTPLAY_PROD_URL", file=sys.stderr)
        sys.exit(1)
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "")
    conn = await asyncpg.connect(url)
    try:
        prod = await fetch_prod_schema(conn)
        models = model_schema()
        report = diff_tables(prod, models)
        print(report)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
