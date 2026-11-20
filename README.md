# Plugin-over-remote-computers (`remote-fleet`)

Use your **Mac Mini** as a fast, lag-free **control interface** for **Antigravity**, **Claude**, **Codex**, **DSH**, **Kimi Code**, and Cursor—while **picking folders, keeping all project files & local datasets, and running heavy calculations** directly on your **PC** and **3 DGX Sparks** over **Home LAN or Tailscale**, with zero GitHub round-trips required.

---

## How It Makes Remote Computers Appear in Every Harness's "New / Create Project" UI

Closed-source desktop apps don't allow third-party plugins to add custom radio buttons inside their welcome screens—instead, `fleet setup-mac` plugs your **PC** and **3 Sparks** into the **two native OS hooks that every harness's "New Project" button already reads**:

1. **macOS Folder Picker (`~/RemoteComputers/{spark-1,spark-2,spark-3,pc}`)**:
   Mounts all 4 remote computers into `~/RemoteComputers/` via `rclone nfsmount` (macOS's built-in NFSv3 client—**no macFUSE kernel extension or reboot needed!**). Drag `~/RemoteComputers` into your **Finder Sidebar Favorites**, and clicking **"Create New Project"** (Antigravity), **"New"** (Claude), **"Create Project"** (Codex), **Kimi**, or **DSH** lets you pick or create any folder on `spark-1..3` or `pc` right inside the normal folder picker!
2. **SSH Host Picker (`~/.ssh/config`)**:
   Writes `Host spark-1`, `Host spark-2`, `Host spark-3`, and `Host pc` with automatic **Home LAN (0.4s fast probe) -> Tailscale failover** into `~/.ssh/config`—so harnesses with built-in **SSH** project pickers (like **Codex** and **Antigravity Remote-SSH**) immediately list all 4 computers!
3. **`remote-fleet` MCP Server + Global Rules**:
   Automatically detects whenever a harness opens a folder inside `~/RemoteComputers/<machine>/...` and routes commands & detached heavy jobs (`fleet_run_command`, `fleet_start_job`) directly to that remote machine—so **0% heavy calculation runs on your Mac Mini**.

---

## 1. Exact One-Time Setup on Your Mac Mini

```bash
# 1. Install rclone on your Mac (enables native macOS NFSv3 mounts without macFUSE):
brew install rclone

# 2. Clone this repo & run the all-in-one Mac setup:
git clone https://github.com/danlinyu/Plugin-over-remote-computers.git
cd Plugin-over-remote-computers
./install.sh
~/.local/bin/fleet setup-mac
```

> **Tip**: Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`. Ensure passwordless SSH works from your Mac to each Spark/PC (either enable Tailscale SSH with `sudo tailscale up --ssh` on each Spark, or run `ssh-copy-id <user>@spark-1.local`), customize usernames/IPs in `~/.config/remote-fleet/fleet.json` if needed, and re-run `fleet setup-mac` + `fleet doctor`.

---

## 2. What to Click in Each Harness When Starting a New Project

Once you drag **`~/RemoteComputers`** into your **macOS Finder Sidebar** (and restart open GUI apps once):

- **Antigravity ("Create New Project")**:
  Click **Create New Project** -> **Open Local Folder / This Computer** -> click **`RemoteComputers` -> `spark-1` (or `spark-2`, `spark-3`, `pc`)** in the Finder sidebar -> pick or create your project folder! *(All UI stays fast on your Mac; files stay on the Spark/PC; MCP runs compute on the Spark/PC.)*
- **Claude Code / Claude Desktop ("New")**:
  Click **New** -> select folder -> choose **`RemoteComputers` -> `<machine>` -> `<folder>`** (or run `/pick-remote` in chat).
- **Codex ("Create Project")**:
  - **Option A (SSH)**: Choose **SSH** -> pick `spark-1`, `spark-2`, `spark-3`, or `pc` (auto-configured in `~/.ssh/config` with Home LAN + Tailscale failover!).
  - **Option B (Local UI + Mounted Folder)**: Choose **`RemoteComputers` -> `<machine>` -> `<folder>`**.
- **Kimi Code & DSH**:
  Open **`~/RemoteComputers/<machine>/<folder>`** in the folder picker, or run `fleet start --harness kimi` / `fleet start --harness dsh` (or double-click **`~/Fleet/Pick Remote Folder.command`**).

---

## 3. MCP Tools Reference (`remote-fleet-mcp`)

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
