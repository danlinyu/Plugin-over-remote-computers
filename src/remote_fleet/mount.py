"""Local workspace bridge: macOS rclone nfsmount / SSHFS mounts, dataset-safe code sync, live watch, and Start-Here launchpad."""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from remote_fleet.config import (
    ActiveProjectState,
    FleetConfig,
    MachineConfig,
    WORKSPACE_MARKER_FILENAME,
)
from remote_fleet.transport import FleetTransport, IGNORED_DIRS


# Heavy dataset / checkpoint patterns excluded during code sync so large data stays on Sparks/PC
DATASET_IGNORE_DIRS = {
    "checkpoints",
    "datasets",
    "wandb",
    "mlruns",
    "outputs",
    "logs",
}

DATASET_IGNORE_GLOBS = (
    "*.pt",
    "*.pth",
    "*.safetensors",
    "*.ckpt",
    "*.onnx",
    "*.gguf",
    "*.bin",
    "*.parquet",
    "*.arrow",
    "*.feather",
    "*.h5",
    "*.hdf5",
    "*.npy",
    "*.npz",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.zip",
    "*.7z",
    "*.mp4",
    "*.mov",
    "*.bag",
)

BRIDGE_META_FILES = {
    WORKSPACE_MARKER_FILENAME,
    ".mcp.json",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
}


def get_repo_root() -> Path:
    """Return the root directory of the Plugin-over-remote-computers installation."""
    return Path(__file__).resolve().parents[2]


def _is_dataset_artifact(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(part in DATASET_IGNORE_DIRS for part in parts[:-1]):
        return True
    fname = parts[-1]
    return any(fnmatch.fnmatch(fname.lower(), pat) for pat in DATASET_IGNORE_GLOBS)


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


def create_launchpad_workspace(
    fleet_mount_dir: str = "~/Fleet",
    python_executable: Optional[str] = None,
) -> Path:
    """Create `~/Fleet/Start-Here` — open this folder in ANY harness on your Mac to start a remote project."""
    base = Path(os.path.expanduser(fleet_mount_dir)).resolve()
    launchpad = base / "Start-Here"
    launchpad.mkdir(parents=True, exist_ok=True)

    py_exe = python_executable or sys.executable or "python3"
    repo_src = str(get_repo_root() / "src")
    mcp_payload = {
        "mcpServers": {
            "remote-fleet": {
                "command": py_exe,
                "args": ["-m", "remote_fleet.mcp_server"],
                "env": {"PYTHONPATH": repo_src},
            }
        }
    }
    (launchpad / ".mcp.json").write_text(
        json.dumps(mcp_payload, indent=2) + "\n", encoding="utf-8"
    )

    launchpad_doc = """# Remote Fleet Launchpad (Start a New Project on Any Machine)

You are running on the user's **local interface/terminal machine** (e.g., Mac Mini), while heavy compute and large local datasets live on their remote machines (**PC** and **3 Sparks** over Home LAN / Tailscale).

## Mandatory Startup Behavior in This Folder

1. **Immediately offer to pick a target computer & folder**:
   - Call `fleet_list_machines(probe=true, include_load=true)` to show online computers (`mac-mini`, `pc`, `spark-1`, `spark-2`, `spark-3`) along with their live LAN/Tailscale latency and CPU/GPU load.
   - Let the user browse folders with `fleet_browse_folders(machine=...)` and either pick an existing folder (`fleet_select_project`) or create a new project folder (`fleet_create_project`).
2. **Keep ALL files, changes, and heavy computation on the remote machine**:
   - Do **NOT** create project source files inside this `Start-Here` folder or run heavy CPU/GPU jobs locally on this Mac.
   - Use `fleet_read_file`, `fleet_write_file`, `fleet_edit_file`, `fleet_list_files`, and `fleet_search_files` so every file change stays directly in the selected folder on the remote machine (with full access to its local `/data` or drives).
   - Use `fleet_run_command` for quick commands/tests on the remote machine, and `fleet_start_job` / `fleet_job_status` / `fleet_job_logs` for heavy or long-running calculations so this harness never blocks or slows down.
"""
    for doc_name in ("GEMINI.md", "CLAUDE.md", "AGENTS.md", "README.md"):
        (launchpad / doc_name).write_text(launchpad_doc, encoding="utf-8")

    return launchpad


def _write_harness_bridge_files(
    local_dir: Path,
    machine: MachineConfig,
    remote_path: str,
) -> None:
    """Write `.remote-fleet.json`, `.mcp.json`, `GEMINI.md`, `CLAUDE.md`, and `AGENTS.md` into a linked workspace."""
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
    mcp_file.write_text(json.dumps(mcp_payload, indent=2) + "\n", encoding="utf-8")

    instructions = f"""# Remote Fleet Linked Workspace (`{machine.id}:{remote_path}`)

This local folder (`{local_dir}`) is a lightweight control handle on your interface machine pointing to:

- **Target Computer**: `{machine.id}` ({machine.name})
- **Remote Project Folder**: `{remote_path}`

## Rules for Working on This Project

1. **Keep files, edits, and data on `{machine.id}` — NOT only on this local machine**:
   - Perform all file reads, writes, surgical edits, and code searches directly on `{machine.id}` using the `remote-fleet` MCP tools: `fleet_read_file`, `fleet_write_file`, `fleet_edit_file`, `fleet_list_files`, and `fleet_search_files`.
   - You can also read any other local dataset path on `{machine.id}` (such as `/data/...` or `~/datasets/...`) by passing that absolute path to `fleet_read_file`, `fleet_list_files`, or `fleet_run_command`.
2. **Keep heavy calculations off this interface computer**:
   - Execute builds, tests, python scripts, and GPU workloads on `{machine.id}` via `fleet_run_command` (runs inside `{remote_path}`).
   - For heavy or long-running training/calculation jobs on `{machine.id}`, use `fleet_start_job`, `fleet_job_status`, and `fleet_job_logs` so `{machine.id}` runs detached without slowing down or timing out this harness.
3. **CLI Fallback (`fleet`)**:
   - `fleet ls`, `fleet read <file>`, `fleet exec -- <cmd>`, `fleet jobs start -- <cmd>`, `fleet sync pull`, `fleet sync push`.
"""
    for doc_name in ("GEMINI.md", "CLAUDE.md", "AGENTS.md"):
        doc_path = local_dir / doc_name
        if not doc_path.exists():
            doc_path.write_text(instructions, encoding="utf-8")


def mount_sshfs(
    transport: FleetTransport,
    machine: MachineConfig,
    remote_path: str,
    local_dir: Path,
) -> Dict[str, Any]:
    """Mount `machine:remote_path` onto `local_dir` using macOS `rclone nfsmount` (no macFUSE!) or `sshfs`."""
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
            "mode": "mount",
            "local_path": str(local_dir),
            "already_mounted": True,
            "endpoint": ep.to_dict(),
        }

    inspected = transport.run_worker_op(
        machine, {"op": "inspect_project", "path": remote_path, "create_if_missing": False}
    )
    resolved_remote = inspected.get("path", remote_path) if inspected.get("ok") else remote_path

    # 1. On macOS (or if sshfs is absent), prefer `rclone nfsmount` — uses native macOS NFSv3, zero kernel extensions!
    rclone_bin = shutil.which("rclone")
    is_macos = platform.system().lower() == "darwin"
    sshfs_bin = shutil.which("sshfs")

    if rclone_bin and (is_macos or not sshfs_bin):
        sftp_opts = [f"host={ep.host}"]
        if machine.user:
            sftp_opts.append(f"user={machine.user}")
        if machine.port and machine.port != 22:
            sftp_opts.append(f"port={machine.port}")
        if machine.ssh_identity_file:
            sftp_opts.append(f"key_file={os.path.expanduser(machine.ssh_identity_file)}")
        else:
            sftp_opts.append("use_insecure_cipher=false")
        remote_spec = f":sftp,{','.join(sftp_opts)}:{resolved_remote}"
        mount_subcmd = "nfsmount" if is_macos else "mount"
        cmd = [
            rclone_bin,
            mount_subcmd,
            remote_spec,
            str(local_dir),
            "--daemon",
            "--vfs-cache-mode",
            "writes",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        if proc.returncode == 0:
            return {
                "ok": True,
                "mode": f"rclone-{mount_subcmd}",
                "local_path": str(local_dir),
                "remote_path": resolved_remote,
                "endpoint": ep.to_dict(),
            }

    # 2. Try `sshfs`
    if sshfs_bin:
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
        if proc.returncode == 0:
            return {
                "ok": True,
                "mode": "sshfs",
                "local_path": str(local_dir),
                "remote_path": resolved_remote,
                "endpoint": ep.to_dict(),
            }
        return {
            "ok": False,
            "error": f"sshfs mount failed: {proc.stderr.strip() or proc.stdout.strip()}",
            "endpoint": ep.to_dict(),
        }

    hint = (
        "Install `rclone` (`brew install rclone` — mounts on macOS without macFUSE!) or use `--mode link` / `--mode sync`."
        if is_macos
        else "Install `sshfs` or `rclone`, or use `--mode link` / `--mode sync`."
    )
    return {"ok": False, "error": f"Neither rclone nor sshfs is installed. {hint}"}


def unmount_sshfs(local_dir: Path) -> Dict[str, Any]:
    """Unmount an rclone/SSHFS-mounted workspace directory."""
    resolved = local_dir.expanduser().resolve()
    if not is_mounted(resolved):
        return {"ok": True, "unmounted": False, "message": f"{resolved} is not currently mounted."}

    for candidate_cmd in (
        ["umount", str(resolved)],
        ["diskutil", "unmount", str(resolved)],
        ["fusermount", "-u", str(resolved)],
        ["fusermount3", "-u", str(resolved)],
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
    include_datasets: bool = False,
) -> Dict[str, Any]:
    """Synchronize code files between `local_dir` and `machine:remote_path` (`pull` or `push`).

    By default, excludes heavy dataset/checkpoint files (`*.pt`, `*.safetensors`, `*.parquet`, `datasets/`, etc.)
    so large local data stays on the PC/Sparks without filling up the Mac Mini.
    """
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

        trimmed_remote = resolved_remote.rstrip("/")
        remote_spec = (
            f"{machine.user}@{ep.host}:{trimmed_remote}/"
            if machine.user
            else f"{ep.host}:{trimmed_remote}/"
        )
        trimmed_local = str(local_dir).rstrip("/")
        local_spec = f"{trimmed_local}/"

        cmd = ["rsync", "-az", "--max-size=5m" if not include_datasets else "--max-size=10g"]
        if delete:
            cmd.append("--delete")
        for ignored in sorted(IGNORED_DIRS - {".git"}):
            cmd.extend(["--exclude", ignored])
        if not include_datasets:
            for d_ign in sorted(DATASET_IGNORE_DIRS):
                cmd.extend(["--exclude", f"{d_ign}/"])
            for g_ign in DATASET_IGNORE_GLOBS:
                cmd.extend(["--exclude", g_ign])
        for meta_f in sorted(BRIDGE_META_FILES):
            cmd.extend(["--exclude", meta_f])
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
            if not include_datasets and _is_dataset_artifact(rel_path):
                continue
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
        synced_files = 0
        for root_str, dirs, files in os.walk(local_dir):
            dirs[:] = [
                d
                for d in dirs
                if d not in IGNORED_DIRS
                and (include_datasets or d not in DATASET_IGNORE_DIRS)
                and not d.startswith(".")
            ]
            for fname in files:
                if fname in BRIDGE_META_FILES:
                    continue
                fpath = Path(root_str) / fname
                rel = str(fpath.relative_to(local_dir)).replace("\\", "/")
                if not include_datasets and _is_dataset_artifact(rel):
                    continue
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


def watch_workspace(
    transport: FleetTransport,
    machine: MachineConfig,
    remote_path: str,
    local_dir: Path,
    interval_sec: float = 1.5,
    max_cycles: Optional[int] = None,
    print_fn: Callable[[str], None] = print,
) -> int:
    """Pull lightweight code files once, then auto-push any local file edits to `machine:remote_path`."""
    init_pull = sync_workspace(transport, machine, remote_path, local_dir, direction="pull")
    if not init_pull.get("ok"):
        print_fn(f"Initial pull failed: {init_pull.get('error')}")
        return 1
    resolved_remote = init_pull.get("remote_path", remote_path)
    local_dir = Path(init_pull["local_path"]).resolve()

    def _snapshot() -> Dict[str, float]:
        mtimes: Dict[str, float] = {}
        for root_str, dirs, files in os.walk(local_dir):
            dirs[:] = [
                d for d in dirs if d not in IGNORED_DIRS and d not in DATASET_IGNORE_DIRS and not d.startswith(".")
            ]
            for fname in files:
                if fname in BRIDGE_META_FILES:
                    continue
                fpath = Path(root_str) / fname
                rel = str(fpath.relative_to(local_dir)).replace("\\", "/")
                if _is_dataset_artifact(rel):
                    continue
                try:
                    st = fpath.stat()
                    if st.st_size <= 2_000_000:
                        mtimes[rel] = st.st_mtime
                except OSError:
                    continue
        return mtimes

    known = _snapshot()
    print_fn(
        f"Watching {local_dir} -> {machine.id}:{resolved_remote} "
        f"({len(known)} code files mirrored; datasets stay on {machine.id}). Press Ctrl+C to stop."
    )

    cycles = 0
    try:
        while True:
            if max_cycles is not None and cycles >= max_cycles:
                break
            time.sleep(interval_sec)
            cycles += 1
            current = _snapshot()
            changed = [rel for rel, mt in current.items() if known.get(rel) != mt]
            for rel in changed:
                fpath = local_dir / rel
                try:
                    content = fpath.read_text(encoding="utf-8")
                except Exception:
                    continue
                res = transport.run_worker_op(
                    machine,
                    {"op": "write_file", "base_dir": resolved_remote, "path": rel, "content": content},
                    timeout_sec=20,
                )
                if res.get("ok"):
                    print_fn(f"  [synced -> {machine.id}] {rel}")
                    known[rel] = current[rel]
    except KeyboardInterrupt:
        print_fn("\nStopped workspace watcher.")
    return 0


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
