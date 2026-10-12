"""界面运行时上下文。

界面各页面共享同一份配置、模型客户端、流水线与存储句柄。集中在这里的好处是：
"设置页改了 provider"能立即反映到"审查页"，不需要重启程序，也不需要各页面
各自去读配置文件。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config.settings import ConfigManager, get_manager
from ..core.models import Material, ReviewConclusion, ReviewTask
from ..llm.registry import LLMClient, get_client
from ..review.pipeline import PipelineOptions, ReviewPipeline
from ..rules.engine import RuleEngine
from ..rules.knowledge_base import KnowledgeBase, load_knowledge_base
from ..rules.loader import load_all_rules
from ..storage.audit import AuditLogger
from ..storage.repository import Repository
from ..storage import open_storage

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """全局运行时状态。"""

    config: ConfigManager
    client: LLMClient
    repository: Repository
    audit: AuditLogger
    pipeline: ReviewPipeline

    #: 当前待审查的材料（上传页写入，审查页读取）
    pending_materials: list[Material] = field(default_factory=list)
    pending_title: str = ""
    #: 最近一次结论
    last_conclusion: ReviewConclusion | None = None
    last_task: ReviewTask | None = None

    # ---------------------------------------------------------- 构造

    @classmethod
    def create(cls, config: ConfigManager | None = None) -> "AppContext":
        cfg = config or get_manager()
        cfg.settings.ensure_dirs()
        client = get_client() if config is None else LLMClient(cfg)
        repository, audit = open_storage(cfg)
        pipeline = ReviewPipeline.from_config(cfg)
        pipeline.client = client
        pipeline.extractor.client = client
        pipeline.reasoner.client = client
        pipeline.reporter.client = client
        return cls(
            config=cfg,
            client=client,
            repository=repository,
            audit=audit,
            pipeline=pipeline,
        )

    # ---------------------------------------------------------- 热更新

    def reload_pipeline(self) -> list[str]:
        """重新加载规则库与知识库，返回提示信息。"""
        messages: list[str] = []
        engine = RuleEngine()
        try:
            rules = load_all_rules(self.config.settings.resolved_rules_dir())
            engine.add_rules(rules)
            messages.append(f"已加载规则 {len(rules)} 条")
            problems = engine.validate()
            if problems:
                messages.append(f"发现 {len(problems)} 条规则存在问题，已跳过")
                for problem in problems[:5]:
                    logger.warning("规则问题：%s", problem)
        except Exception as exc:  # noqa: BLE001 - 规则加载失败不应让界面崩掉
            messages.append(f"规则库加载失败：{exc}")
            logger.exception("规则库加载失败")
        self.pipeline.rule_engine = engine

        try:
            kb = load_knowledge_base(self.config.settings.resolved_knowledge_dir())
            self.pipeline.knowledge_base = kb
            messages.append(f"已加载制度条款 {len(kb)} 条")
        except Exception as exc:  # noqa: BLE001
            messages.append(f"知识库加载失败：{exc}")
            logger.exception("知识库加载失败")
        return messages

    def reload_client(self) -> None:
        """配置变更后重建模型客户端，并让流水线各阶段指向新客户端。"""
        self.client = LLMClient(self.config)
        self.pipeline.client = self.client
        self.pipeline.extractor.client = self.client
        self.pipeline.reasoner.client = self.client
        self.pipeline.reporter.client = self.client

    def reload_repository(self) -> None:
        """数据库路径变更后重开存储。"""
        self.repository, self.audit = open_storage(self.config)

    def reload_all(self) -> list[str]:
        self.config = get_manager(reload=True)
        self.reload_client()
        self.reload_repository()
        return self.reload_pipeline()

    # ---------------------------------------------------------- 便捷属性

    @property
    def rule_engine(self) -> RuleEngine:
        return self.pipeline.rule_engine

    @property
    def knowledge_base(self) -> KnowledgeBase:
        return self.pipeline.knowledge_base

    def default_options(self, event_type=None) -> PipelineOptions:
        from ..core.enums import EventType

        return PipelineOptions(
            event_type=event_type or EventType.OTHER,
            provider=self.config.active_provider_name,
        )


__all__ = ["AppContext"]
