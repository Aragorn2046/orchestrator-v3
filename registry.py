"""Agent registry: profile loading, runtime state CRUD, and ID generation."""

import json
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import List, Optional

import yaml

REQUIRED_PROFILE_FIELDS = [
    "name",
    "display_name",
    "role",
    "department",
    "model",
    "budget_cap",
    "personality",
    "context_files",
    "allowed_tools",
    "can_spawn",
    "reports_to",
    "result_contract",
]

AGENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*-[0-9a-f]{8}$")


@dataclass
class AgentState:
    agent_id: str
    name: str
    role: str
    machine: str
    pid: int
    session_id: str
    status: str
    current_task: Optional[str]
    spawned_at: str
    last_heartbeat: str
    budget_allocated: float
    budget_spent: float


def generate_agent_id(name: str) -> str:
    """Generate an agent ID in the format <name>-<8-char-hex>."""
    return f"{name}-{uuid.uuid4().hex[:8]}"


def _resolve_vault(path: str) -> str:
    """Replace $VAULT in a path with the actual vault directory."""
    if "$VAULT" not in path:
        return path
    vault = os.environ.get("VAULT_DIR", os.environ.get("VAULT", ""))
    if not vault:
        vault = os.path.expanduser("~/vault")
    if not vault:
        raise RuntimeError(
            "Cannot resolve $VAULT: set VAULT_DIR environment variable."
        )
    return path.replace("$VAULT", vault)


class Registry:
    """Agent registry: manages profiles and runtime state."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.registry_dir = os.path.join(base_dir, "registry")
        self.roles_dir = os.path.join(base_dir, "roles")
        self.active_dir = os.path.join(self.registry_dir, "active")
        os.makedirs(self.active_dir, exist_ok=True)

    def load_profile(self, agent_name: str) -> dict:
        """Load a YAML profile by name. Searches registry/ then roles/."""
        path = os.path.join(self.registry_dir, f"{agent_name}.yaml")
        if not os.path.exists(path):
            path = os.path.join(self.roles_dir, f"{agent_name}.yaml")
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No profile found for '{agent_name}' in registry/ or roles/"
            )

        with open(path) as f:
            profile = yaml.safe_load(f)

        missing = [f for f in REQUIRED_PROFILE_FIELDS if f not in profile]
        if missing:
            raise ValueError(
                f"Profile '{agent_name}' missing required fields: {', '.join(missing)}"
            )

        # Resolve $VAULT in context_files
        profile["context_files"] = [
            _resolve_vault(p) for p in profile["context_files"]
        ]

        return profile

    def create_active_state(
        self,
        agent_id: str,
        profile: dict,
        pid: int,
        session_id: str,
        machine: str,
        task_id: str,
        budget: float,
    ) -> AgentState:
        """Create an active state file for a spawned agent."""
        if not AGENT_ID_PATTERN.match(agent_id):
            raise ValueError(
                f"Invalid agent_id format: '{agent_id}'. "
                f"Expected <name>-<8hexchars>."
            )

        now = datetime.now(timezone.utc).isoformat()
        state = AgentState(
            agent_id=agent_id,
            name=profile["name"],
            role=profile["role"],
            machine=machine,
            pid=pid,
            session_id=session_id,
            status="active",
            current_task=task_id,
            spawned_at=now,
            last_heartbeat=now,
            budget_allocated=budget,
            budget_spent=0.0,
        )

        self._write_state(state)
        return state

    def read_active_state(self, agent_id: str) -> Optional[AgentState]:
        """Read an active state file. Returns None if not found or corrupted."""
        path = os.path.join(self.active_dir, f"{agent_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            return AgentState(**data)
        except (json.JSONDecodeError, TypeError, KeyError):
            return None

    def update_active_state(self, agent_id: str, **kwargs) -> AgentState:
        """Update fields on an active state and write back."""
        state = self.read_active_state(agent_id)
        if state is None:
            raise FileNotFoundError(f"No active state for '{agent_id}'")
        for key, value in kwargs.items():
            if not hasattr(state, key):
                raise ValueError(f"AgentState has no field '{key}'")
            setattr(state, key, value)
        self._write_state(state)
        return state

    def remove_active_state(self, agent_id: str) -> bool:
        """Remove an active state file. Returns True if removed."""
        path = os.path.join(self.active_dir, f"{agent_id}.json")
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False

    def list_active_states(self) -> List[AgentState]:
        """List all active agent states, sorted by name."""
        states = []
        if not os.path.exists(self.active_dir):
            return states
        for filename in os.listdir(self.active_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self.active_dir, filename)
            try:
                with open(path) as f:
                    data = json.load(f)
                states.append(AgentState(**data))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
        states.sort(key=lambda s: s.name)
        return states

    def _write_state(self, state: AgentState) -> None:
        """Atomic write of state to JSON file."""
        path = os.path.join(self.active_dir, f"{state.agent_id}.json")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(asdict(state), f, indent=2)
        os.rename(tmp_path, path)
