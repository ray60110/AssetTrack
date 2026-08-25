#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for AssetTrack.
#
# AssetTrack is a macOS-oriented offline TUI (Touch ID + macOS Keychain). On a
# headless Linux Cloud Agent VM we install the same Python package plus a
# file-based `keyring` backend so the interactive TUI can register/unlock an
# account without a desktop secret service. The real macOS build is unaffected.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

PY="$(command -v python3)"
PY_TAG="$("$PY" -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"

# Cursor's default image ships CPython but not the matching `venv` stdlib
# package on Debian/Ubuntu; install it once (runs at build time for snapshots).
if ! "$PY" -c 'import ensurepip' >/dev/null 2>&1; then
  echo "Installing ${PY_TAG}-venv…"
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends "${PY_TAG}-venv" || \
    sudo apt-get install -y --no-install-recommends python3-venv
fi

if [ ! -x "${REPO_ROOT}/.venv/bin/python" ]; then
  echo "Creating virtualenv…"
  "$PY" -m venv "${REPO_ROOT}/.venv"
fi

# shellcheck disable=SC1091
source "${REPO_ROOT}/.venv/bin/activate"

python -m pip install --upgrade pip
# Editable install with dev (pytest) extras. Optional broker extras (ibkr,
# firstrade) are intentionally skipped: they need TWS/IB Gateway or an
# unofficial reverse-engineered client and are not part of the core dev loop.
pip install -e ".[dev]"
# Headless secret store so the interactive TUI can run without macOS Keychain
# or a desktop SecretService. The automated tests mock keyring and do not use
# this backend.
pip install "keyrings.alt>=5.0"

# Make the file-based keyring the default without mutating shell profiles or
# requiring an env var. keyring reads this config from the user config dir.
KEYRING_CFG_DIR="$("${REPO_ROOT}/.venv/bin/python" -c 'import keyring.util.platform_ as p; print(p.config_root())')"
mkdir -p "${KEYRING_CFG_DIR}"
cat > "${KEYRING_CFG_DIR}/keyringrc.cfg" <<'CFG'
[backend]
default-keyring=keyrings.alt.file.PlaintextKeyring
CFG

echo "AssetTrack environment ready. Run tests with: .venv/bin/pytest"
echo "Launch the TUI with:            .venv/bin/assettrack -u <user>"
