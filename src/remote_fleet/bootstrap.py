"""Turnkey Home LAN & Tailscale Bootstrapper (`fleet bootstrap` / `./bootstrap_mac.sh`).

Runs on the user's Mac Mini (either directly in Terminal or via a Local Antigravity session) to:
1. Auto-scan the Home LAN (mDNS `.local`, bare hostnames, and ARP table) + Tailscale for
   `spark01`, `spark02`, `spark03`, and `pc`.
2. Ensure `~/.ssh/id_ed25519` exists and run `ssh-copy-id` to any reachable computer that doesn't
   yet accept passwordless SSH.
3. Diagnose & optionally repair userspace-shadowed Tailscale (`/home/<user>/.local/bin/tailscale`
   with missing `tailscaled.service`) on `spark01..03`.
4. Install `rclone` (via Homebrew if missing on macOS), write `~/.config/remote-fleet/fleet.json`,
   update `~/.ssh/config`, install MCP + Skills across Antigravity / Claude / Codex / Kimi / DSH,
   and mount `~/RemoteComputers/`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from remote_fleet.config import (
    FleetConfig,
    MachineConfig,
    default_home_fleet_config,
    merge_tailscale_into_config,
    query_tailscale_nodes,
    save_fleet_config,
)
from remote_fleet.installer import install_all_harnesses
from remote_fleet.mac_integration import (
    install_macos_launch_agent,
    mount_all_machines,
    sync_ssh_config,
)
from remote_fleet.transport import FleetTransport


# Candidate LAN hostnames to probe automatically for each logical slot
CANDIDATE_LAN_HOSTS: Dict[str, List[str]] = {
    "spark01": ["spark01.local", "spark01", "spark-1.local", "spark-1", "spark1.local", "spark1"],
    "spark02": ["spark02.local", "spark02", "spark-2.local", "spark-2", "spark2.local", "spark2"],
    "spark03": ["spark03.local", "spark03", "spark-3.local", "spark-3", "spark3.local", "spark3"],
    "pc": ["pc.local", "pc", "desktop.local", "workstation.local"],
}


def _tcp_open(host: str, port: int = 22, timeout: float = 0.6) -> Optional[str]:
    """Return resolved IP if `host:port` accepts TCP within `timeout`, else None."""
    try:
        ip = socket.gethostbyname(host)
        with socket.create_connection((ip, port), timeout=timeout):
            return ip
    except Exception:
        return None


def scan_arp_ssh_neighbors(timeout: float = 0.45) -> List[Tuple[str, str]]:
    """Inspect `arp -a` table for local LAN neighbors that have TCP port 22 open.

    Returns list of `(hostname_or_ip, ip)`.
    """
    if not shutil.which("arp"):
        return []
    try:
        proc = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=3, check=False)
    except Exception:
        return []

    entries: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        # macOS/BSD format: `spark01.lan (192.168.1.51) at aa:bb:cc...`
        m = re.match(r"^(\S+)\s+\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+([0-9a-fA-F:]+)", line.strip())
        if m:
            hname, ip, _mac = m.groups()
            if hname == "?":
                hname = ip
            entries.append((hname, ip))

    if not entries:
        return []

    ssh_neighbors: List[Tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(16, len(entries))) as pool:
        futs = [(h, ip, pool.submit(_tcp_open, ip, 22, timeout)) for h, ip in entries]
        for h, ip, fut in futs:
            if fut.result():
                ssh_neighbors.append((h, ip))
    return ssh_neighbors


def probe_home_lan_candidates() -> Dict[str, Tuple[str, str]]:
    """Probe `CANDIDATE_LAN_HOSTS` in parallel. Returns `{slot: (winning_host, resolved_ip)}`."""
    found: Dict[str, Tuple[str, str]] = {}
    tasks: List[Tuple[str, str]] = []
    for slot, names in CANDIDATE_LAN_HOSTS.items():
        for name in names:
            tasks.append((slot, name))

    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [(slot, name, pool.submit(_tcp_open, name, 22, 0.6)) for slot, name in tasks]
        for slot, name, fut in futs:
            ip = fut.result()
            if ip and slot not in found:
                found[slot] = (name, ip)

    # Also match ARP neighbors whose hostname contains `spark` or `pc`
    for hname, ip in scan_arp_ssh_neighbors():
        low = hname.lower()
        for idx in ("1", "2", "3"):
            slot = f"spark0{idx}"
            if slot not in found and (f"spark0{idx}" in low or f"spark-{idx}" in low or f"spark{idx}" in low):
                found[slot] = (hname if hname != ip else ip, ip)
        if "pc" not in found and ("pc" in low or "desktop" in low):
            found["pc"] = (hname if hname != ip else ip, ip)

    return found


def ensure_local_ssh_key() -> Path:
    """Ensure `~/.ssh/id_ed25519` exists on the Mac Mini."""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        ssh_dir.chmod(0o700)

    key_file = ssh_dir / "id_ed25519"
    if not key_file.exists():
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


def copy_ssh_key_interactive(user: str, host: str, key_file: Path, port: int = 22) -> bool:
    """Run `ssh-copy-id` interactively so the user types their password once to enable key auth."""
    target = f"{user}@{host}" if user else host
    pub_file = Path(f"{key_file}.pub")
    print(f"\n  -> Setting up passwordless SSH key on {target} (enter password for {target} if prompted):")
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

    # Fallback if ssh-copy-id is absent
    pub_text = pub_file.read_text(encoding="utf-8").strip()
    remote_cmd = (
        f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"grep -qxF {shlex.quote(pub_text)} ~/.ssh/authorized_keys 2>/dev/null || "
        f"echo {shlex.quote(pub_text)} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
    )
    rc = subprocess.call(["ssh", "-p", str(port), target, remote_cmd])
    return rc == 0 and check_batch_ssh(user, host, port)


def diagnose_and_fix_spark_tailscale(user: str, host: str, fix: bool = False) -> Dict[str, Any]:
    """Inspect a Spark's Tailscale installation (detecting `~/.local/bin/tailscale` shadowing & missing `tailscaled.service`)."""
    target = f"{user}@{host}" if user else host
    probe_script = (
        "echo SYS_BIN=$( [ -x /usr/bin/tailscale ] && echo yes || echo no ); "
        "echo USER_BIN=$( [ -x \"$HOME/.local/bin/tailscale\" ] && echo yes || echo no ); "
        "echo SERVICE=$( systemctl is-active tailscaled 2>/dev/null || echo missing ); "
        "echo TUN=$( ip -o addr show tailscale0 2>/dev/null | awk '{print $4}' | head -n1 )"
    )
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", target, probe_script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()}

    kv: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    status = {
        "ok": True,
        "host": host,
        "system_tailscale_installed": kv.get("SYS_BIN") == "yes",
        "userspace_local_bin_shadow": kv.get("USER_BIN") == "yes",
        "tailscaled_service": kv.get("SERVICE", "missing"),
        "tailscale_ip": kv.get("TUN", ""),
    }

    if fix and (not status["system_tailscale_installed"] or status["tailscaled_service"] != "active"):
        print(f"\n  -> Installing/enabling system-level `tailscaled` on {target} (sudo password on {host} may be requested)...")
        fix_cmd = (
            "if [ -f \"$HOME/.local/bin/tailscale\" ]; then "
            "  mv \"$HOME/.local/bin/tailscale\" \"$HOME/.local/bin/tailscale.userspace.bak\"; "
            "  echo 'Moved shadowing ~/.local/bin/tailscale -> ~/.local/bin/tailscale.userspace.bak'; "
            "fi; "
            "if [ ! -x /usr/bin/tailscale ]; then "
            "  curl -fsSL https://tailscale.com/install.sh | sh; "
            "fi; "
            "sudo systemctl enable --now tailscaled && sudo /usr/bin/tailscale up --ssh"
        )
        subprocess.call(["ssh", "-t", target, fix_cmd])

    return status


def run_bootstrap(
    ssh_user: str = "danlinyu",
    extra_hosts: Optional[List[str]] = None,
    interactive_ssh_copy: bool = True,
    fix_spark_tailscale: bool = False,
    mount_dir: Optional[Path] = None,
) -> int:
    """Run full turnkey discovery, SSH key setup, harness installation, and Mac mounting."""
    print("=== Remote Fleet Turnkey Bootstrap ===")
    print(f"Default remote SSH username: {ssh_user}\n")

    # 1. Discover LAN hosts
    print("[1/5] Scanning Home Network (mDNS + DNS + ARP) and Tailscale...")
    lan_hits = probe_home_lan_candidates()
    for slot, (h, ip) in sorted(lan_hits.items()):
        print(f"  [LAN FOUND] {slot:<10} -> {h} ({ip}:22)")

    arp_all = scan_arp_ssh_neighbors()
    known_ips = {ip for _, ip in lan_hits.values()}
    unassigned_arp = [(h, ip) for h, ip in arp_all if ip not in known_ips]
    for h, ip in unassigned_arp:
        print(f"  [LAN SSH]   neighbor   -> {h} ({ip}:22)")

    # Build updated FleetConfig with user's actual naming (`spark01`, `spark02`, `spark03`, `pc`, `mac-mini`)
    local_hostname = socket.gethostname().removesuffix(".local")
    machines: List[MachineConfig] = [
        MachineConfig(
            id="mac-mini",
            name="Mac Mini",
            lan_host=f"{local_hostname}.local",
            tailscale_host=local_hostname,
            os="macos" if platform.system().lower() == "darwin" else platform.system().lower(),
            default_roots=["~/Projects", "~/Developer", "~"],
            tags=["macos", "interface"],
            hostnames=[local_hostname, "mac-mini", "macmini"],
            description="Local Mac Mini interface computer",
        ),
        MachineConfig(
            id="spark01",
            name="DGX Spark 01",
            lan_host=lan_hits.get("spark01", ("spark01.local", ""))[0],
            tailscale_host="spark01",
            user=ssh_user,
            os="linux",
            default_roots=["~/projects", "~/workspace", "~"],
            tags=["linux", "gpu", "spark"],
            hostnames=["spark01", "spark-1", "spark1"],
        ),
        MachineConfig(
            id="spark02",
            name="DGX Spark 02",
            lan_host=lan_hits.get("spark02", ("spark02.local", ""))[0],
            tailscale_host="spark02",
            user=ssh_user,
            os="linux",
            default_roots=["~/projects", "~/workspace", "~"],
            tags=["linux", "gpu", "spark"],
            hostnames=["spark02", "spark-2", "spark2"],
        ),
        MachineConfig(
            id="spark03",
            name="DGX Spark 03",
            lan_host=lan_hits.get("spark03", ("spark03.local", ""))[0],
            tailscale_host="spark03",
            user=ssh_user,
            os="linux",
            default_roots=["~/projects", "~/workspace", "~"],
            tags=["linux", "gpu", "spark"],
            hostnames=["spark03", "spark-3", "spark3"],
        ),
        MachineConfig(
            id="pc",
            name="Home PC",
            lan_host=lan_hits.get("pc", ("pc.local", ""))[0],
            tailscale_host="pc",
            user=ssh_user,
            os="auto",
            default_roots=["~/projects", "~/source/repos", "~"],
            tags=["pc", "workstation"],
            hostnames=["pc", "desktop-pc"],
        ),
    ]

    # Allow user to pass explicit `--host id=host` overrides on CLI
    if extra_hosts:
        for spec in extra_hosts:
            if "=" in spec:
                mid, hval = spec.split("=", 1)
                mid = mid.strip()
                hval = hval.strip()
                existing = next((x for x in machines if x.id == mid), None)
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

    cfg = FleetConfig(machines=machines)
    ts_nodes = query_tailscale_nodes()
    ts_self = None
    if ts_nodes:
        cfg, ts_self = merge_tailscale_into_config(cfg, ts_nodes)
        print(f"  [TAILSCALE] Merged {len(ts_nodes)} Tailscale node(s)")

    saved_cfg = save_fleet_config(cfg)
    print(f"  Saved fleet config -> {saved_cfg}")

    # 2. SSH Key check + interactive ssh-copy-id for reachable machines
    print("\n[2/5] Verifying passwordless SSH keys to reachable computers...")
    key_file = ensure_local_ssh_key()
    transport = FleetTransport(cfg, ts_self)
    ep_map = transport.resolve_all_endpoints(cfg.machines, force_refresh=True)

    for m in cfg.machines:
        ep = ep_map[m.id]
        if ep.transport == "local":
            print(f"  [LOCAL] {m.id:<10} (this computer)")
            continue
        if not ep.reachable:
            print(f"  [OFF]   {m.id:<10} ({m.lan_host} not currently reachable on port {m.port})")
            continue
        if check_batch_ssh(m.user, ep.host, m.port):
            print(f"  [OK]    {m.id:<10} passwordless SSH works ({m.user}@{ep.host})")
        elif interactive_ssh_copy and sys.stdin.isatty():
            ok = copy_ssh_key_interactive(m.user, ep.host, key_file, m.port)
            print(f"  [{'OK' if ok else 'WARN'}]  {m.id:<10} key setup {'succeeded' if ok else 'incomplete'}")
        else:
            print(f"  [AUTH]  {m.id:<10} needs SSH key: run `ssh-copy-id {m.user}@{ep.host}`")

        # Diagnose Spark Tailscale state if it's a Spark and SSH works
        if "spark" in m.id and check_batch_ssh(m.user, ep.host, m.port):
            ts_diag = diagnose_and_fix_spark_tailscale(m.user, ep.host, fix=fix_spark_tailscale)
            if ts_diag.get("ok") and not ts_diag.get("system_tailscale_installed"):
                print(
                    f"          Note on {m.id}: only userspace `~/.local/bin/tailscale` is present "
                    f"(no system `tailscaled.service`). Home LAN works now; pass `--fix-spark-tailscale` "
                    f"if you also want system Tailscale installed on {m.id}."
                )

    # 3. Install rclone on macOS if missing and brew exists
    print("\n[3/5] Checking rclone (for native macOS ~/RemoteComputers mounts)...")
    if platform.system().lower() == "darwin" and not shutil.which("rclone"):
        if shutil.which("brew"):
            print("  Installing `rclone` via Homebrew...")
            subprocess.call(["brew", "install", "rclone"])
        else:
            print("  Homebrew not found; skip auto-installing rclone (`brew install rclone`).")
    else:
        print(f"  rclone: {shutil.which('rclone') or 'not installed'}")

    # 4. Install across all harnesses + ~/.ssh/config
    print("\n[4/5] Installing MCP server, Skills, Global Rules & ~/.ssh/config...")
    install_all_harnesses()
    ssh_cfg = sync_ssh_config(cfg, ts_self)
    print(f"  Updated {ssh_cfg} (for Codex SSH & Remote-SSH)")

    # 5. Mount ~/RemoteComputers/
    print("\n[5/5] Mounting reachable computers under ~/RemoteComputers/...")
    target_mount_dir = (mount_dir or (Path.home() / "RemoteComputers")).expanduser().resolve()
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
        print(f"\nInstalled macOS login auto-mounter: {plist}")

    print("\n=== Bootstrap Complete! ===")
    print(f"1. Open Finder -> drag `{target_mount_dir}` into your Finder Favorites sidebar.")
    print("2. Quit (Cmd+Q) and reopen Antigravity, Claude, Codex, and Kimi once.")
    print("3. Click 'Create New Project' / 'New' -> pick any folder inside `RemoteComputers/spark01`, `spark02`, `spark03`, or `pc`!")
    return 0
