"""Endpoint tests for /org/api/document-templates/* — Phase 2.2.

S3 is mocked at the service boundary (`put_bytes` / `get_bytes`) — we
exercise the validation, persistence, and scoping logic without touching
AWS. PyMuPDF is still real (it's pure Python on the wheel).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.models.document_templates import DocumentTemplate
from src.models.organizations import Organization

pytestmark = pytest.mark.asyncio


# Minimum valid PDF — Adobe spec lower bound. PyMuPDF can render this.
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<<>>>>endobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000054 00000 n \n"
    b"0000000101 00000 n \n"
    b"trailer<</Root 1 0 R/Size 4>>\n"
    b"startxref\n166\n%%EOF"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _upload(
    client: AsyncClient,
    *,
    body: bytes = _MINIMAL_PDF,
    filename: str = "form.pdf",
    name: str = "הצהרת בריאות 2026",
    category: str = "HEALTH",
    requires_signature: bool = True,
    content_type: str = "application/pdf",
):
    """Drive POST /org/api/document-templates with put_bytes mocked."""
    with patch("src.services.document_template_service.s3.put_bytes") as mock:
        mock.return_value = None
        return await client.post(
            "/org/api/document-templates",
            files={"file": (filename, body, content_type)},
            data={
                "name": name,
                "category": category,
                "requires_signature": "true" if requires_signature else "false",
            },
        ), mock


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_upload_pdf_creates_template(
    org_admin_client: AsyncClient, api_session_factory,
):
    r, mock = await _upload(org_admin_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "הצהרת בריאות 2026"
    assert body["uploaded_file_type"] == "PDF"
    assert body["uploaded_file_size"] == len(_MINIMAL_PDF)
    assert body["requires_signature"] is True
    assert body["is_active"] is True
    assert body["uploaded_file_url"].startswith("org_")
    assert body["uploaded_file_url"].endswith(".pdf")

    # S3 was called with the right kwargs.
    assert mock.await_count == 1
    kwargs = mock.await_args.kwargs
    assert kwargs["content_type"] == "application/pdf"
    assert kwargs["data"] == _MINIMAL_PDF
    assert kwargs["key"] == body["uploaded_file_url"]

    # Row really lives in the DB.
    async with api_session_factory() as s:
        row = (await s.execute(select(DocumentTemplate))).scalar_one()
        assert row.organization_id == org_admin_client.org_seed["organization_id"]
        assert row.is_active is True


async def test_list_excludes_inactive_by_default(
    org_admin_client: AsyncClient, api_session_factory,
):
    # Upload two templates, soft-delete one.
    r1, _ = await _upload(org_admin_client, name="Tpl 1")
    r2, _ = await _upload(org_admin_client, name="Tpl 2")
    id_to_delete = r2.json()["id"]
    r_del = await org_admin_client.delete(
        f"/org/api/document-templates/{id_to_delete}"
    )
    assert r_del.status_code == 200

    r = await org_admin_client.get("/org/api/document-templates")
    names = [t["name"] for t in r.json()["templates"]]
    assert names == ["Tpl 1"]

    r = await org_admin_client.get(
        "/org/api/document-templates?include_inactive=true"
    )
    names = sorted(t["name"] for t in r.json()["templates"])
    assert names == ["Tpl 1", "Tpl 2"]


async def test_patch_fields_roundtrips_through_jsontext(
    org_admin_client: AsyncClient,
):
    r, _ = await _upload(org_admin_client)
    tid = r.json()["id"]

    fields = [
        {
            "id": "f1", "type": "text", "label": "שם השחקן",
            "required": True, "x": 100, "y": 200, "width": 300, "height": 30,
            "page": 1,
        }
    ]
    zones = [
        {
            "id": "s1", "label": "חתימת ההורה",
            "x": 100, "y": 700, "width": 200, "height": 50, "page": 1,
        }
    ]
    r = await org_admin_client.patch(
        f"/org/api/document-templates/{tid}/fields",
        json={"form_fields": fields, "signature_zones": zones},
    )
    assert r.status_code == 200, r.text

    r = await org_admin_client.get(f"/org/api/document-templates/{tid}")
    body = r.json()
    assert body["form_fields"][0]["id"] == "f1"
    assert body["form_fields"][0]["label"] == "שם השחקן"
    assert body["signature_zones"][0]["id"] == "s1"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_upload_rejects_unsupported_file_type(org_admin_client: AsyncClient):
    r, _ = await _upload(
        org_admin_client,
        body=b"Hello world, not a PDF.",
        filename="hello.pdf",
        content_type="application/pdf",
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body.get("code") == "unsupported_file_type"


async def test_upload_rejects_files_over_10mb(org_admin_client: AsyncClient):
    big = b"%PDF-1.4\n" + (b"a" * (10 * 1024 * 1024 + 1))
    r, _ = await _upload(org_admin_client, body=big)
    assert r.status_code == 422, r.text
    assert r.json().get("code") == "file_too_large"


async def test_patch_fields_rejects_duplicate_ids(org_admin_client: AsyncClient):
    r, _ = await _upload(org_admin_client)
    tid = r.json()["id"]
    r = await org_admin_client.patch(
        f"/org/api/document-templates/{tid}/fields",
        json={
            "form_fields": [
                {"id": "x", "type": "text", "label": "A", "required": False,
                 "x": 0, "y": 0, "width": 100, "height": 20, "page": 1},
                {"id": "x", "type": "text", "label": "B", "required": False,
                 "x": 0, "y": 30, "width": 100, "height": 20, "page": 1},
            ],
            "signature_zones": [],
        },
    )
    assert r.status_code == 422, r.text
    assert r.json().get("code") == "duplicate_field_id"


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_cross_org_get_returns_404(
    org_admin_client: AsyncClient,
    seed_org_admin,
    api_client: AsyncClient,
):
    # Upload as org A.
    r, _ = await _upload(org_admin_client)
    tid_a = r.json()["id"]

    # Log out org A, log in as a different org.
    await api_client.post("/org/logout")
    other = await seed_org_admin(
        email="b@org.test", org_slug="other-org", org_name="Other Org",
    )
    r = await api_client.post(
        "/org/login", json={"email": other["email"], "password": other["password"]}
    )
    assert r.status_code == 200, r.text

    # Org B should not see org A's template.
    r = await api_client.get(f"/org/api/document-templates/{tid_a}")
    assert r.status_code == 404
    r = await api_client.get("/org/api/document-templates")
    assert r.json()["templates"] == []


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------


async def test_delete_is_soft_and_preserves_s3_key(
    org_admin_client: AsyncClient, api_session_factory,
):
    r, _ = await _upload(org_admin_client)
    tid = r.json()["id"]
    original_key = r.json()["uploaded_file_url"]

    r = await org_admin_client.delete(f"/org/api/document-templates/{tid}")
    assert r.status_code == 200
    # Idempotent
    r = await org_admin_client.delete(f"/org/api/document-templates/{tid}")
    assert r.status_code == 200

    async with api_session_factory() as s:
        row = (
            await s.execute(
                select(DocumentTemplate).where(DocumentTemplate.id == tid)
            )
        ).scalar_one()
        assert row.is_active is False
        assert row.uploaded_file_url == original_key  # untouched
