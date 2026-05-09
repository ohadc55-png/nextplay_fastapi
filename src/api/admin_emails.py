"""Admin email management — log viewer, mailing lists, composer stubs.

Async port of `backend/admin/email_routes.py`. Three sub-areas:
  - Email log: read-only audit trail of every outbound email (24h/7d/30d
    counts, filterable list).
  - Mailing lists: admin-defined recipient segments. CRUD + membership.
  - Composer: preview / test-send / broadcast. The rendering pipeline
    (Jinja2 email templates + LLM body rewrite + Resend delivery) lands
    in Phase 7; for now the endpoints accept the request shape and
    return a stub response so the admin UI flow doesn't break.

Routes (mirrors v1):
  GET    /admin/api/emails                       — log + filters + stats
  GET    /admin/api/emails/templates             — distinct templates (for filter dropdown)
  POST   /admin/api/emails/preview               — preview composed body (stub)
  POST   /admin/api/emails/test-send             — send to ADMIN_EMAIL only (stub)
  POST   /admin/api/emails/send                  — broadcast (stub)
  GET    /admin/api/emails/users                 — user search for "specific users" mode
  GET    /admin/api/emails/lists                 — mailing lists (with member counts)
  POST   /admin/api/emails/lists                 — create
  DELETE /admin/api/emails/lists/{list_id}       — delete
  GET    /admin/api/emails/lists/{list_id}/members
  POST   /admin/api/emails/lists/{list_id}/members
  DELETE /admin/api/emails/lists/{list_id}/members/{user_id}
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps.auth import get_current_admin
from src.core.config import settings
from src.core.database import get_db
from src.models.email import EmailLog, MailingList, MailingListMember
from src.models.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api/emails", tags=["admin-emails"])


# ---------------------------------------------------------------------------
# Email log + stats
# ---------------------------------------------------------------------------

@router.get("")
async def email_log(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    template: str | None = None,
    status: str | None = None,
    email: str | None = None,
) -> dict:
    """Read the audit log + headline stats. Filters: template, status,
    to_email substring. Returns up to 200 most-recent rows."""
    today = date.today()
    yday = (today - timedelta(days=1)).isoformat()
    week = (today - timedelta(days=7)).isoformat()
    month = (today - timedelta(days=30)).isoformat()

    async def count_since(cutoff: str, **filters: str) -> int:
        stmt = select(func.count()).select_from(EmailLog).where(
            EmailLog.sent_at >= cutoff
        )
        for k, v in filters.items():
            stmt = stmt.where(getattr(EmailLog, k) == v)
        return int((await db.execute(stmt)).scalar() or 0)

    total_24h = await count_since(yday)
    total_7d = await count_since(week)
    total_30d = await count_since(month)
    failed_30d = int((await db.execute(
        select(func.count()).select_from(EmailLog)
        .where(EmailLog.status == "failed").where(EmailLog.created_at >= month)
    )).scalar() or 0)

    by_template = [
        {"template": r["template"], "cnt": int(r["cnt"])}
        for r in (await db.execute(
            text(
                "SELECT template, COUNT(*) AS cnt FROM email_log "
                "WHERE created_at >= :c GROUP BY template ORDER BY cnt DESC"
            ),
            {"c": month},
        )).mappings().all()
    ]

    stmt = select(EmailLog)
    if template:
        stmt = stmt.where(EmailLog.template == template)
    if status:
        stmt = stmt.where(EmailLog.status == status)
    if email:
        stmt = stmt.where(func.lower(EmailLog.to_email).like(f"%{email.lower()}%"))
    stmt = stmt.order_by(EmailLog.id.desc()).limit(200)

    rows = list((await db.execute(stmt)).scalars().all())
    serialized = [
        {
            "id": r.id, "user_id": r.user_id, "to_email": r.to_email,
            "template": r.template, "subject": r.subject, "language": r.language,
            "status": r.status, "provider_id": r.provider_id, "error": r.error,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]

    return {
        "total_24h": total_24h, "total_7d": total_7d, "total_30d": total_30d,
        "failed_30d": failed_30d, "by_template": by_template,
        "rows": serialized,
    }


@router.get("/templates")
async def email_templates(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Distinct templates seen in the log — populates the filter dropdown."""
    rows = (await db.execute(
        text("SELECT DISTINCT template FROM email_log ORDER BY template")
    )).all()
    return {"templates": [r[0] for r in rows if r[0]]}


# ---------------------------------------------------------------------------
# Composer (preview / test-send / broadcast — Phase 7 wires real sending)
# ---------------------------------------------------------------------------

class ComposeRequest(BaseModel):
    """Body shape mirrors v1 admin/email_routes.py composer JSON."""

    subject: str | None = ""
    body: str | None = ""
    language: str | None = "en"
    llm_rewrite: bool = False
    mode: str | None = "all"  # "all" | "list" | "specific"
    list_id: int | None = None
    user_ids: list[int] | None = None


def _wrap_html_body(body: str) -> str:
    """If body looks like plaintext, wrap each paragraph in <p>. v1 logic
    at email_routes.py:147-150 + :191-194."""
    if "<" in body:
        return body
    return "".join(
        f"<p style=\"margin:0 0 16px;\">{line.strip()}</p>"
        for line in body.split("\n\n")
        if line.strip()
    )


@router.post("/preview")
async def email_preview(
    body: ComposeRequest,
    _: str = Depends(get_current_admin),
) -> dict:
    """Render a quick HTML preview of the composed body. The full Jinja2
    template wrapper (`_broadcast_wrapper`) and LLM-rewrite path land in
    Phase 7 — for now we ship a simple preview so the admin UI's preview
    pane works."""
    subject = (body.subject or "").strip()
    raw = (body.body or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="body required")

    html_body = _wrap_html_body(raw)
    cta_url = (settings.APP_BASE_URL or "http://localhost:5050") + "/main"
    full_html = (
        f"<div style=\"font-family:system-ui;max-width:560px;margin:0 auto;\">"
        f"<h1 style=\"font-size:22px;\">{subject or '(no subject)'}</h1>"
        f"{html_body}"
        f"<p><a href=\"{cta_url}\">Open NextPlay</a></p>"
        f"</div>"
    )
    return {
        "subject": subject,
        "html": full_html,
        "stub": True,  # signals UI that LLM-rewrite + final wrapper come in Phase 7
    }


@router.post("/test-send")
async def email_test_send(
    body: ComposeRequest,
    _: str = Depends(get_current_admin),
) -> dict:
    """Send a test broadcast to ADMIN_EMAIL only. Phase 7 wires Resend;
    for now we log + return ok so the UI flow works in dev."""
    if not settings.ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="ADMIN_EMAIL env var not set")
    subject = (body.subject or "").strip()
    raw = (body.body or "").strip()
    if not subject or not raw:
        raise HTTPException(status_code=400, detail="subject and body are required")

    logger.info(
        "[admin-email] test-send subject=%r → %s (STUB until Phase 7)",
        subject, settings.ADMIN_EMAIL,
    )
    return {"ok": True, "to": settings.ADMIN_EMAIL, "stub": True}


@router.post("/send")
async def email_broadcast(
    body: ComposeRequest,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Resolve the recipient set + queue a broadcast. The actual queue
    runner (Resend + per-user template rendering) lands in Phase 7;
    here we count recipients and return the queued count so the admin UI
    can confirm the recipient set."""
    subject = (body.subject or "").strip()
    raw = (body.body or "").strip()
    if not subject or not raw:
        raise HTTPException(status_code=400, detail="subject and body are required")

    mode = (body.mode or "all").lower()
    if mode == "all":
        stmt = select(User).where(
            User.deleted_at.is_(None),
            User.email.isnot(None),
            User.email_marketing.is_(True),
        )
    elif mode == "list":
        if not body.list_id:
            raise HTTPException(status_code=400, detail="list_id required for mode=list")
        stmt = (
            select(User)
            .join(MailingListMember, MailingListMember.user_id == User.id)
            .where(MailingListMember.list_id == body.list_id)
            .where(User.deleted_at.is_(None))
            .where(User.email.isnot(None))
            .where(User.email_marketing.is_(True))
        )
    elif mode == "specific":
        if not body.user_ids:
            raise HTTPException(status_code=400, detail="user_ids required for mode=specific")
        stmt = select(User).where(
            User.id.in_(body.user_ids),
            User.deleted_at.is_(None),
            User.email.isnot(None),
        )
    else:
        raise HTTPException(status_code=400, detail="invalid mode")

    recipients = list((await db.execute(stmt)).scalars().all())
    logger.info(
        "[admin-email] broadcast subject=%r mode=%s recipients=%d (STUB)",
        subject, mode, len(recipients),
    )
    return {
        "queued": len(recipients),
        "recipient_count": len(recipients),
        "stub": True,
    }


@router.get("/users")
async def email_users_search(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    q: str = Query(default="", min_length=0, max_length=128),
) -> dict:
    """Used by the composer's 'specific users' mode. Empty `q` returns
    nothing — keeps the surface tight."""
    q = q.strip().lower()
    if not q:
        return {"users": []}
    like = f"%{q}%"
    stmt = (
        select(User.id, User.email, User.display_name)
        .where(User.deleted_at.is_(None))
        .where(User.email.isnot(None))
        .where(
            func.lower(User.email).like(like) |
            func.lower(User.display_name).like(like)
        )
        .order_by(User.email)
        .limit(20)
    )
    rows = (await db.execute(stmt)).all()
    return {
        "users": [
            {"id": r.id, "email": r.email, "display_name": r.display_name}
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Mailing lists
# ---------------------------------------------------------------------------

class MailingListCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = ""


class MailingListMemberAddRequest(BaseModel):
    """Add by email — we resolve user_id server-side. Mirrors v1 endpoint."""

    email: EmailStr


@router.get("/lists")
async def list_mailing_lists(
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """All mailing lists with member counts. Order by name."""
    rows = (await db.execute(
        text(
            "SELECT ml.id, ml.name, ml.description, ml.created_at,"
            " (SELECT COUNT(*) FROM mailing_list_members m WHERE m.list_id = ml.id) AS member_count "
            "FROM mailing_lists ml ORDER BY ml.name"
        )
    )).mappings().all()
    return {
        "lists": [
            {
                "id": r["id"], "name": r["name"], "description": r["description"],
                "created_at": (r["created_at"].isoformat()
                               if hasattr(r["created_at"], "isoformat") else r["created_at"]),
                "member_count": int(r["member_count"] or 0),
            }
            for r in rows
        ]
    }


@router.post("/lists")
async def create_mailing_list(
    body: MailingListCreateRequest,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a list. UNIQUE(name) — duplicates return 400."""
    name = body.name.strip()
    existing = (await db.execute(
        select(MailingList).where(MailingList.name == name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="list name already exists")

    ml = MailingList(name=name, description=(body.description or "").strip())
    db.add(ml)
    await db.flush()
    return {"ok": True, "id": ml.id}


@router.delete("/lists/{list_id}")
async def delete_mailing_list(
    list_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Hard delete the list AND its members. v1 does both inline."""
    await db.execute(
        delete(MailingListMember).where(MailingListMember.list_id == list_id)
    )
    await db.execute(delete(MailingList).where(MailingList.id == list_id))
    await db.flush()
    return {"ok": True}


@router.get("/lists/{list_id}/members")
async def list_mailing_list_members(
    list_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (await db.execute(
        text(
            "SELECT u.id, u.email, u.display_name FROM mailing_list_members m "
            "JOIN users u ON u.id = m.user_id WHERE m.list_id = :lid "
            "ORDER BY u.email"
        ),
        {"lid": list_id},
    )).mappings().all()
    return {"members": [dict(r) for r in rows]}


@router.post("/lists/{list_id}/members")
async def add_mailing_list_member(
    list_id: int,
    body: MailingListMemberAddRequest,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Resolve email → user_id, then add to list. 404 if no such user.
    Idempotent: re-adding is silent (matches v1's `try/except pass` pattern)."""
    user = (await db.execute(
        select(User).where(User.email == body.email.lower())
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="no user with that email")

    if not (await db.execute(
        select(MailingListMember).where(
            MailingListMember.list_id == list_id,
            MailingListMember.user_id == user.id,
        )
    )).scalar_one_or_none():
        db.add(MailingListMember(list_id=list_id, user_id=user.id))
        await db.flush()
    return {"ok": True}


@router.delete("/lists/{list_id}/members/{user_id}")
async def remove_mailing_list_member(
    list_id: int,
    user_id: int,
    _: str = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await db.execute(
        delete(MailingListMember).where(
            MailingListMember.list_id == list_id,
            MailingListMember.user_id == user_id,
        )
    )
    await db.flush()
    return {"ok": True}
