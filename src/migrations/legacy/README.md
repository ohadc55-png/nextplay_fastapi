# Legacy migrations (informational only)

The Flask v1.0-flask codebase ships ~25 idempotent migration scripts in
`backend/migrations/`. They were check-then-alter helpers that ran on every
boot to keep dev DBs in sync with whatever schema was current.

**They are not ported here.** Reasons:

1. **The Alembic baseline already captures the post-migration state.** Every
   column those scripts add is already part of the model definitions. Re-
   running them on a fresh Alembic-generated schema would be a no-op at
   best (idempotent guards short-circuit) or a confusing source of drift at
   worst.
2. **They depend on v1 plumbing.** They use `backend.db_connection.get_connection()`,
   psycopg2 sync, the `PgConnectionWrapper` adapter, and SQLite-flavored
   `INTEGER PRIMARY KEY AUTOINCREMENT` patterns. Porting them to async
   SQLAlchemy would be a rewrite, not a copy.
3. **Data-only migrations are obsolete.** Two scripts (`migrate_memory_team_scope.py`,
   `backfill_memory_embeddings.py`) backfill existing rows. There are no
   existing rows in the new repo's DB at boot, so they are no-ops.

## When to consult the v1 scripts

If you need to understand the **history** of why a column exists or what an
index was meant to optimize, look at the corresponding migration in
[`v1.0-flask:backend/migrations/`](https://github.com/example/basketball_coach_ai/tree/v1.0-flask/backend/migrations).

The 25 scripts (as of `v1.0-flask`):

| Script | Purpose |
|--------|---------|
| `add_user_id_columns.py` | Backfilled user_id on every tenant table |
| `add_team_id_columns.py` | Added team_id + users.active_team_id |
| `add_memory_system.py` | Created memories, entities, entity_observations, session_summaries |
| `add_memory_embeddings.py` | Added embedding_json to memories |
| `add_email_infrastructure.py` | Created email_log, auth_tokens, mailing_lists, mailing_list_members + user columns |
| `add_push_infrastructure.py` | Created push_subscriptions, push_log + user columns |
| `add_admin_tasks.py` | Created admin_tasks + subtasks + comments |
| `add_api_usage_logs.py` | Created api_usage_logs |
| `add_compile_cards.py` | Created scouting_players + compile_cards |
| `add_club_support.py` | Created clubs + user columns |
| `add_subscription_columns.py` | Created invite_codes + user columns |
| `add_file_content_cache.py` | Added uploads.content_cache |
| `add_page_views.py` | Created page_views |
| `add_data_purge_at.py` | Added users.data_purge_at |
| `add_onboarding_events.py` | Created onboarding_events |
| `add_research_url_log.py` | Created research_url_log |
| `add_storage_limit.py` | Added team_profile.extra_storage_gb |
| `add_player_photo.py` | Added players.photo_url |
| `add_player_scout_fields.py` | Added players.scout_summary, metrics_filled_at |
| `add_ip_geo_cache.py` | Created ip_geo_cache |
| `add_notebook_entry_players.py` | Created notebook_entry_players M-M join |
| `fix_api_usage_logs_timestamp.py` | Converted api_usage_logs.created_at TEXT → TIMESTAMP |
| `migrate_memory_team_scope.py` | Data-only: moved style/preference/philosophy memories to team_id=NULL |
| `backfill_memory_embeddings.py` | Data-only: computed embeddings for existing memories |
| `add_performance_indexes.py` | Composite indexes for query optimization |

## Going forward

New schema changes happen via `alembic revision --autogenerate -m "..."`.
Hand-edit the generated migration if autogenerate misses something subtle.
