"""Does a tool loop keep hitting Anthropic's prompt cache from one step to the next?

Anthropic caches by prefix: a breakpoint writes one entry, a hash of every byte of
``tools``, ``system`` and ``messages`` up to and including its block, and the next
request reads it only if its own prompt starts with exactly those bytes
(https://platform.claude.com/docs/en/build-with-claude/prompt-caching, "How
automatic prefix checking works"). ``test_anthropic_chat_translation.py`` pins
where the breakpoints go in one request. What decides whether the *next* request
hits is whether the bytes before them stayed the same, and that crosses the whole
request path: the redaction of the whole history (it is re-screened on every turn,
nothing is remembered between requests), the system note, the translation, and the
serialisation. So this runs the real route, turn after turn, against a fake
Anthropic, and compares what reached it.

"Prefix" below is the prompt as the cache reads it: the model, then every tool,
system block and message block in order, one entry each, with ``cache_control``
removed (a breakpoint is not part of what is hashed, which is why moving one
costs nothing). Entries are serialised as the gateway sends them, keys unsorted: a
test that sorted them would hide the instability it is here to find.
"""

from __future__ import annotations

import uuid
from itertools import pairwise
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import orjson
from conftest import FakeUpstream, Seeded
from gateway.config import EntityPolicy, RedactionPolicy
from gateway.models import PrivacyPipeline
from gateway.redaction.note import RESTORABLE_NOTE
from llmp_shared.redaction import PLACEHOLDER_RE
from privacy_helpers import add_policy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_anthropic_chat_route import anthropic as _anthropic_fixture
from test_anthropic_chat_route import message
from test_redaction_http import FakeDetector
from test_redaction_restore_surfaces import CF

OTHER = "BNCLRA70A41F205Z"
SYSTEM = "You are a careful assistant. " * 40
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look a code up.",
            "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
        },
    }
]

Prefix = list[bytes]

#: The seeded model served by an Anthropic provider with cache prices, as the route tests
#: set it up. Assigned rather than imported under its own name, which would make every
#: test parameter that asks for it a redefinition of an unused import.
served = _anthropic_fixture


# ---------------------------------------------------------------------------
# reading a request the way the cache does
# ---------------------------------------------------------------------------


def _bare(block: dict[str, Any]) -> dict[str, Any]:
    """The block without its breakpoint, the one nested in a tool_result's content too."""
    out = {key: value for key, value in block.items() if key != "cache_control"}
    if isinstance(out.get("content"), list):
        out["content"] = [_bare(inner) for inner in out["content"]]
    return out


def prompt(body: dict[str, Any]) -> list[tuple[str, bool, bytes]]:
    """``(label, has_breakpoint, bytes)`` for the model and every block, in prompt order.

    The cache keys on the model, the tools, the system prompt and the messages;
    the model is entry 0. A message block's entry carries its role, because the
    same text as a user turn and as an assistant turn is a different prompt.
    """
    entries = [("model", False, orjson.dumps({"model": body["model"]}))]
    for tool in body.get("tools") or []:
        entries.append((f"tool:{tool['name']}", "cache_control" in tool, orjson.dumps(_bare(tool))))
    for block in body.get("system") or []:
        entries.append(("system", "cache_control" in block, orjson.dumps(_bare(block))))
    for index, turn in enumerate(body["messages"]):
        for block in turn["content"]:
            marked = "cache_control" in block or any(
                "cache_control" in inner
                for inner in (
                    block.get("content") if isinstance(block.get("content"), list) else []
                )
            )
            entry = orjson.dumps({"role": turn["role"], "block": _bare(block)})
            entries.append((f"message{index}:{block['type']}", marked, entry))
    return entries


def whole(body: dict[str, Any]) -> Prefix:
    return [entry for _, _, entry in prompt(body)]


def breakpoint_prefixes(body: dict[str, Any]) -> list[tuple[str, Prefix]]:
    """For each breakpoint, what Anthropic hashes for it: every entry up to its block."""
    entries = prompt(body)
    return [
        (label, [entry for _, _, entry in entries[: index + 1]])
        for index, (label, marked, _) in enumerate(entries)
        if marked
    ]


def starts_with(request: Prefix, prefix: Prefix) -> bool:
    return request[: len(prefix)] == prefix


def hits(earlier: dict[str, Any], later: dict[str, Any]) -> dict[str, bool]:
    """Which of the earlier request's breakpoints the later request would read."""
    request = whole(later)
    return {label: starts_with(request, prefix) for label, prefix in breakpoint_prefixes(earlier)}


def breakpoint_labels(body: dict[str, Any]) -> list[str]:
    return [label for label, _ in breakpoint_prefixes(body)]


# ---------------------------------------------------------------------------
# a tool loop, driven through the route
# ---------------------------------------------------------------------------


def call(n: int, code: str | None) -> dict[str, Any]:
    """The assistant's n-th tool call. The client holds the *restored* value."""
    arguments = orjson.dumps({"code": code} if code else {}).decode()
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"toolu_{n}",
                "type": "function",
                "function": {"name": "lookup", "arguments": arguments},
            }
        ],
    }


def result(n: int, text: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": f"toolu_{n}", "content": text}


async def send(
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream: FakeUpstream,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """One request through ``/v1/chat/completions``; the body Anthropic received."""
    fake_upstream.set_json(message([{"type": "text", "text": "ok"}]))
    response = await client.post(
        "/v1/chat/completions",
        json={"model": seeded.model.name, "messages": messages, "tools": TOOLS},
        headers=seeded.auth,
    )
    assert response.status_code == 200, response.text
    assert str(fake_upstream.urls[-1]).endswith("/messages")
    body: dict[str, Any] = fake_upstream.last_body
    return body


def placeholders(body: dict[str, Any]) -> set[str]:
    """The stand-ins in the conversation. The system note is left out: it quotes
    ``<LABEL_TOKEN>`` as an example, which has the shape of one."""
    return {
        match.group(0) for match in PLACEHOLDER_RE.finditer(orjson.dumps(body["messages"]).decode())
    }


def system_text(body: dict[str, Any]) -> str:
    return "\n".join(block["text"] for block in body["system"])


async def install_rule(
    app: Any,
    session_factory: async_sessionmaker[AsyncSession],
    detector: FakeDetector,
    conditions: list[dict[str, Any]],
) -> None:
    """One administrator rule hiding fiscal codes (restorable), with *conditions*."""
    policy_id = await add_policy(
        app, session_factory, "Codes", entities={"IT_FISCAL_CODE": EntityPolicy()}
    )
    rule = {
        "id": str(uuid.uuid4()),
        "name": "hide",
        "enabled": True,
        "policies": [str(policy_id)],
        "match": "all",
        "conditions": conditions,
        "mode": "anonymise_restore",
        "overrides": {},
    }
    async with session_factory() as db:
        db.add(
            PrivacyPipeline(
                owner_id=None,
                kind="content",
                name="Hide codes",
                enabled=True,
                position=0,
                who={"mode": "all", "ids": []},
                target={"mode": "all", "providers": [], "models": [], "model_types": ["chat"]},
                rules=[rule],
            )
        )
        await db.commit()
    await app.state.privacy.refresh()
    app.state.redactor = detector.redactor()

    async def _noop() -> None:
        return None

    # The request path applies a pipeline's rules only when a resolver is mounted; the
    # environment's own policy ships filtering nothing (ADR 0039).
    app.state.redaction = SimpleNamespace(
        policy=RedactionPolicy(), redactor=app.state.redactor, aclose=_noop
    )


def loop(code: str | None, *results: str) -> list[dict[str, Any]]:
    """The conversation after ``len(results)`` tool steps. *code* is what the user pasted."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"my code is {code}, look it up" if code else "look it up"},
    ]
    for n, text in enumerate(results, start=1):
        messages += [call(n, code), result(n, text)]
    return messages


def assert_every_breakpoint_hits(earlier: dict[str, Any], later: dict[str, Any]) -> None:
    got = hits(earlier, later)
    assert got, "no breakpoints at all"
    assert all(got.values()), f"the next request would miss {[k for k, v in got.items() if not v]}"


def assert_the_loop_reads_the_last_step(earlier: dict[str, Any], later: dict[str, Any]) -> None:
    """The newest turn of step N is the "previous turn" breakpoint of step N+1, and the
    bytes behind it are the same, so N+1 reads what N wrote."""
    wrote = dict(breakpoint_prefixes(earlier))
    read = breakpoint_prefixes(later)
    newest = [label for label in wrote if label.startswith("message")][-1]
    earlier_turn = [(label, prefix) for label, prefix in read if label.startswith("message")][-2]
    assert earlier_turn[1] == wrote[newest], (newest, earlier_turn[0])


# ---------------------------------------------------------------------------
# without redaction: the translation alone
# ---------------------------------------------------------------------------


class TestWithoutRedaction:
    async def test_each_step_reads_the_breakpoints_the_one_before_wrote(
        self, served: Seeded, client: httpx.AsyncClient, fake_upstream: FakeUpstream
    ) -> None:
        steps = [
            await send(client, served, fake_upstream, loop(None, *["r"] * n)) for n in (1, 2, 3, 4)
        ]
        for earlier, later in pairwise(steps):
            assert_every_breakpoint_hits(earlier, later)
            assert_the_loop_reads_the_last_step(earlier, later)
        assert breakpoint_labels(steps[-1]) == [
            "tool:lookup",
            "system",
            "message6:tool_result",
            "message8:tool_result",
        ]


# ---------------------------------------------------------------------------
# (a) redaction on, an unconditional hiding rule
# ---------------------------------------------------------------------------


class TestUnconditionalRule:
    """The rule hides on every turn, so every turn re-screens the whole history to the
    same bytes: the placeholder is a keyed hash of the value, and the system note
    follows the rules in scope, not what a turn happened to contain."""

    async def steps(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> list[dict[str, Any]]:
        await install_rule(app, session_factory, FakeDetector({CF: "IT_FISCAL_CODE"}), [])
        # the code comes back in the second tool result, so later turns hold it in
        # the user message, a tool call's arguments and a tool result
        results = ["no record", f"record for {CF}", "done", "done again"]
        return [
            await send(client, seeded, fake_upstream, loop(CF, *results[:n])) for n in (1, 2, 3, 4)
        ]

    async def test_the_prefix_is_byte_identical_from_step_to_step(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        steps = await self.steps(app, client, served, fake_upstream, session_factory)
        for body in steps:
            sent = orjson.dumps(body).decode()
            assert CF not in sent, "the real code reached the provider"
            assert placeholders(body), "the rule did not hide the code"
        for earlier, later in pairwise(steps):
            assert_every_breakpoint_hits(earlier, later)
            assert_the_loop_reads_the_last_step(earlier, later)

    async def test_the_system_note_is_the_same_final_block_on_every_step(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        steps = await self.steps(app, client, served, fake_upstream, session_factory)
        note = RESTORABLE_NOTE.split("{where}")[0]
        for body in steps:
            assert note in body["system"][-1]["text"]
            assert body["system"][0]["text"].startswith("You are a careful assistant.")
        assert len({orjson.dumps(body["system"]) for body in steps}) == 1
        assert len({orjson.dumps(body["tools"]) for body in steps}) == 1

    async def test_the_same_value_is_the_same_placeholder_on_every_step_and_in_every_process(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        steps = await self.steps(app, client, served, fake_upstream, session_factory)
        seen = [placeholders(body) for body in steps]
        assert all(len(found) == 1 for found in seen), seen
        assert seen[0] == seen[1] == seen[2] == seen[3]
        # a second redactor holding the same key, with nothing remembered between
        # them, derives the same one: that is what lets a worker that never saw
        # the earlier turns build the same prefix
        other = (
            await FakeDetector({CF: "IT_FISCAL_CODE"})
            .redactor()
            .redact_request([{"role": "user", "content": CF}])
        )
        assert {str(other.messages[0]["content"])} == seen[0]

    async def test_the_placeholder_is_in_the_user_turn_the_tool_call_and_the_tool_result(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Every place the client holds the restored value is re-hidden the same way."""
        steps = await self.steps(app, client, served, fake_upstream, session_factory)
        (token,) = placeholders(steps[1])
        sent = steps[1]["messages"]
        user_turn, tool_call, tool_result = sent[0], sent[1], sent[4]
        assert tool_result["content"][0]["type"] == "tool_result"
        for held in (user_turn, tool_call, tool_result):
            assert token in orjson.dumps(held).decode(), held


# ---------------------------------------------------------------------------
# (b) redaction on, a conditional rule ("finds at least 2") that starts to match
# ---------------------------------------------------------------------------


class TestConditionalRuleThatStartsMatching:
    """Documented behaviour, not a defect: a rule whose condition has not been met hides
    nothing, so while the conversation holds one code it goes out as written. At the
    step that brings the second one, the rule matches and the *whole history* is
    re-screened, the first code included. The bytes of the first user turn change
    once, so the message breakpoints written before that step are not read by it
    (one cache miss on the history, rewritten at the new bytes). Everything in front
    of the messages, the tools and the system prompt with its note, is unchanged,
    because the note follows the rules in scope and not the ones that matched; and from
    then on the history only grows, the count stays at two or more, and every step is
    stable again."""

    FINDS_TWO: ClassVar[list[dict[str, Any]]] = [{"key": "finds", "values": [], "count": 2}]

    async def run(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> list[dict[str, Any]]:
        await install_rule(
            app,
            session_factory,
            FakeDetector({CF: "IT_FISCAL_CODE", OTHER: "IT_FISCAL_CODE"}),
            self.FINDS_TWO,
        )
        user_text = f"my code is {CF}, look it up"
        first = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_text},
            call(1, None),
            result(1, "no record"),
        ]
        second = [*first, call(2, None), result(2, f"also {OTHER}")]
        third = [*second, call(3, None), result(3, "done")]
        fourth = [*third, call(4, None), result(4, "done again")]
        return [
            await send(client, seeded, fake_upstream, m) for m in (first, second, third, fourth)
        ]

    async def test_one_code_is_not_hidden_and_two_are(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        steps = await self.run(app, client, served, fake_upstream, session_factory)
        assert CF in orjson.dumps(steps[0]).decode()  # one find: the condition is not met
        for later in steps[1:]:
            sent = orjson.dumps(later).decode()
            assert CF not in sent and OTHER not in sent
            assert len(placeholders(later)) == 2

    async def test_the_tools_and_the_system_prompt_do_not_move(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        steps = await self.run(app, client, served, fake_upstream, session_factory)
        assert len({orjson.dumps(body["tools"]) for body in steps}) == 1
        assert len({orjson.dumps(body["system"]) for body in steps}) == 1
        assert RESTORABLE_NOTE.split("{where}")[0] in system_text(steps[0])
        got = hits(steps[0], steps[1])
        assert got["tool:lookup"] and got["system"]

    async def test_the_message_breakpoints_miss_once_at_the_step_that_matches(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        steps = await self.run(app, client, served, fake_upstream, session_factory)
        before, after = steps[0], steps[1]
        got = hits(before, after)
        message_labels = [label for label in got if label.startswith("message")]
        assert message_labels, got
        assert not any(got[label] for label in message_labels), got
        # why: the first user turn is the one that changed, and only that one
        assert before["messages"][0] != after["messages"][0]
        assert CF in orjson.dumps(before["messages"][0]).decode()
        assert CF not in orjson.dumps(after["messages"][0]).decode()

    async def test_every_step_after_that_is_stable_again(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        steps = await self.run(app, client, served, fake_upstream, session_factory)
        for earlier, later in pairwise(steps[1:]):
            assert_every_breakpoint_hits(earlier, later)
            assert_the_loop_reads_the_last_step(earlier, later)

    async def test_the_placeholders_are_the_same_before_and_after_the_change(
        self,
        app: Any,
        client: httpx.AsyncClient,
        served: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The hash of a value does not depend on when a rule started to match, on the
        other values in the request, or on the process: step 3's tokens are step 2's,
        and a redactor that has seen none of it derives them again."""
        steps = await self.run(app, client, served, fake_upstream, session_factory)
        assert placeholders(steps[1]) == placeholders(steps[2]) == placeholders(steps[3])
        fresh = FakeDetector({CF: "IT_FISCAL_CODE", OTHER: "IT_FISCAL_CODE"}).redactor()
        derived = {
            str(
                (await fresh.redact_request([{"role": "user", "content": value}])).messages[0][
                    "content"
                ]
            )
            for value in (CF, OTHER)
        }
        assert derived == placeholders(steps[1])
        # and the first turn in step 2 holds the one derived for CF, in the place CF was
        assert PLACEHOLDER_RE.search(orjson.dumps(steps[1]["messages"][0]).decode())
