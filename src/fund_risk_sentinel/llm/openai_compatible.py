"""OpenAI 兼容协议适配器。

一家适配器覆盖绝大多数服务，因为国内主流厂商都提供了 OpenAI 兼容端点：

===============================================  ==========================================================
服务                                             base_url
===============================================  ==========================================================
DeepSeek                                        ``https://api.deepseek.com/v1``
通义千问（阿里云百炼兼容模式）                  ``https://dashscope.aliyuncs.com/compatible-mode/v1``
Kimi（月之暗面）                                ``https://api.moonshot.cn/v1``
智谱 GLM                                        ``https://open.bigmodel.cn/api/paas/v4``
OpenAI                                          ``https://api.openai.com/v1``
本地 Ollama                                     ``http://127.0.0.1:11434/v1``
本地 vLLM / LM Studio / 自建微调模型            ``http://127.0.0.1:8000/v1``
===============================================  ==========================================================

因此"程序能调用任何模型 API"在工程上就是：**只要它兼容这套协议，改一行 base_url 即可**。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..core.exceptions import (
    LLMAuthError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMResponseError,
    LLMUnavailableError,
)
from .base import BaseLLMProvider, LLMRequest, LLMResponse


class OpenAICompatibleProvider(BaseLLMProvider):
    """兼容 ``POST {base_url}/chat/completions`` 的服务。"""

    provider_type = "openai_compatible"

    def _endpoint(self) -> str:
        base = (self.config.base_url or "").rstrip("/")
        if not base:
            raise LLMUnavailableError(f"[{self.name}] 未配置 base_url，无法发起调用")
        # 允许用户直接填到 /chat/completions
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "fund-risk-sentinel/0.1",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers or {})
        return headers

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        model, temperature, max_tokens = self._resolve(request)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if request.json_mode and self.supports_json_mode and not request.json_schema:
            payload["response_format"] = {"type": "json_object"}
        if request.stop:
            payload["stop"] = request.stop
        payload.update(self._request_kwargs(request))
        return payload

    # ---------------------------------------------------------- 主流程

    def complete(self, request: LLMRequest) -> LLMResponse:
        url = self._endpoint()
        payload = self._payload(request)
        headers = self._headers()
        attempts = max(1, self.config.max_retries + 1)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                with httpx.Client(timeout=self.config.timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)
                elapsed = self._elapsed_ms(started)

                if resp.status_code >= 400:
                    last_error = self._map_http_error(resp)
                    # 认证/参数类错误重试无意义，直接抛出
                    if isinstance(last_error, (LLMAuthError, LLMResponseError)) and not _retryable(
                        resp.status_code
                    ):
                        raise last_error
                    if attempt < attempts:
                        time.sleep(_backoff(attempt))
                        continue
                    raise last_error

                data = resp.json()
                return self._parse_response(data, elapsed, attempt)

            except (LLMAuthError, LLMResponseError) as exc:
                if attempt >= attempts or not _is_retryable_exception(exc):
                    raise
                last_error = exc
            except httpx.TimeoutException as exc:
                last_error = LLMConnectionError(
                    f"[{self.name}] 请求超时（{self.config.timeout}s）：{url}"
                )
                last_error.__cause__ = exc
            except httpx.ConnectError as exc:
                hint = ""
                if self.is_local:
                    hint = "（本地模型服务似乎未启动，请确认 Ollama / vLLM 正在运行）"
                last_error = LLMConnectionError(f"[{self.name}] 无法连接到 {url}{hint}：{exc}")
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = LLMConnectionError(f"[{self.name}] 网络错误：{exc}")
                last_error.__cause__ = exc

            if attempt < attempts:
                time.sleep(_backoff(attempt))

        raise last_error or LLMResponseError(f"[{self.name}] 调用失败且无具体错误信息")

    # ---------------------------------------------------------- 响应解析

    def _parse_response(self, data: dict[str, Any], elapsed_ms: int, attempt: int) -> LLMResponse:
        try:
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = message.get("content") or ""
            if not text and message.get("reasoning_content"):
                # 部分推理模型把内容放在 reasoning_content 里
                text = message["reasoning_content"]
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                f"[{self.name}] 响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}"
            ) from exc

        if not text:
            raise LLMResponseError(
                f"[{self.name}] 模型返回空内容：{json.dumps(data, ensure_ascii=False)[:300]}"
            )

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=data.get("model") or self.config.model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or ""),
            latency_ms=elapsed_ms,
            attempts=attempt,
            raw=data,
        )

    def _map_http_error(self, resp: httpx.Response) -> Exception:
        code = resp.status_code
        detail = _extract_error_detail(resp)
        if code in (401, 403):
            return LLMAuthError(f"[{self.name}] 认证失败（HTTP {code}）：{detail}")
        if code == 404:
            return LLMResponseError(
                f"[{self.name}] 接口不存在（HTTP 404）：请检查 base_url 是否写对（应为 …/v1）。{detail}"
            )
        if code == 429:
            return LLMRateLimitError(f"[{self.name}] 触发限流（HTTP 429）：{detail}")
        if code >= 500:
            return LLMResponseError(f"[{self.name}] 服务端错误（HTTP {code}）：{detail}")
        return LLMResponseError(f"[{self.name}] 请求被拒绝（HTTP {code}）：{detail}")


def _extract_error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - 错误体可能不是 JSON
        return (resp.text or "")[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return json.dumps(data, ensure_ascii=False)[:300]


def _retryable(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _is_retryable_exception(exc: Exception) -> bool:
    return isinstance(exc, (LLMRateLimitError,))


def _backoff(attempt: int) -> float:
    """指数退避，上限 8 秒。"""
    return min(2.0 ** (attempt - 1), 8.0)


__all__ = ["OpenAICompatibleProvider"]
