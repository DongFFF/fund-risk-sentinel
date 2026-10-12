"""风险审查页：执行审查、查看线索、记录人工复核结论。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.enums import (
    DISPOSITION_TYPE_LABELS,
    EVENT_TYPE_LABELS,
    HUMAN_VERDICT_LABELS,
    RISK_CATEGORY_LABELS,
    RISK_LEVEL_LABELS,
    EventType,
    HumanVerdict,
    RiskLevel,
    StageStatus,
)
from ...core.models import ReviewConclusion, ReviewTask, RiskFinding
from ...explain.reporter import render_markdown, render_text
from ...review.pipeline import PipelineOptions
from ..context import AppContext
from ..widgets.common import (
    COLORS,
    LEVEL_COLORS,
    Card,
    DataTable,
    hint_label,
    primary_button,
    section_title,
    status_badge,
)

logger = logging.getLogger(__name__)


class ReviewWorker(QThread):
    """在后台线程执行审查，避免界面卡死。"""

    stage_done = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        context: AppContext,
        title: str,
        options: PipelineOptions,
        *,
        materials: list | None = None,
    ) -> None:
        super().__init__()
        self.context = context
        self.title = title
        self.options = options
        self.materials = materials
        options.on_stage = self.stage_done.emit

    def run(self) -> None:  # noqa: D102 - QThread 契约
        try:
            pipeline = self.context.pipeline
            materials = self.materials if self.materials is not None else self.context.pending_materials
            conclusion = pipeline.run_materials(
                materials,
                title=self.title,
                options=self.options,
                reviewer="desktop",
            )
            self.succeeded.emit(conclusion)
        except Exception as exc:  # noqa: BLE001 - 后台异常必须回传界面
            logger.exception("审查执行失败")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ReviewPage(QWidget):
    """风险审查主界面。"""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.conclusion: ReviewConclusion | None = None
        self.findings: list[RiskFinding] = []
        self.worker: ReviewWorker | None = None
        self._build()

    # ---------------------------------------------------------- 构建

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("风险审查")
        title.setProperty("role", "title")
        title_box.addWidget(title)
        self.subtitle = hint_label("请先在「材料上传」页导入材料，或从概览页双击历史任务查看结论。")
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.export_md_button = QPushButton("导出审查底稿")
        self.export_md_button.clicked.connect(self.export_markdown)
        self.export_md_button.setEnabled(False)
        header.addWidget(self.export_md_button)
        layout.addLayout(header)

        # ---------------- 运行参数
        options_card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("事项类型："))
        self.event_combo = QComboBox()
        for event in EventType:
            self.event_combo.addItem(EVENT_TYPE_LABELS.get(event, event.value), event.value)
        self.event_combo.setCurrentIndex(self.event_combo.findData(EventType.REIMBURSEMENT.value))
        row.addWidget(self.event_combo)

        row.addWidget(QLabel("模型："))
        self.provider_combo = QComboBox()
        self._reload_providers()
        row.addWidget(self.provider_combo)

        self.rules_check = QCheckBox("结构化规则校验")
        self.rules_check.setChecked(True)
        self.rag_check = QCheckBox("制度依据检索")
        self.rag_check.setChecked(True)
        self.llm_check = QCheckBox("大模型语义推理")
        self.llm_check.setChecked(True)
        for box in (self.rules_check, self.rag_check, self.llm_check):
            row.addWidget(box)

        row.addStretch(1)
        self.run_button = primary_button("开始审查")
        self.run_button.clicked.connect(self.start_review)
        row.addWidget(self.run_button)
        options_card.body().addLayout(row)
        layout.addWidget(options_card)

        # ---------------- 主体
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        progress_card = Card()
        progress_card.body().addWidget(section_title("执行进度"))
        self.progress_list = QListWidget()
        self.progress_list.setMaximumHeight(150)
        self.progress_list.setStyleSheet("background: transparent; border: none;")
        progress_card.body().addWidget(self.progress_list)
        left_layout.addWidget(progress_card)

        findings_card = Card()
        findings_header = QHBoxLayout()
        findings_header.addWidget(section_title("风险线索"))
        self.overall_label = QLabel("—")
        findings_header.addStretch(1)
        findings_header.addWidget(self.overall_label)
        findings_card.body().addLayout(findings_header)
        self.findings_table = DataTable(["等级", "类别", "线索标题", "置信度", "产生方式", "复核"])
        self.findings_table.setMinimumHeight(240)
        self.findings_table.itemSelectionChanged.connect(self._show_selected_finding)
        findings_card.body().addWidget(self.findings_table)
        left_layout.addWidget(findings_card, stretch=1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        summary_card = Card()
        summary_card.body().addWidget(section_title("结论摘要"))
        self.summary_view = QPlainTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setMinimumHeight(110)
        self.summary_view.setStyleSheet(
            f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        summary_card.body().addWidget(self.summary_view)
        right_layout.addWidget(summary_card)

        detail_card = Card()
        detail_card.body().addWidget(section_title("线索详情"))
        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(self._make_text_tab("疑点与推理"), "疑点与推理")
        self.detail_tabs.addTab(self._make_text_tab("命中依据"), "命中依据")
        self.detail_tabs.addTab(self._make_text_tab("关联材料"), "关联材料")
        self.detail_tabs.addTab(self._make_text_tab("不确定点"), "不确定点")
        self.detail_tabs.addTab(self._make_verdict_tab(), "人工复核")
        detail_card.body().addWidget(self.detail_tabs)
        right_layout.addWidget(detail_card, stretch=1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, stretch=1)

    def _make_text_tab(self, placeholder: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlaceholderText(placeholder)
        editor.setStyleSheet(
            f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        layout.addWidget(editor)
        setattr(self, f"tab_{placeholder}", editor)
        return page

    def _make_verdict_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        layout.addWidget(
            hint_label(
                "人工复核结论是六维评测的基准。请如实记录：模型提示正确选「采纳」，"
                "部分正确选「部分采纳」，误报选「否决」；若人工发现了系统未提示的风险，"
                "请另建线索或在下方的备注中说明，并标记为「模型漏报」。"
            )
        )

        form = QFormLayout()
        self.verdict_combo = QComboBox()
        for verdict in HumanVerdict:
            self.verdict_combo.addItem(HUMAN_VERDICT_LABELS.get(verdict, verdict.value), verdict.value)
        form.addRow("复核结论：", self.verdict_combo)

        self.disposition_combo = QComboBox()
        for disposition, label in DISPOSITION_TYPE_LABELS.items():
            self.disposition_combo.addItem(label, disposition.value)
        form.addRow("处置方式：", self.disposition_combo)
        layout.addLayout(form)

        layout.addWidget(QLabel("备注 / 例外说明："))
        self.verdict_note = QPlainTextEdit()
        self.verdict_note.setPlaceholderText("记录复核理由、例外情形说明等，将写入审计日志。")
        self.verdict_note.setMinimumHeight(90)
        layout.addWidget(self.verdict_note)

        row = QHBoxLayout()
        row.addStretch(1)
        save_button = primary_button("保存复核记录")
        save_button.clicked.connect(self.save_verdict)
        row.addWidget(save_button)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    # ---------------------------------------------------------- 执行审查

    def _reload_providers(self) -> None:
        self.provider_combo.clear()
        for item in self.context.client.available_providers():
            label = f"{item['name']}（{item['model'] or '-'}）"
            if not item["enabled"]:
                label += " [未启用]"
            if item["local"]:
                label += " [本地]"
            self.provider_combo.addItem(label, item["name"])
        index = self.provider_combo.findData(self.context.config.active_provider_name)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)

    def start_review(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "提示", "当前已有审查任务在执行，请稍候。")
            return
        materials = self.context.pending_materials
        if not materials:
            QMessageBox.information(
                self, "提示", "没有待审查的材料。请先在「材料上传」页导入材料。"
            )
            return

        event_type = EventType.from_value(self.event_combo.currentData(), EventType.OTHER)
        options = PipelineOptions(
            event_type=event_type,
            provider=self.provider_combo.currentData(),
            enable_rules=self.rules_check.isChecked(),
            enable_rag=self.rag_check.isChecked(),
            enable_llm=self.llm_check.isChecked(),
        )

        self.progress_list.clear()
        self.findings_table.setRowCount(0)
        self.findings = []
        self.conclusion = None
        self.summary_view.clear()
        self._clear_detail()
        self.run_button.setEnabled(False)
        self.run_button.setText("审查中…")
        self.export_md_button.setEnabled(False)

        title = self.context.pending_title or f"{len(materials)} 份材料的经费审查"
        self.worker = ReviewWorker(self.context, title, options, materials=list(materials))
        self.worker.stage_done.connect(self.on_stage)
        self.worker.succeeded.connect(self.on_success)
        self.worker.failed.connect(self.on_failure)
        self.worker.start()

    def on_stage(self, stage) -> None:
        status_label = {
            StageStatus.OK: "完成",
            StageStatus.DEGRADED: "降级完成",
            StageStatus.SKIPPED: "已跳过",
            StageStatus.FAILED: "失败",
        }.get(stage.status, stage.status.value)
        color = {
            StageStatus.OK: COLORS["ok"],
            StageStatus.DEGRADED: COLORS["degraded"],
            StageStatus.FAILED: COLORS["high"],
        }.get(stage.status, COLORS["text_muted"])
        item = QListWidgetItem(
            f"{stage.title or stage.stage}　{status_label}　{stage.duration_ms} ms"
            + (f"　{stage.message}" if stage.message else "")
        )
        item.setForeground(Qt.gray if stage.status == StageStatus.SKIPPED else item.foreground())
        if color != COLORS["text_muted"]:
            from PySide6.QtGui import QColor

            item.setForeground(QColor(color))
        self.progress_list.addItem(item)
        self.progress_list.scrollToBottom()

    def on_success(self, conclusion: ReviewConclusion) -> None:
        self.run_button.setEnabled(True)
        self.run_button.setText("开始审查")
        self.conclusion = conclusion
        self.findings = conclusion.findings
        self._persist(conclusion)
        self._render_conclusion(conclusion)

    def on_failure(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.run_button.setText("开始审查")
        QMessageBox.critical(self, "审查失败", f"审查过程中发生错误：\n{message}")
        try:
            self.context.audit.log_error("review.pipeline", message)
        except Exception:  # noqa: BLE001
            logger.exception("写入错误审计日志失败")

    # ---------------------------------------------------------- 持久化

    def _persist(self, conclusion: ReviewConclusion) -> None:
        try:
            materials = self.context.pending_materials
            task = ReviewTask(
                title=self.context.pending_title or "经费审查任务",
                event_type=conclusion.event_type,
                materials=list(materials),
                reviewer="desktop",
            )
            self.context.repository.save_task(task)
            self.context.repository.save_materials(task.task_id, materials)
            conclusion.task_id = task.task_id
            self.context.repository.save_conclusion(conclusion)
            task.conclusion = conclusion
            task.status = conclusion.status
            self.context.repository.save_task(task)
            self.context.last_task = task
            self.context.last_conclusion = conclusion

            self.context.audit.log_conclusion(
                conclusion.conclusion_id,
                task.task_id,
                overall_level=conclusion.overall_level.value,
                finding_count=len(conclusion.findings),
                provider=conclusion.provider,
                model=conclusion.model,
                duration_ms=conclusion.duration_ms,
                degraded_stages=conclusion.degraded_stages,
            )
            for record in self.context.client.call_log:
                self.context.audit.log_provider_call(
                    provider=record.provider,
                    model=record.model,
                    task=record.task,
                    latency_ms=record.latency_ms,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    degraded=record.degraded,
                    ok=record.ok,
                    error=record.error,
                )
            self.context.client.call_log.clear()
        except Exception as exc:  # noqa: BLE001 - 落库失败要提示但不丢失界面结果
            logger.exception("结论落库失败")
            QMessageBox.warning(
                self,
                "保存失败",
                f"审查已完成，但结论未能写入数据库：\n{exc}\n\n"
                "界面上的结论仍可查看，请及时导出。",
            )

    # ---------------------------------------------------------- 渲染

    def _render_conclusion(self, conclusion: ReviewConclusion) -> None:
        event_label = EVENT_TYPE_LABELS.get(conclusion.event_type, conclusion.event_type.value)
        self.subtitle.setText(
            f"事项类型：{event_label}　|　线索 {len(conclusion.findings)} 条　|　"
            f"耗时 {conclusion.duration_ms / 1000:.1f} 秒　|　模型 "
            f"{conclusion.provider or '未使用'}/{conclusion.model or '-'}"
        )

        level = conclusion.overall_level
        self.overall_label.setText(
            f"总体等级：{RISK_LEVEL_LABELS.get(level, level.value)}"
            + ("　⚠ 存在降级执行的阶段" if conclusion.degraded_stages else "")
        )
        self.overall_label.setStyleSheet(
            f"color: {LEVEL_COLORS.get(level.value, COLORS['text'])}; font-weight: 600;"
        )

        self.summary_view.setPlainText(conclusion.summary or "（无摘要）")

        rows = []
        colors: dict[int, str] = {}
        for index, finding in enumerate(conclusion.findings):
            rows.append(
                [
                    RISK_LEVEL_LABELS.get(finding.level, finding.level.value),
                    RISK_CATEGORY_LABELS.get(finding.category, finding.category.value),
                    finding.title,
                    f"{finding.confidence:.2f}",
                    {"rule": "规则", "llm": "模型", "hybrid": "规则+模型"}.get(
                        finding.produced_by, finding.produced_by or "-"
                    ),
                    HUMAN_VERDICT_LABELS.get(finding.human_verdict, "待复核"),
                ]
            )
            colors[index] = LEVEL_COLORS.get(finding.level.value, COLORS["text"])
        self.findings_table.set_rows(rows, colors=colors)
        self.export_md_button.setEnabled(bool(conclusion.findings) or bool(conclusion.summary))

        # 降级提示直接追加到摘要区，避免被忽略
        if conclusion.degraded_stages:
            extra = (
                "\n\n【能力边界提示】本次审查有以下阶段降级执行："
                + "、".join(conclusion.degraded_stages)
                + "。请据此判断结论的覆盖范围。"
            )
            self.summary_view.setPlainText((conclusion.summary or "") + extra)

        if conclusion.findings:
            self.findings_table.selectRow(0)

    def _clear_detail(self) -> None:
        for name in ("疑点与推理", "命中依据", "关联材料", "不确定点"):
            editor = getattr(self, f"tab_{name}", None)
            if editor is not None:
                editor.clear()
        self.verdict_note.clear()

    def _show_selected_finding(self) -> None:
        row = self.findings_table.selected_row()
        if row < 0 or row >= len(self.findings):
            return
        finding = self.findings[row]

        reasoning = (
            f"【风险等级】{RISK_LEVEL_LABELS.get(finding.level, finding.level.value)}　"
            f"【风险类别】{RISK_CATEGORY_LABELS.get(finding.category, finding.category.value)}　"
            f"【置信度】{finding.confidence:.2f}\n\n"
            f"【疑点描述】\n{finding.description or '（无）'}\n\n"
            f"【推理与依据】\n{finding.rationale or '（无）'}\n\n"
            f"【建议复核点】\n{finding.suggestion or '（无）'}"
        )
        self.tab_疑点与推理.setPlainText(reasoning)

        if finding.matched_clauses:
            lines = []
            for index, clause in enumerate(finding.matched_clauses, start=1):
                lines.append(f"[{index}] {clause.display()}")
                if clause.text:
                    lines.append(f"    原文：{clause.text}")
            self.tab_命中依据.setPlainText("\n".join(lines))
        else:
            self.tab_命中依据.setPlainText(
                "本条线索未附明确制度依据，属经验性提示。建议人工补充相关制度条款后再作处置。"
            )

        if finding.evidences:
            lines = []
            for index, evidence in enumerate(finding.evidences, start=1):
                lines.append(
                    f"[{index}] {evidence.material_name}"
                    + (f"｜{evidence.locator}" if evidence.locator else "")
                )
                if evidence.snippet:
                    lines.append(f"    原文摘录：{evidence.snippet}")
            self.tab_关联材料.setPlainText("\n".join(lines))
        else:
            self.tab_关联材料.setPlainText("未定位到具体材料位置。")

        if finding.counter_evidence:
            self.tab_不确定点.setPlainText(
                "\n".join(f"· {item}" for item in finding.counter_evidence)
            )
        else:
            self.tab_不确定点.setPlainText("本条线索未记录反向证据或不确定点。")

        index = self.verdict_combo.findData(finding.human_verdict.value)
        if index >= 0:
            self.verdict_combo.setCurrentIndex(index)

    # ---------------------------------------------------------- 人工复核

    def save_verdict(self) -> None:
        row = self.findings_table.selected_row()
        if row < 0 or row >= len(self.findings):
            QMessageBox.information(self, "提示", "请先选择一条线索。")
            return
        finding = self.findings[row]
        verdict = HumanVerdict.from_value(self.verdict_combo.currentData(), HumanVerdict.PENDING)
        note = self.verdict_note.toPlainText().strip()

        finding.human_verdict = verdict
        finding.human_note = note
        finding.reviewed_by = "desktop"

        try:
            self.context.repository.update_finding_verdict(
                finding.finding_id, verdict, note=note, reviewed_by="desktop"
            )
            self.context.audit.log_verdict(
                finding.finding_id, verdict.value, note=note, reviewer="desktop"
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", f"复核结论未能写入数据库：\n{exc}")
            return

        from PySide6.QtWidgets import QTableWidgetItem

        self.findings_table.setItem(
            row, 5, QTableWidgetItem(HUMAN_VERDICT_LABELS.get(verdict, verdict.value))
        )
        QMessageBox.information(
            self,
            "已记录",
            f"线索「{finding.title}」的复核结论已记录为："
            f"{HUMAN_VERDICT_LABELS.get(verdict, verdict.value)}。",
        )

    # ---------------------------------------------------------- 导出

    def export_markdown(self) -> None:
        if self.conclusion is None:
            QMessageBox.information(self, "提示", "当前没有可导出的结论。")
            return
        default_dir = self.context.config.settings.resolved_export_dir()
        default_name = f"审查底稿-{self.conclusion.generated_at:%Y%m%d-%H%M%S}.md"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出审查底稿", str(default_dir / default_name), "Markdown 文件 (*.md);;文本文件 (*.txt)"
        )
        if not path:
            return
        target = Path(path)
        content = (
            render_markdown(self.conclusion)
            if target.suffix.lower() != ".txt"
            else render_text(self.conclusion)
        )
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"无法写入文件：\n{exc}")
            return
        self.context.audit.log_export(
            str(target), fmt=target.suffix.lstrip("."), task_id=self.conclusion.task_id
        )
        QMessageBox.information(self, "导出完成", f"审查底稿已保存至：\n{target}")

    # ---------------------------------------------------------- 载入历史任务

    def open_task(self, task_id: str) -> None:
        task = self.context.repository.get_task(task_id)
        if task is None or task.conclusion is None:
            QMessageBox.information(self, "提示", "该任务没有可展示的结论。")
            return
        self.conclusion = task.conclusion
        self.findings = list(task.conclusion.findings)
        self.context.pending_materials = list(task.materials)
        self.context.pending_title = task.title
        self.context.last_task = task
        self.context.last_conclusion = task.conclusion
        self.progress_list.clear()
        for stage in task.conclusion.pipeline_trace:
            self.on_stage(stage)
        index = self.event_combo.findData(task.event_type.value)
        if index >= 0:
            self.event_combo.setCurrentIndex(index)
        self._render_conclusion(task.conclusion)
