"""macOS / OS-level integration: `~/.ssh/config` generator and `~/RemoteComputers` verified live mounter."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

from remote_fleet.config import (
    FleetConfig,
    MachineConfig,
    TailscaleNodeInfo,
    _parse_wsl_drive_spec,
    format_wsl_path,
    is_local_tailscale_running,
    is_self_machine,
    is_tailscale_only_host,
    load_mounts_registry,
    parse_wsl_path,
    save_mounts_registry,
)
from remote_fleet.mount import (
    get_macos_mount_table,
    get_repo_root,
    is_mounted,
    normalize_mount_path,
    safe_lexical_resolve,
    unmount_sshfs,
)
from remote_fleet.transport import FleetTransport


SSH_BEGIN_MARKER = "# >>> remote-fleet-ssh >>>"
SSH_END_MARKER = "# <<< remote-fleet-ssh <<<"

LAUNCH_AGENT_LABEL = "com.remote-fleet.automount"
LOGIN_APP_NAME = "Remote Fleet Mount"
LOGIN_APP_BUNDLE_ID = "com.remote-fleet.login-mount"
LOGIN_APP_EXECUTABLE = "RemoteFleetMount"
LOGIN_APP_VERSION = "v1"
LOGIN_FALLBACK_AGENT_LABEL = "com.remote-fleet.login-mount"


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
    """Build `--sftp-ssh` string so rclone uses `/usr/bin/ssh` directly with keepalives."""
    parts = [
        shutil.which("ssh") or "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "TCPKeepAlive=yes",
    ]
    if machine.port and machine.port != 22:
        parts.extend(["-p", str(machine.port)])
    if machine.ssh_identity_file:
        parts.extend(["-i", os.path.expanduser(machine.ssh_identity_file)])
    target = f"{machine.user}@{host}" if machine.user else host
    parts.append(target)
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


_RCLONE_FLAGS_CACHE: Dict[str, str] = {}


def _rclone_supports_flag(rclone_bin: str, flag: str) -> bool:
    """Check once per process whether `rclone_bin` supports a global CLI flag (e.g. `--max-connections`)."""
    if rclone_bin not in _RCLONE_FLAGS_CACHE:
        try:
            proc = subprocess.run(
                [rclone_bin, "help", "flags"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            _RCLONE_FLAGS_CACHE[rclone_bin] = proc.stdout if proc.returncode == 0 else ""
        except (subprocess.TimeoutExpired, OSError):
            _RCLONE_FLAGS_CACHE[rclone_bin] = ""
    return flag in _RCLONE_FLAGS_CACHE[rclone_bin]


def _ensure_spotlight_never_index(directory: Path) -> None:
    """Drop an empty `.metadata_never_index` marker inside a local APFS container folder on macOS.

    Stops Spotlight (`mds`/`mdworker`) from crawling remote NFS mounts under `~/RemoteComputers/`
    (especially when symlinked onto an external SSD such as `/Volumes/X10 Pro/...`), which otherwise
    wakes parked Windows HDDs and triggers spurious macOS 'Server connections interrupted' alerts.
    Never writes into an already-mounted NFS filesystem.
    """
    if platform.system().lower() != "darwin":
        return
    if is_mounted(directory) or not directory.is_dir():
        return
    marker = directory / ".metadata_never_index"
    if not marker.exists():
        try:
            marker.touch(exist_ok=True)
        except OSError:
            pass


def _cleanup_unmounted_empty_dir(mount_point: Path) -> None:
    """Remove an unmounted folder that only contains `.DS_Store`/`.metadata_never_index` or nothing
    so Finder never shows dead empty folders."""
    if is_mounted(mount_point) or not mount_point.is_dir():
        return
    ignorable = {".DS_Store", ".metadata_never_index"}
    try:
        items = [p for p in mount_point.iterdir() if p.name not in ignorable]
        if not items:
            for meta_name in ignorable:
                mf = mount_point / meta_name
                if mf.is_file():
                    mf.unlink()
            mount_point.rmdir()
    except OSError:
        pass


def _select_verified_rclone_sftp_flags(
    rclone_bin: str,
    machine: MachineConfig,
    host: str,
    rclone_path: str,
    allow_empty_fallback: bool = True,
    sftp_server_command: str = "",
    treat_as_posix: bool = False,
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
    if machine.os == "windows" and not treat_as_posix:
        # Skip legacy NTFS junction traps in C:\Users\<user> and C:\ (Cookies, My Documents, Documents and Settings, etc.)
        common_sftp_opts.append("--sftp-skip-links")
    if sftp_server_command:
        # Official rclone `--sftp-server-command` launches a custom SFTP server over SSH (e.g. WSL's native Linux sftp-server).
        # Note: rclone ignores `--sftp-server-command` whenever `--sftp-ssh` is set, so we strictly use rclone's native SSH client.
        common_sftp_opts.extend(["--sftp-server-command", sftp_server_command])

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

    if sftp_server_command:
        agent_flags = [
            "--sftp-host",
            host,
            "--sftp-port",
            str(machine.port or 22),
            "--sftp-key-use-agent",
            *common_sftp_opts,
        ]
        if machine.user:
            agent_flags.extend(["--sftp-user", machine.user])
        flag_candidates.append(("native-agent", agent_flags))
    else:
        ssh_ext = build_rclone_sftp_ssh_command(machine, host)
        ext_flags = [
            "--sftp-ssh",
            ssh_ext,
            *common_sftp_opts,
        ]
        flag_candidates.append(("external-ssh", ext_flags))

    raw_paths: List[str] = []
    if machine.os == "windows" and not treat_as_posix:
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

    preflight_timeout = 15 if sftp_server_command else 10
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
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=preflight_timeout, check=False)
            except (subprocess.TimeoutExpired, OSError) as exc:
                errors.append(f"{label}({cand_path or '.'}): {exc}")
                continue
            if proc.returncode == 0:
                return cand_path, flags, ""
            errors.append(f"{label}({cand_path or '.'}): {proc.stderr.strip() or proc.stdout.strip()}")

    return rclone_path, None, "; ".join(errors)


def _daemons_dir() -> Path:
    d = Path.home() / ".cache" / "remote-fleet" / "daemons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_tcp_ready(port: int, proc: subprocess.Popen[Any], timeout_sec: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# Schema v3: IPv4 127.0.0.1:/ + vers=3,proto=tcp,deadtimeout=0,mutejukebox,noquota,noatime,acreg*/acdir*,
# tighter `--sftp-idle-timeout 5m` (prevents stale idle TCP sockets), longer `--dir-cache-time` (prevents
# waking parked HDDs), and Spotlight `.metadata_never_index` containers.
DAEMON_SCHEMA_VERSION = 3


def _probe_mount_health(
    mount_point: Path,
    log_stem: str,
    timeout_sec: float = 4.0,
    expected_host: str = "",
) -> str:
    """Uncached NFS liveness probe using a random nonexistent filename (`ENOENT` = healthy).

    Never rely on `os.listdir(mount_point)` or call `mount_point.is_dir()` on the main thread:
    macOS NFS caches directory listings even after the server or SSH socket dies, and `stat` on a
    legacy hard mount blocks without a thread deadline. Checking `is_mounted` (`MNT_NOWAIT`) +
    daemon PID first, then `os.lstat('.fleet-probe-<uuid>')` on a daemon thread, forces a live LOOKUP RPC safely.
    """
    if not is_mounted(mount_point):
        return "gone"

    if platform.system().lower() == "darwin":
        meta_file = _daemons_dir() / f"{log_stem}.json"
        if not meta_file.is_file():
            # Mounted by legacy `rclone nfsmount` (hard mount) — upgrade cleanly to soft `serve nfs`!
            return "legacy_nfsmount"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if int(meta.get("schema") or 0) < DAEMON_SCHEMA_VERSION:
                return "outdated_daemon"
            meta_host = str(meta.get("host") or "")
            if (
                expected_host
                and meta_host
                and meta_host != expected_host
                and is_tailscale_only_host(meta_host)
                and not is_tailscale_only_host(expected_host)
            ):
                # Upgrade from Tailscale back to fast Home LAN once LAN is reachable again.
                # For LAN -> Tailscale or LAN -> LAN alias differences, let the live uncached `lstat`
                # probe below decide so a background `launchd` LAN probe blip / Local Network TCC block
                # never tears down an actively healthy LAN mount!
                return "endpoint_changed"
            pid = int(meta.get("pid") or 0)
            if pid <= 0:
                return "dead_daemon"
            os.kill(pid, 0)
        except Exception:
            return "dead_daemon"

    probe_target = str(mount_point / f".fleet-probe-{uuid.uuid4().hex[:12]}")
    outcome: Dict[str, int] = {}

    def _stat_worker() -> None:
        try:
            os.lstat(probe_target)
            outcome["errno"] = 0
        except OSError as exc:
            outcome["errno"] = int(exc.errno or errno.EIO)

    t = threading.Thread(target=_stat_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive() or "errno" not in outcome:
        return "unresponsive"

    code = outcome["errno"]
    if code in (0, errno.ENOENT):
        return "healthy"
    dead_codes = {
        errno.ETIMEDOUT,
        errno.ESTALE,
        errno.EIO,
        errno.ENOTCONN,
        errno.ECONNREFUSED,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENXIO,
    }
    return "unresponsive" if code in dead_codes else "healthy"


def _safe_kill_rclone_daemon_pid(pid: int) -> None:
    """Terminate `pid` ONLY if it is NOT an unrelated recycled process after a reboot.

    After a Mac reboot, PIDs saved in `~/.cache/remote-fleet/daemons/*.json` can be reassigned
    by macOS to unrelated user apps (e.g. Finder, Dock, Antigravity, Tailscale). Checking
    `ps -p <pid> -o command=` guarantees we never send `SIGTERM` to a non-rclone process.
    """
    if pid <= 0 or pid == os.getpid():
        return
    ps_bin = "/bin/ps" if os.path.exists("/bin/ps") else shutil.which("ps")
    if ps_bin:
        try:
            chk = subprocess.run(
                [ps_bin, "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if chk.returncode == 0:
                cmdline = chk.stdout.strip().lower()
                if cmdline and ("rclone" not in cmdline or "serve nfs" not in cmdline):
                    return
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _teardown_mount_and_daemon(mount_point: Path, log_stem: str) -> None:
    """Unmount FIRST, then terminate the backing `rclone serve nfs` daemon (never kill before unmount!)."""
    if is_mounted(mount_point):
        unmount_sshfs(mount_point)

    meta_file = _daemons_dir() / f"{log_stem}.json"
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            pid = int(meta.get("pid") or 0)
            if pid > 0:
                _safe_kill_rclone_daemon_pid(pid)
        except Exception:
            pass
        try:
            meta_file.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_sample_entries(mount_point: Path, timeout_sec: float = 2.5) -> List[str]:
    out: Dict[str, List[str]] = {}

    def _list_worker() -> None:
        try:
            out["items"] = [x for x in sorted(os.listdir(mount_point)) if not x.startswith(".")]
        except OSError:
            out["items"] = []

    t = threading.Thread(target=_list_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    return out.get("items") or []


def _mount_sftp_directory(
    machine: MachineConfig,
    ep_dict: Dict[str, Any],
    host: str,
    remote_target: str,
    mount_point: Path,
    vol_label: str,
    log_stem: str,
    allow_empty_fallback: bool = True,
    preverified_sftp: Optional[Tuple[str, List[str]]] = None,
    sftp_server_command: str = "",
    treat_as_posix: bool = False,
    rclone_remote_path: Optional[str] = None,
    registry_extra: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Mount a single remote folder, Windows drive, or WSL root (`remote_target`) onto `mount_point`
    with hardened macOS soft-NFS options and self-healing."""
    _ = vol_label
    mount_point = safe_lexical_resolve(mount_point)

    reg_entry: Dict[str, str] = {"machine_id": machine.id, "remote_root": remote_target}
    if registry_extra:
        for rk, rv in registry_extra.items():
            if rv:
                reg_entry[str(rk)] = str(rv)

    health = _probe_mount_health(mount_point, log_stem, timeout_sec=12.0, expected_host=host)
    if health == "healthy":
        visible = _safe_sample_entries(mount_point, timeout_sec=2.5)
        reg = load_mounts_registry()
        reg[str(mount_point)] = dict(reg_entry)
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

    # Tear down any dead, wedged, or legacy hard `nfsmount` cleanly BEFORE remounting
    _teardown_mount_and_daemon(mount_point, log_stem)

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

    try:
        mount_point.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        is_perm = isinstance(exc, PermissionError) or exc.errno in (errno.EPERM, errno.EACCES)
        return {
            "ok": False,
            "machine_id": machine.id,
            "permission_denied": is_perm,
            "error": f"Cannot access mount folder {mount_point}: {exc}",
        }
    for junk_name in (".DS_Store", ".metadata_never_index"):
        try:
            (mount_point / junk_name).unlink(missing_ok=True)
        except OSError:
            pass

    cache_root = Path.home() / ".cache" / "remote-fleet"
    cache_root.mkdir(parents=True, exist_ok=True)

    effective_rclone_path = rclone_remote_path if rclone_remote_path is not None else remote_target
    initial_rclone_path = (
        f"/{effective_rclone_path}"
        if (not treat_as_posix and len(effective_rclone_path) >= 2 and effective_rclone_path[1] == ":")
        else effective_rclone_path
    )

    # 1. macOS preferred path: `rclone serve nfs` on IPv4 loopback + hardened `/sbin/mount -t nfs`
    if rclone_bin and is_macos and os.path.exists("/sbin/mount"):
        if preverified_sftp is not None:
            verified_rclone_path, sftp_flags = preverified_sftp[0], list(preverified_sftp[1])
            preflight_err = ""
        else:
            verified_rclone_path, sftp_flags, preflight_err = _select_verified_rclone_sftp_flags(
                rclone_bin,
                machine,
                host,
                initial_rclone_path,
                allow_empty_fallback=allow_empty_fallback,
                sftp_server_command=sftp_server_command,
                treat_as_posix=treat_as_posix,
            )
        if not sftp_flags:
            _cleanup_unmounted_empty_dir(mount_point)
            return {
                "ok": False,
                "machine_id": machine.id,
                "error": f"rclone SFTP preflight failed on {host} ({remote_target}): {preflight_err}",
            }

        # Never pass `--max-connections 1` (deadlocks rclone VFS on nested Finder READDIRPLUS + .DS_Store calls!).
        # Secondary Windows drives (D..Z, often parked USB/SATA HDDs) use a longer dir-cache so background
        # Finder metadata reads do not repeatedly spin up disks and trigger macOS's 5-12s disconnect warning.
        is_secondary_win_drive = (
            machine.os == "windows"
            and not treat_as_posix
            and len(remote_target) >= 2
            and remote_target[1] == ":"
            and remote_target[0].upper() != "C"
        )
        dir_cache_time = "15m" if is_secondary_win_drive else "5m"
        permission_denied = False
        probe_timeout = 25.0 if sftp_server_command else (20.0 if machine.os == "windows" else 15.0)
        for _attempt in range(2 if preverified_sftp is not None else 1):
            nfs_port = _allocate_loopback_port()
            nfs_cache_dir = cache_root / "nfs-handles" / log_stem
            vfs_cache_dir = cache_root / "vfs-cache" / log_stem
            nfs_cache_dir.mkdir(parents=True, exist_ok=True)
            vfs_cache_dir.mkdir(parents=True, exist_ok=True)
            log_file = cache_root / f"rclone-{log_stem}.log"

            serve_cmd = [
                rclone_bin,
                "serve",
                "nfs",
                f":sftp:{verified_rclone_path}",
                *sftp_flags,
                "--addr",
                f"127.0.0.1:{nfs_port}",
                "--nfs-cache-type",
                "disk",
                "--nfs-cache-dir",
                str(nfs_cache_dir),
                "--cache-dir",
                str(vfs_cache_dir),
                "--vfs-cache-mode",
                "writes",
                "--vfs-cache-max-size",
                "256M",
                "--vfs-cache-max-age",
                "15m",
                "--vfs-fast-fingerprint",
                "--dir-cache-time",
                dir_cache_time,
                "--poll-interval",
                "0",
                "--sftp-idle-timeout",
                "5m",
                "--contimeout",
                "5s",
                "--timeout",
                "15s",
                "--low-level-retries",
                "3",
                "--retries",
                "3",
                "--checkers",
                "2",
                "--transfers",
                "2",
            ]

            with open(log_file, "w", encoding="utf-8") as lf:
                proc = subprocess.Popen(
                    serve_cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )

            mounted_ok = False
            mount_err_detail = ""
            if _wait_tcp_ready(nfs_port, proc, timeout_sec=10.0):
                # Hardened IPv4 TCP NFSv3 mount:
                # - 127.0.0.1:/ + vers=3,proto=tcp avoids IPv6 ::1 / mDNS delays & v4 negotiation.
                # - soft,intr,deadtimeout=0,mutejukebox prevents Finder wedges, auto-force-unmounts, and jukebox popups.
                # - noquota,noatime + acreg*/acdir* avoids redundant RQUOTA/atime/GETATTR RPC storms on idle mounts.
                # - Never pass `nobrowse`: Finder/NSOpenPanel hides `nobrowse` child mountpoints inside containers.
                nfs_opts = (
                    f"vers=3,proto=tcp,port={nfs_port},mountport={nfs_port},"
                    "soft,intr,timeo=600,retrans=2,deadtimeout=0,mutejukebox,"
                    "nolocks,locallocks,noquota,noatime,nfc,"
                    "acregmin=5,acregmax=60,acdirmin=15,acdirmax=120,"
                    "rsize=131072,wsize=131072"
                )
                try:
                    mnt_proc = subprocess.run(
                        ["/sbin/mount", "-t", "nfs", "-o", nfs_opts, "127.0.0.1:/", str(mount_point)],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    if mnt_proc.returncode == 0 and is_mounted(mount_point):
                        mounted_ok = True
                    else:
                        mount_err_detail = mnt_proc.stderr.strip() or mnt_proc.stdout.strip()
                except (subprocess.TimeoutExpired, OSError) as exc:
                    mount_err_detail = str(exc)

            if mounted_ok:
                recorded_mp = str(mount_point)
                table_now = get_macos_mount_table()
                if table_now:
                    mp_norm = normalize_mount_path(recorded_mp)
                    for tbl_key in table_now.keys():
                        if normalize_mount_path(tbl_key) == mp_norm:
                            recorded_mp = tbl_key
                            break
                meta_file = _daemons_dir() / f"{log_stem}.json"
                meta_payload: Dict[str, Any] = {
                    "schema": DAEMON_SCHEMA_VERSION,
                    "pid": proc.pid,
                    "port": nfs_port,
                    "host": host,
                    "machine_id": machine.id,
                    "mount_point": recorded_mp,
                    "remote_target": remote_target,
                }
                if registry_extra and registry_extra.get("wsl_distro"):
                    meta_payload["wsl_distro"] = registry_extra["wsl_distro"]
                meta_file.write_text(
                    json.dumps(meta_payload, indent=2),
                    encoding="utf-8",
                )
                if _probe_mount_health(mount_point, log_stem, timeout_sec=probe_timeout, expected_host=host) == "healthy":
                    visible = _safe_sample_entries(mount_point, timeout_sec=4.0)
                    reg = load_mounts_registry()
                    reg[recorded_mp] = dict(reg_entry)
                    save_mounts_registry(reg)
                    return {
                        "ok": True,
                        "machine_id": machine.id,
                        "engine": "rclone-serve-nfs-soft",
                        "mount_point": str(mount_point),
                        "remote_root": remote_target,
                        "visible_count": len(visible),
                        "sample_entries": visible[:6],
                        "endpoint": ep_dict,
                        "verified_sftp_flags": sftp_flags,
                        "verified_rclone_path": verified_rclone_path,
                    }
                _teardown_mount_and_daemon(mount_point, log_stem)

            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

            err_low = mount_err_detail.lower()
            if "operation not permitted" in err_low or "permission denied" in err_low:
                permission_denied = True
                break

            if _attempt == 0 and preverified_sftp is not None:
                verified_rclone_path, fallback_flags, preflight_err = _select_verified_rclone_sftp_flags(
                    rclone_bin,
                    machine,
                    host,
                    initial_rclone_path,
                    allow_empty_fallback=allow_empty_fallback,
                    sftp_server_command=sftp_server_command,
                    treat_as_posix=treat_as_posix,
                )
                if not fallback_flags:
                    mount_err_detail = preflight_err
                    break
                sftp_flags = fallback_flags

        log_tail = ""
        log_file = cache_root / f"rclone-{log_stem}.log"
        if log_file.is_file():
            log_tail = "\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-8:])
        if not sshfs_bin or permission_denied:
            if not permission_denied:
                _cleanup_unmounted_empty_dir(mount_point)
            return {
                "ok": False,
                "machine_id": machine.id,
                "permission_denied": permission_denied,
                "error": f"rclone serve nfs mount failed on {mount_point}: {mount_err_detail or log_tail or 'timeout'}",
            }

    # 2. Linux / non-macOS rclone FUSE fallback
    if rclone_bin and not is_macos and not sshfs_bin:
        verified_rclone_path, sftp_flags, preflight_err = _select_verified_rclone_sftp_flags(
            rclone_bin,
            machine,
            host,
            initial_rclone_path,
            allow_empty_fallback=allow_empty_fallback,
            sftp_server_command=sftp_server_command,
            treat_as_posix=treat_as_posix,
        )
        if sftp_flags:
            log_file = cache_root / f"rclone-{log_stem}.log"
            cmd = [
                rclone_bin,
                "mount",
                f":sftp:{verified_rclone_path}",
                str(mount_point),
                *sftp_flags,
                "--allow-non-empty",
                "--vfs-cache-mode",
                "writes",
                "--vfs-cache-max-size",
                "256M",
                "--vfs-cache-max-age",
                "15m",
                "--vfs-fast-fingerprint",
                "--poll-interval",
                "0",
                "--dir-cache-time",
                "30s",
                "--sftp-idle-timeout",
                "5m",
            ]
            with open(log_file, "w", encoding="utf-8") as lf:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True
                )
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if is_mounted(mount_point):
                    visible = _safe_sample_entries(mount_point, timeout_sec=2.0)
                    reg = load_mounts_registry()
                    reg[str(mount_point)] = dict(reg_entry)
                    save_mounts_registry(reg)
                    return {
                        "ok": True,
                        "machine_id": machine.id,
                        "engine": "rclone-mount",
                        "mount_point": str(mount_point),
                        "remote_root": remote_target,
                        "visible_count": len(visible),
                        "sample_entries": visible[:6],
                        "endpoint": ep_dict,
                    }
                if proc.poll() is not None:
                    break
                time.sleep(0.25)

    # 3. `sshfs` fallback
    if sshfs_bin:
        target_spec = (
            f"{machine.user}@{host}:{effective_rclone_path}"
            if machine.user
            else f"{host}:{effective_rclone_path}"
        )
        cmd = [
            sshfs_bin,
            target_spec,
            str(mount_point),
            "-o",
            "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3",
        ]
        if sftp_server_command:
            cmd.extend(["-o", f"sftp_server={sftp_server_command}"])
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
            visible = _safe_sample_entries(mount_point, timeout_sec=2.0)
            reg = load_mounts_registry()
            reg[str(mount_point)] = dict(reg_entry)
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


def _prune_stale_registry_entries(machine_id: str, container_dir: Path) -> Dict[str, Dict[str, str]]:
    """Remove stale `mounts.json` keys for `machine_id` outside `container_dir` (e.g. pre-symlink paths)."""
    reg = load_mounts_registry()
    c_norm = normalize_mount_path(str(container_dir))
    stale_keys = [
        key
        for key, info in reg.items()
        if str(info.get("machine_id") or "") == machine_id
        and normalize_mount_path(key) != c_norm
        and not normalize_mount_path(key).startswith(c_norm + "/")
    ]
    if stale_keys:
        for key in stale_keys:
            reg.pop(key, None)
        save_mounts_registry(reg)
    return reg


def _mount_windows_all_drives(
    machine: MachineConfig,
    ep_dict: Dict[str, Any],
    host: str,
    ping: Dict[str, Any],
    container_dir: Path,
) -> Dict[str, Any]:
    """Mount EVERY drive (`C:/`, `D:/`, `E:/`, ...) AND any discovered WSL drives/distros (e.g. `Y:` -> WSL)
    on a Windows PC under `~/RemoteComputers/<id>/<Drive>`, plus `Home` (`C/Users/<user>`) and `WSL` shortcuts."""
    container_dir = safe_lexical_resolve(container_dir)

    # Transition cleanly if `container_dir` (`~/RemoteComputers/pc`) was previously mounted directly to `C:\Users\<user>`
    if is_mounted(container_dir):
        unmount_sshfs(container_dir)
    if is_mounted(container_dir):
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"Could not detach previous single-folder mount at {container_dir}; close any terminal inside {container_dir} and retry.",
        }

    # Prune stale pre-symlink keys and record container entry so nested `<container>/<Drive>` entries govern path resolution
    try:
        container_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "machine_id": machine.id,
            "permission_denied": isinstance(exc, PermissionError) or exc.errno in (errno.EPERM, errno.EACCES),
            "error": f"Cannot access {container_dir}: {exc}",
        }
    _ensure_spotlight_never_index(container_dir)
    remote_home = str(ping.get("home") or "C:/").replace("\\", "/")
    reg = _prune_stale_registry_entries(machine.id, container_dir)
    reg[str(container_dir)] = {
        "machine_id": machine.id,
        "remote_root": remote_home,
        "container_only": "1",
    }
    save_mounts_registry(reg)

    wsl_targets: List[Dict[str, Any]] = [
        t
        for t in (ping.get("wsl_targets") or [])
        if isinstance(t, dict) and str(t.get("distro") or "").strip() and str(t.get("mount_name") or "").strip()
    ]
    wsl_claimed_letters: Set[str] = {
        str(t.get("drive_letter") or "").strip().upper()
        for t in wsl_targets
        if str(t.get("drive_letter") or "").strip()
    }

    raw_drives = ping.get("drives") or ["C:/"]
    drive_letters: List[str] = []
    for drv in raw_drives:
        s = str(drv).strip().replace("\\", "/")
        if s and s[0].isalpha():
            letter = s[0].upper()
            if letter not in drive_letters and letter not in wsl_claimed_letters:
                drive_letters.append(letter)
    if not drive_letters and not wsl_targets:
        drive_letters = ["C"]

    mounted_drives: List[str] = []
    mounted_wsl: List[Dict[str, str]] = []
    drive_errors: Dict[str, str] = {}
    wsl_errors: Dict[str, str] = {}
    failed_letters: List[str] = []
    failed_wsl_targets: List[Dict[str, Any]] = []
    total_visible = 0
    any_already = True
    cached_sftp_flags: Optional[List[str]] = None
    use_leading_slash = True
    new_mount_attempts = 0
    aborted_permission = False

    for letter in drive_letters:
        drive_mp = safe_lexical_resolve(container_dir / letter)
        extra_kwargs: Dict[str, Any] = {}
        if cached_sftp_flags is not None:
            cand_rclone_path = f"/{letter}:/" if use_leading_slash else f"{letter}:/"
            extra_kwargs["preverified_sftp"] = (cand_rclone_path, cached_sftp_flags)
            # Space out newly launched Windows SFTP mounts so we never burst past Windows sshd MaxStartups (10)
            if new_mount_attempts > 0:
                time.sleep(0.4)

        res = _mount_sftp_directory(
            machine=machine,
            ep_dict=ep_dict,
            host=host,
            remote_target=f"{letter}:/",
            mount_point=drive_mp,
            vol_label=f"Fleet-{machine.id}-{letter}",
            log_stem=f"{machine.id}-{letter}",
            allow_empty_fallback=False,
            **extra_kwargs,
        )
        if res.get("ok"):
            mounted_drives.append(f"{letter}:/")
            total_visible += int(res.get("visible_count") or 0)
            if not res.get("already_mounted"):
                any_already = False
                new_mount_attempts += 1
                if cached_sftp_flags is None and res.get("verified_sftp_flags"):
                    cached_sftp_flags = list(res["verified_sftp_flags"])
                    vpath = str(res.get("verified_rclone_path") or f"/{letter}:/")
                    use_leading_slash = vpath.startswith("/")
        else:
            new_mount_attempts += 1
            drive_errors[letter] = f"{letter}: {res.get('error')}"
            failed_letters.append(letter)
            if res.get("permission_denied"):
                aborted_permission = True
                break

    has_explicit_wsl = any(
        bool(t.get("explicit") or t.get("drive_letter")) for t in wsl_targets
    )

    def _attempt_wsl_mount(target: Dict[str, Any]) -> Tuple[bool, bool, str]:
        nonlocal total_visible, any_already, new_mount_attempts
        mname = str(target["mount_name"]).strip()
        distro = str(target["distro"]).strip()
        wsl_root = str(target.get("wsl_root") or "/").strip() or "/"
        remote_uri = str(target.get("remote_uri") or format_wsl_path(distro, wsl_root))
        drv_letter = str(target.get("drive_letter") or "").strip().upper()
        sftp_cmd = str(target.get("sftp_server_command") or "").strip()
        is_explicit = bool(target.get("explicit") or drv_letter)
        label_key = f"{drv_letter}:" if drv_letter else mname

        if not sftp_cmd:
            # Don't spam warnings for old/unused unmapped WSL distros when a mapped WSL drive (e.g. Y:) is already present
            if not is_explicit and has_explicit_wsl:
                return False, False, label_key
            err_msg = str(
                target.get("probe_error")
                or (
                    f"WSL distro '{distro}' is missing sftp-server. "
                    "Run once in WSL: sudo apt update && sudo apt install -y openssh-sftp-server"
                )
            )
            wsl_errors[label_key] = f"{label_key} (WSL {distro}): {err_msg}"
            return False, False, label_key

        wsl_mp = safe_lexical_resolve(container_dir / mname)
        if not is_mounted(wsl_mp) and wsl_mp.is_symlink():
            try:
                wsl_mp.unlink()
            except OSError:
                pass

        if new_mount_attempts > 0:
            time.sleep(0.4)

        reg_extra: Dict[str, str] = {"wsl_distro": distro, "wsl_root": wsl_root}
        if drv_letter:
            reg_extra["windows_drive"] = f"{drv_letter}:"

        res_wsl = _mount_sftp_directory(
            machine=machine,
            ep_dict=ep_dict,
            host=host,
            remote_target=remote_uri,
            mount_point=wsl_mp,
            vol_label=f"Fleet-{machine.id}-{mname}",
            log_stem=f"{machine.id}-{mname}",
            allow_empty_fallback=False,
            sftp_server_command=sftp_cmd,
            treat_as_posix=True,
            rclone_remote_path=wsl_root,
            registry_extra=reg_extra,
        )
        if res_wsl.get("ok"):
            mounted_wsl.append(
                {
                    "mount_name": mname,
                    "drive_letter": drv_letter,
                    "distro": distro,
                    "wsl_root": wsl_root,
                    "remote_root": remote_uri,
                    "mount_point": str(wsl_mp),
                }
            )
            total_visible += int(res_wsl.get("visible_count") or 0)
            if not res_wsl.get("already_mounted"):
                any_already = False
                new_mount_attempts += 1
            wsl_errors.pop(label_key, None)
            return True, False, label_key

        new_mount_attempts += 1
        wsl_errors[label_key] = f"{label_key} (WSL {distro}): {res_wsl.get('error')}"
        return False, bool(res_wsl.get("permission_denied")), label_key

    if wsl_targets and not aborted_permission:
        for wt in wsl_targets:
            ok_w, perm_w, _ = _attempt_wsl_mount(wt)
            if not ok_w:
                if perm_w:
                    aborted_permission = True
                    break
                if str(wt.get("sftp_server_command") or "").strip():
                    failed_wsl_targets.append(wt)

    # Single clean retry pass for any drives or WSL targets that transiently failed (e.g. parked HDD spin-up, cold WSL2 boot, or sshd hiccup)
    if (failed_letters or failed_wsl_targets) and not aborted_permission:
        time.sleep(2.0)
        for letter in list(failed_letters):
            drive_mp = safe_lexical_resolve(container_dir / letter)
            res2 = _mount_sftp_directory(
                machine=machine,
                ep_dict=ep_dict,
                host=host,
                remote_target=f"{letter}:/",
                mount_point=drive_mp,
                vol_label=f"Fleet-{machine.id}-{letter}",
                log_stem=f"{machine.id}-{letter}",
                allow_empty_fallback=False,
            )
            if res2.get("ok"):
                mounted_drives.append(f"{letter}:/")
                total_visible += int(res2.get("visible_count") or 0)
                any_already = False
                drive_errors.pop(letter, None)
                failed_letters.remove(letter)
            else:
                drive_errors[letter] = f"{letter}: {res2.get('error')}"
                if res2.get("permission_denied"):
                    aborted_permission = True
                    break
            time.sleep(0.4)

        if not aborted_permission:
            for wt in list(failed_wsl_targets):
                ok_w2, perm_w2, _ = _attempt_wsl_mount(wt)
                if ok_w2:
                    failed_wsl_targets.remove(wt)
                elif perm_w2:
                    aborted_permission = True
                    break

    mounted_drives.sort()
    final_errors = [drive_errors[ch] for ch in sorted(drive_errors.keys())] + [
        wsl_errors[k] for k in sorted(wsl_errors.keys())
    ]

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

    # Create/refresh `WSL` shortcut (`~/RemoteComputers/pc/WSL -> Y`) when primary WSL distro is mounted on a drive letter
    mounted_wsl_names = {w["mount_name"] for w in mounted_wsl}
    wsl_shortcut_created = False
    if mounted_wsl and "WSL" not in mounted_wsl_names:
        wsl_link = container_dir / "WSL"
        rel_wsl_target = mounted_wsl[0]["mount_name"]
        try:
            if not is_mounted(wsl_link):
                if wsl_link.is_symlink() or wsl_link.is_file():
                    wsl_link.unlink()
                if not wsl_link.exists():
                    wsl_link.symlink_to(rel_wsl_target)
                wsl_shortcut_created = True
        except OSError:
            pass

    # Clean up any stale unmounted empty drive directories from previously unplugged USB drives
    mounted_names = {d[0] for d in mounted_drives} | mounted_wsl_names
    try:
        for child in list(container_dir.iterdir()):
            if child.name in ("Home", "WSL") or child.name in mounted_names:
                continue
            if not is_mounted(child) and not child.is_symlink() and child.is_dir():
                _cleanup_unmounted_empty_dir(child)
    except OSError:
        pass

    if not mounted_drives and not mounted_wsl:
        # Never delete `container_dir` itself — keep its APFS inode permanent for macOS Sidebar Favorites!
        return {
            "ok": False,
            "machine_id": machine.id,
            "permission_denied": aborted_permission,
            "error": "; ".join(final_errors) or "No Windows drives could be mounted.",
        }

    flat_entries = [d[0] for d in mounted_drives]
    for wm in mounted_wsl:
        if wm["mount_name"] not in flat_entries:
            flat_entries.append(wm["mount_name"])
    if wsl_shortcut_created and "WSL" not in flat_entries:
        flat_entries.append("WSL")
    if home_created:
        flat_entries.insert(0, "Home")

    remote_parts = list(mounted_drives) + [
        (f"{w['drive_letter']}: ({w['remote_root']})" if w["drive_letter"] else w["remote_root"])
        for w in mounted_wsl
    ]

    out: Dict[str, Any] = {
        "ok": True,
        "machine_id": machine.id,
        "engine": "rclone-serve-nfs-drives" if platform.system().lower() == "darwin" else "rclone-mount-drives",
        "mount_point": str(container_dir),
        "remote_root": ", ".join(remote_parts),
        "drives": mounted_drives,
        "home_created": home_created,
        "already_mounted": any_already,
        "visible_count": total_visible,
        "sample_entries": flat_entries,
        "endpoint": ep_dict,
    }
    if mounted_wsl:
        out["wsl_mounts"] = mounted_wsl
    if wsl_shortcut_created:
        out["wsl_shortcut_created"] = True
    if final_errors:
        out["failed_drives"] = final_errors
    return out


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
    container_dir = safe_lexical_resolve(container_dir)

    # Transition cleanly if `container_dir` (`~/RemoteComputers/spark01`) was previously mounted directly
    if is_mounted(container_dir):
        unmount_sshfs(container_dir)
    if is_mounted(container_dir):
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"Could not detach previous direct mount at {container_dir}; close any terminal inside {container_dir} and retry.",
        }

    try:
        container_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "machine_id": machine.id,
            "permission_denied": isinstance(exc, PermissionError) or exc.errno in (errno.EPERM, errno.EACCES),
            "error": f"Cannot access {container_dir}: {exc}",
        }
    _ensure_spotlight_never_index(container_dir)
    remote_home = str(ping.get("home") or "/").replace("\\", "/")

    reg = _prune_stale_registry_entries(machine.id, container_dir)
    reg[str(container_dir)] = {
        "machine_id": machine.id,
        "remote_root": remote_home,
        "container_only": "1",
    }
    save_mounts_registry(reg)

    home_mp = safe_lexical_resolve(container_dir / "Home")
    # If `Home` was previously a symlink, remove the symlink before creating mount directory
    if not is_mounted(home_mp) and home_mp.is_symlink():
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
        ext_mp = safe_lexical_resolve(container_dir / slug)
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


def repair_macos_sidebar_favorites(
    base_dir: Path,
    machine_ids: List[str],
    dismiss_stuck_panels: bool = True,
) -> Dict[str, Any]:
    """Repair stale macOS Finder & `NSOpenPanel` Sidebar Favorites (`FavoriteItems.sfl4` / `.sfl3`)
    so `pc`, `spark01`, `spark03`, `spark04` never trigger `'original item can't be found'` in Antigravity/Claude/Codex."""
    if platform.system().lower() != "darwin" or not machine_ids:
        return {"ok": True, "repaired": False, "skipped": True}

    swift_bin = shutil.which("swift") or "/usr/bin/swift"
    if not os.path.exists(swift_bin):
        return {"ok": False, "repaired": False, "error": "swift not found"}

    # Only dismiss hung `openAndSavePanelService` upfront during interactive `fleet mount-all` (never close an open dialog every 30s!)
    if dismiss_stuck_panels:
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


def _all_recorded_daemons_healthy(machine_id: str, container_dir: Path, expected_host: str) -> bool:
    """Fast zero-SSH check for background LaunchAgent ticks: returns True iff `machine_id` already has
    all expected soft-NFS daemons recorded under `container_dir` and every one passes uncached
    `.fleet-probe-<uuid>` health."""
    if platform.system().lower() != "darwin" or is_mounted(container_dir):
        return False
    daemons = _daemons_dir()
    meta_files = sorted(daemons.glob(f"{machine_id}-*.json"))
    if not meta_files:
        return False

    container_prefix = normalize_mount_path(str(safe_lexical_resolve(container_dir)))
    reg = load_mounts_registry()
    expected_targets: Set[str] = set()
    for reg_key, info in reg.items():
        if str(info.get("machine_id") or "") != machine_id or info.get("container_only") == "1":
            continue
        key_norm = normalize_mount_path(reg_key)
        if key_norm.startswith(container_prefix + "/"):
            expected_targets.add(str(info.get("remote_root") or key_norm))
    if len(expected_targets) > len(meta_files):
        return False

    healthy_count = 0
    for mf in meta_files:
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
            mp_str = str(meta.get("mount_point") or "")
            if not mp_str:
                return False
            if not normalize_mount_path(mp_str).startswith(container_prefix + "/"):
                return False
        except Exception:
            return False
        if _probe_mount_health(Path(mp_str), mf.stem, timeout_sec=12.0, expected_host=expected_host) != "healthy":
            return False
        healthy_count += 1
    return healthy_count > 0


def _parse_sftp_windows_batch_output(stdout: str, default_user: str = "") -> Tuple[str, List[str]]:
    """Parse `@pwd` + `-@cd /X:/` + `@pwd` batch output from OpenSSH `sftp` against Win32-OpenSSH.
    Returns `(remote_home, drive_letters)` without ever spawning `powershell.exe` or `cmd.exe`."""
    default_home = f"C:/Users/{default_user}" if default_user else "C:/"
    remote_home = default_home
    discovered_letters: List[str] = []
    pwd_lines: List[str] = []
    for line in stdout.splitlines():
        m = re.search(r"Remote working directory:\s*(.+?)\s*$", line)
        if m:
            pwd_lines.append(m.group(1).strip().replace("\\", "/"))

    if pwd_lines:
        first = pwd_lines[0]
        hm = re.match(r"^/?([A-Za-z]):/(.+)$", first)
        if hm:
            remote_home = f"{hm.group(1).upper()}:/{hm.group(2).strip('/')}"
        for raw_dir in pwd_lines[1:]:
            dm = re.match(r"^/?([A-Za-z]):/?$", raw_dir)
            if dm:
                letter = dm.group(1).upper()
                if letter not in discovered_letters:
                    discovered_letters.append(letter)

    return remote_home, discovered_letters


def _discover_windows_via_pure_sftp(
    machine: MachineConfig,
    host: str,
    container_dir: Path,
) -> Optional[Dict[str, Any]]:
    """Discover Windows home path and all accessible drive letters (`C:`..`Z:`) using PURE SFTP ONLY.

    Connects directly to Windows OpenSSH's `sftp-server.exe` subsystem — NEVER spawns `powershell.exe`,
    `cmd.exe`, or `conhost.exe` on Windows, eliminating leaked PowerShell processes permanently!
    """
    sftp_bin = shutil.which("sftp") or ("/usr/bin/sftp" if os.path.exists("/usr/bin/sftp") else None)
    if sftp_bin:
        cmd = [
            sftp_bin,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
        ]
        if machine.port and machine.port != 22:
            cmd.extend(["-P", str(machine.port)])
        if machine.ssh_identity_file:
            cmd.extend(["-i", os.path.expanduser(machine.ssh_identity_file)])
        target = f"{machine.user}@{host}" if machine.user else host
        cmd.extend(["-b", "-", target])

        batch_lines = ["@pwd"]
        for ch in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            batch_lines.append(f"-@cd /{ch}:/")
            batch_lines.append("@pwd")
        batch_input = "\n".join(batch_lines) + "\n"

        try:
            proc = subprocess.run(
                cmd,
                input=batch_input,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            if proc.stdout:
                remote_home, letters = _parse_sftp_windows_batch_output(proc.stdout, default_user=machine.user)
                if letters:
                    return {
                        "ok": True,
                        "os": "windows",
                        "home": remote_home,
                        "drives": [f"{ch}:/" for ch in letters],
                        "sftp_discovery": True,
                    }
        except (subprocess.TimeoutExpired, OSError):
            pass

    return _fallback_windows_ping_via_sftp(machine, host, container_dir)


def _fallback_windows_ping_via_sftp(
    machine: MachineConfig,
    host: str,
    container_dir: Path,
) -> Optional[Dict[str, Any]]:
    """Pure-SFTP fallback via `rclone lsf` (`--sftp-shell-type none`) when `/usr/bin/sftp` batch probe is unavailable:
    verifies SFTP on `C:/` and recovers drive letters from `mounts.json` + existing `<container>/<Drive>` folders."""
    rclone_bin = find_rclone_binary()
    if not rclone_bin:
        return None
    _, sftp_flags, _ = _select_verified_rclone_sftp_flags(
        rclone_bin, machine, host, "/C:/", allow_empty_fallback=False
    )
    if not sftp_flags:
        return None

    known_letters: List[str] = ["C"]
    default_home = f"C:/Users/{machine.user}" if machine.user else "C:/"
    reg = load_mounts_registry()
    for info in reg.values():
        if str(info.get("machine_id") or "") != machine.id:
            continue
        rroot = str(info.get("remote_root") or "").strip().replace("\\", "/")
        if info.get("container_only") == "1" and len(rroot) > 3 and rroot[1] == ":":
            default_home = rroot
        elif len(rroot) >= 2 and rroot[0].isalpha() and rroot[1] == ":":
            letter = rroot[0].upper()
            if letter not in known_letters:
                known_letters.append(letter)

    if not is_mounted(container_dir) and container_dir.is_dir():
        try:
            for child in sorted(container_dir.iterdir()):
                if len(child.name) == 1 and child.name.isalpha() and child.name.isupper():
                    if child.name not in known_letters:
                        known_letters.append(child.name)
        except OSError:
            pass

    return {
        "ok": True,
        "os": "windows",
        "home": default_home,
        "drives": [f"{ch}:/" for ch in known_letters],
        "sftp_fallback": True,
    }


_IGNORED_WSL_DISTROS = {
    "docker-desktop",
    "docker-desktop-data",
    "rancher-desktop",
    "rancher-desktop-data",
    "podman-machine-default",
}

_WSL_SFTP_SERVER_CANDIDATES = (
    "/usr/lib/openssh/sftp-server",
    "/usr/libexec/openssh/sftp-server",
    "/usr/lib/ssh/sftp-server",
    "/usr/libexec/sftp-server",
)


def _decode_windows_cli_bytes(raw: Any) -> str:
    """Decode Windows CLI output that may be UTF-16LE (e.g. `wsl.exe -l -v`), UTF-8, or already `str`."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.replace("\x00", "").lstrip("\ufeff")
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        if not data:
            return ""
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16", errors="replace").replace("\x00", "").lstrip("\ufeff")
        if b"\x00" in data:
            return data.decode("utf-16-le", errors="replace").replace("\x00", "").lstrip("\ufeff")
        return data.decode("utf-8", errors="replace").lstrip("\ufeff")
    return str(raw)


def _parse_windows_network_registry_wsl(stdout: str) -> List[Dict[str, str]]:
    """Parse `reg.exe query HKCU\\Network /s` output for persistent mapped drive letters pointing to WSL
    (e.g. `Y:` -> `\\\\wsl.localhost\\Ubuntu` or `\\\\wsl$\\Ubuntu\\home\\danli`)."""
    found: List[Dict[str, str]] = []
    seen_letters: Set[str] = set()
    current_letter: Optional[str] = None

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        km = re.match(r"^HKEY_CURRENT_USER\\Network\\([A-Za-z])$", line, re.IGNORECASE)
        if km:
            current_letter = km.group(1).upper()
            continue
        if line.upper().startswith("HKEY_"):
            current_letter = None
            continue
        if not current_letter:
            continue
        vm = re.match(r"^RemotePath\s+REG_(?:EXPAND_)?SZ\s+(.+?)\s*$", line, re.IGNORECASE)
        if not vm:
            continue
        parsed = parse_wsl_path(vm.group(1).strip())
        if not parsed:
            continue
        distro, wsl_root = parsed
        if not distro or current_letter in seen_letters:
            continue
        seen_letters.add(current_letter)
        found.append(
            {
                "drive_letter": current_letter,
                "distro": distro,
                "wsl_root": wsl_root if wsl_root.startswith("/") else "/",
            }
        )
    return found


def _parse_wsl_list_verbose(text: str) -> List[Dict[str, Any]]:
    """Parse `wsl.exe -l -v` output, filtering out internal Docker/Rancher/Podman distros and placing the default first."""
    distros: List[Dict[str, Any]] = []
    seen_names: Set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.replace("\r", "").strip()
        if not line:
            continue
        m = re.match(
            r"^(\*?)\s*(.+?)\s+(Running|Stopped|Installing|Converting|Uninstalling)\s+(\d+)\s*$",
            line,
            re.IGNORECASE,
        )
        if not m:
            continue
        is_default = bool(m.group(1))
        name = m.group(2).strip()
        state = m.group(3)
        version = int(m.group(4))
        low = name.lower()
        if (
            not name
            or low in _IGNORED_WSL_DISTROS
            or low.startswith("docker-desktop")
            or low.startswith("podman-machine-")
            or low in seen_names
        ):
            continue
        seen_names.add(low)
        distros.append(
            {
                "name": name,
                "state": state,
                "version": version,
                "is_default": is_default,
            }
        )
    distros.sort(key=lambda item: (0 if item["is_default"] else 1))
    return distros


def _build_direct_windows_ssh_cmd(machine: MachineConfig, host: str) -> Optional[List[str]]:
    """Build a direct, non-multiplexed SSH command against a Windows OpenSSH server (zero PowerShell)."""
    ssh_bin = shutil.which("ssh") or ("/usr/bin/ssh" if os.path.exists("/usr/bin/ssh") else None)
    if not ssh_bin:
        return None
    cmd = [
        ssh_bin,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-T",
    ]
    if machine.port and machine.port != 22:
        cmd.extend(["-p", str(machine.port)])
    key_path = _detect_default_ssh_key(machine)
    if key_path:
        cmd.extend(["-i", key_path])
    target = f"{machine.user}@{host}" if machine.user else host
    cmd.append(target)
    return cmd


def _build_wsl_sftp_server_command(distro: str, sftp_server_path: str) -> str:
    """Build the remote command passed to rclone's `--sftp-server-command` to launch WSL's native Linux `sftp-server`.

    Uses `-e` (never `--`) and quotes `distro` only if it contains whitespace so both `cmd.exe` and PowerShell
    `DefaultShell` configurations on Windows OpenSSH invoke `wsl.exe` identically.
    """
    clean_distro = distro.strip()
    clean_bin = sftp_server_path.strip()
    if not clean_distro or not clean_bin or any(ch in clean_distro for ch in "\r\n;&|`$"):
        return ""
    d_arg = f'"{clean_distro}"' if any(c.isspace() for c in clean_distro) else clean_distro
    return f"C:\\Windows\\System32\\wsl.exe -d {d_arg} -e {clean_bin}"


def _probe_wsl_distro_sftp_server(machine: MachineConfig, host: str, distro: str) -> Dict[str, Any]:
    """Check which Linux `sftp-server` binary is installed inside WSL distro `distro` (zero PowerShell)."""
    ssh_base = _build_direct_windows_ssh_cmd(machine, host)
    if not ssh_base:
        return {"ok": False, "sftp_server": "", "sftp_server_command": "", "error": "ssh CLI not found"}
    clean_distro = distro.strip()
    if not clean_distro or any(ch in clean_distro for ch in "\r\n;&|`$"):
        return {"ok": False, "sftp_server": "", "sftp_server_command": "", "error": f"Invalid WSL distro name: {distro!r}"}

    d_arg = f'"{clean_distro}"' if any(c.isspace() for c in clean_distro) else clean_distro
    probe_cmd = f"C:\\Windows\\System32\\wsl.exe -d {d_arg} -e ls -1 " + " ".join(_WSL_SFTP_SERVER_CANDIDATES)
    try:
        proc = subprocess.run(
            [*ssh_base, probe_cmd],
            capture_output=True,
            timeout=12,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {
            "ok": False,
            "sftp_server": "",
            "sftp_server_command": "",
            "error": f"Timed out probing WSL distro '{clean_distro}': {exc}",
        }

    out_text = _decode_windows_cli_bytes(proc.stdout)
    err_text = _decode_windows_cli_bytes(proc.stderr)
    cand_set = set(_WSL_SFTP_SERVER_CANDIDATES)
    for raw_line in out_text.splitlines():
        cand = raw_line.strip()
        if cand in cand_set:
            return {
                "ok": True,
                "sftp_server": cand,
                "sftp_server_command": _build_wsl_sftp_server_command(clean_distro, cand),
                "error": "",
            }

    err_low = (out_text + "\n" + err_text).lower()
    if "there is no distribution" in err_low or "wsl_e_distro_not_found" in err_low:
        return {
            "ok": False,
            "sftp_server": "",
            "sftp_server_command": "",
            "error": f"WSL distro '{clean_distro}' was not found on {host}.",
        }
    return {
        "ok": False,
        "sftp_server": "",
        "sftp_server_command": "",
        "error": (
            f"sftp-server not installed in WSL distro '{clean_distro}'. "
            "Run once in WSL: sudo apt update && sudo apt install -y openssh-sftp-server"
        ),
    }


def discover_windows_wsl_targets(machine: MachineConfig, host: str) -> List[Dict[str, Any]]:
    """Discover WSL mapped drives (`HKCU\\Network\\Y` -> `\\\\wsl.localhost\\Ubuntu`), explicit config
    `MachineConfig.wsl_drives`, and installed non-Docker WSL distributions (`wsl.exe -l -v`) on a Windows PC.

    Uses lightweight direct `reg.exe` / `wsl.exe` SSH calls (never spawns PowerShell) and probes each
    discovered distro for its native Linux `sftp-server` binary so rclone can mount WSL directly over SFTP.
    """
    ssh_base = _build_direct_windows_ssh_cmd(machine, host)
    if not ssh_base:
        return []

    mapped_entries: List[Dict[str, str]] = []
    try:
        reg_proc = subprocess.run(
            [*ssh_base, "C:\\Windows\\System32\\reg.exe query HKCU\\Network /s"],
            capture_output=True,
            timeout=8,
            check=False,
        )
        reg_text = _decode_windows_cli_bytes(reg_proc.stdout)
        if reg_text:
            mapped_entries = _parse_windows_network_registry_wsl(reg_text)
    except (subprocess.TimeoutExpired, OSError):
        pass

    installed_distros: List[Dict[str, Any]] = []
    try:
        wsl_proc = subprocess.run(
            [*ssh_base, "C:\\Windows\\System32\\wsl.exe -l -v"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        wsl_text = _decode_windows_cli_bytes(wsl_proc.stdout)
        if wsl_text:
            installed_distros = _parse_wsl_list_verbose(wsl_text)
    except (subprocess.TimeoutExpired, OSError):
        pass

    canonical_distro_names = {d["name"].lower(): d["name"] for d in installed_distros}

    raw_targets: List[Dict[str, str]] = []
    used_mount_names: Set[str] = set()
    covered_distros: Set[str] = set()

    # 1. Explicit `wsl_drives` overrides from `~/.config/remote-fleet/fleet.json` (e.g. {"Y": "Ubuntu"})
    for raw_letter, spec in sorted((machine.wsl_drives or {}).items()):
        letter = raw_letter.strip().rstrip(":/\\").upper()
        if len(letter) != 1 or not letter.isalpha():
            continue
        distro, wsl_root = _parse_wsl_drive_spec(spec)
        if not distro:
            continue
        distro = canonical_distro_names.get(distro.lower(), distro)
        if letter in used_mount_names:
            continue
        used_mount_names.add(letter)
        covered_distros.add(distro.lower())
        raw_targets.append(
            {
                "mount_name": letter,
                "drive_letter": letter,
                "distro": distro,
                "wsl_root": wsl_root or "/",
            }
        )

    # 2. Persistent Windows mapped network drives pointing to `\\wsl.localhost\<distro>` or `\\wsl$\<distro>`
    for entry in mapped_entries:
        letter = entry["drive_letter"].upper()
        if letter in used_mount_names:
            continue
        distro = canonical_distro_names.get(entry["distro"].lower(), entry["distro"])
        used_mount_names.add(letter)
        covered_distros.add(distro.lower())
        raw_targets.append(
            {
                "mount_name": letter,
                "drive_letter": letter,
                "distro": distro,
                "wsl_root": entry.get("wsl_root") or "/",
            }
        )

    has_mapped_wsl = bool(raw_targets)

    # 3. Any remaining user WSL distributions from `wsl.exe -l -v` not yet exposed via a mapped drive letter.
    #    Never cold-boot old/unused Stopped distros when an explicit mapped drive (e.g. `Y:`) or primary WSL target already exists.
    for item in installed_distros:
        distro = str(item["name"])
        if distro.lower() in covered_distros:
            continue
        is_running = str(item.get("state") or "").lower() == "running"
        if not raw_targets and "WSL" not in used_mount_names:
            mount_name = "WSL"
        else:
            if not is_running:
                continue
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", distro).strip("-") or "Linux"
            mount_name = f"WSL-{slug}"
        if mount_name in used_mount_names:
            continue
        used_mount_names.add(mount_name)
        covered_distros.add(distro.lower())
        raw_targets.append(
            {
                "mount_name": mount_name,
                "drive_letter": "",
                "distro": distro,
                "wsl_root": "/",
            }
        )

    if not raw_targets:
        return []

    # Probe native Linux `sftp-server` once per unique distro
    probe_cache: Dict[str, Dict[str, Any]] = {}
    targets: List[Dict[str, Any]] = []
    for item in raw_targets:
        distro = item["distro"]
        cache_key = distro.lower()
        if cache_key not in probe_cache:
            probe_cache[cache_key] = _probe_wsl_distro_sftp_server(machine, host, distro)
        probe = probe_cache[cache_key]
        wsl_root = item["wsl_root"] or "/"
        targets.append(
            {
                "mount_name": item["mount_name"],
                "drive_letter": item["drive_letter"],
                "distro": distro,
                "wsl_root": wsl_root,
                "remote_uri": format_wsl_path(distro, wsl_root),
                "explicit": bool(item["drive_letter"]) or (not has_mapped_wsl and item["mount_name"] == "WSL"),
                "sftp_server": str(probe.get("sftp_server") or ""),
                "sftp_server_command": str(probe.get("sftp_server_command") or ""),
                "probe_error": str(probe.get("error") or ""),
            }
        )
    return targets


def mount_single_machine_root(
    transport: FleetTransport,
    machine: MachineConfig,
    base_dir: Path,
    from_launchd: bool = False,
    from_login_app: bool = False,
) -> Dict[str, Any]:
    """Mount a remote computer inside permanent local APFS container `~/RemoteComputers/<machine.id>/`."""
    mount_point = safe_lexical_resolve(base_dir / machine.id)

    ep = transport.resolve_endpoint(machine)
    if ep.transport == "local":
        return {"ok": True, "skipped": True, "reason": "self (local computer)", "machine_id": machine.id}

    # Ensure any legacy direct mount on `mount_point` is detached and keep `mount_point` permanent on APFS!
    if is_mounted(mount_point):
        unmount_sshfs(mount_point)
    if not is_mounted(mount_point):
        try:
            mount_point.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "ok": False,
                "machine_id": machine.id,
                "permission_denied": isinstance(exc, PermissionError) or exc.errno in (errno.EPERM, errno.EACCES),
                "error": f"Cannot access {mount_point}: {exc}",
            }

    # On non-interactive/login-app runs, if every recorded mount for `machine.id` is already live & passes
    # uncached `.fleet-probe-<uuid>` health, skip spawning SSH / SFTP probes completely!
    if (from_launchd or from_login_app) and _all_recorded_daemons_healthy(
        machine.id, mount_point, ep.host if ep.reachable else ""
    ):
        return {
            "ok": True,
            "machine_id": machine.id,
            "mount_point": str(mount_point),
            "already_mounted": True,
            "endpoint": ep.to_dict(),
        }

    if not ep.reachable:
        return {"ok": False, "machine_id": machine.id, "error": f"Unreachable ({ep.error})"}

    # For Windows PCs, NEVER spawn `powershell.exe` over SSH to discover drives!
    # Use 100% pure SFTP (`sftp -b -` / `rclone lsf`) + direct `reg.exe`/`wsl.exe` WSL discovery.
    if machine.os == "windows":
        cand_hosts = [ep.host]
        if machine.tailscale_host and machine.tailscale_host not in cand_hosts:
            if not is_tailscale_only_host(machine.tailscale_host) or is_local_tailscale_running():
                cand_hosts.append(machine.tailscale_host)
        for chost in cand_hosts:
            sftp_ping = _discover_windows_via_pure_sftp(machine, chost, mount_point)
            if sftp_ping is not None:
                wsl_targets = discover_windows_wsl_targets(machine, chost)
                if wsl_targets:
                    sftp_ping["wsl_targets"] = wsl_targets
                active_ep_dict = {
                    **ep.to_dict(),
                    "host": chost,
                    "transport": "tailscale" if chost == machine.tailscale_host else ep.transport,
                }
                return _mount_windows_all_drives(machine, active_ep_dict, chost, sftp_ping, mount_point)
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"SFTP connection failed on {ep.host} (check OpenSSH Server / sshd on Windows).",
        }

    ping = transport.run_worker_op(machine, {"op": "ping"}, timeout_sec=20)
    active_ep_dict = ping.get("endpoint") if isinstance(ping.get("endpoint"), dict) else ep.to_dict()
    active_host = str(active_ep_dict.get("host") or ep.host)

    if not ping.get("ok"):
        return {
            "ok": False,
            "machine_id": machine.id,
            "error": f"SSH auth failed on {active_host}: {ping.get('error')}",
        }

    if str(ping.get("os") or "").lower() == "windows":
        machine.os = "windows"
        wsl_targets = discover_windows_wsl_targets(machine, active_host)
        if wsl_targets:
            ping["wsl_targets"] = wsl_targets
        return _mount_windows_all_drives(machine, active_ep_dict, active_host, ping, mount_point)

    return _mount_posix_container(machine, active_ep_dict, active_host, ping, mount_point)


def _cleanup_stale_ssh_mux_sockets() -> None:
    """Remove any wedged `/tmp/rf-ssh-*` OpenSSH ControlMaster sockets & background mux processes."""
    if os.name != "posix":
        return
    if shutil.which("pkill"):
        try:
            subprocess.run(["pkill", "-f", "ssh: /tmp/rf-ssh-"], capture_output=True, timeout=3, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        for sock in Path("/tmp").glob("rf-ssh-*"):
            try:
                sock.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _kill_orphan_rclone_serve_daemons(live_daemon_pids: Set[int]) -> None:
    """Terminate leaked `rclone serve nfs` daemons referencing `remote-fleet/nfs-handles/`
    whose PID is NOT tracked in `live_daemon_pids`. Must ONLY be called after verifying
    zero unowned NFS mounts remain in `/sbin/mount`."""
    ps_bin = "/bin/ps" if os.path.exists("/bin/ps") else shutil.which("ps")
    if not ps_bin:
        return
    try:
        proc = subprocess.run(
            [ps_bin, "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return
    if proc.returncode != 0:
        return
    my_pid = os.getpid()
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmdline = parts[1]
        if (
            pid > 0
            and pid != my_pid
            and pid not in live_daemon_pids
            and "rclone" in cmdline
            and "serve nfs" in cmdline
            and "remote-fleet/nfs-handles/" in cmdline
        ):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def _cleanup_legacy_nfsmounts(root: Path, raw_root: Optional[Path] = None) -> None:
    """Detach ONLY unowned NFS mounts (legacy `rclone nfsmount` or dead-daemon orphans) under `root`,
    then sweep leftover `rclone nfsmount` and orphaned `rclone serve nfs` processes.

    CRITICAL macOS FACT: `/sbin/mount` prints only generic `statfs.f_flags` (e.g.
    `(nfs, nodev, nosuid, mounted by danlinyu)`) and NEVER prints NFS-layer options like `soft`
    (those only appear in `nfsstat -m`). Therefore we identify live managed mounts strictly via
    active daemon PIDs in `~/.cache/remote-fleet/daemons/*.json`, NEVER by checking for `"soft"`
    in `/sbin/mount` output!
    """
    if platform.system().lower() != "darwin":
        return

    raw_prefixes = {str(safe_lexical_resolve(root))}
    try:
        raw_prefixes.add(str(root.resolve()))
    except OSError:
        pass
    if raw_root is not None:
        raw_prefixes.add(str(safe_lexical_resolve(raw_root)))
        raw_prefixes.add(str(raw_root))
    prefixes = {normalize_mount_path(p) for p in raw_prefixes if p}

    live_daemon_mounts: Set[str] = set()
    live_daemon_pids: Set[int] = set()
    daemons = _daemons_dir()
    if daemons.is_dir():
        for mf in daemons.glob("*.json"):
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
                pid = int(meta.get("pid") or 0)
                mp_val = str(meta.get("mount_point") or "")
                if pid > 0 and mp_val:
                    os.kill(pid, 0)
                    live_daemon_pids.add(pid)
                    live_daemon_mounts.add(normalize_mount_path(mp_val))
                    live_daemon_mounts.add(normalize_mount_path(str(safe_lexical_resolve(Path(mp_val)))))
            except Exception:
                pass

    table = get_macos_mount_table()
    if table is None:
        return

    def _find_unowned_nfs(tbl: Dict[str, str]) -> List[str]:
        found: List[str] = []
        for mp_str in sorted(tbl.keys(), key=len, reverse=True):
            mp_norm = normalize_mount_path(mp_str)
            if any(mp_norm == pfx or mp_norm.startswith(pfx + "/") for pfx in prefixes):
                if "nfs" in tbl[mp_str].lower() and mp_norm not in live_daemon_mounts:
                    found.append(mp_str)
        return found

    # Unmount deepest paths first ONLY for truly unowned/orphaned NFS mounts under `RemoteComputers`
    unowned = _find_unowned_nfs(table)
    for mp_str in unowned:
        unmount_sshfs(Path(mp_str))

    # Never terminate any backing rclone process while an unowned NFS mount is still attached in the kernel
    if unowned:
        table_after = get_macos_mount_table()
        if table_after is None or _find_unowned_nfs(table_after):
            return

    if shutil.which("pgrep") and shutil.which("pkill"):
        try:
            chk = subprocess.run(
                ["pgrep", "-f", "rclone.*nfsmount"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if chk.returncode == 0 and chk.stdout.strip():
                subprocess.run(["pkill", "-f", "rclone.*nfsmount"], capture_output=True, timeout=4, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass

    _kill_orphan_rclone_serve_daemons(live_daemon_pids)


def _external_volume_not_ready(target_root: Path) -> Optional[Path]:
    """On macOS, if `target_root` resides under `/Volumes/<VolName>/...` (e.g. `/Volumes/X10 Pro/...`),
    never call `mkdir` before `diskarbitrationd` mounts `<VolName>` — creating `/Volumes/X10 Pro` early at login
    would cause macOS to mount the real external SSD at `/Volumes/X10 Pro 1`!"""
    if platform.system().lower() != "darwin":
        return None
    parts = target_root.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        vol_dir = Path("/Volumes") / parts[2]
        if not is_mounted(vol_dir):
            return vol_dir
    return None


def _resolve_mount_root(base_dir: Optional[Path]) -> Tuple[Path, Path]:
    """Return `(resolved_root, raw_root)` so symlinked `~/RemoteComputers -> /Volumes/X10 Pro/...` resolves cleanly."""
    raw_root = Path(os.path.abspath(os.path.expanduser(str(base_dir or (Path.home() / "RemoteComputers")))))
    try:
        resolved_root = raw_root.resolve()
    except OSError:
        resolved_root = safe_lexical_resolve(raw_root)
    return resolved_root, raw_root


def mount_all_machines(
    config: FleetConfig,
    transport: FleetTransport,
    base_dir: Optional[Path] = None,
    from_launchd: bool = False,
    from_login_app: bool = False,
) -> Dict[str, Any]:
    """Mount all reachable remote fleet computers under `~/RemoteComputers/<id>/` with mutex & self-healing."""
    root, raw_root = _resolve_mount_root(base_dir)
    non_interactive = from_launchd or from_login_app

    unready_vol = _external_volume_not_ready(root)
    if unready_vol is not None:
        return {
            "base_dir": str(root),
            "results": [],
            "waiting_for_volume": str(unready_vol),
        }

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "base_dir": str(root),
            "results": [],
            "error": f"Cannot access {root}: {exc}",
        }
    _ensure_spotlight_never_index(root)

    lock_dir = Path.home() / ".cache" / "remote-fleet"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file_path = lock_dir / "mount-all.lock"
    lock_fp = open(lock_file_path, "w", encoding="utf-8")
    try:
        if fcntl is not None:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_interactive else 0)
            try:
                fcntl.flock(lock_fp.fileno(), flags)
            except OSError:
                return {"base_dir": str(root), "results": [], "skipped_locked": True}

        if not from_launchd:
            _cleanup_stale_ssh_mux_sockets()
        _cleanup_legacy_nfsmounts(root, raw_root=raw_root)
        transport.resolve_all_endpoints(config.machines, force_refresh=True)

        results = []
        remote_ids: List[str] = []
        for m in config.machines:
            res = mount_single_machine_root(
                transport,
                m,
                root,
                from_launchd=from_launchd,
                from_login_app=from_login_app,
            )
            results.append(res)
            if not res.get("skipped"):
                remote_ids.append(m.id)

        newly_mounted = any(
            r.get("ok") and not r.get("already_mounted") and not r.get("skipped")
            for r in results
        )

        sidebar_info: Dict[str, Any] = {"repaired": False}
        if platform.system().lower() == "darwin" and remote_ids and (not non_interactive or newly_mounted):
            sidebar_info = repair_macos_sidebar_favorites(
                root,
                remote_ids,
                dismiss_stuck_panels=not non_interactive,
            )

        # Refresh macOS Finder windows (`~/RemoteComputers` and `~/RemoteComputers/<id>`) when new mounts appear
        if (
            platform.system().lower() == "darwin"
            and shutil.which("osascript")
            and newly_mounted
            and not sidebar_info.get("repaired")
        ):
            refresh_targets = [root]
            if raw_root != root and raw_root.exists():
                refresh_targets.append(raw_root)
            for mid in remote_ids:
                cand = root / mid
                if not is_mounted(cand) and cand.is_dir():
                    refresh_targets.append(cand)
            script_lines = []
            for p in refresh_targets:
                esc_p = str(p).replace("\\", "\\\\").replace('"', '\\"')
                script_lines.append(
                    f'tell application "Finder" to if exists (POSIX file "{esc_p}" as alias) then update (POSIX file "{esc_p}" as alias)'
                )
            try:
                subprocess.run(
                    ["osascript", "-e", "\n".join(script_lines)],
                    capture_output=True,
                    timeout=4,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass

        clean_info: Optional[Dict[str, Any]] = None
        if not non_interactive:
            clean_info = cleanup_remote_fleet_cache()

        out_summary: Dict[str, Any] = {
            "base_dir": str(root),
            "results": results,
            "sidebar_repaired": bool(sidebar_info.get("repaired")),
        }
        if clean_info and clean_info.get("removed_count"):
            out_summary["cleaned_bytes"] = int(clean_info.get("freed_bytes") or 0)
            out_summary["cleaned_count"] = int(clean_info.get("removed_count") or 0)
        return out_summary
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock_fp.close()


def unmount_all_machines(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    root, raw_root = _resolve_mount_root(base_dir)
    prefixes = {normalize_mount_path(str(root)), normalize_mount_path(str(raw_root))}
    results = []

    lock_dir = Path.home() / ".cache" / "remote-fleet"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_fp = open(lock_dir / "mount-all.lock", "w", encoding="utf-8")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass

        table = get_macos_mount_table()
        if table is not None:
            for mp_str in sorted(table.keys(), key=len, reverse=True):
                mp_norm = normalize_mount_path(mp_str)
                if any(mp_norm == pfx or mp_norm.startswith(pfx + "/") for pfx in prefixes):
                    results.append({"path": mp_str, **unmount_sshfs(Path(mp_str))})
        elif root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_symlink() or not child.is_dir():
                    continue
                if is_mounted(child):
                    results.append({"path": str(child), **unmount_sshfs(child)})
                else:
                    # Unmount nested multi-drive mounts (e.g. `~/RemoteComputers/pc/C`, `~/RemoteComputers/spark01/Home`)
                    for sub in sorted(child.iterdir()):
                        if not sub.is_symlink() and sub.is_dir() and is_mounted(sub):
                            results.append({"path": str(sub), **unmount_sshfs(sub)})

        # Stop all recorded `rclone serve nfs` daemons AFTER unmounting
        daemons = _daemons_dir()
        if daemons.is_dir():
            for mf in sorted(daemons.glob("*.json")):
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                    pid = int(meta.get("pid") or 0)
                    if pid > 0:
                        _safe_kill_rclone_daemon_pid(pid)
                except Exception:
                    pass
                try:
                    mf.unlink(missing_ok=True)
                except OSError:
                    pass

        # Sweep any leaked orphan `rclone serve nfs` daemons strictly after verifying no mounts remain
        if platform.system().lower() == "darwin":
            table_after = get_macos_mount_table()
            still_mounted = False
            if table_after is None:
                still_mounted = True
            else:
                for mp_str in table_after.keys():
                    mp_norm = normalize_mount_path(mp_str)
                    if any(mp_norm == pfx or mp_norm.startswith(pfx + "/") for pfx in prefixes):
                        still_mounted = True
                        break
            if not still_mounted:
                _kill_orphan_rclone_serve_daemons(set())

        clean_summary = cleanup_remote_fleet_cache()
        return {"base_dir": str(root), "results": results, "cleaned": clean_summary}
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock_fp.close()


def _path_size_bytes(path: Path) -> int:
    """Return total byte size of a file or directory without following symlinks."""
    try:
        if not path.exists() and not path.is_symlink():
            return 0
        if path.is_symlink() or path.is_file():
            return int(path.lstat().st_size)
        total = 0
        for dirpath, _dirnames, filenames in os.walk(str(path), followlinks=False):
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    total += int(fpath.lstat().st_size)
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _is_live_rclone_daemon_pid(pid: int) -> bool:
    """Return True iff `pid` is currently alive and belongs to an `rclone` daemon (protects against recycled PIDs)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    ps_bin = "/bin/ps" if os.path.exists("/bin/ps") else shutil.which("ps")
    if ps_bin:
        try:
            chk = subprocess.run(
                [ps_bin, "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if chk.returncode == 0:
                cmdline = chk.stdout.strip().lower()
                if cmdline and "rclone" not in cmdline:
                    return False
        except (subprocess.TimeoutExpired, OSError):
            pass
    return True


def cleanup_remote_fleet_cache(
    home_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Safely find and remove stale/obsolete `remote-fleet` caches, logs, and temp files on internal storage
    while preserving all currently active live mounts, configs, and harnesses."""
    home = (home_dir or Path.home()).expanduser().resolve()
    cache_root = home / ".cache" / "remote-fleet"
    daemons_dir = cache_root / "daemons"

    live_stems: Set[str] = set()
    removed: List[Dict[str, Any]] = []
    freed_bytes = 0

    def _remove_target(target: Path, reason: str) -> None:
        nonlocal freed_bytes
        if not target.exists() and not target.is_symlink():
            return
        size = _path_size_bytes(target)
        if not dry_run:
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                return
        freed_bytes += size
        removed.append({"path": str(target), "bytes": size, "reason": reason})

    # 1. Inspect recorded daemons: preserve live rclone daemons, prune dead daemon metadata files
    if daemons_dir.is_dir():
        for mf in sorted(daemons_dir.glob("*.json")):
            pid = 0
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
                pid = int(meta.get("pid") or 0)
            except Exception:
                pid = 0
            if pid > 0 and _is_live_rclone_daemon_pid(pid):
                live_stems.add(mf.stem)
            else:
                _remove_target(mf, "dead daemon metadata")

    # 2. Remove orphaned NFS handle caches and VFS write caches for non-live mounts
    for sub_name, label in (
        ("nfs-handles", "stale NFS handle cache"),
        ("vfs-cache", "stale VFS write cache"),
    ):
        sub_dir = cache_root / sub_name
        if sub_dir.is_dir():
            for child in sorted(sub_dir.iterdir()):
                if child.name not in live_stems:
                    _remove_target(child, label)

    # 3. Remove stale `rclone-*.log` files for non-live mounts
    if cache_root.is_dir():
        for lf in sorted(cache_root.glob("rclone-*.log")):
            stem = lf.stem[len("rclone-") :]
            if stem not in live_stems:
                _remove_target(lf, "stale rclone log")

        # 4. Remove obsolete legacy LaunchAgent logs and temporary Swift source after helper binary is compiled
        for legacy_log in ("automount.log", "automount.err.log"):
            _remove_target(cache_root / legacy_log, "obsolete LaunchAgent log")

        fallback_plist = home / "Library" / "LaunchAgents" / f"{LOGIN_FALLBACK_AGENT_LABEL}.plist"
        if not fallback_plist.exists():
            for fb_log in ("login-app-launch.log", "login-app-launch.err.log"):
                _remove_target(cache_root / fb_log, "unused fallback launcher log")

        if (cache_root / "repair-sidebar-v1").is_file():
            _remove_target(cache_root / "repair_sidebar_v1.swift", "temporary Swift source (already compiled)")

    # 5. Remove legacy `rclone nfsmount` handle cache under `~/.cache/rclone/serve-nfs-handle-cache` if no `nfsmount` runs
    legacy_rclone_nfs = home / ".cache" / "rclone" / "serve-nfs-handle-cache"
    if legacy_rclone_nfs.exists():
        nfsmount_active = False
        if shutil.which("pgrep"):
            try:
                chk = subprocess.run(
                    ["pgrep", "-f", "rclone.*nfsmount"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                nfsmount_active = chk.returncode == 0 and bool(chk.stdout.strip())
            except (subprocess.TimeoutExpired, OSError):
                nfsmount_active = True
        if not nfsmount_active:
            _remove_target(legacy_rclone_nfs, "legacy rclone nfsmount handle cache")

    # 6. Clean stale `/tmp/rf-ssh-*` control sockets (only when targeting live user home)
    if home_dir is None and os.name == "posix":
        try:
            for sock in sorted(Path("/tmp").glob("rf-ssh-*")):
                _remove_target(sock, "stale SSH mux socket")
        except OSError:
            pass

    storage_paths = [
        ("cache_remote_fleet", cache_root),
        ("cache_rclone", home / ".cache" / "rclone"),
        ("local_share_mirror", home / ".local" / "share" / "remote-fleet"),
        ("login_app", home / "Applications" / f"{LOGIN_APP_NAME}.app"),
        ("fleet_workspaces", home / "Fleet"),
    ]
    storage_usage = [
        {"name": name, "path": str(p), "bytes": _path_size_bytes(p), "exists": p.exists()}
        for name, p in storage_paths
    ]

    return {
        "ok": True,
        "dry_run": dry_run,
        "live_mounts": sorted(live_stems),
        "removed_count": len(removed),
        "freed_bytes": freed_bytes,
        "removed": removed,
        "storage_usage": storage_usage,
    }


def _sync_local_runtime_mirror(home: Path, repo_src: Path) -> Path:
    """Keep an internal-APFS copy of `remote_fleet` under `~/.local/share/remote-fleet/src`
    so the login LaunchAgent always boots cleanly even if the git checkout sits on an external SSD."""
    mirror_src = home / ".local" / "share" / "remote-fleet" / "src"
    pkg_src = repo_src / "remote_fleet"
    pkg_dst = mirror_src / "remote_fleet"
    if pkg_src.is_dir():
        try:
            pkg_dst.mkdir(parents=True, exist_ok=True)
            for py_file in pkg_src.glob("*.py"):
                shutil.copy2(py_file, pkg_dst / py_file.name)
        except OSError:
            pass
    return mirror_src


def uninstall_macos_launch_agent(home_dir: Optional[Path] = None) -> bool:
    """Unload and remove `~/Library/LaunchAgents/com.remote-fleet.automount.plist` so no background
    polling timer can ever run on this Mac."""
    home = (home_dir or Path.home()).expanduser().resolve()
    plist_path = home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    existed = plist_path.is_file()

    if home_dir is None and platform.system().lower() == "darwin" and shutil.which("launchctl"):
        uid = os.getuid()
        for lcmd in (
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"],
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
        ):
            try:
                subprocess.run(lcmd, capture_output=True, timeout=5, check=False)
            except (subprocess.TimeoutExpired, OSError):
                pass

    if existed:
        try:
            plist_path.unlink(missing_ok=True)
        except OSError:
            pass
    return existed


def install_macos_launch_agent(
    home_dir: Optional[Path] = None,
    python_executable: Optional[str] = None,
    base_dir: Optional[Path] = None,
    load_now: bool = True,
) -> Path:
    """Create an opt-in one-shot login LaunchAgent (`RunAtLoad` ONLY — NO repeating `StartInterval` polling)."""
    home = (home_dir or Path.home()).expanduser().resolve()
    py_exe = python_executable or sys.executable or "/usr/bin/python3"
    repo_src_path = get_repo_root() / "src"
    mirror_src_path = _sync_local_runtime_mirror(home, repo_src_path)
    py_path_str = f"{repo_src_path}:{mirror_src_path}"
    mount_root = os.path.abspath(os.path.expanduser(str(base_dir or (home / "RemoteComputers"))))

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
            "--from-launchd",
        ],
        "EnvironmentVariables": {
            "PYTHONPATH": py_path_str,
            "PATH": f"{home / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "AbandonProcessGroup": True,
        "StandardOutPath": str(home / ".cache" / "remote-fleet" / "automount.log"),
        "StandardErrorPath": str(home / ".cache" / "remote-fleet" / "automount.err.log"),
    }
    (home / ".cache" / "remote-fleet").mkdir(parents=True, exist_ok=True)
    plist_bytes = plistlib.dumps(plist_data)
    existing_bytes: Optional[bytes] = None
    if plist_path.is_file():
        try:
            existing_bytes = plist_path.read_bytes()
        except OSError:
            existing_bytes = None
    plist_changed = existing_bytes != plist_bytes
    if plist_changed:
        plist_path.write_bytes(plist_bytes)

    if load_now and home_dir is None and platform.system().lower() == "darwin" and shutil.which("launchctl"):
        uid = os.getuid()
        already_loaded = False
        try:
            chk = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"],
                capture_output=True,
                timeout=4,
                check=False,
            )
            already_loaded = chk.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            already_loaded = False

        # Skip redundant bootout/bootstrap when plist is unchanged and already loaded
        if plist_changed or not already_loaded:
            try:
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
            time.sleep(0.25)
            for _attempt in range(2):
                try:
                    boot = subprocess.run(
                        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    if boot.returncode == 0:
                        break
                except (subprocess.TimeoutExpired, OSError):
                    break
                time.sleep(0.3)

    return plist_path


_SWIFT_LOGIN_MOUNT_SOURCE = r'''
import Foundation

struct LoginMountConfig: Decodable {
    let python_exe: String
    let mount_dir: String
    let resolved_dir: String
    let pythonpath: String
    let path_env: String
}

let home = NSHomeDirectory()
let cfgURL = URL(fileURLWithPath: home).appendingPathComponent(".config/remote-fleet/login-mount.json")
let logDir = URL(fileURLWithPath: home).appendingPathComponent(".cache/remote-fleet")
let logURL = logDir.appendingPathComponent("login-mount.log")

func logLine(_ msg: String) {
    try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
    let ts = ISO8601DateFormatter().string(from: Date())
    let line = "[\(ts)] \(msg)\n"
    if let attrs = try? FileManager.default.attributesOfItem(atPath: logURL.path),
       let size = attrs[.size] as? NSNumber, size.intValue > 262144 {
        try? Data().write(to: logURL)
    }
    if let data = line.data(using: .utf8) {
        if FileManager.default.fileExists(atPath: logURL.path),
           let fh = try? FileHandle(forWritingTo: logURL) {
            fh.seekToEndOfFile()
            fh.write(data)
            fh.closeFile()
        } else {
            try? data.write(to: logURL)
        }
    }
}

guard let rawCfg = try? Data(contentsOf: cfgURL),
      let cfg = try? JSONDecoder().decode(LoginMountConfig.self, from: rawCfg) else {
    logLine("Missing or unreadable config at \(cfgURL.path); exiting.")
    exit(0)
}

func externalVolumeRoot(_ path: String) -> String? {
    let comps = (path as NSString).pathComponents
    if comps.count >= 3 && comps[0] == "/" && comps[1] == "Volumes" {
        return "/Volumes/\(comps[2])"
    }
    return nil
}

func isVolumeMounted(_ volRoot: String) -> Bool {
    let urls = FileManager.default.mountedVolumeURLs(includingResourceValuesForKeys: nil, options: []) ?? []
    for u in urls {
        if u.standardizedFileURL.path == volRoot {
            return true
        }
    }
    return false
}

let volRoot = externalVolumeRoot(cfg.resolved_dir) ?? externalVolumeRoot(cfg.mount_dir)
if let vr = volRoot {
    var ready = false
    for _ in 0..<23 {
        if isVolumeMounted(vr) {
            ready = true
            break
        }
        Thread.sleep(forTimeInterval: 2.0)
    }
    if !ready {
        logLine("External volume \(vr) did not mount within 45s; exiting.")
        exit(0)
    }
}

if volRoot != nil || FileManager.default.fileExists(atPath: cfg.resolved_dir) {
    let probePath = FileManager.default.fileExists(atPath: cfg.resolved_dir)
        ? cfg.resolved_dir
        : (volRoot ?? cfg.resolved_dir)
    do {
        _ = try FileManager.default.contentsOfDirectory(atPath: probePath)
    } catch {
        logLine("Cannot access \(probePath) (macOS Privacy/Removable Volumes): \(error.localizedDescription)")
        exit(1)
    }
}

func runMountOnce() -> (Int32, String) {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: cfg.python_exe)
    proc.arguments = ["-m", "remote_fleet", "mount-all", "--dir", cfg.mount_dir, "--from-login-app"]
    var env = ProcessInfo.processInfo.environment
    env["PYTHONPATH"] = cfg.pythonpath
    env["PATH"] = cfg.path_env
    env["PYTHONUNBUFFERED"] = "1"
    proc.environment = env
    proc.standardInput = FileHandle.nullDevice

    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = pipe

    do {
        try proc.run()
    } catch {
        return (1, "Failed to launch python: \(error.localizedDescription)")
    }

    let killer = DispatchWorkItem {
        if proc.isRunning {
            proc.terminate()
        }
    }
    DispatchQueue.global().asyncAfter(deadline: .now() + 180.0, execute: killer)

    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()
    killer.cancel()

    let text = String(data: data, encoding: .utf8) ?? ""
    return (proc.terminationStatus, text)
}

logLine("Starting login auto-mount for \(cfg.mount_dir) (resolved: \(cfg.resolved_dir))")
let (rc1, out1) = runMountOnce()
if !out1.isEmpty {
    logLine("Pass 1 (rc=\(rc1)):\n\(out1.trimmingCharacters(in: .whitespacesAndNewlines))")
}

let lower1 = out1.lowercased()
if lower1.contains("unreachable") && !lower1.contains("operation not permitted") && !lower1.contains("permission denied") {
    logLine("One or more machines unreachable during early login; waiting 10s for 1 final retry...")
    Thread.sleep(forTimeInterval: 10.0)
    let (rc2, out2) = runMountOnce()
    if !out2.isEmpty {
        logLine("Pass 2 (rc=\(rc2)):\n\(out2.trimmingCharacters(in: .whitespacesAndNewlines))")
    }
}

logLine("Login auto-mount finished.")
exit(0)
'''


def _uninstall_login_app_fallback_agent(home: Path, live_darwin: bool) -> bool:
    plist_path = home / "Library" / "LaunchAgents" / f"{LOGIN_FALLBACK_AGENT_LABEL}.plist"
    existed = plist_path.is_file()
    if live_darwin and shutil.which("launchctl"):
        uid = os.getuid()
        for lcmd in (
            ["launchctl", "bootout", f"gui/{uid}/{LOGIN_FALLBACK_AGENT_LABEL}"],
            ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
        ):
            try:
                subprocess.run(lcmd, capture_output=True, timeout=5, check=False)
            except (subprocess.TimeoutExpired, OSError):
                pass
    if existed:
        try:
            plist_path.unlink(missing_ok=True)
        except OSError:
            pass
    return existed


def _install_login_app_fallback_agent(home: Path, app_path: Path) -> Path:
    """Install a strictly one-shot (`RunAtLoad` ONLY, ZERO `StartInterval`) LaunchAgent that calls
    `/usr/bin/open -g <Remote Fleet Mount.app>` once on reboot/login if `System Events` Login Items
    automation was denied."""
    agents_dir = home / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{LOGIN_FALLBACK_AGENT_LABEL}.plist"
    plist_data = {
        "Label": LOGIN_FALLBACK_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-g", str(app_path)],
        "RunAtLoad": True,
        "StandardOutPath": str(home / ".cache" / "remote-fleet" / "login-app-launch.log"),
        "StandardErrorPath": str(home / ".cache" / "remote-fleet" / "login-app-launch.err.log"),
    }
    (home / ".cache" / "remote-fleet").mkdir(parents=True, exist_ok=True)
    plist_bytes = plistlib.dumps(plist_data)
    existing: Optional[bytes] = None
    if plist_path.is_file():
        try:
            existing = plist_path.read_bytes()
        except OSError:
            existing = None
    if existing != plist_bytes:
        plist_path.write_bytes(plist_bytes)
    return plist_path


def uninstall_macos_login_item_app(home_dir: Optional[Path] = None) -> bool:
    """Remove `~/Applications/Remote Fleet Mount.app`, its macOS Login Item entry, fallback plist, and config."""
    home = (home_dir or Path.home()).expanduser().resolve()
    live_darwin = home_dir is None and platform.system().lower() == "darwin"
    removed = False

    if live_darwin and shutil.which("osascript"):
        script = (
            'tell application "System Events"\n'
            f'    if exists login item "{LOGIN_APP_NAME}" then\n'
            f'        delete login item "{LOGIN_APP_NAME}"\n'
            "    end if\n"
            "end tell\n"
        )
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=8, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass

    if _uninstall_login_app_fallback_agent(home, live_darwin=live_darwin):
        removed = True

    app_path = home / "Applications" / f"{LOGIN_APP_NAME}.app"
    if app_path.exists():
        try:
            shutil.rmtree(app_path)
            removed = True
        except OSError:
            pass

    cfg_file = home / ".config" / "remote-fleet" / "login-mount.json"
    if cfg_file.is_file():
        try:
            cfg_file.unlink(missing_ok=True)
            removed = True
        except OSError:
            pass
    return removed


def install_macos_login_item_app(
    home_dir: Optional[Path] = None,
    python_executable: Optional[str] = None,
    base_dir: Optional[Path] = None,
    register_login_item: bool = True,
) -> Dict[str, Any]:
    """Build & ad-hoc sign `~/Applications/Remote Fleet Mount.app` and register it to run ONCE at login/reboot.

    Why a native compiled `.app` bundle instead of bare `launchd` `python3`:
    - macOS TCC denies `/Volumes/<ExternalSSD>` access (`Operation not permitted`) to bare `launchd` Python scripts
      without ever showing an Allow dialog.
    - A signed `.app` bundle on the internal disk (`~/Applications/Remote Fleet Mount.app`) has a stable bundle ID
      (`com.remote-fleet.login-mount`) and `NSRemovableVolumesUsageDescription` so macOS prompts ONCE for Removable
      Volumes permission and remembers that approval across every reboot, log-out/log-in, and `git pull` (because we
      keep dynamic paths in `~/.config/remote-fleet/login-mount.json` and NEVER recompile/re-sign unless `version.txt`
      changes).
    - Runs strictly ONCE at login/reboot with ZERO background polling timer.
    """
    home = (home_dir or Path.home()).expanduser().resolve()
    py_exe = python_executable or sys.executable or "/usr/bin/python3"
    repo_src_path = get_repo_root() / "src"
    mirror_src_path = _sync_local_runtime_mirror(home, repo_src_path)
    resolved_root, raw_root = _resolve_mount_root(base_dir or (home / "RemoteComputers"))

    cfg_dir = home / ".config" / "remote-fleet"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "login-mount.json"
    cfg_payload = {
        "python_exe": py_exe,
        "mount_dir": str(raw_root),
        "resolved_dir": str(resolved_root),
        "pythonpath": f"{repo_src_path}:{mirror_src_path}",
        "path_env": f"{home / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    cfg_file.write_text(json.dumps(cfg_payload, indent=2) + "\n", encoding="utf-8")

    app_path = home / "Applications" / f"{LOGIN_APP_NAME}.app"
    contents_dir = app_path / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"
    exec_path = macos_dir / LOGIN_APP_EXECUTABLE
    info_plist = contents_dir / "Info.plist"
    version_file = resources_dir / "version.txt"

    current_ver = ""
    if version_file.is_file():
        try:
            current_ver = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            current_ver = ""

    needs_build = not (
        exec_path.is_file()
        and os.access(exec_path, os.X_OK)
        and info_plist.is_file()
        and current_ver == LOGIN_APP_VERSION
    )
    newly_created = False

    if needs_build:
        swiftc_bin = shutil.which("swiftc") or ("/usr/bin/swiftc" if os.path.exists("/usr/bin/swiftc") else None)
        if not swiftc_bin:
            return {
                "ok": False,
                "app_path": str(app_path),
                "config_path": str(cfg_file),
                "newly_created": False,
                "login_item_registered": False,
                "fallback_agent_installed": False,
                "error": "swiftc not found (install Apple Command Line Tools: xcode-select --install)",
            }

        macos_dir.mkdir(parents=True, exist_ok=True)
        resources_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="rf-login-app-") as tmp:
            swift_src = Path(tmp) / "main.swift"
            swift_src.write_text(_SWIFT_LOGIN_MOUNT_SOURCE, encoding="utf-8")
            try:
                comp = subprocess.run(
                    [swiftc_bin, "-O", str(swift_src), "-o", str(exec_path)],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                return {
                    "ok": False,
                    "app_path": str(app_path),
                    "config_path": str(cfg_file),
                    "newly_created": False,
                    "login_item_registered": False,
                    "fallback_agent_installed": False,
                    "error": f"Failed to compile {LOGIN_APP_NAME}.app: {exc}",
                }
            if comp.returncode != 0 or not exec_path.is_file():
                err_txt = comp.stderr.strip() or comp.stdout.strip() or f"exit code {comp.returncode}"
                return {
                    "ok": False,
                    "app_path": str(app_path),
                    "config_path": str(cfg_file),
                    "newly_created": False,
                    "login_item_registered": False,
                    "fallback_agent_installed": False,
                    "error": f"swiftc failed for {LOGIN_APP_NAME}.app: {err_txt}",
                }

        plist_payload = {
            "CFBundleIdentifier": LOGIN_APP_BUNDLE_ID,
            "CFBundleName": LOGIN_APP_NAME,
            "CFBundleDisplayName": LOGIN_APP_NAME,
            "CFBundleExecutable": LOGIN_APP_EXECUTABLE,
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": LOGIN_APP_VERSION,
            "CFBundleShortVersionString": "1.0",
            "LSUIElement": True,
            "LSBackgroundOnly": False,
            "NSRemovableVolumesUsageDescription": (
                "Remote Fleet Mount accesses external volumes (such as ~/RemoteComputers on an external SSD) "
                "to mount your PC and Spark drives automatically when you log in."
            ),
            "NSLocalNetworkUsageDescription": (
                "Remote Fleet Mount connects to your PC and DGX Sparks over your local network "
                "to mount their drives automatically when you log in."
            ),
        }
        info_plist.write_bytes(plistlib.dumps(plist_payload))
        version_file.write_text(f"{LOGIN_APP_VERSION}\n", encoding="utf-8")

        if shutil.which("xattr"):
            try:
                subprocess.run(["xattr", "-cr", str(app_path)], capture_output=True, timeout=5, check=False)
            except (subprocess.TimeoutExpired, OSError):
                pass

        codesign_bin = shutil.which("codesign") or ("/usr/bin/codesign" if os.path.exists("/usr/bin/codesign") else None)
        if codesign_bin:
            try:
                subprocess.run(
                    [
                        codesign_bin,
                        "--force",
                        "--deep",
                        "--sign",
                        "-",
                        "--identifier",
                        LOGIN_APP_BUNDLE_ID,
                        str(app_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass
        newly_created = True

    live_darwin = home_dir is None and platform.system().lower() == "darwin"
    login_item_registered = False
    fallback_agent_installed = False

    if register_login_item and live_darwin:
        esc_app = str(app_path).replace("\\", "\\\\").replace('"', '\\"')
        if shutil.which("osascript"):
            script = (
                'tell application "System Events"\n'
                f'    if exists login item "{LOGIN_APP_NAME}" then\n'
                f'        if (path of login item "{LOGIN_APP_NAME}") is not "{esc_app}" then\n'
                f'            delete login item "{LOGIN_APP_NAME}"\n'
                f'            make login item at end with properties {{path:"{esc_app}", hidden:false}}\n'
                "        end if\n"
                "    else\n"
                f'        make login item at end with properties {{path:"{esc_app}", hidden:false}}\n'
                "    end if\n"
                "end tell\n"
            )
            try:
                reg_proc = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                login_item_registered = reg_proc.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                login_item_registered = False

        if login_item_registered:
            _uninstall_login_app_fallback_agent(home, live_darwin=True)
        else:
            _install_login_app_fallback_agent(home, app_path)
            fallback_agent_installed = True

    return {
        "ok": True,
        "app_path": str(app_path),
        "config_path": str(cfg_file),
        "newly_created": newly_created,
        "login_item_registered": login_item_registered,
        "fallback_agent_installed": fallback_agent_installed,
        "error": "",
    }

