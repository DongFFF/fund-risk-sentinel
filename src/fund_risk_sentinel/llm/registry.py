"""provider 注册表与统一调用客户端。

业务层只需要 ``LLMClient.chat_json(...)``；至于背后走 DeepSeek、Claude 还是本地
Ollama，由配置决定。``LLMClient`` 同时负责：

- **回退链**：主 provider 失败时依次尝试 ``fallback``，并在返回结果上标注降级。
- **用量核算**：累计 token 与调用次数，供界面展示与成本估算。
- **审计留痕**：记录每次调用的 provider / model / 耗时 / 是否降级，写入审计日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config.settings import ConfigManager, ProviderConfig, get_manager
from ..core.exceptions import (
    LLMError,
    LLMUnavailableError,
    ProviderNotConfiguredError,
)
from ..core.models import TokenUsage
from .anthropic import AnthropicProvider
from .base import BaseLLMProvider, LLMRequest, LLMResponse
from .gemini import GeminiProvider
from .mock import MockProvider
from .openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


# ================================================================ 注册表

class ProviderRegistry:
    """provider 类型 → 适配器类 的映射。"""

    _types: dict[str, type[BaseLLMProvider]] = {}

    @classmethod
    def register(cls, provider_type: str, adapter: type[BaseLLMProvider]) -> None:
        cls._types[provider_type] = adapter

    @classmethod
    def registered_types(cls) -> list[str]:
        return sorted(cls._types)

    @classmethod
    def create(cls, config: ProviderConfig) -> BaseLLMProvider:
        adapter = cls._types.get(config.type)
        if adapter is None:
            # 未知类型一律按 OpenAI 兼容处理——这是覆盖面最广的兜底
            logger.warning(
                "provider %s 的类型 %r 未注册，回退为 openai_compatible 适配器",
                config.name,
                config.type,
            )
            adapter = OpenAICompatibleProvider
        return adapter(config)


ProviderRegistry.register("openai_compatible", OpenAICompatibleProvider)
ProviderRegistry.register("anthropic", AnthropicProvider)
ProviderRegistry.register("gemini", GeminiProvider)
ProviderRegistry.register("mock", MockProvider)
# custom 交给用户自行继承 BaseLLMProvider 后注册；未注册时按 OpenAI 兼容处理
ProviderRegistry.register("custom", OpenAICompatibleProvider)


# ================================================================ 调用记录

@dataclass
class CallRecord:
    """单次调用的审计记录。"""

    provider: str
    model: str
    task: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    ok: bool
    degraded: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "task": self.task,
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "ok": self.ok,
            "degraded": self.degraded,
            "error": self.error,
            "metadata": self.metadata,
        }


# ================================================================ 客户端

class LLMClient:
    """带回退链与用量统计的统一调用入口。"""

    def __init__(self, config: ConfigManager | None = None) -> None:
        self.config = config or get_manager()
        self._cache: dict[str, BaseLLMProvider] = {}
        self.usage = TokenUsage()
        self.call_log: list[CallRecord] = []
        self.last_degraded: bool = False
        self.last_error: str = ""

    # ---------------------------------------------------------- provider

    def provider(self, name: str | None = None) -> BaseLLMProvider:
        """取（并缓存）provider 实例。"""
        key = name or self.config.active_provider_name
        if key not in self._cache:
            cfg = self.config.provider(key)
            self._cache[key] = ProviderRegistry.create(cfg)
        return self._cache[key]

    def refresh(self) -> None:
        """配置变更后清空缓存。"""
        self._cache.clear()

    def available_providers(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, cfg in self.config.providers.providers.items():
            item = {
                "name": name,
                "enabled": cfg.enabled,
                "type": cfg.type,
                "model": cfg.model,
                "local": cfg.is_local,
                "ready": cfg.ready,
                "description": cfg.description,
                "active": name == self.config.active_provider_name,
            }
            out.append(item)
        return out

    # ---------------------------------------------------------- 调用

    def complete(
        self,
        request: LLMRequest,
        *,
        provider: str | None = None,
        allow_fallback: bool = True,
    ) -> LLMResponse:
        """执行调用。``allow_fallback`` 时按回退链逐个尝试，全部失败才抛错。"""
        chain = self._chain(provider, allow_fallback)
        errors: list[str] = []
        self.last_degraded = False

        for index, cfg in enumerate(chain):
            try:
                adapter = self._adapter_for(cfg)
                response = adapter.complete(request)
            except LLMError as exc:
                errors.append(f"{cfg.name}: {type(exc).__name__}: {exc}")
                logger.warning("provider %s 调用失败：%s", cfg.name, exc)
                self.call_log.append(
                    CallRecord(
                        provider=cfg.name,
                        model=cfg.model,
                        task=str(request.metadata.get("task", "")),
                        latency_ms=0,
                        prompt_tokens=0,
                        completion_tokens=0,
                        ok=False,
                        error=str(exc),
                        metadata=request.metadata,
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 未预期异常也纳入回退，避免整条流水线崩掉
                errors.append(f"{cfg.name}: {type(exc).__name__}: {exc}")
                logger.exception("provider %s 抛出未预期异常", cfg.name)
                continue

            degraded = index > 0
            self.last_degraded = degraded
            if degraded:
                response.warnings.append(
                    f"主模型不可用，已回退到 {cfg.name}（原因：{' | '.join(errors)}）"
                )
            self.usage.prompt_tokens += response.prompt_tokens
            self.usage.completion_tokens += response.completion_tokens
            self.usage.total_calls += 1
            self.call_log.append(
                CallRecord(
                    provider=cfg.name,
                    model=response.model,
                    task=str(request.metadata.get("task", "")),
                    latency_ms=response.latency_ms,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    ok=True,
                    degraded=degraded,
                    metadata=request.metadata,
                )
            )
            return response

        self.last_error = " | ".join(errors) or "无可用模型"
        raise LLMUnavailableError(f"所有模型均调用失败：{self.last_error}")

    def chat(
        self,
        system: str,
        user: str,
        *,
        provider: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
        allow_fallback: bool = True,
    ) -> LLMResponse:
        return self.complete(
            LLMRequest.single_turn(
                system,
                user,
                json_mode=json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
                metadata=metadata,
            ),
            provider=provider,
            allow_fallback=allow_fallback,
        )

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        provider: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
        required: bool = True,
        allow_fallback: bool = True,
    ) -> LLMResponse:
        """要求 JSON 输出并完成解析。"""
        from .base import extract_json

        resp = self.chat(
            system,
            user,
            provider=provider,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata=metadata,
            allow_fallback=allow_fallback,
        )
        resp.parsed = extract_json(resp.text)
        if resp.parsed is None and required:
            from ..core.exceptions import LLMResponseError

            raise LLMResponseError(
                f"[{resp.provider}] 未返回可解析 JSON。原始输出前 300 字：{resp.text[:300]!r}"
            )
        return resp

    # ---------------------------------------------------------- 自检

    def health_check(self, name: str | None = None) -> dict[str, Any]:
        """对指定（或全部启用的）provider 做连通性自检。"""
        targets = [name] if name else self.config.providers.enabled_names()
        results: dict[str, Any] = {}
        for key in targets:
            try:
                adapter = self._adapter_for(self.config.provider(key))
            except ProviderNotConfiguredError as exc:
                results[key] = {"ok": False, "message": str(exc)}
                continue
            ok, message = adapter.health_check()
            results[key] = {"ok": ok, "message": message, "model": adapter.model}
        return results

    # ---------------------------------------------------------- 内部

    def _adapter_for(self, cfg: ProviderConfig) -> BaseLLMProvider:
        if cfg.name not in self._cache:
            self._cache[cfg.name] = ProviderRegistry.create(cfg)
        return self._cache[cfg.name]

    def _chain(self, provider: str | None, allow_fallback: bool) -> list[ProviderConfig]:
        if allow_fallback:
            return self.config.route_chain(provider)
        return [self.config.provider(provider)]

    # ---------------------------------------------------------- 汇总

    def usage_summary(self) -> dict[str, Any]:
        prompt_cost = 0.0
        completion_cost = 0.0
        for record in self.call_log:
            if not record.ok:
                continue
            cfg = self.config.providers.providers.get(record.provider)
            if cfg:
                prompt_cost += record.prompt_tokens / 1000 * cfg.cost_per_1k_prompt
                completion_cost += record.completion_tokens / 1000 * cfg.cost_per_1k_completion
        return {
            "calls": self.usage.total_calls,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
            "estimated_cost": round(prompt_cost + completion_cost, 4),
            "degraded_calls": sum(1 for r in self.call_log if r.degraded),
            "failed_calls": sum(1 for r in self.call_log if not r.ok),
        }


# ================================================================ 单例

_CLIENT: LLMClient | None = None


def get_client(reload: bool = False) -> LLMClient:
    """获取全局 LLM 客户端。"""
    global _CLIENT
    if _CLIENT is None or reload:
        _CLIENT = LLMClient()
    return _CLIENT


def set_client(client: LLMClient) -> None:
    global _CLIENT
    _CLIENT = client


__all__ = [
    "ProviderRegistry",
    "CallRecord",
    "LLMClient",
    "get_client",
    "set_client",
]
