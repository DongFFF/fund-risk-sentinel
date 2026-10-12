#!/usr/bin/env python
"""PyInstaller 打包脚本：产出 Windows 单文件 exe。

设计要点：

1. **单文件（onefile）**：用户拿到一个 exe 就能跑，不需要装 Python。
2. **资源随包**：``config/rules``、``config/knowledge`` 与示例配置一起打进去，
   首次运行会复制到用户配置目录，用户可改；解压出的临时目录只读，不能当配置目录用。
3. **窗口模式**：``console=False``，双击不弹黑框。
4. **隐藏导入**：PySide6 的部分子模块 PyInstaller 静态分析扫不到，需显式声明。

用法::

    pip install -r requirements-dev.txt
    python scripts/build_exe.py                 # 默认单文件
    python scripts/build_exe.py --onedir        # 目录模式（启动更快，便于排错）
    python scripts/build_exe.py --console       # 保留控制台窗口（调试用）
    python scripts/build_exe.py --clean         # 先清理 build/dist
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "FundRiskSentinel"
ENTRY = ROOT / "run.py"
ICON = ROOT / "assets" / "app.ico"

#: 随包资源：(源路径, 包内目标目录)
BUNDLE_DIRS = [
    ("config/rules", "config/rules"),
    ("config/knowledge", "config/knowledge"),
]

#: 单独随包的示例配置文件
BUNDLE_FILES = [
    "config/settings.example.yaml",
    "config/providers.example.yaml",
]

#: PyInstaller 静态分析容易漏掉的导入
HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pydantic",
    "pydantic_core",
    "yaml",
    "httpx",
    "httpcore",
    "docx",
    "openpyxl",
    "pypdf",
    "fitz",          # PyMuPDF
    "PIL",
    "PIL.Image",
    "sqlite3",
]

#: 明确排除，避免把体积撑大（本程序不用）
EXCLUDES = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "IPython",
    "jupyter",
    "pytest",
]


def _check_pyinstaller() -> str:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "未安装 PyInstaller。请先执行：\n    pip install -r requirements-dev.txt\n"
        )
        raise SystemExit(2)
    return sys.executable


def _datas() -> list[str]:
    """拼出 PyInstaller 的 --add-data 参数（Windows 用 ``;`` 分隔）。"""
    sep = ";" if sys.platform == "win32" else ":"
    datas: list[str] = []
    for src, dst in BUNDLE_DIRS:
        path = ROOT / src
        if not path.exists():
            print(f"  [跳过] 资源目录不存在：{src}")
            continue
        datas += ["--add-data", f"{path}{sep}{dst}"]
    for rel in BUNDLE_FILES:
        path = ROOT / rel
        if not path.exists():
            print(f"  [跳过] 资源文件不存在：{rel}")
            continue
        datas += ["--add-data", f"{path}{sep}config"]
    return datas


def build(args: argparse.Namespace) -> int:
    python = _check_pyinstaller()

    if args.clean:
        for name in ("build", "dist"):
            target = ROOT / name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                print(f"  已清理 {target}")

    if not ENTRY.exists():
        sys.stderr.write(f"入口文件不存在：{ENTRY}\n")
        return 2

    cmd: list[str] = [
        python, "-m", "PyInstaller",
        "--noconfirm",
        "--name", APP_NAME,
        "--onefile" if not args.onedir else "--onedir",
        "--console" if args.console else "--windowed",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--paths", str(ROOT / "src"),
    ]
    if ICON.exists():
        cmd += ["--icon", str(ICON)]
    else:
        print("  [提示] 未找到 assets/app.ico，使用默认图标（可自行放置一个 .ico）")

    for name in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", name]
    for name in EXCLUDES:
        cmd += ["--exclude-module", name]
    cmd += _datas()
    cmd.append(str(ENTRY))

    print("执行打包命令：")
    print("  " + " ".join(cmd))
    print("-" * 60)

    result = subprocess.run(cmd, cwd=str(ROOT), check=False)
    if result.returncode != 0:
        sys.stderr.write("\n打包失败，请查看上方输出。\n")
        return result.returncode

    suffix = ".exe" if sys.platform == "win32" else ""
    if args.onedir:
        artifact = ROOT / "dist" / APP_NAME / f"{APP_NAME}{suffix}"
    else:
        artifact = ROOT / "dist" / f"{APP_NAME}{suffix}"

    print("-" * 60)
    if artifact.exists():
        size_mb = artifact.stat().st_size / 1024 / 1024
        print(f"打包完成：{artifact}  （{size_mb:.1f} MB）")
        print("\n首次运行会在用户配置目录生成可编辑的默认配置：")
        print("  %APPDATA%/FundRiskSentinel/config/")
        print("数据、日志与导出保存在：")
        print("  %LOCALAPPDATA%/FundRiskSentinel/")
    else:
        print("打包命令返回成功，但未找到预期产物，请检查 dist 目录。")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="打包 fund-risk-sentinel 桌面程序")
    parser.add_argument("--onedir", action="store_true", help="生成目录模式而非单文件")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（调试用）")
    parser.add_argument("--clean", action="store_true", help="打包前清理 build/ 与 dist/")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return build(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
