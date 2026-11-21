"""macOS / OS-level integration: `~/.ssh/config` generator and `~/RemoteComputers` verified live mounter."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from remote_fleet.config import (
    FleetConfig,
    MachineConfig,
    TailscaleNodeInfo,
    is_local_tailscale_running,
    is_self_machine,
    is_tailscale_only_host,
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
    tailscale_available: Optional[bool] = None,
) -> str:
    """Render `~/.ssh/config` entries.

    - When Tailscale IS installed/running locally: emits automatic Home-LAN-first -> Tailscale fallback.
    - When Tailscale is NOT installed/running on this Mac: emits direct Home LAN `HostName` entries
      so SSH never falls through to an unroutable `100.x.y.z` IP.
    """
    ts_active = (
        tailscale_available
        if tailscale_available is not None
        else ((tailscale_self is not None) or is_local_tailscale_running())
    )
    mode_note = (
        "Automatic Home LAN -> Tailscale failover."
        if ts_active
        else "Home LAN direct mode (Tailscale not currently active on this computer)."
    )
    lines: List[str] = [
        SSH_BEGIN_MARKER,
        f"# Managed by remote-fleet (`fleet ssh-config`). {mode_note}",
    ]
    base_probe = max(0.3, round(config.lan_probe_timeout_ms / 1000.0, 2))

    for m in config.machines:
        if is_self_machine(m, tailscale_self):
            continue
        primary_lan = m.lan_host.strip()
        raw_ts = (m.tailscale_host or "").strip()
        ts_usable = bool(raw_ts) and (ts_active or not is_tailscale_only_host(raw_ts))
        port = m.port or 22

        if config.prefer_lan and primary_lan and ts_usable and primary_lan != raw_ts:
            probe_sec = max(1.2, base_probe) if primary_lan.lower().endswith(".local") else base_probe
            probe_py = (
                f"python3 -c 'import socket; s=socket.create_connection((\"{primary_lan}\",{port}),{probe_sec}); s.close()' 2>/dev/null"
            )
            lines.append(f'Match host {m.id} exec "{probe_py}"')
            lines.append(f"    HostName {primary_lan}")
            lines.append("")
            default_hostname = raw_ts
        else:
            default_hostname = primary_lan or (raw_ts if ts_usable else primary_lan) or m.id

        lines.append(f"Host {m.id}")
        lines.append(f"    HostName {default_hostname}")
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
    tailscale_available: Optional[bool] = None,
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
    block = render_ssh_config_block(config, tailscale_self, tailscale_available=tailscale_available)

    pattern = re.compile(
        re.escape(SSH_BEGIN_MARKER) + r".*?" + re.escape(SSH_END_MARKER) + r"\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing)
    else:
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


def build_rclone_sftp_ssh_command(machine: MachineConfig, host: str) -> str:
    """Build `--sftp-ssh` string so rclone uses `/usr/bin/ssh` directly."""
    parts = [
        shutil.which("ssh") or "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if machine.port and machine.port != 22:
        parts.extend(["-p", str(machine.port)])
    if machine.ssh_identity_file:
        parts.extend(["-i", os.path.expanduser(machine.ssh_identity_file)])
    target = f"{machine.user}@{host}" if machine.user else host
    parts.append(target)
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def _cleanup_unmounted_empty_dir(mount_point: Path) -> None:
    """Remove an unmounted folder that only contains `.DS_Store` or nothing so Finder never shows dead empty folders."""
    if not mount_point.is_dir() or is_mounted(mount_point):
        return
    try:
        items = [p for p in mount_point.iterdir() if p.name != ".DS_Store"]
        if not items:
            ds = mount_point / ".DS_Store"
            if ds.is_file():
                ds.unlink()
            mount_point.rmdir()
    except OSError:
        pass


def _select_verified_rclone_sftp_flags(
    rclone_bin: str,
    machine: MachineConfig,
    host: str,
    rclone_path: str,
    allow_empty_fallback: bool = True,
) -> Tuple[str, Optional[List[str]], str]:
    """Run a fast `rclone lsf --max-depth 1` preflight to pick working SFTP path & flags before mounting."""
    flag_candidates: List[Tuple[str, List[str]]] = []
    common_sftp_opts = [
        "--sftp-shell-type",
        "none",
        "--sftp-md5sum-command",
        "none",
        "--sftp-sha1sum-command",
        "none",
    ]
    if machine.os == "windows":
        # Skip legacy NTFS junction traps in C:\Users\<user> and C:\ (Cookies, My Documents, Documents and Settings, etc.)
        common_sftp_opts.append("--sftp-skip-links")

    key_path = _detect_default_ssh_key(machine)
    if key_path:
        native_flags = [
            "--sftp-host",
            host,
            "--sftp-port",
            str(machine.port or 22),
            "--sftp-key-file",
            key_path,
            *common_sftp_opts,
        ]
        if machine.user:
            native_flags.extend(["--sftp-user", machine.user])
        flag_candidates.append(("native-key", native_flags))

    ssh_ext = build_rclone_sftp_ssh_command(machine, host)
    ext_flags = [
        "--sftp-ssh",
        ssh_ext,
        *common_sftp_opts,
    ]
    flag_candidates.append(("external-ssh", ext_flags))

    raw_paths: List[str] = []
    if machine.os == "windows":
        drv_match = re.match(r"^/?([A-Za-z]):/?$", rclone_path.strip())
        if drv_match and not allow_empty_fallback:
            letter = drv_match.group(1).upper()
            raw_paths = [f"/{letter}:/", f"{letter}:/", f"/{letter}:", f"{letter}:"]
        else:
            raw_paths.append(rclone_path)
            stripped = rclone_path.lstrip("/")
            if stripped and stripped not in raw_paths:
                raw_paths.append(stripped)
            if allow_empty_fallback and "" not in raw_paths:
                raw_paths.append("")
    else:
        raw_paths.append(rclone_path)
        if allow_empty_fallback and "" not in raw_paths:
            raw_paths.append("")

    errors: List[str] = []
    for cand_path in raw_paths:
        for label, flags in flag_candidates:
            cmd = [
                rclone_bin,
                "lsf",
                f":sftp:{cand_path}",
                *flags,
                "--max-depth",
                "1",
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            except (subprocess.TimeoutExpired, OSError) as exc:
                errors.append(f"{label}({cand_path or '.'}): {exc}")
                continue
            if proc.returncode == 0:
                return cand_path, flags, ""
            errors.append(f"{label}({cand_path or '.'}): {proc.stderr.strip() or proc.stdout.strip()}")

    return rclone_path, None, "; ".join(errors)


def _mount_sftp_directory(
    machine: MachineConfig,
    ep_dict: Dict[str, Any],
    host: str,
    remote_target: str,
    mount_point: Path,
    vol_label: str,
    log_stem: str,
    allow_empty_fallback: bool = True,
) -> Dict[str, Any]:
    """Mount a single remote folder or drive (`remote_target`) onto `mount_point` and verify live entries."""
    mount_point = mount_point.resolve()

    if mount_point.is_dir() and is_mounted(mount_point):
        try:
            visible = [x for x in sorted(os.listdir(mount_point)) if not x.startswith(".")]
            reg = load_mounts_registry()
            reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_target}
            save_mounts_registry(reg)
            return {
                "ok": True,
                "machine_id": machine.id,
                "mount_point": str(mount_point),
                "remote_root": remote_target,
                "already_mounted": True,
                "visible_count": len(visible),
                "sample_entries": visible[:6],
                "endpoint": ep_dict,
            }
        except OSError:
            unmount_sshfs(mount_point)

    is_macos = platform.system().lower() == "darwin"
    rclone_bin = find_rclone_binary()
    sshfs_bin = shutil.which("sshfs")

    if not rclone_bin and not sshfs_bin:
        _cleanup_unmounted_empty_dir(mount_point)
        return {
            "ok": False,
            "machine_id": machine.id,
            "missing_mounter": True,
            "error": "Neither `rclone` nor `sshfs` is installed yet.",
        }

    mount_point.mkdir(parents=True, exist_ok=True)
    try:
        (mount_point / ".DS_Store").unlink(missing_ok=True)
    except OSError:
        pass

    if rclone_bin and (is_macos or not sshfs_bin):
        initial_rclone_path = (
            f"/{remote_target}"
            if (len(remote_target) >= 2 and remote_target[1] == ":")
            else remote_target
        )
        verified_rclone_path, sftp_flags, preflight_err = _select_verified_rclone_sftp_flags(
            rclone_bin,
            machine,
            host,
            initial_rclone_path,
            allow_empty_fallback=allow_empty_fallback,
        )
        if not sftp_flags:
            _cleanup_unmounted_empty_dir(mount_point)
            return {
                "ok": False,
                "machine_id": machine.id,
                "error": f"rclone SFTP preflight failed on {host} ({remote_target}): {preflight_err}",
            }

        subcmd = "nfsmount" if is_macos else "mount"
        log_dir = Path.home() / ".cache" / "remote-fleet"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"rclone-{log_stem}.log"

        cmd = [
            rclone_bin,
            subcmd,
            f":sftp:{verified_rclone_path}",
            str(mount_point),
            *sftp_flags,
            "--allow-non-empty",
            "--vfs-cache-mode",
            "writes",
            "--vfs-fast-fingerprint",
            "--poll-interval",
            "0",
            "--dir-cache-time",
            "5s",
            "--volname",
            vol_label,
        ]

        with open(log_file, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )

        deadline = time.monotonic() + 12.0
        mounted_ok = False
        while time.monotonic() < deadline:
            if is_mounted(mount_point):
                mounted_ok = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.3)

        if mounted_ok:
            visible = []
            try:
                visible = [x for x in sorted(os.listdir(mount_point)) if not x.startswith(".")]
            except OSError:
                pass
            reg = load_mounts_registry()
            reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_target}
            save_mounts_registry(reg)
            return {
                "ok": True,
                "machine_id": machine.id,
                "engine": f"rclone-{subcmd}",
                "mount_point": str(mount_point),
                "remote_root": remote_target,
                "visible_count": len(visible),
                "sample_entries": visible[:6],
                "endpoint": ep_dict,
            }

        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        log_tail = ""
        if log_file.is_file():
            log_tail = "\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-8:])
        if not sshfs_bin:
            _cleanup_unmounted_empty_dir(mount_point)
            return {
                "ok": False,
                "machine_id": machine.id,
                "error": f"rclone {subcmd} did not mount {mount_point}: {log_tail or 'timeout'}",
            }

    if sshfs_bin:
        target_spec = (
            f"{machine.user}@{host}:{remote_target}"
            if machine.user
            else f"{host}:{remote_target}"
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
        try:
            proc2 = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (subprocess.TimeoutExpired, OSError) as exc:
            _cleanup_unmounted_empty_dir(mount_point)
            return {
                "ok": False,
                "machine_id": machine.id,
                "error": f"sshfs failed: {exc}",
            }
        if proc2.returncode == 0 and is_mounted(mount_point):
            visible = [x for x in sorted(os.listdir(mount_point)) if not x.startswith(".")]
            reg = load_mounts_registry()
            reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_target}
            save_mounts_registry(reg)
            return {
                "ok": True,
                "machine_id": machine.id,
                "engine": "sshfs",
                "mount_point": str(mount_point),
                "remote_root": remote_target,
                "visible_count": len(visible),
                "sample_entries": visible[:6],
                "endpoint": ep_dict,
            }
        _cleanup_unmounted_empty_dir(mount_point)
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"sshfs failed: {proc2.stderr.strip() or proc2.stdout.strip()}",
        }

    _cleanup_unmounted_empty_dir(mount_point)
    return {"ok": False, "machine_id": machine.id, "error": "Mount failed."}


def _mount_windows_all_drives(
    machine: MachineConfig,
    ep_dict: Dict[str, Any],
    host: str,
    ping: Dict[str, Any],
    container_dir: Path,
) -> Dict[str, Any]:
    """Mount EVERY drive (`C:/`, `D:/`, `E:/`, ...) on a Windows PC under `~/RemoteComputers/<id>/<Drive>`
    plus a `Home` shortcut pointing straight to `C/Users/<user>`."""
    container_dir = container_dir.resolve()

    # Transition cleanly if `container_dir` (`~/RemoteComputers/pc`) was previously mounted directly to `C:\Users\<user>`
    if is_mounted(container_dir):
        unmount_sshfs(container_dir)
    if is_mounted(container_dir):
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"Could not detach previous single-folder mount at {container_dir}; close any terminal inside {container_dir} and retry.",
        }

    # Remove legacy direct container key from mounts.json so nested `<container>/<Drive>` entries govern path resolution
    reg = load_mounts_registry()
    if str(container_dir) in reg:
        reg.pop(str(container_dir), None)
        save_mounts_registry(reg)

    container_dir.mkdir(parents=True, exist_ok=True)
    remote_home = str(ping.get("home") or "C:/").replace("\\", "/")
    reg = load_mounts_registry()
    reg[str(container_dir)] = {
        "machine_id": machine.id,
        "remote_root": remote_home,
        "container_only": "1",
    }
    save_mounts_registry(reg)

    raw_drives = ping.get("drives") or ["C:/"]
    drive_letters: List[str] = []
    for drv in raw_drives:
        s = str(drv).strip().replace("\\", "/")
        if s and s[0].isalpha():
            letter = s[0].upper()
            if letter not in drive_letters:
                drive_letters.append(letter)
    if not drive_letters:
        drive_letters = ["C"]

    mounted_drives: List[str] = []
    drive_errors: List[str] = []
    total_visible = 0
    any_already = True

    for letter in drive_letters:
        drive_mp = (container_dir / letter).resolve()
        res = _mount_sftp_directory(
            machine=machine,
            ep_dict=ep_dict,
            host=host,
            remote_target=f"{letter}:/",
            mount_point=drive_mp,
            vol_label=f"Fleet-{machine.id}-{letter}",
            log_stem=f"{machine.id}-{letter}",
            allow_empty_fallback=False,
        )
        if res.get("ok"):
            mounted_drives.append(f"{letter}:/")
            total_visible += int(res.get("visible_count") or 0)
            if not res.get("already_mounted"):
                any_already = False
        else:
            drive_errors.append(f"{letter}: {res.get('error')}")

    # Create/refresh `Home` shortcut (`~/RemoteComputers/pc/Home -> C/Users/danli`) for 1-click access to user profile
    home_link = container_dir / "Home"
    home_created = False
    hm = re.match(r"^([A-Za-z]):/(.+)$", remote_home)
    if hm:
        h_letter = hm.group(1).upper()
        h_sub = hm.group(2).strip("/")
        if f"{h_letter}:/" in mounted_drives and h_sub:
            rel_target = f"{h_letter}/{h_sub}"
            try:
                if home_link.is_symlink() or home_link.exists():
                    if home_link.is_symlink() or home_link.is_file():
                        home_link.unlink()
                if not home_link.exists():
                    home_link.symlink_to(rel_target)
                home_created = True
            except OSError:
                pass

    # Clean up any stale unmounted empty drive directories from previously unplugged USB drives
    for child in list(container_dir.iterdir()):
        if child.name == "Home" or child.is_symlink():
            continue
        if child.is_dir() and child.name not in [d[0] for d in mounted_drives]:
            _cleanup_unmounted_empty_dir(child)

    if not mounted_drives:
        # Never delete `container_dir` itself — keep its APFS inode permanent for macOS Sidebar Favorites!
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": "; ".join(drive_errors) or "No Windows drives could be mounted.",
        }

    flat_entries = [d[0] for d in mounted_drives]
    if home_created:
        flat_entries.insert(0, "Home")

    return {
        "ok": True,
        "machine_id": machine.id,
        "engine": "rclone-nfsmount-drives" if platform.system().lower() == "darwin" else "rclone-mount-drives",
        "mount_point": str(container_dir),
        "remote_root": ", ".join(mounted_drives),
        "drives": mounted_drives,
        "already_mounted": any_already,
        "visible_count": total_visible,
        "sample_entries": flat_entries,
        "endpoint": ep_dict,
    }


def _mount_posix_container(
    machine: MachineConfig,
    ep_dict: Dict[str, Any],
    host: str,
    ping: Dict[str, Any],
    container_dir: Path,
) -> Dict[str, Any]:
    """Mount a Linux/POSIX computer inside a permanent local APFS container (`~/RemoteComputers/<id>/Home`, etc.).

    Why a local APFS container folder instead of mounting NFS directly on `~/RemoteComputers/<id>`:
    macOS 15 Sequoia & macOS 26 Tahoe `NSOpenPanel` (`openAndSavePanelService` in Antigravity, Claude, Codex)
    resolves Sidebar Favorites strictly via APFS `Bookmark` `(Volume UUID + FileID)`. Mounting synthetic NFS
    directly onto the Favorited folder masks its APFS FileID and triggers
    `"<name>" can't be opened because the original item can't be found.` (deadlocking Electron sheet modals!).
    Keeping `~/RemoteComputers/<id>` as an unmounted, permanent APFS folder makes Sidebar clicks 100% instant & immortal.
    """
    container_dir = container_dir.resolve()

    # Transition cleanly if `container_dir` (`~/RemoteComputers/spark01`) was previously mounted directly
    if is_mounted(container_dir):
        unmount_sshfs(container_dir)
    if is_mounted(container_dir):
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"Could not detach previous direct mount at {container_dir}; close any terminal inside {container_dir} and retry.",
        }

    container_dir.mkdir(parents=True, exist_ok=True)
    remote_home = str(ping.get("home") or "/").replace("\\", "/")

    reg = load_mounts_registry()
    reg[str(container_dir)] = {
        "machine_id": machine.id,
        "remote_root": remote_home,
        "container_only": "1",
    }
    save_mounts_registry(reg)

    home_mp = (container_dir / "Home").resolve()
    # If `Home` was previously a symlink, remove the symlink before creating mount directory
    if home_mp.is_symlink():
        try:
            home_mp.unlink()
        except OSError:
            pass

    home_res = _mount_sftp_directory(
        machine=machine,
        ep_dict=ep_dict,
        host=host,
        remote_target=remote_home,
        mount_point=home_mp,
        vol_label=f"Fleet-{machine.id}-Home",
        log_stem=f"{machine.id}-Home",
        allow_empty_fallback=True,
    )
    if not home_res.get("ok"):
        return home_res

    sample_entries = ["Home"]

    # Convenience 1-click symlink `Projects -> Home/projects` (or `Workspace -> Home/workspace`) if present on remote
    for label, sub_name in (("Projects", "projects"), ("Workspace", "workspace")):
        link_path = container_dir / label
        try:
            if (home_mp / sub_name).is_dir():
                if link_path.is_symlink() or link_path.is_file():
                    link_path.unlink()
                if not link_path.exists():
                    link_path.symlink_to(f"Home/{sub_name}")
                sample_entries.append(label)
            elif link_path.is_symlink():
                link_path.unlink()
        except OSError:
            pass

    # Also auto-mount any non-empty external/data drives on Linux Sparks right inside `~/RemoteComputers/<id>/<slug>`
    extra_mounted: List[str] = []
    for ext_root in ping.get("extra_roots") or []:
        ext_str = str(ext_root).strip()
        if not ext_str or ext_str == remote_home or ext_str.startswith(remote_home.rstrip("/") + "/"):
            continue
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ext_str.strip("/")).strip("-")
        if not slug or slug in ("Home", "Projects", "Workspace"):
            continue
        ext_mp = (container_dir / slug).resolve()
        ext_res = _mount_sftp_directory(
            machine=machine,
            ep_dict=ep_dict,
            host=host,
            remote_target=ext_str,
            mount_point=ext_mp,
            vol_label=f"Fleet-{machine.id}-{slug}",
            log_stem=f"{machine.id}-{slug}",
            allow_empty_fallback=False,
        )
        if ext_res.get("ok"):
            extra_mounted.append(f"{ext_str} -> {slug}")
            sample_entries.append(slug)

    out: Dict[str, Any] = {
        "ok": True,
        "machine_id": machine.id,
        "engine": home_res.get("engine"),
        "mount_point": str(container_dir),
        "home_mount": str(home_mp),
        "remote_root": remote_home,
        "already_mounted": bool(home_res.get("already_mounted")),
        "visible_count": int(home_res.get("visible_count") or 0),
        "sample_entries": sample_entries,
        "endpoint": ep_dict,
    }
    if extra_mounted:
        out["extra_mounts"] = extra_mounted
    return out


_SWIFT_REPAIR_SIDEBAR_SOURCE = r'''
import Foundation

let args = CommandLine.arguments
guard args.count >= 3 else { exit(0) }
let targetDir = args[1]
let machineIds = Array(args.dropFirst(2))

let home = NSHomeDirectory()
let sflDir = URL(fileURLWithPath: home).appendingPathComponent("Library/Application Support/com.apple.sharedfilelist")
let favCandidates = [
    sflDir.appendingPathComponent("com.apple.LSSharedFileList.FavoriteItems.sfl4"),
    sflDir.appendingPathComponent("com.apple.LSSharedFileList.FavoriteItems.sfl3")
]
let volCandidates = [
    sflDir.appendingPathComponent("com.apple.LSSharedFileList.FavoriteVolumes.sfl4"),
    sflDir.appendingPathComponent("com.apple.LSSharedFileList.FavoriteVolumes.sfl3")
]

let classes: [AnyClass] = [
    NSDictionary.self, NSMutableDictionary.self,
    NSArray.self, NSMutableArray.self,
    NSString.self, NSMutableString.self,
    NSNumber.self, NSData.self, NSMutableData.self,
    NSUUID.self, NSURL.self, NSDate.self
]

var changedAnyFile = false

for sflURL in favCandidates {
    guard FileManager.default.fileExists(atPath: sflURL.path),
          let raw = try? Data(contentsOf: sflURL),
          let unarchived = try? NSKeyedUnarchiver.unarchivedObject(ofClasses: classes, from: raw),
          var rootDict = unarchived as? [String: Any],
          let oldItems = rootDict["items"] as? [[String: Any]]
    else { continue }

    var fileChanged = false
    var newItems: [[String: Any]] = []
    var seenIds = Set<String>()

    for var item in oldItems {
        if let bm = item["Bookmark"] as? Data {
            let vals = URL.resourceValues(forKeys: [.pathKey, .nameKey, .localizedNameKey], fromBookmarkData: bm)
            let bmPath = vals?.path ?? ""
            let bmName = vals?.name ?? vals?.localizedName ?? (bmPath as NSString).lastPathComponent

            if bmName == "spark02" || bmName == "Fleet-spark02" || bmPath.hasSuffix("/RemoteComputers/spark02") {
                fileChanged = true
                continue
            }

            var matchedId: String? = nil
            for mid in machineIds {
                if bmPath == "\(targetDir)/\(mid)" || bmName == mid || bmName == "Fleet-\(mid)" {
                    matchedId = mid
                    break
                }
            }

            if let mid = matchedId {
                if seenIds.contains(mid) {
                    fileChanged = true
                    continue
                }
                seenIds.insert(mid)
                let freshURL = URL(fileURLWithPath: "\(targetDir)/\(mid)", isDirectory: true)
                if FileManager.default.fileExists(atPath: freshURL.path) {
                    var isStale = false
                    let resolved = try? URL(
                        resolvingBookmarkData: bm,
                        options: [.withoutUI, .withoutMounting],
                        relativeTo: nil,
                        bookmarkDataIsStale: &isStale
                    )
                    let resolvedID = (try? resolved?.resourceValues(forKeys: [.fileResourceIdentifierKey]).fileResourceIdentifier) as? NSObject
                    let freshID = (try? freshURL.resourceValues(forKeys: [.fileResourceIdentifierKey]).fileResourceIdentifier) as? NSObject
                    let sameResource = (resolvedID != nil && freshID != nil && resolvedID!.isEqual(freshID!))

                    if resolved == nil || isStale || !sameResource {
                        if let freshBM = try? freshURL.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {
                            item["Bookmark"] = freshBM
                            fileChanged = true
                        }
                    }
                }
            }
        }
        newItems.append(item)
    }

    for mid in machineIds where !seenIds.contains(mid) {
        let freshURL = URL(fileURLWithPath: "\(targetDir)/\(mid)", isDirectory: true)
        if FileManager.default.fileExists(atPath: freshURL.path),
           let freshBM = try? freshURL.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil) {
            newItems.append([
                "visibility": 0,
                "CustomItemProperties": [String: Any](),
                "uuid": UUID().uuidString,
                "Bookmark": freshBM
            ])
            fileChanged = true
        }
    }

    if fileChanged {
        rootDict["items"] = newItems
        if let outData = try? NSKeyedArchiver.archivedData(withRootObject: rootDict, requiringSecureCoding: false) {
            let bakURL = sflURL.appendingPathExtension("remote-fleet.bak")
            if !FileManager.default.fileExists(atPath: bakURL.path) {
                try? raw.write(to: bakURL)
            }
            if (try? outData.write(to: sflURL, options: .atomic)) != nil {
                changedAnyFile = true
            }
        }
    }
}

// Also remove any broken synthetic `Fleet-*` volume ghosts from `FavoriteVolumes.sfl4/.sfl3`
for volURL in volCandidates {
    guard FileManager.default.fileExists(atPath: volURL.path),
          let raw = try? Data(contentsOf: volURL),
          let unarchived = try? NSKeyedUnarchiver.unarchivedObject(ofClasses: classes, from: raw),
          var rootDict = unarchived as? [String: Any],
          let oldItems = rootDict["items"] as? [[String: Any]]
    else { continue }

    var volChanged = false
    var keptItems: [[String: Any]] = []
    for item in oldItems {
        if let bm = item["Bookmark"] as? Data {
            let vals = URL.resourceValues(forKeys: [.pathKey, .nameKey, .localizedNameKey], fromBookmarkData: bm)
            let bmName = vals?.name ?? vals?.localizedName ?? ""
            let bmPath = vals?.path ?? ""
            if bmName.hasPrefix("Fleet-") || bmPath.contains("/RemoteComputers/") {
                var isStale = false
                let resolved = try? URL(
                    resolvingBookmarkData: bm,
                    options: [.withoutUI, .withoutMounting],
                    relativeTo: nil,
                    bookmarkDataIsStale: &isStale
                )
                if resolved == nil || isStale {
                    volChanged = true
                    continue
                }
            }
        }
        keptItems.append(item)
    }
    if volChanged {
        rootDict["items"] = keptItems
        if let outData = try? NSKeyedArchiver.archivedData(withRootObject: rootDict, requiringSecureCoding: false) {
            if (try? outData.write(to: volURL, options: .atomic)) != nil {
                changedAnyFile = true
            }
        }
    }
}

if changedAnyFile {
    print("SIDEBAR_REPAIRED")
} else {
    print("SIDEBAR_OK")
}
'''


def repair_macos_sidebar_favorites(base_dir: Path, machine_ids: List[str]) -> Dict[str, Any]:
    """Repair stale macOS Finder & `NSOpenPanel` Sidebar Favorites (`FavoriteItems.sfl4` / `.sfl3`)
    so `pc`, `spark01`, `spark03`, `spark04` never trigger `'original item can't be found'` in Antigravity/Claude/Codex."""
    if platform.system().lower() != "darwin" or not machine_ids:
        return {"ok": True, "repaired": False, "skipped": True}

    swift_bin = shutil.which("swift") or "/usr/bin/swift"
    if not os.path.exists(swift_bin):
        return {"ok": False, "repaired": False, "error": "swift not found"}

    # Always dismiss any stuck `openAndSavePanelService` sheet modal first if one is hung
    try:
        subprocess.run(
            ["killall", "com.apple.appkit.xpc.openAndSavePanelService"],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    cache_dir = Path.home() / ".cache" / "remote-fleet"
    cache_dir.mkdir(parents=True, exist_ok=True)
    helper_bin = cache_dir / "repair-sidebar-v1"
    swift_file = cache_dir / "repair_sidebar_v1.swift"

    cmd: List[str]
    if helper_bin.is_file() and os.access(helper_bin, os.X_OK):
        cmd = [str(helper_bin), str(base_dir.resolve()), *machine_ids]
    else:
        swift_file.write_text(_SWIFT_REPAIR_SIDEBAR_SOURCE, encoding="utf-8")
        swiftc_bin = shutil.which("swiftc") or "/usr/bin/swiftc"
        compiled = False
        if os.path.exists(swiftc_bin):
            try:
                comp = subprocess.run(
                    [swiftc_bin, "-O", str(swift_file), "-o", str(helper_bin)],
                    capture_output=True,
                    text=True,
                    timeout=25,
                    check=False,
                )
                compiled = comp.returncode == 0 and helper_bin.is_file()
            except (subprocess.TimeoutExpired, OSError):
                compiled = False
        if compiled:
            cmd = [str(helper_bin), str(base_dir.resolve()), *machine_ids]
        else:
            cmd = [swift_bin, str(swift_file), str(base_dir.resolve()), *machine_ids]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "repaired": False, "error": str(exc)}

    repaired = "SIDEBAR_REPAIRED" in proc.stdout
    if repaired:
        # SIGKILL `sharedfilelistd` so it reloads our newly written `.sfl4`/`.sfl3` from disk without overwriting on exit
        for kill_cmd in (
            ["killall", "-9", "sharedfilelistd"],
            ["killall", "com.apple.appkit.xpc.openAndSavePanelService"],
        ):
            try:
                subprocess.run(kill_cmd, capture_output=True, timeout=3, check=False)
            except (subprocess.TimeoutExpired, OSError):
                pass
        time.sleep(0.35)
        try:
            subprocess.run(["killall", "Finder"], capture_output=True, timeout=4, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return {"ok": proc.returncode == 0, "repaired": repaired, "stdout": proc.stdout.strip()}


def mount_single_machine_root(
    transport: FleetTransport,
    machine: MachineConfig,
    base_dir: Path,
) -> Dict[str, Any]:
    """Mount a remote computer inside permanent local APFS container `~/RemoteComputers/<machine.id>/`."""
    mount_point = (base_dir / machine.id).resolve()

    ep = transport.resolve_endpoint(machine)
    if ep.transport == "local":
        return {"ok": True, "skipped": True, "reason": "self (local computer)", "machine_id": machine.id}

    # Ensure any legacy direct mount on `mount_point` is detached and keep `mount_point` permanent on APFS!
    if is_mounted(mount_point):
        unmount_sshfs(mount_point)
    if not is_mounted(mount_point):
        mount_point.mkdir(parents=True, exist_ok=True)

    if not ep.reachable:
        return {"ok": False, "machine_id": machine.id, "error": f"Unreachable ({ep.error})"}

    ping = transport.run_worker_op(machine, {"op": "ping"}, timeout_sec=10)
    if not ping.get("ok"):
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"SSH auth failed on {ep.host}: {ping.get('error')}",
        }

    if machine.os == "windows" or str(ping.get("os") or "").lower() == "windows":
        machine.os = "windows"
        return _mount_windows_all_drives(machine, ep.to_dict(), ep.host, ping, mount_point)

    return _mount_posix_container(machine, ep.to_dict(), ep.host, ping, mount_point)


def mount_all_machines(
    config: FleetConfig,
    transport: FleetTransport,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Mount all reachable remote fleet computers under `~/RemoteComputers/<id>/` and repair macOS Sidebar Favorites."""
    root = (base_dir or (Path.home() / "RemoteComputers")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    transport.resolve_all_endpoints(config.machines, force_refresh=True)

    results = []
    remote_ids: List[str] = []
    for m in config.machines:
        res = mount_single_machine_root(transport, m, root)
        results.append(res)
        if not res.get("skipped"):
            remote_ids.append(m.id)

    sidebar_info: Dict[str, Any] = {"repaired": False}
    if platform.system().lower() == "darwin" and remote_ids:
        sidebar_info = repair_macos_sidebar_favorites(root, remote_ids)

    # Refresh macOS Finder windows (`~/RemoteComputers` and `~/RemoteComputers/<id>`) so contents update live
    if platform.system().lower() == "darwin" and shutil.which("osascript") and not sidebar_info.get("repaired"):
        refresh_targets = [root]
        for mid in remote_ids:
            cand = root / mid
            if cand.is_dir():
                refresh_targets.append(cand)
        script_lines = [
            f'tell application "Finder" to if exists (POSIX file "{p}" as alias) then update (POSIX file "{p}" as alias)'
            for p in refresh_targets
        ]
        try:
            subprocess.run(
                ["osascript", "-e", "\n".join(script_lines)],
                capture_output=True,
                timeout=4,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    return {
        "base_dir": str(root),
        "results": results,
        "sidebar_repaired": bool(sidebar_info.get("repaired")),
    }


def unmount_all_machines(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    root = (base_dir or (Path.home() / "RemoteComputers")).expanduser().resolve()
    results = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            if is_mounted(child):
                results.append({"path": str(child), **unmount_sshfs(child)})
            else:
                # Unmount nested multi-drive mounts (e.g. `~/RemoteComputers/pc/C`, `~/RemoteComputers/pc/D`)
                for sub in sorted(child.iterdir()):
                    if not sub.is_symlink() and sub.is_dir() and is_mounted(sub):
                        results.append({"path": str(sub), **unmount_sshfs(sub)})
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
