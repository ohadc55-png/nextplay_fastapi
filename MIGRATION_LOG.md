# Migration Log

Chronological record of large migration milestones. Each entry summarizes
the change set, the verification gate, and any follow-up captured in
[MIGRATION_TODO.md](MIGRATION_TODO.md).

---

## 2026-05-10 — Phase 0 Enterprise (multi-org foundation)

Adds the schema + auth + middleware + repository + audit foundation that
the Sha'ar Shivyon Enterprise rollout (640 teams, 500 users) builds on.
Private-coach behaviour is unchanged.

### Sub-phases

| # | Scope | Migration ID | Key files |
|---|---|---|---|
| 0.0 | Schema + models + migration | `468e539efa17` | [src/models/organizations.py](src/models/organizations.py), [src/models/regions.py](src/models/regions.py), [src/models/branches.py](src/models/branches.py), [src/models/user_organizations.py](src/models/user_organizations.py), [src/models/org_audit.py](src/models/org_audit.py), [src/models/org_invites.py](src/models/org_invites.py); +3 columns on `users`/`team_profile`/`auth_tokens` |
| 0.1 | Repositories + audit service | — | [src/repositories/org_scoped_repository.py](src/repositories/org_scoped_repository.py) + 6 concrete repos; [src/services/org_audit_service.py](src/services/org_audit_service.py) |
| 0.2 | Middleware + dependencies | — | [src/middleware/org_context.py](src/middleware/org_context.py), [src/api/deps/org_auth.py](src/api/deps/org_auth.py); [src/core/database.py:get_db](src/core/database.py) extended for Postgres `set_config('app.current_org_id', ...)` |
| 0.3 | `/org/*` routes + templates | — | [src/api/org.py](src/api/org.py), [src/api/org_pages.py](src/api/org_pages.py), [src/schemas/org.py](src/schemas/org.py), [frontend/templates/org/](frontend/templates/org/) (login, role_select, dashboard) |
| 0.4 | System Admin extensions | — | [src/api/admin_orgs.py](src/api/admin_orgs.py) — `/admin/api/orgs/*` JSON |
| 0.5 | Email invites | — | [src/services/email_service.py:send_org_invite_email](src/services/email_service.py) reuses existing Resend dispatcher in [src/services/email.py](src/services/email.py); invite + accept endpoints in `org.py` |
| 0.6 | RLS migration + verifier | `9a3f2b1c4e7d` | Postgres-only alembic revision (no-op on SQLite); [tools/verify_rls.py](tools/verify_rls.py) standalone smoke script |

### What changed in existing files

- [src/models/users.py](src/models/users.py) — `+ active_organization_id` (FK organizations, nullable), `+ organizations` relationship (lazy="raise")
- [src/models/teams.py](src/models/teams.py) — `+ organization_id`, `+ branch_id` (both FK SET NULL, nullable), `+ idx_team_profile_org`/`_branch`
- [src/models/auth.py](src/models/auth.py) — `auth_tokens.user_id` widened to nullable (org invites can pre-date their invitee's `users` row)
- [src/services/email_service.py](src/services/email_service.py) — `_issue_auth_token` now returns `(raw, token_id)`; existing callers updated
- [src/main.py](src/main.py) — register `OrgContextMiddleware` (BEFORE `SessionMiddleware` in add order = AFTER on request path); include `org_router`, `org_pages_router`, `admin_orgs_router`
- [tests/api/conftest.py](tests/api/conftest.py) — `+ seed_org_admin`, `+ org_admin_client` fixtures

### Architecture invariants

- New invariant **§13 Multi-org tenancy** added to [CLAUDE.md](CLAUDE.md). Three independent auth contexts (Coach JWT, System Admin session, Org Admin session) coexist on a single signed cookie via independent session keys (`org_user_id` / `org_active_org_id` / `org_active_role` vs `admin_email`).
- **404-not-403** rule enforced at three points: `get_current_org_membership` (session tamper), `require_role` (privilege mismatch), and route bodies (`get_for_org` returning None).
- Three layers of tenancy defense — middleware → `OrgScopedRepository` → Postgres RLS — with the third being a no-op on SQLite (so tests + CI alembic-smoke don't cover RLS; production verification is `tools/verify_rls.py`).

### Verification

- 52 new tests written: `tests/repositories/test_org_repos.py` (9), `tests/services/test_org_audit_service.py` (4), `tests/middleware/test_org_context.py` (3), `tests/api/test_org_auth.py` (11), `tests/api/test_org_pages.py` (7), `tests/api/test_admin_orgs.py` (10), `tests/api/test_org_invites.py` (8). All pass.
- Smoke run of `tests/api tests/auth tests/middleware tests/repositories tests/services` (excluding chat/crew/research): 531 passed + 1 xfailed (documented). One known pre-existing `test_home_unauthed_returns_401` failure remains (verified existing pre-Phase-0 via `git stash`); that test belongs to the user's untracked `test_pages.py` WIP and is unrelated to this work.
- App boots: 229 routes registered, including all 15 `/org/*` and `/admin/api/orgs/*` paths.
- `alembic upgrade head` clean to `9a3f2b1c4e7d` against local SQLite (with the RLS revision as a no-op).

### Deferred (tracked in [MIGRATION_TODO.md](MIGRATION_TODO.md))

- RLS verification against Postgres (run `python tools/verify_rls.py` against staging before cutover).
- Background tasks bypass `get_db` and the `app.current_org_id` GUC. Phase 0 background work doesn't touch org-scoped tables — safe for now.
- Clubs → Orgs migration (legacy `clubs` table coexists with `organizations` for now).
- Org Admin HTML pages beyond Phase 0 set (members / branches / regions / teams / audit pages land in Phase 1 with the bulk-import flow).
- System Admin HTML pages for `/admin/orgs` (only JSON endpoints in Phase 0; HTML in Phase 1).
