"""材料角色识别。

证据链要成立，前提是知道每份材料"是什么"。文件名和内容里通常有足够线索，
先按规则自动判定，界面再允许人工纠正——这比要求用户逐个下拉选择省事得多。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.enums import DocumentRole

#: 角色 → (文件名关键词, 内容关键词)，命中任一即计分
_ROLE_KEYWORDS: dict[DocumentRole, tuple[tuple[str, ...], tuple[str, ...]]] = {
    DocumentRole.INVOICE: (
        ("发票", "票据", "invoice", "receipt", "票据", "电子发票"),
        ("发票代码", "发票号码", "价税合计", "销售方", "购买方", "开票日期", "税号"),
    ),
    DocumentRole.CONTRACT: (
        ("合同", "协议", "contract", "agreement", "采购协议"),
        ("甲方", "乙方", "合同编号", "签订地点", "违约责任", "合同金额"),
    ),
    DocumentRole.BUDGET: (
        ("预算", "budget", "经费预算", "预算书", "预算调整"),
        ("预算科目", "预算金额", "预算额度", "预算科目名称", "预算调整"),
    ),
    DocumentRole.APPROVAL: (
        ("审批", "签批", "批复", "approval", "审批单", "意见"),
        ("审批意见", "同意", "批准", "审核人", "签批", "负责人签字"),
    ),
    DocumentRole.ACCEPTANCE: (
        ("验收", "结题", "结项", "验收单", "成果"),
        ("验收结论", "验收合格", "结题报告", "成果交付"),
    ),
    DocumentRole.APPLICATION: (
        ("报销", "申请", "借款", "差旅", "报销单", "申请单", "预支"),
        ("报销单", "报销人", "申请金额", "事由", "借款单", "差旅费"),
    ),
    DocumentRole.POLICY: (
        ("办法", "规定", "制度", "通知", "意见", "细则", "负面清单", "policy", "规程"),
        ("第一条", "第二章", "本办法", "本规定", "本细则", "本意见", "依据", "施行"),
    ),
}

#: 制度文本的强特征：出现这些表述基本可确定是制度而非业务单据
_POLICY_STRONG = re.compile(r"第[一二三四五六七八九十百零\d]+条|本办法|本规定|本细则|自.{0,8}起施行")
#: 条文序号（行首）：用于"条文数量"这一更强的判据
_ARTICLE_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百零〇\d]+条", re.MULTILINE)
#: 达到该条文数量即直接判定为制度文本——业务单据不会按条编号
_POLICY_ARTICLE_THRESHOLD = 3


def infer_role(filename: str, text: str = "") -> DocumentRole:
    """按文件名与正文关键词推断材料角色。"""
    name = Path(filename).stem.lower()
    body = text[:8000]

    # 先看结构特征：按「第X条」分条编号的材料几乎必然是制度文本，优先级高于
    # 关键词打分——否则「报销实施细则」会因为文件名含"报销"被误判为报销单。
    if len(_ARTICLE_HEADING.findall(body)) >= _POLICY_ARTICLE_THRESHOLD:
        return DocumentRole.POLICY

    scores: dict[DocumentRole, float] = {}
    for role, (name_kw, body_kw) in _ROLE_KEYWORDS.items():
        score = 0.0
        for kw in name_kw:
            if kw.lower() in name:
                score += 3.0  # 文件名权重更高
        for kw in body_kw:
            if kw in body:
                score += 1.0
        if score:
            scores[role] = score

    if not scores:
        # 制度文本即使没命中关键词，也用条文结构兜底
        if _POLICY_STRONG.search(body):
            return DocumentRole.POLICY
        return DocumentRole.OTHER

    best = max(scores.items(), key=lambda kv: kv[1])
    # 分数太低（只命中一个泛化内容词）时保守返回 OTHER，避免误判污染证据链
    return best[0] if best[1] >= 2.0 else DocumentRole.OTHER


def describe_role(role: DocumentRole) -> str:
    from ..core.enums import DOCUMENT_ROLE_LABELS

    return DOCUMENT_ROLE_LABELS.get(role, role.value)


__all__ = ["infer_role", "describe_role"]
