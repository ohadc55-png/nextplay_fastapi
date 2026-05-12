"""Phase 2 closeout — anti-bot challenge unit tests."""

from __future__ import annotations

import time

from src.services.sign_challenge import (
    issue_arithmetic_challenge,
    verify_challenge,
)


def test_issued_challenge_has_required_fields():
    c = issue_arithmetic_challenge()
    assert "a" in c and "b" in c
    assert "question" in c
    assert "expires_at" in c
    assert "token" in c
    assert len(c["token"]) == 64  # sha256 hex


def test_verify_correct_answer_passes():
    c = issue_arithmetic_challenge()
    correct = c["a"] + c["b"]
    assert verify_challenge(
        answer=correct,
        expires_at=c["expires_at"],
        token=c["token"],
    ) is True


def test_verify_wrong_answer_fails():
    c = issue_arithmetic_challenge()
    wrong = c["a"] + c["b"] + 1
    assert verify_challenge(
        answer=wrong,
        expires_at=c["expires_at"],
        token=c["token"],
    ) is False


def test_verify_tampered_token_fails():
    c = issue_arithmetic_challenge()
    correct = c["a"] + c["b"]
    assert verify_challenge(
        answer=correct,
        expires_at=c["expires_at"],
        token="0" * 64,
    ) is False


def test_verify_expired_challenge_fails():
    c = issue_arithmetic_challenge()
    correct = c["a"] + c["b"]
    # Force a past timestamp — HMAC will mismatch because the issued
    # token was bound to the original expiry.
    past = int(time.time()) - 1
    assert verify_challenge(
        answer=correct,
        expires_at=past,
        token=c["token"],
    ) is False


def test_verify_handles_missing_inputs():
    assert verify_challenge(answer=None, expires_at=None, token=None) is False
    assert verify_challenge(answer=5, expires_at=None, token="abc") is False


def test_verify_handles_non_int_answer():
    c = issue_arithmetic_challenge()
    assert verify_challenge(
        answer="not a number", expires_at=c["expires_at"], token=c["token"],
    ) is False


def test_challenges_are_unique():
    """Two calls produce different values (high probability)."""
    seen = set()
    for _ in range(20):
        c = issue_arithmetic_challenge()
        seen.add((c["a"], c["b"], c["token"][:8]))
    # With 8^2 * many tokens, we expect at least 5 distinct.
    assert len(seen) > 5
