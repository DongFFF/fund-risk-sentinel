"""离线冒烟测试：不联网、不依赖任何 API Key，走内置 mock 模型验证全链路。

覆盖范围：
1. 配置加载（含规则库与知识库定位）
2. 材料上传与解析（txt / csv / md，含角色识别与脱敏）
3. 结构化抽取（规则路径 + mock 模型路径 + 交叉校验）
4. 规则引擎求值（安全表达式）
5. 制度知识库检索（BM25 召回）
6. 风险推理与解释生成
7. SQLite 落库与读回
8. 审计日志哈希链校验
9. 六维评测

用法::

    python scripts/smoke_test.py            # 正常输出
    python scripts/smoke_test.py --quiet    # 只输出结果摘要
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fund_risk_sentinel.config import ConfigManager, set_manager  # noqa: E402
from fund_risk_sentinel.core.enums import EventType, HumanVerdict  # noqa: E402
from fund_risk_sentinel.evaluate import Evaluator, format_report  # noqa: E402
from fund_risk_sentinel.explain import render_markdown, render_text  # noqa: E402
from fund_risk_sentinel.llm import LLMClient, set_client  # noqa: E402
from fund_risk_sentinel.review import PipelineOptions, ReviewPipeline  # noqa: E402
from fund_risk_sentinel.rules import KnowledgeBase, RuleEngine, build_facts  # noqa: E402
from fund_risk_sentinel.rules.loader import load_all_rules  # noqa: E402
from fund_risk_sentinel.storage import open_storage  # noqa: E402


class Checker:
    """极简断言收集器：把所有检查跑完再统一汇报，便于一次看到全部问题。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: bool, description: str) -> bool:
        if condition:
            self.passed += 1
            print(f"  [OK]   {description}")
        else:
            self.failed.append(description)
            print(f"  [FAIL] {description}")
        return bool(condition)


def main() -> int:
    parser = argparse.ArgumentParser(description="fund-risk-sentinel 离线冒烟测试")
    parser.add_argument("--quiet", action="store_true", help="只输出最终结果")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录以便排查")
    args = parser.parse_args()

    def log(message: str = "") -> None:
        if not args.quiet:
            print(message)

    checker = Checker()
    workdir = Path(tempfile.mkdtemp(prefix="fund-risk-smoke-"))
    log(f"临时工作目录：{workdir}\n")

    try:
        # ---------------------------------------------------------- 1. 配置
        log("[1/9] 配置加载")
        config = ConfigManager.load(ROOT / "config")
        config.settings.data_dir = str(workdir / "data")
        config.settings.ensure_dirs()
        set_manager(config)
        set_client(LLMClient(config))

        checker.check(config.active_provider_name == "mock", "默认激活离线 mock 模型（零配置可运行）")
        problems = config.validate()
        checker.check(not problems, f"配置校验通过（问题数 {len(problems)}）")
        checker.check(
            config.settings.resolved_rules_dir().is_dir(),
            f"规则目录存在：{config.settings.resolved_rules_dir()}",
        )
        checker.check(
            config.settings.resolved_knowledge_dir().is_dir(),
            f"知识库目录存在：{config.settings.resolved_knowledge_dir()}",
        )
        log()

        # ---------------------------------------------------------- 2. 规则与知识库
        log("[2/9] 规则库与知识库")
        rules = load_all_rules(config.settings.resolved_rules_dir())
        checker.check(len(rules) >= 15, f"加载规则 {len(rules)} 条（期望 >= 15）")
        engine = RuleEngine(rules)
        rule_problems = engine.validate()
        checker.check(not rule_problems, f"全部规则条件表达式合法（问题数 {rule_problems}）")

        from fund_risk_sentinel.rules.knowledge_base import load_knowledge_base

        kb = load_knowledge_base(config.settings.resolved_knowledge_dir())
        checker.check(len(kb) >= 20, f"加载制度条款 {len(kb)} 条（期望 >= 20）")
        hits = kb.search("差旅费 报销 超预算 审批", top_k=5)
        checker.check(len(hits) > 0, f"知识库检索召回 {len(hits)} 条（查询：差旅费 报销 超预算 审批）")
        if hits:
            log(f"        Top1：{hits[0].display()}")
        log()

        # ---------------------------------------------------------- 3. 上传解析
        log("[3/9] 材料上传与解析")
        pipeline = ReviewPipeline.from_config(config)
        samples = sorted((ROOT / "examples").glob("样例-*"))
        checker.check(len(samples) >= 4, f"发现样例材料 {len(samples)} 份")

        ingest_result = pipeline.ingest.load_paths(samples)
        checker.check(not ingest_result.failed, f"全部材料解析成功（{ingest_result.summary()}）")
        roles = {m.role.value for m in ingest_result.materials}
        checker.check("invoice" in roles, f"自动识别出发票类材料（识别到的角色：{sorted(roles)}）")
        checker.check(
            any(m.role.value == "policy" for m in ingest_result.materials),
            "自动识别出制度文本类材料",
        )
        checker.check(
            any(m.redaction_applied for m in ingest_result.materials),
            "至少一份材料触发了脱敏（含手机号/账号类信息）",
        )
        log()

        # ---------------------------------------------------------- 4. 结构化抽取
        log("[4/9] 材料理解与结构化抽取")
        from fund_risk_sentinel.extract import Extractor

        materials_text, _ = pipeline.ingest.assemble_context(ingest_result.materials)
        extraction = Extractor(pipeline.client, config.settings.review).extract(
            ingest_result.materials,
            materials_text=materials_text,
            event_type=EventType.REIMBURSEMENT,
        )
        fields = extraction.fields
        checker.check(fields.get("amount") != "", f"抽取到金额：{fields.get('amount')}")
        checker.check(fields.get("budget_amount") != "", f"抽取到预算余额：{fields.get('budget_amount')}")
        checker.check(fields.get("budget_subject") != "", f"抽取到经费科目：{fields.get('budget_subject')}")
        checker.check(
            fields.get("project_name") != "", f"抽取到项目名称：{fields.get('project_name')}"
        )
        checker.check(
            len(fields.consistency_issues) > 0,
            f"识别到 {len(fields.consistency_issues)} 条一致性疑点",
        )
        log()

        # ---------------------------------------------------------- 5. 规则求值
        log("[5/9] 结构化规则校验")
        facts = build_facts(fields, ingest_result.materials, event_type=EventType.REIMBURSEMENT)
        rule_hits = engine.evaluate(facts, event_type=EventType.REIMBURSEMENT)
        checker.check(len(rule_hits) > 0, f"规则命中 {len(rule_hits)} 条")
        for hit in rule_hits[:5]:
            log(f"        · [{hit.level_hint.value}] {hit.rule_name}（{hit.dimension.value}）")
        log()

        # ---------------------------------------------------------- 6. 完整审查
        log("[6/9] 完整审查流水线（含风险推理与解释）")
        options = PipelineOptions(event_type=EventType.REIMBURSEMENT)
        conclusion = pipeline.run_materials(
            ingest_result.materials,
            title="冒烟测试：差旅费报销审查",
            options=options,
            reviewer="smoke-test",
            ingest_summary=ingest_result.summary(),
            ingest_warnings=ingest_result.warnings,
        )
        checker.check(conclusion.status.value in ("needs_human", "completed"), f"任务状态：{conclusion.status.value}")
        checker.check(len(conclusion.findings) > 0, f"形成 {len(conclusion.findings)} 条风险线索")
        checker.check(bool(conclusion.summary), "生成了结论摘要")
        checker.check(
            len(conclusion.pipeline_trace) >= 6,
            f"流水线记录了 {len(conclusion.pipeline_trace)} 个阶段",
        )
        checker.check(
            all(f.needs_human_review for f in conclusion.findings),
            "全部线索均标记为待人工复核（治理边界要求）",
        )
        with_evidence = sum(1 for f in conclusion.findings if f.evidences)
        checker.check(
            with_evidence == len(conclusion.findings),
            f"全部线索均带证据定位（{with_evidence}/{len(conclusion.findings)}）",
        )
        log()
        log("  审查结论：")
        for line in render_text(conclusion).splitlines():
            log(f"    {line}")
        log()

        # ---------------------------------------------------------- 7. 落库
        log("[7/9] SQLite 落库与读回")
        repo, audit = open_storage(config, db_path=workdir / "smoke.db")
        from fund_risk_sentinel.core.models import ReviewTask

        task = ReviewTask(
            title="冒烟测试：差旅费报销审查",
            event_type=EventType.REIMBURSEMENT,
            materials=ingest_result.materials,
            reviewer="smoke-test",
        )
        repo.save_task(task)
        repo.save_materials(task.task_id, ingest_result.materials)
        conclusion.task_id = task.task_id
        repo.save_conclusion(conclusion)
        task.conclusion = conclusion
        task.status = conclusion.status
        repo.save_task(task)

        reloaded = repo.get_conclusion(conclusion.conclusion_id)
        checker.check(reloaded is not None, "结论可从数据库读回")
        if reloaded:
            checker.check(
                len(reloaded.findings) == len(conclusion.findings),
                f"线索数量一致（{len(reloaded.findings)}）",
            )
            checker.check(reloaded.summary == conclusion.summary, "摘要内容一致")

        loaded_task = repo.get_task(task.task_id)
        checker.check(loaded_task is not None and loaded_task.material_count == len(ingest_result.materials),
                      f"任务与材料读回正常（{loaded_task.material_count if loaded_task else 0} 份材料）")

        # 人工复核回流
        if reloaded and reloaded.findings:
            first = reloaded.findings[0]
            repo.update_finding_verdict(
                first.finding_id, HumanVerdict.ACCEPTED, note="冒烟测试：确认成立", reviewed_by="tester"
            )
            again = repo.get_finding(first.finding_id)
            checker.check(
                again is not None and again.human_verdict == HumanVerdict.ACCEPTED,
                "人工复核结论可写入并读回",
            )
        log()

        # ---------------------------------------------------------- 8. 审计链
        log("[8/9] 审计日志")
        audit.log_task_created(task.task_id, task.title, task.material_count)
        audit.log_conclusion(
            conclusion.conclusion_id,
            task.task_id,
            overall_level=conclusion.overall_level.value,
            finding_count=len(conclusion.findings),
            provider=conclusion.provider,
            model=conclusion.model,
            duration_ms=conclusion.duration_ms,
            degraded_stages=conclusion.degraded_stages,
        )
        ok, message = audit.verify_chain()
        checker.check(ok, f"审计日志哈希链完整：{message}")
        checker.check(len(audit.recent(10)) >= 2, "审计日志可查询")
        log()

        # ---------------------------------------------------------- 9. 评测
        log("[9/9] 六维评测")
        evaluator = Evaluator(repository=repo)
        report = evaluator.evaluate(task_ids=[task.task_id])
        checker.check(len(report.metrics) == 6, f"生成 {len(report.metrics)} 个维度的指标")
        dims = {m.dimension.value for m in report.metrics}
        expected_dims = {"accuracy", "recall", "latency", "consistency", "explainability", "adoption"}
        checker.check(dims == expected_dims, f"六维齐备：{sorted(dims)}")
        log()
        log(format_report(report))
        log()

        # ---------------------------------------------------------- 导出渲染
        md = render_markdown(conclusion)
        checker.check(len(md) > 500, f"Markdown 渲染正常（{len(md)} 字符）")
        export_path = workdir / "conclusion.md"
        export_path.write_text(md, encoding="utf-8")

        # ---------------------------------------------------------- 汇总
        print("=" * 72)
        if checker.failed:
            print(f"冒烟测试未通过：通过 {checker.passed} 项，失败 {len(checker.failed)} 项")
            for item in checker.failed:
                print(f"  - {item}")
            print(f"\n工作目录（保留以便排查）：{workdir}")
            return 1

        print(f"冒烟测试全部通过：{checker.passed} 项检查")
        print(f"结论已导出：{export_path}")
        print(f"数据库：{workdir / 'smoke.db'}")
        print(f"审计日志：{config.settings.resolved_log_dir()}")
        return 0

    except Exception:  # noqa: BLE001 - 冒烟测试需要完整堆栈
        print("\n冒烟测试发生未捕获异常：")
        traceback.print_exc()
        print(f"\n工作目录：{workdir}")
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
