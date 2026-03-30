"""Inbound relay message handler for the orchestrator peer network.

Processes relay messages arriving on the local machine and routes them
to the appropriate component (CEO inbox, task graph, CEO registry).
Includes retry queue management for undeliverable messages.
"""

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from communication import atomic_write

logger = logging.getLogger(__name__)

_orchestrated_root = os.environ.get(
    "ORCHESTRATED_ROOT",
    os.path.expanduser("~/projects/_orchestrated"),
)
DEFAULT_RELAY_QUEUE = os.path.join(_orchestrated_root, "relay-queue")
DEFAULT_INBOX_ROOT = os.path.join(_orchestrated_root, "_heads")


def handle_relay_message(
    message_type: str,
    payload: dict,
    inbox_root: Optional[str] = None,
    registry_path: Optional[str] = None,
    relay_queue_dir: Optional[str] = None,
) -> bool:
    """Route an incoming relay message to the correct handler.

    Message types:
    - "task": Write to local CEO's inbox for delegation
    - "task_result": Write to result collection area
    - "ceo_heartbeat": Update CEO registry (Day only)
    - "ceo_roster": Respond with local roster data

    Returns True if handled successfully, False if queued for retry.
    """
    inbox_root = inbox_root or DEFAULT_INBOX_ROOT
    relay_queue_dir = relay_queue_dir or DEFAULT_RELAY_QUEUE

    try:
        if message_type == "task":
            return _handle_task(payload, inbox_root)
        elif message_type == "task_result":
            return _handle_task_result(payload, inbox_root)
        elif message_type == "ceo_heartbeat":
            return _handle_heartbeat(payload, registry_path)
        elif message_type == "ceo_roster":
            return _handle_roster_query(payload, inbox_root)
        else:
            logger.warning("Unknown relay message type: %s", message_type)
            return False
    except (OSError, PermissionError) as e:
        logger.error("Failed to handle %s message: %s", message_type, e)
        queue_for_retry({"type": message_type, "payload": payload},
                        relay_queue_dir)
        return False


def _handle_task(payload: dict, inbox_root: str) -> bool:
    """Write incoming task to the target department head's inbox."""
    department = payload.get("department", "")
    task_id = payload.get("task_id", "unknown")

    # Map department to head name
    dept_to_head = {
        "research": "atlas",
        "dev": "forge",
        "content": "scribe",
        "hr": "maven",
    }
    head = dept_to_head.get(department)
    if not head:
        logger.error("No head for department '%s', queuing task %s",
                      department, task_id)
        return False

    inbox = os.path.join(inbox_root, head, "inbox")
    os.makedirs(inbox, exist_ok=True)

    filepath = os.path.join(inbox, f"task-{task_id}.json")
    data = json.dumps(payload, indent=2)
    atomic_write(filepath, data)

    logger.info("Delivered relay task %s to %s inbox", task_id, head)
    return True


def _handle_task_result(payload: dict, inbox_root: str) -> bool:
    """Write incoming task result to the originator's result area."""
    task_id = payload.get("task_id", "unknown")

    # Results go to a central results directory
    results_dir = os.path.join(
        os.path.dirname(inbox_root), "relay-results"
    )
    os.makedirs(results_dir, exist_ok=True)

    filepath = os.path.join(results_dir, f"result-{task_id}.json")
    data = json.dumps(payload, indent=2)
    atomic_write(filepath, data)

    logger.info("Delivered relay result for task %s", task_id)
    return True


def _handle_heartbeat(payload: dict, registry_path: Optional[str]) -> bool:
    """Update CEO registry with heartbeat data (Day only)."""
    if not registry_path:
        logger.warning("No registry path for heartbeat handling")
        return False

    machine = payload.get("machine", "")
    if not machine:
        return False

    # Load existing registry
    registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path) as f:
                registry = json.load(f)
        except (json.JSONDecodeError, OSError):
            registry = {}

    # Update entry
    registry[machine] = payload

    # Atomic write
    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
    tmp_path = registry_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(registry, f, indent=2)
    os.replace(tmp_path, registry_path)

    return True


def _handle_roster_query(payload: dict, inbox_root: str) -> bool:
    """Handle a roster query -- respond with local agent info.

    In practice, the response would be sent back via relay.
    For now, we log the query and return True.
    """
    logger.info("Roster query from %s (type=%s)",
                payload.get("from_machine", "unknown"),
                payload.get("query_type", "full"))
    return True


# ---------------------------------------------------------------------------
# Retry Queue Management
# ---------------------------------------------------------------------------

def queue_for_retry(message: dict, queue_dir: str) -> str:
    """Write a message to the relay-queue directory for later retry.

    Uses atomic write pattern (tmp + rename + .ready sentinel).
    Returns the queued file path.
    """
    os.makedirs(queue_dir, exist_ok=True)

    # Generate unique filename
    task_id = message.get("payload", {}).get("task_id", "")
    msg_type = message.get("type", "unknown")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    filename = f"{msg_type}-{task_id}-{ts}.json" if task_id else f"{msg_type}-{ts}.json"

    filepath = os.path.join(queue_dir, filename)
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(message, f, indent=2)
    os.replace(tmp_path, filepath)

    # Write .ready sentinel
    ready_path = os.path.splitext(filepath)[0] + ".ready"
    tmp_ready = ready_path + ".tmp"
    with open(tmp_ready, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    os.replace(tmp_ready, ready_path)

    return filepath


def process_retry_queue(queue_dir: str, handler_fn=None) -> int:
    """Process all queued messages in relay-queue/.

    Called periodically or when CEO starts up.
    Returns number of messages successfully processed.
    """
    if not os.path.isdir(queue_dir):
        return 0

    processed_dir = os.path.join(queue_dir, "_processed")
    os.makedirs(processed_dir, exist_ok=True)

    count = 0
    for entry in sorted(os.scandir(queue_dir), key=lambda e: e.name):
        if not entry.name.endswith(".json"):
            continue
        if entry.name.startswith(".tmp-"):
            continue

        # Check for .ready sentinel
        ready_path = os.path.splitext(entry.path)[0] + ".ready"
        if not os.path.exists(ready_path):
            continue

        try:
            with open(entry.path) as f:
                message = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Process the message
        success = False
        if handler_fn:
            try:
                success = handler_fn(
                    message.get("type", "unknown"),
                    message.get("payload", {}),
                )
            except Exception as e:
                logger.warning("Retry failed for %s: %s", entry.name, e)
                continue
        else:
            # Default: try handle_relay_message
            try:
                success = handle_relay_message(
                    message.get("type", "unknown"),
                    message.get("payload", {}),
                )
            except Exception:
                continue

        if success:
            # Move to _processed
            shutil.move(entry.path, os.path.join(processed_dir, entry.name))
            if os.path.exists(ready_path):
                shutil.move(ready_path,
                            os.path.join(processed_dir,
                                         os.path.basename(ready_path)))
            count += 1

    return count
