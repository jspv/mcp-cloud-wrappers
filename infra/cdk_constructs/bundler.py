"""Local pip/uv bundler for Lambda deployment packages.

Extracted from glidepath — installs requirements.txt into the CDK output
directory using uv (preferred) or pip, then copies source files.  Falls
back to Docker bundling if the local install fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import aws_cdk as cdk
import jsii


@jsii.implements(cdk.ILocalBundling)
class LocalPipBundler:
    """Local bundler: ``uv pip install`` -> ``pip install`` -> Docker fallback."""

    def __init__(self, source_dir: str) -> None:
        self._source_dir = source_dir

    def try_bundle(self, output_dir: str, options) -> bool:  # type: ignore[override]
        req = os.path.join(self._source_dir, "requirements.txt")
        try:
            if os.path.exists(req):
                uv_bin = shutil.which("uv")
                if uv_bin:
                    subprocess.check_call(
                        [
                            uv_bin, "pip", "install",
                            "-r", req,
                            "--target", output_dir,
                            "--link-mode", "copy",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    subprocess.check_call(
                        [
                            sys.executable, "-m", "pip", "install",
                            "-r", req, "-t", output_dir,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            # Copy source files to output
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
