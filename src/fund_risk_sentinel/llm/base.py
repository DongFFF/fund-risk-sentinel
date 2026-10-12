"""模型接入层抽象基类。

设计目标：**换模型不改业务代码**。

业务侧只依赖 ``BaseLLMProvider.complete(request) -> LLMResponse`` 这一个契约；
新增一家模型服务，只需在 ``llm/`` 下加一个子类并在 ``registry`` 注册协议类型，
``extract`` / ``review`` / ``explain`` 三层完全不用动。
"""

from __future__ import annotations

import abc
import json
import re
import time
from typing import Any, Literal

from pydantic import Field

from ..config.settings import ProviderConfig
from ..core.exceptions import LLMResponseError
from ..core.models import Base

Role = Literal["system", "user", "assistant"]

#: 从模型输出里抠出 JSON：容忍 ```json 围栏、前后寒暄、尾随逗号
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class Message(Base):
    """一条对话消息。"""

    role: Role
    content: str

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> "Message":
        return cls(role="assistant", content=content)


class LLMRequest(Base):
    """统一的模型调用请求。"""

    messages: list[Message] = Field(default_factory=list)
    model: str = Field(default="", description="留空则用 provider 配置中的 model")
    temperature: float | None = None
    max_tokens: int | None = None
    json_mode: bool = Field(default=False, description="要求返回严格 JSON")
    json_schema: dict[str, Any] | None = Field(default=None, description="JSON Schema（服务端支持时生效）")
    stop: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict, description="调用方上下文，用于审计日志")

    @classmethod
    def single_turn(
        cls,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "LLMRequest":
        """最常用的单轮问答构造器。"""
        return cls(
            messages=[Message.system(system), Message.user(user)],
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata=metadata or {},
        )

    @property
    def prompt_chars(self) -> int:
        return sum(len(m.content) for m in self.messages)


class LLMResponse(Base):
    """统一的模型返回。"""

    text: str = ""
    parsed: dict[str, Any] | list[Any] | None = Field(default=None, description="JSON 解析结果（json_mode 时）")
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    latency_ms: int = 0
    attempts: int = 1
    raw: dict[str, Any] | None = Field(default=None, description="原始响应，调试用，默认不落库")
    warnings: list[str] = Field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def extract_json(text: str) -> dict[str, Any] | list[Any] | None:
    """从模型输出中稳健地提取 JSON。

    依次尝试：整体解析 → 去掉 ``` 围栏后解析 → 截取首个平衡括号片段解析。
    失败返回 ``None``，由调用方决定降级策略，而不是直接抛错。
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]

    for match in _FENCE_RE.findall(text):
        candidates.append(match.strip())

    stripped = text.strip()
    start = min(
        (i for i in (stripped.find("{"), stripped.find("[")) if i != -1),
        default=-1,
    )
    if start != -1:
        candidates.append(_balanced_slice(stripped[start:]))

    for candidate in candidates:
        if not candidate:
            continue
        for variant in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                parsed = json.loads(variant)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, (dict, list)):
                return parsed
    return None


def _balanced_slice(text: str) -> str:
    """截取从第一个括号开始、括号配平的最长片段。"""
    pairs = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_string = False
    escaped = False
    for idx, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
                if not stack:
                    return text[: idx + 1]
            else:
                return text
    return text


class BaseLLMProvider(abc.ABC):
    """所有模型服务适配器的基类。"""

    #: 该适配器对应的 provider type，供 registry 注册
    provider_type: str = "base"

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.name

    # ---------------------------------------------------------- 元信息

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def is_local(self) -> bool:
        return self.config.is_local

    @property
    def supports_json_mode(self) -> bool:
        return self.config.supports_json_mode

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "type": self.provider_type,
            "model": self.model,
            "base_url": self.config.base_url or "-",
            "local": self.is_local,
            "json_mode": self.supports_json_mode,
        }

    # ---------------------------------------------------------- 核心契约

    @abc.abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """执行一次补全。实现方负责把异常映射为 ``core.exceptions`` 中的类型。"""
        raise NotImplementedError

    def health_check(self) -> tuple[bool, str]:
        """连通性自检。默认实现发一条极短请求；子类可覆盖为更轻的探活。"""
        try:
            resp = self.complete(
                LLMRequest.single_turn("你是连通性检测助手。", "回复两个字：正常", max_tokens=16)
            )
        except Exception as exc:  # noqa: BLE001 - 自检需要把任何失败都转成可展示文本
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"连接正常（{resp.latency_ms} ms，模型 {resp.model}）"

    # ---------------------------------------------------------- 便捷方法

    def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return self.complete(
            LLMRequest.single_turn(
                system,
                user,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
                metadata=metadata,
            )
        )

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
        required: bool = True,
    ) -> LLMResponse:
        """要求返回 JSON 并完成解析。``required=True`` 时解析失败即抛错。"""
        resp = self.chat(
            system,
            user,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata=metadata,
        )
        resp.parsed = extract_json(resp.text)
        if resp.parsed is None and required:
            raise LLMResponseError(
                f"[{self.name}] 模型未返回可解析的 JSON。原始输出前 300 字：{resp.text[:300]!r}"
            )
        return resp

    # ---------------------------------------------------------- 请求参数合并

    def _resolve(self, request: LLMRequest) -> tuple[str, float, int]:
        model = request.model or self.config.model
        temperature = (
            request.temperature if request.temperature is not None else self.config.temperature
        )
        max_tokens = request.max_tokens if request.max_tokens is not None else self.config.max_tokens
        return model, temperature, max_tokens

    def _request_kwargs(self, request: LLMRequest) -> dict[str, Any]:
        """组装服务端额外参数（extra_body 透传）。"""
        kwargs: dict[str, Any] = {}
        if request.json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "risk_output", "schema": request.json_schema},
            }
        kwargs.update(self.config.extra_body or {})
        return kwargs

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)


__all__ = [
    "Role",
    "Message",
    "LLMRequest",
    "LLMResponse",
    "BaseLLMProvider",
    "extract_json",
]
