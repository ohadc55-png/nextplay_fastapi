"""Players + PlayerContacts under /org/api/players/* (Phase 1.6).

Critical invariant: private-coach players (organization_id IS NULL) NEVER
appear in org-context responses; the Coach App's /api/players keeps working
for them.

Sensitive contact data (parent_phone, national_id, medical_notes, address)
is stored encrypted at rest. Every contact READ (GET /contact) AND every
contact WRITE (PUT /contact) emits an OrgAuditLog row — `player.contact.read`
or `player.contact.write` — so we have a regulatory trail.

Auth & roles (org session via `get_current_org_membership`):

- GET    /org/api/players                  any active member (scoped)
- POST   /org/api/players                  org_admin / region_manager /
                                           branch_manager / coach (must own team)
- GET    /org/api/players/{id}             scoped read (NO contact decryption)
- PATCH  /org/api/players/{id}             scoped write
- DELETE /org/api/players/{id}             scoped (soft via active=False)
- GET    /org/api/players/{id}/contact     scoped + audit `player.contact.read`
- PUT    /org/api/players/{id}/contact     scoped + audit `player.contact.write`

Cross-org / out-of-scope → 404 (never 403).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.org_auth import get_current_org_membership, require_role
from src.core.database import get_db
from src.core.exceptions import NotFoundError
from src.models.branches import Branch
from src.models.player_contacts import PlayerContact
from src.models.players import Player
from src.models.teams import TeamProfile
from src.models.user_organizations import UserOrganization
from src.repositories.player_contacts_repo import PlayerContactsRepository
from src.repositories.players_repo import PlayersRepository
from src.schemas.org_players import (
    PlayerContactOut,
    PlayerContactUpsert,
    PlayerCreate,
    PlayerOut,
    PlayerUpdate,
)
from src.services.document_deliveries_view import (
    count_pending_for_players,
    list_for_player,
)
from src.services.org_audit_service import log_org_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/org/api/players", tags=["org-players"])


# ---------------------------------------------------------------------------
# Helpers — scope guards
# ---------------------------------------------------------------------------


async def _ensure_team_in_scope(
    db: AsyncSession, team_id: int, membership: UserOrganization,
) -> TeamProfile:
    """Fetch team scoped to org + role. 404 covers cross-org / out-of-scope.

    Visibility honors BOTH the new direct `team.region_id` FK and the
    legacy `team.branch_id -> branch.region_id` path.
    """
    team = (
        await db.execute(
            select(TeamProfile).where(
                TeamProfile.id == team_id,
                TeamProfile.organization_id == membership.organization_id,
            )
        )
    ).scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found")

    role = membership.role
    if role == "program_manager":
        pm_program = getattr(membership, "program_id", None)
        if pm_program is None:
            raise NotFoundError("Team not found")
        # Phase 12 — program is on the team directly. Legacy region walk gone.
        if team.program_id != pm_program:
            raise NotFoundError("Team not found")
    elif role == "branch_manager":
        if team.branch_id != membership.branch_id:
            raise NotFoundError("Team not found")
    elif role == "region_manager":
        # Phase 12 — pinned-program RM additionally checks team.program_id.
        rm_program = getattr(membership, "program_id", None)
        if rm_program is not None and team.program_id != rm_program:
            raise NotFoundError("Team not found")
        eff_region_id = team.region_id
        if eff_region_id is None and team.branch_id is not None:
            branch = await db.get(Branch, team.branch_id)
            eff_region_id = branch.region_id if branch else None
        if eff_region_id != membership.region_id:
            raise NotFoundError("Team not found")
    elif role == "coach":
        if team.user_id != membership.user_id:
            raise NotFoundError("Team not found")
    return team


async def _ensure_player_visible(
    db: AsyncSession, player_id: int, membership: UserOrganization,
) -> Player:
    """Player + team-scope guard. Reuses `_ensure_team_in_scope`."""
    player = await PlayersRepository(db).get_for_org(
        player_id, membership.organization_id
    )
    if player is None:
        raise NotFoundError("Player not found")
    if player.team_id is not None:
        await _ensure_team_in_scope(db, player.team_id, membership)
    return player


# ---------------------------------------------------------------------------
# GET /org/api/players — list
# ---------------------------------------------------------------------------


@router.get("", response_model=dict)
async def list_players(
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
    team_id: int | None = Query(default=None),
    branch_id: int | None = Query(default=None),
    region_id: int | None = Query(default=None),
    include_inactive: bool = Query(default=False),
) -> dict:
    repo = PlayersRepository(db)
    role = membership.role

    if role == "coach":
        rows = await repo.list_for_org(
            membership.organization_id,
            coach_user_id=membership.user_id,
            team_id=team_id,
            include_inactive=include_inactive,
        )
    elif role == "branch_manager":
        rows = await repo.list_for_org(
            membership.organization_id,
            branch_id=membership.branch_id,
            team_id=team_id,
            include_inactive=include_inactive,
        )
    elif role == "region_manager":
        # Phase 12 — RMs pinned to a program (via UserOrganization.program_id)
        # narrow to that (region × program) slice; otherwise span all programs
        # in their region.
        rm_program = getattr(membership, "program_id", None)
        rows = await repo.list_for_org(
            membership.organization_id,
            region_id=membership.region_id,
            program_id=rm_program,
            team_id=team_id,
            branch_id=branch_id,
            include_inactive=include_inactive,
        )
    elif role == "program_manager":
        # Phase 12 — PM is auto-clamped to their program. Region/branch
        # query filters still apply within that subset; mismatched team_id
        # falls through to the repo which 404s through the empty-list cloak.
        pm_program = getattr(membership, "program_id", None)
        if pm_program is None:
            return {"players": []}
        rows = await repo.list_for_org(
            membership.organization_id,
            program_id=pm_program,
            team_id=team_id,
            branch_id=branch_id,
            region_id=region_id,
            include_inactive=include_inactive,
        )
    else:  # org_admin / viewer
        rows = await repo.list_for_org(
            membership.organization_id,
            team_id=team_id,
            branch_id=branch_id,
            region_id=region_id,
            include_inactive=include_inactive,
        )
    # Phase 2.5 — augment each row with a pending-approvals count so the
    # players table can show a badge without N+1 queries.
    pending = await count_pending_for_players(
        db,
        player_ids=[p.id for p in rows],
        organization_id=membership.organization_id,
    )
    out = []
    for p in rows:
        row = PlayerOut.model_validate(p).model_dump(mode="json")
        row["pending_approvals"] = pending.get(p.id, 0)
        out.append(row)
    return {"players": out}


# ---------------------------------------------------------------------------
# POST /org/api/players — create
# ---------------------------------------------------------------------------


@router.post("", response_model=PlayerOut, status_code=status.HTTP_201_CREATED)
async def create_player(
    body: PlayerCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager", "coach")
    ),
) -> PlayerOut:
    team = await _ensure_team_in_scope(db, body.team_id, membership)
    player = Player(
        organization_id=membership.organization_id,
        team_id=team.id,
        user_id=team.user_id,  # mirrors coach ownership convention
        name=body.name.strip(),
        number=body.number,
        position=body.position,
        height=body.height,
        weight=body.weight,
        age=body.age,
        dominant_hand=body.dominant_hand,
        notes=body.notes,
        active=True,
    )
    db.add(player)
    await db.flush()
    await db.refresh(player)

    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="player.create",
        target_type="player",
        target_id=player.id,
        request=request,
        extra={"name": player.name, "team_id": team.id},
    )
    return PlayerOut.model_validate(player)


# ---------------------------------------------------------------------------
# GET /org/api/players/{player_id} — detail (no contact)
# ---------------------------------------------------------------------------


@router.get("/{player_id}", response_model=PlayerOut)
async def get_player(
    player_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> PlayerOut:
    player = await _ensure_player_visible(db, player_id, membership)
    return PlayerOut.model_validate(player)


# ---------------------------------------------------------------------------
# PATCH /org/api/players/{player_id} — update
# ---------------------------------------------------------------------------


@router.patch("/{player_id}", response_model=PlayerOut)
async def update_player(
    player_id: int,
    body: PlayerUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager", "coach")
    ),
) -> PlayerOut:
    player = await _ensure_player_visible(db, player_id, membership)

    changes: dict = {}

    def _set(attr: str, new):
        old = getattr(player, attr)
        if new != old:
            changes[attr] = {"from": old, "to": new}
            setattr(player, attr, new)

    if body.name is not None:
        _set("name", body.name.strip())
    if body.number is not None:
        _set("number", body.number)
    if body.position is not None:
        _set("position", body.position)
    if body.height is not None:
        _set("height", body.height)
    if body.weight is not None:
        _set("weight", body.weight)
    if body.age is not None:
        _set("age", body.age)
    if body.dominant_hand is not None:
        _set("dominant_hand", body.dominant_hand)
    if body.strengths is not None:
        _set("strengths", body.strengths)
    if body.weaknesses is not None:
        _set("weaknesses", body.weaknesses)
    if body.notes is not None:
        _set("notes", body.notes)
    if body.active is not None:
        _set("active", body.active)

    if changes:
        await db.flush()
        await db.refresh(player)
        await log_org_action(
            db,
            organization_id=membership.organization_id,
            actor_user_id=request.state.user.id,
            actor_email=request.state.user.email,
            action="player.update",
            target_type="player",
            target_id=player.id,
            request=request,
            extra=changes,
        )
    return PlayerOut.model_validate(player)


# ---------------------------------------------------------------------------
# DELETE /org/api/players/{player_id} — soft delete (active=False)
# ---------------------------------------------------------------------------


@router.delete("/{player_id}", response_model=dict)
async def delete_player(
    player_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager", "coach")
    ),
) -> dict:
    player = await _ensure_player_visible(db, player_id, membership)
    if not player.active:
        return {"ok": True}
    player.active = False
    await db.flush()
    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="player.delete",
        target_type="player",
        target_id=player.id,
        request=request,
        extra={"name": player.name},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Contact endpoints — audited on every read AND write.
# ---------------------------------------------------------------------------


def _contact_to_out(contact: PlayerContact | None, player_id: int) -> PlayerContactOut:
    """Map model column names (*_enc) to API field names (no suffix). The
    TypeDecorator already decrypted the values by the time we get here."""
    if contact is None:
        return PlayerContactOut(player_id=player_id)
    return PlayerContactOut(
        player_id=contact.player_id,
        organization_id=contact.organization_id,
        parent_name=contact.parent_name,
        parent_email=contact.parent_email,
        parent_phone=contact.parent_phone_enc,
        national_id=contact.national_id_enc,
        medical_notes=contact.medical_notes_enc,
        address=contact.address_enc,
        updated_at=contact.updated_at,
    )


@router.get("/{player_id}/contact", response_model=PlayerContactOut)
async def get_player_contact(
    player_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> PlayerContactOut:
    """Return the decrypted parent/medical contact for a player. EVERY call
    here logs `player.contact.read` — that's the audit trail required for
    sensitive PII access."""
    player = await _ensure_player_visible(db, player_id, membership)
    contact = await PlayerContactsRepository(db).get_for_player(
        player_id, organization_id=membership.organization_id
    )

    # Audit the read — even when the row is empty, the fact that we tried
    # is itself an event worth recording.
    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="player.contact.read",
        target_type="player",
        target_id=player.id,
        request=request,
        extra={"present": contact is not None},
    )
    return _contact_to_out(contact, player_id)


@router.put("/{player_id}/contact", response_model=PlayerContactOut)
async def upsert_player_contact(
    player_id: int,
    body: PlayerContactUpsert,
    request: Request,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(
        require_role("org_admin", "program_manager", "region_manager", "branch_manager", "coach")
    ),
) -> PlayerContactOut:
    """Insert if no contact row yet, otherwise update fields that are not
    None. Pass an empty string to explicitly clear a column. Logs
    `player.contact.write` with a list of which columns changed.
    """
    player = await _ensure_player_visible(db, player_id, membership)

    repo = PlayerContactsRepository(db)
    contact = await repo.upsert_for_player(
        player_id=player.id,
        organization_id=membership.organization_id,
        parent_name=body.parent_name,
        parent_email=body.parent_email,
        parent_phone=body.parent_phone,
        national_id=body.national_id,
        medical_notes=body.medical_notes,
        address=body.address,
    )

    changed_fields = [
        f for f in (
            "parent_name", "parent_email", "parent_phone",
            "national_id", "medical_notes", "address",
        )
        if getattr(body, f) is not None
    ]
    await log_org_action(
        db,
        organization_id=membership.organization_id,
        actor_user_id=request.state.user.id,
        actor_email=request.state.user.email,
        action="player.contact.write",
        target_type="player",
        target_id=player.id,
        request=request,
        extra={"fields_changed": changed_fields},
    )
    return _contact_to_out(contact, player_id)


# ---------------------------------------------------------------------------
# GET /{player_id}/deliveries — Phase 2.5 visibility modal
# ---------------------------------------------------------------------------


@router.get("/{player_id}/deliveries", response_model=dict)
async def list_player_deliveries(
    player_id: int,
    db: AsyncSession = Depends(get_db),
    membership: UserOrganization = Depends(get_current_org_membership),
) -> dict:
    """All document deliveries for this player (signed + pending + failed).
    Cross-org / out-of-scope → 404."""
    repo = PlayersRepository(db)
    player = await repo.get_for_org(player_id, membership.organization_id)
    if player is None:
        raise NotFoundError("Player not found")

    rows = await list_for_player(
        db,
        player_id=player_id,
        organization_id=membership.organization_id,
    )
    return {
        "player": {"id": player.id, "name": player.name},
        "deliveries": [
            {
                "id": d.id,
                "campaign_id": d.campaign_id,
                "recipient_name": d.recipient_name,
                "recipient_email": d.recipient_email,
                "delivery_status": d.delivery_status,
                "document_status": d.document_status,
                "channel_used": d.channel_used,
                "sent_at": d.sent_at.isoformat() if d.sent_at else None,
                "signed_at": d.signed_at.isoformat() if d.signed_at else None,
                "expires_at": d.expires_at.isoformat() if d.expires_at else None,
                "final_pdf_url": d.final_pdf_url,
            }
            for d in rows
        ],
    }


__all__ = ["router"]
