"""Workspace guard: isolation, confinement, and lifecycle management.

Enforces that agents operate within strict boundaries:
- Grunts cannot escape their workspace
- Heads can only write within their own workspace
- Heads can read other heads' memory/ directories (cross-read)
- Heads cannot write to other heads' memory/ directories
- Depth enforcement prevents infinite spawning chains
"""

import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STALE_GRUNT_DAYS = 7
STALE_ARCHIVE_DAYS = 30

# Grunt workspace structure files
GRUNT_WORKSPACE_FILES = ["CLAUDE.md", "prompt.md", "result.json"]

# Head workspace subdirectories
HEAD_WORKSPACE_DIRS = ["inbox", "outbox", "memory", "current"]


# ---------------------------------------------------------------------------
# Workspace Lifecycle
# ---------------------------------------------------------------------------

def create_grunt_workspace(workspace: str) -> str:
    """Create a grunt workspace with standard structure.

    Creates directory with CLAUDE.md, prompt.md, and result.json placeholders.
    Returns the workspace path.
    """
    os.makedirs(workspace, exist_ok=True)
    for fname in GRUNT_WORKSPACE_FILES:
        path = os.path.join(workspace, fname)
        if not os.path.exists(path):
            with open(path, "w") as f:
                if fname == "result.json":
                    f.write("{}")
                else:
                    f.write("")
    return workspace


def create_head_workspace(workspace: str) -> str:
    """Create a head workspace with persistent structure.

    Creates inbox/, outbox/, memory/, current/ subdirectories.
    Idempotent: safe to call on existing workspace.
    Returns the workspace path.
    """
    os.makedirs(workspace, exist_ok=True)
    for subdir in HEAD_WORKSPACE_DIRS:
        os.makedirs(os.path.join(workspace, subdir), exist_ok=True)
    return workspace


def archive_workspace(workspace: str, done_dir: str) -> str:
    """Archive a workspace to _done/ with timestamp suffix.

    Returns the destination path.
    """
    os.makedirs(done_dir, exist_ok=True)
    name = os.path.basename(workspace)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = os.path.join(done_dir, f"{name}-{ts}")
    # Handle collision
    if os.path.exists(dest):
        dest = f"{dest}-{os.getpid()}"
    shutil.move(workspace, dest)
    return dest


def head_workspace_persists_memory(workspace: str) -> bool:
    """Check if a head workspace's memory/ directory exists and has content."""
    memory_dir = os.path.join(workspace, "memory")
    if not os.path.isdir(memory_dir):
        return False
    return len(os.listdir(memory_dir)) > 0


def cleanup_stale_workspaces(
    grunts_dir: str,
    done_dir: str,
    grunt_max_age_days: int = STALE_GRUNT_DAYS,
    archive_max_age_days: int = STALE_ARCHIVE_DAYS,
) -> dict:
    """Clean up stale grunt workspaces and old archives.

    Returns dict with counts of cleaned items.
    """
    now = time.time()
    cleaned = {"grunts": 0, "archives": 0}

    # Clean stale grunt workspaces
    if os.path.isdir(grunts_dir):
        for entry in os.scandir(grunts_dir):
            if not entry.is_dir():
                continue
            age_days = (now - entry.stat().st_mtime) / 86400
            if age_days > grunt_max_age_days:
                shutil.rmtree(entry.path)
                cleaned["grunts"] += 1

    # Clean old archives
    if os.path.isdir(done_dir):
        for entry in os.scandir(done_dir):
            if not entry.is_dir():
                continue
            age_days = (now - entry.stat().st_mtime) / 86400
            if age_days > archive_max_age_days:
                shutil.rmtree(entry.path)
                cleaned["archives"] += 1

    return cleaned


# ---------------------------------------------------------------------------
# Workspace Confinement
# ---------------------------------------------------------------------------

def check_write_allowed(
    target_path: str,
    agent_workspace: str,
    agent_role: str,
    agent_name: str,
    heads_base_dir: Optional[str] = None,
) -> tuple:
    """Check if a write (Edit/Write) operation is allowed.

    Returns (allowed: bool, reason: str).
    Resolves symlinks to prevent escape attacks.
    """
    try:
        resolved_target = os.path.realpath(target_path)
    except (OSError, ValueError):
        return False, f"Cannot resolve target path: {target_path}"

    resolved_workspace = os.path.realpath(agent_workspace)

    # Check if target is within the agent's own workspace
    if resolved_target.startswith(resolved_workspace + os.sep) or resolved_target == resolved_workspace:
        return True, "Within own workspace"

    # For heads: block writes to other heads' memory directories
    if agent_role == "head" and heads_base_dir:
        resolved_heads = os.path.realpath(heads_base_dir)
        # Check if writing to another head's memory/
        match = _extract_head_memory_path(resolved_target, resolved_heads)
        if match and match != agent_name:
            return False, f"Cannot write to another head's memory directory ({match})"

    return False, f"Write outside workspace boundary: {target_path}"


def check_read_allowed(
    target_path: str,
    agent_workspace: str,
    agent_role: str,
    heads_base_dir: Optional[str] = None,
) -> tuple:
    """Check if a Read operation is allowed for cross-read permissions.

    Heads can read other heads' memory/ directories.
    Returns (allowed: bool, reason: str).
    """
    resolved_target = os.path.realpath(target_path)
    resolved_workspace = os.path.realpath(agent_workspace)

    # Always allowed within own workspace
    if resolved_target.startswith(resolved_workspace + os.sep) or resolved_target == resolved_workspace:
        return True, "Within own workspace"

    # Heads can read any head's memory/ directory
    if agent_role == "head" and heads_base_dir:
        resolved_heads = os.path.realpath(heads_base_dir)
        head_name = _extract_head_memory_path(resolved_target, resolved_heads)
        if head_name:
            return True, f"Cross-read from {head_name}'s memory"

    # For non-memory reads outside workspace, allow (read is generally safe)
    # The session-guard.sh handles broader read restrictions
    return True, "Read allowed"


def _extract_head_memory_path(resolved_path: str, resolved_heads: str) -> Optional[str]:
    """Extract the head name if the path is under _heads/<name>/memory/.

    Returns the head name if path is in a head's memory directory, None otherwise.
    """
    if not resolved_path.startswith(resolved_heads + os.sep):
        return None

    relative = resolved_path[len(resolved_heads) + 1:]
    parts = relative.split(os.sep)
    if len(parts) >= 2 and parts[1] == "memory":
        return parts[0]
    return None


# ---------------------------------------------------------------------------
# Depth Enforcement
# ---------------------------------------------------------------------------

def check_depth_allowed(current_depth: int, max_depth: int = 2) -> tuple:
    """Check if spawning is allowed at the current depth.

    Returns (allowed: bool, reason: str).
    Depth >= max_depth blocks spawning.
    """
    if current_depth >= max_depth:
        return False, f"Depth limit reached (depth={current_depth}). Cannot spawn further agents."
    return True, f"Spawning allowed at depth {current_depth}"


def check_spawn_command(
    command: str,
    current_depth: int,
    max_depth: int = 2,
) -> tuple:
    """Check if a Bash command is a spawn attempt and if it's allowed.

    Returns (allowed: bool, reason: str).
    """
    if current_depth < max_depth:
        return True, "Depth within limits"

    # Pattern-match for spawn commands
    spawn_patterns = [
        r"\bclaude\b",
        r"orchestrator\.sh\s+spawn",
        r"orchestrator\.sh\s+recruit",
    ]
    for pattern in spawn_patterns:
        if re.search(pattern, command):
            return False, f"Depth limit reached (depth={current_depth}). Cannot spawn further agents."

    return True, "Not a spawn command"
