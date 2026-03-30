# Orchestrator v3 — Corporate Hierarchy for Claude Code

A task orchestration system that turns your Claude Code session into a CEO that automatically delegates work through a corporate hierarchy of AI agents.

## How It Works

```
You (User)
  └── CEO (Main Claude Code Session)
        ├── Atlas — Research Director
        │     └── Scout (grunt)
        ├── Forge — Engineering Director
        │     └── Smith (grunt)
        ├── Scribe — Content Director
        │     └── Quill (grunt)
        └── Maven — HR / Task Classification
```

Your main Claude Code session becomes the **CEO**. When you give it a complex task, it:
1. **Assesses** complexity — simple tasks are handled directly
2. **Routes** to the right department head (Atlas for research, Forge for code, etc.)
3. **Delegates** through the hierarchy — heads recruit specialists (grunts)
4. **Monitors** progress and handles failures
5. **Synthesizes** results back to you

## Features

- **Auto-CEO mode** — every main session is automatically a CEO (opt-out: `export ORCHESTRATE_MODE=0`)
- **Named agents** with persistent identities and YAML profiles
- **Budget tracking** — hierarchical caps (session → department → agent → task)
- **Task graph** — DAG-based dependency management for multi-step tasks
- **Workspace isolation** — each specialist gets a confined workspace
- **Graceful degradation** — falls back cleanly when components aren't available
- **Dry-run mode** — test the full pipeline without spawning real sessions

## Quick Start

### Prerequisites
- [Claude Code](https://claude.ai/code) CLI installed
- Python 3.10+
- A Claude API subscription (Opus recommended for CEO, Sonnet for specialists)

### Installation

```bash
git clone https://github.com/Aragorn2046/orchestrator-v3.git
cd orchestrator-v3
bash auto-orchestration/setup.sh
```

This will:
1. Install the CEO rules file to `~/.claude/rules/`
2. Configure the SessionStart hook in `~/.claude/settings.json`
3. Create the project directory structure

### Verify Installation

```bash
python3 -m pytest tests/ -v
```

### Start Using It

Start a new Claude Code session. The CEO protocol activates automatically. Try:

- "Research WebSocket libraries and build a prototype" — delegates to Atlas then Forge
- "Write a blog post about [topic]" — delegates to Scribe
- "Fix the bug in server.py AND write tests" — delegates to Forge

Simple tasks ("fix the typo in config.json") are handled directly — the CEO is smart about when to delegate.

## Configuration

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `ORCHESTRATE_MODE` | `1` | Set to `0` to disable CEO mode |
| `CLAUDE_SESSION_TYPE` | `main` | Session type (main/spinoff/cron/specialist) |
| `ORCHESTRATOR_AGENT_ID` | (unset) | Set automatically for worker sessions |
| `VAULT` | `$HOME/vault` | Path to your knowledge vault (if using one) |

### Customizing Agents

Edit the YAML files in `registry/` (heads) and `roles/` (grunts):

```yaml
# registry/atlas.yaml
name: atlas
display_name: "Atlas — Research Director"
role: head
department: research
model: opus           # or sonnet for budget savings
budget_cap: 5.0
context_files:
  - "$VAULT/your-research-context.md"  # customize these
```

### Customizing Templates

Templates in `templates/` control what instructions specialists receive. Edit them to match your workflow:

- `base.yaml` — injected into ALL specialists
- `code.yaml` — software development tasks
- `research.yaml` — research and analysis
- `writing-en.yaml` — English writing (customize voice/style references)

### Adding Your Own Department Head

1. Create `registry/your-head.yaml` following the atlas.yaml pattern
2. Add routing keywords in `hierarchy.py` ROUTING_RULES
3. Create a grunt role in `roles/your-grunt.yaml`

## Architecture

### Core Modules

| Module | Purpose |
|---|---|
| `hierarchy.py` | Recruit, dismiss, roster, route tasks |
| `communication.py` | File-based IPC with atomic writes |
| `task_graph.py` | DAG dependency management |
| `maven.py` | Task classification and agent recruitment |
| `budget_tracker.py` | Hierarchical budget tracking |
| `context_assembler.py` | Identity-aware CLAUDE.md generation |
| `workspace_guard.py` | Path confinement and workspace isolation |
| `migration.py` | System integration + CEO protocol validation |
| `degradation.py` | Graceful fallbacks when components unavailable |
| `dry_run.py` | Mock pipeline for testing |
| `orchestrator.sh` | Bash entry point for spawning/monitoring |
| `cli.py` | CLI for roster, budget, routing commands |

### Auto-Orchestration (Three-Layer Architecture)

1. **SessionStart Hook** — injects `CEO_SESSION_INFO` into every session
2. **Rules File** (`~/.claude/rules/orchestrator-ceo.md`) — behavioral contract for the CEO
3. **`is_orchestrate_mode()`** — runtime check with 3-tier priority:
   - `ORCHESTRATE_MODE=0` → CEO off (explicit opt-out)
   - `ORCHESTRATOR_AGENT_ID` set → CEO off (worker session)
   - `CLAUDE_SESSION_TYPE=main` or unset → CEO on

### Session Type Matrix

| Session Type | ORCHESTRATE_MODE | AGENT_ID | CEO Active? |
|---|---|---|---|
| main | (unset/1) | (unset) | YES |
| main | 0 | (unset) | NO (opted out) |
| spinoff | (any) | (unset) | NO |
| specialist | (any) | atlas-001 | NO |
| cron | (any) | (unset) | NO |

## Testing

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run specific module tests
python3 -m pytest tests/test_migration.py -v    # auto-orchestration
python3 -m pytest tests/test_hierarchy.py -v     # core hierarchy
python3 -m pytest tests/test_budget_tracker.py -v
```

## Opt-Out / Disable

```bash
# Disable for one session
export ORCHESTRATE_MODE=0

# Disable permanently — remove the rules file
rm ~/.claude/rules/orchestrator-ceo.md

# Full uninstall — also remove the hook from settings.json
# Edit ~/.claude/settings.json and remove the CEO_SESSION_INFO hook entry
```

## License

MIT
