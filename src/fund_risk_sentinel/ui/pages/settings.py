"""设置页：模型接入、审查参数、安全策略与目录信息。

这是"程序可以调用任何模型 API"的用户界面入口：改 base_url、填 Key、
点"测试连接"，即可在 DeepSeek / 通义 / Kimi / 智谱 / OpenAI / Claude /
Gemini / 本地 Ollama / 自建微调模型之间切换，无需改代码。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import user_config_dir
from ...core.enums import ADAPTATION_PATH_LABELS, AdaptationPath
from ...core.exceptions import LLMError
from ..context import AppContext
from ..widgets.common import (
    COLORS,
    Card,
    DataTable,
    hint_label,
    primary_button,
    section_title,
)
from .rules import _open_path

logger = logging.getLogger(__name__)


class SettingsPage(QWidget):
    """设置。"""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(12)

        title = QLabel("设置")
        title.setProperty("role", "title")
        outer.addWidget(title)
        outer.addWidget(
            hint_label(
                "配置保存在用户配置目录，不随程序目录变化，也不会随程序更新丢失。"
                "API Key 建议写成 ${环境变量名}，避免明文落盘。"
            )
        )

        tabs = QTabWidget()
        tabs.addTab(self._build_provider_tab(), "模型接入")
        tabs.addTab(self._build_review_tab(), "审查参数")
        tabs.addTab(self._build_security_tab(), "数据与安全")
        tabs.addTab(self._build_about_tab(), "目录与关于")
        outer.addWidget(tabs, stretch=1)

        self.refresh()

    # ============================================================ 模型接入

    def _build_provider_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        row = QHBoxLayout()
        row.addWidget(section_title("已配置的模型服务"))
        row.addStretch(1)
        self.set_active_button = primary_button("设为当前模型")
        self.set_active_button.clicked.connect(self.set_active_provider)
        row.addWidget(self.set_active_button)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self.test_connection)
        row.addWidget(self.test_button)
        self.save_button = QPushButton("保存配置到用户目录")
        self.save_button.clicked.connect(self.save_providers)
        row.addWidget(self.save_button)
        layout.addLayout(row)

        self.provider_table = DataTable(
            ["名称", "协议", "模型", "部署", "状态", "当前", "说明"], stretch_last=True
        )
        self.provider_table.setMinimumHeight(180)
        self.provider_table.itemSelectionChanged.connect(self._load_provider_form)
        layout.addWidget(self.provider_table)

        edit_group = QGroupBox("编辑选中的模型服务")
        form = QFormLayout(edit_group)
        form.setLabelAlignment(Qt.AlignRight)

        self.name_edit = QLineEdit()
        self.name_edit.setReadOnly(True)
        form.addRow("名称：", self.name_edit)

        self.type_combo = QComboBox()
        for value, label in (
            ("openai_compatible", "OpenAI 兼容协议（DeepSeek / 通义 / Kimi / 智谱 / Ollama / vLLM）"),
            ("anthropic", "Anthropic Claude"),
            ("gemini", "Google Gemini"),
            ("mock", "离线模拟（不联网）"),
        ):
            self.type_combo.addItem(label, value)
        form.addRow("协议类型：", self.type_combo)

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("例如 https://api.deepseek.com/v1")
        form.addRow("API 基址：", self.base_url_edit)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("可填明文，也可填 ${DEEPSEEK_API_KEY}")
        form.addRow("API Key：", self.api_key_edit)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("例如 deepseek-chat / qwen-plus / qwen2.5:7b-instruct")
        form.addRow("模型名称：", self.model_edit)

        params = QHBoxLayout()
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setDecimals(2)
        params.addWidget(QLabel("temperature："))
        params.addWidget(self.temperature_spin)
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(256, 200_000)
        self.max_tokens_spin.setSingleStep(512)
        params.addWidget(QLabel("max_tokens："))
        params.addWidget(self.max_tokens_spin)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 1800)
        params.addWidget(QLabel("超时(秒)："))
        params.addWidget(self.timeout_spin)
        params.addStretch(1)
        form.addRow("生成参数：", params)

        flags = QHBoxLayout()
        self.enabled_check = QCheckBox("启用该服务")
        self.local_check = QCheckBox("本地部署（材料不出本机）")
        self.json_check = QCheckBox("支持强制 JSON 输出")
        for box in (self.enabled_check, self.local_check, self.json_check):
            flags.addWidget(box)
        flags.addStretch(1)
        form.addRow("选项：", flags)

        self.adaptation_combo = QComboBox()
        self.adaptation_combo.addItem("（未标注）", "")
        for path in AdaptationPath:
            self.adaptation_combo.addItem(ADAPTATION_PATH_LABELS.get(path, path.value), path.value)
        form.addRow("适配路径：", self.adaptation_combo)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("备注说明，例如：本校私有部署，数据不出校园网")
        form.addRow("说明：", self.description_edit)

        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        self.apply_button = primary_button("应用到当前运行")
        self.apply_button.clicked.connect(self.apply_provider_edit)
        apply_row.addWidget(self.apply_button)
        form.addRow("", apply_row)

        layout.addWidget(edit_group)

        layout.addWidget(
            hint_label(
                "提示：修改后点「应用到当前运行」立即生效（不写盘）；"
                "点「保存配置到用户目录」才会持久化到 "
                f"{user_config_dir() / 'providers.yaml'}。"
            )
        )
        layout.addStretch(1)
        return _scrollable(page)

    # ============================================================ 审查参数

    def _build_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        group = QGroupBox("审查流水线参数")
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignRight)

        self.max_materials_spin = QSpinBox()
        self.max_materials_spin.setRange(1, 500)
        form.addRow("单任务材料上限：", self.max_materials_spin)

        self.max_chars_spin = QSpinBox()
        self.max_chars_spin.setRange(1_000, 500_000)
        self.max_chars_spin.setSingleStep(5_000)
        self.max_chars_spin.setSuffix(" 字符")
        form.addRow("单份材料送模型上限：", self.max_chars_spin)

        self.rag_top_k_spin = QSpinBox()
        self.rag_top_k_spin.setRange(1, 30)
        form.addRow("制度依据召回条数：", self.rag_top_k_spin)

        self.min_confidence_spin = QDoubleSpinBox()
        self.min_confidence_spin.setRange(0.0, 1.0)
        self.min_confidence_spin.setSingleStep(0.05)
        self.min_confidence_spin.setDecimals(2)
        form.addRow("线索最低置信度：", self.min_confidence_spin)

        toggles = QHBoxLayout()
        self.rule_check = QCheckBox("启用结构化规则校验")
        self.rag_check = QCheckBox("启用制度依据检索")
        self.llm_check = QCheckBox("启用大模型语义推理")
        self.degrade_check = QCheckBox("模型不可用时降级为规则结果")
        self.human_check = QCheckBox("强制标记为待人工复核")
        for box in (self.rule_check, self.rag_check, self.llm_check, self.degrade_check, self.human_check):
            toggles.addWidget(box)
        toggles.addStretch(1)
        form.addRow("能力开关：", toggles)
        layout.addWidget(group)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        apply_button = primary_button("应用到当前运行")
        apply_button.clicked.connect(self.apply_review_settings)
        save_row.addWidget(apply_button)
        persist = QPushButton("保存配置到用户目录")
        persist.clicked.connect(self.save_settings)
        save_row.addWidget(persist)
        layout.addLayout(save_row)

        layout.addWidget(
            hint_label(
                "「模型不可用时降级为规则结果」关闭后，模型调用失败将直接中断审查；"
                "开启时系统会退回规则引擎结果，并在结论中明确标注降级阶段。"
            )
        )
        layout.addStretch(1)
        return _scrollable(page)

    # ============================================================ 数据与安全

    def _build_security_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        group = QGroupBox("脱敏策略（材料送模型前执行）")
        grid = QVBoxLayout(group)
        self.redact_id_check = QCheckBox("身份证号")
        self.redact_phone_check = QCheckBox("手机号")
        self.redact_bank_check = QCheckBox("银行账号")
        self.redact_email_check = QCheckBox("电子邮箱")
        for box in (
            self.redact_id_check,
            self.redact_phone_check,
            self.redact_bank_check,
            self.redact_email_check,
        ):
            grid.addWidget(box)
        grid.addWidget(
            hint_label("脱敏采用保形替换（保留长度与首尾特征），便于判断格式是否正常但不泄露真实号码。")
        )
        layout.addWidget(group)

        group2 = QGroupBox("数据安全与合规")
        form = QFormLayout(group2)
        form.setLabelAlignment(Qt.AlignRight)

        self.cloud_check = QCheckBox("允许调用云端模型（关闭后仅允许本地模型）")
        form.addRow("", self.cloud_check)

        self.classification_combo = QComboBox()
        for level in ("公开", "内部", "敏感", "核心"):
            self.classification_combo.addItem(level)
        form.addRow("数据分类分级：", self.classification_combo)

        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(30, 36_500)
        self.retention_spin.setSuffix(" 天")
        form.addRow("材料与结论保存期限：", self.retention_spin)

        self.audit_check = QCheckBox("启用审计日志（哈希链防篡改）")
        form.addRow("", self.audit_check)
        layout.addWidget(group2)

        row = QHBoxLayout()
        row.addStretch(1)
        apply_button = primary_button("应用到当前运行")
        apply_button.clicked.connect(self.apply_security_settings)
        row.addWidget(apply_button)
        persist = QPushButton("保存配置到用户目录")
        persist.clicked.connect(self.save_settings)
        row.addWidget(persist)
        layout.addLayout(row)

        self.audit_status = QPlainTextEdit()
        self.audit_status.setReadOnly(True)
        self.audit_status.setMinimumHeight(120)
        self.audit_status.setStyleSheet(
            f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        layout.addWidget(section_title("审计日志状态"))
        layout.addWidget(self.audit_status)
        verify_button = QPushButton("校验审计日志链完整性")
        verify_button.clicked.connect(self.verify_audit)
        layout.addWidget(verify_button, alignment=Qt.AlignRight)
        layout.addStretch(1)
        return _scrollable(page)

    # ============================================================ 目录与关于

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.paths_card = Card()
        self.paths_card.body().addWidget(section_title("目录位置"))
        self.paths_layout = QFormLayout()
        self.paths_card.body().addLayout(self.paths_layout)
        layout.addWidget(self.paths_card)

        button_row = QHBoxLayout()
        for text, handler in (
            ("打开数据目录", lambda: _open_path(self.context.config.settings.resolved_data_dir(), self)),
            ("打开配置目录", lambda: _open_path(user_config_dir(), self)),
            ("打开规则目录", lambda: _open_path(self.context.config.settings.resolved_rules_dir(), self)),
            ("打开知识库目录", lambda: _open_path(self.context.config.settings.resolved_knowledge_dir(), self)),
            ("打开导出目录", lambda: _open_path(self.context.config.settings.resolved_export_dir(), self)),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            button_row.addWidget(button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.config_view = QPlainTextEdit()
        self.config_view.setReadOnly(True)
        self.config_view.setStyleSheet(
            f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        layout.addWidget(section_title("配置概览"))
        layout.addWidget(self.config_view, stretch=1)

        layout.addWidget(
            hint_label(
                "本程序输出的是可核验的风险线索，不构成合规与否的认定；"
                "所有线索须经具有相应权限的人员复核后方可作为处置依据。"
                "模型选用、数据输入、上线审批、运行监测、人工复核、应急接管与退出处置"
                "应纳入本单位人工智能应用清单与全生命周期管理。"
            )
        )
        return _scrollable(page)

    # ============================================================ 刷新与保存

    def refresh(self) -> None:
        self._refresh_provider_table()
        self._load_settings_form()
        self._refresh_paths()
        self._refresh_config_view()
        self.verify_audit(silent=True)

    def _refresh_provider_table(self) -> None:
        rows = []
        active = self.context.config.active_provider_name
        for item in self.context.client.available_providers():
            rows.append(
                [
                    item["name"],
                    item["type"],
                    item["model"] or "-",
                    "本地" if item["local"] else "云端",
                    ("就绪" if item["ready"] else "配置不完整") if item["enabled"] else "未启用",
                    "●" if item["name"] == active else "",
                    item["description"] or "",
                ]
            )
        self.provider_table.set_rows(rows)

    def _load_settings_form(self) -> None:
        review = self.context.config.settings.review
        security = self.context.config.settings.security

        self.max_materials_spin.setValue(review.max_materials_per_task)
        self.max_chars_spin.setValue(review.max_chars_per_material)
        self.rag_top_k_spin.setValue(review.rag_top_k)
        self.min_confidence_spin.setValue(review.min_confidence_to_report)
        self.rule_check.setChecked(review.enable_rule_engine)
        self.rag_check.setChecked(review.enable_rag)
        self.llm_check.setChecked(review.enable_llm_reasoning)
        self.degrade_check.setChecked(review.degrade_on_llm_failure)
        self.human_check.setChecked(review.require_human_review)

        self.redact_id_check.setChecked(security.redact_id_card)
        self.redact_phone_check.setChecked(security.redact_phone)
        self.redact_bank_check.setChecked(security.redact_bank_account)
        self.redact_email_check.setChecked(security.redact_email)
        self.cloud_check.setChecked(security.allow_cloud_providers)
        index = self.classification_combo.findText(security.data_classification)
        if index >= 0:
            self.classification_combo.setCurrentIndex(index)
        self.retention_spin.setValue(security.retention_days)
        self.audit_check.setChecked(security.audit_log_enabled)

    def _load_provider_form(self) -> None:
        row = self.provider_table.selected_row()
        names = [item["name"] for item in self.context.client.available_providers()]
        if row < 0 or row >= len(names):
            return
        name = names[row]
        try:
            provider = self.context.config.provider(name)
        except Exception:  # noqa: BLE001
            return
        self.name_edit.setText(provider.name)
        index = self.type_combo.findData(provider.type)
        if index >= 0:
            self.type_combo.setCurrentIndex(index)
        self.base_url_edit.setText(provider.base_url)
        self.api_key_edit.setText(provider.api_key)
        self.model_edit.setText(provider.model)
        self.temperature_spin.setValue(provider.temperature)
        self.max_tokens_spin.setValue(provider.max_tokens)
        self.timeout_spin.setValue(provider.timeout)
        self.enabled_check.setChecked(provider.enabled)
        self.local_check.setChecked(provider.is_local)
        self.json_check.setChecked(provider.supports_json_mode)
        index = self.adaptation_combo.findData(
            provider.adaptation_path.value if provider.adaptation_path else ""
        )
        self.adaptation_combo.setCurrentIndex(max(0, index))
        self.description_edit.setText(provider.description)

    def _refresh_paths(self) -> None:
        while self.paths_layout.rowCount():
            self.paths_layout.removeRow(0)
        settings = self.context.config.settings
        items = [
            ("用户配置目录", str(user_config_dir())),
            ("数据目录", str(settings.resolved_data_dir())),
            ("数据库", str(settings.resolved_db_path())),
            ("日志目录", str(settings.resolved_log_dir())),
            ("规则目录", str(settings.resolved_rules_dir())),
            ("知识库目录", str(settings.resolved_knowledge_dir())),
            ("导出目录", str(settings.resolved_export_dir())),
        ]
        for label, value in items:
            name = QLabel(label)
            name.setProperty("role", "hint")
            content = QLabel(value)
            content.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.paths_layout.addRow(name, content)

    def _refresh_config_view(self) -> None:
        describe = self.context.config.describe()
        lines = []
        for key, value in describe.items():
            if isinstance(value, list):
                lines.append(f"{key}：")
                lines.extend(f"    · {item}" for item in value)
            else:
                lines.append(f"{key}：{value}")
        self.config_view.setPlainText("\n".join(lines))

    # ============================================================ 操作

    def _selected_provider_name(self) -> str:
        row = self.provider_table.selected_row()
        names = [item["name"] for item in self.context.client.available_providers()]
        if row < 0 or row >= len(names):
            return ""
        return names[row]

    def set_active_provider(self) -> None:
        name = self._selected_provider_name()
        if not name:
            QMessageBox.information(self, "提示", "请先在表格中选择一个模型服务。")
            return
        provider = self.context.config.provider(name)
        if not provider.ready and provider.type != "mock":
            answer = QMessageBox.question(
                self,
                "配置不完整",
                f"「{name}」的配置似乎不完整（缺少 base_url / model / api_key）。\n"
                "仍要设为当前模型吗？调用时可能失败。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.context.config.set_active_provider(name)
        self.context.reload_client()
        self._refresh_provider_table()
        self._refresh_config_view()
        try:
            self.context.audit.log_config_change(changes={"active_provider": name}, actor="desktop")
        except Exception:  # noqa: BLE001
            logger.exception("写入配置变更审计失败")
        QMessageBox.information(self, "已切换", f"当前模型已切换为「{name}」。")

    def test_connection(self) -> None:
        name = self._selected_provider_name()
        if not name:
            QMessageBox.information(self, "提示", "请先在表格中选择一个模型服务。")
            return
        QMessageBox.information(
            self,
            "开始测试",
            f"将对「{name}」发起一次极短请求做连通性检测，可能需要几秒钟。",
        )
        try:
            results = self.context.client.health_check(name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "测试失败", f"{type(exc).__name__}: {exc}")
            return
        lines = [
            f"{key}：{'正常' if value.get('ok') else '失败'}\n    {value.get('message', '')}"
            for key, value in results.items()
        ]
        QMessageBox.information(self, "测试结果", "\n\n".join(lines) or "未执行任何测试。")

    def apply_provider_edit(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "提示", "请先在表格中选择一个模型服务。")
            return
        try:
            provider = self.context.config.provider(name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法编辑", str(exc))
            return

        provider.type = self.type_combo.currentData()
        provider.base_url = self.base_url_edit.text().strip()
        provider.api_key = self.api_key_edit.text().strip()
        provider.model = self.model_edit.text().strip()
        provider.temperature = self.temperature_spin.value()
        provider.max_tokens = self.max_tokens_spin.value()
        provider.timeout = self.timeout_spin.value()
        provider.enabled = self.enabled_check.isChecked()
        provider.is_local = self.local_check.isChecked()
        provider.supports_json_mode = self.json_check.isChecked()
        adaptation = self.adaptation_combo.currentData()
        provider.adaptation_path = AdaptationPath.from_value(adaptation) if adaptation else None
        provider.description = self.description_edit.text().strip()

        self.context.reload_client()
        self._refresh_provider_table()
        self._refresh_config_view()
        QMessageBox.information(
            self,
            "已应用",
            f"「{name}」的配置已在当前运行中生效。\n\n"
            "如需长期保留，请点击「保存配置到用户目录」。",
        )

    def apply_review_settings(self) -> None:
        review = self.context.config.settings.review
        review.max_materials_per_task = self.max_materials_spin.value()
        review.max_chars_per_material = self.max_chars_spin.value()
        review.rag_top_k = self.rag_top_k_spin.value()
        review.min_confidence_to_report = self.min_confidence_spin.value()
        review.enable_rule_engine = self.rule_check.isChecked()
        review.enable_rag = self.rag_check.isChecked()
        review.enable_llm_reasoning = self.llm_check.isChecked()
        review.degrade_on_llm_failure = self.degrade_check.isChecked()
        review.require_human_review = self.human_check.isChecked()
        self._refresh_config_view()
        QMessageBox.information(self, "已应用", "审查参数已在当前运行中生效。")

    def apply_security_settings(self) -> None:
        security = self.context.config.settings.security
        security.redact_id_card = self.redact_id_check.isChecked()
        security.redact_phone = self.redact_phone_check.isChecked()
        security.redact_bank_account = self.redact_bank_check.isChecked()
        security.redact_email = self.redact_email_check.isChecked()
        security.allow_cloud_providers = self.cloud_check.isChecked()
        security.data_classification = self.classification_combo.currentText()
        security.retention_days = self.retention_spin.value()
        security.audit_log_enabled = self.audit_check.isChecked()
        self.context.audit.enabled = security.audit_log_enabled
        self._refresh_config_view()
        QMessageBox.information(self, "已应用", "数据与安全设置已在当前运行中生效。")

    def save_providers(self) -> None:
        try:
            path = self.context.config.save_providers()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"{exc}")
            return
        QMessageBox.information(self, "已保存", f"模型配置已写入：\n{path}")

    def save_settings(self) -> None:
        self.apply_review_settings()
        self.apply_security_settings()
        try:
            path = self.context.config.save_settings()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"{exc}")
            return
        QMessageBox.information(self, "已保存", f"应用设置已写入：\n{path}")

    def verify_audit(self, silent: bool = False) -> None:
        try:
            ok, message = self.context.audit.verify_chain()
        except Exception as exc:  # noqa: BLE001
            message = f"校验失败：{exc}"
            ok = False
        self.audit_status.setPlainText(
            ("✔ " if ok else "✘ ") + message
            + f"\n\n日志目录：{self.context.config.settings.resolved_log_dir()}\n"
            "审计记录采用哈希链串联：任何一条被修改，后续链路校验都会失败。"
        )
        if not silent:
            QMessageBox.information(self, "审计日志校验", message)


def _scrollable(page: QWidget) -> QWidget:
    """把页面包进滚动区域，避免小屏下控件被截断。"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(page)
    return scroll
