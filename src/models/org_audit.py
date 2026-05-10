"""OrgAuditLog model — append-only audit trail for organization actions.

Distinct from `audit_logs` (auth events; src/models/auth.py:AuditLog).
Immutable: only `created_at`, no `updated_at`. Postgres RLS additionally
revokes UPDATE/DELETE from the app role.

`actor_email` is a snapshot so audit rows survive `users` deletes.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base, JSONText

if TYPE_CHECKING:
    from src.models.organizations import Organization
    from src.models.users import User


class OrgAuditLog(Base):
    __tablename__ = "org_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="")
    attributes_json: Mapped[dict | None] = mapped_column(JSONText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    organization: Mapped[Organization] = relationship("Organization", lazy="raise")
    actor: Mapped[User | None] = relationship("User", lazy="raise", foreign_keys=[actor_user_id])

    __table_args__ = (
        Index("idx_org_audit_org_created", "organization_id", "created_at"),
        Index("idx_org_audit_actor", "actor_user_id"),
        Index("idx_org_audit_action", "action"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<OrgAuditLog id={self.id} org_id={self.organization_id} "
            f"action={self.action!r}>"
        )


__all__ = ["OrgAuditLog"]
