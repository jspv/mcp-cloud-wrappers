"""Local pip/uv bundler for Lambda deployment packages.

Installs requirements.txt into the CDK output directory using uv
(preferred) or pip, then copies source files.  The ``mcp-wrapper-runtime``
package is always installed automatically from this repo.

On non-Linux hosts, compiled C extensions may be for the wrong platform.
CDK falls back to Docker/Podman bundling automatically when the local
bundler returns False.  Set ``CDK_DOCKER=podman`` if using Podman.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import aws_cdk as cdk
import jsii

# mcp-wrapper-runtime lives at a known location relative to this file.
_RUNTIME_PKG = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "packages", "mcp-wrapper-runtime")
)


@jsii.implements(cdk.ILocalBundling)
class LocalPipBundler:
    """Local bundler: ``uv pip install`` -> ``pip install`` -> Docker fallback."""

    def __init__(self, source_dir: str) -> None:
        self._source_dir = source_dir

    def _pip_install(self, target_dir: str, *args: str) -> None:
        """Install packages into *target_dir* using uv or pip."""
        uv_bin = shutil.which("uv")
        if uv_bin:
            subprocess.check_call(
                [uv_bin, "pip", "install", "--target", target_dir,
                 "--link-mode", "copy", *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-t", target_dir, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def try_bundle(self, output_dir: str, options) -> bool:  # type: ignore[override]
        req = os.path.join(self._source_dir, "requirements.txt")

        # On non-Linux hosts, always fall back to Docker/Podman so
        # compiled C extensions get the correct Linux ARM64 binaries.
        import platform

        if platform.system() != "Linux":
            return False

        try:
            # Install mcp-wrapper-runtime from this repo automatically.
            if os.path.isdir(_RUNTIME_PKG):
                self._pip_install(output_dir, _RUNTIME_PKG)

            # Install service requirements.
            if os.path.exists(req):
                self._pip_install(output_dir, "-r", req)

            # Copy source files to output.
            for item in os.listdir(self._source_dir):
                src = os.path.join(self._source_dir, item)
                dst = os.path.join(output_dir, item)
                if os.path.isdir(src):
                    if not os.path.exists(dst):
                        shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            return True
        except Exception:  # noqa: BLE001
            return False
