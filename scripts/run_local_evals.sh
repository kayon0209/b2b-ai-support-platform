#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
artifact_root="${APP_EVAL_ARTIFACT_DIR:-$repo_root/tests/artifacts}"
export APP_EVAL_ARTIFACT_DIR="$artifact_root"
export APP_TEST_ARTIFACT_DIR="$artifact_root"
if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="$repo_root/tests:$PYTHONPATH"
else
  export PYTHONPATH="$repo_root/tests"
fi
results_dir="$artifact_root/deepeval"
mkdir -p "$results_dir/state"
chmod 700 "$results_dir"
chmod 700 "$results_dir/state"
umask 077

# Keep eval data and traces local even if a user's shell or dotenv contains a
# Confident AI key. Also disable anonymous SDK telemetry for this opt-in run.
export DEEPEVAL_DISABLE_DOTENV=1
export DEEPEVAL_DISABLE_LEGACY_KEYFILE=1
export DEEPEVAL_TELEMETRY_OPT_OUT=1
export DEEPEVAL_LOCAL_STORE=json
export DEEPEVAL_RESULTS_FOLDER="$results_dir"
export DEEPEVAL_CACHE_FOLDER="$results_dir/state"
export DEEPEVAL_LOCAL_EVAL_RUN=1
export DEEPEVAL_NO_INSPECT_PROMPT=1
export CONFIDENT_API_KEY=""
export CONFIDENT_TRACE_VERBOSE=0
export CONFIDENT_TRACE_FLUSH=0

cd "$repo_root"
chmod_local_artifacts() {
  find "$results_dir" -type f -exec chmod 600 {} +
}
trap chmod_local_artifacts EXIT
deepeval test run "$@"
