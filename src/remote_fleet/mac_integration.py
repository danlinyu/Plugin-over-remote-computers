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
    is_local_tailscale_running,
    is_self_machine,
    is_tailscale_only_host,
    load_mounts_registry,
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


def _cleanup_unmounted_empty_dir(mount_point: Path) -> None:
    """Remove an unmounted folder that only contains `.DS_Store` or nothing so Finder never shows dead empty folders."""
    if is_mounted(mount_point) or not mount_point.is_dir():
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


DAEMON_SCHEMA_VERSION = 2


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
                os.kill(pid, signal.SIGTERM)
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
) -> Dict[str, Any]:
    """Mount a single remote folder or drive (`remote_target`) onto `mount_point` with soft-NFS self-healing."""
    _ = vol_label
    mount_point = safe_lexical_resolve(mount_point)

    health = _probe_mount_health(mount_point, log_stem, timeout_sec=12.0, expected_host=host)
    if health == "healthy":
        visible = _safe_sample_entries(mount_point, timeout_sec=2.5)
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
    try:
        (mount_point / ".DS_Store").unlink(missing_ok=True)
    except OSError:
        pass

    cache_root = Path.home() / ".cache" / "remote-fleet"
    cache_root.mkdir(parents=True, exist_ok=True)

    # 1. macOS preferred path: `rclone serve nfs` on loopback + `/sbin/mount -t nfs` with soft,intr,nolocks,locallocks,nfc
    if rclone_bin and is_macos and os.path.exists("/sbin/mount"):
        initial_rclone_path = (
            f"/{remote_target}"
            if (len(remote_target) >= 2 and remote_target[1] == ":")
            else remote_target
        )
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
            )
        if not sftp_flags:
            _cleanup_unmounted_empty_dir(mount_point)
            return {
                "ok": False,
                "machine_id": machine.id,
                "error": f"rclone SFTP preflight failed on {host} ({remote_target}): {preflight_err}",
            }

        # Never pass `--max-connections 1` (deadlocks rclone VFS on nested Finder READDIRPLUS + .DS_Store calls!).
        permission_denied = False
        probe_timeout = 20.0 if machine.os == "windows" else 15.0
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
                "--vfs-fast-fingerprint",
                "--dir-cache-time",
                "2m",
                "--poll-interval",
                "0",
                "--sftp-idle-timeout",
                "4h",
                "--contimeout",
                "8s",
                "--timeout",
                "30s",
                "--low-level-retries",
                "2",
                "--retries",
                "2",
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
                # Soft, interruptible, local-lock, NFC-normalized NFSv3 mount — never wedges Finder or pops hard-mount dialogs!
                nfs_opts = (
                    f"port={nfs_port},mountport={nfs_port},tcp,"
                    "soft,intr,timeo=600,retrans=2,"
                    "nolocks,locallocks,nfc,rsize=131072,wsize=131072"
                )
                try:
                    mnt_proc = subprocess.run(
                        ["/sbin/mount", "-t", "nfs", "-o", nfs_opts, "localhost:/", str(mount_point)],
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
                meta_file.write_text(
                    json.dumps(
                        {
                            "schema": DAEMON_SCHEMA_VERSION,
                            "pid": proc.pid,
                            "port": nfs_port,
                            "host": host,
                            "machine_id": machine.id,
                            "mount_point": recorded_mp,
                            "remote_target": remote_target,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                if _probe_mount_health(mount_point, log_stem, timeout_sec=probe_timeout, expected_host=host) == "healthy":
                    visible = _safe_sample_entries(mount_point, timeout_sec=4.0)
                    reg = load_mounts_registry()
                    reg[recorded_mp] = {"machine_id": machine.id, "remote_root": remote_target}
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
        initial_rclone_path = (
            f"/{remote_target}"
            if (len(remote_target) >= 2 and remote_target[1] == ":")
            else remote_target
        )
        verified_rclone_path, sftp_flags, preflight_err = _select_verified_rclone_sftp_flags(
            rclone_bin, machine, host, initial_rclone_path, allow_empty_fallback=allow_empty_fallback
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
                "--vfs-fast-fingerprint",
                "--poll-interval",
                "0",
                "--dir-cache-time",
                "30s",
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
                    reg[str(mount_point)] = {"machine_id": machine.id, "remote_root": remote_target}
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
            visible = _safe_sample_entries(mount_point, timeout_sec=2.0)
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
    """Mount EVERY drive (`C:/`, `D:/`, `E:/`, ...) on a Windows PC under `~/RemoteComputers/<id>/<Drive>`
    plus a `Home` shortcut pointing straight to `C/Users/<user>`."""
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
    remote_home = str(ping.get("home") or "C:/").replace("\\", "/")
    reg = _prune_stale_registry_entries(machine.id, container_dir)
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
    drive_errors: Dict[str, str] = {}
    failed_letters: List[str] = []
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

    # Single clean retry pass for any drives that transiently failed (e.g. parked HDD spin-up or sshd handshake hiccup)
    if failed_letters and not aborted_permission:
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
                    break
            time.sleep(0.4)

    mounted_drives.sort()
    final_errors = [drive_errors[ch] for ch in sorted(drive_errors.keys())]

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
    mounted_names = {d[0] for d in mounted_drives}
    try:
        for child in list(container_dir.iterdir()):
            if child.name == "Home" or child.name in mounted_names:
                continue
            if not is_mounted(child) and not child.is_symlink() and child.is_dir():
                _cleanup_unmounted_empty_dir(child)
    except OSError:
        pass

    if not mounted_drives:
        # Never delete `container_dir` itself — keep its APFS inode permanent for macOS Sidebar Favorites!
        return {
            "ok": False,
            "machine_id": machine.id,
            "permission_denied": aborted_permission,
            "error": "; ".join(final_errors) or "No Windows drives could be mounted.",
        }

    flat_entries = [d[0] for d in mounted_drives]
    if home_created:
        flat_entries.insert(0, "Home")

    out: Dict[str, Any] = {
        "ok": True,
        "machine_id": machine.id,
        "engine": "rclone-serve-nfs-drives" if platform.system().lower() == "darwin" else "rclone-mount-drives",
        "mount_point": str(container_dir),
        "remote_root": ", ".join(mounted_drives),
        "drives": mounted_drives,
        "home_created": home_created,
        "already_mounted": any_already,
        "visible_count": total_visible,
        "sample_entries": flat_entries,
        "endpoint": ep_dict,
    }
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

    container_dir.mkdir(parents=True, exist_ok=True)
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


def mount_single_machine_root(
    transport: FleetTransport,
    machine: MachineConfig,
    base_dir: Path,
    from_launchd: bool = False,
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

    # On background ticks, if every recorded mount for `machine.id` is already live & passes uncached
    # `.fleet-probe-<uuid>` health, skip spawning SSH / SFTP probes completely (and preserve working
    # mounts even if `launchd` saw a transient TCP/Local Network probe failure!).
    if from_launchd and _all_recorded_daemons_healthy(
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
    # Use 100% pure SFTP (`sftp -b -` / `rclone lsf`) so zero PowerShell/cmd/conhost sessions are ever created.
    if machine.os == "windows":
        cand_hosts = [ep.host]
        if machine.tailscale_host and machine.tailscale_host not in cand_hosts:
            if not is_tailscale_only_host(machine.tailscale_host) or is_local_tailscale_running():
                cand_hosts.append(machine.tailscale_host)
        for chost in cand_hosts:
            sftp_ping = _discover_windows_via_pure_sftp(machine, chost, mount_point)
            if sftp_ping is not None:
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
) -> Dict[str, Any]:
    """Mount all reachable remote fleet computers under `~/RemoteComputers/<id>/` with mutex & self-healing."""
    root, raw_root = _resolve_mount_root(base_dir)

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

    lock_dir = Path.home() / ".cache" / "remote-fleet"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file_path = lock_dir / "mount-all.lock"
    lock_fp = open(lock_file_path, "w", encoding="utf-8")
    try:
        if fcntl is not None:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if from_launchd else 0)
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
            res = mount_single_machine_root(transport, m, root, from_launchd=from_launchd)
            results.append(res)
            if not res.get("skipped"):
                remote_ids.append(m.id)

        newly_mounted = any(
            r.get("ok") and not r.get("already_mounted") and not r.get("skipped")
            for r in results
        )

        sidebar_info: Dict[str, Any] = {"repaired": False}
        if platform.system().lower() == "darwin" and remote_ids and (not from_launchd or newly_mounted):
            sidebar_info = repair_macos_sidebar_favorites(
                root,
                remote_ids,
                dismiss_stuck_panels=not from_launchd,
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
                        os.kill(pid, signal.SIGTERM)
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

        return {"base_dir": str(root), "results": results}
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        lock_fp.close()


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
