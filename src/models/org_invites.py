"""OrgInvite model — pending invite rows that map an email to an org/role.

The actual single-use token lives in `auth_tokens` (purpose='org_invite').
This row links the token to the invite metadata (which org, role, scope).
On acceptance: row.status flips to 'accepted', and a UserOrganization row
is created.

App-layer rule: only one 'pending' row per (organization_id, email, role).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText

if TYPE_CHECKING:
    from src.models.auth import AuthToken
    from src.models.branches import Branch
    from src.models.organizations import Organization
    from src.models.programs import Program
    from src.models.regions import Region
    from src.models.users import User


class OrgInvite(Base):
    __tablename__ = "org_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    program_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("programs.id", ondelete="SET NULL"), nullable=True
    )
    region_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True
    )
    branch_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )
    # Phase 14 — coach invites can pre-assign a team. Set by PM/RM during
    # the invite UI (single team per invite). At redeem time the team's
    # `user_id` is rewritten to the new coach and `users.active_team_id`
    # is stamped so the Coach App opens straight on this team. NULL for
    # invites where role != "coach" (other roles don't own teams).
    team_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("team_profile.id", ondelete="SET NULL"), nullable=True
    )
    auth_token_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("auth_tokens.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Short human-readable redemption code (8 chars, unambiguous alphabet). The
    # invitee enters this on /org/join to self-register + auto-join the org.
    # Nullable so pre-existing rows stay valid; new invites always set it.
    # One-time-use: redemption marks the auth_token used, so neither the magic
    # link nor the code can be re-used.
    short_code: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    invited_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
    program: Mapped[Program | None] = relationship(
        "Program", lazy="raise", foreign_keys=[program_id]
    )
    region: Mapped[Region | None] = relationship("Region", lazy="raise", foreign_keys=[region_id])
    branch: Mapped[Branch | None] = relationship("Branch", lazy="raise", foreign_keys=[branch_id])
    auth_token: Mapped[AuthToken] = relationship("AuthToken", lazy="raise")
    inviter: Mapped[User | None] = relationship("User", lazy="raise", foreign_keys=[invited_by])

    __table_args__ = (
        Index("idx_org_invites_org", "organization_id"),
        Index("idx_org_invites_email", "email"),
        Index("idx_org_invites_status", "status"),
        Index("idx_org_invites_short_code", "short_code"),
        Index("idx_org_invites_team", "team_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<OrgInvite id={self.id} org_id={self.organization_id} "
            f"email={self.email!r} role={self.role!r} status={self.status!r}>"
        )


__all__ = ["OrgInvite"]
