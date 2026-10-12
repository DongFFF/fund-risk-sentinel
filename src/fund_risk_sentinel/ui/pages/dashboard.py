"""概览页：系统状态与工作概览。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.enums import EVENT_TYPE_LABELS, REVIEW_STATUS_LABELS, EventType, ReviewStatus
from ..context import AppContext
from ..widgets.common import (
    COLORS,
    Card,
    DataTable,
    StatCard,
    hint_label,
    primary_button,
    section_title,
)


class DashboardPage(QWidget):
    """首页概览。"""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._build()

    # ---------------------------------------------------------- 构建

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        # ---------------- 标题行
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("系统概览")
        title.setProperty("role", "title")
        title_box.addWidget(title)
        title_box.addWidget(
            hint_label(
                "本系统输出的是可核验的风险线索，不作出合规与否的认定。"
                "所有线索须经人工复核后方可作为处置依据。"
            )
        )
        header.addLayout(title_box)
        header.addStretch(1)
        self.refresh_button = primary_button("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button, alignment=Qt.AlignTop)
        layout.addLayout(header)

        # ---------------- 指标卡
        cards = QGridLayout()
        cards.setSpacing(12)
        self.card_tasks = StatCard("审查任务", "0", "累计完成的任务数")
        self.card_materials = StatCard("材料份数", "0", "已解析入库的材料")
        self.card_findings = StatCard("风险线索", "0", "全部待核验线索")
        self.card_high = StatCard("高风险线索", "0", "建议办理前处理", color=COLORS["high"])
        self.card_pending = StatCard("待人工复核", "0", "尚未给出复核结论", color=COLORS["medium"])
        for index, card in enumerate(
            (self.card_tasks, self.card_materials, self.card_findings, self.card_high, self.card_pending)
        ):
            cards.addWidget(card, 0, index)
        layout.addLayout(cards)

        # ---------------- 最近任务
        recent_card = Card()
        recent_card.body().addWidget(section_title("最近审查任务"))
        self.recent_table = DataTable(
            ["时间", "任务名称", "事项类型", "材料", "线索", "高风险", "总体等级", "状态"]
        )
        self.recent_table.setMinimumHeight(180)
        self.recent_table.doubleClicked.connect(self._open_selected_task)
        recent_card.body().addWidget(self.recent_table)
        recent_card.body().addWidget(
            hint_label("双击一行可跳转到风险审查页查看该任务的结论明细。")
        )
        layout.addWidget(recent_card)

        # ---------------- 系统状态
        status_card = Card()
        status_card.body().addWidget(section_title("系统状态"))
        self.status_layout = QGridLayout()
        self.status_layout.setHorizontalSpacing(24)
        self.status_layout.setVerticalSpacing(8)
        status_card.body().addLayout(self.status_layout)
        layout.addWidget(status_card)

        layout.addStretch(1)

    # ---------------------------------------------------------- 刷新

    def refresh(self) -> None:
        """重新读取统计并刷新界面。"""
        try:
            stats = self.context.repository.dashboard_stats()
        except Exception as exc:  # noqa: BLE001 - 数据库异常要在界面上说清楚
            self.card_tasks.set_value("错误", str(exc)[:60], COLORS["high"])
            return

        counts = stats.get("counts", {})
        by_level = stats.get("findings_by_level", {})
        by_verdict = stats.get("findings_by_verdict", {})

        self.card_tasks.set_value(str(counts.get("tasks", 0)))
        self.card_materials.set_value(str(counts.get("materials", 0)))
        self.card_findings.set_value(str(counts.get("findings", 0)))
        self.card_high.set_value(str(by_level.get("high", 0)), color=COLORS["high"])
        self.card_pending.set_value(
            str(by_verdict.get("pending", 0)), color=COLORS["medium"]
        )

        rows = []
        for task in stats.get("recent_tasks", []):
            event = EventType.from_value(task.get("event_type"), EventType.OTHER)
            status = ReviewStatus.from_value(task.get("status"), ReviewStatus.PENDING)
            rows.append(
                [
                    (task.get("created_at") or "")[:19].replace("T", " "),
                    task.get("title", ""),
                    EVENT_TYPE_LABELS.get(event, event.value),
                    task.get("material_count", 0),
                    task.get("finding_count", 0),
                    task.get("high_count", 0),
                    {"high": "高", "medium": "中", "low": "低", "unknown": "待核验"}.get(
                        task.get("overall_level", "unknown"), "待核验"
                    ),
                    REVIEW_STATUS_LABELS.get(status, status.value),
                ]
            )
        self.recent_table.set_rows(rows)
        self._refresh_status(stats)

    def _refresh_status(self, stats: dict) -> None:
        while self.status_layout.count():
            item = self.status_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        audit_ok, audit_message = self.context.audit.verify_chain()
        config_problems = self.context.config.validate()
        provider = self.context.config.provider()
        engine_stats = self.context.rule_engine.describe()

        rows = [
            ("当前模型", f"{provider.name} / {provider.model or '-'}", COLORS["text"]),
            (
                "模型协议",
                f"{provider.type}" + ("（本地部署，材料不出本机）" if provider.is_local else "（云端服务）"),
                COLORS["text"],
            ),
            (
                "规则库",
                f"{engine_stats['enabled']} 条启用 / 共 {engine_stats['total']} 条",
                COLORS["text"],
            ),
            ("制度知识库", f"{len(self.context.knowledge_base)} 条条款", COLORS["text"]),
            (
                "审计日志链",
                audit_message,
                COLORS["ok"] if audit_ok else COLORS["high"],
            ),
            (
                "配置检查",
                "正常" if not config_problems else f"{len(config_problems)} 项待处理：{config_problems[0]}",
                COLORS["ok"] if not config_problems else COLORS["warn"],
            ),
            ("数据库", str(self.context.config.settings.resolved_db_path()), COLORS["text_muted"]),
            ("数据目录", str(self.context.config.settings.resolved_data_dir()), COLORS["text_muted"]),
            ("审计记录数", str(stats.get("counts", {}).get("audit_log", 0)), COLORS["text"]),
        ]

        for index, (label, value, color) in enumerate(rows):
            name = QLabel(label)
            name.setProperty("role", "hint")
            content = QLabel(value)
            content.setStyleSheet(f"color: {color};")
            content.setWordWrap(True)
            self.status_layout.addWidget(name, index, 0, alignment=Qt.AlignTop)
            self.status_layout.addWidget(content, index, 1)
        self.status_layout.setColumnStretch(1, 1)

    # ---------------------------------------------------------- 交互

    def _open_selected_task(self) -> None:
        row = self.recent_table.selected_row()
        if row < 0:
            return
        tasks = self.context.repository.list_tasks(limit=5)
        if row >= len(tasks):
            return
        task_id = tasks[row]["task_id"]
        window = self.window()
        if hasattr(window, "open_task"):
            window.open_task(task_id)
