"""Anthropic Claude 适配器。

与 OpenAI 协议的差异：
- 端点 ``POST {base_url}/v1/messages``，默认基址 ``https://api.anthropic.com``
- 认证头 ``x-api-key`` + ``anthropic-version``（不是 Bearer）
- ``system`` 必须作为顶层参数，不能出现在 ``messages`` 里
- 返回体是 ``content: [{type: "text", text: ...}]`` 列表
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

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(BaseLLMProvider):
    """Claude 系列适配器。"""

    provider_type = "anthropic"

    def _endpoint(self) -> str:
        base = (self.config.base_url or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key:
            raise LLMUnavailableError(f"[{self.name}] 未配置 api_key")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        headers.update(self.config.extra_headers or {})
        return headers

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        model, temperature, max_tokens = self._resolve(request)
        system_parts = [m.content for m in request.messages if m.role == "system"]
        convo = [
            {"role": m.role, "content": m.content}
            for m in request.messages
            if m.role in ("user", "assistant")
        ]
        if not convo:
            convo = [{"role": "user", "content": "(空)"}]

        payload: dict[str, Any] = {
            "model": model,
            "messages": convo,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.stop:
            payload["stop_sequences"] = request.stop
        if request.json_mode:
            # Claude 没有 response_format，用"预填充助手回合"逼出纯 JSON
            convo.append({"role": "assistant", "content": "{"})
        payload.update(self.config.extra_body or {})
        return payload

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
                    err = self._map_http_error(resp)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if attempt < attempts:
                            last_error = err
                            time.sleep(min(2.0 ** (attempt - 1), 8.0))
                            continue
                    raise err

                data = resp.json()
                text = self._extract_text(data)
                if request.json_mode and not text.lstrip().startswith("{"):
                    # 预填充的 "{" 会被服务端回显在续写内容里，这里补齐
                    text = "{" + text
                usage = data.get("usage") or {}
                return LLMResponse(
                    text=text,
                    provider=self.name,
                    model=data.get("model") or self.config.model,
                    prompt_tokens=int(usage.get("input_tokens") or 0),
                    completion_tokens=int(usage.get("output_tokens") or 0),
                    finish_reason=str(data.get("stop_reason") or ""),
                    latency_ms=elapsed,
                    attempts=attempt,
                    raw=data,
                )
            except (LLMAuthError, LLMResponseError):
                raise
            except httpx.TimeoutException as exc:
                last_error = LLMConnectionError(f"[{self.name}] 请求超时（{self.config.timeout}s）")
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = LLMConnectionError(f"[{self.name}] 网络错误：{exc}")
                last_error.__cause__ = exc
            if attempt < attempts:
                time.sleep(min(2.0 ** (attempt - 1), 8.0))

        raise last_error or LLMResponseError(f"[{self.name}] 调用失败")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        blocks = data.get("content")
        if isinstance(blocks, list):
            texts = [
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "".join(texts)
            if joined:
                return joined
        raise LLMResponseError(
            f"响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}"
        )

    def _map_http_error(self, resp: httpx.Response) -> Exception:
        code = resp.status_code
        detail = resp.text[:300]
        try:
            body = resp.json()
            if isinstance(body, dict):
                err = body.get("error")
                detail = str(err.get("message") if isinstance(err, dict) else err or detail)[:300]
        except Exception:  # noqa: BLE001
            pass
        if code in (401, 403):
            return LLMAuthError(f"[{self.name}] 认证失败（HTTP {code}）：{detail}")
        if code == 429:
            return LLMRateLimitError(f"[{self.name}] 触发限流：{detail}")
        return LLMResponseError(f"[{self.name}] 请求失败（HTTP {code}）：{detail}")


__all__ = ["AnthropicProvider", "ANTHROPIC_VERSION"]
