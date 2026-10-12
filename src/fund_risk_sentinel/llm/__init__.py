"""模型 API 统一接入层。

对外只需要记住三件事：

.. code-block:: python

    from fund_risk_sentinel.llm import get_client

    client = get_client()
    resp = client.chat_json(
        system="你是高校科研经费风险审查助手。",
        user=材料正文,
        metadata={"task": "reason"},
    )
    findings = resp.parsed["findings"]

换模型只改 ``config/providers.yaml`` 里的 ``active``，代码零改动。
"""

from .anthropic import AnthropicProvider
from .base import (
    BaseLLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    extract_json,
)
from .gemini import GeminiProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider
from .registry import CallRecord, LLMClient, ProviderRegistry, get_client, set_client

__all__ = [
    "BaseLLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "extract_json",
    "OpenAICompatibleProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "MockProvider",
    "ProviderRegistry",
    "LLMClient",
    "CallRecord",
    "get_client",
    "set_client",
]
