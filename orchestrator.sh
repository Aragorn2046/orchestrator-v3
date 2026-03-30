#!/usr/bin/env bash
# orchestrator.sh — Spawn, monitor, and collect specialist Claude Code sessions
#
# Usage:
#   orchestrator.sh spawn <job-id>              Spawn specialist from existing project dir
#   orchestrator.sh remote <machine> <job-id>   Dispatch to remote machine via relay
#   orchestrator.sh status [job-id]             Check status of active jobs
#   orchestrator.sh collect <job-id>            Read result.json from completed job
#   orchestrator.sh cleanup <job-id>            Archive completed job
#   orchestrator.sh cancel <job-id>             Kill a running specialist
#   orchestrator.sh machines                    Show reachable machines
#
# The context-assembler.py and task-router.py handle project creation and classification.
# This script handles the runtime lifecycle.

set -euo pipefail

ORCHESTRATED_DIR="${ORCHESTRATED_ROOT:-$HOME/projects/_orchestrated}"
JOBS_DIR="/tmp/orchestrator/jobs"
BUDGET_FILE="/tmp/orchestrator/budget.json"

mkdir -p "$JOBS_DIR"

# ── Helpers ──

log() { echo "[orchestrator] $*" >&2; }

read_spec() {
    local job_id="$1"
    local spec="$ORCHESTRATED_DIR/$job_id/spec.json"
    if [ ! -f "$spec" ]; then
        log "ERROR: No spec.json for job $job_id"
        return 1
    fi
    cat "$spec"
}

# ── Commands ──

cmd_spawn() {
    local job_id="$1"
    local project_dir="$ORCHESTRATED_DIR/$job_id"

    if [ ! -d "$project_dir" ]; then
        log "ERROR: Project directory not found: $project_dir"
        exit 1
    fi

    local spec
    spec=$(read_spec "$job_id") || exit 1

    local model budget tools
    model=$(echo "$spec" | python3 -c "import sys,json; print(json.load(sys.stdin)['model'])")
    budget=$(echo "$spec" | python3 -c "import sys,json; print(json.load(sys.stdin)['budget'])")
    tools=$(echo "$spec" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin)['allowed_tools']))")

    # Create job runtime dir
    local job_runtime="$JOBS_DIR/$job_id"
    mkdir -p "$job_runtime"

    # Write job metadata
    cat > "$job_runtime/meta.json" <<METAEOF
{
    "job_id": "$job_id",
    "model": "$model",
    "budget": $budget,
    "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "status": "running"
}
METAEOF

    log "Spawning specialist: $job_id (model=$model, budget=\$$budget)"

    # Calculate orchestrator depth (prevent recursive spawning)
    local current_depth="${ORCHESTRATOR_DEPTH:-0}"
    local next_depth=$((current_depth + 1))

    # Spawn headless Claude Code
    cd "$project_dir"
    env -u CLAUDECODE CLAUDE_SESSION_TYPE=specialist ORCHESTRATOR_DEPTH="$next_depth" \
        bash -c "cat prompt.md | claude --model $model -p \
            --permission-mode acceptEdits \
            --allowed-tools \"$tools\" \
            --max-budget-usd $budget" \
        > "$job_runtime/stdout.log" 2>&1 &

    local pid=$!
    echo "$pid" > "$job_runtime/pid"

    log "Specialist running: PID=$pid, log=$job_runtime/stdout.log"

    # Log event (optional external logger)
    if [ -n "${ORCHESTRATOR_LOG_CMD:-}" ]; then
        $ORCHESTRATOR_LOG_CMD spawn "$job_id" \
            "model=$model budget=\$$budget template=$(echo "$spec" | python3 -c "import sys,json; print(json.load(sys.stdin).get('template','?'))" 2>/dev/null)" 2>/dev/null || true
    fi

    echo "{\"job_id\": \"$job_id\", \"pid\": $pid, \"status\": \"running\"}"
}

cmd_status() {
    local target_id="${1:-}"

    if [ -n "$target_id" ]; then
        # Single job status
        _job_status "$target_id"
    else
        # All jobs
        local found=0
        for job_dir in "$JOBS_DIR"/*/; do
            [ -d "$job_dir" ] || continue
            local jid
            jid=$(basename "$job_dir")
            _job_status "$jid"
            found=1
        done
        if [ "$found" -eq 0 ]; then
            echo '{"active_jobs": 0, "message": "No active jobs"}'
        fi
    fi
}

_job_status() {
    local job_id="$1"
    local job_runtime="$JOBS_DIR/$job_id"
    local project_dir="$ORCHESTRATED_DIR/$job_id"
    local result_file="$project_dir/result.json"
    local pid_file="$job_runtime/pid"

    if [ ! -d "$job_runtime" ]; then
        echo "{\"job_id\": \"$job_id\", \"status\": \"unknown\", \"error\": \"No runtime data\"}"
        return
    fi

    # Check if result.json exists (specialist completed)
    if [ -f "$result_file" ]; then
        local status
        status=$(python3 -c "import json; print(json.load(open('$result_file'))['status'])" 2>/dev/null || echo "unknown")
        echo "{\"job_id\": \"$job_id\", \"status\": \"$status\", \"has_result\": true}"
        return
    fi

    # Check PID
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            local elapsed
            elapsed=$(( $(date +%s) - $(stat -f %m "$pid_file" 2>/dev/null || stat -c %Y "$pid_file" 2>/dev/null || echo "0") ))
            echo "{\"job_id\": \"$job_id\", \"status\": \"running\", \"pid\": $pid, \"elapsed_seconds\": $elapsed}"
        else
            echo "{\"job_id\": \"$job_id\", \"status\": \"crashed\", \"pid\": $pid, \"has_result\": false}"
        fi
    else
        echo "{\"job_id\": \"$job_id\", \"status\": \"unknown\", \"error\": \"No PID file\"}"
    fi
}

cmd_collect() {
    local job_id="$1"
    local project_dir="$ORCHESTRATED_DIR/$job_id"
    local result_file="$project_dir/result.json"

    if [ ! -f "$result_file" ]; then
        log "No result.json for job $job_id"
        # Try to extract info from stdout
        local stdout_log="$JOBS_DIR/$job_id/stdout.log"
        if [ -f "$stdout_log" ]; then
            local lines
            lines=$(wc -l < "$stdout_log")
            echo "{\"job_id\": \"$job_id\", \"status\": \"no_result\", \"stdout_lines\": $lines}"
        else
            echo "{\"job_id\": \"$job_id\", \"status\": \"no_result\", \"stdout_lines\": 0}"
        fi
        return 1
    fi

    cat "$result_file"
}

cmd_cleanup() {
    local job_id="$1"
    local project_dir="$ORCHESTRATED_DIR/$job_id"
    local job_runtime="$JOBS_DIR/$job_id"
    local done_dir="$ORCHESTRATED_DIR/_done"

    mkdir -p "$done_dir"

    if [ -d "$project_dir" ]; then
        mv "$project_dir" "$done_dir/$job_id"
        log "Archived project: $done_dir/$job_id"
    fi

    if [ -d "$job_runtime" ]; then
        rm -rf "$job_runtime"
        log "Cleaned runtime: $job_runtime"
    fi

    echo "{\"job_id\": \"$job_id\", \"status\": \"archived\"}"
}

cmd_cancel() {
    local job_id="$1"
    local pid_file="$JOBS_DIR/$job_id/pid"

    if [ ! -f "$pid_file" ]; then
        log "No PID file for job $job_id"
        return 1
    fi

    local pid
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        log "Killed specialist $job_id (PID=$pid)"
        echo "{\"job_id\": \"$job_id\", \"status\": \"cancelled\", \"pid\": $pid}"
    else
        log "PID $pid already dead"
        echo "{\"job_id\": \"$job_id\", \"status\": \"already_dead\", \"pid\": $pid}"
    fi
}

cmd_remote() {
    local machine="$1"
    local job_id="$2"
    local project_dir="$ORCHESTRATED_DIR/$job_id"

    if [ ! -d "$project_dir" ]; then
        log "ERROR: Project directory not found: $project_dir"
        exit 1
    fi

    local spec
    spec=$(read_spec "$job_id") || exit 1

    local model budget task
    model=$(echo "$spec" | python3 -c "import sys,json; print(json.load(sys.stdin)['model'])")
    budget=$(echo "$spec" | python3 -c "import sys,json; print(json.load(sys.stdin)['budget'])")
    task=$(cat "$project_dir/prompt.md")

    # Create job runtime dir for tracking
    local job_runtime="$JOBS_DIR/$job_id"
    mkdir -p "$job_runtime"

    cat > "$job_runtime/meta.json" <<METAEOF
{
    "job_id": "$job_id",
    "model": "$model",
    "budget": $budget,
    "machine": "$machine",
    "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "status": "remote",
    "dispatch": "relay"
}
METAEOF

    log "Dispatching to $machine via relay: $job_id (model=$model, budget=\$$budget)"

    # Find relay.py (set RELAY_CMD env var to override)
    local relay_py="${RELAY_CMD:-}"
    if [ -z "$relay_py" ]; then
        for candidate in "$HOME/projects/relay/relay.py" "$HOME/scripts/relay.py"; do
            if [ -f "$candidate" ]; then
                relay_py="$candidate"
                break
            fi
        done
    fi

    if [ -z "$relay_py" ]; then
        log "ERROR: relay.py not found. Set RELAY_CMD env var."
        echo "{\"job_id\": \"$job_id\", \"status\": \"error\", \"error\": \"relay.py not found\"}"
        exit 1
    fi

    # Dispatch via relay with auto-execute
    local relay_output
    relay_output=$(python3 "$relay_py" send "$machine" --auto --budget "$budget" --model "$model" "$task" 2>&1) || true

    echo "relay" > "$job_runtime/dispatch_method"
    echo "$machine" > "$job_runtime/remote_machine"

    # Log event (optional external logger)
    if [ -n "${ORCHESTRATOR_LOG_CMD:-}" ]; then
        $ORCHESTRATOR_LOG_CMD remote "$job_id" \
            "machine=$machine model=$model budget=\$$budget" 2>/dev/null || true
    fi

    log "Relay dispatch: $relay_output"
    echo "{\"job_id\": \"$job_id\", \"machine\": \"$machine\", \"status\": \"dispatched\", \"relay\": \"$relay_output\"}"
}

cmd_machines() {
    # Check which machines are reachable via relay
    local relay_py="${RELAY_CMD:-}"
    if [ -z "$relay_py" ]; then
        for candidate in "$HOME/projects/relay/relay.py" "$HOME/scripts/relay.py"; do
            if [ -f "$candidate" ]; then
                relay_py="$candidate"
                break
            fi
        done
    fi

    if [ -z "$relay_py" ]; then
        echo "{\"error\": \"relay.py not found. Set RELAY_CMD env var.\"}"
        exit 1
    fi

    echo "{"
    local first=true
    # List your machine names here (or set MACHINES env var, space-separated)
    for machine in ${MACHINES:-hub worker-1 worker-2}; do
        local result
        result=$(python3 "$relay_py" ping "$machine" 2>&1) || result="unreachable"
        if [ "$first" = true ]; then
            first=false
        else
            echo ","
        fi
        local reachable="false"
        echo "$result" | grep -qi "pong\|alive\|ok" && reachable="true"
        printf '  "%s": {"reachable": %s, "detail": "%s"}' "$machine" "$reachable" "$(echo "$result" | tr '"' "'" | head -1)"
    done
    echo ""
    echo "}"
}

# ── Main ──

case "${1:-}" in
    spawn)    cmd_spawn "${2:?Job ID required}" ;;
    remote)   cmd_remote "${2:?Machine required}" "${3:?Job ID required}" ;;
    status)   cmd_status "${2:-}" ;;
    collect)  cmd_collect "${2:?Job ID required}" ;;
    cleanup)  cmd_cleanup "${2:?Job ID required}" ;;
    cancel)   cmd_cancel "${2:?Job ID required}" ;;
    machines) cmd_machines ;;
    *)
        echo "Usage: orchestrator.sh {spawn|remote|status|collect|cleanup|cancel|machines} [args]"
        exit 1
        ;;
esac
