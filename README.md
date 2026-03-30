# Orchestrator v3 -- Corporate Hierarchy for Claude Code

A corporate-hierarchy task orchestration system for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Your main session becomes the **CEO** who delegates work through **Department Heads** down to **Grunts** -- named AI agents with persistent identities, isolated workspaces, and budget tracking.

## Why

Flat task delegation hits a wall: no memory between sessions, no specialization, no budget control, no dependency management. Orchestrator v3 models your AI workforce as a company org chart, where each agent knows its role, reports to a boss, and works in its own sandbox.

## Architecture

```
You --> CEO (depth 0, Opus, main session)
         |-- Atlas (depth 1, Opus, Research Director)
         |     |-- Scout-1 (depth 2, Sonnet)
         |     +-- Scout-2 (depth 2, Sonnet)
         |-- Forge (depth 1, Opus, Development Lead)
         |     |-- Smith-1 (depth 2, Sonnet)
         |     +-- Smith-2 (depth 2, Sonnet)
         |-- Maven (depth 1, Sonnet, HR/Classification)
         +-- Scribe (depth 1, Opus, Content Director)
               |-- Quill-1 (depth 2, Sonnet)
               +-- Quill-2 (depth 2, Haiku)
```

### Communication Flow

```
User: "Research AI chip supply chains and write a post about it"
  -> CEO classifies: research task + writing task
  -> CEO -> Atlas inbox: "Research AI chip supply chains"
  -> CEO -> Scribe inbox: "Write post (blocked by Atlas research)"
  -> Atlas spawns Scout-1 for web research
  -> Scout-1 writes result.json -> Atlas collects and synthesizes
  -> Atlas writes result to outbox -> CEO collects
  -> CEO unblocks Scribe -> Scribe spawns Quill-1 to draft
  -> Quill-1 writes result.json -> Scribe reviews and refines
  -> Scribe writes final to outbox -> CEO delivers to user
```

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/Aragorn2046/orchestrator-v3.git
cd orchestrator-v3

# Copy and edit environment config
cp config.example.env .env
# Edit .env -- at minimum set ORCHESTRATED_ROOT and VAULT_DIR
```

### 2. Install dependencies

```bash
pip install pyyaml  # Only external dependency
```

### 3. Create workspace directories

```bash
mkdir -p ~/projects/_orchestrated/{_heads,_grunts,_done}
```

### 4. Use the CLI

```bash
# Route a task to the right department head
python3 cli.py route "Research the latest AI chip architectures"

# Recruit an agent
python3 cli.py recruit atlas --task "Research AI chip supply chains" --budget 3.0

# Check active agents
python3 cli.py roster

# Delegate a task to a head's inbox
python3 cli.py delegate atlas --task "Compare NVIDIA and AMD server GPUs"

# Collect results from a head's outbox
python3 cli.py collect atlas

# View org chart
python3 cli.py org-chart

# Dry-run (simulate without spawning)
python3 cli.py dry-run "Build a REST API for the dashboard"

# Check budget
python3 cli.py budget
```

### 5. Use the shell script (for spawning sessions)

```bash
# Spawn a specialist Claude Code session
./orchestrator.sh spawn <job-id>

# Check status
./orchestrator.sh status

# Collect results
./orchestrator.sh collect <job-id>

# Dispatch to a remote machine (requires relay)
./orchestrator.sh remote worker-1 <job-id>
```

## Module Overview

| Module | Purpose |
|---|---|
| `cli.py` | Command-line interface -- bridges Python modules to shell |
| `hierarchy.py` | Core lifecycle: recruit, dismiss, roster, route, delegate, collect |
| `registry.py` | Agent registry: YAML profiles, active state management |
| `context_assembler.py` | Builds CLAUDE.md files from agent profiles |
| `communication.py` | File-based inbox/outbox protocol with atomic writes |
| `task_graph.py` | DAG-based task dependencies, parallel waves, failure propagation |
| `maven.py` | HR agent: task classification, grunt template selection |
| `budget_tracker.py` | Per-agent and per-department budget tracking with caps |
| `org_chart.py` | Builds tree visualization of the hierarchy |
| `memory.py` | Per-agent persistent memory (append-only markdown, TTL cleanup) |
| `memory_logging.py` | Structured JSON event logging and performance tracking |
| `workspace_guard.py` | Workspace isolation and access control |
| `dry_run.py` | Full simulation mode -- test routing without spawning |
| `degradation.py` | Graceful degradation when hierarchy components fail |
| `migration.py` | Health checks and migration verification tools |
| `maintenance.py` | Memory TTL cleanup and log rotation (cron-friendly) |
| `relay_tasks.py` | Cross-machine task dispatch via relay (multi-CEO) |
| `relay_handler.py` | Inbound relay message routing |
| `ceo_scheduler.py` | Serialized cross-machine dispatcher (hub only) |
| `orchestrator.sh` | Shell script for spawning and managing Claude Code sessions |

## Agent Profiles

Profiles live in `registry/` (heads) and `roles/` (grunts) as YAML files:

| Profile | Role | Department | Model | Description |
|---|---|---|---|---|
| `atlas.yaml` | Head | Research | Opus | Cross-references sources, structured findings |
| `forge.yaml` | Head | Dev | Sonnet | Pragmatic engineer, tests before declaring done |
| `maven.yaml` | Head | HR | Sonnet | Task classifier and grunt recruiter |
| `scribe.yaml` | Head | Content | Opus | Writes in your voice, follows style guides |
| `scout.yaml` | Grunt | Research | Sonnet | Focused research sub-tasks |
| `smith.yaml` | Grunt | Dev | Sonnet | Heads-down coding, writes tests |
| `quill.yaml` | Grunt | Content | Sonnet | Draft writing per provided brief |

### Customizing Profiles

Edit the YAML files to match your setup. Key fields:

- `context_files`: Paths to files loaded into the agent's context (supports `$VAULT` variable)
- `personality`: Agent behavioral instructions
- `allowed_tools`: Which Claude Code tools the agent can use
- `can_spawn`: Which grunt roles this head can recruit
- `budget_cap`: Maximum USD spend per session

## Configuration

All paths are configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `ORCHESTRATED_ROOT` | `~/projects/_orchestrated` | Root for agent workspaces |
| `VAULT_DIR` | `~/vault` | Directory for context files (`$VAULT` in profiles) |
| `ORCHESTRATOR_SH` | `./orchestrator.sh` | Path to the shell spawner |
| `RELAY_CMD` | `~/projects/relay/relay.py` | Cross-machine relay command |
| `HUB_MACHINE` | `hub` | Name of the hub machine for relay routing |
| `MACHINE` | (hostname) | Override local machine name |
| `MACHINES` | `hub worker-1 worker-2` | Space-separated machine list |

## Multi-Machine Setup

Orchestrator v3 supports distributing work across multiple machines via a hub-and-spoke relay pattern:

1. Designate one machine as the **hub** (always-on server recommended)
2. Set `HUB_MACHINE` and `MACHINE` on each machine
3. Configure `RELAY_CMD` to point to your relay transport
4. Use `orchestrator.sh remote <machine> <job-id>` to dispatch

The `ceo_scheduler.py` runs on the hub and handles:
- Task queuing and serialized dispatch
- CEO liveness tracking via heartbeats
- Resource affinity routing (e.g., GPU tasks to the right machine)

## How It Works

### Workspace Layout

```
~/projects/_orchestrated/
  _heads/
    atlas/
      inbox/          # Tasks from the CEO
      outbox/         # Results back to the CEO
      memory/         # Persistent cross-session memory
      current/        # Active work
      CLAUDE.md       # Agent identity (auto-generated)
    forge/
      ...
  _grunts/
    scout-a1b2c3d4/   # Ephemeral workspace
      result.json     # Output collected by head
      CLAUDE.md
  _done/              # Archived completed workspaces
  events.jsonl        # Structured event log
  task-graph.json     # Persisted dependency graph
```

### Depth Control

- **Depth 0**: CEO (your main session) -- delegates, never executes
- **Depth 1**: Department Heads -- own a domain, can spawn grunts
- **Depth 2**: Grunts -- execute a single task, return results
- **Max depth**: 2 (enforced via `ORCHESTRATOR_DEPTH` env var)

### Budget Tracking

Every agent has a budget cap. The budget tracker:
- Allocates budget on recruit, releases on failure
- Tracks per-agent and per-department spend
- Prevents spawning when budget is exhausted
- Persists state across sessions

## Requirements

- Python 3.9+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI (`claude` on PATH)
- PyYAML (`pip install pyyaml`)
- For multi-machine: a relay transport (not included)

## License

MIT
