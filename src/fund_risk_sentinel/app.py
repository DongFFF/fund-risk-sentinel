"""应用入口：启动桌面界面。

这个模块是 ``fund-risk-sentinel`` 命令与 ``python -m fund_risk_sentinel``
共同指向的入口，也是 PyInstaller 打包时的启动脚本。

启动流程：

1. 准备日志（写用户数据目录，打包后临时目录只读，不能写那里）；
2. 首次运行把默认配置写到用户配置目录（可编辑，不污染安装目录）；
3. 建立 ``QApplication``，加载全局样式；
4. 组装 ``AppContext``（配置 / 模型客户端 / 流水线 / 存储）；
5. 显示主窗口，进入事件循环。

任何一步失败都尽量以弹窗形式告诉用户"出了什么问题、去哪里看日志"，
而不是抛一堆堆栈让人无从下手——这是"使用没有任何门槛"的一部分。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

from .config.paths import is_frozen, user_config_dir, user_data_dir
from .config.settings import (
    PROVIDERS_FILE,
    SETTINGS_FILE,
    ConfigManager,
    get_manager,
    set_manager,
    write_default_configs,
)
from .core.exceptions import SentinelError

logger = logging.getLogger("fund_risk_sentinel")

#: 单文件 exe 场景下退出时不要让子进程残留
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")


# ================================================================ 日志

def _log_file() -> Path:
    directory = user_data_dir() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "app.log"


def setup_logging(level: int = logging.INFO) -> Path | None:
    """配置日志：控制台 + 滚动文件。返回日志文件路径（失败返回 None）。"""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log_path: Path | None = None
    try:
        log_path = _log_file()
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception:  # noqa: BLE001 - 日志失败不该阻断程序启动
        logger.warning("无法写入日志文件，仅输出到控制台", exc_info=True)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path


# ================================================================ 首次运行

def ensure_user_config() -> list[Path]:
    """首次运行时在用户配置目录写入一份可编辑的默认配置。

    只在用户目录里**完全找不到**配置文件时才写，避免覆盖用户已改过的配置。
    源码运行时项目 ``config/`` 下已有配置，这一步通常什么也不做。
    """
    target = user_config_dir()
    has_settings = (target / SETTINGS_FILE).is_file()
    has_providers = (target / PROVIDERS_FILE).is_file()
    if has_settings and has_providers:
        return []
    written = write_default_configs(target, overwrite=False)
    if written:
        logger.info("已写入默认配置：%s", ", ".join(str(p) for p in written))
    return written


# ================================================================ 异常兜底

def _install_excepthook(app) -> None:  # noqa: ANN001 - 避免顶层 import Qt
    """把未捕获异常变成可见的提示，而不是静默崩溃。"""
    from PySide6.QtWidgets import QMessageBox

    def hook(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("未捕获异常：\n%s", detail)
        try:
            QMessageBox.critical(
                None,
                "程序遇到问题",
                f"{exc_type.__name__}: {exc_value}\n\n"
                f"详细信息已写入日志：\n{_log_file()}\n\n"
                "可尝试「文件 → 重新加载配置与规则库」或重启程序。",
            )
        except Exception:  # noqa: BLE001 - 弹窗再失败就只能记日志了
            logger.exception("展示异常提示失败")

    sys.excepthook = hook


# ================================================================ 主流程

def build_context():  # noqa: ANN201 - 返回 AppContext，延迟导入避免无界面环境报错
    from .ui.context import AppContext

    return AppContext.create()


def main(argv: list[str] | None = None) -> int:
    """程序主入口。返回进程退出码。"""
    argv = list(sys.argv if argv is None else argv)
    setup_logging()
    logger.info("启动 %s", "打包模式" if is_frozen() else "源码模式")
    ensure_user_config()

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:  # pragma: no cover - 缺依赖时给出人话提示
        sys.stderr.write(
            "缺少图形界面依赖 PySide6。\n"
            "请先执行：pip install -r requirements.txt\n"
            f"原始错误：{exc}\n"
        )
        return 2

    # 高分屏缩放：Qt6 默认已开启，这里只在需要时补充取整策略
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:  # noqa: BLE001 - 老版本 Qt 没有该 API
        pass

    app = QApplication(argv)
    app.setApplicationName("fund-risk-sentinel")
    app.setApplicationDisplayName("高校科研经费风险智能识别系统")
    app.setOrganizationName("FundRiskSentinel")
    _install_excepthook(app)

    # ---------------- 组装运行时上下文
    try:
        context = build_context()
    except SentinelError as exc:
        logger.exception("初始化失败")
        QMessageBox.critical(
            None,
            "初始化失败",
            f"{exc}\n\n可尝试检查配置文件是否正确：\n{user_config_dir()}",
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("初始化出现未预期错误")
        QMessageBox.critical(
            None,
            "初始化失败",
            f"{type(exc).__name__}: {exc}\n\n日志：{_log_file()}",
        )
        return 1

    # 加载规则库与知识库（失败不致命，界面会提示为空）
    messages = context.reload_pipeline()
    for message in messages:
        logger.info("初始化：%s", message)

    # ---------------- 主窗口
    from .ui.main_window import MainWindow

    window = MainWindow(context)
    window.show()
    logger.info("界面已就绪")

    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "setup_logging", "ensure_user_config", "build_context"]
