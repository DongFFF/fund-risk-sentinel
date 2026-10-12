"""材料关键要素的正则模式与基础抽取。

放在 ``core`` 层是为了让两个使用方共享同一套口径，避免"模型离线模拟"与
"规则兜底抽取"识别出不同的字段——那会让一致性评测失去意义。

使用方：
- ``extract.extractor`` 的规则兜底路径（模型不可用时的底线能力）
- ``extract.extractor`` 的交叉校验路径（与模型输出比对，差异即线索）
- ``llm.mock`` 的离线模拟路径
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

#: 金额前的引导词
AMOUNT_KEYS = r"(?:价税合计|合计金额|报销金额|申请金额|合同金额|发票金额|借款金额|金额|总额|小写)"

#: 字段名 → 候选正则（按顺序尝试，先命中先用）
FIELD_PATTERNS: dict[str, list[str]] = {
    "project_name": [
        r"项目名称[：:\s]*([^\n，,；;|]{2,50})",
        r"课题名称[：:\s]*([^\n，,；;|]{2,50})",
        r"项目[：:\s]+([^\n，,；;|]{2,50})",
    ],
    "project_code": [
        r"(?:项目编号|项目号|课题编号|经费卡号|项目代码)[：:\s]*([A-Za-z0-9\-—_/.（）()]{3,40})"
    ],
    "budget_subject": [
        r"(?:经费科目|预算科目|开支科目|科目名称|费用类别|支出科目)[：:\s]*([^\n，,；;|]{2,40})"
    ],
    # 优先取"余额"类字段：规则关心的是"还能花多少"，而不是"原本批了多少"
    "budget_amount": [
        r"(?:预算余额|科目余额|可用余额|剩余额度|预算剩余)[：:\s]*(?:人民币|RMB|￥|¥)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"(?:预算金额|预算额度|预算数|批复金额)[：:\s]*(?:人民币|RMB|￥|¥)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    ],
    "contract_amount": [
        r"(?:合同金额|合同价款|协议金额)[：:\s]*(?:人民币|RMB|￥|¥)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    ],
    "supplier": [
        r"(?:供应商|销售方|开票方|收款方|受托方|服务方|单位名称)[：:\s]*([^\n，,；;|]{2,60})"
    ],
    "applicant": [r"(?:报销人|申请人|经办人|填报人)[：:\s]*([^\n，,；;|]{2,20})"],
    "approver": [r"(?:审批人|审核人|项目负责人|批准人|签批人)[：:\s]*([^\n，,；;|]{2,20})"],
    "approver_level": [
        r"(?:审批层级|审批级别|审批权限)[：:\s]*([^\n，,；;|]{2,30})"
    ],
    "invoice_no": [r"(?:发票号码|发票号|票据号码)[：:\s]*([0-9A-Za-z]{6,32})"],
    "contract_no": [r"(?:合同编号|合同号|协议编号)[：:\s]*([0-9A-Za-z\-—_/]{3,40})"],
    "purpose": [r"(?:事由|用途|开支内容|报销事由|摘要|费用内容)[：:\s]*([^\n，,；;|]{2,80})"],
    "event_date": [r"(?:业务日期|发生日期|开票日期|报销日期|申请日期)[：:\s]*(\d{4}[-年/\.]\d{1,2}[-月/\.]\d{1,2})"],
}

#: 金额：优先取带引导词的
AMOUNT_PATTERN = re.compile(
    AMOUNT_KEYS + r"[：:\s]*(?:人民币|RMB|￥|¥|\$)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
)
#: 兜底：任何带货币符号的数字
ANY_AMOUNT_PATTERN = re.compile(r"(?:人民币|RMB|￥|¥)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
#: 日期
DATE_PATTERN = re.compile(r"(20\d{2})\s*[-年/\.]\s*(\d{1,2})\s*[-月/\.]\s*(\d{1,2})")
#: 金额中的数字（去千分位）
_NUMERIC = re.compile(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?")

#: 附件齐全性判定：附件名 → 出现即视为已提供的关键词
ATTACHMENT_HINTS: dict[str, tuple[str, ...]] = {
    "发票": ("发票", "票据", "invoice"),
    "合同": ("合同", "协议"),
    "验收单": ("验收",),
    "审批单": ("审批", "批准", "签批", "审核意见"),
    "预算书": ("预算",),
    "结算单": ("结算",),
    "签收单": ("签收", "领用"),
}

#: 默认期望的附件清单（可按事项类型细化）
DEFAULT_EXPECTED_ATTACHMENTS: dict[str, tuple[str, ...]] = {
    "reimbursement": ("发票", "合同", "审批单"),
    "contract": ("合同", "审批单"),
    "advance": ("审批单",),
    "procurement": ("合同", "发票", "验收单", "审批单"),
    "transfer": ("合同", "审批单"),
    "settlement": ("结算单", "验收单"),
    "budget": ("预算书",),
    "other": ("发票",),
}


def to_number(value: Any) -> float | None:
    """把带千分位/货币符号的字符串转为 float；失败返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    match = _NUMERIC.search(text.replace("，", ","))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def normalize_date(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def parse_date(text: str) -> date | None:
    """从文本中解析第一个日期。"""
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(normalize_date(*match.groups()), "%Y-%m-%d").date()
    except ValueError:
        return None


def context_snippet(text: str, start: int, end: int, window: int = 40) -> str:
    """截取命中位置前后的上下文，供证据定位使用。"""
    left = max(0, start - window)
    right = min(len(text), end + window)
    return text[left:right].replace("\n", " ").strip()


def find_attachments(text: str) -> tuple[list[str], list[str]]:
    """扫描文本，返回 ``(检出的附件名列表, 未检出的附件名列表)``。"""
    found: list[str] = []
    missing: list[str] = []
    for name, hints in ATTACHMENT_HINTS.items():
        if any(hint.lower() in text.lower() for hint in hints):
            found.append(name)
        else:
            missing.append(name)
    return found, missing


def extract_fields(text: str) -> dict[str, dict[str, Any]]:
    """规则抽取：返回 ``{字段名: {value, raw, confidence, locator}}``。

    置信度语义：正则命中位置越明确、格式越规整，置信度越高（0.6~0.9）。
    规则抽取天然低于模型抽取，因此这里不会给出 0.9 以上的值——
    让"哪个字段可信"这件事在数据层面就区分开。
    """
    fields: dict[str, dict[str, Any]] = {}
    for name, patterns in FIELD_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            raw_value = match.group(1).strip()
            value = raw_value
            confidence = 0.75
            if name in ("budget_amount", "contract_amount"):
                number = to_number(raw_value)
                if number is None:
                    continue
                value = f"{number:.2f}"
                confidence = 0.8
            elif name == "event_date":
                parsed = parse_date(raw_value)
                if parsed is None:
                    continue
                value = parsed.isoformat()
                confidence = 0.85
            fields[name] = {
                "value": value,
                "raw": match.group(0).strip(),
                "confidence": confidence,
                "locator": f"字符 {match.start()}-{match.end()}",
            }
            break

    amounts = [m.group(1).replace(",", "") for m in AMOUNT_PATTERN.finditer(text)]
    if not amounts:
        amounts = [m.group(1).replace(",", "") for m in ANY_AMOUNT_PATTERN.finditer(text)]
    if amounts:
        fields.setdefault(
            "amount",
            {
                "value": amounts[0],
                "raw": amounts[0],
                "confidence": 0.7,
                "locator": "金额字段",
            },
        )
        distinct = sorted(set(amounts))
        if len(distinct) > 1:
            fields["amount_secondary"] = {
                "value": distinct[1],
                "raw": distinct[1],
                "confidence": 0.6,
                "locator": "第二处金额",
            }
            fields["amount_all"] = {
                "value": "、".join(distinct),
                "raw": "、".join(distinct),
                "confidence": 0.7,
                "locator": "全部金额",
            }

    dates = DATE_PATTERN.findall(text)
    if dates:
        fields.setdefault(
            "event_date",
            {
                "value": normalize_date(*dates[0]),
                "raw": "-".join(dates[0]),
                "confidence": 0.8,
                "locator": "第一个日期",
            },
        )
        if len(dates) > 1:
            fields["last_date"] = {
                "value": normalize_date(*dates[-1]),
                "raw": "-".join(dates[-1]),
                "confidence": 0.65,
                "locator": "最后一个日期",
            }
    return fields


__all__ = [
    "AMOUNT_KEYS",
    "FIELD_PATTERNS",
    "AMOUNT_PATTERN",
    "ANY_AMOUNT_PATTERN",
    "DATE_PATTERN",
    "ATTACHMENT_HINTS",
    "DEFAULT_EXPECTED_ATTACHMENTS",
    "to_number",
    "normalize_date",
    "parse_date",
    "context_snippet",
    "find_attachments",
    "extract_fields",
]
