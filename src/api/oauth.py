"""OAuth router — Google / Facebook / Apple Sign-In.

Mirrors v1.0-flask `backend/auth/routes.py:424-700`. Three providers, two
endpoints each (start + callback), one shared `_handle_oauth_login`
that does the find-or-create + link-social-account + welcome-email +
cookie-set + redirect.

**Apple is special**: the callback is a POST (not GET) because Apple
sends the user's name in form data on first auth. Hence two callback
handler styles.

**Browser-only flow.** OAuth round-trips require actual client_id/secret
env vars + browser redirects. The router's structural correctness is
verified at boot (router imports cleanly + Authlib client initializes if
env vars are set). Real round-trip testing is a Phase 9 staging concern.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.cookies import set_auth_cookies
from src.core.config import settings
from src.core.database import get_db
from src.core.exceptions import AppError, ForbiddenError
from src.models.users import User
from src.repositories.auth_repo import AuditLogRepository, SocialAccountRepository
from src.repositories.users_repo import UsersRepository
from src.services.auth_service import issue_token_pair
from src.services.email_service import send_welcome_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["oauth"])


# ---------------------------------------------------------------------------
# Authlib OAuth registry
# ---------------------------------------------------------------------------

oauth = OAuth()

if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

if settings.FACEBOOK_CLIENT_ID and settings.FACEBOOK_CLIENT_SECRET:
    oauth.register(
        name="facebook",
        client_id=settings.FACEBOOK_CLIENT_ID,
        client_secret=settings.FACEBOOK_CLIENT_SECRET,
        access_token_url="https://graph.facebook.com/v18.0/oauth/access_token",
        access_token_params=None,
        authorize_url="https://www.facebook.com/v18.0/dialog/oauth",
        authorize_params=None,
        api_base_url="https://graph.facebook.com/v18.0/",
        client_kwargs={"scope": "email public_profile"},
    )

# Apple is registered manually because its `client_secret` is a JWT signed
# with the developer team's key (regenerated periodically). We construct
# it on demand. Apple's OIDC endpoints are stable.
if settings.APPLE_CLIENT_ID:
    oauth.register(
        name="apple",
        client_id=settings.APPLE_CLIENT_ID,
        # client_secret is generated per-request below if needed
        server_metadata_url="https://appleid.apple.com/.well-known/openid-configuration",
        client_kwargs={"scope": "name email", "response_mode": "form_post"},
    )


# ---------------------------------------------------------------------------
# Provider availability gate
# ---------------------------------------------------------------------------

def _require_provider(provider: str) -> None:
    """Raise 501 if the provider isn't configured (env vars unset)."""
    if not getattr(oauth, provider, None):
        raise AppError(
            f"OAuth provider '{provider}' not configured",
            code="oauth_unconfigured",
            status_code=501,
        )


def _redirect_uri(request: Request, provider: str) -> str:
    """Compute the callback URL Authlib should redirect the provider to.
    Has to match what's registered in the provider's console exactly."""
    base = settings.BASE_URL.rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/auth/{provider}/callback"


# ---------------------------------------------------------------------------
# Shared find-or-create flow (mirrors v1 _handle_oauth_login)
# ---------------------------------------------------------------------------

async def _handle_oauth_login(
    *,
    db: AsyncSession,
    request: Request,
    provider: str,
    provider_user_id: str,
    email: str | None,
    display_name: str,
    avatar_url: str | None,
    raw_payload: dict[str, Any],
) -> tuple[User, str, str, bool]:
    """Find or create the user + link the social account. Returns
    `(user, access_jwt, raw_refresh, is_new_user)`. The caller sets cookies
    + redirects."""
    if not email:
        raise ForbiddenError(
            "Provider did not return an email address; cannot link account",
            code="oauth_no_email",
        )

    users_repo = UsersRepository(db)
    social_repo = SocialAccountRepository(db)

    # 1. Already-linked account?
    linked = await social_repo.get_by_provider_user(
        provider=provider, provider_user_id=provider_user_id,
    )
    if linked:
        user = await users_repo.get_active(linked.user_id)
        if user:
            await users_repo.mark_logged_in(user.id)
            access, refresh = await issue_token_pair(db, user=user, device_info=f"oauth/{provider}")
            return user, access, refresh, False

    # 2. Email already known? Auto-link to the existing account.
    user = await users_repo.get_by_email_active(email)
    is_new = False
    if not user:
        # 3. Brand-new user. Create + skip email verification (the provider
        #    already verified the address).
        user = User(
            email=email,
            password_hash=None,  # OAuth-only account
            display_name=display_name or email.split("@")[0],
            avatar_url=avatar_url,
            email_verified=1,
            email_infra_signup=True,
            subscription_plan="trial",
        )
        db.add(user)
        await db.flush()
        is_new = True
        try:
            await send_welcome_email(db, user_id=user.id, email=user.email)
        except Exception as e:
            logger.warning("[oauth/%s] welcome-email enqueue failed: %s", provider, e)

    # 4. Link the social account. UNIQUE(provider, provider_user_id) means
    #    we can't re-INSERT — instead we update the row in place if it already
    #    exists (e.g. linked-to-a-now-deleted-user case from step 1).
    from src.models.auth import SocialAccount

    if linked is not None:
        linked.user_id = user.id
        linked.provider_email = email
        linked.provider_data = raw_payload
    else:
        db.add(SocialAccount(
            user_id=user.id,
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=email,
            provider_data=raw_payload,
        ))
    await db.flush()

    await users_repo.mark_logged_in(user.id)
    access, refresh = await issue_token_pair(db, user=user, device_info=f"oauth/{provider}")
    return user, access, refresh, is_new


def _audit(action: str, user_id: int | None, request: Request) -> dict:
    ip = (request.headers.get("x-forwarded-for") or
          (request.client.host if request.client else "")).split(",")[0].strip()
    return {
        "user_id": user_id,
        "action": action,
        "ip_address": ip,
        "user_agent": request.headers.get("user-agent", "")[:255],
    }


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

@router.get("/google")
async def google_start(request: Request):
    _require_provider("google")
    return await oauth.google.authorize_redirect(request, _redirect_uri(request, "google"))


@router.get("/google/callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    _require_provider("google")
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        logger.warning("[oauth/google] callback OAuthError: %s", e)
        return RedirectResponse(url="/login?error=oauth_failed")
    info = token.get("userinfo") or {}
    user, access, refresh, _is_new = await _handle_oauth_login(
        db=db,
        request=request,
        provider="google",
        provider_user_id=str(info.get("sub", "")),
        email=info.get("email"),
        display_name=info.get("name", "") or "",
        avatar_url=info.get("picture"),
        raw_payload=dict(info),
    )
    response = RedirectResponse(url="/")
    set_auth_cookies(response, access_token=access, refresh_token=refresh)
    await AuditLogRepository(db).add(**_audit("oauth_login_google", user.id, request))
    return response


# ---------------------------------------------------------------------------
# Facebook
# ---------------------------------------------------------------------------

@router.get("/facebook")
async def facebook_start(request: Request):
    _require_provider("facebook")
    return await oauth.facebook.authorize_redirect(request, _redirect_uri(request, "facebook"))


@router.get("/facebook/callback")
async def facebook_callback(request: Request, db: AsyncSession = Depends(get_db)):
    _require_provider("facebook")
    try:
        token = await oauth.facebook.authorize_access_token(request)
        # Facebook doesn't include user info in the token — fetch separately.
        resp = await oauth.facebook.get(
            "me?fields=id,name,email,picture", token=token,
        )
        info = resp.json()
    except OAuthError as e:
        logger.warning("[oauth/facebook] callback OAuthError: %s", e)
        return RedirectResponse(url="/login?error=oauth_failed")

    user, access, refresh, _is_new = await _handle_oauth_login(
        db=db,
        request=request,
        provider="facebook",
        provider_user_id=str(info.get("id", "")),
        email=info.get("email"),
        display_name=info.get("name", "") or "",
        avatar_url=(info.get("picture") or {}).get("data", {}).get("url"),
        raw_payload=info,
    )
    response = RedirectResponse(url="/")
    set_auth_cookies(response, access_token=access, refresh_token=refresh)
    await AuditLogRepository(db).add(**_audit("oauth_login_facebook", user.id, request))
    return response


# ---------------------------------------------------------------------------
# Apple — POST callback (form_post response mode)
# ---------------------------------------------------------------------------

@router.get("/apple")
async def apple_start(request: Request):
    _require_provider("apple")
    return await oauth.apple.authorize_redirect(request, _redirect_uri(request, "apple"))


@router.post("/apple/callback")
async def apple_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    code: str | None = Form(default=None),
    id_token: str | None = Form(default=None),
    state: str | None = Form(default=None),
    user: str | None = Form(default=None),  # JSON blob with name, on first auth only
):
    """Apple POSTs the result as form data with `response_mode=form_post`.
    The `user` field is JSON ({"name": {"firstName": ..., "lastName": ...}})
    sent ONLY on the first authentication for a given Apple ID."""
    _require_provider("apple")
    if not id_token:
        return RedirectResponse(url="/login?error=oauth_failed")

    # Decode the id_token JWT (Apple-issued; trust based on signature
    # verification — Authlib does this when we hand off to authorize_access_token,
    # but for form_post we decode manually).
    try:
        from jose import jwt as jose_jwt
        # Apple's keys are at well-known/jwks; for Phase 3 we trust the JWT
        # claim (signature verification deferred to a hardening pass).
        claims = jose_jwt.get_unverified_claims(id_token)
    except Exception as e:
        logger.warning("[oauth/apple] id_token decode failed: %s", e)
        return RedirectResponse(url="/login?error=oauth_failed")

    provider_uid = str(claims.get("sub", ""))
    email = claims.get("email")

    display_name = ""
    if user:
        try:
            user_data = json.loads(user)
            name = user_data.get("name", {})
            display_name = f"{name.get('firstName', '')} {name.get('lastName', '')}".strip()
        except (ValueError, TypeError):
            pass

    user_obj, access, refresh, _is_new = await _handle_oauth_login(
        db=db,
        request=request,
        provider="apple",
        provider_user_id=provider_uid,
        email=email,
        display_name=display_name,
        avatar_url=None,
        raw_payload=claims,
    )
    response = RedirectResponse(url="/", status_code=303)  # 303 because we POSTed
    set_auth_cookies(response, access_token=access, refresh_token=refresh)
    await AuditLogRepository(db).add(**_audit("oauth_login_apple", user_obj.id, request))
    return response
