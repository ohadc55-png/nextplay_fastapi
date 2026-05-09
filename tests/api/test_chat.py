"""Chat router smoke tests with a mocked OpenAI client.

We don't burn real tokens in CI — every test patches `src.crew.llm.get_client`
to return a fake AsyncOpenAI that yields canned responses. The tests verify:
  - request shape is validated (Pydantic)
  - both user + assistant messages land in `conversations`
  - cost rows land in `api_usage_logs`
  - SSE events come out in the v1 byte-for-byte format
  - errors degrade gracefully (no 500)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from src.crew import llm as llm_module
from src.services import chat_service
from src.models.analytics import ApiUsageLog
from src.models.conversations import Conversation


# ---------------------------------------------------------------------------
# Fake OpenAI helpers
# ---------------------------------------------------------------------------

def _fake_completion(text: str = "Hello coach!"):
    """Build a fake non-streaming OpenAI completion response."""
    return SimpleNamespace(
        model="gpt-4o-mini",
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text),
        )],
        usage=SimpleNamespace(prompt_tokens=42, completion_tokens=18),
    )


async def _fake_stream():
    """Async generator that yields chunks then a usage block — same shape
    the OpenAI SDK emits for stream=True + include_usage."""
    pieces = ["Hello", " coach", "!"]
    for p in pieces:
        yield SimpleNamespace(
            model="gpt-4o-mini",
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=p),
            )],
            usage=None,
        )
    # Final usage chunk — no choices
    yield SimpleNamespace(
        model="gpt-4o-mini",
        choices=[],
        usage=SimpleNamespace(prompt_tokens=42, completion_tokens=18),
    )


@pytest.fixture
def fake_openai():
    """Patch get_client() to return a fake AsyncOpenAI for the test."""
    fake_completions = SimpleNamespace(
        create=AsyncMock(side_effect=_make_create()),
    )
    fake_chat = SimpleNamespace(completions=fake_completions)
    fake_client = SimpleNamespace(chat=fake_chat)
    # Patch BOTH the original module and the service-side import — Python
    # imports `get_client` by-value, so chat_service's reference is bound at
    # import time and won't see a llm_module patch alone.
    with patch.object(llm_module, "get_client", return_value=fake_client), \
         patch.object(chat_service, "get_client", return_value=fake_client):
        yield fake_client


def _make_create():
    """Side-effect router: streaming calls return an async iterable, non-
    streaming calls return a single response object."""
    async def _create(**kwargs):
        if kwargs.get("stream"):
            return _fake_stream()
        return _fake_completion("Hello coach!")
    return _create


# ---------------------------------------------------------------------------
# /api/chat (non-streaming)
# ---------------------------------------------------------------------------

class TestChat:
    async def test_anon_request_rejected(self, api_client: AsyncClient):
        r = await api_client.post(
            "/api/chat",
            json={"message": "hi", "session_id": "s1"},
        )
        assert r.status_code == 401

    async def test_happy_path_persists_messages_and_logs_cost(
        self, authed_client: AsyncClient, api_session_factory, fake_openai
    ):
        r = await authed_client.post(
            "/api/chat",
            json={"message": "How do we beat zone defense?", "session_id": "s1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["response"] == "Hello coach!"
        assert body["session_id"] == "s1"
        assert body["agent_used"] == "default"

        async with api_session_factory() as s:
            msgs = list((await s.execute(
                select(Conversation).order_by(Conversation.id)
            )).scalars().all())
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[0].content == "How do we beat zone defense?"
            assert msgs[1].content == "Hello coach!"
            assert msgs[1].agent_used == "default"

            usage = list((await s.execute(select(ApiUsageLog))).scalars().all())
            assert len(usage) == 1
            assert usage[0].prompt_tokens == 42
            assert usage[0].completion_tokens == 18

    async def test_empty_message_rejected_at_pydantic(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/chat",
            json={"message": "", "session_id": "s1"},
        )
        assert r.status_code == 422  # min_length=1

    async def test_too_long_message_rejected(self, authed_client: AsyncClient):
        r = await authed_client.post(
            "/api/chat",
            json={"message": "x" * 6000, "session_id": "s1"},
        )
        assert r.status_code == 422

    async def test_openai_failure_returns_friendly_message(
        self, authed_client: AsyncClient, api_session_factory
    ):
        # Patch get_client to return a client whose .chat.completions.create raises
        from openai import APIError as OpenAPIError
        bad_completions = SimpleNamespace(
            create=AsyncMock(side_effect=OpenAPIError("boom", request=None, body=None)),
        )
        bad_client = SimpleNamespace(chat=SimpleNamespace(completions=bad_completions))

        with patch.object(llm_module, "get_client", return_value=bad_client), \
             patch.object(chat_service, "get_client", return_value=bad_client):
            r = await authed_client.post(
                "/api/chat",
                json={"message": "hi", "session_id": "s1"},
            )
        # Chat must NEVER 500 on the user
        assert r.status_code == 200
        assert "trouble" in r.json()["response"].lower()
        assert r.json()["agent_used"] == "error"

        # User message persisted even though LLM failed
        async with api_session_factory() as s:
            msgs = list((await s.execute(
                select(Conversation).order_by(Conversation.id)
            )).scalars().all())
            assert [m.role for m in msgs] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# /api/chat-stream (SSE)
# ---------------------------------------------------------------------------

class TestChatStream:
    async def test_stream_emits_chunks_and_done(
        self, authed_client: AsyncClient, api_session_factory, fake_openai
    ):
        r = await authed_client.post(
            "/api/chat-stream",
            json={"message": "hi", "session_id": "s2"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")

        text = r.text
        # Chunks come in v1 format: data: {"t":"chunk","c":"..."}\n\n
        events = [line for line in text.split("\n\n") if line.startswith("data:")]
        parsed = [json.loads(e[len("data:"):].strip()) for e in events]
        types = [p["t"] for p in parsed]
        assert types[-1] == "done"
        chunks = [p["c"] for p in parsed if p["t"] == "chunk"]
        assert "".join(chunks) == "Hello coach!"

        async with api_session_factory() as s:
            msgs = list((await s.execute(
                select(Conversation).order_by(Conversation.id)
            )).scalars().all())
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[1].content == "Hello coach!"

            # Cost logged via the stream's final usage chunk
            usage = list((await s.execute(select(ApiUsageLog))).scalars().all())
            assert len(usage) == 1
            assert usage[0].endpoint == "chat-stream"


# ---------------------------------------------------------------------------
# /api/opening-message (stub until agent personalities)
# ---------------------------------------------------------------------------

class TestOpening:
    async def test_returns_greeting_with_user_name(
        self, authed_client: AsyncClient
    ):
        r = await authed_client.post(
            "/api/opening-message",
            json={"agent": "gm", "session_id": "s3"},
        )
        assert r.status_code == 200
        body = r.json()
        # Default registered user is "Tester" (from authed_client fixture)
        assert "Tester" in body["response"]
        assert body["agent_used"] == "gm"
