"""Google Gemini 适配器。

差异点：
- 端点 ``POST {base}/v1beta/models/{model}:generateContent``，Key 走查询参数或 ``x-goog-api-key`` 头
- ``system`` 走顶层 ``systemInstruction``
- 内容结构为 ``contents: [{role, parts: [{text}]}]``
- 强制 JSON 用 ``generationConfig.responseMimeType = application/json``
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

DEFAULT_BASE = "https://generativelanguage.googleapis.com"


class GeminiProvider(BaseLLMProvider):
    """Google Gemini 适配器。"""

    provider_type = "gemini"

    def _endpoint(self) -> str:
        if not self.config.api_key:
            raise LLMUnavailableError(f"[{self.name}] 未配置 api_key")
        if not self.config.model:
            raise LLMUnavailableError(f"[{self.name}] 未配置 model（如 gemini-1.5-pro）")
        base = (self.config.base_url or DEFAULT_BASE).rstrip("/")
        return f"{base}/v1beta/models/{self.config.model}:generateContent"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.config.api_key,
        }
        headers.update(self.config.extra_headers or {})
        return headers

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        _model, temperature, max_tokens = self._resolve(request)
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in request.messages
            if m.role in ("user", "assistant")
        ]
        system_text = "\n\n".join(m.content for m in request.messages if m.role == "system")

        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if request.json_mode:
            generation_config["responseMimeType"] = "application/json"
        if request.stop:
            generation_config["stopSequences"] = request.stop

        payload: dict[str, Any] = {
            "contents": contents or [{"role": "user", "parts": [{"text": "(空)"}]}],
            "generationConfig": generation_config,
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
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
                    if (resp.status_code == 429 or resp.status_code >= 500) and attempt < attempts:
                        last_error = err
                        time.sleep(min(2.0 ** (attempt - 1), 8.0))
                        continue
                    raise err

                data = resp.json()
                text = self._extract_text(data)
                usage = data.get("usageMetadata") or {}
                return LLMResponse(
                    text=text,
                    provider=self.name,
                    model=self.config.model,
                    prompt_tokens=int(usage.get("promptTokenCount") or 0),
                    completion_tokens=int(usage.get("candidatesTokenCount") or 0),
                    finish_reason=str(
                        ((data.get("candidates") or [{}])[0]).get("finishReason") or ""
                    ),
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
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if text:
                return text
            finish = candidates[0].get("finishReason")
            if finish == "SAFETY":
                raise LLMResponseError("模型因安全策略拒绝回答（finishReason=SAFETY）")
        raise LLMResponseError(f"响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}")

    def _map_http_error(self, resp: httpx.Response) -> Exception:
        code = resp.status_code
        detail = resp.text[:300]
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                detail = str(body["error"].get("message") or detail)[:300]
        except Exception:  # noqa: BLE001
            pass
        if code in (401, 403):
            return LLMAuthError(f"[{self.name}] 认证失败（HTTP {code}）：{detail}")
        if code == 429:
            return LLMRateLimitError(f"[{self.name}] 触发限流：{detail}")
        return LLMResponseError(f"[{self.name}] 请求失败（HTTP {code}）：{detail}")


__all__ = ["GeminiProvider", "DEFAULT_BASE"]
