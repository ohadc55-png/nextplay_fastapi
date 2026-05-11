"""PlayerContacts repository — org-scoped reads + upsert.

Sensitive fields (parent_phone, national_id, medical_notes, address) are
stored encrypted via the `EncryptedText` TypeDecorator on the model. This
repository talks to the ORM in plaintext; encryption/decryption happens
transparently at the DB boundary.

Caller responsibility: every contact READ must call `log_org_action(...)`
with `action="player.contact.read"`. The repository doesn't audit — that's
intentional, so the route layer has full control over when an audit event
gets emitted (e.g., bulk imports may suppress per-row events).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.player_contacts import PlayerContact
from src.repositories.org_scoped_repository import OrgScopedRepository


class PlayerContactsRepository(OrgScopedRepository[PlayerContact]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PlayerContact)

    async def get_for_player(
        self, player_id: int, *, organization_id: int | None
    ) -> PlayerContact | None:
        """Lookup the (unique) contact row for a player, scoped to its org.
        Returns None if cross-org. Decryption happens transparently when
        the ORM materializes the row."""
        if organization_id is None:
            return None
        stmt = select(PlayerContact).where(
            PlayerContact.player_id == player_id,
            PlayerContact.organization_id == organization_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_for_player(
        self,
        *,
        player_id: int,
        organization_id: int,
        parent_name: str | None = None,
        parent_email: str | None = None,
        parent_phone: str | None = None,
        national_id: str | None = None,
        medical_notes: str | None = None,
        address: str | None = None,
    ) -> PlayerContact:
        """Insert if missing; otherwise update plain + encrypted columns.
        Returns the persisted row. Idempotent within a transaction."""
        existing = await self.get_for_player(
            player_id, organization_id=organization_id
        )
        if existing is None:
            new = PlayerContact(
                player_id=player_id,
                organization_id=organization_id,
                parent_name=parent_name,
                parent_email=parent_email,
                parent_phone_enc=parent_phone,
                national_id_enc=national_id,
                medical_notes_enc=medical_notes,
                address_enc=address,
            )
            self.session.add(new)
            await self.session.flush()
            return new

        if parent_name is not None:
            existing.parent_name = parent_name
        if parent_email is not None:
            existing.parent_email = parent_email
        if parent_phone is not None:
            existing.parent_phone_enc = parent_phone
        if national_id is not None:
            existing.national_id_enc = national_id
        if medical_notes is not None:
            existing.medical_notes_enc = medical_notes
        if address is not None:
            existing.address_enc = address
        await self.session.flush()
        return existing


__all__ = ["PlayerContactsRepository"]
