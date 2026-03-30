"""Org chart API handlers: roster, hierarchy tree, agent details.

Provides handler functions for the EMOC dashboard org chart tab.
Reads agent profiles (YAML), active state (JSON), events (JSONL),
and memory files to build roster, hierarchy tree, and agent detail
responses.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

GHOST_HEARTBEAT_THRESHOLD_SECONDS = 90


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------

def build_roster(active_dir: str) -> dict:
    """Read all active state files and return roster with ghost detection.

    Returns {"agents": [...]}, empty list if directory missing or empty.
    Each agent entry includes all AgentState fields plus is_ghost flag.
    """
    agents = []
    if not os.path.isdir(active_dir):
        return {"agents": agents}

    now = datetime.now(timezone.utc)
    for filename in os.listdir(active_dir):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(active_dir, filename)
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Ghost detection based on last_heartbeat
        is_ghost = False
        try:
            last_hb = datetime.fromisoformat(data.get("last_heartbeat", ""))
            if (now - last_hb).total_seconds() > GHOST_HEARTBEAT_THRESHOLD_SECONDS:
                is_ghost = True
        except (ValueError, TypeError):
            is_ghost = True

        data["is_ghost"] = is_ghost
        agents.append(data)

    agents.sort(key=lambda a: a.get("name", ""))
    return {"agents": agents}


# ---------------------------------------------------------------------------
# Org Chart Tree
# ---------------------------------------------------------------------------

def _load_profiles(registry_dir: str) -> list:
    """Load all YAML profiles from a directory. Returns list of dicts."""
    profiles = []
    if not os.path.isdir(registry_dir):
        return profiles
    for filename in os.listdir(registry_dir):
        if not filename.endswith(".yaml"):
            continue
        path = os.path.join(registry_dir, filename)
        try:
            with open(path) as f:
                profile = yaml.safe_load(f)
            if isinstance(profile, dict):
                profiles.append(profile)
        except (yaml.YAMLError, OSError):
            logger.warning("Skipping malformed profile: %s", path)
    return profiles


def _load_active_states(active_dir: str) -> dict:
    """Load active state files, keyed by agent name.

    Returns dict mapping name -> state_dict.
    If multiple agents share a name, the most recent is kept.
    """
    states = {}
    if not os.path.isdir(active_dir):
        return states
    for filename in os.listdir(active_dir):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(active_dir, filename)
        try:
            with open(path) as f:
                data = json.load(f)
            name = data.get("name", "")
            states[name] = data
        except (json.JSONDecodeError, OSError):
            continue
    return states


def _is_ghost(state: Optional[dict]) -> bool:
    """Check if an agent state represents a ghost (stale heartbeat)."""
    if state is None:
        return True
    now = datetime.now(timezone.utc)
    try:
        last_hb = datetime.fromisoformat(state.get("last_heartbeat", ""))
        return (now - last_hb).total_seconds() > GHOST_HEARTBEAT_THRESHOLD_SECONDS
    except (ValueError, TypeError):
        return True


def build_org_chart(
    registry_dir: str,
    roles_dir: str,
    active_dir: str,
) -> dict:
    """Build nested hierarchy tree for D3.js rendering.

    Structure: CEO -> heads -> grunts.
    Profiles without active state appear as ghost nodes.
    Returns D3-compatible nested dict.
    """
    head_profiles = _load_profiles(registry_dir)
    grunt_profiles = {p["name"]: p for p in _load_profiles(roles_dir)}
    active_states = _load_active_states(active_dir)

    # Build head children
    head_nodes = []
    for profile in sorted(head_profiles, key=lambda p: p.get("name", "")):
        name = profile["name"]
        state = active_states.get(name)
        ghost = _is_ghost(state)

        node = {
            "name": name,
            "role": "head",
            "department": profile.get("department", ""),
            "status": state.get("status", "ghost") if state and not ghost else "ghost",
            "ghost": ghost,
            "current_task": state.get("current_task") if state else None,
            "budget_spent": state.get("budget_spent", 0.0) if state else 0.0,
            "budget_remaining": (
                (state.get("budget_allocated", 0.0) - state.get("budget_spent", 0.0))
                if state else 0.0
            ),
            "children": [],
        }

        # Build grunt children from can_spawn
        can_spawn = profile.get("can_spawn", [])
        for grunt_name in sorted(can_spawn):
            grunt_state = active_states.get(grunt_name)
            grunt_ghost = _is_ghost(grunt_state)
            grunt_profile = grunt_profiles.get(grunt_name, {})

            grunt_node = {
                "name": grunt_name,
                "role": "grunt",
                "department": grunt_profile.get("department", profile.get("department", "")),
                "status": grunt_state.get("status", "ghost") if grunt_state and not grunt_ghost else "ghost",
                "ghost": grunt_ghost,
                "current_task": grunt_state.get("current_task") if grunt_state else None,
                "children": [],
            }
            node["children"].append(grunt_node)

        head_nodes.append(node)

    return {
        "name": "CEO",
        "role": "ceo",
        "status": "active",
        "children": head_nodes,
    }


# ---------------------------------------------------------------------------
# Agent Detail
# ---------------------------------------------------------------------------

def _find_profile(name: str, registry_dir: str, roles_dir: str) -> Optional[dict]:
    """Find a profile by name in registry/ or roles/."""
    for directory in [registry_dir, roles_dir]:
        path = os.path.join(directory, f"{name}.yaml")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return yaml.safe_load(f)
            except (yaml.YAMLError, OSError):
                pass
    return None


def _find_active_state(name: str, active_dir: str) -> Optional[dict]:
    """Find active state for a named agent."""
    if not os.path.isdir(active_dir):
        return None
    for filename in os.listdir(active_dir):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(active_dir, filename)
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("name") == name:
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


def get_agent_detail(
    name: str,
    registry_dir: str,
    roles_dir: str,
    active_dir: str,
) -> Optional[dict]:
    """Return detailed info for a named agent.

    Merges YAML profile with active runtime state.
    Returns None if agent unknown (no profile and no active state).
    """
    profile = _find_profile(name, registry_dir, roles_dir)
    state = _find_active_state(name, active_dir)

    if profile is None and state is None:
        return None

    result = {}
    if profile:
        result.update({
            "name": profile.get("name"),
            "display_name": profile.get("display_name"),
            "role": profile.get("role"),
            "department": profile.get("department"),
            "model": profile.get("model"),
            "personality": profile.get("personality"),
            "budget_cap": profile.get("budget_cap"),
            "can_spawn": profile.get("can_spawn"),
            "reports_to": profile.get("reports_to"),
        })

    if state:
        result.update({
            "agent_id": state.get("agent_id"),
            "status": state.get("status"),
            "machine": state.get("machine"),
            "pid": state.get("pid"),
            "session_id": state.get("session_id"),
            "current_task": state.get("current_task"),
            "spawned_at": state.get("spawned_at"),
            "last_heartbeat": state.get("last_heartbeat"),
            "budget_allocated": state.get("budget_allocated"),
            "budget_spent": state.get("budget_spent"),
        })

    return result


# ---------------------------------------------------------------------------
# Agent History
# ---------------------------------------------------------------------------

def get_agent_history(name: str, events_path: str) -> list:
    """Return task history for a named agent from events.jsonl.

    Returns list of event dicts sorted by timestamp.
    Returns empty list if file missing or agent has no history.
    """
    if not os.path.exists(events_path):
        return []

    history = []
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if event.get("agent_name") == name:
                        history.append(event)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    history.sort(key=lambda e: e.get("timestamp", ""))
    return history


# ---------------------------------------------------------------------------
# Agent Memory
# ---------------------------------------------------------------------------

def get_agent_memory(name: str, heads_dir: str) -> list:
    """Return list of memory files for a named agent.

    Returns [{filename, modified_at, size_bytes}, ...].
    Returns empty list if agent has no memory directory or files.
    """
    memory_dir = os.path.join(heads_dir, name, "memory")
    if not os.path.isdir(memory_dir):
        return []

    files = []
    for entry in os.scandir(memory_dir):
        if not entry.is_file():
            continue
        stat = entry.stat()
        files.append({
            "filename": entry.name,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "size_bytes": stat.st_size,
        })

    files.sort(key=lambda f: f["filename"])
    return files
