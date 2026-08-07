#!/usr/bin/env bash
# __________________________________________________
#
# Cloud Native BNG / Cisco Subscriber Edge
# Control Plane Deployment Agent
#
# Author: Gurpreet Dhaliwal, TME MiG
# __________________________________________________

set -euo pipefail

RELEASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${RELEASE_DIR}/.venv"

cd "${RELEASE_DIR}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required but was not found in PATH." >&2
  exit 1
fi

echo "Creating Python virtual environment: ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"

echo "Installing CNBNG dependencies..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${RELEASE_DIR}/requirements.txt"

echo
echo "Install complete."
echo "Activate the environment before running CNBNG:"
echo "  source .venv/bin/activate"
echo
echo "Verify the agent:"
echo "  ./bin/cnbng --version"
