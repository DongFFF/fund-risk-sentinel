#!/usr/bin/env python
"""命令行审查演示：对样例材料跑一次完整审查，输出 Markdown 底稿。

这个脚本的价值在于**脱离界面验证能力**：同样的材料，界面里能得到什么，
命令行里就能得到什么（两者调用的是同一套 ``ReviewPipeline``）。

默认使用 ``mock`` 离线模型，不需要任何 API Key；带上 ``--provider`` 就换成
真实模型（需先在 ``config/providers.yaml`` 里配好）。

用法::

    # 离线跑一份样例（默认）
    python examples/run_review_demo.py

    # 指定材料
    python examples/run_review_demo.py --file examples/样例-差旅报销单.txt \\
                                       --file examples/样例-会议服务发票.txt

    # 用真实模型（示例：deepseek）
    python examples/run_review_demo.py --provider deepseek --save out/审查底稿.md

    # 指定事项类型
    python examples/run_review_demo.py --event-type reimbursement
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fund_risk_sentinel.config import ConfigManager, get_manager, set_manager  # noqa: E402
from fund_risk_sentinel.core.enums import (  # noqa: E402
    RISK_LEVEL_LABELS,
    EventType,
    RiskLevel,
)
from fund_risk_sentinel.explain import render_markdown  # noqa: E402
from fund_risk_sentinel.llm import LLMClient, set_client  # noqa: E402
from fund_risk_sentinel.review import PipelineOptions, ReviewPipeline  # noqa: E402

DEFAULT_FILES = [
    ROOT / "examples" / "样例-差旅报销单.txt",
    ROOT / "examples" / "样例-会议服务发票.txt",
    ROOT / "examples" / "样例-项目预算执行表.csv",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对样例材料执行一次风险审查")
    parser.add_argument(
        "--file", "-f", action="append", default=None,
        help="待审查材料路径，可重复；默认使用 examples/ 下三份样例",
    )
    parser.add_argument("--provider", "-p", default="mock", help="模型配置名（默认 mock 离线）")
    parser.add_argument(
        "--event-type", "-e", default="reimbursement",
        choices=[e.value for e in EventType],
        help="事项类型（默认 reimbursement）",
    )
    parser.add_argument("--title", "-t", default="样例审查", help="本次审查的标题")
    parser.add_argument("--save", "-s", default="", help="把 Markdown 底稿写到该路径")
    parser.add_argument("--quiet", "-q", action="store_true", help="只输出摘要")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    paths = [Path(p) for p in (args.file or [str(p) for p in DEFAULT_FILES])]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        for p in missing:
            sys.stderr.write(f"材料不存在：{p}\n")
        return 2

    config: ConfigManager = get_manager()
    set_manager(config)

    provider_name = args.provider
    if provider_name not in config.providers.providers:
        sys.stderr.write(
            f"未找到名为 {provider_name!r} 的模型配置。\n"
            f"可用：{', '.join(sorted(config.providers.providers))}\n"
        )
        return 2
    config.providers.active = provider_name

    client = LLMClient(config)
    set_client(client)

    pipeline = ReviewPipeline.from_config(config)
    pipeline.client = client
    pipeline.extractor.client = client
    pipeline.reasoner.client = client
    pipeline.reporter.client = client

    options = PipelineOptions(
        event_type=EventType.from_value(args.event_type),
        provider=provider_name,
    )

    print(f"材料：{len(paths)} 份")
    for p in paths:
        print(f"  · {p.name}")
    print(f"模型：{provider_name}（{config.provider().model or '-'}）")
    print(f"事项：{options.event_type.value}")
    print("-" * 60)

    conclusion = pipeline.run_paths(
        [str(p) for p in paths], title=args.title, options=options
    )

    if not args.quiet:
        print(render_markdown(conclusion))
    else:
        print(conclusion.summary)

    findings = conclusion.findings
    by_level: dict[str, int] = {}
    for f in findings:
        by_level[f.level.value] = by_level.get(f.level.value, 0) + 1

    print("-" * 60)
    print(f"共 {len(findings)} 条风险线索")
    for level in RiskLevel:
        count = by_level.get(level.value, 0)
        if count:
            print(f"  {RISK_LEVEL_LABELS[level]}：{count}")
    if conclusion.degraded_stages:
        print(f"降级阶段：{', '.join(conclusion.degraded_stages)}")
    print(f"用时：{conclusion.duration_ms} ms")

    if args.save:
        out = Path(args.save)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(conclusion), encoding="utf-8")
        print(f"底稿已保存：{out.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
