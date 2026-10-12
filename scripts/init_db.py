#!/usr/bin/env python
"""初始化数据库：建表、写元信息、做一次审计链自检。

一般不需要手动跑——程序启动时会自动建库。这个脚本用于：

- 部署前预建库，确认目录权限没问题；
- 排查"数据库是不是坏了"；
- 演示环境下快速重置（``--reset``，会先备份旧库）。

用法::

    python scripts/init_db.py              # 建库（已存在则只校验）
    python scripts/init_db.py --reset      # 备份旧库后重建
    python scripts/init_db.py --info       # 只看当前库状态
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fund_risk_sentinel.config import get_manager  # noqa: E402
from fund_risk_sentinel.storage import open_storage  # noqa: E402
from fund_risk_sentinel.storage.db import SCHEMA_VERSION  # noqa: E402


def show_info(repository, audit, db_path: Path) -> None:
    counts = repository.db.table_counts()
    size_kb = repository.db.size_bytes() / 1024
    print(f"数据库：{db_path}")
    print(f"大小：{size_kb:.1f} KB")
    print(f"架构版本：v{SCHEMA_VERSION}")
    print("表记录数：")
    for name, value in sorted(counts.items()):
        print(f"  {name:<16} {value}")

    ok, message = audit.verify_chain()
    print(f"审计链校验：{'通过' if ok else '异常'} —— {message}")

    stats = repository.dashboard_stats()
    print("概览：")
    for key, value in stats.items():
        print(f"  {key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化 fund-risk-sentinel 数据库")
    parser.add_argument("--reset", action="store_true", help="备份后重建数据库")
    parser.add_argument("--info", action="store_true", help="仅显示当前状态")
    args = parser.parse_args(argv)

    config = get_manager()
    config.settings.ensure_dirs()
    db_path = config.settings.resolved_db_path()

    if args.reset and db_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"{db_path.stem}.backup-{stamp}{db_path.suffix}")
        shutil.copy2(db_path, backup)
        db_path.unlink()
        print(f"已备份旧库到：{backup}")

    repository, audit = open_storage(config)

    if args.info:
        show_info(repository, audit, db_path)
        return 0

    print(f"数据库就绪：{db_path}")
    audit.log_config_change(changes={"action": "init_db", "detail": "脚本初始化数据库"})
    show_info(repository, audit, db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
