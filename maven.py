"""Maven HR Agent: classification and recruitment for the orchestrator hierarchy.

Maven classifies tasks to departments/heads and recruits new grunt specialists
when no suitable agent exists. Spawned on-demand as a Sonnet-tier head agent.

Fast-path regex classification lives in hierarchy.py (route_task()).
This module provides the deeper classification logic and recruitment flow.
"""

import logging
import os
import re
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Default paths (relative to this module)
DEFAULT_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "registry")
DEFAULT_ROLES_PATH = os.path.join(os.path.dirname(__file__), "roles")

# Department-to-head mapping
DEPARTMENT_HEADS = {
    "research": "atlas",
    "dev": "forge",
    "content": "scribe",
    "hr": "maven",
}

# Fast-path regex rules (mirrors hierarchy.py ROUTING_RULES for standalone use)
FAST_PATH_RULES = [
    (re.compile(r"\b(research|investigate|analyze|study|explore|find\s+out|deep\s+dive|compare)\b", re.IGNORECASE), "atlas", "research"),
    (re.compile(r"\b(write|draft|blog|article|post|newsletter|document|summarize|content)\b", re.IGNORECASE), "scribe", "content"),
    (re.compile(r"\b(build|code|implement|fix|debug|develop|deploy|script|refactor|test)\b", re.IGNORECASE), "forge", "dev"),
]

# Template selection rules: department -> default template
DEPARTMENT_TEMPLATES = {
    "research": "scout.yaml",
    "dev": "smith.yaml",
    "content": "quill.yaml",
}

# Model recommendation based on task complexity heuristics
MODEL_RULES = [
    (re.compile(r"\b(deep|complex|architecture|design|strategy)\b", re.IGNORECASE), "opus"),
    (re.compile(r"\b(simple|quick|routine|basic|straightforward)\b", re.IGNORECASE), "haiku"),
]
DEFAULT_MODEL = "sonnet"

# Budget defaults by department
DEPARTMENT_BUDGETS = {
    "research": 1.0,
    "dev": 2.0,
    "content": 1.0,
}
DEFAULT_BUDGET = 1.0


def fast_path_classify(task_description: str) -> Optional[Dict]:
    """Regex-based fast-path classification (no LLM needed).

    Returns a classification dict if a clear match is found, None if ambiguous.
    Ambiguous = multiple department matches or no matches.
    """
    matches = []
    for pattern, head, department in FAST_PATH_RULES:
        if pattern.search(task_description):
            matches.append((head, department))

    if len(matches) == 1:
        head, department = matches[0]
        model = _recommend_model(task_description)
        budget = DEPARTMENT_BUDGETS.get(department, DEFAULT_BUDGET)
        return {
            "head": head,
            "department": department,
            "model": model,
            "budget": budget,
            "reasoning": f"Fast-path regex match for {department}",
            "needs_recruitment": False,
        }

    # Ambiguous (multiple matches or no match)
    return None


def classify_task(task_description: str, registry_profiles: List[Dict]) -> Dict:
    """Classify a task and return routing recommendation.

    This is the full classification function used when Maven is spawned as an agent.
    For unit testing without an LLM, this uses the fast-path as the core logic
    and enriches it with registry awareness.

    Returns a dict with:
        head: str           - Target head name (atlas/forge/scribe)
        department: str     - Department name
        model: str          - Recommended model tier
        budget: float       - Recommended budget
        reasoning: str      - Why this classification was chosen
        needs_recruitment: bool - Whether a new grunt is needed
    """
    # Try fast-path first
    result = fast_path_classify(task_description)
    if result is not None:
        # Check if an existing specialist in the registry could handle it
        result["needs_recruitment"] = not _has_matching_specialist(
            task_description, result["department"], registry_profiles
        )
        return result

    # Fallback: analyze keywords more broadly
    # Count keyword hits per department
    scores = {"research": 0, "dev": 0, "content": 0}
    for pattern, _head, department in FAST_PATH_RULES:
        hits = len(pattern.findall(task_description))
        scores[department] += hits

    best_dept = max(scores, key=scores.get)
    if scores[best_dept] > 0:
        head = DEPARTMENT_HEADS[best_dept]
        model = _recommend_model(task_description)
        budget = DEPARTMENT_BUDGETS.get(best_dept, DEFAULT_BUDGET)
        return {
            "head": head,
            "department": best_dept,
            "model": model,
            "budget": budget,
            "reasoning": f"Keyword scoring: {best_dept} ({scores[best_dept]} hits)",
            "needs_recruitment": not _has_matching_specialist(
                task_description, best_dept, registry_profiles
            ),
        }

    # No keywords matched at all — default to research (safest fallback)
    return {
        "head": "atlas",
        "department": "research",
        "model": DEFAULT_MODEL,
        "budget": DEFAULT_BUDGET,
        "reasoning": "No keyword matches, defaulting to research",
        "needs_recruitment": True,
    }


def select_grunt_template(task_description: str, department: str) -> str:
    """Select the best grunt role template for a sub-task.

    Matches task characteristics to available templates:
        scout.yaml - Research/investigation sub-tasks
        smith.yaml - Code/build sub-tasks
        quill.yaml - Writing/content sub-tasks

    Returns the template filename.
    """
    return DEPARTMENT_TEMPLATES.get(department, "scout.yaml")


def customize_profile(
    template_path: str,
    task_description: str,
    overrides: Optional[Dict] = None,
) -> Dict:
    """Customize a grunt template with task-specific additions.

    Takes a base template and applies:
        - Task-specific personality additions
        - Context file injections
        - Tool restriction adjustments
        - Budget cap from task allocation

    Returns the customized profile as a dict (ready to write as YAML).
    """
    if overrides is None:
        overrides = {}

    with open(template_path) as f:
        profile = yaml.safe_load(f)

    # Apply task-specific personality addition
    base_personality = profile.get("personality", "")
    task_context = f" Current task: {task_description}"
    profile["personality"] = base_personality + task_context

    # Apply overrides
    for key, value in overrides.items():
        if key == "context_files" and key in profile:
            # Merge context files rather than replace
            existing = profile.get("context_files", [])
            profile["context_files"] = existing + value
        elif key == "allowed_tools" and key in profile:
            # Merge tools
            existing = set(profile.get("allowed_tools", []))
            existing.update(value)
            profile["allowed_tools"] = sorted(existing)
        else:
            profile[key] = value

    return profile


def load_inventory(registry_path: str = DEFAULT_REGISTRY_PATH) -> List[Dict]:
    """Read all YAML profiles from the registry directory.

    Called at Maven startup to build awareness of available specialists.
    Returns list of parsed profile dicts.
    """
    profiles = []
    if not os.path.isdir(registry_path):
        logger.error("Registry directory not found: %s", registry_path)
        return profiles

    for entry in os.scandir(registry_path):
        if not entry.name.endswith(".yaml"):
            continue
        if entry.name == "active":
            continue
        try:
            with open(entry.path) as f:
                profile = yaml.safe_load(f)
            if profile:
                profile["_source_path"] = entry.path
                profiles.append(profile)
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Failed to load profile %s: %s", entry.name, e)

    return profiles


def load_roles_inventory(roles_path: str = DEFAULT_ROLES_PATH) -> List[Dict]:
    """Read all YAML role templates from the roles directory.

    Returns list of parsed role template dicts.
    """
    templates = []
    if not os.path.isdir(roles_path):
        logger.error("Roles directory not found: %s", roles_path)
        return templates

    for entry in os.scandir(roles_path):
        if not entry.name.endswith(".yaml"):
            continue
        try:
            with open(entry.path) as f:
                template = yaml.safe_load(f)
            if template:
                template["_source_path"] = entry.path
                templates.append(template)
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Failed to load role template %s: %s", entry.name, e)

    return templates


def _recommend_model(task_description: str) -> str:
    """Recommend a model tier based on task complexity heuristics."""
    for pattern, model in MODEL_RULES:
        if pattern.search(task_description):
            return model
    return DEFAULT_MODEL


def _has_matching_specialist(
    task_description: str,
    department: str,
    profiles: List[Dict],
) -> bool:
    """Check if any existing profile in the registry matches the task's department."""
    for profile in profiles:
        if profile.get("department") == department and profile.get("role") == "grunt":
            return True
    return False
