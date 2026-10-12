"""pytest 公共夹具。

测试全部离线运行：使用内置 ``mock`` 模型与临时目录，不触碰用户真实数据目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fund_risk_sentinel.config.settings import (  # noqa: E402
    AppSettings,
    ConfigManager,
    ProviderConfig,
    ProvidersConfig,
    get_manager,
    set_manager,
)
from fund_risk_sentinel.llm.registry import LLMClient, set_client  # noqa: E402
from fund_risk_sentinel.review.pipeline import PipelineOptions, ReviewPipeline  # noqa: E402
from fund_risk_sentinel.storage import open_storage  # noqa: E402

EXAMPLES = ROOT / "examples"


@pytest.fixture()
def tmp_config(tmp_path: Path) -> ConfigManager:
    """指向临时目录的配置：数据库、日志、导出都写在 tmp_path 下。"""
    settings = AppSettings(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "sentinel.db"),
        log_dir=str(tmp_path / "logs"),
        export_dir=str(tmp_path / "exports"),
        rules_dir=str(ROOT / "config" / "rules"),
        knowledge_dir=str(ROOT / "config" / "knowledge"),
    )
    settings.ensure_dirs()
    providers = ProvidersConfig(
        active="mock",
        providers={"mock": ProviderConfig(name="mock", type="mock", model="mock-rules-v1")},
    )
    return ConfigManager(settings, providers)


@pytest.fixture()
def mock_client(tmp_config: ConfigManager) -> LLMClient:
    client = LLMClient(tmp_config)
    set_client(client)
    return client


@pytest.fixture()
def pipeline(tmp_config: ConfigManager, mock_client: LLMClient) -> ReviewPipeline:
    set_manager(tmp_config)
    pipe = ReviewPipeline.from_config(tmp_config)
    pipe.client = mock_client
    pipe.extractor.client = mock_client
    pipe.reasoner.client = mock_client
    pipe.reporter.client = mock_client
    return pipe


@pytest.fixture()
def storage(tmp_config: ConfigManager):
    repository, audit = open_storage(tmp_config)
    return repository, audit


@pytest.fixture()
def sample_paths() -> list[Path]:
    """examples/ 下的样例材料。"""
    return sorted(p for p in EXAMPLES.iterdir() if p.suffix.lower() in {".txt", ".csv", ".md"})


@pytest.fixture()
def default_options() -> PipelineOptions:
    from fund_risk_sentinel.core.enums import EventType

    return PipelineOptions(event_type=EventType.REIMBURSEMENT, provider="mock")
