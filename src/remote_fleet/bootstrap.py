"""Turnkey Home LAN & Tailscale Bootstrapper (`fleet bootstrap` / `./bootstrap_mac.sh`).

Tailored for Mac Mini (`danlins-mac-mini`) + 3 DGX Sparks (`spark01`, `spark03`, `spark04`) +
Windows 11 Home PC (`danlin-hud` / `pc`):
1. Reconnects macOS Tailscale if stopped, and scans Home LAN + Tailscale.
2. Cleans up duplicate apt `tailscaled` (`spark01-1`, `spark03-1`) if present so each Spark keeps
   its original single Tailscale node (`spark01`, `spark03`, `spark04`).
3. Sets up passwordless SSH keys (`spark01`, `spark03`, `spark04`, and Windows 11 `danlin-hud` helper
   including `C:\\ProgramData\\ssh\\administrators_authorized_keys`).
4. Downloads official standalone `rclone` binary directly into `~/.local/bin/rclone` on macOS
   when Homebrew is not installed (zero `brew`, zero `sudo`, zero macFUSE!).
5. Configures `~/.ssh/config`, all 5 harnesses, and mounts `~/RemoteComputers/{spark01,spark03,spark04,pc}`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from remote_fleet.config import (
    FleetConfig,
    MachineConfig,
    find_tailscale_binary,
    merge_tailscale_into_config,
    query_tailscale_nodes,
    save_fleet_config,
)
from remote_fleet.installer import install_all_harnesses
from remote_fleet.mac_integration import (
    find_rclone_binary,
    install_macos_launch_agent,
    mount_all_machines,
    sync_ssh_config,
)
from remote_fleet.mount import is_mounted
from remote_fleet.transport import FleetTransport


CANDIDATE_LAN_HOSTS: Dict[str, List[str]] = {
    "spark01": ["spark01.local", "spark01"],
    "spark03": ["spark03.local", "spark03"],
    "spark04": ["spark04.local", "spark04"],
    "pc": ["danlin-hud.local", "danlin-hud", "pc.local", "pc"],
}

# Known Tailscale fallback IPs from the tailnet (`danlinyu@gmail.com`)
KNOWN_TAILSCALE_IPS: Dict[str, str] = {
    "mac-mini": "100.71.43.59",
    "spark01": "100.85.241.120",
    "spark03": "100.87.248.70",
    "spark04": "100.119.255.85",
    "pc": "100.93.148.108",  # danlin-hud (Windows 11)
}


def _tcp_open(host: str, port: int = 22, timeout: float = 0.8) -> Optional[str]:
    """Return resolved IP if `host:port` accepts TCP within `timeout`, else None."""
    effective_timeout = max(1.3, timeout) if host.lower().endswith(".local") else timeout
    try:
        ip = socket.gethostbyname(host)
        with socket.create_connection((ip, port), timeout=effective_timeout):
            return ip
    except Exception:
        return None


def _host_resolves_or_pings(host: str) -> Optional[str]:
    """Check if a Windows PC resolves via mDNS/DNS or SMB/RDP ports even when SSH port 22 is off."""
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        return None
    for win_port in (22, 445, 3389, 135):
        try:
            with socket.create_connection((ip, win_port), timeout=0.45):
                return ip
        except Exception:
            continue
    return ip


def _get_local_private_ipv4() -> Optional[str]:
    """Detect this Mac's primary Home LAN IPv4 address (e.g. `192.168.68.x`) without sending traffic."""
    for target in ("192.168.68.1", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith(("192.168.", "10.", "172.")):
                return ip
        except Exception:
            continue
    return None


def _query_netbios_name(ip: str, timeout: float = 0.35) -> str:
    """Send a standard 50-byte UDP 137 NetBIOS NBSTAT query to identify a Windows 11 PC by name on Home LAN."""
    packet = (
        b"\x82\x28\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        b"\x20CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00\x00\x21\x00\x01"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ip, 137))
        resp, _ = sock.recvfrom(1024)
        if len(resp) > 57:
            num_names = resp[56]
            offset = 57
            for _ in range(num_names):
                if offset + 18 > len(resp):
                    break
                raw_name = resp[offset : offset + 15].decode("ascii", errors="ignore").strip()
                suffix = resp[offset + 15]
                flags = (resp[offset + 16] << 8) | resp[offset + 17]
                is_group = bool(flags & 0x8000)
                if suffix == 0x00 and not is_group and raw_name:
                    return raw_name
                offset += 18
    except Exception:
        pass
    finally:
        sock.close()
    return ""


def _scan_subnet_for_missing_hosts(
    subnet_prefix: str,
    local_ip: str,
    already_found_ips: set[str],
    ssh_user: str,
) -> Tuple[Dict[str, Tuple[str, str]], Optional[Tuple[str, str]]]:
    """Scan `<subnet_prefix>.1..254` in ~1.2s to find `spark04` or Windows PC `danlin-hud` even without mDNS or Tailscale."""
    discovered: Dict[str, Tuple[str, str]] = {}
    win_no_ssh: Optional[Tuple[str, str]] = None

    def _probe_single_ip(ip: str) -> Tuple[str, bool, bool]:
        if ip == local_ip or ip in already_found_ips:
            return ip, False, False
        ssh_ok = False
        try:
            with socket.create_connection((ip, 22), timeout=0.28):
                ssh_ok = True
        except Exception:
            pass
        win_ok = False
        if not ssh_ok:
            for wp in (445, 3389, 135):
                try:
                    with socket.create_connection((ip, wp), timeout=0.22):
                        win_ok = True
                        break
                except Exception:
                    continue
        return ip, ssh_ok, win_ok

    all_ips = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as pool:
        scan_results = list(pool.map(_probe_single_ip, all_ips))

    for ip, ssh_ok, win_ok in scan_results:
        if ssh_ok:
            # Identify hostname via reverse DNS, NetBIOS, or quick BatchMode SSH `hostname`
            name_guess = ""
            try:
                name_guess = socket.gethostbyaddr(ip)[0].lower()
            except Exception:
                pass
            if not name_guess:
                nb = _query_netbios_name(ip)
                if nb:
                    name_guess = nb.lower()
            if not name_guess:
                proc = subprocess.run(
                    [
                        "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                        "-o", "StrictHostKeyChecking=accept-new", f"{ssh_user}@{ip}", "hostname",
                    ],
                    capture_output=True, text=True, timeout=4, check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    name_guess = proc.stdout.strip().lower()

            for slot in ("spark01", "spark03", "spark04"):
                if slot in name_guess and slot not in discovered:
                    discovered[slot] = (ip, ip)
            if ("danlin-hud" in name_guess or "danlindep" in name_guess) and "pc" not in discovered:
                discovered["pc"] = (ip, ip)

        elif win_ok and win_no_ssh is None:
            nb = _query_netbios_name(ip).lower()
            rdns = ""
            try:
                rdns = socket.gethostbyaddr(ip)[0].lower()
            except Exception:
                pass
            label = nb or rdns
            if "danlin-hud" in label or "danlin" in label or not label:
                win_no_ssh = (label or ip, ip)

    return discovered, win_no_ssh


def ensure_macos_tailscale_connected() -> bool:
    """Check if Tailscale is installed & running on this Mac. Returns True if running."""
    ts_bin = find_tailscale_binary()
    if not ts_bin:
        print(
            "  [HOME LAN MODE] Tailscale is not installed on this Mac Mini — using pure Home LAN "
            "(Tailscale is completely optional while at home)."
        )
        return False
    try:
        proc = subprocess.run(
            [ts_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            state = str(data.get("BackendState") or "")
            if state == "Running":
                print("  [TAILSCALE] Local Tailscale is connected and running.")
                return True
            print(f"  [TAILSCALE] Local Tailscale state is '{state}' — using Home LAN.")
    except Exception:
        pass
    return False


def scan_arp_neighbors() -> List[Tuple[str, str]]:
    """Return `(hostname_or_ip, ip)` entries from `arp -a`."""
    if not shutil.which("arp"):
        return []
    try:
        proc = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=3, check=False)
    except Exception:
        return []
    out: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        m = re.match(r"^(\S+)\s+\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]+)", line.strip())
        if m:
            hname, ip, _ = m.groups()
            out.append((ip if hname == "?" else hname, ip))
    return out


def probe_home_lan_candidates(
    ssh_user: str = "danlinyu", tailscale_running: bool = False
) -> Tuple[Dict[str, Tuple[str, str]], Optional[Tuple[str, str]]]:
    """Probe `spark01`, `spark03`, `spark04`, and `danlin-hud` (`pc`) on Home LAN."""
    found: Dict[str, Tuple[str, str]] = {}
    tasks: List[Tuple[str, str]] = []
    for slot, names in CANDIDATE_LAN_HOSTS.items():
        for name in names:
            tasks.append((slot, name))

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [(slot, name, pool.submit(_tcp_open, name, 22, 0.8)) for slot, name in tasks]
        for slot, name, fut in futs:
            ip = fut.result()
            if ip and slot not in found:
                found[slot] = (name, ip)

    for hname, ip in scan_arp_neighbors():
        low = hname.lower()
        for slot in ("spark01", "spark03", "spark04"):
            if slot not in found and slot in low and _tcp_open(ip, 22, 0.5):
                found[slot] = (hname if hname != ip else ip, ip)
        if "pc" not in found and ("danlin-hud" in low or "danlindep" in low):
            if _tcp_open(ip, 22, 0.5):
                found["pc"] = (hname, ip)

    pc_alive_no_ssh: Optional[Tuple[str, str]] = None
    if "pc" not in found:
        pc_cands = ["danlin-hud.local", "danlin-hud"]
        if tailscale_running:
            pc_cands.append(KNOWN_TAILSCALE_IPS["pc"])
        for pc_cand in pc_cands:
            ip = _host_resolves_or_pings(pc_cand)
            if ip:
                pc_alive_no_ssh = (pc_cand, ip)
                break

    # If spark04 or pc wasn't found via mDNS/ARP yet, do a fast 1.2s /24 Home LAN subnet scan!
    missing_slots = {s for s in ("spark01", "spark03", "spark04", "pc") if s not in found}
    if missing_slots:
        local_ip = _get_local_private_ipv4()
        if local_ip:
            prefix = ".".join(local_ip.split(".")[:3])
            known_ips = {ip for _, ip in found.values()}
            sub_found, sub_win = _scan_subnet_for_missing_hosts(prefix, local_ip, known_ips, ssh_user)
            for slot, val in sub_found.items():
                if slot not in found:
                    found[slot] = val
            if pc_alive_no_ssh is None and sub_win is not None:
                pc_alive_no_ssh = sub_win

    return found, pc_alive_no_ssh


def ensure_local_ssh_key() -> Path:
    """Ensure `~/.ssh/id_ed25519` (or existing `id_rsa`) exists on the Mac Mini."""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        ssh_dir.chmod(0o700)

    for existing_name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        cand = ssh_dir / existing_name
        if cand.is_file() and Path(f"{cand}.pub").is_file():
            return cand

    key_file = ssh_dir / "id_ed25519"
    print(f"  Generating new SSH key at {key_file}...")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_file)],
        check=True,
    )
    return key_file


def check_batch_ssh(user: str, host: str, port: int = 22) -> bool:
    target = f"{user}@{host}" if user else host
    proc = subprocess.run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=4",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(port),
            target,
            "echo ok",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return proc.returncode == 0 and "ok" in proc.stdout


def copy_ssh_key_interactive(
    user: str, host: str, key_file: Path, port: int = 22, is_windows: bool = False
) -> bool:
    """Copy SSH public key to a Linux Spark or Windows 11 PC."""
    target = f"{user}@{host}" if user else host
    pub_file = Path(f"{key_file}.pub")
    pub_text = pub_file.read_text(encoding="utf-8").strip()

    print(f"\n  -> Authorizing SSH key on {target} (enter password for {target} if prompted):")
    if is_windows:
        # Windows OpenSSH requires writing administrators_authorized_keys when user is an Administrator
        ps_cmd = (
            f"$pub = '{pub_text}'; "
            "$uDir = Join-Path $HOME '.ssh'; New-Item -ItemType Directory -Force -Path $uDir | Out-Null; "
            "Add-Content -Path (Join-Path $uDir 'authorized_keys') -Value $pub; "
            "$adm = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'; "
            "if (Test-Path 'C:\\ProgramData\\ssh') { "
            "  Add-Content -Path $adm -Value $pub -ErrorAction SilentlyContinue; "
            "  icacls.exe $adm /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null "
            "}"
        )
        rc = subprocess.call(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-p",
                str(port),
                target,
                f"powershell -NoProfile -Command \"{ps_cmd}\"",
            ]
        )
        return rc == 0 and check_batch_ssh(user, host, port)

    if shutil.which("ssh-copy-id"):
        rc = subprocess.call(
            [
                "ssh-copy-id",
                "-i",
                str(pub_file),
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-p",
                str(port),
                target,
            ]
        )
        return rc == 0 and check_batch_ssh(user, host, port)

    remote_cmd = (
        f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"grep -qxF {shlex.quote(pub_text)} ~/.ssh/authorized_keys 2>/dev/null || "
        f"echo {shlex.quote(pub_text)} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    )
    rc = subprocess.call(["ssh", "-p", str(port), target, remote_cmd])
    return rc == 0 and check_batch_ssh(user, host, port)


def cleanup_duplicate_spark_tailscale_if_present(user: str, host: str) -> None:
    """If both userspace Tailscale backup AND newly-installed apt `tailscaled` exist, remove the duplicate apt daemon."""
    target = f"{user}@{host}" if user else host
    check_cmd = (
        "[ -f \"$HOME/.local/bin/tailscale.userspace.bak\" ] && [ -x /usr/bin/tailscale ] && echo DUPLICATE || echo CLEAN"
    )
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", target, check_cmd],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if "DUPLICATE" not in proc.stdout:
        return

    print(
        f"\n  -> Cleaning up redundant second Tailscale (`{host.split('.')[0]}-1`) on {target} "
        f"and restoring original Tailscale..."
    )
    cleanup_cmd = (
        "sudo /usr/bin/tailscale logout 2>/dev/null || true; "
        "sudo systemctl disable --now tailscaled 2>/dev/null || true; "
        "sudo apt-get purge -y tailscale tailscale-archive-keyring >/dev/null 2>&1 || true; "
        "sudo rm -rf /var/lib/tailscale /etc/apt/sources.list.d/tailscale.list; "
        "if [ -f \"$HOME/.local/bin/tailscale.userspace.bak\" ]; then "
        "  mv \"$HOME/.local/bin/tailscale.userspace.bak\" \"$HOME/.local/bin/tailscale\"; "
        "  chmod +x \"$HOME/.local/bin/tailscale\"; "
        "fi; "
        "echo '  [CLEANED] Removed duplicate system tailscaled and restored original ~/.local/bin/tailscale'"
    )
    subprocess.call(["ssh", "-t", target, cleanup_cmd])


def ensure_rclone_installed_macos() -> Optional[str]:
    """Find `rclone` or download official standalone macOS binary into `~/.local/bin/rclone` (zero Homebrew needed!)."""
    existing = find_rclone_binary()
    if existing:
        return existing

    if platform.system().lower() != "darwin":
        return None

    arch = platform.machine().lower()
    zip_arch = "arm64" if arch in ("arm64", "aarch64") else "amd64"
    url = f"https://downloads.rclone.org/rclone-current-osx-{zip_arch}.zip"
    dest_dir = Path.home() / ".local" / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_bin = dest_dir / "rclone"

    print(f"  Downloading standalone macOS `rclone` ({zip_arch}) -> {dest_bin} (no Homebrew needed)...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "rclone.zip"
            urllib.request.urlretrieve(url, str(zip_path))
            with zipfile.ZipFile(zip_path, "r") as zf:
                member = next((n for n in zf.namelist() if n.endswith("/rclone")), None)
                if not member:
                    raise RuntimeError("rclone binary not found inside archive")
                with zf.open(member) as src, open(dest_bin, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        dest_bin.chmod(dest_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if shutil.which("xattr"):
            subprocess.run(["xattr", "-c", str(dest_bin)], capture_output=True, check=False)
        return str(dest_bin)
    except Exception as exc:
        print(f"  ! Could not auto-download rclone ({exc}). You can download it from https://rclone.org/downloads/")
        return None


def _print_windows_pc_setup_instructions(pc_user: str, pub_key: str) -> None:
    print("\n  --- One-Time Windows 11 PC (`danlin-hud`) SSH Enablement ---")
    print("  Windows 11 disables OpenSSH Server (`sshd` port 22) by default. To make `danlin-hud` appear")
    print("  alongside `spark01`, `spark03`, and `spark04`, open **PowerShell as Administrator** on `danlin-hud`")
    print("  once and paste these 4 lines:\n")
    print("    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0")
    print("    Start-Service sshd; Set-Service -Name sshd -StartupType 'Automatic'")
    print(f"    $k = \"{pub_key}\"")
    print(
        "    New-Item -Force -ItemType Directory $HOME\\.ssh | Out-Null; Add-Content $HOME\\.ssh\\authorized_keys $k; "
        "Add-Content C:\\ProgramData\\ssh\\administrators_authorized_keys $k; "
        "icacls C:\\ProgramData\\ssh\\administrators_authorized_keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F'"
    )
    print(f"\n  (If your Windows username on `danlin-hud` is different from `{pc_user}`, re-run with `--pc-user <win_user>`.)")


def run_bootstrap(
    ssh_user: str = "danlinyu",
    pc_user: str = "",
    extra_hosts: Optional[List[str]] = None,
    interactive_ssh_copy: bool = True,
    fix_spark_tailscale: bool = False,
    cleanup_duplicate_tailscale: bool = True,
    mount_dir: Optional[Path] = None,
) -> int:
    """Run full turnkey discovery, duplicate Tailscale cleanup, SSH key setup, standalone rclone install, and Mac mounting."""
    effective_pc_user = (pc_user or ssh_user).strip()
    print("=== Remote Fleet Turnkey Bootstrap ===")
    print(f"Spark SSH user: {ssh_user} | Windows PC (`danlin-hud`) SSH user: {effective_pc_user}\n")

    # 1. Check local Tailscale + scan Home LAN (mDNS + ARP + /24 subnet)
    print("[1/5] Scanning Home Network (spark01, spark03, spark04, danlin-hud)...")
    ts_running = ensure_macos_tailscale_connected() if platform.system().lower() == "darwin" else False

    lan_hits, pc_alive_no_ssh = probe_home_lan_candidates(
        ssh_user=ssh_user, tailscale_running=ts_running
    )
    for slot, (h, ip) in sorted(lan_hits.items()):
        print(f"  [LAN FOUND] {slot:<10} -> {h} ({ip}:22)")
    if pc_alive_no_ssh and "pc" not in lan_hits:
        print(f"  [LAN PC]    danlin-hud -> {pc_alive_no_ssh[1]} (online on Home LAN; Windows SSH port 22 not yet enabled)")

    local_hostname = socket.gethostname().removesuffix(".local")
    pc_default_lan = (
        lan_hits["pc"][0]
        if "pc" in lan_hits
        else (pc_alive_no_ssh[1] if pc_alive_no_ssh else "danlin-hud.local")
    )
    machines: List[MachineConfig] = [
        MachineConfig(
            id="mac-mini",
            name="Mac Mini",
            lan_host=f"{local_hostname}.local",
            tailscale_host=KNOWN_TAILSCALE_IPS["mac-mini"],
            os="macos" if platform.system().lower() == "darwin" else platform.system().lower(),
            default_roots=["~/Projects", "~/Developer", "~"],
            tags=["macos", "interface"],
            hostnames=[local_hostname, "danlins-mac-mini", "mac-mini", "macmini"],
            description="Local Mac Mini interface computer",
        ),
        MachineConfig(
            id="spark01",
            name="DGX Spark 01",
            lan_host=lan_hits.get("spark01", ("spark01.local", ""))[0],
            tailscale_host=KNOWN_TAILSCALE_IPS["spark01"],
            user=ssh_user,
            os="linux",
            default_roots=["~/projects", "~/workspace", "~"],
            tags=["linux", "gpu", "spark"],
            hostnames=["spark01", "spark-1", "spark1", KNOWN_TAILSCALE_IPS["spark01"]],
        ),
        MachineConfig(
            id="spark03",
            name="DGX Spark 03",
            lan_host=lan_hits.get("spark03", ("spark03.local", ""))[0],
            tailscale_host=KNOWN_TAILSCALE_IPS["spark03"],
            user=ssh_user,
            os="linux",
            default_roots=["~/projects", "~/workspace", "~"],
            tags=["linux", "gpu", "spark"],
            hostnames=["spark03", "spark-3", "spark3", KNOWN_TAILSCALE_IPS["spark03"]],
        ),
        MachineConfig(
            id="spark04",
            name="DGX Spark 04",
            lan_host=lan_hits.get("spark04", ("spark04.local", ""))[0],
            tailscale_host=KNOWN_TAILSCALE_IPS["spark04"],
            user=ssh_user,
            os="linux",
            default_roots=["~/projects", "~/workspace", "~"],
            tags=["linux", "gpu", "spark"],
            hostnames=["spark04", "spark-4", "spark4", KNOWN_TAILSCALE_IPS["spark04"]],
        ),
        MachineConfig(
            id="pc",
            name="Home PC (danlin-hud)",
            lan_host=pc_default_lan,
            tailscale_host=KNOWN_TAILSCALE_IPS["pc"],
            user=effective_pc_user,
            os="windows",
            default_roots=["~/projects", "~/source/repos", "~"],
            tags=["windows", "pc", "workstation"],
            hostnames=["danlin-hud", "pc", KNOWN_TAILSCALE_IPS["pc"]],
        ),
    ]

    if extra_hosts:
        for spec in extra_hosts:
            if "=" in spec:
                mid, hval = spec.split("=", 1)
                mid, hval = mid.strip(), hval.strip()
                existing = next((x for x in machines if x.id == mid or mid in x.hostnames), None)
                if existing:
                    existing.lan_host = hval
                else:
                    machines.append(
                        MachineConfig(
                            id=mid,
                            name=mid,
                            lan_host=hval,
                            tailscale_host=mid,
                            user=ssh_user,
                            os="auto",
                        )
                    )

    # Do NOT auto-append duplicate `spark01-1` / `spark03-1` or `danlindep` unless explicitly added
    cfg = FleetConfig(machines=machines, include_unlisted_tailscale_peers=False)
    ts_nodes = query_tailscale_nodes()
    ts_self = None
    if ts_nodes:
        # Filter out transient `-1` duplicate nodes when merging
        clean_ts_nodes = [n for n in ts_nodes if not re.search(r"^spark0\d-\d+$", n.hostname.lower())]
        cfg, ts_self = merge_tailscale_into_config(cfg, clean_ts_nodes)
        print(f"  [TAILSCALE] Synced {len(clean_ts_nodes)} Tailscale peer(s)")

    saved_cfg = save_fleet_config(cfg)
    print(f"  Saved fleet config -> {saved_cfg}")

    # Remove stale ~/RemoteComputers/spark02 directory from earlier run if empty & unmounted
    target_mount_dir = (mount_dir or (Path.home() / "RemoteComputers")).expanduser().resolve()
    stale_spark02 = target_mount_dir / "spark02"
    if stale_spark02.is_dir() and not is_mounted(stale_spark02):
        try:
            stale_spark02.rmdir()
            print(f"  Removed unused folder {stale_spark02}")
        except OSError:
            pass

    # 2. SSH Key check + duplicate Tailscale cleanup on Sparks + Windows PC check
    print("\n[2/5] Verifying SSH keys & cleaning up duplicate Tailscale nodes if needed...")
    key_file = ensure_local_ssh_key()
    pub_key_str = Path(f"{key_file}.pub").read_text(encoding="utf-8").strip()
    transport = FleetTransport(cfg, ts_self)
    ep_map = transport.resolve_all_endpoints(cfg.machines, force_refresh=True)

    for m in cfg.machines:
        ep = ep_map[m.id]
        if ep.transport == "local":
            print(f"  [LOCAL] {m.id:<10} (this Mac Mini)")
            continue
        if not ep.reachable:
            print(f"  [OFF]   {m.id:<10} (port 22 not reachable at LAN={m.lan_host} or Tailscale={m.tailscale_host})")
            if m.id == "pc":
                if pc_alive_no_ssh:
                    print(f"          Detected `danlin-hud` online at {pc_alive_no_ssh[1]}, but SSH port 22 is closed.")
                _print_windows_pc_setup_instructions(m.user, pub_key_str)
            continue

        if check_batch_ssh(m.user, ep.host, m.port):
            print(f"  [OK]    {m.id:<10} passwordless SSH works ({m.user}@{ep.host} via {ep.transport.upper()})")
        elif interactive_ssh_copy and sys.stdin.isatty():
            ok = copy_ssh_key_interactive(
                m.user, ep.host, key_file, m.port, is_windows=(m.os == "windows")
            )
            print(f"  [{'OK' if ok else 'WARN'}]  {m.id:<10} key setup {'succeeded' if ok else 'incomplete'}")
        else:
            print(f"  [AUTH]  {m.id:<10} needs SSH key on {m.user}@{ep.host}")

        if cleanup_duplicate_tailscale and m.id.startswith("spark") and check_batch_ssh(m.user, ep.host, m.port):
            cleanup_duplicate_spark_tailscale_if_present(m.user, ep.host)

    # 3. Ensure rclone is installed (downloads standalone binary to ~/.local/bin/rclone if Homebrew is absent!)
    print("\n[3/5] Ensuring `rclone` is installed on this Mac (no Homebrew required)...")
    rclone_path = ensure_rclone_installed_macos()
    print(f"  rclone ready: {rclone_path or 'not installed'}")

    # 4. Install across all harnesses + ~/.ssh/config
    print("\n[4/5] Updating Antigravity, Claude, Codex, Kimi, DSH & ~/.ssh/config...")
    install_all_harnesses()
    ssh_cfg = sync_ssh_config(cfg, ts_self)
    print(f"  Updated {ssh_cfg} (hosts: spark01, spark03, spark04, pc)")

    # 5. Mount ~/RemoteComputers/
    print("\n[5/5] Mounting online computers under ~/RemoteComputers/...")
    summary = mount_all_machines(cfg, FleetTransport(cfg, ts_self), base_dir=target_mount_dir)
    for r in summary["results"]:
        mid = r.get("machine_id", "?")
        if r.get("skipped"):
            continue
        if r.get("ok"):
            print(f"  [MOUNTED] {mid:<10} -> {r.get('mount_point')}")
        else:
            print(f"  [INFO]    {mid:<10} -> {r.get('error')}")

    if platform.system().lower() == "darwin":
        plist = install_macos_launch_agent(base_dir=target_mount_dir)
        print(f"\nUpdated macOS login auto-mounter: {plist}")

    print("\n=== All Set! ===")
    print(f"1. Open `{target_mount_dir}` in Finder (`open ~/RemoteComputers`) and drag it to your Finder Sidebar.")
    print("2. In your Tailscale web console (`console.tailscale.com/admin/machines`), click `... -> Remove` on `spark01-1` and `spark03-1` if still listed as logged-out.")
    print("3. Quit (Cmd+Q) and reopen Antigravity, Claude, Codex, and Kimi once — then pick `RemoteComputers/spark01`, `spark03`, `spark04`, or `pc`!")
    return 0
