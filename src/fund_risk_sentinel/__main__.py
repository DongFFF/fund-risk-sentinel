"""``python -m fund_risk_sentinel`` 的入口。"""

from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
