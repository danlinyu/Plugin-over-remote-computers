# Plugin-over-remote-computers (`remote-fleet`)

Use your **Mac Mini** (or any computer) as a fast, lag-free **control interface** for **Antigravity**, **Claude**, **Codex**, **DSH**, **Kimi Code**, and Cursor—while **picking folders, keeping all project files & local datasets, and running heavy calculations** directly on your **PC** and **3 DGX Sparks** over **Home LAN or Tailscale**, with zero GitHub round-trips required.

---

## How the Architecture Works

```text
┌──────────────────────────────────────────────────────────┐
│  Mac Mini (Lightweight Interface — 0% Heavy Compute)     │
│  Runs: Antigravity / Claude / Codex / DSH / Kimi Code    │
│  ├── Native GUI Picker (`fleet gui` / `.command`)        │
│  ├── Universal Launchpad (`~/Fleet/Start-Here`)          │
│  └── `remote-fleet-mcp` + Global Agent Rules             │
└───────────────┬──────────────────────────┬───────────────┘
     Home LAN   │ (auto-preferred, ~1ms)   │ Tailscale fallback
        ┌───────┴────────┐         ┌───────┴────────┐
        ▼                ▼         ▼                ▼
   [DGX Spark 1]   [DGX Spark 2] [DGX Spark 3]  [Home PC]
   Files stay here  Files stay    Files stay     Files stay
   Local /data/...  here + GPUs   here + GPUs    here + CPU/GPU
   Heavy jobs run   detached      detached       detached
```

1. **Zero Heavy Compute on Your Mac**: The harness UI and LLM conversation run smoothly on your Mac Mini, unaffected even when your PC and 3 Sparks are at 100% CPU/GPU load.
2. **Files & Datasets Stay on the Target Machine**: Code edits (`fleet_write_file`, `fleet_edit_file`) write directly to the chosen folder on `spark-1..3` or `pc`, and scripts/queries read local datasets (`/data/...`, `D:\...`) in place without copying gigabytes of data onto your Mac.
3. **Detached Background Jobs (`fleet_start_job`)**: Heavy calculations launch detached on the PC or Sparks so SSH returns in milliseconds and never freezes or times out your harness session.

---

## 1. One-Time Enablement on Your Mac Mini

```bash
git clone https://github.com/danlinyu/Plugin-over-remote-computers.git
cd Plugin-over-remote-computers
./install.sh

# Auto-populate live Tailscale hostnames/IPs & verify passwordless SSH to PC + 3 Sparks:
fleet discover --save
fleet doctor
```

> **Tip**: Make sure `~/.local/bin` is on your `PATH`. For one-time passwordless SSH to your Sparks and PC, either enable Tailscale SSH (`sudo tailscale up --ssh`) or run `ssh-copy-id user@spark-1.local`.

### What `./install.sh` Enables Automatically Across Every Harness

| Harness | MCP Config Installed | Global Startup Rule Installed | Skill / Command Installed |
| :--- | :--- | :--- | :--- |
| **Antigravity** | `~/.gemini/antigravity/mcp_config.json` | `~/.gemini/GEMINI.md` | `~/.gemini/antigravity/skills/remote-fleet/` |
| **Claude Code / Desktop** | `~/.claude.json` (+ Claude Desktop config) | `~/.claude/CLAUDE.md` | `/pick-remote`, `/fleet-status`, `~/.claude/skills/remote-fleet/` |
| **Codex** | `~/.codex/config.toml` (`[mcp_servers.remote_fleet]`) | `~/.codex/AGENTS.md` | `~/.codex/skills/remote-fleet/`, `~/.agents/skills/remote-fleet/` |
| **Kimi Code** | `~/.kimi/mcp.json` | `~/.kimi/AGENTS.md` | `~/.kimi/skills/remote-fleet/` |
| **DSH** | Workspace `.mcp.json` + CLI | `~/.dsh/AGENTS.md` | `~/.dsh/skills/remote-fleet/` |

*(Note: After running `./install.sh` the first time, restart any already-open GUI harness such as Antigravity Desktop or Claude Desktop so it reloads its MCP servers.)*

---

## 2. Three Effortless Ways to Start a New Project from Your Mac

### Way 1: Native Mac Clickable Picker (`Pick Remote Folder.command` / `fleet gui`)
Double-click **`~/Fleet/Pick Remote Folder.command`** in Finder (you can drag it straight onto your macOS Dock!) or run:
```bash
fleet gui
```
1. A native macOS window lists **Mac Mini**, **Home PC**, **DGX Spark 1**, **DGX Spark 2**, and **DGX Spark 3** with live `[LAN]` / `[TAILSCALE]` latency.
2. Click through folders on that remote machine—or click **`[+ New Project Folder Here...]`**.
3. Pick which harness to open on your Mac (**Antigravity**, **Claude Code**, **Codex**, **Kimi Code**, or **DSH**). It opens locally on your Mac, pre-bound to that remote folder!

### Way 2: Open `~/Fleet/Start-Here` Inside Any GUI Harness (Antigravity, Kimi IDE, Cursor)
When clicking **"Open Folder"** to start a brand-new project in a GUI IDE on your Mac:
1. Open **`~/Fleet/Start-Here`**.
2. Type *"Start a new project"* (or `/pick-remote` in Claude).
3. Because `~/Fleet/Start-Here` has `GEMINI.md`, `CLAUDE.md`, `AGENTS.md`, and `.mcp.json` pre-configured, the agent immediately checks live CPU/GPU load across your PC and 3 Sparks (`fleet_list_machines(include_load=true)`), lets you pick or create a folder on any machine, and keeps **all** file changes and compute on that remote machine.

### Way 3: Terminal One-Liner (`fleet start`)
```bash
fleet start --harness claude       # or: antigravity, codex, dsh, kimi
```

---

## 3. Optional: Seeing Remote Files in a Local Sidebar Explorer

By default (`--mode link`), **zero project files need to be copied to your Mac**—the harness edits files and reads datasets directly on the remote machine over MCP/SSH.

If you *also* want a GUI editor's left-hand file tree on your Mac to display the remote source files:
- **Zero-Kernel-Extension Mount on macOS (`rclone nfsmount`)**:
  Install `rclone` (`brew install rclone` — uses macOS's built-in NFSv3 client, **no macFUSE or reboot required!**), then run:
  ```bash
  fleet mount spark-1 ~/projects/my-model
  ```
- **Heavy-Load-Proof Code Watcher (`fleet watch`)**:
  If a Spark's disk is thrashing under heavy calculation (making network mounts feel slow), mirror *only* small code files (`< 2MB`, automatically skipping `*.pt`, `*.safetensors`, `*.parquet`, `datasets/`, `checkpoints/`, `wandb/`) and auto-push saves in ~1s:
  ```bash
  fleet watch
  ```

---

## 4. MCP Tools Reference (`remote-fleet-mcp`)

| Tool | Purpose |
| :--- | :--- |
| `fleet_list_machines` | Parallel `LOCAL` / `LAN` / `TAILSCALE` probe + optional `include_load=true` for live CPU/GPU load |
| `fleet_browse_folders` | Browse directories (and Windows drives) on any computer |
| `fleet_select_project` | Bind active session to an existing remote folder |
| `fleet_create_project` | Create a new folder (`empty`, `python`, `node` + `git init`) on any computer |
| `fleet_current_project` | Inspect current `(machine, remote_path, local_workspace_path)` binding |
| `fleet_list_files` | List remote directory tree (project root or external `/data/...` path) |
| `fleet_read_file` | Read UTF-8 text files (with optional line range) on the remote computer |
| `fleet_write_file` | Create or overwrite files directly on the remote computer |
| `fleet_edit_file` | Exact surgical text replacement on remote files + unified diff |
| `fleet_search_files` | Fast remote code search (`rg` ripgrep or pure-Python fallback) |
| `fleet_run_command` | Run quick commands (`git`, `pytest`, `nvidia-smi`, `uv`) on the remote computer |
| `fleet_start_job` | Launch heavy/long calculations detached on PC/Sparks (`low_priority` option) |
| `fleet_job_status` | Poll status, exit code, and log tail of detached remote jobs |
| `fleet_stop_job` | Stop a running detached remote job |
| `fleet_sync_project` | Dataset-safe code pull/push between Mac handle and remote machine |

---

## Running Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
