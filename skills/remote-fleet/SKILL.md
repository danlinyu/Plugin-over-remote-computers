---
name: remote-fleet
description: Browse folders, pick or create a project directory, edit files, and run commands on any computer across your Home LAN or Tailscale (Mac Mini, PC, DGX Spark 1, Spark 2, Spark 3). Use whenever the user wants to start or open a project on another machine, list online machines, browse remote directories, edit remote files, or execute commands/GPU workloads on a remote computer without going through GitHub.
---

# Remote Fleet (Home LAN & Tailscale)

`remote-fleet` enables any LLM harness (**Antigravity**, **Claude**, **Codex**, **DSH**, **Kimi Code**, etc.) to access all computers across your Home LAN or Tailscale, browse/pick or create a project folder on any computer, and run file operations + commands directly on that target host.

## Preferred Interface: MCP Tools (`remote-fleet`)

If the `remote-fleet` MCP tools are registered in this harness session, prefer calling them directly:

1. **Discover & check computers**: `fleet_list_machines(probe=true)`
2. **Browse folders on a target computer**: `fleet_browse_folders(machine="spark-1", path="~/projects")`
3. **Select an existing folder**: `fleet_select_project(machine="spark-1", path="~/projects/my-app")`
4. **Create a brand-new project folder**: `fleet_create_project(machine="spark-2", path="~/projects/new-app", init_git=true, template="python")`
5. **Read / Write / Edit / Search files**:
   - `fleet_list_files()`
   - `fleet_read_file(path="src/main.py")`
   - `fleet_write_file(path="src/new_file.py", content="...")`
   - `fleet_edit_file(path="src/main.py", old_text="...", new_text="...")`
   - `fleet_search_files(query="def train")`
6. **Execute commands / tests / GPU jobs directly on that computer**:
   - `fleet_run_command(command="nvidia-smi")`
   - `fleet_run_command(command="pytest -q")`
7. **Sync local <-> remote mirror if needed**:
   - `fleet_sync_project(direction="pull")` or `fleet_sync_project(direction="push")`

## CLI Interface (`fleet`)

When MCP tools are not active or when running shell commands directly, invoke `<path/to/this/skill>/scripts/fleet` (or `<path\to\this\skill>\scripts\fleet.cmd` on Windows, or `fleet` if `~/.local/bin` is on `PATH`):

```bash
# 1. Check all computers (Mac Mini, PC, Spark 1-3 + Tailscale peers)
<path/to/this/skill>/scripts/fleet status

# 2. Browse folders on a machine
<path/to/this/skill>/scripts/fleet browse spark-1 ~/projects

# 3. Select (or create) a folder as the active project
<path/to/this/skill>/scripts/fleet use spark-1 ~/projects/my-app
<path/to/this/skill>/scripts/fleet use spark-2 ~/projects/new-model --create --init-git --template python

# 4. Inspect & edit files on the active remote project
<path/to/this/skill>/scripts/fleet ls
<path/to/this/skill>/scripts/fleet read README.md
<path/to/this/skill>/scripts/fleet edit src/app.py --old "DEBUG = False" --new "DEBUG = True"
<path/to/this/skill>/scripts/fleet search "TODO"

# 5. Run builds, tests, git, or GPU jobs directly on the target computer
<path/to/this/skill>/scripts/fleet exec -- pytest -q
<path/to/this/skill>/scripts/fleet exec -- nvidia-smi
```

## Workflow When User Starts a New Project

1. Run `fleet_list_machines` (or `fleet status --json`) to see which computers (`mac-mini`, `pc`, `spark-1`, `spark-2`, `spark-3`, etc.) are online via `[LOCAL]`, `[LAN]`, or `[TAILSCALE]`.
2. If the user named a computer (e.g. *"start a project on Spark 2"*), browse its default root with `fleet_browse_folders(machine="spark-2")` and ask which subfolder to open or what new folder name to create. If they haven't picked a computer yet, show the online computers and ask which one they want.
3. Call `fleet_select_project` or `fleet_create_project`. From that point forward, execute all project file reads, edits, searches, and terminal commands against that computer!
