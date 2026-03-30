"""Context assembler: builds CLAUDE.md files from agent profiles.

Generates identity-aware CLAUDE.md files from YAML agent profiles.
Supports two modes:
- Profile mode (v3): builds from registry YAML profiles
- Template mode (v2): preserved for backward compatibility (not in this module)
"""

import os
import subprocess
from typing import Optional

REQUIRED_PROFILE_FIELDS = ["name", "role", "department", "reports_to"]


def _resolve_path(path: str) -> str:
    """Resolve $VAULT and $HOME in a path."""
    if "$HOME" in path:
        path = path.replace("$HOME", os.path.expanduser("~"))
    if "$VAULT" not in path:
        return path
    vault = os.environ.get("VAULT", "")
    if not vault:
        try:
            result = subprocess.run(
                ["bash", "-c", "echo $VAULT"],
                capture_output=True, text=True, timeout=5,
            )
            vault = result.stdout.strip()
        except Exception:
            pass
    if vault:
        path = path.replace("$VAULT", vault)
    return path


class ContextAssembler:
    """Assembles CLAUDE.md documents from agent profiles."""

    def assemble(
        self,
        profile: dict,
        agent_id: str,
        task: str,
        budget: float,
        workspace: str,
    ) -> str:
        """Assemble a CLAUDE.md from a registry profile.

        Returns the assembled markdown string with sections in order:
        1. Identity, 2. Personality, 3. Delegation, 4. Context Files,
        5. Result Contract, 6. Budget (BATS), 7. Communication, 8. Session Resume (heads)
        """
        self._validate_profile(profile)
        if budget < 0:
            budget = 0.0

        sections = []

        # 1. Identity Block
        sections.append(self._build_identity(profile, agent_id))

        # 2. Personality
        sections.append(self._build_personality(profile))

        # 3. Delegation Rules
        sections.append(self._build_delegation(profile))

        # 4. Context Files
        ctx = self._build_context_files(profile)
        if ctx:
            sections.append(ctx)

        # 5. Result Contract
        sections.append(self._build_result_contract(profile))

        # 6. Budget (BATS)
        sections.append(self._build_budget(budget))

        # 7. Communication Instructions
        sections.append(self._build_communication(profile, workspace))

        # 8. Session Resume (heads only)
        if profile.get("role") == "head":
            sections.append(self._build_session_resume(workspace))

        # 9. Current Task
        sections.append(self._build_task(task))

        return "\n\n".join(s for s in sections if s)

    def create_workspace(
        self,
        profile: dict,
        agent_id: str,
        task: str,
        budget: float,
        workspace: str,
    ) -> str:
        """Create a full agent workspace with CLAUDE.md and directory structure.

        For heads: creates inbox/, outbox/, memory/, current/ (idempotent).
        For grunts: flat workspace.
        Returns the workspace path.
        """
        os.makedirs(workspace, exist_ok=True)

        # Head workspaces get persistent subdirs
        if profile.get("role") == "head":
            for subdir in ("inbox", "outbox", "memory", "current"):
                os.makedirs(os.path.join(workspace, subdir), exist_ok=True)

        # Assemble and write CLAUDE.md (always regenerated)
        md = self.assemble(
            profile=profile,
            agent_id=agent_id,
            task=task,
            budget=budget,
            workspace=workspace,
        )
        claude_path = os.path.join(workspace, "CLAUDE.md")
        tmp_path = claude_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(md)
        os.replace(tmp_path, claude_path)

        # Write prompt.md with task description
        prompt_path = os.path.join(workspace, "prompt.md")
        tmp_path = prompt_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(f"# Task\n\n{task}\n")
        os.replace(tmp_path, prompt_path)

        return workspace

    @staticmethod
    def _validate_profile(profile: dict) -> None:
        """Validate that required profile fields are present."""
        missing = [f for f in REQUIRED_PROFILE_FIELDS if f not in profile]
        if missing:
            raise ValueError(
                f"Profile missing required fields: {', '.join(missing)}"
            )

    def _build_identity(self, profile: dict, agent_id: str) -> str:
        display = profile.get("display_name", profile["name"])
        lines = [
            f"# Agent: {display}",
            "",
            f"- **Name**: {profile['name']}",
            f"- **Role**: {profile['role']}",
            f"- **Department**: {profile['department']}",
            f"- **Reports to**: {profile['reports_to']}",
            f"- **Agent ID**: {agent_id}",
        ]
        return "\n".join(lines)

    def _build_personality(self, profile: dict) -> str:
        personality = profile.get("personality", "")
        if not personality:
            return "## Personality\n\nNo personality defined."
        return f"## Personality\n\n{personality}"

    def _build_delegation(self, profile: dict) -> str:
        role = profile.get("role", "grunt")
        can_spawn = profile.get("can_spawn", [])

        if role == "head" and can_spawn:
            spawn_list = ", ".join(can_spawn)
            return (
                f"## Delegation\n\n"
                f"You can spawn the following grunt roles: {spawn_list}.\n"
                f"Delegate sub-tasks when they are self-contained and "
                f"would benefit from parallel execution. Handle directly "
                f"when the task requires your full context or judgment."
            )
        elif role == "head":
            return (
                "## Delegation\n\n"
                "You are a head agent but have no grunt roles to spawn. "
                "Handle all tasks directly."
            )
        else:
            return (
                "## Delegation\n\n"
                "You are a grunt agent. You cannot spawn other agents. "
                "Focus on your assigned task and return results to your head."
            )

    def _build_context_files(self, profile: dict) -> Optional[str]:
        context_files = profile.get("context_files", [])
        if not context_files:
            return None

        parts = ["## Context"]
        for path in context_files:
            path = _resolve_path(path)
            if not os.path.isfile(path):
                parts.append(f"\n### {os.path.basename(path)}\n\n*File not found: {path}*")
                continue
            try:
                with open(path) as f:
                    content = f.read()
                parts.append(f"\n### {os.path.basename(path)}\n\n{content}")
            except OSError:
                parts.append(f"\n### {os.path.basename(path)}\n\n*Could not read: {path}*")

        return "\n".join(parts)

    def _build_result_contract(self, profile: dict) -> str:
        contract = profile.get("result_contract", "Write results as structured JSON.")
        return f"## Result Contract\n\n{contract}"

    def _build_budget(self, budget: float) -> str:
        return (
            f"## Budget\n\n"
            f"**Budget**: You have ${budget:.2f} remaining for this task. "
            f"Minimize unnecessary tool calls."
        )

    def _build_communication(self, profile: dict, workspace: str) -> str:
        role = profile.get("role", "grunt")

        if role == "head":
            outbox = os.path.join(workspace, "outbox/")
            inbox = os.path.join(workspace, "inbox/")
            return (
                f"## Communication\n\n"
                f"- **Outbox**: `{outbox}` -- write result and progress files here\n"
                f"- **Inbox**: `{inbox}` -- check here for new tasks\n"
                f"- **Atomic write protocol**: write to `.tmp-` prefixed file, "
                f"then `os.replace()` to final name, then write `.ready` sentinel\n"
                f"- **Heartbeat**: handled by wrapper process (do not write heartbeat files)\n"
                f"- **Progress updates**: write `progress-<task_id>.json` to outbox/ for mid-task status"
            )
        else:
            return (
                "## Communication\n\n"
                "Write your result to `result.json` in your workspace root. "
                "Your head agent will collect it."
            )

    def _build_session_resume(self, workspace: str) -> str:
        return (
            "## Session Resume Protocol\n\n"
            "On startup, perform these steps:\n\n"
            f"1. Scan `inbox/` for unprocessed task files "
            f"(tasks without a matching result in `outbox/`)\n"
            f"2. Check `outbox/` for stale results from a previous session\n"
            f"3. Read `memory/` index to prime context with relevant past findings\n"
            f"4. Clean stale progress files from `outbox/` (from previous incarnation)"
        )

    def _build_task(self, task: str) -> str:
        return f"## Current Task\n\n{task}"
