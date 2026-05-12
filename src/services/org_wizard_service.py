"""Org Creation Wizard service (Phase 1.8).

`commit_wizard()` runs the full 4-step transaction:
1. Validate slug + subdomain availability (re-check, even after preflight).
2. Create the Organization with every wizard field populated.
3. Find or create the CEO user (no password yet — they set it via invite).
4. Create the first UserOrganization (role=org_admin).
5. If send_invite_immediately: issue an org_invite token (7-day) + create the
   OrgInvite row + schedule the email via BackgroundTasks.
6. Audit log: `org.create.wizard` (System Admin actor).

Atomicity: the email *body dispatch* happens in BackgroundTasks (deferred);
everything else lives in the same DB transaction. The route layer drives
`commit` inside a `get_db`-yielded session, so failure rolls back as a unit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError
from src.models.org_invites import OrgInvite
from src.models.organizations import Organization
from src.models.user_organizations import UserOrganization
from src.models.users import User
from src.repositories.organizations_repo import OrganizationsRepository
from src.repositories.users_repo import UsersRepository
from src.schemas.org_wizard import WizardCommit
from src.services.email_service import send_org_invite_email
from src.services.org_audit_service import log_org_action
from src.services.org_validators import validate_slug, validate_subdomain

logger = logging.getLogger(__name__)


@dataclass
class WizardCommitOutcome:
    org_id: int
    slug: str
    ceo_user_id: int
    ceo_invite_sent: bool


async def check_slug_available(db: AsyncSession, slug: str) -> bool:
    """True if no organization owns this slug (including soft-deleted ones)."""
    return await OrganizationsRepository(db).get_by_slug(slug) is None


async def check_subdomain_available(db: AsyncSession, subdomain: str) -> bool:
    """True if no organization owns this subdomain. Soft-deleted orgs are
    excluded (their subdomain is "released"). The DB enforces uniqueness via
    the partial unique index on `subdomain WHERE NOT NULL`."""
    stmt = select(Organization).where(
        Organization.subdomain == subdomain,
        Organization.deleted_at.is_(None),
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    return row is None


async def commit_wizard(
    db: AsyncSession,
    *,
    data: WizardCommit,
    actor_email: str,
    request: Request,
    background: BackgroundTasks,
) -> WizardCommitOutcome:
    """Run the atomic create-org-and-invite-CEO flow."""
    s1 = data.step1
    s2 = data.step2
    s3 = data.step3
    s4 = data.step4

    # 1. Re-validate slug + subdomain — preflight is a hint, the commit is
    #    the source of truth.
    slug = validate_slug(s1.slug)
    if not await check_slug_available(db, slug):
        raise ConflictError(
            "An organization with this slug already exists.",
            code="slug_taken",
        )

    subdomain = validate_subdomain(s2.subdomain)
    if subdomain and not await check_subdomain_available(db, subdomain):
        raise ConflictError(
            "This subdomain is taken.", code="subdomain_taken",
        )

    # 2. Create the Organization row.
    trial_ends_at = None
    if s3.status == "trial" and s3.trial_days > 0:
        trial_ends_at = datetime.combine(
            s3.contract_start, datetime.min.time(), tzinfo=UTC,
        ) + timedelta(days=s3.trial_days)
        # Store naive UTC to match the rest of the schema (matches v1).
        trial_ends_at = trial_ends_at.replace(tzinfo=None)

    org = Organization(
        slug=slug,
        name=s1.name.strip(),
        legal_name=(s1.legal_name or None),
        tax_id=(s1.tax_id or None),
        address=(s1.address or None),
        logo_url=(s2.logo_url or None),
        primary_color=s2.primary_color,
        subdomain=subdomain,
        structure_type=s3.structure_type,
        monthly_fee_cents=s3.monthly_fee_cents,
        setup_fee_cents=s3.setup_fee_cents,
        trial_ends_at=trial_ends_at,
        contract_start=s3.contract_start,
        plan="enterprise",
        status="active" if s3.status == "active" else "active",  # 'trial' rows are still 'active' system-wide; trial_ends_at marks the trial
        attributes_json={
            "wizard_completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "billing_status": s3.status,
        },
    )
    db.add(org)
    await db.flush()

    # 3. Find or create the CEO user.
    users_repo = UsersRepository(db)
    invitee_email = s4.email.strip().lower()
    existing_user = await users_repo.get_by_email_active(invitee_email)
    if existing_user is None:
        # Create with no password — they'll set one via the invite link.
        # `password_hash=""` is OK because verify_password fails closed on empty.
        ceo = User(
            email=invitee_email,
            password_hash="",
            display_name=s4.full_name.strip(),
            email_verified=False,
        )
        db.add(ceo)
        await db.flush()
    else:
        ceo = existing_user

    # 4. First membership.
    membership = UserOrganization(
        user_id=ceo.id,
        organization_id=org.id,
        role=s4.role,
        status="active",
        invited_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(membership)
    await db.flush()

    # 5. Invite (optional).
    invite_sent = False
    if s4.send_invite_immediately:
        _raw_token, auth_token_id = await send_org_invite_email(
            db,
            inviter_display_name="System Admin",
            invitee_email=invitee_email,
            invitee_user_id=ceo.id,
            organization_name=org.name,
            role=s4.role,
            background=background,
        )
        invite_row = OrgInvite(
            organization_id=org.id,
            email=invitee_email,
            role=s4.role,
            auth_token_id=auth_token_id,
            invited_by=None,  # System Admin has no user row
            status="pending",
        )
        db.add(invite_row)
        await db.flush()
        invite_sent = True

    # 6. Audit log — actor_user_id=None for System Admin.
    await log_org_action(
        db,
        organization_id=org.id,
        actor_user_id=None,
        actor_email=actor_email,
        action="org.create.wizard",
        target_type="organization",
        target_id=org.id,
        request=request,
        extra={
            "slug": org.slug,
            "name": org.name,
            "subdomain": subdomain,
            "ceo_email": invitee_email,
            "ceo_invite_sent": invite_sent,
            "billing_status": s3.status,
        },
    )

    return WizardCommitOutcome(
        org_id=org.id,
        slug=org.slug,
        ceo_user_id=ceo.id,
        ceo_invite_sent=invite_sent,
    )


__all__ = [
    "WizardCommitOutcome",
    "check_slug_available",
    "check_subdomain_available",
    "commit_wizard",
]
