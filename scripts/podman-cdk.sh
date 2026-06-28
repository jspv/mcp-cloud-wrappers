#!/usr/bin/env bash
# CDK container-engine shim for rootless podman.
#
# CDK bundles Lambda assets by running the build image as `-u <uid>:<gid>` with
# the build output directory bind-mounted from the host. On rootless podman —
# notably the libkrun machine provider — that mount appears root-owned inside
# the container, so the build process (running as the host UID) cannot write to
# it and bundling fails with a permission error.
#
# `--userns=keep-id` maps the host user into the container's user namespace so
# the bind mount is owned by, and writable as, the forced UID. We inject it only
# for `run`; every other invocation (version, inspect, build, login, ...) is
# passed through untouched. Docker users never reach this shim — the Makefile
# prefers docker when it is installed.
set -euo pipefail

engine="$(command -v podman)"
if [[ -z "$engine" ]]; then
  echo "podman-cdk.sh: podman not found on PATH" >&2
  exit 127
fi

if [[ "${1:-}" == "run" ]]; then
  shift
  exec "$engine" run --userns=keep-id "$@"
fi

exec "$engine" "$@"
