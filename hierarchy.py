"""hierarchy.py -- Core hierarchy management for Orchestrator v3.

Wraps existing orchestrator scripts to add named-agent lifecycle,
inbox/outbox delegation, and department-based task routing.
"""

import json
import logging
import os
import re
import signal
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from registry import Registry, AgentState, generate_agent_id
from context_assembler import ContextAssembler
from budget_tracker import BudgetTracker, BudgetExhaustedError

logger = logging.getLogger(__name__)

ORCHESTRATED_ROOT = Path(os.environ.get(
    "ORCHESTRATED_ROOT",
    str(Path.home() / "projects" / "_orchestrated"),
))

GHOST_HEARTBEAT_THRESHOLD_SECONDS = 90
SPAWN_LIVENESS_DELAY_SECONDS = 2
DISMISS_SIGTERM_TIMEOUT_SECONDS = 10

# Regex fast-path routing rules (absorbed from task-router.py)
ROUTING_RULES = [
    (r"\b(research|investigate|analyze|study|explore|find out)\b", "atlas"),
    (r"\b(write|draft|blog|article|post|newsletter|content)\b", "scribe"),
    (r"\b(build|code|implement|fix|debug|develop|deploy|script)\b", "forge"),
    (r"\b(classify|recruit|assess|evaluate workforce)\b", "maven"),
]


class HierarchyError(Exception):
    """Base error for hierarchy operations."""


class SpawnFailedError(HierarchyError):
    """Agent process exited immediately after spawn."""


class AgentNotFoundError(HierarchyError):
    """No active agent with the given agent_id."""


class Hierarchy:
    """Core hierarchy management: recruit, dismiss, roster, route, delegate."""

    def __init__(
        self,
        registry: Registry,
        assembler: ContextAssembler,
        budget_tracker: BudgetTracker,
        orchestrated_root: Optional[Path] = None,
        orchestrator_sh: Optional[str] = None,
    ):
        self.registry = registry
        self.assembler = assembler
        self.budget_tracker = budget_tracker
        self.root = orchestrated_root or ORCHESTRATED_ROOT
        self.heads_dir = self.root / "_heads"
        self.grunts_dir = self.root / "_grunts"
        self.done_dir = self.root / "_done"
        self.orchestrator_sh = orchestrator_sh or os.environ.get(
            "ORCHESTRATOR_SH",
            os.path.join(str(Path(__file__).parent), "orchestrator.sh"),
        )

    def recruit(
        self,
        agent_name: str,
        task_id: str,
        task_description: str,
        budget: float,
    ) -> str:
        """Recruit a named agent. Returns agent_id."""
        # 1. Load and validate profile
        try:
            profile = self.registry.load_profile(agent_name)
        except FileNotFoundError:
            raise ValueError(f"No profile found for agent: {agent_name}")
        role = profile.get("role", "grunt")

        # 1b. Verify Claude binary is accessible
        if not shutil.which("claude"):
            raise SpawnFailedError("Claude binary not found on PATH")

        # 2. Check budget
        department = profile.get("department", "general")
        self.budget_tracker.allocate(
            task_id=task_id,
            agent_id=agent_name,  # temporary, replaced after spawn
            department=department,
            amount=budget,
        )

        # Everything after budget allocation is wrapped in try/except
        # to release budget on failure
        try:
            # 3. Generate agent ID
            agent_id = generate_agent_id(agent_name)

            # 4. Determine workspace path
            if role == "head":
                workspace = self.heads_dir / agent_name
            else:
                workspace = self.grunts_dir / agent_id

            # 5. Handle stale workspace from crashed session
            self._clean_stale_state(agent_name, role, workspace)

            # 6. Create workspace via context assembler
            self.assembler.create_workspace(
                profile=profile,
                agent_id=agent_id,
                task=task_description,
                budget=budget,
                workspace=str(workspace),
            )

            # Ensure outbox/_processed/ exists for heads
            if role == "head":
                os.makedirs(workspace / "outbox" / "_processed", exist_ok=True)

            # 7. Determine depth
            parent_depth = int(os.environ.get("ORCHESTRATOR_DEPTH", "0"))
            depth = parent_depth + 1

            # 8. Spawn the Claude session
            machine = socket.gethostname().lower()
            env = os.environ.copy()
            env.update({
                "ORCHESTRATOR_DEPTH": str(depth),
                "AGENT_NAME": agent_name,
                "AGENT_WORKSPACE": str(workspace),
                "AGENT_ID": agent_id,
                "AGENT_ROLE": role,
            })
            # Remove CLAUDECODE to prevent nested session detection
            env.pop("CLAUDECODE", None)

            model = profile.get("model", "sonnet")
            allowed_tools = profile.get("allowed_tools", [])

            # Build spawn command
            spawn_cmd = [
                self.orchestrator_sh,
                "spawn",
                "--model", model,
                "--workspace", str(workspace),
                "--agent-id", agent_id,
            ]
            if allowed_tools:
                spawn_cmd.extend(["--allowed-tools", ",".join(allowed_tools)])

            try:
                proc = subprocess.Popen(
                    spawn_cmd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                raise SpawnFailedError(
                    f"orchestrator.sh not found at {self.orchestrator_sh}"
                )

            # 9. Post-spawn liveness check
            time.sleep(SPAWN_LIVENESS_DELAY_SECONDS)
            ret = proc.poll()
            if ret is not None:
                # Process already exited
                self.registry.remove_active_state(agent_id)
                raise SpawnFailedError(
                    f"Agent {agent_id} exited immediately with code {ret}"
                )

            # 10. Write active state
            session_id = f"sess-{uuid.uuid4().hex[:8]}"
            self.registry.create_active_state(
                agent_id=agent_id,
                profile=profile,
                pid=proc.pid,
                session_id=session_id,
                machine=machine,
                task_id=task_id,
                budget=budget,
            )

            logger.info("Recruited %s as %s (pid=%d, task=%s, budget=%.2f)",
                        agent_name, agent_id, proc.pid, task_id, budget)
            return agent_id

        except Exception:
            # Release budget on any failure after allocation
            self.budget_tracker.release(task_id=task_id)
            logger.error("Failed to recruit %s for task %s, budget released",
                         agent_name, task_id)
            raise

    def dismiss(self, agent_id: str, archive: bool = True) -> None:
        """Dismiss an agent: stop process, optionally archive workspace."""
        state = self.registry.read_active_state(agent_id)
        if state is None:
            raise AgentNotFoundError(f"No active agent: {agent_id}")

        logger.info("Dismissing %s (pid=%d, archive=%s)", agent_id, state.pid, archive)

        # Send SIGTERM, then SIGKILL if needed
        try:
            os.kill(state.pid, signal.SIGTERM)
            deadline = time.time() + DISMISS_SIGTERM_TIMEOUT_SECONDS
            while time.time() < deadline:
                try:
                    os.kill(state.pid, 0)
                    time.sleep(0.5)
                except ProcessLookupError:
                    break
            else:
                # Still alive after timeout
                logger.warning("Agent %s did not exit after SIGTERM, sending SIGKILL", agent_id)
                try:
                    os.kill(state.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except ProcessLookupError:
            pass  # Already dead

        # Archive or leave workspace
        if archive:
            role = state.role
            if role == "head":
                workspace = self.heads_dir / state.name
            else:
                workspace = self.grunts_dir / agent_id
            if workspace.exists():
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                dest = self.done_dir / f"{state.name}-{ts}-{uuid.uuid4().hex[:4]}"
                os.makedirs(self.done_dir, exist_ok=True)
                shutil.move(str(workspace), str(dest))

        # Remove active state
        self.registry.remove_active_state(agent_id)

    def roster(self, machine: Optional[str] = None) -> List[dict]:
        """List all active agents, with ghost detection."""
        states = self.registry.list_active_states()
        result = []
        now = datetime.now(timezone.utc)

        for state in states:
            entry = asdict(state)

            # Check heartbeat for ghost detection
            is_ghost = False
            if state.role == "head":
                hb_path = self.heads_dir / state.name / "heartbeat.json"
            else:
                hb_path = self.grunts_dir / state.agent_id / "heartbeat.json"

            if hb_path.exists():
                try:
                    with open(hb_path) as f:
                        hb = json.load(f)
                    updated = datetime.fromisoformat(hb["updated_at"])
                    if (now - updated).total_seconds() > GHOST_HEARTBEAT_THRESHOLD_SECONDS:
                        is_ghost = True
                except (json.JSONDecodeError, KeyError, ValueError):
                    is_ghost = True
            else:
                # No heartbeat file — check last_heartbeat from state
                try:
                    last_hb = datetime.fromisoformat(state.last_heartbeat)
                    if (now - last_hb).total_seconds() > GHOST_HEARTBEAT_THRESHOLD_SECONDS:
                        is_ghost = True
                except (ValueError, TypeError):
                    is_ghost = True

            entry["is_ghost"] = is_ghost

            if machine and state.machine != machine:
                continue
            result.append(entry)

        result.sort(key=lambda x: x["name"])
        return result

    def route_task(self, task_description: str, task_id: str = "") -> Optional[str]:
        """Route a task to a department head via regex fast-path.
        Returns head name or None if ambiguous."""
        text = task_description.lower()
        for pattern, head in ROUTING_RULES:
            if re.search(pattern, text):
                return head
        return None

    def delegate_to_head(self, agent_name: str, task: dict) -> str:
        """Write a task to a head's inbox. Returns task_id."""
        task = dict(task)  # Don't mutate caller's dict
        task_id = task.get("task_id") or f"task-{uuid.uuid4().hex[:8]}"
        task["task_id"] = task_id
        task.setdefault("created_at", datetime.now(timezone.utc).isoformat())

        inbox = self.heads_dir / agent_name / "inbox"
        os.makedirs(inbox, exist_ok=True)

        # Atomic write
        tmp_path = inbox / f".tmp-task-{task_id}.json"
        final_path = inbox / f"task-{task_id}.json"
        with open(tmp_path, "w") as f:
            json.dump(task, f, indent=2)
        os.replace(str(tmp_path), str(final_path))

        return task_id

    def collect_from_head(self, agent_name: str) -> List[dict]:
        """Collect ready results from a head's outbox."""
        outbox = self.heads_dir / agent_name / "outbox"
        if not outbox.exists():
            return []

        processed_dir = outbox / "_processed"
        os.makedirs(processed_dir, exist_ok=True)
        results = []

        for entry in sorted(outbox.iterdir()):
            if not entry.name.startswith("result-") or not entry.name.endswith(".json"):
                continue

            # Check for .ready sentinel
            task_id_part = entry.stem.replace("result-", "")
            ready_path = outbox / f"result-{task_id_part}.ready"
            if not ready_path.exists():
                continue

            # Read with retry
            data = None
            for attempt in range(3):
                try:
                    with open(entry) as f:
                        data = json.load(f)
                    break
                except (json.JSONDecodeError, FileNotFoundError):
                    if attempt < 2:
                        time.sleep(0.1 * (2 ** attempt))

            if data is None:
                continue

            results.append(data)

            # Move to _processed
            shutil.move(str(entry), str(processed_dir / entry.name))
            if ready_path.exists():
                shutil.move(str(ready_path), str(processed_dir / ready_path.name))

        return results

    def get_agent_history(self, agent_name: str) -> List[dict]:
        """Get completed task history for a named agent from events.jsonl."""
        events_path = self.root / "events.jsonl"
        if not events_path.exists():
            return []

        history = []
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("agent_name") == agent_name:
                        history.append(event)
                except json.JSONDecodeError:
                    continue

        history.sort(key=lambda e: e.get("timestamp", ""))
        return history

    def _clean_stale_state(
        self, agent_name: str, role: str, workspace: Path
    ) -> None:
        """Clean stale active state from a crashed session."""
        for state in self.registry.list_active_states():
            if state.name != agent_name:
                continue
            # Check if process is still alive
            try:
                os.kill(state.pid, 0)
                # Process is alive — skip (could be PID recycling, but
                # we can't distinguish without /proc; err on safe side)
            except ProcessLookupError:
                # Process dead — clean stale state
                logger.info("Cleaning stale state for %s (pid=%d was dead)",
                            agent_name, state.pid)
                self.registry.remove_active_state(state.agent_id)
                # For heads, clean empty work dirs but preserve memory/
                if role == "head" and workspace.exists():
                    for subdir in ("inbox", "outbox", "current"):
                        sub = workspace / subdir
                        if sub.exists() and sub.is_dir():
                            files = [f for f in sub.iterdir()
                                     if not f.name.startswith("_")]
                            if not files:
                                shutil.rmtree(str(sub))
            except PermissionError:
                # Process exists but owned by another user — leave it alone
                logger.warning("PID %d for %s is alive (owned by another user), skipping",
                               state.pid, agent_name)
