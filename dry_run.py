"""Dry-run mode and depth control for Orchestrator v3.

Dry-run mode replaces real Claude sessions with mock agent behavior,
enabling full end-to-end pipeline testing at zero cost.

Depth control enforces the three-level hierarchy:
  CEO (depth 0) -> heads (depth 1) -> grunts (depth 2) -> CANNOT spawn.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from communication import atomic_write
from task_graph import TaskGraph, TaskNode

logger = logging.getLogger(__name__)

MAX_DEPTH = 2  # Grunts at depth 2 cannot spawn


# ---------------------------------------------------------------------------
# Depth Control
# ---------------------------------------------------------------------------

def get_current_depth() -> int:
    """Read the current orchestrator depth from environment."""
    return int(os.environ.get("ORCHESTRATOR_DEPTH", "0"))


def can_spawn() -> bool:
    """Check whether the current depth allows spawning children."""
    return get_current_depth() < MAX_DEPTH


def check_spawn_allowed() -> None:
    """Raise if spawning is not allowed at current depth.

    Called before every spawn attempt (primary enforcement layer).
    """
    depth = get_current_depth()
    if depth >= MAX_DEPTH:
        raise DepthLimitError(
            f"Spawn rejected -- ORCHESTRATOR_DEPTH={depth} (max depth is {MAX_DEPTH})"
        )


def build_child_env(
    parent_env: Optional[dict] = None,
    agent_name: str = "",
    agent_id: str = "",
    agent_role: str = "grunt",
    workspace: str = "",
    dry_run: bool = False,
) -> dict:
    """Build environment dict for a child agent.

    Increments ORCHESTRATOR_DEPTH and sets all required agent env vars.
    """
    env = dict(parent_env) if parent_env else dict(os.environ)
    parent_depth = int(env.get("ORCHESTRATOR_DEPTH", "0"))

    env["ORCHESTRATOR_DEPTH"] = str(parent_depth + 1)
    env["AGENT_NAME"] = agent_name
    env["AGENT_ID"] = agent_id
    env["AGENT_ROLE"] = agent_role
    env["AGENT_WORKSPACE"] = workspace

    if dry_run:
        env["ORCHESTRATOR_DRY_RUN"] = "1"

    # Remove CLAUDECODE to prevent nested session detection
    env.pop("CLAUDECODE", None)

    return env


class DepthLimitError(Exception):
    """Spawn rejected because ORCHESTRATOR_DEPTH >= MAX_DEPTH."""


# ---------------------------------------------------------------------------
# Session Guard Helpers (for PreToolUse hook integration)
# ---------------------------------------------------------------------------

# Patterns that session-guard should block at depth >= MAX_DEPTH
BLOCKED_COMMAND_PATTERNS = [
    r"claude\s",
    r"orchestrator\.sh\s+(spawn|recruit)",
]


def should_block_command(command: str, depth: Optional[int] = None) -> bool:
    """Check if a bash command should be blocked at the current depth.

    This is the Python equivalent of the session-guard.sh check.
    Returns True if the command tries to spawn at depth >= MAX_DEPTH.
    """
    import re

    if depth is None:
        depth = get_current_depth()

    if depth < MAX_DEPTH:
        return False

    for pattern in BLOCKED_COMMAND_PATTERNS:
        if re.search(pattern, command):
            return True

    return False


# ---------------------------------------------------------------------------
# Mock Agent (Python equivalent of mock-agent.sh)
# ---------------------------------------------------------------------------

class MockAgent:
    """Simulates a Claude agent session for dry-run testing.

    Performs the same file I/O as a real agent:
    - Reads inbox task
    - Writes heartbeats
    - Writes progress updates
    - Writes final result (success or failure)
    """

    def __init__(
        self,
        workspace: str,
        delay: float = 0.0,
        simulate_failure: bool = False,
    ):
        self.workspace = workspace
        self.delay = delay
        self.simulate_failure = simulate_failure
        self.inbox_dir = os.path.join(workspace, "inbox")
        self.outbox_dir = os.path.join(workspace, "outbox")
        self.task_data: Optional[dict] = None

    def read_inbox(self) -> Optional[dict]:
        """Read the first task from inbox."""
        if not os.path.isdir(self.inbox_dir):
            return None

        for entry in sorted(os.scandir(self.inbox_dir), key=lambda e: e.name):
            if entry.name.startswith("task-") and entry.name.endswith(".json"):
                try:
                    with open(entry.path) as f:
                        self.task_data = json.load(f)
                    return self.task_data
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    def write_heartbeat(self) -> None:
        """Write a heartbeat.json to the outbox."""
        os.makedirs(self.outbox_dir, exist_ok=True)
        hb = {
            "agent_id": os.environ.get("AGENT_ID", "mock-agent"),
            "status": "running",
            "current_task": self.task_data.get("task_id", "") if self.task_data else "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = os.path.join(self.outbox_dir, "heartbeat.json")
        with open(path, "w") as f:
            json.dump(hb, f, indent=2)

    def write_progress(self, percent: int, step: str = "") -> None:
        """Write a progress update to the outbox."""
        os.makedirs(self.outbox_dir, exist_ok=True)
        task_id = self.task_data.get("task_id", "unknown") if self.task_data else "unknown"
        progress = {
            "task_id": task_id,
            "agent_id": os.environ.get("AGENT_ID", "mock-agent"),
            "step": step or f"Processing ({percent}%)",
            "percent_complete": percent,
            "details": f"Mock progress at {percent}%",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path = os.path.join(self.outbox_dir, f"progress-{task_id}.json")
        atomic_write(path, json.dumps(progress, indent=2))

    def write_result(self) -> str:
        """Write the final result to the outbox. Returns result path."""
        os.makedirs(self.outbox_dir, exist_ok=True)
        task_id = self.task_data.get("task_id", "unknown") if self.task_data else "unknown"

        if self.simulate_failure:
            result = {
                "task_id": task_id,
                "agent_id": os.environ.get("AGENT_ID", "mock-agent"),
                "status": "error",
                "summary": "Mock agent simulated failure",
                "output_path": None,
                "files_created": [],
                "files_modified": [],
                "warnings": [],
                "error": "Simulated failure for testing",
                "budget_spent": 0.0,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            result = {
                "task_id": task_id,
                "agent_id": os.environ.get("AGENT_ID", "mock-agent"),
                "status": "completed",
                "summary": f"Mock agent completed task: {task_id}",
                "output_path": None,
                "files_created": [],
                "files_modified": [],
                "warnings": [],
                "error": None,
                "budget_spent": 0.0,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

        path = os.path.join(self.outbox_dir, f"result-{task_id}.json")
        atomic_write(path, json.dumps(result, indent=2))
        return path

    def run(self) -> dict:
        """Execute the full mock agent lifecycle.

        1. Read inbox
        2. Write heartbeat
        3. Write progress updates during delay
        4. Write final result

        Returns the result dict.
        """
        self.read_inbox()
        self.write_heartbeat()

        # Progress updates during delay
        if self.delay > 0:
            steps = [25, 50, 75]
            step_delay = self.delay / len(steps)
            for pct in steps:
                time.sleep(step_delay)
                self.write_progress(pct)
        else:
            self.write_progress(50)

        self.write_result()

        # Read back the result
        task_id = self.task_data.get("task_id", "unknown") if self.task_data else "unknown"
        result_path = os.path.join(self.outbox_dir, f"result-{task_id}.json")
        with open(result_path) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Dry-Run Pipeline
# ---------------------------------------------------------------------------

def is_dry_run() -> bool:
    """Check if we're in dry-run mode."""
    return os.environ.get("ORCHESTRATOR_DRY_RUN", "") == "1"


def run_mock_pipeline(
    tasks: List[Dict],
    graph: TaskGraph,
    simulate_failures: Optional[Dict[str, bool]] = None,
    workspace_root: Optional[str] = None,
) -> Dict[str, dict]:
    """Run a complete dry-run pipeline with mock agents.

    Args:
        tasks: List of task dicts with 'task_id', 'description', 'department'
        graph: Pre-built TaskGraph with dependencies
        simulate_failures: Optional dict {task_id: True} for failure simulation
        workspace_root: Root directory for agent workspaces

    Returns:
        Dict mapping task_id -> result dict
    """
    if simulate_failures is None:
        simulate_failures = {}
    if workspace_root is None:
        workspace_root = os.environ.get(
            "ORCHESTRATED_ROOT",
            os.path.expanduser("~/projects/_orchestrated"),
        )

    results = {}

    # Process tasks in dependency order
    max_iterations = len(tasks) * 3  # Safety limit
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        ready = graph.get_ready_tasks()
        if not ready:
            # Check if all tasks are done
            all_done = all(
                graph.get_task(t["task_id"]).status in ("completed", "failed", "dependency_failed")
                for t in tasks
                if t["task_id"] in graph._nodes
            )
            if all_done or not any(
                graph.get_task(t["task_id"]).status == "pending"
                for t in tasks
                if t["task_id"] in graph._nodes
            ):
                break
            continue

        for task_id in ready:
            node = graph.get_task(task_id)
            if node is None:
                continue

            # Find task details
            task_detail = next(
                (t for t in tasks if t["task_id"] == task_id), None
            )
            if task_detail is None:
                continue

            # Create workspace
            agent_name = node.department or "mock"
            workspace = os.path.join(workspace_root, "_grunts", f"mock-{task_id}")
            os.makedirs(os.path.join(workspace, "inbox"), exist_ok=True)
            os.makedirs(os.path.join(workspace, "outbox"), exist_ok=True)

            # Write task to inbox
            inbox_path = os.path.join(workspace, "inbox", f"task-{task_id}.json")
            with open(inbox_path, "w") as f:
                json.dump(task_detail, f, indent=2)

            # Mark in progress
            graph.mark_in_progress(task_id, agent_name)

            # Run mock agent
            should_fail = simulate_failures.get(task_id, False)
            agent = MockAgent(
                workspace=workspace,
                delay=0.0,  # No delay in pipeline mode
                simulate_failure=should_fail,
            )
            result = agent.run()
            results[task_id] = result

            # Update graph
            if result.get("status") == "completed":
                result_path = os.path.join(
                    workspace, "outbox", f"result-{task_id}.json"
                )
                graph.mark_completed(task_id, result_path)
            else:
                graph.propagate_failure(task_id)

    return results
