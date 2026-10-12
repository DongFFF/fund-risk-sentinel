"""模型接入层：JSON 容错解析、mock 提供方、回退链与用量统计。"""

from __future__ import annotations

import pytest

from fund_risk_sentinel.config.settings import (
    AppSettings,
    ConfigManager,
    ProviderConfig,
    ProvidersConfig,
)
from fund_risk_sentinel.core.exceptions import ProviderNotConfiguredError
from fund_risk_sentinel.llm.base import LLMRequest, extract_json
from fund_risk_sentinel.llm.mock import MockProvider
from fund_risk_sentinel.llm.registry import LLMClient, ProviderRegistry


class TestExtractJson:
    def test_plain_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_strips_code_fence(self) -> None:
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_extracts_from_surrounding_prose(self) -> None:
        raw = '好的，结果如下：{"a": 1, "b": [2, 3]} 以上。'
        assert extract_json(raw) == {"a": 1, "b": [2, 3]}

    def test_removes_trailing_commas(self) -> None:
        assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_nested_braces(self) -> None:
        raw = '{"outer": {"inner": {"x": 1}}}'
        assert extract_json(raw)["outer"]["inner"]["x"] == 1

    def test_unparseable_returns_none(self) -> None:
        assert extract_json("完全不是 JSON") is None


class TestMockProvider:
    def test_health_check_ok(self) -> None:
        provider = MockProvider(ProviderConfig(name="mock", type="mock", model="mock-rules-v1"))
        ok, message = provider.health_check()
        assert ok, message

    def test_extract_task_returns_fields(self) -> None:
        provider = MockProvider(ProviderConfig(name="mock", type="mock"))
        request = LLMRequest.single_turn(
            "抽取",
            "项目名称：测试项目\n金额：1,000.00\n报销人：张伟",
            metadata={"task": "extract"},
        )
        payload = provider.complete(request).parsed
        assert isinstance(payload, dict)
        assert "fields" in payload

    def test_explain_counts_from_statistics_line(self) -> None:
        """回归：摘要条数必须取自【线索统计】行，而非数关键词。"""
        provider = MockProvider(ProviderConfig(name="mock", type="mock"))
        prompt = (
            "【线索统计】\n总数 3；按等级 high:1，medium:2；按类别 missing:3\n\n"
            "【待解释的风险线索】\n[线索 1] A\n[线索 2] B\n[线索 3] C"
        )
        request = LLMRequest.single_turn("解释", prompt, metadata={"task": "explain"})
        payload = provider.complete(request).parsed
        assert "3 条待核验线索" in payload["summary"]
        assert "高 1" in payload["summary"]

    def test_reason_task_returns_findings_list(self) -> None:
        provider = MockProvider(ProviderConfig(name="mock", type="mock"))
        request = LLMRequest.single_turn(
            "推理",
            "合同金额：8,660.00\n报销金额：9,560.00\n预算余额：3,800.00",
            metadata={"task": "reason"},
        )
        payload = provider.complete(request).parsed
        assert isinstance(payload.get("findings"), list)


class TestProviderRegistry:
    def test_known_types(self) -> None:
        registry = ProviderRegistry()
        for ptype in ("mock", "openai_compatible", "anthropic", "gemini"):
            assert registry.create(ProviderConfig(name="p", type=ptype, model="m"))

    def test_custom_type_falls_back_to_openai_compatible(self) -> None:
        """未注册自有适配器时，"custom" 走 OpenAI 兼容——覆盖面最广的兜底。"""
        from fund_risk_sentinel.llm.openai_compatible import OpenAICompatibleProvider

        registry = ProviderRegistry()
        provider = registry.create(ProviderConfig(name="p", type="custom", model="m"))
        assert isinstance(provider, OpenAICompatibleProvider)


class TestLLMClient:
    def _config(self, active: str = "mock", fallback: list[str] | None = None) -> ConfigManager:
        return ConfigManager(
            AppSettings(),
            ProvidersConfig(
                active=active,
                fallback=fallback or [],
                providers={
                    "mock": ProviderConfig(name="mock", type="mock", model="mock-rules-v1"),
                    "broken": ProviderConfig(
                        name="broken",
                        type="openai_compatible",
                        base_url="http://127.0.0.1:1/v1",
                        api_key="x",
                        model="nope",
                        timeout=1,
                    ),
                },
            ),
        )

    def test_route_chain_includes_fallback(self) -> None:
        client = LLMClient(self._config("broken", ["mock"]))
        chain = client.config.route_chain()
        assert [p.name for p in chain] == ["broken", "mock"]

    def test_unknown_active_provider_raises(self) -> None:
        client = LLMClient(self._config("不存在"))
        with pytest.raises(ProviderNotConfiguredError):
            client.provider()

    def test_mock_call_records_usage(self) -> None:
        client = LLMClient(self._config())
        response = client.chat("系统", "用户", metadata={"task": "extract"})
        assert response.text
        summary = client.usage_summary()
        assert summary["calls"] >= 1

    def test_call_log_records_provider(self) -> None:
        client = LLMClient(self._config())
        client.chat("s", "u", metadata={"task": "reason"})
        assert client.call_log
        assert client.call_log[-1].provider
