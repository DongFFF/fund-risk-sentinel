"""规则引擎：安全表达式求值 + 规则命中判定。

为什么不用 ``eval()``：规则库是 YAML 文件，用户会自己写条件。直接 ``eval`` 意味着
任何拿到规则文件的人都能在这台机器上执行任意代码。这里用一个基于 AST 白名单的
受限求值器——只允许算术、比较、布尔、成员判断和几个纯函数，彻底挡掉属性穿透、
导入、lambda、推导式等逃逸路径。

表达式里可用的变量（由 ``build_facts`` 提供）：

======================  ==========  ================================================
变量                    类型        含义
======================  ==========  ================================================
``amount``              float       本笔业务金额（元）
``budget_amount``       float       对应预算科目额度/余额
``budget_subject``      str         经费科目
``purpose``             str         事由/用途
``event_type``          str         事项类型
``days_after_event``    int         业务发生至办理的间隔天数
``contract_amount``     float       合同金额
``has_contract``        bool        是否有合同
``has_invoice``         bool        是否有发票
``has_approval``        bool        是否有审批
``has_acceptance``      bool        是否有验收材料
``approver_level``      str         审批层级
``supplier``            str         供应商
``invoice_count``       int         发票张数
``material_count``      int         材料份数
``missing_attachments`` list[str]   缺失附件名列表
``text``                str         全部材料正文拼接（供关键词判断）
``fields``              dict        原始抽取字段 ``{名: 值}``
======================  ==========  ================================================

可用函数：``abs min max len round int float str bool sum any all to_number is_empty
contains_any contains_all regex_match days_between``。
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..core.enums import (
    DocumentRole,
    EventType,
    RiskCategory,
    RiskLevel,
    RuleDimension,
)
from ..core.exceptions import RuleSyntaxError
from ..core.models import ClauseRef, ExtractedFields, Material, Rule, RuleHit

logger = logging.getLogger(__name__)


# ================================================================ 安全求值器

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.Attribute,
    ast.Call,
    ast.keyword,
)

#: 允许调用的字符串/字典方法（只读，无副作用）
_ALLOWED_METHODS = {
    "lower", "upper", "strip", "lstrip", "rstrip", "startswith", "endswith",
    "replace", "count", "find", "get", "split", "join", "isdigit", "isalpha",
}


def _to_float_safe(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").replace("，", "").strip())
    except (TypeError, ValueError):
        return default


def _to_int_safe(value: Any, default: int = 0) -> int:
    return int(_to_float_safe(value, default))


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _contains_any(text: Any, *keywords: Any) -> bool:
    haystack = str(text or "")
    return any(str(k) in haystack for k in keywords)


def _contains_all(text: Any, *keywords: Any) -> bool:
    haystack = str(text or "")
    return all(str(k) in haystack for k in keywords)


def _regex_match(pattern: Any, text: Any) -> bool:
    try:
        return re.search(str(pattern), str(text or "")) is not None
    except re.error:
        return False


def _days_between(start: Any, end: Any) -> int:
    from datetime import date, datetime

    def _parse(value: Any) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y%m%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return 0
    return (b - a).days


#: 允许直接调用的函数（定义顺序在辅助函数之后，避免导入期 NameError）
_ALLOWED_FUNCS = {
    "abs": abs,
    "min": min,
    "max": max,
    "len": len,
    "round": round,
    "int": _to_int_safe,
    "float": _to_float_safe,
    "str": str,
    "bool": bool,
    "sum": sum,
    "any": any,
    "all": all,
    "to_number": _to_float_safe,
    "is_empty": _is_empty,
    "contains_any": _contains_any,
    "contains_all": _contains_all,
    "regex_match": _regex_match,
    "days_between": _days_between,
}


def safe_eval(expression: str, variables: dict[str, Any]) -> Any:
    """在受限沙箱中求值表达式。语法非法或用了不允许的语法时抛 ``RuleSyntaxError``。"""
    if not expression or not expression.strip():
        return False
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RuleSyntaxError(f"规则条件语法错误：{expression!r} → {exc.msg}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleSyntaxError(
                f"规则条件使用了不允许的语法：{type(node).__name__}（表达式：{expression!r}）"
            )
        if isinstance(node, ast.Attribute):
            # 只允许 .方法名 形式，且方法名在只读白名单内；禁止属性穿透（如 a.b.c）
            if node.attr not in _ALLOWED_METHODS:
                raise RuleSyntaxError(
                    f"规则条件访问了不允许的属性 .{node.attr}（表达式：{expression!r}）。"
                    f"可用方法：{', '.join(sorted(_ALLOWED_METHODS))}"
                )
            if not isinstance(node.value, (ast.Name, ast.Subscript, ast.Call)):
                raise RuleSyntaxError(
                    f"规则条件中出现了不允许的属性链（表达式：{expression!r}）"
                )
        if isinstance(node, ast.Name) and node.id not in variables and node.id not in _ALLOWED_FUNCS:
            # 未定义变量按 None 处理会让规则静默失效，这里明确报错更安全
            raise RuleSyntaxError(
                f"规则条件引用了未知变量 {node.id!r}（表达式：{expression!r}）。"
                f"可用变量：{', '.join(sorted(variables))}"
            )
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id not in _ALLOWED_FUNCS:
                    raise RuleSyntaxError(f"规则条件调用了不允许的函数 {func.id!r}")
            elif isinstance(func, ast.Attribute):
                if func.attr not in _ALLOWED_METHODS:
                    raise RuleSyntaxError(f"规则条件调用了不允许的方法 .{func.attr}()")
            else:
                raise RuleSyntaxError("规则条件中出现了不允许的调用形式")

    scope = dict(variables)
    scope.update(_ALLOWED_FUNCS)
    try:
        return eval(compile(tree, "<rule>", "eval"), {"__builtins__": {}}, scope)  # noqa: S307
    except RuleSyntaxError:
        raise
    except Exception as exc:  # noqa: BLE001 - 求值期异常（如 None 比较）转为规则错误
        raise RuleSyntaxError(f"规则条件求值失败：{expression!r} → {type(exc).__name__}: {exc}") from exc


#: 校验规则表达式时使用的变量样本。
#: 必须与 ``build_facts`` 的返回值保持同构——否则会把合法规则误判为"引用了未知变量"。
PROBE_FACTS: dict[str, Any] = {
    "amount": 1.0,
    "budget_amount": 1.0,
    "budget_subject": "",
    "purpose": "",
    "event_type": "",
    "days_after_event": 0,
    "contract_amount": 0.0,
    "has_contract": False,
    "has_invoice": False,
    "has_approval": False,
    "has_acceptance": False,
    "approver_level": "",
    "supplier": "",
    "invoice_count": 0,
    "material_count": 0,
    "missing_attachments": [],
    "text": "",
    "fields": {},
    "project_name": "",
    "project_code": "",
    "applicant": "",
    "approver": "",
}


def validate_condition(expression: str, variables: dict[str, Any] | None = None) -> list[str]:
    """校验表达式合法性，返回问题列表（空表示通过）。"""
    probe = variables if variables is not None else dict(PROBE_FACTS)
    try:
        safe_eval(expression, probe)
    except RuleSyntaxError as exc:
        return [str(exc)]
    return []


# ================================================================ 事实构建

def build_facts(
    extracted: ExtractedFields | None,
    materials: Iterable[Material] = (),
    *,
    event_type: EventType | None = None,
) -> dict[str, Any]:
    """把抽取结果与材料集合整理成规则可用的变量字典。"""
    material_list = list(materials)
    ext = extracted or ExtractedFields()

    raw_fields: dict[str, str] = {name: f.value for name, f in ext.fields.items()}
    text = "\n".join(m.text for m in material_list)

    amount = _to_float_safe(raw_fields.get("amount"), 0.0)
    budget_amount = _to_float_safe(
        raw_fields.get("budget_amount") or raw_fields.get("budget_balance"), 0.0
    )
    contract_amount = _to_float_safe(raw_fields.get("contract_amount"), 0.0)

    roles = {m.role for m in material_list}
    present_attachments = {a for a in ext.attachments_present}

    has_contract = DocumentRole.CONTRACT in roles or "合同" in present_attachments
    has_invoice = DocumentRole.INVOICE in roles or "发票" in present_attachments
    has_approval = DocumentRole.APPROVAL in roles or "审批单" in present_attachments
    has_acceptance = DocumentRole.ACCEPTANCE in roles or "验收单" in present_attachments

    invoice_count = sum(1 for m in material_list if m.role == DocumentRole.INVOICE)
    if invoice_count == 0 and "invoice_count" in raw_fields:
        invoice_count = _to_int_safe(raw_fields["invoice_count"])

    # 业务发生至办理的间隔天数：优先取抽取结果，其次由"业务日期 → 最后日期/今天"推算
    days_after_event = _to_int_safe(raw_fields.get("days_after_event"), 0)
    if days_after_event <= 0:
        start = raw_fields.get("event_date") or raw_fields.get("invoice_date")
        end = raw_fields.get("last_date") or raw_fields.get("submit_date")
        if start and end:
            days_after_event = abs(_days_between(start, end))
        elif start:
            from datetime import date

            days_after_event = abs(_days_between(start, date.today().isoformat()))

    return {
        "amount": amount,
        "budget_amount": budget_amount,
        "budget_subject": raw_fields.get("budget_subject", ""),
        "purpose": raw_fields.get("purpose", ""),
        "event_type": (event_type or EventType.OTHER).value,
        "days_after_event": days_after_event,
        "contract_amount": contract_amount,
        "has_contract": has_contract,
        "has_invoice": has_invoice,
        "has_approval": has_approval,
        "has_acceptance": has_acceptance,
        "approver_level": raw_fields.get("approver_level", ""),
        "supplier": raw_fields.get("supplier", ""),
        "invoice_count": invoice_count,
        "material_count": len(material_list),
        "missing_attachments": list(ext.missing_attachments),
        "text": text,
        "fields": raw_fields,
        # 便于规则写 `project_name == "..."` 之类的判断
        "project_name": raw_fields.get("project_name", ""),
        "project_code": raw_fields.get("project_code", ""),
        "applicant": raw_fields.get("applicant", ""),
        # 与 applicant 成对：ATH-002「审批人与经办人为同一人」需要比较两者
        "approver": raw_fields.get("approver", ""),
    }


# ================================================================ 规则引擎

@dataclass
class RuleEngineStats:
    total: int = 0
    enabled: int = 0
    evaluated: int = 0
    hits: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class RuleEngine:
    """结构化规则引擎——课题"四类适配路径"中的基线路径。"""

    def __init__(self, rules: Iterable[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules or [])
        self.stats = RuleEngineStats()

    # ---------------------------------------------------------- 规则管理

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    def add_rules(self, rules: Iterable[Rule]) -> None:
        existing = {r.rule_id for r in self._rules}
        for rule in rules:
            if rule.rule_id in existing:
                logger.warning("规则 ID 重复，已跳过：%s", rule.rule_id)
                continue
            self._rules.append(rule)
            existing.add(rule.rule_id)

    def clear(self) -> None:
        self._rules.clear()

    def enabled_rules(self) -> list[Rule]:
        return [r for r in self._rules if r.enabled]

    def by_dimension(self) -> dict[RuleDimension, list[Rule]]:
        out: dict[RuleDimension, list[Rule]] = {}
        for rule in self._rules:
            out.setdefault(rule.dimension, []).append(rule)
        return out

    def validate(self) -> list[str]:
        """逐条校验规则条件，返回问题列表。"""
        problems: list[str] = []
        for rule in self._rules:
            for issue in validate_condition(rule.condition):
                problems.append(f"[{rule.rule_id} {rule.name}] {issue}")
        return problems

    # ---------------------------------------------------------- 判定

    def evaluate(
        self,
        facts: dict[str, Any],
        *,
        event_type: EventType | None = None,
        dimensions: Iterable[RuleDimension] | None = None,
    ) -> list[RuleHit]:
        """对给定事实执行全部启用规则，返回命中列表。"""
        wanted = set(dimensions) if dimensions else None
        effective_event = event_type or EventType.from_value(facts.get("event_type"), EventType.OTHER)
        hits: list[RuleHit] = []
        self.stats = RuleEngineStats(total=len(self._rules), enabled=len(self.enabled_rules()))

        for rule in self._rules:
            if not rule.enabled:
                continue
            if wanted and rule.dimension not in wanted:
                continue
            if rule.event_types and effective_event not in rule.event_types:
                continue
            self.stats.evaluated += 1
            if not rule.condition.strip():
                continue
            try:
                matched = bool(safe_eval(rule.condition, facts))
            except RuleSyntaxError as exc:
                self.stats.errors.append(f"[{rule.rule_id} {rule.name}] {exc}")
                logger.warning("规则 %s 求值失败：%s", rule.rule_id, exc)
                continue
            if not matched:
                continue
            hits.append(
                RuleHit(
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    dimension=rule.dimension,
                    requirement=rule.requirement,
                    risk_category=rule.risk_category,
                    level_hint=rule.level_hint,
                    disposition_hint=rule.disposition_hint,
                    suggestion=rule.suggestion,
                    matched_expression=rule.condition,
                    evaluated_values=_snapshot(facts, rule.condition),
                    basis=rule.basis,
                    weight=rule.weight,
                )
            )

        # 命中按等级与权重排序，重要的排前面
        hits.sort(key=lambda h: (_level_weight(h.level_hint), h.weight), reverse=True)
        self.stats.hits = len(hits)
        return hits

    def describe(self) -> dict[str, Any]:
        by_dim = {k.value: len(v) for k, v in self.by_dimension().items()}
        return {
            "total": len(self._rules),
            "enabled": len(self.enabled_rules()),
            "by_dimension": by_dim,
            "by_category": _count_by(self._rules, lambda r: r.risk_category.value),
            "by_source": _count_by(self._rules, lambda r: r.source),
        }


def _level_weight(level: RiskLevel) -> int:
    from ..core.enums import RISK_LEVEL_WEIGHT

    return RISK_LEVEL_WEIGHT.get(level, 0)


def _count_by(items: Iterable[Any], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        k = key(item)
        out[k] = out.get(k, 0) + 1
    return out


def _snapshot(facts: dict[str, Any], expression: str) -> dict[str, Any]:
    """记录参与本次判定的变量值，便于审核人回看"为什么触发了"。"""
    names = {node.id for node in ast.walk(ast.parse(expression, mode="eval")) if isinstance(node, ast.Name)}
    snapshot: dict[str, Any] = {}
    for name in sorted(names):
        if name in facts:
            value = facts[name]
            if name == "text":
                continue  # 全文太长，不入快照
            if isinstance(value, (str, int, float, bool)) or value is None:
                snapshot[name] = value
            elif isinstance(value, (list, tuple)):
                snapshot[name] = list(value)[:20]
            elif isinstance(value, dict):
                snapshot[name] = dict(list(value.items())[:20])
    return snapshot


def rule_from_dict(data: dict[str, Any]) -> Rule:
    """从 YAML 字典构造 ``Rule``，兼容宽松写法。"""
    payload = dict(data)
    if "id" in payload and "rule_id" not in payload:
        payload["rule_id"] = payload.pop("id")
    if "level" in payload and "level_hint" not in payload:
        payload["level_hint"] = payload.pop("level")
    if "category" in payload and "risk_category" not in payload:
        payload["risk_category"] = payload.pop("category")
    if "disposition" in payload and "disposition_hint" not in payload:
        payload["disposition_hint"] = payload.pop("disposition")

    payload["dimension"] = RuleDimension.from_value(payload.get("dimension"), RuleDimension.PROCEDURE)
    payload["risk_category"] = RiskCategory.from_value(
        payload.get("risk_category"), RiskCategory.DEVIATION
    )
    payload["level_hint"] = RiskLevel.from_value(payload.get("level_hint"), RiskLevel.MEDIUM)
    from ..core.enums import DispositionType

    payload["disposition_hint"] = DispositionType.from_value(
        payload.get("disposition_hint"), DispositionType.PENDING_REVIEW
    )
    payload["event_types"] = [
        e for e in (EventType.from_value(x) for x in (payload.get("event_types") or [])) if e
    ]

    basis_raw = payload.get("basis") or []
    if isinstance(basis_raw, str):
        basis_raw = [{"doc_title": basis_raw}]
    payload["basis"] = [ClauseRef.model_validate(b) for b in basis_raw if isinstance(b, dict)]

    return Rule.model_validate(payload)


__all__ = [
    "safe_eval",
    "validate_condition",
    "build_facts",
    "RuleEngine",
    "RuleEngineStats",
    "rule_from_dict",
]
