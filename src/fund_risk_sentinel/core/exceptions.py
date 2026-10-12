"""项目自定义异常。

分层原则：越靠近底层的模块抛越具体的异常，编排层统一捕获并转为
``StageStatus.DEGRADED`` 的降级结果，保证单点故障不会中断整条审查流水线。
"""

from __future__ import annotations


class SentinelError(Exception):
    """所有自定义异常的基类。"""


# ---------------------------------------------------------------- 配置

class ConfigError(SentinelError):
    """配置文件缺失、格式错误或必填项为空。"""


class ProviderNotConfiguredError(ConfigError):
    """请求的模型 provider 未在配置中定义或未填写 API Key。"""


# ---------------------------------------------------------------- 模型接入

class LLMError(SentinelError):
    """模型调用相关的基类异常。"""


class LLMConnectionError(LLMError):
    """网络不可达、超时、DNS 失败等。"""


class LLMAuthError(LLMError):
    """API Key 无效、无权限或额度不足。"""


class LLMRateLimitError(LLMError):
    """触发限流。"""


class LLMResponseError(LLMError):
    """返回体无法解析、结构不符合约定、或模型拒答。"""


class LLMUnavailableError(LLMError):
    """provider 未安装依赖 / 本地服务未启动（如 Ollama 未运行）。"""


# ---------------------------------------------------------------- 材料解析

class IngestError(SentinelError):
    """材料读取与解析相关的基类异常。"""


class UnsupportedFormatError(IngestError):
    """没有匹配的 loader。"""


class CorruptedFileError(IngestError):
    """文件损坏、加密或无法打开。"""


class EmptyContentError(IngestError):
    """文件可打开但未提取到任何有效文本/表格内容。"""


# ---------------------------------------------------------------- 规则与知识库

class RuleError(SentinelError):
    """规则定义或加载错误。"""


class RuleSyntaxError(RuleError):
    """规则条件表达式语法错误。"""


class KnowledgeBaseError(SentinelError):
    """制度知识库构建或检索错误。"""


# ---------------------------------------------------------------- 流水线

class PipelineError(SentinelError):
    """审查流水线编排错误。"""


class StageAbortedError(PipelineError):
    """前置阶段失败导致当前阶段无法继续。"""


# ---------------------------------------------------------------- 存储

class StorageError(SentinelError):
    """数据库读写或文件落盘错误。"""


class AuditError(StorageError):
    """审计日志写入失败——此异常不应被静默吞掉。"""


__all__ = [
    "SentinelError",
    "ConfigError",
    "ProviderNotConfiguredError",
    "LLMError",
    "LLMConnectionError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMUnavailableError",
    "IngestError",
    "UnsupportedFormatError",
    "CorruptedFileError",
    "EmptyContentError",
    "RuleError",
    "RuleSyntaxError",
    "KnowledgeBaseError",
    "PipelineError",
    "StageAbortedError",
    "StorageError",
    "AuditError",
]
