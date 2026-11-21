"""macOS / OS-level integration: `~/.ssh/config` generator (for Codex SSH / Remote-SSH) and `~/RemoteComputers` auto-mounter (for native 'Choose Folder' dialogs in Antigravity, Claude, Codex, Kimi, DSH)."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from remote_fleet.config import (
    FleetConfig,
    MachineConfig,
    TailscaleNodeInfo,
    is_self_machine,
    load_mounts_registry,
    save_mounts_registry,
)
from remote_fleet.mount import get_repo_root, is_mounted, unmount_sshfs
from remote_fleet.transport import FleetTransport


SSH_BEGIN_MARKER = "# >>> remote-fleet-ssh >>>"
SSH_END_MARKER = "# <<< remote-fleet-ssh <<<"

LAUNCH_AGENT_LABEL = "com.remote-fleet.automount"


def render_ssh_config_block(
    config: FleetConfig,
    tailscale_self: Optional[TailscaleNodeInfo] = None,
) -> str:
    """Render `~/.ssh/config` entries with automatic Home-LAN-first -> Tailscale fallback.

    Harnesses with built-in SSH host pickers (like OpenAI Codex Desktop and Antigravity Remote-SSH)
    automatically read `~/.ssh/config` and show `spark-1`, `spark-2`, `spark-3`, `pc` in their UI!
    """
    lines: List[str] = [
        SSH_BEGIN_MARKER,
        "# Managed by remote-fleet (`fleet ssh-config`). Automatic Home LAN -> Tailscale failover.",
    ]
    probe_sec = max(0.2, round(config.lan_probe_timeout_ms / 1000.0, 2))

    for m in config.machines:
        if is_self_machine(m, tailscale_self):
            continue
        primary_lan = m.lan_host.strip()
        fallback_ts = (m.tailscale_host or m.lan_host or m.id).strip()
        port = m.port or 22

        if config.prefer_lan and primary_lan and fallback_ts and primary_lan != fallback_ts:
            probe_py = (
                f"python3 -c 'import socket; s=socket.create_connection((\"{primary_lan}\",{port}),{probe_sec}); s.close()' 2>/dev/null"
            )
            lines.append(f'Match host {m.id} exec "{probe_py}"')
            lines.append(f"    HostName {primary_lan}")
            lines.append("")

        lines.append(f"Host {m.id}")
        lines.append(f"    HostName {fallback_ts if (primary_lan != fallback_ts and fallback_ts) else (primary_lan or fallback_ts)}")
        if m.user:
            lines.append(f"    User {m.user}")
        if port != 22:
            lines.append(f"    Port {port}")
        if m.ssh_identity_file:
            lines.append(f"    IdentityFile {m.ssh_identity_file}")
        lines.append("    ServerAliveInterval 15")
        lines.append("    ServerAliveCountMax 3")
        lines.append("")

    lines.append(SSH_END_MARKER)
    return "\n".join(lines) + "\n"


def sync_ssh_config(
    config: FleetConfig,
    tailscale_self: Optional[TailscaleNodeInfo] = None,
    ssh_config_path: Optional[Path] = None,
) -> Path:
    """Idempotently write `remote-fleet` hosts into `~/.ssh/config` with safe 0600 permissions."""
    target = (ssh_config_path or (Path.home() / ".ssh" / "config")).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    block = render_ssh_config_block(config, tailscale_self)

    pattern = re.compile(
        re.escape(SSH_BEGIN_MARKER) + r".*?" + re.escape(SSH_END_MARKER) + r"\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing)
    else:
        # Insert before `Host *` if present so user catch-all defaults stay at bottom
        wildcard_match = re.search(r"(?m)^Host\s+\*\s*$", existing)
        if wildcard_match:
            idx = wildcard_match.start()
            prefix = existing[:idx].rstrip("\n")
            suffix = existing[idx:]
            updated = (f"{prefix}\n\n{block}\n{suffix}") if prefix else f"{block}\n{suffix}"
        else:
            sep = "\n" if existing and not existing.endswith("\n") else ""
            extra_nl = "\n" if existing.strip() else ""
            updated = f"{existing}{sep}{extra_nl}{block}"

    target.write_text(updated, encoding="utf-8")
    if os.name != "nt":
        try:
            target.chmod(0o600)
        except OSError:
            pass
    return target


def find_rclone_binary() -> Optional[str]:
    found = shutil.which("rclone")
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / "rclone",
        Path("/opt/homebrew/bin/rclone"),
        Path("/usr/local/bin/rclone"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _ssh_agent_has_keys() -> bool:
    if not os.environ.get("SSH_AUTH_SOCK") or not shutil.which("ssh-add"):
        return False
    proc = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=2, check=False)
    return proc.returncode == 0 and "no identities" not in proc.stdout.lower()


def _detect_default_ssh_key(machine: MachineConfig) -> Optional[str]:
    if machine.ssh_identity_file:
        expanded = os.path.expanduser(machine.ssh_identity_file)
        if os.path.isfile(expanded):
            return expanded
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        cand = ssh_dir / name
        if cand.is_file():
            return str(cand)
    return None


def mount_single_machine_root(
    transport: FleetTransport,
    machine: MachineConfig,
    base_dir: Path,
) -> Dict[str, Any]:
    """Mount a remote computer's home/root directory onto `~/RemoteComputers/<machine.id>`."""
    ep = transport.resolve_endpoint(machine)
    if ep.transport == "local":
        return {"ok": True, "skipped": True, "reason": "self (local computer)", "machine_id": machine.id}
    if not ep.reachable:
        return {"ok": False, "machine_id": machine.id, "error": f"Unreachable ({ep.error})"}

    ping = transport.run_worker_op(machine, {"op": "ping"}, timeout_sec=8)
    if not ping.get("ok"):
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"SSH auth failed on {ep.host}: {ping.get('error')}",
        }

    remote_home = str(ping.get("home") or "/").replace("\\", "/")
    mount_point = (base_dir / machine.id).resolve()
    mount_point.mkdir(parents=True, exist_ok=True)

    if is_mounted(mount_point):
        reg = load_mounts_registry()
        reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_home}
        save_mounts_registry(reg)
        return {
            "ok": True,
            "machine_id": machine.id,
            "mount_point": str(mount_point),
            "remote_root": remote_home,
            "already_mounted": True,
            "endpoint": ep.to_dict(),
        }

    is_macos = platform.system().lower() == "darwin"
    rclone_bin = find_rclone_binary()
    sshfs_bin = shutil.which("sshfs")

    # Prefer `rclone nfsmount` on macOS: native macOS user-space NFSv3, zero macFUSE kernel extensions!
    if rclone_bin and (is_macos or not sshfs_bin):
        sftp_opts = [f"host={ep.host}"]
        if machine.user:
            sftp_opts.append(f"user={machine.user}")
        if machine.port and machine.port != 22:
            sftp_opts.append(f"port={machine.port}")
        key_path = _detect_default_ssh_key(machine)
        if key_path:
            sftp_opts.append(f"key_file={key_path}")
        elif _ssh_agent_has_keys():
            sftp_opts.append("key_use_agent=true")

        # Windows OpenSSH SFTP paths start with `/C:/Users/...` when absolute
        rclone_path = f"/{remote_home}" if (len(remote_home) > 2 and remote_home[1] == ":") else remote_home
        remote_spec = f":sftp,{','.join(sftp_opts)}:{rclone_path}"
        subcmd = "nfsmount" if is_macos else "mount"
        cmd = [
            rclone_bin,
            subcmd,
            remote_spec,
            str(mount_point),
            "--daemon",
            "--vfs-cache-mode",
            "writes",
            "--dir-cache-time",
            "5s",
            "--attr-timeout",
            "3s",
            "--volname",
            f"Fleet-{machine.id}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25, check=False)
        if proc.returncode == 0:
            reg = load_mounts_registry()
            reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_home}
            save_mounts_registry(reg)
            return {
                "ok": True,
                "machine_id": machine.id,
                "engine": f"rclone-{subcmd}",
                "mount_point": str(mount_point),
                "remote_root": remote_home,
                "endpoint": ep.to_dict(),
            }
        # If key_file had a passphrase and ssh-agent has keys, retry with key_use_agent=true
        if key_path and _ssh_agent_has_keys():
            retry_opts = [o for o in sftp_opts if not o.startswith("key_file=")] + ["key_use_agent=true"]
            cmd[2] = f":sftp,{','.join(retry_opts)}:{rclone_path}"
            proc2 = subprocess.run(cmd, capture_output=True, text=True, timeout=25, check=False)
            if proc2.returncode == 0:
                reg = load_mounts_registry()
                reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_home}
                save_mounts_registry(reg)
                return {
                    "ok": True,
                    "machine_id": machine.id,
                    "engine": f"rclone-{subcmd}",
                    "mount_point": str(mount_point),
                    "remote_root": remote_home,
                    "endpoint": ep.to_dict(),
                }
        err_msg = proc.stderr.strip() or proc.stdout.strip()
        if not sshfs_bin:
            return {"ok": False, "machine_id": machine.id, "error": f"rclone {subcmd} failed: {err_msg}"}

    if sshfs_bin:
        target_spec = (
            f"{machine.user}@{ep.host}:{remote_home}"
            if machine.user
            else f"{ep.host}:{remote_home}"
        )
        cmd = [
            sshfs_bin,
            target_spec,
            str(mount_point),
            "-o",
            "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3",
        ]
        if machine.port and machine.port != 22:
            cmd.extend(["-p", str(machine.port)])
        key_path = _detect_default_ssh_key(machine)
        if key_path:
            cmd.extend(["-o", f"IdentityFile={key_path}"])
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        if proc.returncode == 0:
            reg = load_mounts_registry()
            reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_home}
            save_mounts_registry(reg)
            return {
                "ok": True,
                "machine_id": machine.id,
                "engine": "sshfs",
                "mount_point": str(mount_point),
                "remote_root": remote_home,
                "endpoint": ep.to_dict(),
            }
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"sshfs failed: {proc.stderr.strip() or proc.stdout.strip()}",
        }

    return {
        "ok": False,
        "machine_id": machine.id,
        "missing_mounter": True,
        "error": (
            "Install `rclone` (`brew install rclone`) on your Mac so `~/RemoteComputers/<machine>` "
            "mounts natively via macOS NFSv3 (no macFUSE required!)."
        ),
    }


def mount_all_machines(
    config: FleetConfig,
    transport: FleetTransport,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Mount all reachable remote fleet computers under `~/RemoteComputers/<id>`."""
    root = (base_dir or (Path.home() / "RemoteComputers")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Prime parallel endpoint cache
    transport.resolve_all_endpoints(config.machines, force_refresh=True)

    results = []
    for m in config.machines:
        results.append(mount_single_machine_root(transport, m, root))
    return {"base_dir": str(root), "results": results}


def unmount_all_machines(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    root = (base_dir or (Path.home() / "RemoteComputers")).expanduser().resolve()
    results = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir():
                results.append({"path": str(child), **unmount_sshfs(child)})
    return {"base_dir": str(root), "results": results}


def install_macos_launch_agent(
    home_dir: Optional[Path] = None,
    python_executable: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> Path:
    """Create `~/Library/LaunchAgents/com.remote-fleet.automount.plist` to mount computers at login."""
    home = (home_dir or Path.home()).expanduser().resolve()
    py_exe = python_executable or sys.executable or "/usr/bin/python3"
    repo_src = str(get_repo_root() / "src")
    mount_root = str((base_dir or (home / "RemoteComputers")).expanduser().resolve())

    agents_dir = home / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{LAUNCH_AGENT_LABEL}.plist"

    plist_data = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            py_exe,
            "-m",
            "remote_fleet",
            "mount-all",
            "--dir",
            mount_root,
        ],
        "EnvironmentVariables": {
            "PYTHONPATH": repo_src,
            "PATH": f"{home / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "StandardOutPath": str(home / ".cache" / "remote-fleet" / "automount.log"),
        "StandardErrorPath": str(home / ".cache" / "remote-fleet" / "automount.err.log"),
    }
    (home / ".cache" / "remote-fleet").mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as fp:
        plistlib.dump(plist_data, fp)
    return plist_path
