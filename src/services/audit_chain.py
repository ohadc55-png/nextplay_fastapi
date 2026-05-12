"""Audit hash-chain — Phase 2 closeout (Part B §13 pitfall #6).

Each signed DocumentDelivery audit row links to the previous one in the
same organization through a SHA-256 chain:

    self_hash = sha256(prev_hash || canonical_payload)

`prev_hash` is the `self_hash` of the most recent SIGNED delivery in the
same organization (lexicographic-id order ensures determinism). The first
signature in an org has `prev_hash = "0" * 64`.

Tampering with any past row breaks the chain — recomputing the next
self_hash will not match what's stored, so `verify_chain()` catches it.

Why a per-org chain and not per-template / per-campaign?
- Per-org gives a single audit timeline an auditor can replay.
- Cross-org isolation is preserved (each tenant has their own chain).
- Branch + region rebalancing inside an org doesn't break the chain.

The chain lives inside the existing `document_deliveries.audit_data`
JSON column — no schema change. We just add two keys: `prev_hash` and
`self_hash`. Older rows (single-hash format) remain readable; the
verifier treats them as "pre-chain" and starts the chain from the first
row that has both keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document_deliveries import DocumentDelivery

logger = logging.getLogger(__name__)


GENESIS_HASH = "0" * 64


def canonical_payload(
    *,
    delivery_id: int,
    organization_id: int,
    campaign_id: int | None,
    signed_at_iso: str,
    payload_hash: str,
    signature_method: str | None,
    final_pdf_url: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> str:
    """Build a stable, JSON-serializable representation of an audit row.

    The shape is fixed (sorted keys, separators without spaces) so that
    two callers computing the same payload always produce the same string
    — the chain depends on byte-equality.

    Uses `campaign_id` (always loaded on the delivery row) instead of
    `template_id` so the verifier doesn't need to lazy-load the campaign
    relationship. Auditors can still trace `campaign_id → template_id`
    via the campaigns table.
    """
    payload = {
        "delivery_id": delivery_id,
        "organization_id": organization_id,
        "campaign_id": campaign_id,
        "signed_at": signed_at_iso,
        "payload_hash": payload_hash,
        "signature_method": signature_method,
        "final_pdf_url": final_pdf_url,
        "ip_address": ip_address,
        "user_agent": (user_agent or "")[:500],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_self_hash(prev_hash: str, payload: str) -> str:
    """Chain step: sha256(prev_hash || canonical_payload)."""
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


async def get_last_org_hash(
    db: AsyncSession, *, organization_id: int, exclude_delivery_id: int | None = None,
) -> str:
    """Return the `self_hash` of the most recent SIGNED delivery for this
    org (excluding the one we're about to write, if known). When there's
    no prior signed delivery, returns the genesis hash."""
    stmt = (
        select(DocumentDelivery.audit_data, DocumentDelivery.id)
        .where(DocumentDelivery.organization_id == organization_id)
        .where(DocumentDelivery.document_status == "SIGNED")
        .order_by(DocumentDelivery.id.desc())
        .limit(50)  # over-fetch a bit so we can skip exclude + pre-chain rows
    )
    rows = (await db.execute(stmt)).all()
    for audit_data, did in rows:
        if exclude_delivery_id is not None and did == exclude_delivery_id:
            continue
        if not audit_data:
            continue
        # Skip pre-chain rows (only payload_hash, no self_hash).
        sh = audit_data.get("self_hash")
        if sh:
            return sh
    return GENESIS_HASH


async def build_signed_audit(
    db: AsyncSession,
    *,
    delivery: DocumentDelivery,
    payload_hash: str,
    signed_at_iso: str,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    """Build the `audit_data` dict for a signing event including the
    hash-chain links. Caller assigns the returned dict to
    `delivery.audit_data` and flushes.
    """
    prev_hash = await get_last_org_hash(
        db, organization_id=delivery.organization_id,
        exclude_delivery_id=delivery.id,
    )

    payload = canonical_payload(
        delivery_id=delivery.id,
        organization_id=delivery.organization_id,
        campaign_id=delivery.campaign_id,
        signed_at_iso=signed_at_iso,
        payload_hash=payload_hash,
        signature_method=delivery.signature_method,
        final_pdf_url=delivery.final_pdf_url,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    self_hash = compute_self_hash(prev_hash, payload)

    return {
        "ip_address": ip_address,
        "user_agent": (user_agent or "")[:500],
        "payload_hash": payload_hash,
        "prev_hash": prev_hash,
        "self_hash": self_hash,
    }


async def verify_org_chain(
    db: AsyncSession, *, organization_id: int,
) -> dict[str, Any]:
    """Walk every SIGNED delivery in `organization_id` in id-asc order and
    recompute the chain. Returns a report dict — used by tools/verify_audit_chain.py.

    Pre-chain rows (no `self_hash` in audit_data) are skipped from chain
    verification but counted in `pre_chain_rows`. The chain resumes from
    the first row that has both `prev_hash` and `self_hash`.
    """
    stmt = (
        select(DocumentDelivery)
        .where(DocumentDelivery.organization_id == organization_id)
        .where(DocumentDelivery.document_status == "SIGNED")
        .order_by(DocumentDelivery.id.asc())
    )
    rows = list((await db.execute(stmt)).scalars().all())

    report = {
        "organization_id": organization_id,
        "total_signed": len(rows),
        "pre_chain_rows": 0,
        "chain_rows": 0,
        "broken_at": [],   # list of delivery_ids whose self_hash didn't match
        "missing_prev": [],  # rows whose prev_hash didn't match their predecessor
    }

    expected_prev = GENESIS_HASH
    chain_started = False
    for d in rows:
        audit = d.audit_data or {}
        stored_prev = audit.get("prev_hash")
        stored_self = audit.get("self_hash")
        if stored_prev is None or stored_self is None:
            report["pre_chain_rows"] += 1
            continue

        report["chain_rows"] += 1
        if not chain_started:
            # First chain row — accept whatever prev_hash it has as the
            # chain's starting point (pre-chain rows may sit before it).
            expected_prev = stored_prev
            chain_started = True
        elif stored_prev != expected_prev:
            report["missing_prev"].append({
                "delivery_id": d.id,
                "stored_prev": stored_prev,
                "expected_prev": expected_prev,
            })

        # Recompute self_hash from canonical payload + stored prev.
        signed_at_iso = d.signed_at.isoformat() if d.signed_at else ""
        payload = canonical_payload(
            delivery_id=d.id,
            organization_id=d.organization_id,
            campaign_id=d.campaign_id,
            signed_at_iso=signed_at_iso,
            payload_hash=audit.get("payload_hash") or "",
            signature_method=d.signature_method,
            final_pdf_url=d.final_pdf_url,
            ip_address=audit.get("ip_address"),
            user_agent=audit.get("user_agent"),
        )
        recomputed = compute_self_hash(stored_prev, payload)
        if recomputed != stored_self:
            report["broken_at"].append({
                "delivery_id": d.id,
                "stored_self": stored_self,
                "recomputed_self": recomputed,
            })
            # Carry forward the STORED hash so downstream rows that link
            # to it can still be evaluated consistently.
        expected_prev = stored_self

    report["valid"] = (not report["broken_at"]) and (not report["missing_prev"])
    return report


__all__ = [
    "GENESIS_HASH",
    "build_signed_audit",
    "canonical_payload",
    "compute_self_hash",
    "get_last_org_hash",
    "verify_org_chain",
]
