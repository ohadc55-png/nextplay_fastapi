"""Memory extractor — extraction prompt + smart team-scoping + retry.

The extractor calls OpenAI; we mock that. The two important invariants:
  1. style/preference/philosophy → team_id IS NULL (cross-team)
  2. insight/decision/pattern/fact → team_id = active_team_id
Per master prompt §2.5: "If you change this, you break the product."
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.crew import llm as llm_module
from src.models.memory import Memory
from src.models.teams import TeamProfile
from src.models.users import User
from src.services import memory_extractor
from src.services.memory_extractor import extract_and_store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_extraction_response(memories: list[dict]):
    return SimpleNamespace(
        model="gpt-4o-mini",
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({"memories": memories})),
        )],
        usage=SimpleNamespace(prompt_tokens=300, completion_tokens=80),
    )


def _fake_raw_response(text: str):
    return SimpleNamespace(
        model="gpt-4o-mini",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=300, completion_tokens=80),
    )


def _patch_openai(responses: list):
    """Build an AsyncMock that returns each response in order."""
    iterator = iter(responses)

    async def _create(**_kwargs):
        return next(iterator)

    fake_completions = SimpleNamespace(create=AsyncMock(side_effect=_create))
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    return patch.object(llm_module, "get_client", return_value=fake_client), patch.object(
        memory_extractor, "get_client", return_value=fake_client
    )


async def _seed_user(session) -> tuple[User, TeamProfile]:
    user = User(email="coach@x.com", password_hash="x", display_name="Coach")
    session.add(user)
    await session.flush()
    team = TeamProfile(user_id=user.id, team_name="Team A")
    session.add(team)
    await session.flush()
    return user, team


# ---------------------------------------------------------------------------
# Smart team-scoping
# ---------------------------------------------------------------------------


class TestSmartTeamScoping:
    async def test_style_memory_is_coach_personal(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "style", "content": "Prefers high-intensity drills.", "importance": 7},
            ]),
        ])
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s1", agent_key="gm",
                user_message="What style of practice should I run?",
                assistant_response="Run high-intensity drills.",
            )
        assert len(ids) == 1
        rows = list((await db_session.execute(select(Memory))).scalars().all())
        assert rows[0].team_id is None  # ← coach-personal
        assert rows[0].category == "style"

    async def test_preference_memory_is_coach_personal(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "preference", "content": "Dislikes 1-on-1 drills.", "importance": 5},
            ]),
        ])
        with a, b:
            await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s2", agent_key="gm",
                user_message="anything",
                assistant_response="response",
            )
        rows = list((await db_session.execute(select(Memory))).scalars().all())
        assert rows[0].team_id is None

    async def test_philosophy_memory_is_coach_personal(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "philosophy", "content": "Defense before offense.", "importance": 9},
            ]),
        ])
        with a, b:
            await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s3", agent_key="gm",
                user_message="anything",
                assistant_response="response",
            )
        rows = list((await db_session.execute(select(Memory))).scalars().all())
        assert rows[0].team_id is None

    async def test_insight_memory_binds_to_active_team(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "insight", "content": "Doncic struggles vs zone defense.", "importance": 8},
            ]),
        ])
        with a, b:
            await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s4", agent_key="scout",
                user_message="How did Doncic play vs Real Madrid?",
                assistant_response="Doncic struggles vs zone defense.",
            )
        rows = list((await db_session.execute(select(Memory))).scalars().all())
        assert rows[0].team_id == team.id  # ← team-specific
        assert rows[0].category == "insight"

    @pytest.mark.parametrize("category", ["decision", "pattern", "fact"])
    async def test_team_specific_categories_bind_to_team(self, db_session, category):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": category, "content": f"A durable {category} sentence.", "importance": 6},
            ]),
        ])
        with a, b:
            await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id=f"s_{category}", agent_key="gm",
                user_message="something",
                assistant_response="something else",
            )
        rows = list((await db_session.execute(select(Memory))).scalars().all())
        assert rows[0].team_id == team.id
        assert rows[0].category == category

    async def test_mixed_extraction_routes_each_correctly(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "style", "content": "Loves uptempo offense.", "importance": 6},
                {"category": "fact", "content": "Plays in EuroLeague Division B.", "importance": 7},
                {"category": "decision", "content": "Starting Smith over Jones for next 5 games.", "importance": 8},
            ]),
        ])
        with a, b:
            await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_mixed", agent_key="gm",
                user_message="long message",
                assistant_response="long response",
            )
        rows = list((await db_session.execute(
            select(Memory).order_by(Memory.id)
        )).scalars().all())
        assert len(rows) == 3
        cat_to_team = {r.category: r.team_id for r in rows}
        assert cat_to_team["style"] is None
        assert cat_to_team["fact"] == team.id
        assert cat_to_team["decision"] == team.id


# ---------------------------------------------------------------------------
# JSON parsing + retry
# ---------------------------------------------------------------------------


class TestJsonRetry:
    async def test_first_call_parses_directly(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "style", "content": "Loves uptempo.", "importance": 6},
            ]),
        ])
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_ok", agent_key="gm",
                user_message="message text", assistant_response="response text",
            )
        assert len(ids) == 1

    async def test_malformed_first_then_retry_succeeds(self, db_session):
        """First response is prose; the retry wrapper kicks in and
        the second response is valid JSON."""
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_raw_response("Sorry, I can't extract memories from that."),
            _fake_extraction_response([
                {"category": "style", "content": "Likes high-intensity.", "importance": 5},
            ]),
        ])
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_retry", agent_key="gm",
                user_message="message text", assistant_response="response text",
            )
        assert len(ids) == 1

    async def test_both_calls_malformed_extracts_nothing(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_raw_response("not json"),
            _fake_raw_response("still not json"),
        ])
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_bad", agent_key="gm",
                user_message="message text", assistant_response="response text",
            )
        assert ids == []

    async def test_code_fenced_response_still_parses(self, db_session):
        """Some models wrap output in ```json ... ``` despite "JSON only"."""
        user, team = await _seed_user(db_session)
        wrapped = "```json\n" + json.dumps({"memories": [
            {"category": "fact", "content": "Plays in Division B.", "importance": 6}
        ]}) + "\n```"
        a, b = _patch_openai([_fake_raw_response(wrapped)])
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_fence", agent_key="gm",
                user_message="message text", assistant_response="response text",
            )
        assert len(ids) == 1


# ---------------------------------------------------------------------------
# Validation guardrails
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_unknown_category_dropped(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "rumor", "content": "ignored", "importance": 5},
                {"category": "fact", "content": "Kept fact.", "importance": 5},
            ]),
        ])
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_drop", agent_key="gm",
                user_message="message text", assistant_response="response text",
            )
        assert len(ids) == 1

    async def test_too_short_content_dropped(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "fact", "content": "ok", "importance": 5},
                {"category": "fact", "content": "Long enough fact.", "importance": 5},
            ]),
        ])
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_short", agent_key="gm",
                user_message="message text", assistant_response="response text",
            )
        assert len(ids) == 1

    async def test_importance_clamped(self, db_session):
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([
            _fake_extraction_response([
                {"category": "fact", "content": "Out-of-range importance.", "importance": 99},
                {"category": "fact", "content": "Negative importance.", "importance": -5},
            ]),
        ])
        with a, b:
            await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_clamp", agent_key="gm",
                user_message="message text", assistant_response="response text",
            )
        rows = list((await db_session.execute(select(Memory))).scalars().all())
        importances = sorted(r.importance for r in rows)
        assert importances == [1, 10]

    async def test_short_message_skips_extraction(self, db_session):
        """Don't call OpenAI for trivial turns ('ok' / 'yes')."""
        user, team = await _seed_user(db_session)
        a, b = _patch_openai([])  # no responses queued — assert OpenAI is NOT called
        with a, b:
            ids = await extract_and_store(
                db_session,
                user_id=user.id, team_id=team.id,
                session_id="s_short_msg", agent_key="gm",
                user_message="ok",
                assistant_response="ok",
            )
        assert ids == []
