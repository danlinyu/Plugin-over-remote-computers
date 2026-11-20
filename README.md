# Plugin-over-remote-computers (`remote-fleet`)

Pick an existing folder—or create a brand-new project folder—on **any computer across your Home Network (LAN) or Tailscale** (e.g., **Mac Mini**, **PC**, and **3 DGX Sparks**), no matter **which computer** you sit down at or **which AI coding harness** you use (**Antigravity**, **Claude**, **Codex**, **DSH**, **Kimi Code**, Cursor, etc.)—without needing to push/clone through GitHub first.

---

## Why This Exists

Normally, starting a project in an LLM coding harness gives you only two options:
1. Open a **local folder** on whichever machine is physically in front of you, or
2. Round-trip everything through **GitHub**.

`remote-fleet` bridges all of your computers over **Home LAN (auto-preferred for sub-millisecond local speed)** with **automatic Tailscale fallback**, giving every harness three seamless ways to start and work on remote projects:

1. **Inside Any Harness Chat (MCP Server + Universal Skill)**:
   Ask the agent (or run `/pick-remote`): *"Show my online machines, browse `~/projects` on `spark-2`, and create a new project `vision-lab` there."*
   The agent reads/edits files and **executes commands, tests, builds, and GPU jobs directly on `spark-2`**.
2. **Interactive Terminal Launcher (`fleet pick` / `fleet start`)**:
   Browse your 5 computers and their folders live in the terminal, then launch `antigravity`, `claude`, `codex`, `dsh`, or `kimi` directly bound to that remote folder.
3. **Native "Open Folder" Bridge (`~/Fleet/<machine>/<project>`)**:
   Every selected remote project automatically gets a local workspace directory at `~/Fleet/<machine>/<project>` supporting:
   - **`link` mode (default, zero-FUSE, instant)**: Drops `.remote-fleet.json`, `.mcp.json`, `AGENTS.md`, and `CLAUDE.md` so opening that folder in *any* harness UI automatically binds MCP & CLI tools to `<machine>:<remote_path>`.
   - **`sync` mode**: Two-way file sync (`rsync` or built-in SSH fallback on Windows) for harnesses that edit local files directly.
   - **`sshfs` mode**: Live OS filesystem mount when `sshfs` / macFUSE is installed.

> **Zero external Python dependencies**: Runs on standard Python 3.10+ + OpenSSH / Tailscale SSH across **macOS** (Mac Mini), **Linux ARM64/x86_64** (DGX Sparks), and **Windows / WSL2** (PC).

---

## Quickstart (Mac Mini + PC + 3 Sparks)

### 1. Install Into All Harnesses

Clone this repo onto the computer(s) where you run your harnesses and run the one-command installer:

```bash
# macOS (Mac Mini), Linux (Sparks), or WSL2:
./install.sh
```

```powershell
# Windows PowerShell (PC):
.\install.ps1
```

Ensure `~/.local/bin` (or `%USERPROFILE%\.local\bin` on Windows) is on your `PATH`.

`install` automatically registers `remote-fleet` across:
- **Antigravity**: `~/.gemini/antigravity/mcp_config.json` + `~/.gemini/antigravity/skills/remote-fleet/` (plus repo-root `plugin.json`)
- **Claude Code & Claude Desktop**: `~/.claude.json`, `~/.claude/skills/remote-fleet/`, and `/pick-remote` + `/fleet-status` slash commands (plus `.claude-plugin/plugin.json`)
- **Codex**: `~/.codex/config.toml` (`[mcp_servers.remote_fleet]`) + `~/.codex/skills/remote-fleet/` + `~/.agents/skills/remote-fleet/`
- **Kimi Code**: `~/.kimi/mcp.json` + `~/.kimi/skills/remote-fleet/`
- **DSH & CLI Agents**: `~/.dsh/skills/remote-fleet/` + `fleet` CLI in `~/.local/bin`

### 2. Discover & Verify Your 5 Computers

By default, `~/.config/remote-fleet/fleet.json` is pre-populated with your 5-machine layout (`mac-mini`, `pc`, `spark-1`, `spark-2`, `spark-3`).

If **Tailscale** is running, auto-populate exact Tailscale MagicDNS names, IPs, and OS types:

```bash
fleet discover --save
```

Verify SSH / Tailscale connectivity across all 5 machines (probed in parallel):

```bash
fleet doctor
```

> **Tip for passwordless auth across all 5 machines**: Either enable **Tailscale SSH** (`sudo tailscale up --ssh` on Sparks / Tailscale SSH toggle) or copy your SSH key once (`ssh-copy-id user@spark-1.local`).

---

## Usage

### Option A: Inside Any Harness Chat (Antigravity, Claude, Codex, Kimi Code, DSH)

Just prompt your agent naturally:
- *"Check which of my computers are online and browse `~/projects` on `spark-1`."*
- *"Pick `/home/danlin/projects/llm-router` on `spark-3` and run `nvidia-smi` and `pytest`."*
- *"Create a new Python project on `pc` at `~/projects/data-pipeline`."*
- In **Claude Code**, you can also type `/pick-remote` or `/fleet-status`.

### Option B: Interactive Terminal Folder Picker & Harness Launcher

```bash
# Interactively pick a computer -> browse/create folder -> set active workspace:
fleet pick

# Pick a folder on any machine and immediately launch a harness:
fleet start --harness claude
fleet start --harness codex
fleet start --harness dsh
fleet start --harness kimi
fleet start --harness antigravity

# Or SSH directly into the remote machine and run a CLI harness on that host:
fleet start --harness claude --remote-ssh
```

### Option C: Non-Interactive CLI Commands

```bash
fleet status                                              # Parallel LAN/Tailscale status & latency table
fleet browse spark-1 ~/projects                           # Browse folders on spark-1
fleet use spark-2 ~/projects/new-app --create --init-git  # Create & select remote folder
fleet ls                                                  # List files in active remote project
fleet exec -- nvidia-smi                                  # Run command directly on active machine
fleet sync pull                                           # Pull remote project files locally
fleet sync push                                           # Push local edits back to remote machine
fleet mount spark-1 ~/projects/app                        # Live-mount via SSHFS at ~/Fleet/spark-1/app
```

---

## MCP Tools Reference (`remote-fleet-mcp`)

| Tool | Purpose |
| :--- | :--- |
| `fleet_list_machines` | Lists all configured + Tailscale-discovered computers with parallel `LOCAL` / `LAN` / `TAILSCALE` latency probes |
| `fleet_browse_folders` | Navigates directories (and Windows drives) on any fleet computer |
| `fleet_select_project` | Binds the session to an existing folder on a target computer and prepares `~/Fleet/<machine>/<project>` |
| `fleet_create_project` | Creates a new folder (`empty`, `python`, or `node` template + optional `git init`) on any computer |
| `fleet_current_project` | Returns the active `(machine, remote_path, local_workspace_path)` binding |
| `fleet_list_files` | Lists project directory tree on the target computer |
| `fleet_read_file` | Reads UTF-8 text files (with optional line-range slicing) from the target computer |
| `fleet_write_file` | Creates or overwrites files on the target computer |
| `fleet_edit_file` | Performs exact surgical replacements on remote files and returns a unified diff |
| `fleet_search_files` | Searches remote code via `rg` (ripgrep) with pure-Python regex fallback |
| `fleet_run_command` | Runs shell commands (`pytest`, `uv`, `git`, `nvidia-smi`, builds) directly on the target computer |
| `fleet_sync_project` | Pulls or pushes files between `~/Fleet/<machine>/<project>` and the remote computer |

---

## Customizing `~/.config/remote-fleet/fleet.json`

See [`configs/fleet.example.json`](configs/fleet.example.json). Each computer entry supports:
- `lan_host`: Home LAN hostname/IP (e.g. `"spark-1.local"` or `"192.168.1.51"`) — tried first when `prefer_lan: true`
- `tailscale_host`: Tailscale MagicDNS name or `100.x.y.z` IP — automatic fallback when away from home or if mDNS isn't reachable
- `user`, `port`, `ssh_identity_file`: Optional per-machine SSH overrides (also respects `~/.ssh/config`)
- `os`: `"auto"`, `"linux"`, `"macos"`, or `"windows"`
- `default_roots`: Favorite starting directories shown first in `fleet pick` and `fleet_browse_folders`

> **Note on Local vs. Cloud Agent Sessions**: Run your harnesses **locally** on your Mac Mini, PC, or Sparks so the machine has network routes to your Home LAN and Tailnet. Cloud-sandboxed browser VMs (e.g., opening a GitHub repo in a remote cloud container without Tailscale) are isolated from private home networks by design.

---

## Running Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
