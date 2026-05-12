"""Phase 2.7a — SMS safety rails.

Verifies the three layers that gate every RealSMSProvider send:

  1. Kill switch (SMS_KILL_SWITCH=true) blocks every send
  2. Whitelist behaviour:
       - empty list + real provider → blocks everything (fail-closed)
       - explicit list → only listed numbers pass
       - phone normalization handles dashes / + / spaces
  3. Audit logging fires for every attempt, with the phone masked

The mock provider must remain exempt from all three — dev/test runs
should keep working without flipping any safety knobs.
"""

from __future__ import annotations

import pytest

from src.services.sms.base import RealSMSProvider, SMSResult
from src.services.sms.factory import get_sms_provider
from src.services.sms.mock import MockSMSProvider
from src.services.sms.safety import (
    OUTCOME_BLOCKED_KILL_SWITCH,
    OUTCOME_BLOCKED_WHITELIST,
    OUTCOME_SENT,
    _mask_phone,
    _strip_phone,
    kill_switch_active,
    safety_decision,
    whitelist_blocks,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeRealProvider(RealSMSProvider):
    """Pretend Twilio. Records what `_send_via_provider` was asked to do."""

    name = "fake-real"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def _send_via_provider(self, phone: str, message: str) -> SMSResult:
        self.calls.append((phone, message))
        return SMSResult(success=True, message_id=f"fake-{len(self.calls)}")


def _patch_settings(monkeypatch, **values):
    """Patch settings attributes used by the safety helpers. Re-imports the
    module's reference because safety.py reads from settings on every call."""
    from src.services.sms import safety
    for key, value in values.items():
        monkeypatch.setattr(safety.settings, key, value, raising=False)


# ---------------------------------------------------------------------------
# Phone helpers
# ---------------------------------------------------------------------------


async def test_strip_phone_normalizes_dashes_and_plus():
    assert _strip_phone("050-123-4567") == "0501234567"
    assert _strip_phone("+972 50 123 4567") == "972501234567"
    assert _strip_phone("") == ""
    assert _strip_phone(None) == ""


async def test_mask_phone_keeps_last_four():
    assert _mask_phone("050-123-4567") == "******4567"
    assert _mask_phone("") == "***"
    assert _mask_phone("12") == "**"


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


async def test_kill_switch_off_by_default(monkeypatch):
    _patch_settings(monkeypatch, SMS_KILL_SWITCH=False)
    assert kill_switch_active() is False


async def test_kill_switch_on_blocks_real_provider(monkeypatch):
    _patch_settings(monkeypatch, SMS_KILL_SWITCH=True, SMS_ALLOWED_RECIPIENTS="050-1234567")
    provider = _FakeRealProvider()
    result = await provider.send("050-1234567", "hi")

    assert result.success is False
    assert result.error == OUTCOME_BLOCKED_KILL_SWITCH
    # CRITICAL: provider's HTTP call was never made.
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------


async def test_whitelist_empty_blocks_everything(monkeypatch):
    """Fail-closed: real provider + empty whitelist = nothing goes out."""
    _patch_settings(monkeypatch, SMS_KILL_SWITCH=False, SMS_ALLOWED_RECIPIENTS="")
    provider = _FakeRealProvider()
    result = await provider.send("050-1234567", "hi")

    assert result.success is False
    assert result.error == OUTCOME_BLOCKED_WHITELIST
    assert provider.calls == []


async def test_whitelist_passes_only_listed_numbers(monkeypatch):
    _patch_settings(
        monkeypatch,
        SMS_KILL_SWITCH=False,
        SMS_ALLOWED_RECIPIENTS="050-1111111, 052-2222222",
    )
    provider = _FakeRealProvider()

    # Listed number → passes through.
    ok = await provider.send("050-1111111", "you pass")
    assert ok.success is True
    assert len(provider.calls) == 1

    # Same number with dashes / spaces removed → still passes
    # (digit-only normalize handles formatting variations).
    ok2 = await provider.send("0521111111".replace("1", "2"), "you also pass")
    # 0522222222 == "052-2222222" after digit-strip
    assert ok2.success is True
    assert len(provider.calls) == 2

    # Not listed → blocked.
    blocked = await provider.send("054-9999999", "you don't")
    assert blocked.success is False
    assert blocked.error == OUTCOME_BLOCKED_WHITELIST
    assert len(provider.calls) == 2  # unchanged — never reached the provider


async def test_whitelist_blocks_helper_direct(monkeypatch):
    _patch_settings(monkeypatch, SMS_ALLOWED_RECIPIENTS="050-1234567")
    assert whitelist_blocks("050-1234567") is False
    assert whitelist_blocks("050-7654321") is True


# ---------------------------------------------------------------------------
# safety_decision composite
# ---------------------------------------------------------------------------


async def test_safety_decision_kill_switch_wins(monkeypatch):
    """If both kill switch + whitelist would block, the kill switch wins
    because it's the more severe signal (and we want it surfaced first)."""
    _patch_settings(
        monkeypatch, SMS_KILL_SWITCH=True, SMS_ALLOWED_RECIPIENTS="050-1234567",
    )
    allowed, reason = safety_decision("050-1234567")
    assert allowed is False
    assert reason == OUTCOME_BLOCKED_KILL_SWITCH


async def test_safety_decision_clear_path(monkeypatch):
    _patch_settings(
        monkeypatch, SMS_KILL_SWITCH=False, SMS_ALLOWED_RECIPIENTS="050-1234567",
    )
    allowed, reason = safety_decision("050-1234567")
    assert allowed is True
    assert reason is None


# ---------------------------------------------------------------------------
# Mock provider must remain exempt from all three layers
# ---------------------------------------------------------------------------


async def test_mock_provider_ignores_kill_switch(monkeypatch):
    _patch_settings(monkeypatch, SMS_KILL_SWITCH=True, SMS_ALLOWED_RECIPIENTS="")
    mock = MockSMSProvider()
    result = await mock.send("050-anything", "still works")
    assert result.success is True


async def test_mock_provider_ignores_whitelist(monkeypatch):
    _patch_settings(monkeypatch, SMS_KILL_SWITCH=False, SMS_ALLOWED_RECIPIENTS="")
    mock = MockSMSProvider()
    result = await mock.send("054-not-listed", "still works")
    assert result.success is True


# ---------------------------------------------------------------------------
# Factory placeholders
# ---------------------------------------------------------------------------


async def test_factory_returns_mock_by_default():
    provider = get_sms_provider()
    assert isinstance(provider, MockSMSProvider)


async def test_factory_raises_for_placeholder_providers(monkeypatch):
    """Real providers raise a helpful NotImplementedError instead of being
    silently 'unknown'. The message points at the file to create."""
    for name in ("twilio", "inforu", "o19", "meta_whatsapp"):
        from src.core.config import settings
        monkeypatch.setattr(settings, "SMS_PROVIDER", name)
        with pytest.raises(NotImplementedError) as exc:
            get_sms_provider()
        assert name in str(exc.value)
        # Points at the file path so the developer knows what to create.
        assert "src/services/sms/" in str(exc.value)


async def test_factory_unknown_provider_value_raises(monkeypatch):
    from src.core.config import settings
    monkeypatch.setattr(settings, "SMS_PROVIDER", "does-not-exist")
    with pytest.raises(ValueError, match="Unknown SMS_PROVIDER"):
        get_sms_provider()


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


async def test_audit_logger_called_on_real_provider_send(monkeypatch):
    """Every send by a RealSMSProvider — pass or fail — writes one audit
    entry. We patch write_audit to count the calls."""
    _patch_settings(
        monkeypatch, SMS_KILL_SWITCH=False, SMS_ALLOWED_RECIPIENTS="050-1111111",
    )

    captured: list[dict] = []

    async def fake_audit(session, **kwargs):
        captured.append(kwargs)

    # safety is imported INSIDE base.RealSMSProvider.send via a local import,
    # so we need to patch the source attribute.
    from src.services.sms import safety as safety_mod
    monkeypatch.setattr(safety_mod, "write_audit", fake_audit)

    provider = _FakeRealProvider()
    # One sent (whitelisted), one blocked (not whitelisted).
    await provider.send("050-1111111", "ok")
    await provider.send("054-9999999", "blocked")

    assert len(captured) == 2
    assert captured[0]["outcome"] == OUTCOME_SENT
    assert captured[0]["provider"] == "fake-real"
    assert captured[1]["outcome"] == OUTCOME_BLOCKED_WHITELIST
    # Phone is always passed through — masking happens inside write_audit.
    assert captured[0]["phone"] == "050-1111111"
