from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm is None:
            raise RuntimeError(
                "Building feishu-dispatcher requires npm. "
                "Install Node.js and npm, then retry the build."
            )

        root = Path(self.root)
        tsc_name = "tsc.cmd" if sys.platform == "win32" else "tsc"
        if not (root / "node_modules" / ".bin" / tsc_name).exists():
            subprocess.run(
                [npm, "ci", "--ignore-scripts"],
                cwd=root,
                check=True,
            )

        subprocess.run(
            [npm, "run", "build:webui"],
            cwd=root,
            check=True,
        )
