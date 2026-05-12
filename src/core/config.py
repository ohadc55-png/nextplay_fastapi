"""Application settings.

Mirrors every env var read by the legacy Flask app (audited via
`grep "os.environ\\|os.getenv"` across `backend/`). Defaults match the Flask
behavior so existing Railway deploys can swap in without re-keying the env.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # === Environment / runtime ===
    RAILWAY_ENVIRONMENT: str = ""  # "production" / "staging" on Railway; empty locally
    LOG_LEVEL: str = "INFO"
    PORT: int = 5050
    BASE_URL: str = "http://localhost:5050"
    # APP_BASE_URL is used in email + auth; currently coexists with BASE_URL — preserve both.
    APP_BASE_URL: str = "http://localhost:5050"
    STATIC_URL: str = ""  # external CDN / frontend service URL; empty -> serve from /static
    CORS_ORIGINS: str = ""  # comma-separated; auto-derived in prod when empty

    # === Database ===
    DATABASE_URL: str = ""  # postgresql:// in prod; empty -> aiosqlite local
    DB_POOL_MAX: int = 10

    # === OpenAI / AI ===
    OPENAI_API_KEY: str = ""
    SERPER_API_KEY: str = ""
    JINA_API_KEY: str = ""
    ENABLE_RESEARCH_AGENT: str = "true"  # "true" / "false" / "0" / "1"
    # 0 in Railway prod (no Chromium in Nixpacks build); 1 locally. Preserves Flask behavior.
    ENABLE_PLAYWRIGHT_FALLBACK: str = "1"

    # === Auth ===
    JWT_SECRET_KEY: str = ""
    # SESSION_SECRET_KEY is the new name; FLASK_SECRET_KEY still accepted for backward compat
    # so existing Railway env doesn't need re-keying at cutover.
    SESSION_SECRET_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("SESSION_SECRET_KEY", "FLASK_SECRET_KEY"),
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # === OAuth (Google / Facebook / Apple) ===
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    FACEBOOK_CLIENT_ID: str = ""
    FACEBOOK_CLIENT_SECRET: str = ""
    APPLE_CLIENT_ID: str = ""
    APPLE_CLIENT_SECRET: str = ""
    APPLE_TEAM_ID: str = ""
    APPLE_KEY_ID: str = ""

    # === Admin ===
    ADMIN_EMAILS: str = "admin@nextplay.co"  # comma-separated allowlist for /admin/*
    ADMIN_EMAIL: str = "ohadc55@gmail.com"  # single recipient for admin email tooling
    ADMIN_PASSWORD_HASH: str = ""

    # === AWS S3 + CloudFront ===
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = "nextplay-videos"
    AWS_S3_REGION: str = "eu-central-1"
    CLOUDFRONT_DOMAIN: str = ""

    # === Email (Resend) ===
    EMAIL_MODE: str = "console"  # "console" | "resend"
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "onboarding@resend.dev"
    EMAIL_FROM_NAME_MK: str = "Ohad from NextPlay"  # marketing sender
    EMAIL_FROM_NAME_TX: str = "NextPlay Team"  # transactional sender

    # === Web Push (VAPID) ===
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:ohadc55@gmail.com"
    PUSH_MORNING_START: int = 10
    PUSH_MORNING_END: int = 12  # exclusive end
    PUSH_INACTIVE_HOURS: int = 48

    # === Rate limiting ===
    # When False, RateLimitMiddleware is a no-op. Tests flip this off
    # in conftest because every test re-uses a single app instance and
    # would otherwise burn through the per-endpoint cap. Mirrors v1's
    # `app.config["TESTING"]` skip at flask_app.py:276-277.
    RATE_LIMIT_ENABLED: bool = True

    # === Knowledge base (ChromaDB) ===
    # Path to the ChromaDB persistent store. Default sits next to the
    # repo root so dev + prod look identical. Override via env in CI /
    # ephemeral environments.
    CHROMA_PERSIST_DIR: str = "./knowledge_base/chroma_store"
    CHROMA_COLLECTION: str = "basketball_knowledge"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Cross-encoder reranker (sentence-transformers). Empty disables it.
    RERANKER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-12-v2"

    # === Internal cron auth ===
    CRON_SECRET: str = ""

    # === Field-level encryption (Phase 1.6) ===
    # Url-safe base64 32-byte Fernet key. Generate ONCE with
    # `python -c "from cryptography.fernet import Fernet;
    #             print(Fernet.generate_key().decode())"`.
    # Save to .env locally + Railway secret + a password manager.
    # Losing this key = losing every encrypted PII column (no recovery).
    ENCRYPTION_KEY: str = ""
    # Optional rotation slot. When non-empty, MultiFernet decrypts using both
    # primary and previous; the rotation script then re-writes every row.
    ENCRYPTION_KEY_PREVIOUS: str = ""

    # === SMS provider (Phase 2.3) ===
    # "mock"          → src.services.sms.mock.MockSMSProvider (logs only; dev/test)
    # "twilio"        → reserved for Sub-Phase 2.7 (NotImplementedError until adapter lands)
    # "inforu"        → reserved for Sub-Phase 2.7
    # "o19"           → reserved for Sub-Phase 2.7
    # "meta_whatsapp" → reserved for Sub-Phase 2.7
    SMS_PROVIDER: str = "mock"

    # === Phase 2.7a — SMS safety rails ===
    # Hard kill switch: when True, every real SMS provider refuses to
    # send. The mock provider ignores this.
    SMS_KILL_SWITCH: bool = False
    # CSV of phone numbers a real provider is allowed to send to. With
    # an empty list + a real provider configured, EVERY send is blocked
    # (fail-closed). Use during rollout to test with a personal phone.
    SMS_ALLOWED_RECIPIENTS: str = ""

    # === Phase 2.7a — Provider credential placeholders ===
    # Filled in `.env` once a provider is chosen. Empty by default so
    # the build still passes and tests don't accidentally hit real APIs.
    # Twilio (SMS + WhatsApp):
    SMS_TWILIO_ACCOUNT_SID: str = ""
    SMS_TWILIO_AUTH_TOKEN: str = ""
    SMS_TWILIO_FROM: str = ""
    # Inforu (Israeli SMS):
    SMS_INFORU_USER: str = ""
    SMS_INFORU_PASSWORD: str = ""
    SMS_INFORU_FROM: str = ""
    # 019 (Israeli SMS):
    SMS_O19_TOKEN: str = ""
    SMS_O19_FROM: str = ""
    # Meta WhatsApp Cloud API (direct, no BSP middleware):
    SMS_META_PHONE_NUMBER_ID: str = ""
    SMS_META_ACCESS_TOKEN: str = ""

    # === Monitoring ===
    SENTRY_DSN: str = ""

    @property
    def is_production(self) -> bool:
        return bool(self.RAILWAY_ENVIRONMENT)

    @property
    def database_url_async(self) -> str:
        """Coerce DATABASE_URL into an async-driver URL.

        - Empty -> aiosqlite local file (matches Flask dev fallback).
        - postgres:// or postgresql:// -> postgresql+asyncpg://
        - Anything already containing +asyncpg or +aiosqlite is returned unchanged.
        """
        url = self.DATABASE_URL.strip()
        if not url:
            return "sqlite+aiosqlite:///./data/coach.db"
        if "+asyncpg" in url or "+aiosqlite" in url:
            return url
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def cors_origins_list(self) -> list[str]:
        if self.CORS_ORIGINS.strip():
            return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        if self.is_production:
            return [self.BASE_URL]
        return [
            "http://localhost:5050",
            "http://localhost:3000",
            "http://127.0.0.1:5050",
        ]

    @property
    def admin_emails_list(self) -> list[str]:
        return [e.strip().lower() for e in self.ADMIN_EMAILS.split(",") if e.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
