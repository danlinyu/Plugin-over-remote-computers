"""Local workspace bridge: SSHFS live mounts, two-way sync, and zero-FUSE linked workspaces."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from remote_fleet.config import (
    ActiveProjectState,
    FleetConfig,
    MachineConfig,
    WORKSPACE_MARKER_FILENAME,
)
from remote_fleet.transport import FleetTransport, IGNORED_DIRS


def get_repo_root() -> Path:
    """Return the root directory of the Plugin-over-remote-computers installation."""
    return Path(__file__).resolve().parents[2]


def compute_local_workspace_dir(
    config: FleetConfig, machine: MachineConfig, remote_path: str
) -> Path:
    """Compute deterministic local folder path under `~/Fleet/<machine_id>/<project_name>`."""
    base = Path(os.path.expanduser(config.fleet_mount_dir)).resolve()
    cleaned = remote_path.rstrip("/\\")
    leaf = cleaned.split("/")[-1].split("\\")[-1] or "root"
    if leaf in ("~", ".", ""):
        leaf = "home"
    safe_leaf = re.sub(r"[^A-Za-z0-9._-]+", "-", leaf).strip("-") or "project"
    return base / machine.id / safe_leaf


def is_mounted(path: Path) -> bool:
    """Check whether `path` is currently an active OS mountpoint."""
    try:
        return os.path.ismount(str(path))
    except Exception:
        return False


def _write_harness_bridge_files(
    local_dir: Path,
    machine: MachineConfig,
    remote_path: str,
) -> None:
    """Write `.remote-fleet.json`, `.mcp.json`, `AGENTS.md`, and `CLAUDE.md` into a linked workspace."""
    local_dir.mkdir(parents=True, exist_ok=True)
    state = ActiveProjectState(
        machine_id=machine.id,
        remote_path=remote_path,
        local_workspace_path=str(local_dir),
    )
    (local_dir / WORKSPACE_MARKER_FILENAME).write_text(
        json.dumps(state.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )

    repo_src = str(get_repo_root() / "src")
    mcp_payload = {
        "mcpServers": {
            "remote-fleet": {
                "command": sys.executable or "python3",
                "args": ["-m", "remote_fleet.mcp_server"],
                "env": {
                    "PYTHONPATH": repo_src,
                    "REMOTE_FLEET_MACHINE": machine.id,
                    "REMOTE_FLEET_PATH": remote_path,
                },
            }
        }
    }
    mcp_file = local_dir / ".mcp.json"
    if not mcp_file.exists():
        mcp_file.write_text(json.dumps(mcp_payload, indent=2) + "\n", encoding="utf-8")

    instructions = f"""# Remote Fleet Linked Workspace

This local directory (`{local_dir}`) is linked to a remote project folder over your Home LAN / Tailscale:

- **Target Machine**: `{machine.id}` ({machine.name})
- **Remote Folder**: `{remote_path}`

## How to Work on This Project

1. **Preferred — MCP Tools (`remote-fleet`)**:
   - Read/write/edit/search files directly on `{machine.id}` using `fleet_read_file`, `fleet_write_file`, `fleet_edit_file`, `fleet_list_files`, and `fleet_search_files`.
   - Run builds, tests, git, and GPU workloads directly on `{machine.id}` using `fleet_run_command` (automatically executes inside `{remote_path}`).
2. **CLI Fallback (`fleet`)**:
   - List files: `fleet ls`
   - Read a file: `fleet read <relative_path>`
   - Run a command on `{machine.id}`: `fleet exec "<command>"`
   - Pull remote files locally: `fleet sync pull`
   - Push local edits to `{machine.id}`: `fleet sync push`
"""
    for doc_name in ("AGENTS.md", "CLAUDE.md"):
        doc_path = local_dir / doc_name
        if not doc_path.exists():
            doc_path.write_text(instructions, encoding="utf-8")


def mount_sshfs(
    transport: FleetTransport,
    machine: MachineConfig,
    remote_path: str,
    local_dir: Path,
) -> Dict[str, Any]:
    """Mount `machine:remote_path` onto `local_dir` via `sshfs` if installed."""
    sshfs_bin = shutil.which("sshfs")
    if not sshfs_bin:
        return {
            "ok": False,
            "error": "sshfs is not installed on this computer (use `--mode link` or `--mode sync`, or install sshfs/macFUSE).",
        }

    ep = transport.resolve_endpoint(machine)
    if ep.transport == "local":
        return {
            "ok": True,
            "mode": "local",
            "local_path": os.path.expanduser(remote_path),
            "message": "Target machine is the current computer; using local directory directly.",
        }
    if not ep.reachable:
        return {"ok": False, "error": f"Machine '{machine.id}' is unreachable: {ep.error}"}

    local_dir.mkdir(parents=True, exist_ok=True)
    if is_mounted(local_dir):
        return {
            "ok": True,
            "mode": "sshfs",
            "local_path": str(local_dir),
            "already_mounted": True,
            "endpoint": ep.to_dict(),
        }

    # Resolve `~` on remote host if needed so sshfs gets an exact path
    inspected = transport.run_worker_op(
        machine, {"op": "inspect_project", "path": remote_path, "create_if_missing": False}
    )
    resolved_remote = inspected.get("path", remote_path) if inspected.get("ok") else remote_path

    target_spec = (
        f"{machine.user}@{ep.host}:{resolved_remote}"
        if machine.user
        else f"{ep.host}:{resolved_remote}"
    )
    cmd = [
        sshfs_bin,
        target_spec,
        str(local_dir),
        "-o",
        "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3",
    ]
    if machine.port and machine.port != 22:
        cmd.extend(["-p", str(machine.port)])
    if machine.ssh_identity_file:
        cmd.extend(["-o", f"IdentityFile={os.path.expanduser(machine.ssh_identity_file)}"])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"sshfs mount failed: {proc.stderr.strip() or proc.stdout.strip()}",
            "endpoint": ep.to_dict(),
        }

    return {
        "ok": True,
        "mode": "sshfs",
        "local_path": str(local_dir),
        "remote_path": resolved_remote,
        "endpoint": ep.to_dict(),
    }


def unmount_sshfs(local_dir: Path) -> Dict[str, Any]:
    """Unmount an SSHFS-mounted workspace directory."""
    resolved = local_dir.expanduser().resolve()
    if not is_mounted(resolved):
        return {"ok": True, "unmounted": False, "message": f"{resolved} is not currently mounted."}

    for candidate_cmd in (
        ["fusermount", "-u", str(resolved)],
        ["fusermount3", "-u", str(resolved)],
        ["umount", str(resolved)],
    ):
        if shutil.which(candidate_cmd[0]):
            proc = subprocess.run(candidate_cmd, capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode == 0:
                return {"ok": True, "unmounted": True, "local_path": str(resolved)}

    return {"ok": False, "error": f"Failed to unmount {resolved}"}


def sync_workspace(
    transport: FleetTransport,
    machine: MachineConfig,
    remote_path: str,
    local_dir: Path,
    direction: str = "pull",
    delete: bool = False,
) -> Dict[str, Any]:
    """Synchronize files between `local_dir` and `machine:remote_path` (`pull` or `push`)."""
    if direction not in ("pull", "push"):
        return {"ok": False, "error": f"Invalid sync direction '{direction}'; expected 'pull' or 'push'."}

    ep = transport.resolve_endpoint(machine)
    if ep.transport == "local":
        return {
            "ok": True,
            "direction": direction,
            "engine": "noop-local",
            "local_path": os.path.expanduser(remote_path),
            "message": "Machine is local; no network sync needed.",
        }
    if not ep.reachable:
        return {"ok": False, "error": f"Machine '{machine.id}' is unreachable: {ep.error}"}

    inspected = transport.run_worker_op(
        machine,
        {"op": "inspect_project", "path": remote_path, "create_if_missing": direction == "push"},
    )
    if not inspected.get("ok"):
        return inspected
    resolved_remote = inspected["path"]

    local_dir = local_dir.expanduser().resolve()
    local_dir.mkdir(parents=True, exist_ok=True)

    # Prefer rsync when available locally, on POSIX remote hosts, and using real ssh
    if (
        shutil.which("rsync")
        and machine.os != "windows"
        and not os.environ.get("REMOTE_FLEET_SSH_BIN")
    ):
        ssh_parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
        if machine.port and machine.port != 22:
            ssh_parts.extend(["-p", str(machine.port)])
        if machine.ssh_identity_file:
            ssh_parts.extend(["-i", os.path.expanduser(machine.ssh_identity_file)])

        remote_spec = (
            f"{machine.user}@{ep.host}:{resolved_remote.rstrip('/')}/"
            if machine.user
            else f"{ep.host}:{resolved_remote.rstrip('/')}/"
        )
        local_spec = f"{str(local_dir).rstrip('/')}/"

        cmd = ["rsync", "-az"]
        if delete:
            cmd.append("--delete")
        for ignored in sorted(IGNORED_DIRS - {".git"}):
            cmd.extend(["--exclude", ignored])
        cmd.extend(["--exclude", WORKSPACE_MARKER_FILENAME])
        cmd.extend(["-e", " ".join(shlex.quote(p) for p in ssh_parts)])

        if direction == "pull":
            cmd.extend([remote_spec, local_spec])
        else:
            cmd.extend([local_spec, remote_spec])

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
        if proc.returncode == 0:
            _write_harness_bridge_files(local_dir, machine, resolved_remote)
            return {
                "ok": True,
                "direction": direction,
                "engine": "rsync",
                "local_path": str(local_dir),
                "remote_path": resolved_remote,
                "endpoint": ep.to_dict(),
            }

    # Portable fallback (works on Windows, macOS, Linux, and custom SSH wrappers without rsync)
    if direction == "pull":
        listing = transport.run_worker_op(
            machine,
            {"op": "list_files", "base_dir": resolved_remote, "path": "", "max_depth": 6, "max_entries": 400},
            timeout_sec=60,
        )
        if not listing.get("ok"):
            return listing
        synced_files = 0
        for item in listing.get("items", []):
            if item.get("type") != "file":
                continue
            rel_path = item["path"]
            if item.get("size", 0) > 2_000_000:
                continue
            read_res = transport.run_worker_op(
                machine,
                {"op": "read_file", "base_dir": resolved_remote, "path": rel_path},
                timeout_sec=30,
            )
            if read_res.get("ok"):
                dest = local_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(read_res["content"], encoding="utf-8")
                synced_files += 1
        _write_harness_bridge_files(local_dir, machine, resolved_remote)
        return {
            "ok": True,
            "direction": "pull",
            "engine": "ssh-worker",
            "files_synced": synced_files,
            "local_path": str(local_dir),
            "remote_path": resolved_remote,
            "endpoint": ep.to_dict(),
        }
    else:
        skip_files = {WORKSPACE_MARKER_FILENAME, ".mcp.json", "AGENTS.md", "CLAUDE.md"}
        synced_files = 0
        for root_str, dirs, files in os.walk(local_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for fname in files:
                if fname in skip_files:
                    continue
                fpath = Path(root_str) / fname
                rel = str(fpath.relative_to(local_dir)).replace("\\", "/")
                try:
                    raw = fpath.read_bytes()
                    if len(raw) > 2_000_000 or b"\x00" in raw[:4096]:
                        continue
                    text = raw.decode("utf-8")
                except Exception:
                    continue
                wres = transport.run_worker_op(
                    machine,
                    {"op": "write_file", "base_dir": resolved_remote, "path": rel, "content": text},
                    timeout_sec=30,
                )
                if wres.get("ok"):
                    synced_files += 1
        return {
            "ok": True,
            "direction": "push",
            "engine": "ssh-worker",
            "files_synced": synced_files,
            "local_path": str(local_dir),
            "remote_path": resolved_remote,
            "endpoint": ep.to_dict(),
        }


def prepare_local_workspace(
    config: FleetConfig,
    transport: FleetTransport,
    machine: MachineConfig,
    remote_path: str,
    mode: str = "link",  # "link" | "sync" | "sshfs"
) -> Dict[str, Any]:
    """Prepare a local folder that any harness can open for `machine:remote_path`."""
    ep = transport.resolve_endpoint(machine)
    if ep.transport == "local":
        local_real = str(Path(os.path.expanduser(remote_path)).resolve())
        return {
            "ok": True,
            "mode": "local",
            "local_path": local_real,
            "remote_path": local_real,
            "endpoint": ep.to_dict(),
        }

    local_dir = compute_local_workspace_dir(config, machine, remote_path)
    if mode == "sshfs":
        return mount_sshfs(transport, machine, remote_path, local_dir)
    if mode == "sync":
        return sync_workspace(transport, machine, remote_path, local_dir, direction="pull")

    # Default "link" mode: zero-FUSE instant local proxy workspace with .remote-fleet.json + .mcp.json + AGENTS.md + CLAUDE.md
    inspected = transport.run_worker_op(
        machine, {"op": "inspect_project", "path": remote_path, "create_if_missing": False}
    )
    resolved_remote = inspected["path"] if inspected.get("ok") else remote_path
    _write_harness_bridge_files(local_dir, machine, resolved_remote)
    return {
        "ok": True,
        "mode": "link",
        "local_path": str(local_dir),
        "remote_path": resolved_remote,
        "endpoint": ep.to_dict(),
    }
