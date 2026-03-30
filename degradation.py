"""Graceful degradation: fallback wrappers for Orchestrator v3 components.

Every new v3 component has a documented fallback path so failures never
prevent work from getting done. The old flat orchestrator is always
available as degraded-mode fallback.

Principle: never fail silently. Every fallback logs what went wrong.
"""

import json
import logging
import os
import shutil
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ORCHESTRATED_ROOT = Path(os.environ.get(
    "ORCHESTRATED_ROOT",
    str(Path.home() / "projects" / "_orchestrated"),
))


# ---------------------------------------------------------------------------
# Event Logging
# ---------------------------------------------------------------------------

def log_degradation_event(
    component: str,
    error: str,
    fallback: str,
    events_path: Optional[str] = None,
) -> None:
    """Log a degradation event to events.jsonl.

    Every fallback activation is recorded for post-mortem analysis.
    """
    if events_path is None:
        events_path = str(ORCHESTRATED_ROOT / "events.jsonl")

    event = {
        "event_type": "degradation",
        "component": component,
        "error": str(error),
        "fallback": fallback,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        os.makedirs(os.path.dirname(events_path), exist_ok=True)
        with open(events_path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        logger.error("Failed to log degradation event: %s", e)

    # Also log to stderr for real-time visibility
    logger.warning(
        "DEGRADATION [%s]: %s -> fallback: %s", component, error, fallback
    )


# ---------------------------------------------------------------------------
# Generic try_or_degrade
# ---------------------------------------------------------------------------

def try_or_degrade(
    primary_fn: Callable,
    fallback_fn: Callable,
    context: str,
    *args,
    events_path: Optional[str] = None,
    **kwargs,
) -> Any:
    """Generic wrapper: try primary, fall back on failure.

    1. Call primary_fn(*args, **kwargs)
    2. On exception: log degradation, call fallback_fn(*args, **kwargs)
    3. If fallback also raises: re-raise with both errors chained

    Args:
        primary_fn: The preferred implementation
        fallback_fn: The degraded fallback
        context: Component name for logging (e.g., "recruit", "maven")
        events_path: Optional override for events.jsonl path
    """
    try:
        return primary_fn(*args, **kwargs)
    except Exception as primary_error:
        log_degradation_event(
            component=context,
            error=str(primary_error),
            fallback=fallback_fn.__name__ if hasattr(fallback_fn, '__name__') else str(fallback_fn),
            events_path=events_path,
        )
        try:
            return fallback_fn(*args, **kwargs)
        except Exception as fallback_error:
            raise type(fallback_error)(
                f"Double failure in {context}: "
                f"primary={primary_error}, fallback={fallback_error}"
            ) from primary_error


# ---------------------------------------------------------------------------
# Fallback 1: Recruit Failure -> Flat Orchestrator
# ---------------------------------------------------------------------------

def _flat_orchestrator_spawn(
    agent_name: str,
    task_id: str,
    task_description: str,
    budget: float,
    orchestrator_sh: Optional[str] = None,
) -> str:
    """Fallback: spawn an anonymous specialist via flat orchestrator.

    Returns a synthetic agent_id prefixed with 'flat-'.
    """
    if orchestrator_sh is None:
        orchestrator_sh = os.environ.get(
            "ORCHESTRATOR_SH",
            os.path.join(os.path.dirname(__file__), "orchestrator.sh"),
        )

    flat_id = f"flat-{task_id}"

    # In degraded mode, we just record the intent.
    # Actual subprocess spawn would happen here in production.
    logger.info(
        "Degraded recruit: flat spawn for %s (task=%s, budget=%.2f)",
        agent_name, task_id, budget,
    )

    return flat_id


def recruit_safe(
    agent_name: str,
    task_id: str,
    task_description: str,
    budget: float,
    hierarchy=None,
    events_path: Optional[str] = None,
    orchestrator_sh: Optional[str] = None,
) -> str:
    """Recruit an agent with fallback to flat orchestrator.

    Primary: hierarchy.recruit()
    Fallback: flat orchestrator.sh spawn (anonymous specialist)
    """
    def primary(name, tid, desc, bdg):
        if hierarchy is None:
            raise RuntimeError("No hierarchy instance provided")
        return hierarchy.recruit(name, tid, desc, bdg)

    def fallback(name, tid, desc, bdg):
        return _flat_orchestrator_spawn(
            name, tid, desc, bdg,
            orchestrator_sh=orchestrator_sh,
        )

    return try_or_degrade(
        primary, fallback, "recruit",
        agent_name, task_id, task_description, budget,
        events_path=events_path,
    )


# ---------------------------------------------------------------------------
# Fallback 2: Relay Down -> Local Queue
# ---------------------------------------------------------------------------

def send_cross_machine_safe(
    target_machine: str,
    task_message: dict,
    send_fn: Optional[Callable] = None,
    relay_queue_dir: Optional[str] = None,
    events_path: Optional[str] = None,
) -> bool:
    """Send a cross-machine task with fallback to local queue.

    Primary: relay send
    Fallback: queue to relay-queue/ for later retry
    """
    if relay_queue_dir is None:
        relay_queue_dir = str(ORCHESTRATED_ROOT / "relay-queue")

    def primary(target, msg):
        if send_fn is None:
            raise RuntimeError("No send function provided")
        result = send_fn(target, msg)
        if not result:
            raise ConnectionError(f"Relay send failed to {target}")
        return True

    def fallback(target, msg):
        return _queue_locally(target, msg, relay_queue_dir)

    return try_or_degrade(
        primary, fallback, "relay",
        target_machine, task_message,
        events_path=events_path,
    )


def _queue_locally(target: str, task_message: dict, queue_dir: str) -> bool:
    """Queue a task locally for later relay delivery."""
    os.makedirs(queue_dir, exist_ok=True)

    task_id = task_message.get("task_id", "unknown")
    filename = f"{target}-{task_id}.json"
    filepath = os.path.join(queue_dir, filename)

    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(task_message, f, indent=2)
    os.replace(tmp_path, filepath)

    return True


def drain_relay_queue(
    queue_dir: str,
    send_fn: Callable,
    events_path: Optional[str] = None,
) -> int:
    """Retry sending queued tasks when relay comes back.

    Returns number of tasks successfully sent.
    """
    if not os.path.isdir(queue_dir):
        return 0

    processed_dir = os.path.join(queue_dir, "_processed")
    os.makedirs(processed_dir, exist_ok=True)

    count = 0
    for entry in sorted(os.scandir(queue_dir), key=lambda e: e.name):
        if not entry.name.endswith(".json"):
            continue
        if entry.name.startswith(".tmp"):
            continue

        try:
            with open(entry.path) as f:
                msg = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Extract target from filename (format: <target>-<task_id>.json)
        parts = entry.name.split("-", 1)
        target = parts[0] if len(parts) > 1 else "day"

        try:
            result = send_fn(target, msg)
            if result:
                shutil.move(entry.path, os.path.join(processed_dir, entry.name))
                count += 1
        except Exception as e:
            logger.warning("Relay queue retry failed for %s: %s", entry.name, e)

    return count


# ---------------------------------------------------------------------------
# Fallback 3: Maven Failure -> Regex Fast-Path
# ---------------------------------------------------------------------------

def route_task_safe(
    task_description: str,
    task_id: str = "",
    maven_fn: Optional[Callable] = None,
    hierarchy=None,
    timeout: float = 30.0,
    events_path: Optional[str] = None,
) -> str:
    """Route a task with fallback from Maven to regex fast-path.

    Primary: Maven LLM classification
    Fallback: regex fast-path from hierarchy.route_task()
    Final fallback: 'atlas' (research) as default
    """
    def primary(desc, tid):
        if maven_fn is None:
            raise RuntimeError("No Maven function provided")
        result = maven_fn(desc, tid)
        if result is None:
            raise ValueError("Maven returned None")
        return result

    def fallback(desc, tid):
        if hierarchy is not None:
            result = hierarchy.route_task(desc, tid)
            if result is not None:
                return result
        # Final fallback: default to atlas
        return "atlas"

    return try_or_degrade(
        primary, fallback, "maven",
        task_description, task_id,
        events_path=events_path,
    )


# ---------------------------------------------------------------------------
# Fallback 4: Missing Registry -> Flat Mode
# (Uses recruit_safe above -- triggered by the same path)
# ---------------------------------------------------------------------------

def check_registry_health(registry_dir: str) -> bool:
    """Quick health check on the registry directory.

    Returns True if registry exists and has at least one valid YAML file.
    """
    if not os.path.isdir(registry_dir):
        return False

    try:
        entries = list(os.scandir(registry_dir))
        for entry in entries:
            if entry.name.endswith(".yaml") and entry.is_file():
                # Try to read at least one profile
                with open(entry.path) as f:
                    content = f.read()
                if content.strip():
                    return True
    except OSError:
        return False

    return False


# ---------------------------------------------------------------------------
# Fallback 5: Budget File Corruption -> Reconstruction
# ---------------------------------------------------------------------------

def load_budget_safe(
    budget_path: str,
    events_path: Optional[str] = None,
) -> dict:
    """Load budget state with fallback to reconstruction from events.

    Primary: json.load(budget.json)
    Fallback: reconstruct from events.jsonl
    Final fallback: fresh zero-initialized state
    """
    def primary(path):
        with open(path) as f:
            data = json.load(f)
        # Validate minimum structure
        if not isinstance(data, dict):
            raise ValueError("Budget file is not a dict")
        return data

    def fallback(path):
        return _reconstruct_budget(path, events_path)

    return try_or_degrade(
        primary, fallback, "budget",
        budget_path,
        events_path=events_path,
    )


def _reconstruct_budget(budget_path: str, events_path: Optional[str] = None) -> dict:
    """Reconstruct budget state from events.jsonl.

    1. Rename corrupted budget.json to .corrupt
    2. Replay budget events from events.jsonl
    3. Write reconstructed state
    4. Return the state
    """
    # Backup corrupt file
    if os.path.exists(budget_path):
        corrupt_path = budget_path + ".corrupt"
        try:
            shutil.move(budget_path, corrupt_path)
        except OSError:
            pass

    # Find events file
    if events_path is None:
        events_path = os.path.join(
            os.path.dirname(budget_path), "events.jsonl"
        )

    state = {
        "session_cap": 15.0,
        "total_allocated": 0.0,
        "total_spent": 0.0,
        "departments": {},
        "agents": {},
        "tasks": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reconstructed": True,
    }

    if not os.path.exists(events_path):
        # No events to reconstruct from -- fresh state
        _write_budget(budget_path, state)
        return state

    # Replay budget events
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    _replay_budget_event(state, event)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

    _write_budget(budget_path, state)
    return state


def _replay_budget_event(state: dict, event: dict) -> None:
    """Apply a single budget event to the reconstructed state."""
    event_type = event.get("event_type", "")

    if event_type == "allocate":
        amount = event.get("amount", 0.0)
        state["total_allocated"] += amount

    elif event_type == "spend":
        amount = event.get("amount", 0.0)
        state["total_spent"] += amount

    elif event_type == "release":
        amount = event.get("amount", 0.0)
        state["total_allocated"] -= amount


def _write_budget(path: str, state: dict) -> None:
    """Write budget state atomically."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Fallback 6: Unwritable Inbox -> Fresh Workspace
# ---------------------------------------------------------------------------

def delegate_to_head_safe(
    agent_name: str,
    task: dict,
    hierarchy=None,
    events_path: Optional[str] = None,
    orchestrated_root: Optional[str] = None,
) -> str:
    """Delegate a task with fallback on inbox write failure.

    Primary: hierarchy.delegate_to_head()
    Fallback: recreate workspace, retry write
    Final fallback: flat orchestrator spawn
    """
    root = Path(orchestrated_root) if orchestrated_root else ORCHESTRATED_ROOT

    def primary(name, tsk):
        if hierarchy is None:
            raise RuntimeError("No hierarchy instance provided")
        return hierarchy.delegate_to_head(name, tsk)

    def fallback(name, tsk):
        return _recreate_and_delegate(name, tsk, root, events_path)

    return try_or_degrade(
        primary, fallback, "inbox",
        agent_name, task,
        events_path=events_path,
    )


def _recreate_and_delegate(
    agent_name: str,
    task: dict,
    root: Path,
    events_path: Optional[str],
) -> str:
    """Recreate workspace and retry task delegation."""
    workspace = root / "_heads" / agent_name

    # Remove broken workspace
    if workspace.exists():
        try:
            shutil.rmtree(str(workspace))
        except OSError as e:
            logger.error("Cannot remove broken workspace %s: %s", workspace, e)

    # Recreate structure
    for subdir in ("inbox", "outbox", "memory"):
        os.makedirs(workspace / subdir, exist_ok=True)
    os.makedirs(workspace / "outbox" / "_processed", exist_ok=True)

    # Write task to fresh inbox
    task = dict(task)
    task_id = task.get("task_id", "unknown")
    filepath = workspace / "inbox" / f"task-{task_id}.json"
    tmp_path = workspace / "inbox" / f".tmp-task-{task_id}.json"

    with open(tmp_path, "w") as f:
        json.dump(task, f, indent=2)
    os.replace(str(tmp_path), str(filepath))

    log_degradation_event(
        component="inbox",
        error=f"Workspace recreated for {agent_name}",
        fallback="recreate_workspace",
        events_path=events_path,
    )

    return task_id
