"""提示模板与指令工程——课题"四类适配路径"中的第三类。

集中管理所有送模型的提示词，好处有三：

1. **可版本化**：提示词是影响输出质量的关键资产，集中放便于评审与回归对比；
2. **可审计**：每条风险线索都能回溯到当时使用的提示版本；
3. **治理边界写进提示**：把"不下合规结论""必须给依据""必须列不确定点"这些
   要求固化在系统提示里，而不是依赖调用方记得加。
"""

from .common import (
    GOVERNANCE_GUARDRAIL,
    JSON_OUTPUT_RULE,
    PROMPT_VERSION,
    SYSTEM_PREAMBLE,
    build_material_block,
)
from .explanation import EXPLANATION_SYSTEM, build_explanation_user
from .extraction import EXTRACTION_SYSTEM, build_extraction_user
from .reasoning import REASONING_SYSTEM, build_reasoning_user

__all__ = [
    "PROMPT_VERSION",
    "SYSTEM_PREAMBLE",
    "GOVERNANCE_GUARDRAIL",
    "JSON_OUTPUT_RULE",
    "build_material_block",
    "EXTRACTION_SYSTEM",
    "build_extraction_user",
    "REASONING_SYSTEM",
    "build_reasoning_user",
    "EXPLANATION_SYSTEM",
    "build_explanation_user",
]
