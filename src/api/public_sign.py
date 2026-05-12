"""Public document signing — Phase 2.3.

The ONLY path in the project that runs without any authenticated session:
a parent receives an SMS/email link to `/sign/{token}`, opens it, verifies
OTP (if the template requires signature), fills the form, signs, and
submits.

Architecture invariants — repeat for emphasis:
- **404, never 403.** Invalid/expired/used token, wrong phone, missing
  signing-session cookie — all return 404. Never reveal which condition
  failed; a leak would help an attacker probe for valid tokens.
- **No request.state.user.** We're outside org/coach/admin auth — the
  audit actor is None.
- **Audit every state change.** OTP request/verify, document open, sign —
  each writes to org_audit_logs with anonymous actor.
- **CSRF is NOT applied** to non-/api/* paths (see csrf.py:60) — this
  router naturally bypasses it.

State machine (each step short-circuits at the first miss):
1. Token resolves to a DocumentDelivery → else 404.
2. delivery.expires_at > now → else 410 Gone.
3. document_status != SIGNED → else render already_signed.html.
4. (submit only) signing_session cookie valid for this token → else 404.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.core.exceptions import NotFoundError, ValidationError
from src.frontend import page_context, templates
from src.models.otp_attempts import OTPAttempt
from src.repositories.document_deliveries_repo import DocumentDeliveriesRepository
from src.repositories.otp_attempts_repo import OTPAttemptsRepository
from src.schemas.document_deliveries import OTPRequest, OTPVerify, SignatureSubmit
from src.services import signing_session
from src.services.audit_chain import build_signed_audit
from src.services.document_signed_email import send_signed_document_email
from src.services.org_audit_service import log_org_action
from src.services.pdf_generation_service import generate_final
from src.services.sign_challenge import (
    ATTEMPTS_BEFORE_CHALLENGE,
    issue_arithmetic_challenge,
    verify_challenge,
)
from src.services.sms import get_sms_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["public-sign"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OTP_TTL_MINUTES = 5
OTP_MAX_ATTEMPTS = 3
OTP_MAX_REQUESTS_PER_HOUR = 3
SIGNING_COOKIE_NAME = "signing_session"


def _hash_otp(code: str) -> str:
    """SHA-256 hex over the literal code. OTPs are 6 digits of entropy —
    bcrypt is overkill; we just want O(1) compare and no plaintext at rest."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _generate_otp_code() -> str:
    """Six-digit numeric OTP. secrets.randbelow → unbiased."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _now_utc_naive() -> datetime:
    """Match the DB column type (no tz). Use UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client else "unknown"


def _error_response(request: Request, *, kind: str, status_code: int):
    """Render the generic public error template."""
    return templates.TemplateResponse(
        "public/error.html",
        page_context(request, user=None, extra={"error_kind": kind}),
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# GET /sign/{token} — the signing page (HTML)
# ---------------------------------------------------------------------------


@router.get("/sign/{token}", response_class=HTMLResponse)
async def signing_page(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Render the signing page (or an error / already-signed page).

    Side effect: first GET flips status NOT_OPENED → OPENED + audit row.
    """
    delivery = await DocumentDeliveriesRepository(db).get_by_token(token)
    if delivery is None:
        return _error_response(request, kind="not_found", status_code=404)
    if delivery.expires_at < _now_utc_naive():
        return _error_response(request, kind="expired", status_code=410)
    if delivery.document_status == "SIGNED":
        return templates.TemplateResponse(
            "public/already_signed.html",
            page_context(
                request,
                user=None,
                extra={"delivery": _delivery_for_template(delivery)},
            ),
        )

    if delivery.document_status == "NOT_OPENED":
        delivery.document_status = "OPENED"
        delivery.opened_at = _now_utc_naive()
        await log_org_action(
            db,
            organization_id=delivery.organization_id,
            actor_user_id=None,
            actor_email=None,
            action="document.opened",
            target_type="document_delivery",
            target_id=delivery.id,
            request=request,
            extra={"token_prefix": token[:8]},
        )

    template = delivery.campaign.template
    return templates.TemplateResponse(
        "public/sign.html",
        page_context(
            request,
            user=None,
            extra={
                "delivery": _delivery_for_template(delivery),
                "template": {
                    "id": template.id,
                    "name": template.name,
                    "requires_signature": template.requires_signature,
                    "form_fields": template.form_fields or [],
                    "signature_zones": template.signature_zones or [],
                },
                "token": token,
                "organization_name": delivery.organization.name if delivery.organization else "",
            },
        ),
    )


def _delivery_for_template(delivery) -> dict:
    """Small projection passed to Jinja — no encrypted fields, no raw row."""
    return {
        "id": delivery.id,
        "recipient_name": delivery.recipient_name,
        "player_name": getattr(delivery.player, "name", "") if delivery.player else "",
        "document_status": delivery.document_status,
        "signed_at": delivery.signed_at.isoformat() if delivery.signed_at else None,
        "expires_at": delivery.expires_at.isoformat() if delivery.expires_at else None,
    }


# ---------------------------------------------------------------------------
# POST /sign/{token}/otp/request  — issue an OTP via SMS
# ---------------------------------------------------------------------------


@router.post("/sign/{token}/otp/request", response_model=dict)
async def request_otp(
    token: str,
    body: OTPRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    delivery = await DocumentDeliveriesRepository(db).get_by_token(token)
    if delivery is None or delivery.expires_at < _now_utc_naive():
        raise NotFoundError("Not found")

    # Wrong phone → 404 (same shape as missing — don't leak existence).
    submitted = _normalize_phone(body.phone)
    expected = _normalize_phone(delivery.recipient_phone or "")
    if not expected or submitted != expected:
        raise NotFoundError("Not found")

    # Per-token rate limit (3 requests/hour). IP-level limiter at the
    # middleware would also help; left out for Part A — only this layer.
    otp_repo = OTPAttemptsRepository(db)
    if await otp_repo.count_recent(token, hours=1) >= OTP_MAX_REQUESTS_PER_HOUR:
        await log_org_action(
            db,
            organization_id=delivery.organization_id,
            actor_user_id=None,
            actor_email=None,
            action="signature.otp.request",
            target_type="document_delivery",
            target_id=delivery.id,
            request=request,
            extra={"result": "rate_limited"},
        )
        return JSONResponse(
            status_code=429,
            content={"error": "Too many OTP requests. Try again later."},
        )

    code = _generate_otp_code()
    otp = OTPAttempt(
        organization_id=delivery.organization_id,
        delivery_token=token,
        phone=submitted,
        code_hash=_hash_otp(code),
        expires_at=_now_utc_naive() + timedelta(minutes=OTP_TTL_MINUTES),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500] or None,
    )
    db.add(otp)
    await db.flush()

    sms = get_sms_provider()
    await sms.send(
        submitted,
        f"קוד אימות NEXTPLAY: {code}\nתקף ל-{OTP_TTL_MINUTES} דקות.",
    )

    await log_org_action(
        db,
        organization_id=delivery.organization_id,
        actor_user_id=None,
        actor_email=None,
        action="signature.otp.request",
        target_type="document_delivery",
        target_id=delivery.id,
        request=request,
        extra={"result": "sent", "otp_id": otp.id},
    )
    return {"status": "ok", "expires_in": OTP_TTL_MINUTES * 60}


def _normalize_phone(phone: str) -> str:
    """Strip spaces, dashes, parens. Keep digits + leading + only. Matches
    the lazy form most Israeli forms accept; both '050-1234567' and
    '0501234567' compare equal."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    return digits


# ---------------------------------------------------------------------------
# POST /sign/{token}/otp/verify — check the code + set signing-session cookie
# ---------------------------------------------------------------------------


@router.post("/sign/{token}/otp/verify")
async def verify_otp(
    token: str,
    body: OTPVerify,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    delivery = await DocumentDeliveriesRepository(db).get_by_token(token)
    if delivery is None or delivery.expires_at < _now_utc_naive():
        raise NotFoundError("Not found")

    otp_repo = OTPAttemptsRepository(db)
    otp = await otp_repo.latest_unverified(token)
    if otp is None:
        raise NotFoundError("Not found")
    if otp.expires_at < _now_utc_naive():
        return JSONResponse(status_code=410, content={"error": "Code expired"})

    if otp.attempts >= otp.max_attempts:
        return JSONResponse(
            status_code=429, content={"error": "Too many attempts"}
        )

    # Phase 2 closeout — anti-bot challenge.
    # After 2 failed attempts on this OTP, the next verify MUST also solve
    # a CAPTCHA. This is the layer between "rate-limit per IP" and "OTP
    # entirely locked" — without it, an attacker can burn through 3 codes
    # before they're locked out, and that's enough for some bots to score.
    if otp.attempts >= ATTEMPTS_BEFORE_CHALLENGE:
        challenge_ok = verify_challenge(
            answer=body.challenge_answer,
            expires_at=body.challenge_expires_at,
            token=body.challenge_token,
        )
        if not challenge_ok:
            # Issue a fresh challenge for the next attempt. Don't bump
            # `attempts` — we haven't actually verified the code yet.
            await log_org_action(
                db,
                organization_id=delivery.organization_id,
                actor_user_id=None,
                actor_email=None,
                action="signature.otp.verify",
                target_type="document_delivery",
                target_id=delivery.id,
                request=request,
                extra={"result": "challenge_required", "attempts": otp.attempts},
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Challenge required",
                    "challenge_required": True,
                    "challenge": issue_arithmetic_challenge(),
                },
            )

    otp.attempts += 1

    if _hash_otp(body.code) != otp.code_hash:
        await db.flush()
        await log_org_action(
            db,
            organization_id=delivery.organization_id,
            actor_user_id=None,
            actor_email=None,
            action="signature.otp.verify",
            target_type="document_delivery",
            target_id=delivery.id,
            request=request,
            extra={"result": "invalid", "attempts": otp.attempts},
        )
        # If we just crossed the threshold, surface the challenge in the
        # response so the next attempt can include it without an extra round-trip.
        invalid_body: dict = {"error": "Invalid code"}
        if otp.attempts >= ATTEMPTS_BEFORE_CHALLENGE:
            invalid_body["challenge_required"] = True
            invalid_body["challenge"] = issue_arithmetic_challenge()
        return JSONResponse(status_code=400, content=invalid_body)

    otp.verified_at = _now_utc_naive()
    await db.flush()

    await log_org_action(
        db,
        organization_id=delivery.organization_id,
        actor_user_id=None,
        actor_email=None,
        action="signature.otp.verify",
        target_type="document_delivery",
        target_id=delivery.id,
        request=request,
        extra={"result": "ok"},
    )

    cookie_value = signing_session.issue(token)
    resp = JSONResponse({"status": "verified"})
    resp.set_cookie(
        key=SIGNING_COOKIE_NAME,
        value=cookie_value,
        max_age=signing_session.DEFAULT_TTL_SECONDS,
        httponly=True,
        secure=False,  # set True in prod via settings.is_production check
        samesite="lax",
        path="/sign",
    )
    return resp


# ---------------------------------------------------------------------------
# POST /sign/{token}/submit — final submission
# ---------------------------------------------------------------------------


@router.post("/sign/{token}/submit")
async def submit_signature(
    token: str,
    body: SignatureSubmit,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    signing_cookie: str | None = Cookie(default=None, alias=SIGNING_COOKIE_NAME),
) -> JSONResponse:
    delivery = await DocumentDeliveriesRepository(db).get_by_token(token)
    if delivery is None or delivery.expires_at < _now_utc_naive():
        raise NotFoundError("Not found")
    if delivery.document_status == "SIGNED":
        raise NotFoundError("Not found")

    template = delivery.campaign.template

    # If signature is required, the cookie MUST be valid for this token.
    if template.requires_signature and not signing_session.verify(signing_cookie, token):
        raise NotFoundError("Not found")

    # Validate required fields.
    for field in template.form_fields or []:
        if field.get("required") and not body.form_response.get(field["id"]):
            raise ValidationError(
                f"Missing required field: {field.get('label') or field['id']}",
                code="missing_required_field",
            )

    # Validate signature (if needed).
    if template.requires_signature:
        if body.signature_method not in ("DRAWN", "TYPED"):
            raise ValidationError("signature_method required.", code="missing_signature")
        if body.signature_method == "DRAWN" and not body.signature_image_base64:
            raise ValidationError("Drawn signature missing.", code="missing_signature_image")
        if body.signature_method == "TYPED" and not body.typed_signature:
            raise ValidationError("Typed signature missing.", code="missing_typed_signature")

    # Generate final PDF + upload.
    final_key = await generate_final(
        template=template,
        delivery=delivery,
        form_response=body.form_response,
        signature_image_base64=body.signature_image_base64,
        typed_signature=body.typed_signature,
    )

    # Update delivery row first so audit_chain builder sees the right
    # signature_method / final_pdf_url / signed_at when computing the
    # canonical payload.
    payload_hash = hashlib.sha256(
        repr(sorted(body.form_response.items())).encode("utf-8")
    ).hexdigest()
    signed_at = _now_utc_naive()
    delivery.document_status = "SIGNED"
    delivery.signed_at = signed_at
    delivery.form_response = body.form_response
    delivery.signature_method = body.signature_method
    delivery.final_pdf_url = final_key

    # Phase 2 closeout — hash-chain audit (Part B §13 pitfall #6).
    delivery.audit_data = await build_signed_audit(
        db,
        delivery=delivery,
        payload_hash=payload_hash,
        signed_at_iso=signed_at.isoformat(),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    # Update campaign counter.
    if delivery.campaign is not None:
        delivery.campaign.total_signed = (delivery.campaign.total_signed or 0) + 1
    await db.flush()

    await log_org_action(
        db,
        organization_id=delivery.organization_id,
        actor_user_id=None,
        actor_email=None,
        action="document.sign",
        target_type="document_delivery",
        target_id=delivery.id,
        request=request,
        extra={
            "method": body.signature_method,
            "payload_hash": payload_hash,
        },
    )

    # Confirmation email — fire-and-forget. Failures here don't block.
    try:
        await send_signed_document_email(delivery=delivery, background=background)
    except Exception as e:  # pragma: no cover — non-critical path
        logger.warning("[sign-submit] confirmation email queue failed: %s", e)

    return JSONResponse({"status": "ok", "redirect": f"/sign/{token}/complete"})


# ---------------------------------------------------------------------------
# GET /sign/{token}/complete — post-submission landing page
# ---------------------------------------------------------------------------


@router.get("/sign/{token}/complete", response_class=HTMLResponse)
async def signing_complete(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Any:
    delivery = await DocumentDeliveriesRepository(db).get_by_token(token)
    if delivery is None or delivery.document_status != "SIGNED":
        return RedirectResponse(f"/sign/{token}", status_code=302)
    return templates.TemplateResponse(
        "public/signed_complete.html",
        page_context(
            request,
            user=None,
            extra={
                "delivery": _delivery_for_template(delivery),
                "organization_name": delivery.organization.name if delivery.organization else "",
            },
        ),
    )


__all__ = ["router"]
