from __future__ import annotations

import argparse
import sys
from typing import Sequence

from lae_core import run_component


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    if "--serve" in args_list:
        parser = argparse.ArgumentParser(prog="lae-api")
        parser.add_argument("--serve", action="store_true")
        parser.add_argument("--host", default="0.0.0.0")
        parser.add_argument("--port", type=int, default=8080)
        args = parser.parse_args(args_list)
        import uvicorn

        uvicorn.run("lae_api.app:app", host=args.host, port=args.port)
        return 0
    return run_component("lae-api", args_list, default_port=8080)


if __name__ == "__main__":
    raise SystemExit(main())
