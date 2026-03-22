#!/usr/bin/env bash
set -euo pipefail

EXTENSION_DIR=${EXTENSION_DIR:-/usr/share/postgresql/18/extension}
LIB_DIR=${LIB_DIR:-/usr/lib/postgresql/18/lib}
RESTORE_OWNER=${RESTORE_OWNER:-root:root}
PG_VERSION=${PG_VERSION:-pg18}
CARGO_HOME=${CARGO_HOME:-/tmp/cargo-home}

if [[ $# -eq 0 ]]; then
  TEST_ARGS=("api_tests")
else
  TEST_ARGS=("$@")
fi

cleanup() {
  sudo chown -R "${RESTORE_OWNER}" "${EXTENSION_DIR}" "${LIB_DIR}"
}

trap cleanup EXIT

mkdir -p "${CARGO_HOME}"
sudo chown -R "$(id -un):$(id -gn)" "${EXTENSION_DIR}" "${LIB_DIR}"

CARGO_HOME="${CARGO_HOME}" cargo pgrx test "${PG_VERSION}" "${TEST_ARGS[@]}"
