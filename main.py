"""Entry point do RemoteOps.

Uso:
    python main.py
    python -m remoteops
"""

from __future__ import annotations

import sys
from pathlib import Path

# Garante que a raiz do projeto esteja no sys.path em execução direta.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    from remoteops.bootstrap import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
