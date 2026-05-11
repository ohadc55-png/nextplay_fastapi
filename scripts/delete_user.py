"""Hard-delete a user (account row + every dependent row in every table).

Use this ONLY for operational cleanup (e.g. removing a test account so
the email can be re-registered). It bypasses the standard
`purge_user_data` flow (which intentionally keeps the User row so the
user can come back and upgrade). For a real expired-trial purge, use
`src.auth.purge_service.purge_user_data` instead.

Usage (PowerShell):

    $env:DATABASE_URL = "postgresql://...railway.app:5432/railway"
    python scripts/delete_user.py --email ohadc55@gmail.com --force

Without --force the script prints what WOULD be deleted and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg


# Order matters: child rows first, then anything referencing users.id,
# then users itself. Every FK that points to users.id without ON DELETE
# CASCADE shows up here.
_TABLES_USER_FK: list[tuple[str, str]] = [
    # auth artifacts (CASCADE in schema but we delete explicitly to be sure)
    ("refresh_tokens", "user_id"),
    ("social_accounts", "user_id"),
    ("auth_tokens", "user_id"),
    ("audit_logs", "user_id"),
    # email
    ("email_dispatch", "user_id"),
    ("email_preferences", "user_id"),
    # analytics
    ("api_usage_logs", "user_id"),
    ("page_views", "user_id"),
    ("openai_usage_log", "user_id"),
    # coach
    ("coach_preferences", "user_id"),
    ("onboarding_events", "user_id"),
    ("feedback", "user_id"),
    # clubs (invite redeemed_by)
    ("invite_codes", "redeemed_by"),
    # inquiries
    ("inquiries", "user_id"),
    # memory / chat
    ("entity_observations", "user_id"),
    ("entities", "user_id"),
    ("session_summaries", "user_id"),
    ("memories", "user_id"),
    ("conversations", "user_id"),
    # notebook
    ("notebook_attendance_subquery", ""),  # special-handled below
    ("notebook_entries", "user_id"),
    # plays
    ("play_shares", "user_id"),
    ("compile_cards", "user_id"),
    ("plays", "user_id"),
    # scouting / video
    ("video_annotations_subquery", ""),
    ("video_clips_subquery", ""),
    ("clip_shares", "created_by"),
    ("playlist_items_subquery", ""),
    ("clip_playlists", "user_id"),
    ("scouting_players", "user_id"),
    ("scouting_videos", "user_id"),
    # teams / players
    ("player_game_stats", "user_id"),
    ("player_metrics", "user_id"),
    ("players", "user_id"),
    ("team_profile", "user_id"),
    # uploads
    ("uploads", "user_id"),
    # push
    ("push_subscriptions", "user_id"),
    ("push_history", "user_id"),
    # org Phase 0/1
    ("org_audit_logs", "actor_user_id"),
    ("org_invites", "invited_by"),
    ("user_organizations", "user_id"),
    # organizations.created_by (SET NULL)
    ("organizations_set_null", ""),
]


_INDIRECT: list[tuple[str, str]] = [
    (
        "notebook_attendance_subquery",
        "DELETE FROM notebook_attendance WHERE entry_id IN "
        "(SELECT id FROM notebook_entries WHERE user_id = $1)",
    ),
    (
        "video_annotations_subquery",
        "DELETE FROM video_annotations WHERE video_id IN "
        "(SELECT id FROM scouting_videos WHERE user_id = $1)",
    ),
    (
        "video_clips_subquery",
        "DELETE FROM video_clips WHERE video_id IN "
        "(SELECT id FROM scouting_videos WHERE user_id = $1)",
    ),
    (
        "playlist_items_subquery",
        "DELETE FROM playlist_items WHERE playlist_id IN "
        "(SELECT id FROM clip_playlists WHERE user_id = $1)",
    ),
    (
        "organizations_set_null",
        "UPDATE organizations SET created_by = NULL WHERE created_by = $1",
    ),
]
_INDIRECT_MAP = dict(_INDIRECT)


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM information_schema.tables WHERE table_name = $1",
        table,
    )
    return row is not None


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    row = await conn.fetchrow(
        "SELECT 1 FROM information_schema.columns WHERE table_name = $1 AND column_name = $2",
        table, column,
    )
    return row is not None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("ERROR: DATABASE_URL env var is required.")
    # asyncpg doesn't grok the SQLAlchemy +asyncpg / +psycopg suffix.
    url = url.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres://", "postgresql://"
    )

    conn = await asyncpg.connect(url)
    try:
        row = await conn.fetchrow(
            "SELECT id, email, display_name, subscription_plan FROM users WHERE LOWER(email) = LOWER($1)",
            args.email,
        )
        if not row:
            print(f"No user found with email={args.email}")
            return 0
        uid = row["id"]
        print(f"Target user: id={uid} email={row['email']} plan={row['subscription_plan']}")

        # Dry-run: count rows in each table referencing this user.
        print("\nRow counts referencing this user:")
        total = 0
        for table, fk in _TABLES_USER_FK:
            if table in _INDIRECT_MAP:
                # subquery-driven cleanup; print the parent table for context
                parent = {
                    "notebook_attendance_subquery": ("notebook_attendance",
                        "SELECT COUNT(*) FROM notebook_attendance WHERE entry_id IN (SELECT id FROM notebook_entries WHERE user_id = $1)"),
                    "video_annotations_subquery": ("video_annotations",
                        "SELECT COUNT(*) FROM video_annotations WHERE video_id IN (SELECT id FROM scouting_videos WHERE user_id = $1)"),
                    "video_clips_subquery": ("video_clips",
                        "SELECT COUNT(*) FROM video_clips WHERE video_id IN (SELECT id FROM scouting_videos WHERE user_id = $1)"),
                    "playlist_items_subquery": ("playlist_items",
                        "SELECT COUNT(*) FROM playlist_items WHERE playlist_id IN (SELECT id FROM clip_playlists WHERE user_id = $1)"),
                    "organizations_set_null": ("organizations",
                        "SELECT COUNT(*) FROM organizations WHERE created_by = $1"),
                }.get(table)
                if parent is None:
                    continue
                pt, q = parent
                if not await _table_exists(conn, pt):
                    continue
                try:
                    n = await conn.fetchval(q, uid)
                except Exception as e:
                    print(f"  {pt:35s} ERROR: {e}")
                    continue
                print(f"  {pt:35s} {n}")
                total += int(n or 0)
            else:
                if not await _table_exists(conn, table):
                    continue
                if not await _column_exists(conn, table, fk):
                    continue
                try:
                    n = await conn.fetchval(
                        f"SELECT COUNT(*) FROM {table} WHERE {fk} = $1", uid
                    )
                except Exception as e:
                    print(f"  {table:35s} ERROR: {e}")
                    continue
                print(f"  {table:35s} {n}")
                total += int(n or 0)

        print(f"\nTotal dependent rows: {total}")
        print("Plus the users row itself.\n")

        if not args.force:
            print("DRY RUN. Re-run with --force to actually delete.")
            return 0

        # Real run: wrap everything in a single transaction.
        async with conn.transaction():
            for table, fk in _TABLES_USER_FK:
                if table in _INDIRECT_MAP:
                    sql = _INDIRECT_MAP[table]
                    try:
                        result = await conn.execute(sql, uid)
                        print(f"  {table:35s} {result}")
                    except Exception as e:
                        print(f"  {table:35s} skipped ({e})")
                    continue
                if not await _table_exists(conn, table):
                    continue
                if not await _column_exists(conn, table, fk):
                    continue
                try:
                    result = await conn.execute(
                        f"DELETE FROM {table} WHERE {fk} = $1", uid
                    )
                    print(f"  {table:35s} {result}")
                except Exception as e:
                    print(f"  {table:35s} FAILED ({e})")
                    raise

            result = await conn.execute("DELETE FROM users WHERE id = $1", uid)
            print(f"  {'users':35s} {result}")

        print(f"\nUser {args.email} deleted.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
