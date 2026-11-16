"""Self-hosted OpenAI-compatible servers: vLLM, SGLang and llama.cpp.

A provider's *type* is its protocol, and all three speak the OpenAI one, so none
is a new type: each is a catalogue entry (``catalogue_data/providers.d/``) whose
plugin names the vendor's quirks, the way Cortecs and Melious do.

What they have in common, and what separates them from a hosted API:

* **No price.** The hardware is the administrator's own, so every model the
  server lists is imported free (zero rates). The ledger still counts tokens, so
  quotas and usage reports work.
* **No address of their own.** The administrator types the server's URL.
* **A key is optional.** A server started without ``--api-key`` needs none.
* **Capabilities depend on how the server was started** (tool-call and reasoning
  parsers, ``--jinja``), so the console probes the server when its models are
  listed or imported (``gateway/server_probe.py``) and proposes what it found.

The plugin only declares the facts: the flavour the probe should speak, and where
an upstream needs the request shaped differently (SGLang's DeepSeek V4).
"""

from __future__ import annotations

import re
from typing import Any

from gateway.models import ApiSurface
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.server_probe import LLAMACPP, SGLANG, VLLM


class SelfHostedPlugin(GenericOpenAIPlugin):
    """The shared base: free models, an optional key, probe-able."""

    #: Read with ``getattr`` by the discovery and import code: models this
    #: provider lists are imported at zero rates, and its server is probed.
    self_hosted = True
    requires_api_key = False
    default_base_url: str | None = None
    #: Which server the probe should speak to. Set by each subclass.
    flavour: str = ""


class VllmPlugin(SelfHostedPlugin):
    name = "vllm"
    label = "vLLM"
    description = (
        "A vLLM server you run yourself, OpenAI-compatible. Models are free to the "
        "gateway; tools and reasoning need the server's parser flags."
    )
    flavour = VLLM


class LlamaCppPlugin(SelfHostedPlugin):
    name = "llamacpp"
    label = "llama.cpp"
    description = (
        "A llama.cpp llama-server you run yourself, OpenAI-compatible. Models are free "
        "to the gateway; tools need the server's --jinja flag."
    )
    flavour = LLAMACPP


#: A served name that is DeepSeek V4 (``deepseek-ai/DeepSeek-V4-Flash`` and the
#: like). Matched on the name because SGLang's DeepSeek V4 path is chosen by the
#: server's parsers, which the gateway cannot see.
_DEEPSEEK_V4 = re.compile(r"deepseek[-_ ]?v4", re.IGNORECASE)


class SglangPlugin(SelfHostedPlugin):
    name = "sglang"
    label = "SGLang"
    description = (
        "An SGLang server you run yourself, OpenAI-compatible. Models are free to the "
        "gateway; tools and reasoning need the server's parser flags."
    )
    flavour = SGLANG

    def prepare_payload(self, payload: dict[str, Any], *, surface: ApiSurface) -> dict[str, Any]:
        """Usage on streams, and DeepSeek V4's thinking switch.

        SGLang serves DeepSeek V4 through its own chat encoding, which thinks only
        when ``chat_template_kwargs.thinking`` is true (server default: off). The
        effort itself is passed through unchanged (a live DeepSeek V4 on SGLang
        accepts ``medium`` and reasons), so for an effort, whether the client's or
        the model's gateway-side default:

        * ``none`` turns thinking off and sends no effort;
        * any other effort switches ``thinking`` on, and is sent as it is.

        Only for a model whose name says DeepSeek V4. A client's own
        ``chat_template_kwargs.thinking`` is never overwritten.
        """
        payload = super().prepare_payload(payload, surface=surface)
        if surface is not ApiSurface.CHAT_COMPLETIONS:
            return payload
        if not _DEEPSEEK_V4.search(str(payload.get("model") or "")):
            return payload
        effort = payload.get("reasoning_effort")
        if not isinstance(effort, str):
            return payload
        kwargs = dict(payload.get("chat_template_kwargs") or {})
        kwargs.setdefault("thinking", effort != "none")
        payload["chat_template_kwargs"] = kwargs
        if effort == "none":
            payload.pop("reasoning_effort")
        return payload
