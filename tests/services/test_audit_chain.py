"""Phase 2 closeout — hash-chain audit unit tests.

Pure-function tests for the canonical-payload and self-hash helpers.
The end-to-end chain (insert → verify on a real signing flow) is
covered by `tests/api/test_public_sign_flow.py::test_submit_writes_hash_chain`.
"""

from __future__ import annotations

from src.services.audit_chain import (
    GENESIS_HASH,
    canonical_payload,
    compute_self_hash,
)

# ---------------------------------------------------------------------------
# canonical_payload
# ---------------------------------------------------------------------------


def test_canonical_payload_is_deterministic():
    args = dict(
        delivery_id=42,
        organization_id=1,
        campaign_id=7,
        signed_at_iso="2026-01-02T03:04:05",
        payload_hash="abc",
        signature_method="DRAWN",
        final_pdf_url="org_1/signed/42.pdf",
        ip_address="1.2.3.4",
        user_agent="UA",
    )
    p1 = canonical_payload(**args)
    p2 = canonical_payload(**args)
    assert p1 == p2
    # Sorted keys → output starts with the alphabetically-first key.
    assert p1.startswith('{"campaign_id":7')


def test_canonical_payload_kwarg_order_invariance():
    """Re-ordering kwargs in the caller must not change the output."""
    a = canonical_payload(
        delivery_id=1, organization_id=2, campaign_id=3,
        signed_at_iso="2026-01-01T00:00:00", payload_hash="x",
        signature_method="DRAWN", final_pdf_url=None,
        ip_address=None, user_agent=None,
    )
    b = canonical_payload(
        user_agent=None, ip_address=None, final_pdf_url=None,
        signature_method="DRAWN", payload_hash="x",
        signed_at_iso="2026-01-01T00:00:00", campaign_id=3,
        organization_id=2, delivery_id=1,
    )
    assert a == b


def test_canonical_payload_truncates_user_agent():
    """Stops absurdly long UA strings from bloating the chain payload."""
    long_ua = "x" * 1000
    p = canonical_payload(
        delivery_id=1, organization_id=1, campaign_id=None,
        signed_at_iso="", payload_hash="",
        signature_method=None, final_pdf_url=None,
        ip_address=None, user_agent=long_ua,
    )
    # 500 chars of x + 6 JSON quote/comma bytes around it. Sanity-check.
    assert '"user_agent":"' + "x" * 500 + '"' in p


def test_canonical_payload_distinguishes_field_changes():
    """Changing any single field changes the canonical output."""
    base = dict(
        delivery_id=1, organization_id=1, campaign_id=None,
        signed_at_iso="t", payload_hash="p",
        signature_method=None, final_pdf_url=None,
        ip_address=None, user_agent=None,
    )
    baseline = canonical_payload(**base)
    for field, new_value in [
        ("delivery_id", 2),
        ("organization_id", 99),
        ("payload_hash", "different"),
        ("signature_method", "TYPED"),
        ("final_pdf_url", "key/x.pdf"),
        ("ip_address", "9.9.9.9"),
    ]:
        mutated = dict(base)
        mutated[field] = new_value
        assert canonical_payload(**mutated) != baseline, f"{field} mutation undetected"


# ---------------------------------------------------------------------------
# compute_self_hash
# ---------------------------------------------------------------------------


def test_compute_self_hash_changes_with_prev():
    payload = "fixed"
    h1 = compute_self_hash(GENESIS_HASH, payload)
    h2 = compute_self_hash("1" * 64, payload)
    assert h1 != h2
    assert len(h1) == 64


def test_compute_self_hash_changes_with_payload():
    h1 = compute_self_hash(GENESIS_HASH, "one")
    h2 = compute_self_hash(GENESIS_HASH, "two")
    assert h1 != h2


def test_genesis_hash_shape():
    assert GENESIS_HASH == "0" * 64
    assert len(GENESIS_HASH) == 64
