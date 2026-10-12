"""评测页：六维指标计算、一致性回放测试与报告导出。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.enums import METRIC_DIMENSION_LABELS, MetricDimension
from ...evaluate.metrics import Evaluator, format_report
from ...core.models import EvaluationReport, RiskFinding
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


class EvaluationPage(QWidget):
    """六维评测。"""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.report: EvaluationReport | None = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("能力评测")
        title.setProperty("role", "title")
        title_box.addWidget(title)
        title_box.addWidget(
            hint_label(
                "按课题表2的六维指标体系评价风险识别能力。指标以人工复核结论为基准——"
                "在标准化标注数据建成之前，请在审查页逐条记录复核结论，指标才会逐步可信。"
            )
        )
        header.addLayout(title_box)
        header.addStretch(1)
        self.compute_button = primary_button("计算指标")
        self.compute_button.clicked.connect(self.compute)
        header.addWidget(self.compute_button)
        self.replay_button = QPushButton("一致性回放测试")
        self.replay_button.clicked.connect(self.replay)
        header.addWidget(self.replay_button)
        self.export_button = QPushButton("导出报告")
        self.export_button.clicked.connect(self.export)
        self.export_button.setEnabled(False)
        header.addWidget(self.export_button)
        layout.addLayout(header)

        table_card = Card()
        table_card.body().addWidget(section_title("六维指标"))
        self.metrics_table = DataTable(
            ["评价维度", "指标值", "目标取向", "样本量", "是否达标", "说明"]
        )
        self.metrics_table.setMinimumHeight(240)
        table_card.body().addWidget(self.metrics_table)
        self.overall_label = QLabel("尚未计算")
        self.overall_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        table_card.body().addWidget(self.overall_label)
        layout.addWidget(table_card, stretch=2)

        bottom = QHBoxLayout()

        note_card = Card()
        note_card.body().addWidget(section_title("说明与数据状态"))
        self.note_list = QListWidget()
        self.note_list.setStyleSheet("background: transparent; border: none;")
        note_card.body().addWidget(self.note_list)
        bottom.addWidget(note_card, stretch=1)

        caliber_card = Card()
        caliber_card.body().addWidget(section_title("指标口径"))
        self.caliber_view = QPlainTextEdit()
        self.caliber_view.setReadOnly(True)
        self.caliber_view.setPlainText(
            "准确性 =（采纳数 + 0.5 × 部分采纳数）/ 已复核线索数\n"
            "召回性 = 采纳数 /（采纳数 + 人工标记的漏报数）\n"
            "时延 = 单次审查从材料解析到结论生成的总耗时\n"
            "一致性 = 同一批材料重复审查所得线索集合的 Jaccard 相似度\n"
            "可解释性 = 同时具备制度依据、材料定位与推理说明的线索占比\n"
            "审核采纳度 =（采纳数 + 部分采纳数）/ 全部线索数\n\n"
            "各维度阈值：准确性 ≥ 80%，召回性 ≥ 70%，时延均值 ≤ 60 秒，\n"
            "一致性 ≥ 85%，可解释性 ≥ 90%，采纳度 ≥ 60%。\n\n"
            "阈值可按本单位管理要求调整（src/fund_risk_sentinel/evaluate/metrics.py\n"
            "中的 DEFAULT_THRESHOLDS）。"
        )
        self.caliber_view.setStyleSheet(
            f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        caliber_card.body().addWidget(self.caliber_view)
        bottom.addWidget(caliber_card, stretch=1)

        layout.addLayout(bottom, stretch=1)

    # ---------------------------------------------------------- 计算

    def compute(self) -> None:
        evaluator = Evaluator(repository=self.context.repository)
        try:
            report = evaluator.evaluate()
        except Exception as exc:  # noqa: BLE001
            logger.exception("指标计算失败")
            QMessageBox.critical(self, "计算失败", f"指标计算过程出错：\n{exc}")
            return
        self.report = report
        self._render(report)
        try:
            self.context.repository.save_evaluation(report)
        except Exception:  # noqa: BLE001 - 落库失败不影响查看
            logger.exception("评测报告落库失败")

    def replay(self) -> None:
        """对当前待审查材料连续审查两次，计算线索集合的一致性。"""
        materials = self.context.pending_materials
        if len(materials) < 1:
            QMessageBox.information(
                self, "提示", "没有可回放的材料。请先在「材料上传」页导入材料。"
            )
            return
        answer = QMessageBox.question(
            self,
            "一致性回放测试",
            "将对当前材料连续执行两次完整审查，比较两次线索集合的一致性。\n"
            "使用云端模型会产生两次调用费用。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        QApplication.setOverrideCursor(QApplication.WaitCursor)
        try:
            runs: list[list[RiskFinding]] = []
            for index in range(2):
                conclusion = self.context.pipeline.run_materials(
                    materials,
                    title=f"一致性回放测试 #{index + 1}",
                    options=self.context.default_options(),
                )
                runs.append(list(conclusion.findings))
            evaluator = Evaluator(repository=self.context.repository)
            report = evaluator.evaluate(
                findings=runs[0],
                conclusions=[],
                replays=runs,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("回放测试失败")
            QMessageBox.critical(self, "回放失败", f"回放测试过程出错：\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.report = report
        self._render(report)
        consistency = report.metric(MetricDimension.CONSISTENCY)
        QMessageBox.information(
            self,
            "回放测试完成",
            (
                f"两次审查分别得到 {len(runs[0])} 条与 {len(runs[1])} 条线索。\n"
                + (consistency.detail if consistency else "")
            ),
        )

    # ---------------------------------------------------------- 渲染

    def _render(self, report: EvaluationReport) -> None:
        rows = []
        colors: dict[int, str] = {}
        for index, metric in enumerate(report.metrics):
            label = METRIC_DIMENSION_LABELS.get(metric.dimension, metric.dimension.value)
            if metric.unit == "ratio":
                value_text = f"{metric.value:.2%}"
            elif metric.unit == "ms":
                value_text = f"{metric.value / 1000:.1f} 秒"
            else:
                value_text = f"{metric.value:g}"
            passed_text = "—" if metric.passed is None else ("达标" if metric.passed else "未达标")
            rows.append(
                [label, value_text, metric.target, metric.sample_size, passed_text, metric.detail]
            )
            if metric.passed is True:
                colors[index] = COLORS["ok"]
            elif metric.passed is False:
                colors[index] = COLORS["high"]
            else:
                colors[index] = COLORS["text_muted"]
        self.metrics_table.set_rows(rows, colors=colors)

        passed = sum(1 for m in report.metrics if m.passed is True)
        failed = sum(1 for m in report.metrics if m.passed is False)
        unknown = sum(1 for m in report.metrics if m.passed is None)
        self.overall_label.setText(
            f"生成时间：{report.generated_at:%Y-%m-%d %H:%M:%S}　|　"
            f"覆盖任务 {len(report.task_ids)} 个　|　达标 {passed} 项，未达标 {failed} 项，待积累数据 {unknown} 项"
        )

        self.note_list.clear()
        for note in report.notes:
            self.note_list.addItem(f"· {note}")
        if not report.notes:
            self.note_list.addItem("· 暂无特别说明。")

        self.export_button.setEnabled(True)

    def export(self) -> None:
        if self.report is None:
            QMessageBox.information(self, "提示", "请先计算指标。")
            return
        default_dir = self.context.config.settings.resolved_export_dir()
        default_name = f"评测报告-{self.report.generated_at:%Y%m%d-%H%M%S}.md"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出评测报告", str(default_dir / default_name), "Markdown 文件 (*.md)"
        )
        if not path:
            return
        target = Path(path)
        try:
            target.write_text(format_report(self.report), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"无法写入文件：\n{exc}")
            return
        self.context.audit.log_export(str(target), fmt="md")
        QMessageBox.information(self, "导出完成", f"评测报告已保存至：\n{target}")

    def refresh(self) -> None:
        """页面切换到本页时的轻量刷新：若已有报告则重算（数据可能已变化）。"""
        if self.report is not None:
            self.compute()
