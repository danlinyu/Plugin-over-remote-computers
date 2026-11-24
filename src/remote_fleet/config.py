"""Fleet configuration, Tailscale peer discovery, local-self detection, and active workspace state."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
from typing import Any, Dict, List, Optional, Tuple


WORKSPACE_MARKER_FILENAME = ".remote-fleet.json"


@dataclass
class MachineConfig:
    """Configuration for a computer in the Home LAN / Tailscale fleet."""

    id: str
    name: str
    lan_host: str = ""
    tailscale_host: str = ""
    user: str = ""
    port: int = 22
    os: str = "auto"  # "auto" | "linux" | "macos" | "windows"
    default_roots: List[str] = field(default_factory=lambda: ["~/projects", "~"])
    tags: List[str] = field(default_factory=list)
    ssh_identity_file: str = ""
    ssh_extra_args: List[str] = field(default_factory=list)
    hostnames: List[str] = field(default_factory=list)
    description: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MachineConfig":
        return cls(
            id=str(data["id"]).strip(),
            name=str(data.get("name") or data["id"]).strip(),
            lan_host=str(data.get("lan_host") or "").strip(),
            tailscale_host=str(data.get("tailscale_host") or "").strip(),
            user=str(data.get("user") or "").strip(),
            port=int(data.get("port") or 22),
            os=str(data.get("os") or "auto").strip().lower(),
            default_roots=list(data.get("default_roots") or ["~/projects", "~"]),
            tags=list(data.get("tags") or []),
            ssh_identity_file=str(data.get("ssh_identity_file") or "").strip(),
            ssh_extra_args=list(data.get("ssh_extra_args") or []),
            hostnames=list(data.get("hostnames") or []),
            description=str(data.get("description") or "").strip(),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "lan_host": self.lan_host,
            "tailscale_host": self.tailscale_host,
            "os": self.os,
            "default_roots": self.default_roots,
            "tags": self.tags,
        }
        if self.user:
            out["user"] = self.user
        if self.port != 22:
            out["port"] = self.port
        if self.ssh_identity_file:
            out["ssh_identity_file"] = self.ssh_identity_file
        if self.ssh_extra_args:
            out["ssh_extra_args"] = self.ssh_extra_args
        if self.hostnames:
            out["hostnames"] = self.hostnames
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class FleetConfig:
    """Top-level configuration for all machines across Home LAN and Tailscale."""

    machines: List[MachineConfig] = field(default_factory=list)
    prefer_lan: bool = True
    lan_probe_timeout_ms: int = 450
    ssh_connect_timeout_sec: int = 5
    auto_discover_tailscale: bool = True
    include_unlisted_tailscale_peers: bool = True
    fleet_mount_dir: str = "~/Fleet"

    def get_machine(self, machine_id_or_name: str) -> Optional[MachineConfig]:
        needle = machine_id_or_name.strip().lower()
        for m in self.machines:
            if m.id.lower() == needle or m.name.lower() == needle:
                return m
            if any(h.lower() == needle for h in m.hostnames):
                return m
            if m.lan_host and m.lan_host.lower().removesuffix(".local") == needle:
                return m
            if m.tailscale_host and m.tailscale_host.lower().split(".")[0] == needle:
                return m
        return None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FleetConfig":
        raw_machines = data.get("machines") or []
        machines = [MachineConfig.from_dict(item) for item in raw_machines]
        return cls(
            machines=machines,
            prefer_lan=bool(data.get("prefer_lan", True)),
            lan_probe_timeout_ms=int(data.get("lan_probe_timeout_ms", 450)),
            ssh_connect_timeout_sec=int(data.get("ssh_connect_timeout_sec", 5)),
            auto_discover_tailscale=bool(data.get("auto_discover_tailscale", True)),
            include_unlisted_tailscale_peers=bool(data.get("include_unlisted_tailscale_peers", True)),
            fleet_mount_dir=str(data.get("fleet_mount_dir") or "~/Fleet"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prefer_lan": self.prefer_lan,
            "lan_probe_timeout_ms": self.lan_probe_timeout_ms,
            "ssh_connect_timeout_sec": self.ssh_connect_timeout_sec,
            "auto_discover_tailscale": self.auto_discover_tailscale,
            "include_unlisted_tailscale_peers": self.include_unlisted_tailscale_peers,
            "fleet_mount_dir": self.fleet_mount_dir,
            "machines": [m.to_dict() for m in self.machines],
        }


@dataclass
class ActiveProjectState:
    """Tracks which machine and project directory are currently selected."""

    machine_id: str
    remote_path: str
    local_workspace_path: str = ""
    selected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = "state"  # "session" | "env" | "workspace_marker" | "state"

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source: str = "state") -> "ActiveProjectState":
        return cls(
            machine_id=str(data["machine_id"]),
            remote_path=str(data["remote_path"]),
            local_workspace_path=str(data.get("local_workspace_path") or ""),
            selected_at=str(data.get("selected_at") or ""),
            source=source,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "remote_path": self.remote_path,
            "local_workspace_path": self.local_workspace_path,
            "selected_at": self.selected_at,
        }


def get_config_dir() -> Path:
    """Return the configuration directory (`~/.config/remote-fleet` by default)."""
    override = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".config" / "remote-fleet"


def get_config_path() -> Path:
    override = os.environ.get("REMOTE_FLEET_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return get_config_dir() / "fleet.json"


def get_state_path() -> Path:
    return get_config_dir() / "state.json"


def default_home_fleet_config() -> FleetConfig:
    """Default 5-computer fleet template: Mac Mini, PC, and 3 Sparks."""
    return FleetConfig(
        prefer_lan=True,
        lan_probe_timeout_ms=450,
        ssh_connect_timeout_sec=5,
        auto_discover_tailscale=True,
        include_unlisted_tailscale_peers=True,
        fleet_mount_dir="~/Fleet",
        machines=[
            MachineConfig(
                id="mac-mini",
                name="Mac Mini",
                lan_host="mac-mini.local",
                tailscale_host="mac-mini",
                os="macos",
                default_roots=["~/Projects", "~/Developer", "~"],
                tags=["macos", "apple-silicon", "desktop"],
                hostnames=["mac-mini", "macmini", "Mac-mini"],
                description="Mac Mini workstation over Home LAN or Tailscale",
            ),
            MachineConfig(
                id="pc",
                name="Home PC",
                lan_host="pc.local",
                tailscale_host="pc",
                os="auto",
                default_roots=["~/projects", "~/source/repos", "~"],
                tags=["pc", "workstation"],
                hostnames=["pc", "desktop-pc"],
                description="PC (Windows / WSL2 / Linux) over Home LAN or Tailscale",
            ),
            MachineConfig(
                id="spark-1",
                name="DGX Spark 1",
                lan_host="spark01.local",
                tailscale_host="spark01",
                os="linux",
                default_roots=["~/projects", "~/workspace", "~"],
                tags=["linux", "gpu", "spark"],
                hostnames=["spark01", "spark-1", "spark1", "dgx-spark-1"],
                description="Spark node #1 over Home LAN or Tailscale",
            ),
            MachineConfig(
                id="spark-2",
                name="DGX Spark 2",
                lan_host="spark02.local",
                tailscale_host="spark02",
                os="linux",
                default_roots=["~/projects", "~/workspace", "~"],
                tags=["linux", "gpu", "spark"],
                hostnames=["spark02", "spark-2", "spark2", "dgx-spark-2"],
                description="Spark node #2 over Home LAN or Tailscale",
            ),
            MachineConfig(
                id="spark-3",
                name="DGX Spark 3",
                lan_host="spark03.local",
                tailscale_host="spark03",
                os="linux",
                default_roots=["~/projects", "~/workspace", "~"],
                tags=["linux", "gpu", "spark"],
                hostnames=["spark03", "spark-3", "spark3", "dgx-spark-3"],
                description="Spark node #3 over Home LAN or Tailscale",
            ),
        ],
    )


def find_tailscale_binary() -> Optional[str]:
    """Locate the `tailscale` CLI across Linux, macOS App bundle, and Windows."""
    override = os.environ.get("REMOTE_FLEET_TAILSCALE_BIN")
    if override and Path(override).exists():
        return override
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
        r"C:\Program Files\Tailscale\tailscale.exe",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


@dataclass
class TailscaleNodeInfo:
    """Parsed node details from `tailscale status --json`."""

    hostname: str
    dns_name: str
    ips: List[str]
    os: str
    online: bool
    is_self: bool


def _normalize_ts_os(raw_os: str) -> str:
    val = (raw_os or "").strip().lower()
    if val in ("macos", "darwin"):
        return "macos"
    if val == "windows":
        return "windows"
    if val in ("linux", "freebsd", "openbsd"):
        return "linux"
    return "auto"


def parse_tailscale_status_json(payload: Dict[str, Any]) -> List[TailscaleNodeInfo]:
    """Parse `tailscale status --json` output into normalized TailscaleNodeInfo items."""
    nodes: List[TailscaleNodeInfo] = []

    def _extract(node_obj: Dict[str, Any], is_self: bool) -> Optional[TailscaleNodeInfo]:
        if not node_obj:
            return None
        os_raw = str(node_obj.get("OS") or "")
        # Skip mobile phones/tablets unless explicitly desktop/server OS
        if os_raw.lower() in ("ios", "android", "tvos"):
            return None
        hostname = str(node_obj.get("HostName") or "").strip()
        dns_name = str(node_obj.get("DNSName") or "").strip().rstrip(".")
        ips = [str(ip) for ip in (node_obj.get("TailscaleIPs") or []) if ip]
        if not hostname and dns_name:
            hostname = dns_name.split(".")[0]
        if not hostname:
            return None
        online = bool(node_obj.get("Online", True if is_self else False))
        return TailscaleNodeInfo(
            hostname=hostname,
            dns_name=dns_name,
            ips=ips,
            os=_normalize_ts_os(os_raw),
            online=online,
            is_self=is_self,
        )

    self_info = _extract(payload.get("Self") or {}, is_self=True)
    if self_info:
        nodes.append(self_info)

    peers = payload.get("Peer") or {}
    if isinstance(peers, dict):
        for peer_obj in peers.values():
            if isinstance(peer_obj, dict):
                info = _extract(peer_obj, is_self=False)
                if info:
                    nodes.append(info)
    return nodes


def is_tailscale_only_host(host: str) -> bool:
    """Return True if `host` is a Tailscale MagicDNS (`.ts.net`) or Tailscale CGNAT (`100.64.0.0/10`) address."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h.endswith(".ts.net"):
        return True
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        first, second = int(parts[0]), int(parts[1])
        if first == 100 and 64 <= second <= 127:
            return True
    return False


def is_local_tailscale_running(timeout_sec: float = 1.5) -> bool:
    """Return True only if Tailscale is installed on THIS computer and `BackendState == 'Running'`."""
    ts_bin = find_tailscale_binary()
    if not ts_bin:
        return False
    try:
        proc = subprocess.run(
            [ts_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        data = json.loads(proc.stdout)
        return str(data.get("BackendState") or "") == "Running"
    except Exception:
        return False


def query_tailscale_nodes(timeout_sec: float = 2.5) -> List[TailscaleNodeInfo]:
    """Query `tailscale status --json` if Tailscale is installed and running."""
    ts_bin = find_tailscale_binary()
    if not ts_bin:
        return []
    try:
        proc = subprocess.run(
            [ts_bin, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        if str(data.get("BackendState") or "") not in ("Running", ""):
            return []
        return parse_tailscale_status_json(data)
    except Exception:
        return []


def merge_tailscale_into_config(
    config: FleetConfig, ts_nodes: List[TailscaleNodeInfo]
) -> Tuple[FleetConfig, Optional[TailscaleNodeInfo]]:
    """Enrich configured machines with live Tailscale addresses and optionally append unlisted peers."""
    self_node: Optional[TailscaleNodeInfo] = None
    for node in ts_nodes:
        if node.is_self:
            self_node = node
        short_dns = node.dns_name.split(".")[0] if node.dns_name else ""
        matched: Optional[MachineConfig] = None
        for candidate in (node.hostname, short_dns):
            if candidate:
                matched = config.get_machine(candidate)
                if matched:
                    break
        if matched:
            # Prefer full MagicDNS name or IPv4 Tailscale IP if configured tailscale_host is bare/empty
            preferred_ts = node.dns_name or (node.ips[0] if node.ips else node.hostname)
            if not matched.tailscale_host or "." not in matched.tailscale_host:
                matched.tailscale_host = preferred_ts
            if matched.os == "auto" and node.os != "auto":
                matched.os = node.os
            for alias in (node.hostname, short_dns, *node.ips):
                if alias and alias not in matched.hostnames:
                    matched.hostnames.append(alias)
        elif config.include_unlisted_tailscale_peers:
            machine_id = (short_dns or node.hostname).lower()
            preferred_ts = node.dns_name or (node.ips[0] if node.ips else node.hostname)
            config.machines.append(
                MachineConfig(
                    id=machine_id,
                    name=f"{node.hostname} (Tailscale)",
                    lan_host=f"{machine_id}.local",
                    tailscale_host=preferred_ts,
                    os=node.os,
                    default_roots=["~/projects", "~"],
                    tags=["tailscale-discovered", node.os],
                    hostnames=[h for h in [node.hostname, short_dns, *node.ips] if h],
                    description="Auto-discovered via Tailscale",
                )
            )
    return config, self_node


def load_fleet_config(include_tailscale: bool = True) -> Tuple[FleetConfig, Optional[TailscaleNodeInfo]]:
    """Load fleet configuration from disk (or default 5-machine template) and optionally merge Tailscale."""
    cfg_path = get_config_path()
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg = FleetConfig.from_dict(raw)
    else:
        cfg = default_home_fleet_config()

    self_ts: Optional[TailscaleNodeInfo] = None
    if include_tailscale and cfg.auto_discover_tailscale:
        ts_nodes = query_tailscale_nodes()
        if ts_nodes:
            cfg, self_ts = merge_tailscale_into_config(cfg, ts_nodes)
    return cfg, self_ts


def save_fleet_config(config: FleetConfig, path: Optional[Path] = None) -> Path:
    """Persist fleet configuration to `fleet.json`."""
    target = path or get_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")
    return target


def get_local_host_identifiers() -> List[str]:
    """Gather local hostname and IP identifiers to detect if a fleet entry is the current machine."""
    identifiers = set()
    for val in (
        socket.gethostname(),
        platform.node(),
        socket.getfqdn(),
    ):
        if val:
            clean = val.strip().lower()
            identifiers.add(clean)
            identifiers.add(clean.removesuffix(".local"))
            identifiers.add(clean.split(".")[0])

    # On macOS, also check LocalHostName if scutil exists
    if platform.system().lower() == "darwin" and shutil.which("scutil"):
        try:
            proc = subprocess.run(
                ["scutil", "--get", "LocalHostName"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                identifiers.add(proc.stdout.strip().lower())
        except Exception:
            pass

    # Best-effort local IP discovery
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr:
                identifiers.add(addr.lower())
    except Exception:
        pass

    return sorted(x for x in identifiers if x and x not in ("localhost", "127.0.0.1", "::1"))


def is_self_machine(
    machine: MachineConfig, tailscale_self: Optional[TailscaleNodeInfo] = None
) -> bool:
    """Return True if `machine` refers to the computer currently running this process."""
    explicit_self = os.environ.get("REMOTE_FLEET_SELF", "").strip().lower()
    if explicit_self:
        return machine.id.lower() == explicit_self or machine.name.lower() == explicit_self

    if machine.lan_host in ("localhost", "127.0.0.1") or machine.tailscale_host in ("localhost", "127.0.0.1"):
        return True

    local_ids = set(get_local_host_identifiers())
    if tailscale_self:
        local_ids.add(tailscale_self.hostname.lower())
        if tailscale_self.dns_name:
            local_ids.add(tailscale_self.dns_name.lower())
            local_ids.add(tailscale_self.dns_name.lower().split(".")[0])
        for ip in tailscale_self.ips:
            local_ids.add(ip.lower())

    machine_candidates = set()
    machine_candidates.add(machine.id.lower())
    for h in machine.hostnames:
        if h:
            machine_candidates.add(h.lower())
            machine_candidates.add(h.lower().removesuffix(".local"))
            machine_candidates.add(h.lower().split(".")[0])
    if machine.lan_host:
        lh = machine.lan_host.lower()
        machine_candidates.add(lh)
        machine_candidates.add(lh.removesuffix(".local"))
    if machine.tailscale_host:
        th = machine.tailscale_host.lower()
        machine_candidates.add(th)
        machine_candidates.add(th.split(".")[0])

    return bool(local_ids.intersection(machine_candidates))


def _safe_lexical_path(raw: str) -> Path:
    """Resolve parent directory symlinks without calling `lstat` on the leaf mountpoint itself."""
    expanded = Path(os.path.abspath(os.path.expanduser(raw)))
    if str(expanded) == expanded.anchor:
        return expanded
    try:
        return expanded.parent.resolve() / expanded.name
    except OSError:
        return expanded


def find_workspace_marker(start_dir: Optional[Path] = None) -> Optional[Tuple[Path, ActiveProjectState]]:
    """Search `start_dir` and its parents for `.remote-fleet.json`."""
    try:
        cur = (start_dir or Path.cwd()).resolve()
    except OSError:
        return None
    for directory in (cur, *cur.parents):
        marker = directory / WORKSPACE_MARKER_FILENAME
        if marker.is_file():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                state = ActiveProjectState.from_dict(data, source="workspace_marker")
                if not state.local_workspace_path:
                    state.local_workspace_path = str(directory)
                return marker, state
            except Exception:
                continue
    return None


def get_mounts_registry_path() -> Path:
    return get_config_dir() / "mounts.json"


def load_mounts_registry() -> Dict[str, Dict[str, str]]:
    reg_file = get_mounts_registry_path()
    if not reg_file.is_file():
        return {}
    try:
        data = json.loads(reg_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_mounts_registry(mapping: Dict[str, Dict[str, str]]) -> Path:
    reg_file = get_mounts_registry_path()
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    return reg_file


def detect_mounted_machine_path(start_dir: Optional[Path] = None) -> Optional[ActiveProjectState]:
    """If `start_dir` lies inside a mounted machine root (e.g. `~/RemoteComputers/pc/D/foo`), map it back."""
    try:
        cur = (start_dir or Path.cwd()).resolve()
    except OSError:
        return None
    registry = load_mounts_registry()
    resolved_entries: List[Tuple[Path, Dict[str, str]]] = [
        (_safe_lexical_path(local_mount_str), info)
        for local_mount_str, info in registry.items()
    ]
    # Sort longest resolved mount paths first so nested drive mounts (`~/RemoteComputers/pc/D`) take precedence
    resolved_entries.sort(key=lambda pair: len(str(pair[0])), reverse=True)
    for mount_root, info in resolved_entries:
        try:
            rel = cur.relative_to(mount_root)
        except Exception:
            continue
        machine_id = str(info.get("machine_id") or "").strip()
        raw_remote_root = str(info.get("remote_root") or "~")
        trimmed_root = raw_remote_root.rstrip("/")
        if not machine_id:
            continue
        rel_posix = str(rel).replace("\\", "/")
        if info.get("container_only") == "1" and rel_posix not in ("", "."):
            continue
        if rel_posix in ("", "."):
            is_win_drive = len(trimmed_root) == 2 and trimmed_root[0].isalpha() and trimmed_root[1] == ":"
            if is_win_drive or raw_remote_root == "/":
                remote_path = f"{trimmed_root}/"
            else:
                remote_path = trimmed_root
        else:
            remote_path = f"/{rel_posix}" if trimmed_root == "" else f"{trimmed_root}/{rel_posix}"
        return ActiveProjectState(
            machine_id=machine_id,
            remote_path=remote_path,
            local_workspace_path=str(cur),
            source="mounted_folder",
        )
    return None


def load_active_project(cwd: Optional[Path] = None) -> Optional[ActiveProjectState]:
    """Resolve the active remote project from env vars, `.remote-fleet.json`, mounted folder path, or saved state."""
    env_machine = os.environ.get("REMOTE_FLEET_MACHINE", "").strip()
    env_path = os.environ.get("REMOTE_FLEET_PATH", "").strip()
    if env_machine and env_path:
        return ActiveProjectState(
            machine_id=env_machine,
            remote_path=env_path,
            source="env",
        )

    found = find_workspace_marker(cwd)
    if found:
        return found[1]

    mounted_state = detect_mounted_machine_path(cwd)
    if mounted_state:
        return mounted_state

    state_file = get_state_path()
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("machine_id") and data.get("remote_path"):
                return ActiveProjectState.from_dict(data, source="state")
        except Exception:
            return None
    return None


def save_active_project(state: ActiveProjectState) -> Path:
    """Save the active remote project to `~/.config/remote-fleet/state.json`."""
    state_file = get_state_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    return state_file
