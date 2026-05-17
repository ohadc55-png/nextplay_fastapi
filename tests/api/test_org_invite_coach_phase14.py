"""Phase 14 — coach-invite + redeem flow.

Behavior under test:

INVITE (POST /org/api/users/invite)
  - Coach invite carries an optional `team_id`. Response echoes it.
  - team_id on a non-coach invite → 422 ValidationError ("team_id_only_for_coach").
  - team_id of a team already assigned to another coach → 409
    ConflictError ("team_already_has_coach"). The inviter must detach
    the existing coach via the team page first; no silent replacement.
  - team_id of a team in a different org → 404 (cross-tenant cloak).

REDEEM (POST /org/api/invites/redeem)
  New user (email not seen before)
    - Coach + team_id: user is created as a CLUB MEMBER from day one
      (`subscription_plan="club"`, `trial_ends_at=None`, `club_id` points
      at the org's enterprise Club row), the team's `user_id` is stamped,
      `user.active_team_id` is set, redirect is `/home` (Coach App).
    - Coach WITHOUT team_id: same club setup, no team assignment,
      redirect still `/home`.
    - Non-coach (PM): club setup, no team assignment, redirect is the
      slug-aware org dashboard (NOT `/home`).
    - JWT auth cookies are set so the Coach App opens without a second
      login round-trip (proven by hitting `/api/auth/me`).

  Existing user (email already has an active account — TRIAL user case)
    - Correct password: same `user_id` is REUSED — no second account is
      created. The trial is converted to club (`subscription_plan` flips
      to "club", `trial_ends_at` cleared, `data_purge_at` cleared,
      `club_id` set). Team is assigned to them. Display name is NOT
      overwritten if already set.
    - Wrong password: 404 cloak ("Invite not found") — must not confirm
      the email is registered.

  Race safety
    - If the team gained a coach between invite + redeem, the redeem
      succeeds (user is created, membership minted, club status set) but
      does NOT overwrite `team.user_id` and does NOT set
      `user.active_team_id`. The original coach is preserved.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Local fixtures — seed programs / regions / teams via the test session
# ---------------------------------------------------------------------------


async def _seed_team(
    api_session_factory,
    *,
    org_id: int,
    name: str = "U14 Yellow",
    program_id: int | None = None,
    region_id: int | None = None,
    user_id: int | None = None,
) -> int:
    from src.models.teams import TeamProfile

    async with api_session_factory() as s:
        team = TeamProfile(
            team_name=name,
            organization_id=org_id,
            program_id=program_id,
            region_id=region_id,
            user_id=user_id,
        )
        s.add(team)
        await s.commit()
        return team.id


async def _seed_another_org(api_session_factory) -> int:
    from src.models.organizations import Organization

    async with api_session_factory() as s:
        org = Organization(slug="other-org", name="Other Org")
        s.add(org)
        await s.commit()
        return org.id


async def _issue_coach_invite(
    client: AsyncClient,
    *,
    email: str = "newcoach@org.test",
    team_id: int | None = None,
) -> dict:
    payload: dict = {"email": email, "role": "coach"}
    if team_id is not None:
        payload["team_id"] = team_id
    r = await client.post("/org/api/users/invite", json=payload)
    return r


# ---------------------------------------------------------------------------
# Section A — invite-side team_id validation
# ---------------------------------------------------------------------------


async def test_coach_invite_with_team_id_succeeds_and_echoes_team_id(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    r = await _issue_coach_invite(org_admin_client, team_id=team_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "coach"
    assert body["team_id"] == team_id


async def test_non_coach_invite_with_team_id_is_rejected(
    org_admin_client: AsyncClient, api_session_factory,
):
    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    r = await org_admin_client.post(
        "/org/api/users/invite",
        json={
            "email": "pm@org.test",
            "role": "program_manager",
            "team_id": team_id,
        },
    )
    assert r.status_code == 422, r.text
    assert r.json().get("code") == "team_id_only_for_coach"


async def test_coach_invite_rejects_team_already_assigned(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Team already has a coach → 409 (no silent replacement)."""
    from src.auth.password_service import hash_password
    from src.models.users import User

    org_id = org_admin_client.org_seed["organization_id"]

    # Seed an existing coach + a team they already own.
    async with api_session_factory() as s:
        existing_coach = User(
            email="existing-coach@org.test",
            password_hash=hash_password("Sup3rSecure!"),
            display_name="Existing Coach",
        )
        s.add(existing_coach)
        await s.commit()
        existing_coach_id = existing_coach.id

    team_id = await _seed_team(
        api_session_factory, org_id=org_id, user_id=existing_coach_id,
    )

    r = await _issue_coach_invite(org_admin_client, team_id=team_id)
    assert r.status_code == 409, r.text
    assert r.json().get("code") == "team_already_has_coach"


async def test_coach_invite_rejects_team_in_other_org(
    org_admin_client: AsyncClient, api_session_factory,
):
    """Cross-org team_id → 404 cloak (don't confirm the team exists)."""
    other_org_id = await _seed_another_org(api_session_factory)
    foreign_team_id = await _seed_team(api_session_factory, org_id=other_org_id)

    r = await _issue_coach_invite(org_admin_client, team_id=foreign_team_id)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Section B — redeem flow, NEW user
# ---------------------------------------------------------------------------


async def test_redeem_coach_with_team_creates_club_member_and_assigns_team(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """New-email path: user is created as club member, team is assigned,
    active_team_id is set, redirect lands the user in the Coach App."""
    from src.models.teams import TeamProfile
    from src.models.users import User

    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    invite = await _issue_coach_invite(
        org_admin_client, email="placeholder@org.test", team_id=team_id,
    )
    assert invite.status_code == 201, invite.text
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "newcoach@org.test",
            "full_name": "New Coach",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # Coach role → Coach App, regardless of slug-flag.
    assert body["redirect"] == "/"

    # Verify club membership + team assignment.
    async with api_session_factory() as s:
        user = (await s.execute(
            select(User).where(User.email == "newcoach@org.test")
        )).scalar_one()
        assert user.subscription_plan == "club"
        assert user.trial_ends_at is None
        assert user.club_id is not None  # enterprise Club lazily created
        assert user.active_team_id == team_id

        team = await s.get(TeamProfile, team_id)
        assert team.user_id == user.id


async def test_redeem_coach_without_team_still_lands_on_home(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Coach invite issued with no team_id: still a club member, still
    /home, but no team assignment + no active_team_id."""
    from src.models.users import User

    invite = await _issue_coach_invite(
        org_admin_client, email="solo-coach@org.test",
    )
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "solo-coach-real@org.test",
            "full_name": "Solo Coach",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["redirect"] == "/"

    async with api_session_factory() as s:
        user = (await s.execute(
            select(User).where(User.email == "solo-coach-real@org.test")
        )).scalar_one()
        assert user.subscription_plan == "club"
        assert user.active_team_id is None


async def test_redeem_non_coach_lands_on_org_dashboard_not_home(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Non-coach roles (PM/RM/admin/viewer) keep going to the org dashboard
    even after the Phase 14 changes — /home is coach-only."""
    from src.models.programs import Program

    org_id = org_admin_client.org_seed["organization_id"]
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="בועטות")
        s.add(prog)
        await s.commit()
        prog_id = prog.id

    r = await org_admin_client.post(
        "/org/api/users/invite",
        json={"email": "pm@org.test", "role": "program_manager", "program_id": prog_id},
    )
    assert r.status_code == 201, r.text
    code = r.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "pm-real@org.test",
            "full_name": "PM Real",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # NOT "/" — PM goes to the org dashboard. Accept either form depending
    # on the slug-URL flag.
    assert body["redirect"] != "/"
    assert body["redirect"].endswith("/dashboard")


async def test_redeem_sets_jwt_cookies_so_coach_app_opens(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """After redeem, the client should hit Coach-App endpoints (under JWT
    auth) without a second login. The /api/auth/me round-trip is the
    cheapest proof — it requires a valid access_token cookie."""
    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    invite = await _issue_coach_invite(
        org_admin_client, email="jwt@org.test", team_id=team_id,
    )
    code = invite.json()["short_code"]

    # `/api/auth/me` serializes the User row through `EmailStr`, which
    # rejects the `.test` TLD as a special-use name. Use `@example.com`
    # for the invitee identity (matches the convention in conftest.py).
    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "jwt-real@example.com",
            "full_name": "Jwt Real",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200

    # Now hit a Coach-App endpoint that uses JWT auth — must succeed.
    me = await api_client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "jwt-real@example.com"


# ---------------------------------------------------------------------------
# Section C — redeem flow, EXISTING user (trial → club conversion)
# ---------------------------------------------------------------------------


async def test_redeem_existing_trial_user_with_correct_password_converts_to_club(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Existing trial user + correct password → SAME user_id, but now club."""
    from datetime import datetime, timedelta

    from src.auth.password_service import hash_password
    from src.models.teams import TeamProfile
    from src.models.users import User

    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    # Seed a pre-existing trial coach (has their own account, no club).
    trial_purge_at = datetime.utcnow() + timedelta(days=30)
    async with api_session_factory() as s:
        trial_user = User(
            email="trial@org.test",
            password_hash=hash_password("Sup3rSecure!"),
            display_name="Trial User",
            email_verified=True,
            subscription_plan="trial",
            trial_ends_at=(datetime.utcnow() + timedelta(days=7)).isoformat(),
            data_purge_at=trial_purge_at,
        )
        s.add(trial_user)
        await s.commit()
        trial_user_id = trial_user.id

    invite = await _issue_coach_invite(
        org_admin_client, email="placeholder@org.test", team_id=team_id,
    )
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "trial@org.test",
            "full_name": "Ignored — display already set",
            "password": "Sup3rSecure!",  # the same password the trial user has
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["redirect"] == "/"

    async with api_session_factory() as s:
        # SAME row — no duplicate account.
        rows = (await s.execute(
            select(User).where(User.email == "trial@org.test")
        )).scalars().all()
        assert len(rows) == 1
        user = rows[0]
        assert user.id == trial_user_id
        # Converted to club — trial state wiped.
        assert user.subscription_plan == "club"
        assert user.trial_ends_at is None
        assert user.data_purge_at is None
        assert user.club_id is not None
        # Existing display_name is preserved (NOT overwritten by full_name).
        assert user.display_name == "Trial User"
        # Team assigned, active_team_id set.
        assert user.active_team_id == team_id
        team = await s.get(TeamProfile, team_id)
        assert team.user_id == trial_user_id


async def test_redeem_existing_user_with_wrong_password_is_cloaked_404(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Existing email + wrong password must NOT confirm the email is
    registered. Returns the same 404 the bad-code path returns."""
    from src.auth.password_service import hash_password
    from src.models.users import User

    async with api_session_factory() as s:
        s.add(User(
            email="victim@org.test",
            password_hash=hash_password("RealPassword!"),
            display_name="Victim",
            email_verified=True,
        ))
        await s.commit()

    invite = await _issue_coach_invite(
        org_admin_client, email="placeholder@org.test",
    )
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "victim@org.test",
            "full_name": "Impostor",
            "password": "WrongPassword!",
        },
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Section D — race safety
# ---------------------------------------------------------------------------


async def test_redeem_hands_off_existing_players_to_new_coach(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Phase 14.6 — players the org pre-loaded onto the team must show up
    in the Coach App roster after redeem. The org-side create stamps
    `Player.user_id = team.user_id` (NULL when no coach yet); the Coach
    App's `_team_data` query filters by `Player.user_id == coach.id`, so
    without the redeem-time handoff the coach would see an empty roster
    despite the org dashboard showing N players on the team."""
    from src.models.players import Player, PlayerMetric

    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    # PM/RM pre-loaded 3 players. user_id stays NULL until a coach lands.
    async with api_session_factory() as s:
        for jersey in (4, 7, 11):
            s.add(Player(
                organization_id=org_id,
                team_id=team_id,
                user_id=None,
                name=f"Player {jersey}",
                number=jersey,
                active=True,
            ))
        await s.commit()
        # And a metric row to verify PlayerMetric.user_id is updated too.
        player = (await s.execute(
            select(Player).where(Player.team_id == team_id, Player.number == 4)
        )).scalar_one()
        s.add(PlayerMetric(
            player_id=player.id,
            team_id=team_id,
            user_id=None,
            metrics_json={"ppg": 12.3},
        ))
        await s.commit()

    invite = await _issue_coach_invite(
        org_admin_client, email="ph@org.test", team_id=team_id,
    )
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "handoff@org.test",
            "full_name": "Handoff Coach",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text

    async with api_session_factory() as s:
        from src.models.users import User
        coach = (await s.execute(
            select(User).where(User.email == "handoff@org.test")
        )).scalar_one()

        # All 3 players are now owned by the new coach.
        roster = list((await s.execute(
            select(Player)
            .where(Player.team_id == team_id)
            .order_by(Player.number)
        )).scalars().all())
        assert len(roster) == 3
        assert all(p.user_id == coach.id for p in roster), [
            (p.number, p.user_id) for p in roster
        ]
        # organization_id is unchanged — players still belong to the org.
        assert all(p.organization_id == org_id for p in roster)

        # Metric row was also handed off (parity with create-time convention).
        metric = (await s.execute(
            select(PlayerMetric).where(PlayerMetric.team_id == team_id)
        )).scalar_one()
        assert metric.user_id == coach.id


async def test_coach_home_page_displays_program_name_for_enterprise_team(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Phase 14.6 — when a coach lands in the Coach App and their active
    team has `program_id` set (enterprise context), the home page renders
    the program name as a kicker above the team title. Private-coach teams
    (program_id IS NULL) keep the legacy display unchanged."""
    from src.models.programs import Program
    from src.models.teams import TeamProfile

    org_id = org_admin_client.org_seed["organization_id"]

    # Seed a program + a team in that program.
    async with api_session_factory() as s:
        prog = Program(organization_id=org_id, name="סל טק")
        s.add(prog)
        await s.commit()
        team = TeamProfile(
            team_name="Haifa U10",
            organization_id=org_id,
            program_id=prog.id,
            user_id=None,
        )
        s.add(team)
        await s.commit()
        team_id = team.id

    invite = await _issue_coach_invite(
        org_admin_client, email="ph@org.test", team_id=team_id,
    )
    code = invite.json()["short_code"]

    # `.test` TLD is rejected by EmailStr on /api/auth/me — use @example.com
    # so the Coach App home can serialize the user.
    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "prog-coach@example.com",
            "full_name": "Prog Coach",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["redirect"] == "/"

    # The coach is now logged in (JWT cookies set by redeem). Hit /.
    home = await api_client.get("/")
    assert home.status_code == 200, home.text
    # Program name appears in the rendered HTML, prominent above the title.
    assert "סל טק" in home.text
    # Team name still rendered (uppercase) — sanity that we didn't break it.
    assert "HAIFA U10" in home.text


async def test_coach_session_can_reach_org_login_form(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Phase 14.8 — /org/login normally redirects an authed session to its
    dashboard, but a coach session must NOT bounce: combined with the
    Phase 14.7 coach-guard on /<slug>/*, the redirect would loop them
    back into the Coach App and they could never reach the form to log
    in as a different account (PM/admin alter-ego). For coach sessions
    /org/login shows the form directly."""
    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    invite = await _issue_coach_invite(
        org_admin_client, email="ph@org.test", team_id=team_id,
    )
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "loginform@org.test",
            "full_name": "Login Form",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text

    # Coach session is now active. /org/login must render the form (200),
    # NOT 302-redirect to a tenant page that would re-bounce to /.
    form = await api_client.get("/org/login", follow_redirects=False)
    assert form.status_code == 200, form.text
    # Sanity — the form has an email field. Don't lean on specific Hebrew
    # text in case the template wording shifts.
    assert 'name="email"' in form.text or "email" in form.text.lower()


async def test_org_admin_session_still_redirects_from_org_login_to_dashboard(
    org_admin_client: AsyncClient,
):
    """Phase 14.8 sanity — the form-fallback applies ONLY to coach
    sessions. Admin/PM/RM keep their convenience redirect so navigating
    to /org/login after auth lands them back on their dashboard."""
    r = await org_admin_client.get("/org/login", follow_redirects=False)
    assert r.status_code == 302
    # Either /<slug>/dashboard (flag ON) or /org/dashboard (flag OFF).
    assert r.headers["location"].endswith("/dashboard")
    assert "login" not in r.headers["location"]


async def test_coach_is_redirected_from_org_tenant_pages_to_coach_app(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Phase 14.7 — coaches live in the Coach App. Every /<slug>/* tenant
    page must 302-redirect them to "/" (the Coach App home), so they never
    see a half-rendered admin view. Managers (admin/PM/RM/BM) keep their
    full access — verified by the broader test suite (any breakage there
    would mean we over-blocked)."""
    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    invite = await _issue_coach_invite(
        org_admin_client, email="ph@org.test", team_id=team_id,
    )
    code = invite.json()["short_code"]

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "rdr@org.test",
            "full_name": "Redirect Coach",
            "password": "Sup3rSecure!",
        },
    )
    assert r.status_code == 200, r.text

    # Every tenant page the coach might reach by typing or via a stale
    # bookmark must bounce them to "/" with 302. We disable auto-follow so
    # the test sees the redirect itself, not the Coach App home payload.
    for path in (
        api_client.slug_url("/dashboard"),
        api_client.slug_url("/teams"),
        api_client.slug_url("/players"),
        api_client.slug_url("/calendar"),
        api_client.slug_url("/users"),
        api_client.slug_url("/regions"),
        api_client.slug_url("/programs"),
    ):
        resp = await api_client.get(path, follow_redirects=False)
        assert resp.status_code == 302, f"{path} -> {resp.status_code} {resp.text[:120]}"
        assert resp.headers["location"] == "/", (path, resp.headers["location"])


async def test_redeem_does_not_overwrite_team_if_coach_assigned_after_invite(
    org_admin_client: AsyncClient, api_client: AsyncClient, api_session_factory,
):
    """Race: team had no coach at invite time, but another coach got
    assigned before this invite was redeemed. The redeem should still
    succeed (user is created, membership minted, club state set), but
    must NOT silently kick the existing coach off the team."""
    from src.auth.password_service import hash_password
    from src.models.teams import TeamProfile
    from src.models.users import User

    org_id = org_admin_client.org_seed["organization_id"]
    team_id = await _seed_team(api_session_factory, org_id=org_id)

    invite = await _issue_coach_invite(
        org_admin_client, email="placeholder@org.test", team_id=team_id,
    )
    code = invite.json()["short_code"]

    # Simulate the race: another coach grabs the team between invite + redeem.
    async with api_session_factory() as s:
        squatter = User(
            email="squatter@org.test",
            password_hash=hash_password("Sup3rSecure!"),
            display_name="Squatter",
        )
        s.add(squatter)
        await s.flush()
        team = await s.get(TeamProfile, team_id)
        team.user_id = squatter.id
        await s.commit()
        squatter_id = squatter.id

    r = await api_client.post(
        "/org/api/invites/redeem",
        json={
            "code": code,
            "email": "loser@org.test",
            "full_name": "Loser",
            "password": "Sup3rSecure!",
        },
    )
    # Redeem itself succeeds — the user gets a club account + membership.
    assert r.status_code == 200, r.text

    async with api_session_factory() as s:
        team = await s.get(TeamProfile, team_id)
        # Squatter is still on the team — no silent replacement.
        assert team.user_id == squatter_id
        loser = (await s.execute(
            select(User).where(User.email == "loser@org.test")
        )).scalar_one()
        # No active team — the inviter must re-assign manually.
        assert loser.active_team_id is None
        # Still a club member, no trial.
        assert loser.subscription_plan == "club"
