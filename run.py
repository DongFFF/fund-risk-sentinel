#!/usr/bin/env python
"""源码运行入口：``python run.py``。

等价于 ``python -m fund_risk_sentinel``，但不需要先安装包——它把 ``src/``
加进 ``sys.path`` 后直接调用应用入口，方便刚克隆仓库时"开箱即跑"。

用法：

.. code-block:: bash

    python run.py              # 启动桌面界面
    python run.py --version    # 查看版本
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _print_version() -> int:
    from fund_risk_sentinel import __app_name__, __version__

    print(f"{__app_name__} v{__version__}")
    print(f"仓库：{ROOT}")
    return 0


def main() -> int:
    if any(a in {"--version", "-V"} for a in sys.argv[1:]):
        return _print_version()

    from fund_risk_sentinel.app import main as app_main

    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())
