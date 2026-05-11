"""Create a single DocumentDelivery for manual end-to-end testing of the
public signing flow (Phase 2.3, before Sub-Phase 2.4's campaign UI lands).

Usage:
    python scripts/create_test_delivery.py --template-id 1 --player-id 42
    python scripts/create_test_delivery.py --template-id 1 --player-id 42 --slug shaar-shivyon

Prints the public signing URL (/sign/{token}). Open it in incognito to
play the parent's role; the OTP will be logged to the uvicorn console.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.core.database import AsyncSessionLocal  # noqa: E402
from src.models.document_campaigns import DocumentCampaign  # noqa: E402
from src.models.document_deliveries import DocumentDelivery  # noqa: E402
from src.models.document_templates import DocumentTemplate  # noqa: E402
from src.models.organizations import Organization  # noqa: E402
from src.models.player_contacts import PlayerContact  # noqa: E402
from src.models.players import Player  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("create-delivery")


async def main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as s:
        # Resolve the template.
        tpl = (await s.execute(
            select(DocumentTemplate).where(DocumentTemplate.id == args.template_id)
        )).scalar_one_or_none()
        if tpl is None:
            log.error(f"Template id={args.template_id} not found.")
            return 1

        # Resolve the player + their contact (snapshot the decrypted phone).
        player = (await s.execute(
            select(Player).where(
                Player.id == args.player_id,
                Player.organization_id == tpl.organization_id,
            )
        )).scalar_one_or_none()
        if player is None:
            log.error(
                f"Player id={args.player_id} not found in org={tpl.organization_id} "
                f"(template's org)."
            )
            return 1

        contact = (await s.execute(
            select(PlayerContact).where(PlayerContact.player_id == player.id)
        )).scalar_one_or_none()
        if contact is None:
            log.error(
                f"Player {player.id} has no PlayerContact — add one via "
                f"/org/players/{player.id} first."
            )
            return 1

        # Find-or-create a stub campaign (one per template per run is fine
        # for testing; in production 2.4 will pool deliveries per campaign).
        campaign = (await s.execute(
            select(DocumentCampaign).where(
                DocumentCampaign.template_id == tpl.id,
                DocumentCampaign.organization_id == tpl.organization_id,
                DocumentCampaign.title == "Manual test send",
            )
        )).scalar_one_or_none()
        if campaign is None:
            now = datetime.now(UTC).replace(tzinfo=None)
            campaign = DocumentCampaign(
                organization_id=tpl.organization_id,
                template_id=tpl.id,
                title="Manual test send",
                recipient_filter={"type": "specific_players", "player_ids": [player.id]},
                delivery_channels=["sms"],
                expires_at=now + timedelta(days=tpl.default_expiry_days),
            )
            s.add(campaign)
            await s.flush()

        # Create the delivery row.
        token = uuid.uuid4().hex
        now = datetime.now(UTC).replace(tzinfo=None)
        delivery = DocumentDelivery(
            campaign_id=campaign.id,
            organization_id=tpl.organization_id,
            player_id=player.id,
            player_contact_id=contact.id,
            recipient_name=contact.parent_name or player.name,
            recipient_email=contact.parent_email,
            # Encrypted column auto-decrypts via EncryptedText.
            recipient_phone=contact.parent_phone_enc,
            unique_token=token,
            expires_at=now + timedelta(days=tpl.default_expiry_days),
        )
        s.add(delivery)
        campaign.total_recipients = (campaign.total_recipients or 0) + 1
        await s.commit()

    base = args.base_url.rstrip("/")
    url = f"{base}/sign/{token}"
    print()
    print("=" * 60)
    print("Manual signing test ready.")
    print("=" * 60)
    print(f"  template:        {tpl.name} (id={tpl.id})")
    print(f"  player:          {player.name} (id={player.id})")
    print(f"  recipient phone: {contact.parent_phone_enc}")
    print(f"  recipient email: {contact.parent_email or '(none)'}")
    print()
    print("Open in an incognito window:")
    print(f"  {url}")
    print()
    print("OTP code: watch the uvicorn console for '[MOCK SMS]' lines.")
    print("=" * 60)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--template-id", type=int, required=True)
    ap.add_argument("--player-id", type=int, required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:5050")
    return ap


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_build_parser().parse_args())))
