"""规则库页：查看、筛选、启停、校验规则。"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...core.enums import (
    DISPOSITION_TYPE_LABELS,
    RISK_CATEGORY_LABELS,
    RISK_LEVEL_LABELS,
    RULE_DIMENSION_LABELS,
    RuleDimension,
)
from ...core.models import Rule
from ..context import AppContext
from ..widgets.common import (
    COLORS,
    Card,
    DataTable,
    hint_label,
    primary_button,
    section_title,
)

logger = logging.getLogger(__name__)


class RulesPage(QWidget):
    """规则库管理。"""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.rules: list[Rule] = []
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("规则库")
        title.setProperty("role", "title")
        title_box.addWidget(title)
        title_box.addWidget(
            hint_label(
                "规则库是纯文本 YAML，可自行维护并走版本评审，无需改代码或重新打包。"
                "校内细则放在 config/rules/institution/ 目录，同名规则会覆盖内置规则。"
            )
        )
        header.addLayout(title_box)
        header.addStretch(1)
        for text, handler in (
            ("重新加载", self.reload),
            ("打开规则目录", self.open_dir),
            ("校验全部规则", self.validate),
        ):
            button = primary_button(text) if text == "重新加载" else QPushButton(text)
            button.clicked.connect(handler)
            header.addWidget(button)
        layout.addLayout(header)

        # ---------------- 筛选
        filter_card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("维度："))
        self.dimension_combo = QComboBox()
        self.dimension_combo.addItem("全部", "")
        for dimension in RuleDimension:
            self.dimension_combo.addItem(RULE_DIMENSION_LABELS.get(dimension, dimension.value), dimension.value)
        self.dimension_combo.currentIndexChanged.connect(self._refresh_table)
        row.addWidget(self.dimension_combo)

        row.addWidget(QLabel("来源："))
        self.source_combo = QComboBox()
        self.source_combo.addItem("全部", "")
        self.source_combo.addItem("内置规则", "builtin")
        self.source_combo.addItem("校内细则", "institution")
        self.source_combo.addItem("用户自定义", "user")
        self.source_combo.currentIndexChanged.connect(self._refresh_table)
        row.addWidget(self.source_combo)

        row.addStretch(1)
        self.toggle_button = QPushButton("启用/停用选中")
        self.toggle_button.clicked.connect(self.toggle_selected)
        row.addWidget(self.toggle_button)
        self.stats_label = QLabel("—")
        self.stats_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        row.addWidget(self.stats_label)
        filter_card.body().addLayout(row)
        layout.addWidget(filter_card)

        # ---------------- 表格与详情
        splitter = QSplitter(Qt.Vertical)

        table_card = Card()
        self.table = DataTable(
            ["规则 ID", "规则名称", "维度", "风险类别", "等级", "建议处置", "来源", "状态"]
        )
        self.table.setMinimumHeight(260)
        self.table.itemSelectionChanged.connect(self._show_selected)
        table_card.body().addWidget(self.table)
        splitter.addWidget(table_card)

        detail_card = Card()
        detail_card.body().addWidget(section_title("规则详情"))
        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setStyleSheet(
            f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        detail_card.body().addWidget(self.detail_view)
        splitter.addWidget(detail_card)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, stretch=1)

    # ---------------------------------------------------------- 数据

    def reload(self) -> None:
        messages = self.context.reload_pipeline()
        self.rules = self.context.rule_engine.rules
        self._refresh_table()
        QMessageBox.information(self, "重新加载", "\n".join(messages) or "完成。")

    def refresh(self) -> None:
        self.rules = self.context.rule_engine.rules
        self._refresh_table()

    def _filtered(self) -> list[Rule]:
        dimension = self.dimension_combo.currentData() or ""
        source = self.source_combo.currentData() or ""
        result = []
        for rule in self.rules:
            if dimension and rule.dimension.value != dimension:
                continue
            if source and rule.source != source:
                continue
            result.append(rule)
        return result

    def _refresh_table(self) -> None:
        rules = self._filtered()
        rows = []
        for rule in rules:
            rows.append(
                [
                    rule.rule_id,
                    rule.name,
                    RULE_DIMENSION_LABELS.get(rule.dimension, rule.dimension.value),
                    RISK_CATEGORY_LABELS.get(rule.risk_category, rule.risk_category.value),
                    RISK_LEVEL_LABELS.get(rule.level_hint, rule.level_hint.value),
                    DISPOSITION_TYPE_LABELS.get(rule.disposition_hint, rule.disposition_hint.value),
                    {"builtin": "内置", "institution": "校内", "user": "用户"}.get(rule.source, rule.source),
                    "启用" if rule.enabled else "停用",
                ]
            )
        colors = {i: (COLORS["text"] if r.enabled else COLORS["text_muted"]) for i, r in enumerate(rules)}
        self.table.set_rows(rows, colors=colors)

        stats = self.context.rule_engine.describe()
        by_dimension = stats.get("by_dimension", {})
        self.stats_label.setText(
            f"共 {stats.get('total', 0)} 条，启用 {stats.get('enabled', 0)} 条　|　"
            + "　".join(
                f"{RULE_DIMENSION_LABELS.get(RuleDimension.from_value(k, RuleDimension.PROCEDURE), k)}"
                f" {v}"
                for k, v in by_dimension.items()
            )
        )

    def _show_selected(self) -> None:
        row = self.table.selected_row()
        rules = self._filtered()
        if row < 0 or row >= len(rules):
            return
        rule = rules[row]
        lines = [
            f"【规则 ID】{rule.rule_id}",
            f"【规则名称】{rule.name}",
            f"【维度】{RULE_DIMENSION_LABELS.get(rule.dimension, rule.dimension.value)}",
            f"【风险类别】{RISK_CATEGORY_LABELS.get(rule.risk_category, rule.risk_category.value)}",
            f"【初始等级】{RISK_LEVEL_LABELS.get(rule.level_hint, rule.level_hint.value)}",
            f"【建议处置】{DISPOSITION_TYPE_LABELS.get(rule.disposition_hint, rule.disposition_hint.value)}",
            f"【适用事项】{'全部' if not rule.event_types else '、'.join(e.value for e in rule.event_types)}",
            f"【来源】{rule.source}　【版本】{rule.version}　【权重】{rule.weight}",
            "",
            "【触发条件】",
            rule.condition or "（无，仅作知识提示）",
            "",
            "【制度要求】",
            rule.requirement or "（未填写）",
            "",
            "【建议复核点】",
            rule.suggestion or "（未填写）",
        ]
        if rule.basis:
            lines.extend(["", "【制度依据】"])
            for index, clause in enumerate(rule.basis, start=1):
                lines.append(f"{index}. {clause.display()}")
                if clause.text:
                    lines.append(f"   {clause.text}")
        if rule.tags:
            lines.extend(["", f"【标签】{'、'.join(rule.tags)}"])
        self.detail_view.setPlainText("\n".join(lines))

    # ---------------------------------------------------------- 操作

    def toggle_selected(self) -> None:
        row = self.table.selected_row()
        rules = self._filtered()
        if row < 0 or row >= len(rules):
            QMessageBox.information(self, "提示", "请先选择一条规则。")
            return
        rule = rules[row]
        rule.enabled = not rule.enabled
        # 规则对象在引擎中是同一引用，直接修改即生效
        self._refresh_table()
        self.table.selectRow(row)
        QMessageBox.information(
            self,
            "已更新",
            f"规则「{rule.name}」已{'启用' if rule.enabled else '停用'}。\n\n"
            "注意：本次修改只在当前运行期间生效。如需长期保留，请直接编辑规则文件。",
        )

    def validate(self) -> None:
        problems = self.context.rule_engine.validate()
        if not problems:
            QMessageBox.information(self, "校验通过", "全部规则的条件表达式均合法。")
            return
        QMessageBox.warning(
            self,
            "发现问题",
            f"发现 {len(problems)} 条规则存在问题：\n\n" + "\n".join(problems[:20]),
        )

    def open_dir(self) -> None:
        path = self.context.config.settings.resolved_rules_dir()
        _open_path(path, self)


def _open_path(path: Path, parent: QWidget) -> None:
    """用系统文件管理器打开目录（跨平台）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])  # noqa: S603
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(parent, "无法打开目录", f"{path}\n\n{exc}")
