"""The self-hosted server probe: parsing from recorded shapes, and the requests.

The fixtures are the shapes each server documents or its code produces (vLLM's
``/version`` and ``max_model_len``, SGLang's ``/server_info`` and ``ModelCard``,
llama.cpp's ``/props``), so a change to a server's answer shows up here as a
failing parse and not as a silent loss of the probe.
"""

from __future__ import annotations

import json

import httpx
import pytest
from gateway import server_probe as sp
from gateway.models import ApiSurface
from gateway.plugins.selfhosted import SglangPlugin

VLLM_MODELS = {
    "object": "list",
    "data": [
        {
            "id": "Qwen/Qwen2.5-7B-Instruct",
            "object": "model",
            "owned_by": "vllm",
            "max_model_len": 32768,
        }
    ],
}
SGLANG_MODELS = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-ai/DeepSeek-V4-Flash",
            "object": "model",
            "owned_by": "sglang",
            "max_model_len": 131072,
        }
    ],
}
LLAMACPP_MODELS = {
    "object": "list",
    "data": [
        {"id": "qwen2.5-0.5b-instruct-q4_k_m.gguf", "object": "model", "owned_by": "llamacpp"}
    ],
}
LLAMACPP_PROPS = {
    "default_generation_settings": {"id": 0, "n_ctx": 2048, "temperature": 0.8},
    "total_slots": 2,
    "model_path": "/models/qwen2.5-0.5b-instruct-q4_k_m.gguf",
    "build_info": "b6123-1a2b3c4",
}

TOOL_CALLS = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ]
}


def answer(content: str, **extra: object) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content, **extra}}]}


class TestIdentity:
    def test_vllm(self) -> None:
        identity = sp.parse_identity(sp.VLLM, {"version": "0.10.1"})
        assert identity is not None
        assert identity.label == "vLLM 0.10.1"

    def test_sglang(self) -> None:
        identity = sp.parse_identity(sp.SGLANG, {"model_path": "x", "version": "0.5.3"})
        assert identity is not None and identity.label == "SGLang 0.5.3"

    def test_llamacpp_reports_the_context_of_one_slot(self) -> None:
        identity = sp.parse_identity(sp.LLAMACPP, LLAMACPP_PROPS)
        assert identity is not None
        assert identity.context_window == 2048
        assert identity.slots == 2
        assert identity.label == "llama.cpp b6123-1a2b3c4"

    @pytest.mark.parametrize("flavour", sp.FLAVOURS)
    @pytest.mark.parametrize("body", [None, [], "text", {"unrelated": 1}])
    def test_an_unrecognised_answer_identifies_nothing(self, flavour: str, body: object) -> None:
        assert sp.parse_identity(flavour, body) is None

    @pytest.mark.parametrize(
        ("payload", "flavour"),
        [
            (VLLM_MODELS, sp.VLLM),
            (SGLANG_MODELS, sp.SGLANG),
            (LLAMACPP_MODELS, sp.LLAMACPP),
            ({"data": [{"id": "x", "owned_by": "someone"}]}, None),
            (None, None),
        ],
    )
    def test_the_models_listing_is_the_fallback_identity(
        self, payload: object, flavour: str | None
    ) -> None:
        assert sp.flavour_from_models(payload) == flavour

    def test_context_window_from_max_model_len(self) -> None:
        assert sp.context_from_models(VLLM_MODELS, "Qwen/Qwen2.5-7B-Instruct") == 32768
        assert sp.context_from_models(SGLANG_MODELS, "deepseek-ai/DeepSeek-V4-Flash") == 131072
        assert sp.context_from_models(VLLM_MODELS, "another") is None

    @pytest.mark.parametrize(
        ("base", "root"),
        [
            ("http://host:8000/v1", "http://host:8000"),
            ("http://host:8000/v1/", "http://host:8000"),
            ("http://host:8000", "http://host:8000"),
            ("https://h/prefix/v1", "https://h/prefix"),
        ],
    )
    def test_root_url(self, base: str, root: str) -> None:
        assert sp.root_url(base) == root


class TestToolAnalysis:
    def test_proper_tool_calls_mean_tools_are_supported(self) -> None:
        found = sp.analyse_tool_response(sp.VLLM, 200, TOOL_CALLS)
        assert found.tools is True
        assert found.supported_features == ("tools",)
        assert found.warnings == ()

    @pytest.mark.parametrize(
        ("flavour", "content", "flag"),
        [
            (sp.VLLM, '<tool_call>\n{"name": "get_weather"}\n</tool_call>', "hermes"),
            (sp.SGLANG, '<tool_call>\n{"name": "get_weather"}\n</tool_call>', "qwen25"),
            (sp.SGLANG, "<｜DSML｜tool_calls>...", "--tool-call-parser deepseekv4"),  # noqa: RUF001
            (sp.VLLM, "<|tool_call_begin|>functions.get_weather:0", "kimi_k2"),
            (sp.VLLM, '[TOOL_CALLS][{"name": "get_weather"}]', "--tool-call-parser mistral"),
            (sp.LLAMACPP, '<tool_call>{"name": "get_weather"}</tool_call>', "--jinja"),
        ],
    )
    def test_markup_in_the_answer_means_the_servers_parser_is_missing(
        self, flavour: str, content: str, flag: str
    ) -> None:
        found = sp.analyse_tool_response(flavour, 200, answer(content))
        assert found.tools is False
        assert found.supported_features == ()
        (warning,) = found.warnings
        assert "parser is missing" in warning and flag in warning

    def test_vllm_refuses_auto_tool_choice_without_its_flags(self) -> None:
        body = {
            "object": "error",
            "message": '"auto" tool choice requires --enable-auto-tool-choice and '
            "--tool-call-parser to be set",
            "code": 400,
        }
        found = sp.analyse_tool_response(sp.VLLM, 400, body)
        assert found.tools is False
        assert "--enable-auto-tool-choice --tool-call-parser" in found.warnings[0]

    def test_llamacpp_without_jinja_refuses_tools(self) -> None:
        body = {"error": {"code": 500, "message": "tools param requires --jinja flag"}}
        found = sp.analyse_tool_response(sp.LLAMACPP, 500, body)
        assert found.tools is False
        assert "--jinja" in found.warnings[0]

    @pytest.mark.parametrize(
        ("status", "body"),
        [(200, answer("It is sunny in Paris.")), (500, {"error": {"message": "boom"}}), (200, {})],
    )
    def test_neither_is_unknown_and_claims_nothing(self, status: int, body: dict) -> None:
        found = sp.analyse_tool_response(sp.VLLM, status, body)
        assert found.tools is None
        assert found.warnings == ()

    def test_a_reasoning_field_is_noticed_for_free(self) -> None:
        for key in ("reasoning_content", "reasoning"):
            found = sp.analyse_tool_response(
                sp.SGLANG,
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                key: "Let me think.",
                                **TOOL_CALLS["choices"][0]["message"],
                            }
                        }
                    ]
                },
            )
            assert found.reasoning is True
            assert found.supported_features == ("tools", "reasoning")

    def test_inline_thinking_is_reasoning_the_server_did_not_separate(self) -> None:
        found = sp.analyse_tool_response(sp.VLLM, 200, answer("<think>hmm</think>Paris"))
        assert found.reasoning is True
        assert "--reasoning-parser" in found.warnings[0]

    def test_the_request_is_one_small_non_streaming_call(self) -> None:
        request = sp.tool_request("m")
        assert request["stream"] is False
        assert request["max_tokens"] <= 128
        assert request["tools"][0]["function"]["name"] == "get_weather"
        assert "tool_choice" not in request  # "auto" is what exposes a missing parser


class Server:
    """A fake self-hosted server, answering by path; records what was asked."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, body = self.routes.get((request.method, request.url.path), (404, {}))
            return httpx.Response(status, json=body)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


class TestProbeServer:
    async def test_llamacpp_window_per_slot_and_tools(self) -> None:
        server = Server(
            {
                ("GET", "/props"): (200, LLAMACPP_PROPS),
                ("POST", "/v1/chat/completions"): (200, TOOL_CALLS),
            }
        )
        probe = await sp.probe_server(
            server.client(),
            base_url="http://h:8080/v1",
            api_key=None,
            flavour=sp.LLAMACPP,
            model_ids=["m"],
        )
        assert probe.server == "llama.cpp b6123-1a2b3c4"
        found = probe.models["m"]
        assert found.context_window == 2048
        assert found.tools is True
        assert found.input_modalities == ("text",)
        # Few requests: one identity, one generation.
        assert server.paths() == ["GET /props", "POST /v1/chat/completions"]

    async def test_vllm_window_from_the_models_listing_and_a_missing_parser(self) -> None:
        server = Server(
            {
                ("GET", "/version"): (200, {"version": "0.10.1"}),
                ("POST", "/v1/chat/completions"): (200, answer("<tool_call>{}</tool_call>")),
            }
        )
        probe = await sp.probe_server(
            server.client(),
            base_url="http://h:8000/v1",
            api_key="k",
            flavour=sp.VLLM,
            model_ids=["Qwen/Qwen2.5-7B-Instruct"],
            models_payload=VLLM_MODELS,
        )
        found = probe.models["Qwen/Qwen2.5-7B-Instruct"]
        assert found.context_window == 32768
        assert found.tools is False
        assert "--enable-auto-tool-choice --tool-call-parser hermes" in found.warnings[0]
        # The credential rides along, and only to the administrator's own server.
        assert {r.headers.get("authorization") for r in server.requests} == {"Bearer k"}

    async def test_sglang_tries_server_info_then_the_deprecated_path(self) -> None:
        server = Server(
            {
                ("GET", "/get_server_info"): (200, {"model_path": "x", "version": "0.5.3"}),
                ("POST", "/v1/chat/completions"): (200, TOOL_CALLS),
            }
        )
        probe = await sp.probe_server(
            server.client(),
            base_url="http://h:30000/v1",
            api_key=None,
            flavour=sp.SGLANG,
            model_ids=["m"],
        )
        assert probe.server == "SGLang 0.5.3"
        assert server.paths()[:2] == ["GET /server_info", "GET /get_server_info"]

    async def test_falls_back_to_the_models_listing_when_identity_is_silent(self) -> None:
        server = Server(
            {
                ("GET", "/v1/models"): (200, VLLM_MODELS),
                ("POST", "/v1/chat/completions"): (200, TOOL_CALLS),
            }
        )
        probe = await sp.probe_server(
            server.client(),
            base_url="http://h:8000/v1",
            api_key=None,
            flavour=sp.VLLM,
            model_ids=["Qwen/Qwen2.5-7B-Instruct"],
        )
        assert probe.models["Qwen/Qwen2.5-7B-Instruct"].context_window == 32768

    async def test_a_server_of_another_kind_is_flagged(self) -> None:
        server = Server(
            {
                ("GET", "/v1/models"): (200, LLAMACPP_MODELS),
                ("POST", "/v1/chat/completions"): (200, TOOL_CALLS),
            }
        )
        probe = await sp.probe_server(
            server.client(),
            base_url="http://h:8000/v1",
            api_key=None,
            flavour=sp.VLLM,
            model_ids=["m"],
        )
        assert "llama.cpp, not vLLM" in probe.warnings[0]

    async def test_a_timeout_is_a_warning_and_never_an_exception(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(404)

        probe = await sp.probe_server(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            base_url="http://h:8000/v1",
            api_key=None,
            flavour=sp.VLLM,
            model_ids=["m"],
        )
        assert probe.models["m"].tools is None
        assert "timed out" in probe.models["m"].warnings[0]

    async def test_at_most_a_handful_of_models_are_probed(self) -> None:
        server = Server({("POST", "/v1/chat/completions"): (200, TOOL_CALLS)})
        probe = await sp.probe_server(
            server.client(),
            base_url="http://h:8000/v1",
            api_key=None,
            flavour=sp.VLLM,
            model_ids=[f"m{i}" for i in range(20)],
        )
        assert len(probe.models) == sp.MAX_PROBED_MODELS
        posts = [r for r in server.requests if r.method == "POST"]
        assert len(posts) == sp.MAX_PROBED_MODELS
        assert json.loads(posts[0].content)["max_tokens"] <= 128


class TestSglangDeepseekV4:
    def shape(self, **payload: object) -> dict:
        body = {"model": "deepseek-ai/DeepSeek-V4-Flash", "messages": [], **payload}
        return SglangPlugin().prepare_payload(body, surface=ApiSurface.CHAT_COMPLETIONS)

    def test_an_effort_turns_thinking_on_and_names_a_tier(self) -> None:
        out = self.shape(reasoning_effort="medium")
        assert out["chat_template_kwargs"] == {"thinking": True}
        assert out["reasoning_effort"] == "high"

    @pytest.mark.parametrize("effort", ["xhigh", "max"])
    def test_the_top_efforts_become_max(self, effort: str) -> None:
        assert self.shape(reasoning_effort=effort)["reasoning_effort"] == "max"

    def test_none_turns_thinking_off_and_sends_no_effort(self) -> None:
        out = self.shape(reasoning_effort="none")
        assert out["chat_template_kwargs"] == {"thinking": False}
        assert "reasoning_effort" not in out

    def test_the_clients_own_thinking_switch_is_kept(self) -> None:
        out = self.shape(reasoning_effort="high", chat_template_kwargs={"thinking": False})
        assert out["chat_template_kwargs"] == {"thinking": False}

    def test_no_effort_changes_nothing(self) -> None:
        assert "chat_template_kwargs" not in self.shape()

    def test_another_model_is_left_alone(self) -> None:
        out = SglangPlugin().prepare_payload(
            {"model": "Qwen/Qwen3-8B", "reasoning_effort": "low"},
            surface=ApiSurface.CHAT_COMPLETIONS,
        )
        assert out == {"model": "Qwen/Qwen3-8B", "reasoning_effort": "low"}

    def test_streams_still_ask_for_usage(self) -> None:
        out = self.shape(stream=True)
        assert out["stream_options"] == {"include_usage": True}
