from __future__ import annotations

import re
import unittest
from pathlib import Path


DOCKER_DIR = Path(__file__).parents[1] / "deploy" / "luma" / "docker"
PROXY_UNSET = (
    "unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy;"
)
NETWORK_BUILD_COMMAND = re.compile(
    r"\b(?:uv sync|corepack prepare|pnpm install|git clone|go (?:run|build))\b"
)


def run_instructions(dockerfile: str) -> list[str]:
    """Return folded RUN instructions without needing a Docker daemon."""

    instructions: list[str] = []
    current: list[str] = []
    for line in dockerfile.splitlines():
        if current:
            current.append(line.strip())
            if not line.rstrip().endswith("\\"):
                instructions.append(" ".join(current))
                current = []
            continue
        if line.startswith("RUN "):
            current = [line]
            if not line.rstrip().endswith("\\"):
                instructions.append(line)
                current = []
    if current:
        instructions.append(" ".join(current))
    return instructions


class PlatformDockerfileTests(unittest.TestCase):
    def test_network_build_steps_ignore_injected_proxy_args(self) -> None:
        checked = 0
        for path in sorted(DOCKER_DIR.glob("*.Dockerfile")):
            for instruction in run_instructions(path.read_text(encoding="utf-8")):
                if not NETWORK_BUILD_COMMAND.search(instruction):
                    continue
                checked += 1
                self.assertIn(PROXY_UNSET, instruction, path.name)
                self.assertLess(
                    instruction.index(PROXY_UNSET),
                    NETWORK_BUILD_COMMAND.search(instruction).start(),  # type: ignore[union-attr]
                    path.name,
                )
        self.assertGreater(checked, 0)

    def test_platform_images_do_not_persist_build_proxy_settings(self) -> None:
        proxy_env = re.compile(
            r"^ENV\s+.*(?:HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|http_proxy|https_proxy|all_proxy)",
            re.MULTILINE,
        )
        for path in sorted(DOCKER_DIR.glob("*.Dockerfile")):
            self.assertIsNone(
                proxy_env.search(path.read_text(encoding="utf-8")),
                path.name,
            )


if __name__ == "__main__":
    unittest.main()
