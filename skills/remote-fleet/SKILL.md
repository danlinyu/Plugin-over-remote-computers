---
name: remote-fleet
description: Browse folders, pick or create a project directory, edit files, access remote datasets, and run heavy calculations on any computer across your Home LAN or Tailscale (Mac Mini, PC, DGX Sparks) while keeping your local interface machine fast and idle.
---

# Remote Fleet (Mac Control Terminal -> Remote Files, Data & Compute)

`remote-fleet` lets the user run any LLM harness (**Antigravity**, **Claude**, **Codex**, **DSH**, **Kimi Code**) smoothly on their local interface computer (e.g., **Mac Mini**), while **picking a folder, keeping all project files/changes/datasets, and running heavy calculations** directly on **PC** or **DGX Sparks** (e.g., `spark01`, `spark03`, `spark04`, or the Mac Mini itself) over **Home LAN or Tailscale**.

## Golden Rules

1. **When starting a new project (or inside `~/Fleet/Start-Here`)**:
   - Call `fleet_list_machines(probe=true, include_load=true)` to show online machines, LAN/Tailscale latency, and live CPU/GPU load so the user can pick a responsive or idle machine.
   - Browse folders with `fleet_browse_folders(machine=...)`, then bind the target directory using `fleet_select_project` or `fleet_create_project`.
2. **Keep files & edits on the target machine — NOT stranded on the local interface machine**:
   - Once a remote folder is selected, perform **all** file reads, writes, edits, and searches via `fleet_read_file`, `fleet_write_file`, `fleet_edit_file`, `fleet_list_files`, and `fleet_search_files`.
   - You can also inspect any other local data folder on that remote machine (e.g. `/data/...`, `/mnt/nvme/...`, `D:\Datasets\...`) by passing its path to `fleet_list_files`, `fleet_read_file`, or `fleet_run_command` — without copying gigabytes of data to the Mac!
3. **Keep heavy calculations off the Mac**:
   - Use `fleet_run_command` for quick commands (`git`, `pytest`, `nvidia-smi`, environment setup) on the target computer.
   - Use `fleet_start_job`, `fleet_job_status`, and `fleet_stop_job` for heavy or long-running training/calculation jobs on the PC or Sparks so the job runs detached in the background and never slows down or times out the harness on the Mac.

## CLI Fallback (`fleet`)

When MCP tools are unavailable, run `<path/to/this/skill>/scripts/fleet` (or `fleet` on `PATH`):

```bash
fleet status --load                                       # Check LAN/Tailscale latency + CPU/GPU load
fleet browse spark01 ~/projects                           # Browse folders on spark01
fleet use spark03 ~/projects/new-app --create --init-git  # Create & select remote folder
fleet ls                                                  # List files in active remote project
fleet read src/main.py                                    # Read remote file
fleet edit src/main.py --old "lr = 1e-3" --new "lr = 3e-4"# Edit remote file directly on target host
fleet exec -- nvidia-smi                                  # Run quick command on target host
fleet jobs start -- python train.py --epochs 100          # Start detached heavy calculation on target host
fleet jobs status                                         # Check background jobs & logs
```
