"""配置加载与校验。

三层配置，互不干扰：

1. ``AppSettings``     —— 应用级设置（目录、审查参数、安全策略、界面偏好）
2. ``ProviderConfig``  —— 单个模型服务的接入参数
3. ``ProvidersConfig`` —— provider 集合 + 当前激活项 + 失败回退链

支持 ``${ENV_VAR}`` 与 ``${ENV_VAR:-默认值}`` 环境变量插值，因此 API Key
可以只放在系统环境变量里，配置文件里不出现明文。
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from ..core.enums import AdaptationPath
from ..core.exceptions import ConfigError, ProviderNotConfiguredError
from ..core.models import Base
from .paths import (
    bundled_dir,
    ensure_dir,
    project_root,
    resolve_config_file,
    user_config_dir,
    user_data_dir,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

SETTINGS_FILE = "settings.yaml"
PROVIDERS_FILE = "providers.yaml"


# ================================================================ 环境变量插值

def interpolate_env(value: Any) -> Any:
    """递归替换字符串中的 ``${VAR}`` / ``${VAR:-default}``。

    未定义且无默认值时报错，避免"Key 变成字面量 ${X}"这种隐蔽故障。
    """
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            if var in os.environ and os.environ[var] != "":
                return os.environ[var]
            if default is not None:
                return default
            raise ConfigError(f"配置引用了未定义的环境变量 ${{{var}}}，请先设置该变量或改用 ${var}:-默认值")
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate_env(v) for v in value]
    return value


# ================================================================ 模型服务配置

ProviderType = Literal["openai_compatible", "anthropic", "gemini", "mock", "custom"]


class ProviderConfig(Base):
    """单个模型服务的接入参数。"""

    name: str = Field(description="provider 标识，配置文件中作为键名")
    type: ProviderType = Field(default="openai_compatible", description="协议类型")
    enabled: bool = True
    base_url: str = Field(default="", description="API 基址，如 https://api.deepseek.com/v1")
    api_key: str = Field(default="", description="API Key；建议写成 ${ENV_VAR}")
    model: str = Field(default="", description="模型名，如 deepseek-chat")
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, gt=0)
    timeout: int = Field(default=120, gt=0, description="单次请求超时（秒）")
    max_retries: int = Field(default=2, ge=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict, description="透传给服务端的额外字段")
    adaptation_path: AdaptationPath | None = Field(
        default=None,
        description="该 provider 对应的适配路径（用于结论中标注技术路线）",
    )
    supports_json_mode: bool = Field(default=True, description="是否支持强制 JSON 输出")
    supports_vision: bool = Field(default=False, description="是否支持图片输入")
    cost_per_1k_prompt: float = Field(default=0.0, ge=0.0, description="每千 prompt token 成本（元）")
    cost_per_1k_completion: float = Field(default=0.0, ge=0.0, description="每千 completion token 成本（元）")
    description: str = ""
    #: 是否为本地部署（本地模型不触发"数据外发"提示）
    is_local: bool = False

    @model_validator(mode="after")
    def _force_local_for_offline_types(self) -> "ProviderConfig":
        """离线协议一律视为本地部署。

        ``mock`` 根本不联网，若因漏写 ``is_local: true`` 而在界面上被标成
        「云端」，会让人误判材料是否外发——这是安全提示，不能靠配置写对来保证。

        直接写 ``__dict__``：本模型开启了 ``validate_assignment``，
        走常规赋值会重新触发本校验器，导致无限递归。
        """
        if self.type == "mock":
            self.__dict__["is_local"] = True
        return self

    @property
    def display_name(self) -> str:
        return self.description or self.name

    @property
    def ready(self) -> bool:
        """是否具备发起调用的最低条件。"""
        if self.type == "mock":
            return True
        if self.type == "openai_compatible" and not self.base_url:
            return False
        return bool(self.model) and bool(self.api_key)


class ProvidersConfig(Base):
    """provider 集合与路由策略。"""

    active: str = Field(default="mock", description="当前激活的 provider 名称")
    fallback: list[str] = Field(default_factory=list, description="激活项失败时依次尝试的 provider")
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    def get(self, name: str | None = None) -> ProviderConfig:
        key = name or self.active
        provider = self.providers.get(key)
        if provider is None:
            raise ProviderNotConfiguredError(
                f"未找到名为 {key!r} 的模型配置。已配置：{sorted(self.providers) or '（空）'}"
            )
        return provider

    def enabled_names(self) -> list[str]:
        return [n for n, p in self.providers.items() if p.enabled]

    def route_chain(self, name: str | None = None) -> list[ProviderConfig]:
        """返回实际尝试顺序：主 provider → fallback 链（去重、只保留启用项）。"""
        chain: list[ProviderConfig] = []
        seen: set[str] = set()
        for key in [name or self.active, *self.fallback]:
            if not key or key in seen:
                continue
            seen.add(key)
            provider = self.providers.get(key)
            if provider and provider.enabled:
                chain.append(provider)
        return chain


# ================================================================ 应用设置

class UiSettings(Base):
    theme: str = Field(default="light", description="light / dark")
    language: str = "zh-CN"
    page_size: int = Field(default=50, gt=0)
    show_raw_json: bool = Field(default=False, description="界面是否显示原始 JSON")
    font_size: int = Field(default=10, ge=8, le=20)
    recent_files: list[str] = Field(default_factory=list)


class ReviewSettings(Base):
    """审查流水线参数。"""

    default_provider: str = Field(default="", description="留空则用 providers.active")
    max_materials_per_task: int = Field(default=50, gt=0)
    max_chars_per_material: int = Field(
        default=60_000, gt=0, description="单份材料送入模型的最大字符数，超出则截断并标注"
    )
    total_prompt_char_budget: int = Field(default=240_000, gt=0)
    enable_rule_engine: bool = Field(default=True, description="是否启用结构化规则校验")
    enable_rag: bool = Field(default=True, description="是否启用制度知识检索（RAG）")
    enable_llm_reasoning: bool = Field(default=True, description="是否启用大模型语义推理")
    rag_top_k: int = Field(default=6, gt=0)
    require_human_review: bool = Field(default=True, description="是否强制标记为待人工复核")
    min_confidence_to_report: float = Field(
        default=0.35, ge=0.0, le=1.0, description="低于此置信度的线索不进入结论"
    )
    degrade_on_llm_failure: bool = Field(
        default=True,
        description="模型不可用时是否降级为纯规则结果（并在结论中明确标注）",
    )
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)


class SecuritySettings(Base):
    """数据安全与个人信息保护相关设置。"""

    allow_cloud_providers: bool = Field(
        default=True, description="关闭后仅允许 is_local 的 provider，材料不出本机"
    )
    redact_id_card: bool = True
    redact_phone: bool = True
    redact_bank_account: bool = True
    redact_email: bool = True
    redact_person_name: bool = Field(default=False, description="人名脱敏（默认关闭，会显著影响可读性）")
    retention_days: int = Field(default=1825, gt=0, description="材料与结论保存期限（天）")
    audit_log_enabled: bool = True
    data_classification: str = Field(default="内部", description="数据分类分级标识")
    mask_api_key_in_ui: bool = True


class AppSettings(Base):
    """应用级设置。"""

    app_name: str = "高校科研经费风险智能识别系统"
    app_version: str = "0.1.0"
    data_dir: str = Field(default="", description="留空则用用户数据目录")
    db_path: str = Field(default="", description="留空则用 <data_dir>/sentinel.db")
    log_dir: str = Field(default="", description="留空则用 <data_dir>/logs")
    rules_dir: str = Field(default="", description="留空则用 <config>/rules")
    knowledge_dir: str = Field(default="", description="留空则用 <config>/knowledge")
    export_dir: str = Field(default="", description="留空则用 <data_dir>/exports")
    ui: UiSettings = Field(default_factory=UiSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    #: 记录配置来源，便于排查"为什么读的是旧配置"
    loaded_from: list[str] = Field(default_factory=list)

    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir).expanduser() if self.data_dir else user_data_dir()

    def resolved_db_path(self) -> Path:
        if self.db_path:
            return Path(self.db_path).expanduser()
        return self.resolved_data_dir() / "sentinel.db"

    def resolved_log_dir(self) -> Path:
        return Path(self.log_dir).expanduser() if self.log_dir else self.resolved_data_dir() / "logs"

    def resolved_export_dir(self) -> Path:
        return Path(self.export_dir).expanduser() if self.export_dir else self.resolved_data_dir() / "exports"

    def resolved_rules_dir(self) -> Path:
        return Path(self.rules_dir).expanduser() if self.rules_dir else project_root() / "config" / "rules"

    def resolved_knowledge_dir(self) -> Path:
        if self.knowledge_dir:
            return Path(self.knowledge_dir).expanduser()
        return project_root() / "config" / "knowledge"

    def ensure_dirs(self) -> None:
        for p in (
            self.resolved_data_dir(),
            self.resolved_log_dir(),
            self.resolved_export_dir(),
        ):
            ensure_dir(p)


# ================================================================ 配置管理器

def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - 依赖损坏文件
        raise ConfigError(f"配置文件 {path} 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 顶层必须是映射（key: value），实际为 {type(data).__name__}")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并：override 覆盖 base，字典递归、其余整体替换。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class ConfigManager:
    """配置的唯一入口。界面与流水线都通过它取配置。"""

    def __init__(self, settings: AppSettings, providers: ProvidersConfig) -> None:
        self.settings = settings
        self.providers = providers

    # ---------------------------------------------------------- 构造

    @classmethod
    def load(cls, config_dir: Path | str | None = None, *, strict: bool = False) -> "ConfigManager":
        """加载配置。

        ``config_dir`` 给定时只读该目录；否则按搜索顺序叠加（后者补前者）。
        ``strict=True`` 时缺失配置文件直接报错。
        """
        settings_data: dict[str, Any] = {}
        providers_data: dict[str, Any] = {}
        loaded: list[str] = []

        dirs = [Path(config_dir)] if config_dir else [d for d in _search_dirs()]
        for directory in dirs:
            for filename, sink in ((SETTINGS_FILE, "settings"), (PROVIDERS_FILE, "providers")):
                path = directory / filename
                if not path.is_file():
                    continue
                raw = interpolate_env(_read_yaml(path))
                loaded.append(str(path))
                if sink == "settings":
                    settings_data = _deep_merge(settings_data, raw)
                else:
                    providers_data = _deep_merge(providers_data, raw)

        if strict and not loaded:
            raise ConfigError(f"未找到任何配置文件（{SETTINGS_FILE} / {PROVIDERS_FILE}）")

        try:
            settings = AppSettings.model_validate(settings_data)
            providers = ProvidersConfig.model_validate(_normalize_providers(providers_data))
        except ConfigError:
            raise
        except Exception as exc:  # pydantic ValidationError
            raise ConfigError(f"配置校验失败：{exc}") from exc

        settings.loaded_from = loaded
        if not providers.providers:
            providers = _default_providers()
        if providers.active not in providers.providers:
            providers.active = next(iter(providers.providers))
        return cls(settings, providers)

    # ---------------------------------------------------------- 取用

    def provider(self, name: str | None = None) -> ProviderConfig:
        """按名称取 provider；名称为空时取激活项。"""
        return self.providers.get(name or self.active_provider_name)

    @property
    def active_provider_name(self) -> str:
        return self.settings.review.default_provider or self.providers.active

    def route_chain(self, name: str | None = None) -> list[ProviderConfig]:
        chain = self.providers.route_chain(name or self.active_provider_name)
        if not chain:
            raise ProviderNotConfiguredError("没有任何可用的模型配置，请检查 providers.yaml")
        if not self.settings.security.allow_cloud_providers:
            local_only = [p for p in chain if p.is_local or p.type == "mock"]
            if not local_only:
                raise ConfigError(
                    "安全策略已禁止使用云端模型（security.allow_cloud_providers=false），"
                    "但没有可用的本地模型配置。请配置 Ollama / vLLM 等本地端点，或调整该策略。"
                )
            chain = local_only
        return chain

    def set_active_provider(self, name: str) -> None:
        self.providers.get(name)  # 校验存在性
        self.providers.active = name

    # ---------------------------------------------------------- 校验与落盘

    def validate(self) -> list[str]:
        """返回问题清单（空列表表示健康）。不抛异常，供界面直接展示。"""
        problems: list[str] = []
        if not self.providers.providers:
            problems.append("未配置任何模型服务")
        for name, p in self.providers.providers.items():
            if not p.enabled:
                continue
            if p.type == "openai_compatible" and not p.base_url:
                problems.append(f"[{name}] 缺少 base_url")
            if p.type != "mock" and not p.model:
                problems.append(f"[{name}] 缺少 model")
            if p.type != "mock" and not p.api_key:
                problems.append(f"[{name}] 缺少 api_key（可写 ${{环境变量名}}）")
            if p.type != "mock" and not p.base_url and p.type != "anthropic" and p.type != "gemini":
                problems.append(f"[{name}] 缺少 base_url")
        active = self.providers.providers.get(self.active_provider_name)
        if active and not active.ready and active.type != "mock":
            problems.append(f"当前激活的 [{active.name}] 配置不完整，调用会失败（可在设置页切换或补全）")
        return problems

    def save_providers(self, target_dir: Path | None = None) -> Path:
        """把当前 provider 配置写回用户配置目录（含明文 Key，故只写用户目录）。"""
        directory = ensure_dir(target_dir or user_config_dir())
        path = directory / PROVIDERS_FILE
        payload = self.providers.model_dump(mode="json", exclude_none=True)
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        return path

    def save_settings(self, target_dir: Path | None = None) -> Path:
        directory = ensure_dir(target_dir or user_config_dir())
        path = directory / SETTINGS_FILE
        payload = self.settings.model_dump(mode="json", exclude={"loaded_from"}, exclude_none=True)
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        return path

    def describe(self) -> dict[str, Any]:
        """供界面「设置」页展示的概览。"""
        active = self.providers.providers.get(self.active_provider_name)
        return {
            "配置文件": self.settings.loaded_from or ["（使用内置默认值）"],
            "当前模型": f"{active.name} / {active.model}" if active else "未配置",
            "模型协议": active.type if active else "-",
            "适配路径": (active.adaptation_path.value if active and active.adaptation_path else "-"),
            "本地部署": "是" if active and active.is_local else "否",
            "数据目录": str(self.settings.resolved_data_dir()),
            "数据库": str(self.settings.resolved_db_path()),
            "规则目录": str(self.settings.resolved_rules_dir()),
            "知识库目录": str(self.settings.resolved_knowledge_dir()),
            "强制人工复核": "开启" if self.settings.review.require_human_review else "关闭",
            "允许云端模型": "允许" if self.settings.security.allow_cloud_providers else "禁止（仅本地）",
            "数据分级": self.settings.security.data_classification,
            "配置问题": self.validate() or ["无"],
        }


def _search_dirs() -> list[Path]:
    from .paths import config_search_paths

    return config_search_paths()


def _normalize_providers(raw: dict[str, Any]) -> dict[str, Any]:
    """兼容两种写法：``providers: {name: {...}}`` 与 ``providers: [{name: x, ...}]``。"""
    data = copy.deepcopy(raw)
    providers = data.get("providers")
    if isinstance(providers, list):
        mapping: dict[str, Any] = {}
        for item in providers:
            if not isinstance(item, dict) or "name" not in item:
                raise ConfigError("providers 列表中的每一项都必须包含 name 字段")
            name = str(item["name"])
            mapping[name] = {**item, "name": name}
        data["providers"] = mapping
    elif isinstance(providers, dict):
        for name, item in providers.items():
            if isinstance(item, dict):
                item.setdefault("name", name)
    return data


def _default_providers() -> ProvidersConfig:
    """零配置也能启动：内置 mock provider（离线、确定性输出）。"""
    return ProvidersConfig(
        active="mock",
        fallback=[],
        providers={
            "mock": ProviderConfig(
                name="mock",
                type="mock",
                model="mock-rules-v1",
                description="离线模拟模型（不联网，用于演示与自检）",
                is_local=True,
                adaptation_path=AdaptationPath.PROMPT,
            )
        },
    )


# ================================================================ 默认配置文件生成

DEFAULT_SETTINGS_YAML = """\
# 高校科研经费风险智能识别系统 —— 应用设置
# 说明：本文件为示例模板。实际使用时复制为 settings.yaml 后再修改。
# 带 ${VAR} 的写法会读取同名环境变量，避免把敏感信息写进文件。

app_name: 高校科研经费风险智能识别系统
app_version: "0.1.0"

# 留空表示使用系统用户数据目录（Windows: %LOCALAPPDATA%\\FundRiskSentinel）
data_dir: ""
db_path: ""
log_dir: ""
rules_dir: ""
knowledge_dir: ""
export_dir: ""

ui:
  theme: light
  language: zh-CN
  page_size: 50
  show_raw_json: false
  font_size: 10

review:
  # 留空则使用 providers.yaml 中的 active
  default_provider: ""
  max_materials_per_task: 50
  max_chars_per_material: 60000
  total_prompt_char_budget: 240000
  enable_rule_engine: true
  enable_rag: true
  enable_llm_reasoning: true
  rag_top_k: 6
  # 治理边界：强制把输出标记为待人工复核，程序不下合规结论
  require_human_review: true
  min_confidence_to_report: 0.35
  # 模型不可用时降级为纯规则结果，并在结论中明确标注
  degrade_on_llm_failure: true
  temperature: 0.1
  max_tokens: 4096

security:
  # 设为 false 则只允许本地模型（材料不出本机）
  allow_cloud_providers: true
  redact_id_card: true
  redact_phone: true
  redact_bank_account: true
  redact_email: true
  redact_person_name: false
  retention_days: 1825
  audit_log_enabled: true
  data_classification: 内部
  mask_api_key_in_ui: true
"""

DEFAULT_PROVIDERS_YAML = """\
# 模型服务配置 —— 程序不绑定任何厂商，按需增删
#
# type 取值：
#   openai_compatible —— 任何兼容 OpenAI /chat/completions 的服务
#                        （DeepSeek、通义千问、Kimi、智谱、OpenAI、Ollama、vLLM、LM Studio…）
#   anthropic         —— Claude 系列
#   gemini            —— Google Gemini
#   mock              —— 离线模拟，用于无网络自检
#
# api_key 支持 ${ENV_VAR} 写法，推荐把 Key 放进系统环境变量，不要写进本文件。

active: mock
fallback: []

providers:

  # ---------------- 离线自检（默认激活） ----------------
  mock:
    type: mock
    model: mock-rules-v1
    description: 离线模拟模型（不联网，用于演示与自检）
    is_local: true

  # ---------------- DeepSeek ----------------
  deepseek:
    type: openai_compatible
    enabled: false
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_API_KEY}
    model: deepseek-chat
    temperature: 0.1
    max_tokens: 4096
    timeout: 120
    supports_json_mode: true
    description: DeepSeek（性价比高，中文财务语境表现好）
    adaptation_path: prompt

  # ---------------- 通义千问（阿里云百炼） ----------------
  qwen:
    type: openai_compatible
    enabled: false
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: ${DASHSCOPE_API_KEY}
    model: qwen-plus
    description: 通义千问（国内合规，支持长上下文）
    adaptation_path: rag

  # ---------------- Kimi（月之暗面） ----------------
  kimi:
    type: openai_compatible
    enabled: false
    base_url: https://api.moonshot.cn/v1
    api_key: ${MOONSHOT_API_KEY}
    model: moonshot-v1-32k
    description: Kimi（长文档处理能力强）

  # ---------------- 智谱 GLM ----------------
  zhipu:
    type: openai_compatible
    enabled: false
    base_url: https://open.bigmodel.cn/api/paas/v4
    api_key: ${ZHIPU_API_KEY}
    model: glm-4-plus
    description: 智谱 GLM

  # ---------------- OpenAI ----------------
  openai:
    type: openai_compatible
    enabled: false
    base_url: https://api.openai.com/v1
    api_key: ${OPENAI_API_KEY}
    model: gpt-4o-mini
    description: OpenAI（境外服务，注意数据合规）

  # ---------------- Anthropic Claude ----------------
  claude:
    type: anthropic
    enabled: false
    api_key: ${ANTHROPIC_API_KEY}
    model: claude-3-5-sonnet-latest
    description: Anthropic Claude（境外服务，注意数据合规）

  # ---------------- Google Gemini ----------------
  gemini:
    type: gemini
    enabled: false
    api_key: ${GEMINI_API_KEY}
    model: gemini-1.5-pro
    description: Google Gemini（境外服务，注意数据合规）

  # ---------------- 本地 Ollama（数据不出本机） ----------------
  ollama:
    type: openai_compatible
    enabled: false
    base_url: http://127.0.0.1:11434/v1
    api_key: ollama
    model: qwen2.5:7b-instruct
    is_local: true
    description: 本地 Ollama（材料完全不出本机）
    adaptation_path: peft

  # ---------------- 本地 vLLM / 微调模型 ----------------
  local_finetuned:
    type: openai_compatible
    enabled: false
    base_url: http://127.0.0.1:8000/v1
    api_key: EMPTY
    model: fund-risk-lora-qwen2.5-7b
    is_local: true
    description: 本地 LoRA/QLoRA 微调模型（课题四路径之参数高效微调）
    adaptation_path: peft
"""


def write_default_configs(target_dir: Path | str | None = None, *, overwrite: bool = False) -> list[Path]:
    """把示例配置写到目标目录（默认项目 config/），返回写入的文件列表。"""
    directory = ensure_dir(Path(target_dir) if target_dir else project_root() / "config")
    written: list[Path] = []
    for filename, content in (
        (SETTINGS_FILE, DEFAULT_SETTINGS_YAML),
        (PROVIDERS_FILE, DEFAULT_PROVIDERS_YAML),
    ):
        path = directory / filename
        if path.exists() and not overwrite:
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


# ================================================================ 单例

_MANAGER: ConfigManager | None = None


def get_manager(reload: bool = False, config_dir: Path | str | None = None) -> ConfigManager:
    """获取全局配置管理器（进程内单例）。"""
    global _MANAGER
    if _MANAGER is None or reload:
        _MANAGER = ConfigManager.load(config_dir)
        _MANAGER.settings.ensure_dirs()
    return _MANAGER


def set_manager(manager: ConfigManager) -> None:
    global _MANAGER
    _MANAGER = manager


__all__ = [
    "SETTINGS_FILE",
    "PROVIDERS_FILE",
    "ProviderConfig",
    "ProvidersConfig",
    "UiSettings",
    "ReviewSettings",
    "SecuritySettings",
    "AppSettings",
    "ConfigManager",
    "get_manager",
    "set_manager",
    "interpolate_env",
    "write_default_configs",
    "DEFAULT_SETTINGS_YAML",
    "DEFAULT_PROVIDERS_YAML",
    "bundled_dir",
]
