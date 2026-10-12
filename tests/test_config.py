"""配置层：模型配置校验、路径解析、环境变量插值。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fund_risk_sentinel.config.settings import (
    AppSettings,
    ConfigManager,
    ProviderConfig,
    ProvidersConfig,
    interpolate_env,
)


class TestProviderConfig:
    def test_mock_is_always_local(self) -> None:
        """回归：漏写 is_local 时，离线 mock 不得被标成「云端」。

        这是安全提示——把不联网的模型标成云端会让人误判材料是否外发。
        """
        cfg = ProviderConfig(name="mock", type="mock", model="mock-rules-v1")
        assert cfg.is_local is True

    def test_mock_local_even_if_explicitly_false(self) -> None:
        cfg = ProviderConfig(name="mock", type="mock", model="m", is_local=False)
        assert cfg.is_local is True

    def test_cloud_provider_defaults_to_not_local(self) -> None:
        cfg = ProviderConfig(
            name="deepseek",
            type="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            api_key="x",
            model="deepseek-chat",
        )
        assert cfg.is_local is False

    def test_local_flag_respected_for_ollama(self) -> None:
        cfg = ProviderConfig(
            name="ollama",
            type="openai_compatible",
            base_url="http://127.0.0.1:11434/v1",
            api_key="EMPTY",
            model="qwen2.5:7b",
            is_local=True,
        )
        assert cfg.is_local is True

    def test_ready_requires_model_and_key(self) -> None:
        incomplete = ProviderConfig(name="x", type="openai_compatible", base_url="http://a/v1")
        assert incomplete.ready is False
        assert ProviderConfig(name="mock", type="mock", model="m").ready is True


class TestRouteChain:
    def _config(self, *, allow_cloud: bool = True) -> ConfigManager:
        settings = AppSettings()
        settings.security.allow_cloud_providers = allow_cloud
        return ConfigManager(
            settings,
            ProvidersConfig(
                active="cloud",
                fallback=["ollama"],
                providers={
                    "cloud": ProviderConfig(
                        name="cloud",
                        type="openai_compatible",
                        base_url="https://api.example.com/v1",
                        api_key="k",
                        model="m",
                    ),
                    "ollama": ProviderConfig(
                        name="ollama",
                        type="openai_compatible",
                        base_url="http://127.0.0.1:11434/v1",
                        api_key="EMPTY",
                        model="qwen2.5:7b",
                        is_local=True,
                    ),
                },
            ),
        )

    def test_chain_follows_active_then_fallback(self) -> None:
        chain = self._config().route_chain()
        assert [p.name for p in chain] == ["cloud", "ollama"]

    def test_cloud_forbidden_falls_back_to_local(self) -> None:
        """安全策略禁止云端时，只保留本地 provider。"""
        chain = self._config(allow_cloud=False).route_chain()
        assert [p.name for p in chain] == ["ollama"]

    def test_cloud_forbidden_without_local_raises(self) -> None:
        from fund_risk_sentinel.core.exceptions import ConfigError

        config = ConfigManager(
            AppSettings(),
            ProvidersConfig(
                active="cloud",
                providers={
                    "cloud": ProviderConfig(
                        name="cloud",
                        type="openai_compatible",
                        base_url="https://api.example.com/v1",
                        api_key="k",
                        model="m",
                    )
                },
            ),
        )
        config.settings.security.allow_cloud_providers = False
        with pytest.raises(ConfigError):
            config.route_chain()


class TestInterpolateEnv:
    def test_plain_substitution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FRS_TEST_KEY", "secret")
        assert interpolate_env("${FRS_TEST_KEY}") == "secret"

    def test_default_value_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FRS_MISSING_KEY", raising=False)
        assert interpolate_env("${FRS_MISSING_KEY:-fallback}") == "fallback"

    def test_nested_structure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FRS_NESTED", "v")
        data = {"providers": {"a": {"api_key": "${FRS_NESTED}"}}}
        assert interpolate_env(data)["providers"]["a"]["api_key"] == "v"


class TestAppSettingsPaths:
    def test_default_paths_are_absolute(self) -> None:
        settings = AppSettings()
        assert settings.resolved_data_dir().is_absolute()
        assert settings.resolved_db_path().is_absolute()

    def test_explicit_paths_are_honored(self, tmp_path: Path) -> None:
        settings = AppSettings(data_dir=str(tmp_path), db_path=str(tmp_path / "x.db"))
        assert settings.resolved_db_path() == tmp_path / "x.db"

    def test_ensure_dirs_creates_directories(self, tmp_path: Path) -> None:
        settings = AppSettings(
            data_dir=str(tmp_path / "d"),
            log_dir=str(tmp_path / "l"),
            export_dir=str(tmp_path / "e"),
        )
        settings.ensure_dirs()
        assert (tmp_path / "d").is_dir()
        assert (tmp_path / "l").is_dir()
        assert (tmp_path / "e").is_dir()
