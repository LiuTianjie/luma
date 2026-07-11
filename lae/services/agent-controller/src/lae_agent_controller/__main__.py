from __future__ import annotations

from typing import Sequence

from lae_core import run_component


def main(argv: Sequence[str] | None = None) -> int:
    return run_component("lae-agent-controller", argv, default_port=8081)


if __name__ == "__main__":
    raise SystemExit(main())
