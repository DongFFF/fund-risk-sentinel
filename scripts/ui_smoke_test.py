#!/usr/bin/env python
"""界面冒烟测试：无头环境下实例化全部页面，验证不崩溃。

为什么需要它？界面代码的问题（拼写错误的信号、缺失的导入、Qt API 签名不符）
只有在真正构造控件时才会暴露，静态导入检查发现不了。这个脚本用 Qt 的
``offscreen`` 平台插件在无显示器环境下把 7 个页面全部建出来并跑一遍刷新，
让"界面能不能起来"变成一条可自动执行的检查。

用法::

    python scripts/ui_smoke_test.py           # 正常输出
    python scripts/ui_smoke_test.py --quiet   # 只输出结论

退出码：0 全部通过，1 有失败项。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 必须在导入任何 Qt 模块之前设置，否则无显示器环境会直接失败
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fund_risk_sentinel.config.settings import (  # noqa: E402
    AppSettings,
    ConfigManager,
    ProviderConfig,
    ProvidersConfig,
    set_manager,
)

EXPECTED_PAGES = [
    "dashboard",
    "upload",
    "review",
    "rules",
    "knowledge",
    "evaluation",
    "settings",
]


class Checker:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: bool, description: str) -> bool:
        if condition:
            self.passed += 1
            if not self.quiet:
                print(f"  [OK]   {description}")
        else:
            self.failed.append(description)
            print(f"  [FAIL] {description}")
        return bool(condition)


def build_config(work_dir: Path) -> ConfigManager:
    """构造指向临时目录的配置，避免污染用户真实数据目录。"""
    settings = AppSettings(
        data_dir=str(work_dir / "data"),
        db_path=str(work_dir / "ui-smoke.db"),
        log_dir=str(work_dir / "logs"),
        export_dir=str(work_dir / "exports"),
        rules_dir=str(ROOT / "config" / "rules"),
        knowledge_dir=str(ROOT / "config" / "knowledge"),
    )
    settings.ensure_dirs()
    providers = ProvidersConfig(
        active="mock",
        providers={"mock": ProviderConfig(name="mock", type="mock", model="mock-rules-v1")},
    )
    return ConfigManager(settings, providers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="界面无头冒烟测试")
    parser.add_argument("--quiet", "-q", action="store_true", help="只输出结论")
    args = parser.parse_args(argv)

    ck = Checker(quiet=args.quiet)
    work_dir = Path(tempfile.mkdtemp(prefix="fund-risk-ui-"))
    print(f"临时工作目录：{work_dir}")
    print("-" * 60)

    # ---------------- 依赖
    print("[1] 依赖检查")
    try:
        import PySide6

        ck.check(True, f"PySide6 可用（{PySide6.__version__}）")
    except ImportError as exc:
        print(f"  [FAIL] 未安装 PySide6：{exc}")
        print("\n请先执行：pip install -r requirements.txt")
        return 1

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    ck.check(True, "QApplication 创建成功")

    # ---------------- 配置与上下文
    print("[2] 运行时上下文")
    config = build_config(work_dir)
    set_manager(config)
    ck.check(config.provider().is_local is True, "离线 mock 被正确判定为本地（不误报数据外发）")

    from fund_risk_sentinel.ui.context import AppContext

    context = AppContext.create(config)
    messages = context.reload_pipeline()
    ck.check(context.rule_engine.describe()["enabled"] > 0, f"规则库已加载（{'；'.join(messages)}）")
    ck.check(len(context.knowledge_base) > 0, f"制度知识库已加载（{len(context.knowledge_base)} 条）")

    # ---------------- 主窗口
    print("[3] 主窗口与页面")
    from fund_risk_sentinel.ui.main_window import MainWindow

    window = MainWindow(context)
    ck.check(bool(window.windowTitle()), f"主窗口创建成功（{window.windowTitle()}）")
    ck.check(len(window.pages) == len(EXPECTED_PAGES), f"页面数量正确（{len(window.pages)}）")

    for key in EXPECTED_PAGES:
        page = window.pages.get(key)
        if page is None:
            ck.check(False, f"页面 {key} 缺失")
            continue
        try:
            if hasattr(page, "refresh"):
                page.refresh()
            ck.check(True, f"页面 {key}（{type(page).__name__}）构造与刷新正常")
        except Exception as exc:  # noqa: BLE001 - 冒烟测试需要把所有异常都收集起来
            ck.check(False, f"页面 {key} 异常：{type(exc).__name__}: {exc}")
            if not args.quiet:
                traceback.print_exc(limit=4)

    # ---------------- 导航切换
    print("[4] 导航切换")
    try:
        for row in range(window.nav.count()):
            window.nav.setCurrentRow(row)
        ck.check(True, f"全部 {window.nav.count()} 个导航项切换无异常")
    except Exception as exc:  # noqa: BLE001
        ck.check(False, f"导航切换异常：{type(exc).__name__}: {exc}")

    # ---------------- 状态栏
    print("[5] 状态栏")
    try:
        window.refresh_status()
        message = window.statusBar().currentMessage()
        ck.check(bool(message), f"状态栏信息正常（{message}）")
    except Exception as exc:  # noqa: BLE001
        ck.check(False, f"状态栏刷新异常：{type(exc).__name__}: {exc}")

    # ---------------- 关于对话框内容
    print("[6] 关于信息")
    try:
        provider = context.config.provider()
        ck.check(provider.is_local, "关于信息会显示「本地部署，材料不出本机」")
    except Exception as exc:  # noqa: BLE001
        ck.check(False, f"关于信息构造异常：{type(exc).__name__}: {exc}")

    window.close()
    del app

    print("=" * 60)
    if ck.failed:
        print(f"界面冒烟测试失败：{ck.passed} 项通过，{len(ck.failed)} 项失败")
        for item in ck.failed:
            print(f"  · {item}")
        return 1
    print(f"界面冒烟测试全部通过：{ck.passed} 项检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
