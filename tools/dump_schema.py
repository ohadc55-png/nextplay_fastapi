"""Dump live DB schema as markdown.

Reads DATABASE_URL from $env (set by `railway run -- python ...`) or from
the first CLI arg. Writes a structured inventory to stdout: every table,
column type, nullable/default, FKs, indexes, unique + check constraints.

Used as the Phase 1 source of truth for SQLAlchemy model writing.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg


SCHEMA = "public"


async def fetch(conn, sql: str, *args):
    return await conn.fetch(sql, *args)


async def main(url: str) -> None:
    # asyncpg expects postgresql:// (not +asyncpg)
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "")

    conn = await asyncpg.connect(url, ssl="require")

    tables = [r["table_name"] for r in await fetch(conn, """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = $1 AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """, SCHEMA)]

    print(f"# NEXTPLAY live schema inventory\n")
    print(f"**Tables: {len(tables)}** (schema=`{SCHEMA}`)\n")
    print("| # | Table |")
    print("|---|-------|")
    for i, t in enumerate(tables, 1):
        print(f"| {i} | `{t}` |")
    print()

    for t in tables:
        print(f"\n---\n\n## `{t}`\n")

        cols = await fetch(conn, """
            SELECT column_name, data_type, udt_name, is_nullable,
                   column_default, character_maximum_length, numeric_precision, numeric_scale
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position
        """, SCHEMA, t)

        print("### Columns\n")
        print("| Name | Type | Nullable | Default |")
        print("|------|------|----------|---------|")
        for c in cols:
            data_type = c["data_type"]
            udt = c["udt_name"]
            if data_type == "USER-DEFINED":
                type_str = udt
            elif data_type == "character varying" and c["character_maximum_length"]:
                type_str = f"VARCHAR({c['character_maximum_length']})"
            elif data_type == "character" and c["character_maximum_length"]:
                type_str = f"CHAR({c['character_maximum_length']})"
            elif data_type == "numeric" and c["numeric_precision"]:
                if c["numeric_scale"]:
                    type_str = f"NUMERIC({c['numeric_precision']},{c['numeric_scale']})"
                else:
                    type_str = f"NUMERIC({c['numeric_precision']})"
            else:
                type_str = data_type.upper()
            nullable = "✓" if c["is_nullable"] == "YES" else ""
            default = c["column_default"] or ""
            if len(default) > 60:
                default = default[:57] + "..."
            print(f"| `{c['column_name']}` | {type_str} | {nullable} | {default} |")
        print()

        # Primary key
        pks = await fetch(conn, """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = ($1 || '.' || $2)::regclass AND i.indisprimary
            ORDER BY array_position(i.indkey::int[], a.attnum)
        """, SCHEMA, t)
        if pks:
            pk_names = ", ".join(f"`{r['attname']}`" for r in pks)
            print(f"**PK:** {pk_names}\n")

        # Foreign keys
        fks = await fetch(conn, """
            SELECT
                tc.constraint_name,
                kcu.column_name,
                ccu.table_name AS foreign_table,
                ccu.column_name AS foreign_column,
                rc.delete_rule, rc.update_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name
                AND ccu.table_schema = tc.table_schema
            JOIN information_schema.referential_constraints rc
                ON rc.constraint_name = tc.constraint_name
                AND rc.constraint_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
                AND tc.table_schema = $1
                AND tc.table_name = $2
            ORDER BY tc.constraint_name
        """, SCHEMA, t)
        if fks:
            print("**FKs:**\n")
            for fk in fks:
                rules = []
                if fk["delete_rule"] != "NO ACTION":
                    rules.append(f"ON DELETE {fk['delete_rule']}")
                if fk["update_rule"] != "NO ACTION":
                    rules.append(f"ON UPDATE {fk['update_rule']}")
                rule_str = " " + ", ".join(rules) if rules else ""
                print(f"- `{fk['column_name']}` → `{fk['foreign_table']}.{fk['foreign_column']}`{rule_str}")
            print()

        # Unique constraints
        uqs = await fetch(conn, """
            SELECT tc.constraint_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'UNIQUE'
                AND tc.table_schema = $1
                AND tc.table_name = $2
            ORDER BY tc.constraint_name, kcu.ordinal_position
        """, SCHEMA, t)
        if uqs:
            from collections import defaultdict
            uq_groups = defaultdict(list)
            for u in uqs:
                uq_groups[u["constraint_name"]].append(u["column_name"])
            print("**Unique constraints:**\n")
            for name, cols_in_uq in uq_groups.items():
                cols_str = ", ".join(f"`{c}`" for c in cols_in_uq)
                print(f"- {name}: ({cols_str})")
            print()

        # Check constraints
        checks = await fetch(conn, """
            SELECT cc.constraint_name, cc.check_clause
            FROM information_schema.check_constraints cc
            JOIN information_schema.table_constraints tc
                ON cc.constraint_name = tc.constraint_name
                AND cc.constraint_schema = tc.constraint_schema
            WHERE tc.table_schema = $1
                AND tc.table_name = $2
                AND cc.check_clause NOT LIKE '%IS NOT NULL%'
            ORDER BY cc.constraint_name
        """, SCHEMA, t)
        if checks:
            print("**Check constraints:**\n")
            for ch in checks:
                print(f"- `{ch['constraint_name']}`: `{ch['check_clause']}`")
            print()

        # Indexes (non-PK, non-unique-constraint)
        idx = await fetch(conn, """
            SELECT i.relname AS index_name,
                   pg_get_indexdef(i.oid) AS definition,
                   ix.indisunique, ix.indisprimary
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = $1 AND t.relname = $2
                AND NOT ix.indisprimary
            ORDER BY i.relname
        """, SCHEMA, t)
        if idx:
            print("**Indexes:**\n")
            for ix in idx:
                marker = "UNIQUE " if ix["indisunique"] else ""
                print(f"- {marker}`{ix['index_name']}`: `{ix['definition']}`")
            print()

    await conn.close()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_URL", "")
    if not url:
        print("ERROR: pass DATABASE_URL as arg or env var", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(url))
