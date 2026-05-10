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
from src.models.analytics import ApiUsageLog
from src.models.conversations import Conversation
from src.services import chat_service

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
    """Patch get_client() to return a fake AsyncOpenAI for the test.

    Patches every module that imports `get_client` directly — Python
    binds the name at import time, so a single `llm_module` patch
    won't catch the references in service modules. Adding a new
    consumer of `get_client`? Add it here too."""
    from src.research import web_researcher as wr_module
    from src.services import memory_extractor as mem_module

    fake_completions = SimpleNamespace(
        create=AsyncMock(side_effect=_make_create()),
    )
    fake_chat = SimpleNamespace(completions=fake_completions)
    fake_client = SimpleNamespace(chat=fake_chat)
    with patch.object(llm_module, "get_client", return_value=fake_client), \
         patch.object(chat_service, "get_client", return_value=fake_client), \
         patch.object(mem_module, "get_client", return_value=fake_client), \
         patch.object(wr_module, "get_client", return_value=fake_client):
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
        # "our team" hits the own-team semantic layer → routes to gm
        # without an LLM classifier call.
        msg = "What does our team need to work on?"
        r = await authed_client.post(
            "/api/chat",
            json={"message": msg, "session_id": "s1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["response"] == "Hello coach!"
        assert body["session_id"] == "s1"
        # No explicit agent → router lands on gm via own-team marker
        assert body["agent_used"] == "gm"

        async with api_session_factory() as s:
            msgs = list((await s.execute(
                select(Conversation).order_by(Conversation.id)
            )).scalars().all())
            assert [m.role for m in msgs] == ["user", "assistant"]
            assert msgs[0].content == msg
            assert msgs[1].content == "Hello coach!"
            assert msgs[1].agent_used == "gm"

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

class TestListAgents:
    async def test_anon_rejected(self, api_client: AsyncClient):
        r = await api_client.get("/api/agents")
        assert r.status_code == 401

    async def test_returns_5_agents_in_v1_order(self, authed_client: AsyncClient):
        r = await authed_client.get("/api/agents")
        assert r.status_code == 200
        agents = r.json()["agents"]
        keys = [a["key"] for a in agents]
        assert keys == ["gm", "scout", "analytics", "tactics", "training"]
        # Each entry has the display fields the SPA renders
        for a in agents:
            assert a["name"] and a["role"] and a["specialty"]


class TestSpecialistRouting:
    async def test_explicit_agent_persisted(
        self, authed_client: AsyncClient, api_session_factory, fake_openai
    ):
        r = await authed_client.post(
            "/api/chat",
            json={"message": "Scout the opponent", "session_id": "s-scout",
                  "agent": "scout"},
        )
        assert r.status_code == 200
        assert r.json()["agent_used"] == "scout"
        async with api_session_factory() as s:
            from sqlalchemy import select

            from src.models.conversations import Conversation
            msgs = list((await s.execute(
                select(Conversation).where(Conversation.role == "assistant")
            )).scalars().all())
            assert msgs[0].agent_used == "scout"


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


# ---------------------------------------------------------------------------
# Tool-calling loop — agent calls a tool, gets the result, replies
# ---------------------------------------------------------------------------


def _fake_tool_call_response(tool_name: str, args_json: str, call_id: str = "call_1"):
    """A response that includes a tool_calls field — the loop should
    detect it, run the tool, then loop again."""
    msg = SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(name=tool_name, arguments=args_json),
        )],
    )
    return SimpleNamespace(
        model="gpt-4o-mini",
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
    )


class TestToolLoop:
    async def test_tool_call_triggers_executor_and_loop_returns_text(
        self, authed_client: AsyncClient, api_session_factory, fake_openai
    ):
        """Two-round-trip flow: round 1 emits tool_calls → executor runs
        query_team_database → round 2 emits a textual answer that
        references the tool result."""
        # Replace the side_effect with a 2-step iterator that drives the loop
        responses = [
            _fake_tool_call_response(
                "query_team_database", '{"action": "list_roster"}',
            ),
            SimpleNamespace(
                model="gpt-4o-mini",
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="Got it — your roster is empty.", tool_calls=None),
                )],
                usage=SimpleNamespace(prompt_tokens=30, completion_tokens=10),
            ),
        ]
        idx = {"i": 0}

        async def _create(**_kwargs):
            r = responses[idx["i"]]
            idx["i"] += 1
            return r

        fake_openai.chat.completions.create.side_effect = _create

        r = await authed_client.post(
            "/api/chat",
            json={"message": "Who's on our roster?", "session_id": "s-tool"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "roster is empty" in body["response"]
        # GM was the resolved agent (own-team marker "our")
        assert body["agent_used"] == "gm"
        # Two round-trips ⇒ two cost rows
        from src.models.analytics import ApiUsageLog
        async with api_session_factory() as s:
            usage = list((await s.execute(select(ApiUsageLog))).scalars().all())
            assert len(usage) == 2
            assert sum(u.prompt_tokens for u in usage) == 50  # 20 + 30

    async def test_chat_upload_with_csv_runs_through_send_message(
        self, authed_client: AsyncClient, api_session_factory, fake_openai, tmp_path
    ):
        """Upload a CSV → file processor extracts → enriched message
        flows through send_message → assistant response persisted."""
        from src.models.conversations import Conversation
        from src.models.uploads import Upload

        csv_bytes = b"name,points\nDoncic,33\nSmith,12\n"
        files = {"file": ("stats.csv", csv_bytes, "text/csv")}
        form = {
            "session_id": "s-upload",
            "agent": "analytics",
            "message": "What stands out from these stats?",
        }
        r = await authed_client.post("/api/chat-upload", files=files, data=form)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "filenames" in body
        # Saved filename may be `stats.csv` or `stats_N.csv` if a previous
        # test in the session left a file behind — the dedupe logic adds
        # a suffix on collision. Match by stem + extension.
        assert len(body["filenames"]) == 1
        saved = body["filenames"][0]
        assert saved.startswith("stats") and saved.endswith(".csv")
        assert body["agent_used"] == "analytics"

        async with api_session_factory() as s:
            uploads = list((await s.execute(select(Upload))).scalars().all())
            assert len(uploads) == 1
            assert uploads[0].filename == saved
            assert uploads[0].file_type == "csv"

            convs = list((await s.execute(
                select(Conversation).order_by(Conversation.id)
            )).scalars().all())
            # user message + assistant message
            assert [c.role for c in convs] == ["user", "assistant"]
            # User message includes the CSV content via the file processor
            assert "Doncic" in convs[0].content
            assert "What stands out" in convs[0].content

    async def test_chat_upload_rejects_unsupported_extension(
        self, authed_client: AsyncClient, fake_openai
    ):
        files = {"file": ("malware.exe", b"MZ\x00\x00", "application/octet-stream")}
        r = await authed_client.post(
            "/api/chat-upload", files=files,
            data={"session_id": "s-bad", "message": "what?"},
        )
        assert r.status_code == 400

    async def test_chat_upload_rejects_oversized_file(
        self, authed_client: AsyncClient, fake_openai
    ):
        # 11MB > 10MB per-file limit
        big = b"x" * (11 * 1024 * 1024)
        files = {"file": ("big.csv", big, "text/csv")}
        r = await authed_client.post(
            "/api/chat-upload", files=files,
            data={"session_id": "s-big", "message": "x"},
        )
        assert r.status_code == 400
        assert "10 MB" in r.json()["detail"]

    async def test_chat_upload_rejects_no_files(
        self, authed_client: AsyncClient, fake_openai
    ):
        r = await authed_client.post(
            "/api/chat-upload",
            data={"session_id": "s-empty", "message": "no file"},
        )
        assert r.status_code == 400

    async def test_injected_user_id_in_tool_args_is_stripped(
        self, authed_client: AsyncClient, fake_openai
    ):
        """If the LLM (or a malicious prompt) tries to override tenant
        in the tool call, the executor strips user_id/team_id before
        invoking the handler. Closure-captured tenancy wins."""
        # Round 1: tool call with injected user_id; round 2: textual answer
        bad_args = '{"action": "list_roster", "user_id": 99999}'
        responses = [
            _fake_tool_call_response("query_team_database", bad_args),
            SimpleNamespace(
                model="gpt-4o-mini",
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="OK", tool_calls=None),
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
            ),
        ]
        idx = {"i": 0}

        async def _create(**_kwargs):
            r = responses[idx["i"]]
            idx["i"] += 1
            return r

        fake_openai.chat.completions.create.side_effect = _create

        r = await authed_client.post(
            "/api/chat",
            json={"message": "What does our team need to work on?", "session_id": "s-inject"},
        )
        # The chat must not 500. The tool result fed back into round 2
        # is for the legit user — the user_id=99999 was dropped.
        assert r.status_code == 200
        assert r.json()["response"] == "OK"
