#!/usr/bin/env bash
# server/run_service.sh
# Wrapper script for the Schedule_Service launchd agent.
#
# launchd does not natively support sourcing environment files, so this wrapper
# loads .env from the repo root before exec-ing the Python service. The plist
# references this script in ProgramArguments instead of Python directly.
#
# JELLYFIN_API_KEY and OFFLINE_CLIP_JELLYFIN_ID are required — the service will
# exit immediately at startup if either is missing (Requirement 33.1).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"

# Source .env if present; required vars are validated by the service itself.
if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090  # ENV_FILE path is dynamic by design
  source "${ENV_FILE}"
  set +a
fi

exec "${REPO_DIR}/.venv/bin/python3" -m server.schedule_service
