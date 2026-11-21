"""Command-line interface (`fleet`) for Remote Fleet."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from remote_fleet import __version__
from remote_fleet.config import (
    ActiveProjectState,
    FleetConfig,
    MachineConfig,
    default_home_fleet_config,
    find_tailscale_binary,
    get_config_path,
    load_active_project,
    load_fleet_config,
    merge_tailscale_into_config,
    query_tailscale_nodes,
    save_active_project,
    save_fleet_config,
)
from remote_fleet.bootstrap import run_bootstrap
from remote_fleet.gui_picker import gui_pick_machine_and_folder
from remote_fleet.installer import install_all_harnesses
from remote_fleet.mac_integration import (
    install_macos_launch_agent,
    mount_all_machines,
    sync_ssh_config,
    unmount_all_machines,
)
from remote_fleet.mcp_server import serve_stdio
from remote_fleet.mount import (
    compute_local_workspace_dir,
    prepare_local_workspace,
    sync_workspace,
    unmount_sshfs,
    watch_workspace,
)
from remote_fleet.picker import interactive_browse_folder, interactive_pick_machine
from remote_fleet.transport import FleetTransport


HARNESS_COMMANDS: Dict[str, List[str]] = {
    "antigravity": ["agy", "."],
    "agy": ["agy", "."],
    "claude": ["claude"],
    "codex": ["codex"],
    "kimi": ["kimi"],
    "dsh": ["dsh"],
    "cursor": ["cursor", "."],
    "code": ["code", "."],
    "open": ["open", "."],
}

MACOS_APP_NAMES: Dict[str, str] = {
    "antigravity": "Antigravity",
    "agy": "Antigravity",
    "cursor": "Cursor",
    "code": "Visual Studio Code",
}


def _resolve_active(
    cfg: FleetConfig, machine_override: Optional[str] = None
) -> Tuple[MachineConfig, str]:
    if machine_override:
        m = cfg.get_machine(machine_override)
        if not m:
            available = ", ".join(x.id for x in cfg.machines)
            raise SystemExit(f"Unknown machine '{machine_override}'. Available: {available}")
        active = load_active_project()
        base = active.remote_path if (active and active.machine_id == m.id) else "~"
        return m, base

    active = load_active_project()
    if not active:
        raise SystemExit(
            "No active project selected. Run `fleet pick` or `fleet use <machine> <path>` first."
        )
    m = cfg.get_machine(active.machine_id)
    if not m:
        raise SystemExit(f"Active project references unknown machine '{active.machine_id}'.")
    return m, active.remote_path


def cmd_init(args: argparse.Namespace) -> int:
    cfg_path = get_config_path()
    if cfg_path.exists() and not args.force:
        print(f"Config already exists at {cfg_path} (pass --force to overwrite).")
        return 0
    cfg = default_home_fleet_config()
    ts_nodes = query_tailscale_nodes()
    if ts_nodes:
        cfg, _ = merge_tailscale_into_config(cfg, ts_nodes)
    save_fleet_config(cfg, cfg_path)
    print(f"Initialized fleet config at {cfg_path} with {len(cfg.machines)} machines:")
    for m in cfg.machines:
        print(f"  - {m.id:<12} ({m.name})  LAN={m.lan_host or '-'}  Tailscale={m.tailscale_host or '-'}")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    ts_bin = find_tailscale_binary()
    if not ts_bin:
        print("Tailscale CLI was not found on this machine.")
        return 1
    nodes = query_tailscale_nodes()
    if not nodes:
        print("No active Tailscale peers returned by `tailscale status --json`. Is Tailscale logged in?")
        return 1

    if args.json:
        print(json.dumps([n.__dict__ for n in nodes], indent=2))
    else:
        print(f"Discovered {len(nodes)} computer(s) via Tailscale:")
        for n in nodes:
            self_badge = " [THIS MACHINE]" if n.is_self else ""
            state_badge = "online" if n.online else "offline"
            ips = ", ".join(n.ips)
            print(f"  - {n.hostname:<16} ({n.os:<7}, {state_badge}){self_badge}  DNS={n.dns_name}  IPs={ips}")

    if args.save:
        cfg, _ = load_fleet_config(include_tailscale=False)
        cfg, _ = merge_tailscale_into_config(cfg, nodes)
        saved = save_fleet_config(cfg)
        print(f"\nSaved merged fleet configuration to {saved}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    active = load_active_project()

    ep_map = transport.resolve_all_endpoints(cfg.machines, force_refresh=True)
    rows = []
    for m in cfg.machines:
        ep = ep_map[m.id]
        row: Dict[str, Any] = {"machine": m.to_dict(), "status": ep.to_dict()}
        if getattr(args, "load", False) and ep.reachable:
            metrics = transport.run_worker_op(m, {"op": "machine_metrics"}, timeout_sec=6)
            if metrics.get("ok"):
                row["load"] = metrics
        rows.append(row)

    if args.json:
        print(
            json.dumps(
                {"machines": rows, "active_project": active.to_dict() if active else None},
                indent=2,
            )
        )
        return 0

    print(f"Fleet Configuration: {get_config_path()}")
    hdr = f"{'ID':<14} {'NAME':<18} {'TRANSPORT':<12} {'ENDPOINT':<22} {'LATENCY':<9}"
    if getattr(args, "load", False):
        hdr += " LOAD / GPUS"
    print(hdr)
    print("-" * (105 if getattr(args, "load", False) else 80))
    for row in rows:
        m = row["machine"]
        st = row["status"]
        lat = f"{st['latency_ms']:.1f}ms" if st["latency_ms"] is not None else "-"
        tr = st["transport"].upper()
        host_disp = "localhost" if st["transport"] == "local" else st["host"]
        line = f"{m['id']:<14} {m['name'][:17]:<18} {tr:<12} {host_disp[:21]:<22} {lat:<9}"
        if getattr(args, "load", False) and "load" in row:
            ld = row["load"]
            load_str = f"cpu={ld.get('load_1m')}/{ld.get('cpu_count')}c"
            if ld.get("gpus"):
                load_str += f" gpu=[{'; '.join(ld['gpus'])}]"
            line += f" {load_str}"
        print(line)

    print("")
    if active:
        print(f"Active Project: {active.machine_id}:{active.remote_path} (source: {active.source})")
        if active.local_workspace_path:
            print(f"Local Workspace: {active.local_workspace_path}")
    else:
        print("Active Project: (none selected — run `fleet pick` or `fleet use <machine> <path>`)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print("=== Remote Fleet Doctor ===")
    print(f"Python:    {sys.version.split()[0]} ({sys.executable})")
    print(f"SSH CLI:   {shutil.which('ssh') or 'MISSING'}")
    print(f"Tailscale: {find_tailscale_binary() or 'not detected'}")
    print(f"rsync:     {shutil.which('rsync') or 'not installed (SSH worker fallback active)'}")
    print(f"sshfs:     {shutil.which('sshfs') or 'not installed (link/sync modes active)'}")
    print(f"Config:    {get_config_path()} ({'exists' if get_config_path().exists() else 'using defaults'})")
    print("")

    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    ep_map = transport.resolve_all_endpoints(cfg.machines, force_refresh=True)
    for m in cfg.machines:
        ep = ep_map[m.id]
        if ep.transport == "local":
            ping = transport.run_worker_op(m, {"op": "ping"})
            print(f"[OK]   {m.id:<12} LOCAL ({ping.get('os')}/{ping.get('arch')}, Python {ping.get('python')})")
        elif ep.reachable:
            ping = transport.run_worker_op(m, {"op": "ping"}, timeout_sec=8)
            if ping.get("ok"):
                print(
                    f"[OK]   {m.id:<12} {ep.transport.upper()} ({ep.host}, {ep.latency_ms:.1f}ms) -> "
                    f"{ping.get('hostname')} ({ping.get('os')}/{ping.get('arch')}, Python {ping.get('python')})"
                )
            else:
                target_spec = f"{m.user}@{ep.host}" if m.user else ep.host
                print(
                    f"[AUTH] {m.id:<12} TCP port {ep.port} open via {ep.transport.upper()} ({ep.host}), "
                    f"but non-interactive SSH failed.\n"
                    f"       Fix: run `ssh-copy-id {target_spec}` or enable Tailscale SSH (`tailscale up --ssh`)."
                )
        else:
            print(
                f"[OFF]  {m.id:<12} Unreachable (LAN={m.lan_host or '-'}, Tailscale={m.tailscale_host or '-'})."
            )
    return 0


def cmd_browse(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m = cfg.get_machine(args.machine)
    if not m:
        available = ", ".join(x.id for x in cfg.machines)
        raise SystemExit(f"Unknown machine '{args.machine}'. Available: {available}")

    res = transport.run_worker_op(
        m,
        {
            "op": "browse_folders",
            "path": args.path or "",
            "include_hidden": args.hidden,
            "default_roots": m.default_roots,
        },
    )
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if not res.get("ok"):
        sys.stderr.write(f"Error: {res.get('error')}\n")
        return 1

    print(f"Machine: {m.name} ({m.id}) [{res['endpoint']['transport']}]")
    print(f"Folder:  {res['path']}" + (" [git]" if res.get("is_git_repo") else ""))
    if res.get("parent"):
        print(f"Parent:  {res['parent']}")
    print("\nSubfolders:")
    for d in res.get("directories", []):
        g = " [git]" if d.get("is_git_repo") else ""
        print(f"  {d['name']}/{g}")
    if not res.get("directories"):
        print("  (none)")
    if res.get("sample_files"):
        print(f"\nFiles ({res.get('file_count', 0)} total): {', '.join(res['sample_files'][:15])}")
    return 0


def _activate_selection(
    cfg: FleetConfig,
    transport: FleetTransport,
    machine: MachineConfig,
    remote_path: str,
    create: bool = False,
    init_git: bool = False,
    template: str = "empty",
    mode: str = "link",
) -> Dict[str, Any]:
    inspected = transport.run_worker_op(
        machine,
        {
            "op": "inspect_project",
            "path": remote_path,
            "create_if_missing": create,
            "init_git": init_git,
            "template": "" if template == "empty" else template,
        },
    )
    if not inspected.get("ok"):
        return inspected

    resolved_remote = inspected["path"]
    ws = prepare_local_workspace(cfg, transport, machine, resolved_remote, mode=mode)
    local_ws_path = str(ws.get("local_path") or "") if ws.get("ok") else ""

    state = ActiveProjectState(
        machine_id=machine.id,
        remote_path=resolved_remote,
        local_workspace_path=local_ws_path,
    )
    save_active_project(state)
    inspected["workspace"] = ws
    inspected["active_project"] = state.to_dict()
    return inspected


def cmd_use(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m = cfg.get_machine(args.machine)
    if not m:
        available = ", ".join(x.id for x in cfg.machines)
        raise SystemExit(f"Unknown machine '{args.machine}'. Available: {available}")

    res = _activate_selection(
        cfg,
        transport,
        m,
        args.path,
        create=args.create,
        init_git=args.init_git,
        template=args.template,
        mode=args.mode,
    )
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    if not res.get("ok"):
        sys.stderr.write(f"Error: {res.get('error')}\n")
        return 1

    print(f"Selected project: {m.id}:{res['path']}")
    if res.get("gpus"):
        print(f"GPUs on {m.id}: {', '.join(res['gpus'])}")
    ws = res.get("workspace") or {}
    if ws.get("local_path"):
        print(f"Local workspace ({ws.get('mode')}): {ws['local_path']}")
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)

    if args.machine:
        m = cfg.get_machine(args.machine)
        if not m:
            raise SystemExit(f"Unknown machine '{args.machine}'.")
    else:
        m = interactive_pick_machine(cfg, transport)
        if not m:
            return 1

    picked = interactive_browse_folder(m, transport, initial_path=args.path or "")
    if not picked:
        return 1
    chosen_path, should_create = picked

    res = _activate_selection(
        cfg,
        transport,
        m,
        chosen_path,
        create=should_create,
        init_git=should_create,
        mode=args.mode,
    )
    if not res.get("ok"):
        sys.stderr.write(f"Error selecting project: {res.get('error')}\n")
        return 1

    print(f"\nActive project set to: {m.id}:{res['path']}")
    ws = res.get("workspace") or {}
    if ws.get("local_path"):
        print(f"Local folder ready for any harness: {ws['local_path']}")
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    active = load_active_project()
    if args.json:
        print(json.dumps(active.to_dict() if active else None, indent=2))
        return 0
    if not active:
        print("No active project selected.")
        return 1
    print(f"Machine:         {active.machine_id}")
    print(f"Remote Path:     {active.remote_path}")
    if active.local_workspace_path:
        print(f"Local Workspace: {active.local_workspace_path}")
    print(f"Source:          {active.source}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    res = transport.run_worker_op(
        m,
        {
            "op": "list_files",
            "base_dir": base,
            "path": args.path or "",
            "max_depth": args.depth,
            "include_hidden": args.hidden,
        },
    )
    if args.json:
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    if not res.get("ok"):
        sys.stderr.write(f"Error: {res.get('error')}\n")
        return 1
    print(f"[{m.id}:{res['root']}]")
    for item in res.get("items", []):
        print(f"  {item['path']}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    res = transport.run_worker_op(
        m,
        {
            "op": "read_file",
            "base_dir": base,
            "path": args.path,
            "start_line": args.start_line,
            "end_line": args.end_line,
        },
    )
    if not res.get("ok"):
        sys.stderr.write(f"Error: {res.get('error')}\n")
        return 1
    sys.stdout.write(res["content"])
    if res["content"] and not res["content"].endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    content = sys.stdin.read() if args.stdin else (args.content or "")
    res = transport.run_worker_op(
        m,
        {"op": "write_file", "base_dir": base, "path": args.path, "content": content},
    )
    if not res.get("ok"):
        sys.stderr.write(f"Error: {res.get('error')}\n")
        return 1
    print(f"Wrote {res['bytes_written']} bytes to {m.id}:{res['path']}")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    res = transport.run_worker_op(
        m,
        {
            "op": "edit_file",
            "base_dir": base,
            "path": args.path,
            "old_text": args.old,
            "new_text": args.new,
            "replace_all": args.replace_all,
        },
    )
    if not res.get("ok"):
        sys.stderr.write(f"Error: {res.get('error')}\n")
        return 1
    print(f"Edited {m.id}:{res['path']} ({res['replacements']} replacement(s))")
    if res.get("diff"):
        print(res["diff"])
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    res = transport.run_worker_op(
        m,
        {
            "op": "search_files",
            "base_dir": base,
            "path": args.path or "",
            "query": args.query,
            "file_pattern": args.glob or "",
        },
    )
    if not res.get("ok"):
        sys.stderr.write(f"Error: {res.get('error')}\n")
        return 1
    for match in res.get("matches", []):
        print(f"{match['file']}:{match['line']}: {match['text']}")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    cmd_str = " ".join(args.command) if isinstance(args.command, list) else str(args.command)
    cwd = args.cwd or base
    res = transport.run_command(m, command=cmd_str, cwd=cwd, timeout_sec=args.timeout)
    if res.get("stdout"):
        sys.stdout.write(res["stdout"])
    if res.get("stderr"):
        sys.stderr.write(res["stderr"])
    return int(res.get("exit_code", 0 if res.get("ok") else 1))


def cmd_sync(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    remote_path = args.remote_path or base
    local_dir = (
        Path(args.local_path).expanduser().resolve()
        if args.local_path
        else compute_local_workspace_dir(cfg, m, remote_path)
    )
    res = sync_workspace(
        transport,
        m,
        remote_path=remote_path,
        local_dir=local_dir,
        direction=args.direction,
        delete=args.delete,
    )
    if not res.get("ok"):
        sys.stderr.write(f"Sync failed: {res.get('error')}\n")
        return 1
    print(json.dumps(res, indent=2))
    return 0


def cmd_mount(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    if args.machine and args.path:
        m = cfg.get_machine(args.machine)
        if not m:
            raise SystemExit(f"Unknown machine '{args.machine}'.")
        remote_path = args.path
    else:
        m, remote_path = _resolve_active(cfg, args.machine)

    res = prepare_local_workspace(cfg, transport, m, remote_path, mode="sshfs")
    if not res.get("ok"):
        sys.stderr.write(f"Mount failed: {res.get('error')}\n")
        return 1
    print(f"Mounted {m.id}:{remote_path} at {res['local_path']}")
    return 0


def cmd_unmount(args: argparse.Namespace) -> int:
    if args.local_path:
        target = Path(args.local_path)
    else:
        active = load_active_project()
        if not active or not active.local_workspace_path:
            raise SystemExit("Pass a mount path or select an active mounted workspace first.")
        target = Path(active.local_workspace_path)
    res = unmount_sshfs(target)
    if not res.get("ok"):
        sys.stderr.write(f"{res.get('error')}\n")
        return 1
    print(res.get("message") or f"Unmounted {res.get('local_path')}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)

    if args.machine and args.path:
        m = cfg.get_machine(args.machine)
        if not m:
            raise SystemExit(f"Unknown machine '{args.machine}'.")
        chosen_path = args.path
        should_create = args.create
    else:
        m = cfg.get_machine(args.machine) if args.machine else interactive_pick_machine(cfg, transport)
        if not m:
            return 1
        picked = interactive_browse_folder(m, transport, initial_path=args.path or "")
        if not picked:
            return 1
        chosen_path, should_create = picked

    res = _activate_selection(
        cfg,
        transport,
        m,
        chosen_path,
        create=should_create,
        init_git=should_create,
        mode=args.mode,
    )
    if not res.get("ok"):
        sys.stderr.write(f"Failed to prepare workspace: {res.get('error')}\n")
        return 1

    resolved_remote = res["path"]
    local_ws = res["workspace"]["local_path"]

    if not args.harness:
        print(f"\nProject ready: {m.id}:{resolved_remote}")
        print(f"Local folder:  {local_ws}")
        return 0

    harness_cmd = HARNESS_COMMANDS.get(args.harness.lower(), [args.harness])

    if args.remote_ssh and res["endpoint"]["transport"] != "local":
        harness_cmd = HARNESS_COMMANDS.get(args.harness.lower(), [args.harness])
        ep_host = res["endpoint"]["host"]
        ssh_base = transport.build_ssh_args(m, ep_host, use_control_master=False)
        ssh_interactive = [("-t" if x == "-T" else x) for x in ssh_base]
        remote_launch = f"cd {shlex.quote(resolved_remote)} && {' '.join(shlex.quote(c) for c in harness_cmd)}"
        print(f"Launching '{args.harness}' directly on {m.id} ({ep_host}:{resolved_remote})...")
        return subprocess.call([*ssh_interactive, remote_launch])

    return _launch_local_harness(args.harness, local_ws, m.id, resolved_remote)


def _launch_local_harness(
    harness_name: str, local_ws: str, machine_id: str, resolved_remote: str
) -> int:
    """Launch a harness locally (e.g. on Mac Mini) bound to `machine_id:resolved_remote`."""
    low = harness_name.lower()
    harness_cmd = HARNESS_COMMANDS.get(low, [harness_name])
    env = os.environ.copy()
    env["REMOTE_FLEET_MACHINE"] = machine_id
    env["REMOTE_FLEET_PATH"] = resolved_remote

    if shutil.which(harness_cmd[0]):
        print(f"Starting {harness_name} locally in {local_ws} (bound to {machine_id}:{resolved_remote})...")
        return subprocess.call(harness_cmd, cwd=local_ws, env=env)

    # On macOS, try `open -a "<AppName>" <local_ws>` for GUI harnesses (Antigravity, Cursor, VS Code)
    if sys.platform == "darwin" and low in MACOS_APP_NAMES and shutil.which("open"):
        app_name = MACOS_APP_NAMES[low]
        print(f"Opening {app_name}.app on this Mac for {local_ws} (bound to {machine_id}:{resolved_remote})...")
        return subprocess.call(["open", "-a", app_name, local_ws], env=env)

    print(f"Project ready at: {local_ws} (linked to {machine_id}:{resolved_remote})")
    print(f"Note: '{harness_cmd[0]}' was not found on PATH; open `{local_ws}` in {harness_name} manually.")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    picked = gui_pick_machine_and_folder(
        cfg,
        transport,
        preselected_machine=args.machine,
        initial_path=args.path or "",
    )
    if not picked:
        return 1
    m, chosen_path, should_create, chosen_harness = picked
    res = _activate_selection(
        cfg,
        transport,
        m,
        chosen_path,
        create=should_create,
        init_git=should_create,
        mode=args.mode,
    )
    if not res.get("ok"):
        sys.stderr.write(f"Failed to activate remote folder: {res.get('error')}\n")
        return 1

    resolved_remote = res["path"]
    local_ws = res["workspace"]["local_path"]
    harness_to_run = args.harness or chosen_harness
    if not harness_to_run:
        print(f"Active project set to {m.id}:{resolved_remote}")
        print(f"Local handle: {local_ws}")
        return 0
    return _launch_local_harness(harness_to_run, local_ws, m.id, resolved_remote)


def cmd_watch(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)
    remote_path = args.remote_path or base
    local_dir = (
        Path(args.local_path).expanduser().resolve()
        if args.local_path
        else compute_local_workspace_dir(cfg, m, remote_path)
    )
    return watch_workspace(
        transport,
        m,
        remote_path=remote_path,
        local_dir=local_dir,
        interval_sec=args.interval,
    )


def cmd_jobs(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    m, base = _resolve_active(cfg, args.machine)

    action = args.job_action or "list"
    if action == "start":
        cmd_parts = args.command or []
        if cmd_parts and cmd_parts[0] == "--":
            cmd_parts = cmd_parts[1:]
        if not cmd_parts:
            raise SystemExit("`fleet jobs start` requires a command (e.g. `fleet jobs start -- python train.py`).")
        res = transport.run_worker_op(
            m,
            {
                "op": "start_job",
                "command": " ".join(cmd_parts),
                "cwd": args.cwd or base,
                "low_priority": args.low_priority,
            },
        )
    elif action == "status":
        if args.job_id:
            res = transport.run_worker_op(
                m, {"op": "job_status", "job_id": args.job_id, "tail_lines": args.tail}
            )
        else:
            res = transport.run_worker_op(m, {"op": "list_jobs"})
    elif action == "stop":
        if not args.job_id:
            raise SystemExit("`fleet jobs stop` requires a <job_id>.")
        res = transport.run_worker_op(m, {"op": "stop_job", "job_id": args.job_id})
    else:
        res = transport.run_worker_op(m, {"op": "list_jobs"})

    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def cmd_ssh_config(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    target = Path(args.output).expanduser().resolve() if args.output else None
    written = sync_ssh_config(cfg, ts_self, ssh_config_path=target)
    print(f"Updated SSH config at {written}")
    print("Configured SSH hosts (automatic Home LAN -> Tailscale fallback):")
    for m in cfg.machines:
        print(f"  - {m.id:<12} (LAN={m.lan_host or '-'} -> Tailscale={m.tailscale_host or '-'})")
    return 0


def cmd_mount_all(args: argparse.Namespace) -> int:
    cfg, ts_self = load_fleet_config(include_tailscale=True)
    transport = FleetTransport(cfg, ts_self)
    base = Path(args.dir).expanduser().resolve() if args.dir else None
    summary = mount_all_machines(cfg, transport, base_dir=base)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"Remote Computers folder: {summary['base_dir']}")
    for r in summary["results"]:
        mid = r.get("machine_id", "?")
        if r.get("skipped"):
            print(f"  [SKIP] {mid:<12} ({r.get('reason')})")
        elif r.get("ok"):
            if r.get("drives"):
                drv_list = ", ".join(r["drives"])
                print(f"  [OK]   {mid:<12} -> {r.get('mount_point')} (drives: {drv_list} + Home shortcut)")
            else:
                entries = ", ".join(r.get("sample_entries") or ["Home"])
                state = "already mounted" if r.get("already_mounted") else f"mounted via {r.get('engine')}"
                print(f"  [OK]   {mid:<12} -> {r.get('mount_point')} ({entries} | {state})")
        else:
            print(f"  [WARN] {mid:<12} -> {r.get('error')}")
    if summary.get("sidebar_repaired"):
        print("  [FIXED] Repaired macOS Finder & Open-Dialog Sidebar Favorites (`pc`, `spark01`, `spark03`, `spark04`)!")
    return 0


def cmd_unmount_all(args: argparse.Namespace) -> int:
    base = Path(args.dir).expanduser().resolve() if args.dir else None
    summary = unmount_all_machines(base_dir=base)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_setup_mac(args: argparse.Namespace) -> int:
    print("=== Step 1/4: Discovering Tailscale peers (if active) & loading fleet config ===")
    ts_nodes = query_tailscale_nodes()
    cfg, ts_self = load_fleet_config(include_tailscale=False)
    if ts_nodes:
        cfg, ts_self = merge_tailscale_into_config(cfg, ts_nodes)
    cfg_path = save_fleet_config(cfg)
    print(f"  Saved {cfg_path} ({len(cfg.machines)} machines)")

    print("\n=== Step 2/4: Installing MCP server, Skills & Global Rules across all Harnesses ===")
    install_all_harnesses()
    print("  Configured: Antigravity, Claude Code / Desktop, Codex, Kimi Code, DSH")

    print("\n=== Step 3/4: Writing ~/.ssh/config hosts (for Codex SSH & Remote-SSH) ===")
    ssh_cfg = sync_ssh_config(cfg, ts_self)
    print(f"  Updated {ssh_cfg} with LAN-first + Tailscale-fallback Host entries")

    print("\n=== Step 4/4: Mounting online computers under ~/RemoteComputers/ (for 'Choose Folder' dialogs) ===")
    mount_dir = Path(args.dir).expanduser().resolve() if args.dir else (Path.home() / "RemoteComputers")
    transport = FleetTransport(cfg, ts_self)
    summary = mount_all_machines(cfg, transport, base_dir=mount_dir)
    for r in summary["results"]:
        mid = r.get("machine_id", "?")
        if r.get("skipped"):
            print(f"  [LOCAL] {mid:<12} (this Mac)")
        elif r.get("ok"):
            print(f"  [MOUNT] {mid:<12} -> {r.get('mount_point')}")
        else:
            print(f"  [INFO]  {mid:<12} -> {r.get('error')}")

    if sys.platform == "darwin" and not args.no_autostart:
        plist = install_macos_launch_agent(base_dir=mount_dir)
        print(f"\nInstalled macOS login auto-mounter: {plist}")

    print(
        f"\nDone! Drag `{mount_dir}` into your macOS Finder Sidebar Favorites so `spark-1`, "
        "`spark-2`, `spark-3`, and `pc` appear directly inside every harness's 'New / Create Project' dialog!"
    )
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    harnesses = (
        [h.strip() for h in args.harnesses.split(",") if h.strip()]
        if args.harnesses
        else None
    )
    report = install_all_harnesses(harnesses=harnesses)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("Installed Remote Fleet across harnesses:")
    for section, paths in report.items():
        print(f"\n  [{section}]")
        for p in paths:
            print(f"    - {p}")
    print("\nTip: On macOS, run `fleet setup-mac` to also configure `~/.ssh/config` and mount `~/RemoteComputers/`!")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet",
        description="Pick folders and work on projects across any computer on your Home LAN or Tailscale.",
    )
    parser.add_argument("--version", action="version", version=f"remote-fleet {__version__}")
    sub = parser.add_subparsers(dest="subcommand")

    p_init = sub.add_parser("init", help="Create ~/.config/remote-fleet/fleet.json (Mac Mini, PC, 3 Sparks + Tailscale)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing fleet.json")
    p_init.set_defaults(func=cmd_init)

    p_disc = sub.add_parser("discover", help="Auto-discover computers from `tailscale status --json`")
    p_disc.add_argument("--save", action="store_true", help="Merge discovered Tailscale peers into fleet.json")
    p_disc.add_argument("--json", action="store_true", help="Output JSON")
    p_disc.set_defaults(func=cmd_discover)

    p_stat = sub.add_parser("status", aliases=["machines"], help="Check all fleet computers over Home LAN & Tailscale")
    p_stat.add_argument("--load", action="store_true", help="Also query CPU & GPU load on online machines")
    p_stat.add_argument("--json", action="store_true", help="Output JSON")
    p_stat.set_defaults(func=cmd_status)

    p_doc = sub.add_parser("doctor", help="Diagnose SSH, Tailscale, and remote connectivity across all machines")
    p_doc.set_defaults(func=cmd_doctor)

    p_browse = sub.add_parser("browse", help="Browse folders on a specific fleet computer")
    p_browse.add_argument("machine", help="Machine ID or name (e.g. spark-1, pc, mac-mini)")
    p_browse.add_argument("path", nargs="?", default="", help="Optional starting directory")
    p_browse.add_argument("--hidden", action="store_true", help="Show hidden dot-folders")
    p_browse.add_argument("--json", action="store_true", help="Output JSON")
    p_browse.set_defaults(func=cmd_browse)

    p_pick = sub.add_parser("pick", help="Interactively pick a computer and project folder")
    p_pick.add_argument("--machine", help="Pre-select computer ID")
    p_pick.add_argument("--path", default="", help="Starting folder path")
    p_pick.add_argument("--mode", choices=["link", "sync", "sshfs"], default="link", help="Local workspace mode")
    p_pick.set_defaults(func=cmd_pick)

    p_gui = sub.add_parser("gui", help="Open native macOS GUI picker to choose computer, folder, and local harness")
    p_gui.add_argument("--machine", help="Optional pre-selected computer ID")
    p_gui.add_argument("--path", default="", help="Optional starting folder path")
    p_gui.add_argument("--harness", help="Optional harness override")
    p_gui.add_argument("--mode", choices=["link", "sync", "sshfs"], default="link")
    p_gui.set_defaults(func=cmd_gui)

    p_use = sub.add_parser("use", help="Select (or create) a remote folder non-interactively")
    p_use.add_argument("machine", help="Machine ID or name (e.g. spark-2, pc, mac-mini)")
    p_use.add_argument("path", help="Folder path on target machine (e.g. ~/projects/app)")
    p_use.add_argument("--create", action="store_true", help="Create folder if missing")
    p_use.add_argument("--init-git", action="store_true", help="Run git init if not yet a repo")
    p_use.add_argument("--template", choices=["empty", "python", "node"], default="empty")
    p_use.add_argument("--mode", choices=["link", "sync", "sshfs"], default="link")
    p_use.add_argument("--json", action="store_true", help="Output JSON")
    p_use.set_defaults(func=cmd_use)

    p_cur = sub.add_parser("current", help="Show the currently selected fleet machine & folder")
    p_cur.add_argument("--json", action="store_true")
    p_cur.set_defaults(func=cmd_current)

    p_ls = sub.add_parser("ls", help="List files inside the active remote project")
    p_ls.add_argument("path", nargs="?", default="")
    p_ls.add_argument("--machine", help="Machine override")
    p_ls.add_argument("--depth", type=int, default=3)
    p_ls.add_argument("--hidden", action="store_true")
    p_ls.add_argument("--json", action="store_true")
    p_ls.set_defaults(func=cmd_ls)

    p_read = sub.add_parser("read", help="Read a text file from the active remote project")
    p_read.add_argument("path")
    p_read.add_argument("--machine")
    p_read.add_argument("--start-line", type=int)
    p_read.add_argument("--end-line", type=int)
    p_read.set_defaults(func=cmd_read)

    p_write = sub.add_parser("write", help="Write a text file to the active remote project")
    p_write.add_argument("path")
    p_write.add_argument("--content", default="")
    p_write.add_argument("--stdin", action="store_true", help="Read file content from stdin")
    p_write.add_argument("--machine")
    p_write.set_defaults(func=cmd_write)

    p_edit = sub.add_parser("edit", help="Replace exact text in a file on the active remote project")
    p_edit.add_argument("path")
    p_edit.add_argument("--old", required=True, help="Exact text to find")
    p_edit.add_argument("--new", required=True, help="Replacement text")
    p_edit.add_argument("--replace-all", action="store_true")
    p_edit.add_argument("--machine")
    p_edit.set_defaults(func=cmd_edit)

    p_search = sub.add_parser("search", help="Search file contents on the active remote project")
    p_search.add_argument("query")
    p_search.add_argument("--path", default="")
    p_search.add_argument("--glob", default="")
    p_search.add_argument("--machine")
    p_search.set_defaults(func=cmd_search)

    p_exec = sub.add_parser("exec", help="Execute a command directly on the active project's computer")
    p_exec.add_argument("--machine", help="Machine override")
    p_exec.add_argument("--cwd", help="Working directory override")
    p_exec.add_argument("--timeout", type=int, default=120)
    p_exec.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute")
    p_exec.set_defaults(func=cmd_exec)

    p_jobs = sub.add_parser("jobs", help="Manage detached heavy background jobs on PC / Sparks")
    p_jobs.add_argument("job_action", nargs="?", choices=["list", "start", "status", "stop"], default="list")
    p_jobs.add_argument("--job-id", help="Job ID for status or stop")
    p_jobs.add_argument("--machine", help="Machine override")
    p_jobs.add_argument("--cwd", help="Working directory override")
    p_jobs.add_argument("--tail", type=int, default=60, help="Log lines to tail")
    p_jobs.add_argument("--low-priority", action="store_true", help="Run with nice -n 10")
    p_jobs.add_argument("command", nargs=argparse.REMAINDER, help="Command when job_action=start")
    p_jobs.set_defaults(func=cmd_jobs)

    p_sync = sub.add_parser("sync", help="Sync code files between local workspace and remote computer (skips datasets)")
    p_sync.add_argument("direction", choices=["pull", "push"])
    p_sync.add_argument("--machine")
    p_sync.add_argument("--remote-path")
    p_sync.add_argument("--local-path")
    p_sync.add_argument("--delete", action="store_true")
    p_sync.set_defaults(func=cmd_sync)

    p_watch = sub.add_parser("watch", help="Mirror lightweight code files locally and auto-push edits to remote")
    p_watch.add_argument("--machine")
    p_watch.add_argument("--remote-path")
    p_watch.add_argument("--local-path")
    p_watch.add_argument("--interval", type=float, default=1.5)
    p_watch.set_defaults(func=cmd_watch)

    p_mount = sub.add_parser("mount", help="Mount remote folder locally via rclone nfsmount (macOS) or SSHFS")
    p_mount.add_argument("machine", nargs="?")
    p_mount.add_argument("path", nargs="?")
    p_mount.set_defaults(func=cmd_mount)

    p_unmount = sub.add_parser("unmount", help="Unmount an rclone/SSHFS workspace")
    p_unmount.add_argument("local_path", nargs="?")
    p_unmount.set_defaults(func=cmd_unmount)

    p_sshcfg = sub.add_parser(
        "ssh-config",
        help="Write LAN-first / Tailscale-fallback Host blocks into ~/.ssh/config (for Codex SSH & Remote-SSH)",
    )
    p_sshcfg.add_argument("--output", help="Optional custom ssh config path")
    p_sshcfg.set_defaults(func=cmd_ssh_config)

    p_mall = sub.add_parser(
        "mount-all",
        help="Mount all reachable computers under ~/RemoteComputers/<machine> so 'Choose Folder' dialogs see them",
    )
    p_mall.add_argument("--dir", help="Mount root directory (default: ~/RemoteComputers)")
    p_mall.add_argument("--json", action="store_true")
    p_mall.set_defaults(func=cmd_mount_all)

    p_umall = sub.add_parser("unmount-all", help="Unmount all ~/RemoteComputers/<machine> mounts")
    p_umall.add_argument("--dir", help="Mount root directory (default: ~/RemoteComputers)")
    p_umall.set_defaults(func=cmd_unmount_all)

    p_mac = sub.add_parser(
        "setup-mac",
        help="All-in-one Mac setup: install into all harnesses, configure ~/.ssh/config, and mount ~/RemoteComputers/",
    )
    p_mac.add_argument("--dir", help="Mount root directory (default: ~/RemoteComputers)")
    p_mac.add_argument("--no-autostart", action="store_true", help="Skip creating macOS login LaunchAgent")
    p_mac.set_defaults(func=cmd_setup_mac)

    p_boot = sub.add_parser(
        "bootstrap",
        help="Turnkey Home LAN & Tailscale discovery, duplicate Tailscale cleanup, standalone rclone download, and Mac setup",
    )
    p_boot.add_argument("--user", default="danlinyu", help="SSH username on Sparks (default: danlinyu)")
    p_boot.add_argument("--pc-user", default="", help="Optional Windows SSH username on `danlin-hud` if different from --user")
    p_boot.add_argument(
        "--host",
        action="append",
        default=[],
        help="Optional explicit override `id=hostname_or_ip` (repeatable, e.g. --host pc=192.168.68.50)",
    )
    p_boot.add_argument(
        "--fix-spark-tailscale",
        action="store_true",
        help="Deprecated no-op flag kept for CLI compatibility",
    )
    p_boot.add_argument(
        "--install-tailscale",
        action="store_true",
        default=True,
        help="Download, install, and launch official standalone Tailscale on this Mac Mini (default: enabled)",
    )
    p_boot.add_argument(
        "--no-install-tailscale",
        action="store_true",
        help="Skip installing Tailscale on macOS",
    )
    p_boot.add_argument(
        "--no-cleanup-tailscale",
        action="store_true",
        help="Skip removing duplicate second Tailscale (`spark01-1` / `spark03-1`)",
    )
    p_boot.add_argument("--no-ssh-copy", action="store_true", help="Skip interactive ssh-copy-id prompts")
    p_boot.add_argument("--dir", help="Mount root directory (default: ~/RemoteComputers)")
    p_boot.set_defaults(
        func=lambda a: run_bootstrap(
            ssh_user=a.user,
            pc_user=a.pc_user,
            extra_hosts=a.host,
            interactive_ssh_copy=not a.no_ssh_copy,
            fix_spark_tailscale=a.fix_spark_tailscale,
            cleanup_duplicate_tailscale=not a.no_cleanup_tailscale,
            install_tailscale=not a.no_install_tailscale,
            mount_dir=Path(a.dir) if a.dir else None,
        )
    )

    p_start = sub.add_parser(
        "start",
        help="Pick a folder on any machine and launch Antigravity, Claude, Codex, DSH, or Kimi Code locally",
    )
    p_start.add_argument("--harness", help="Harness to launch locally (antigravity, claude, codex, dsh, kimi, cursor)")
    p_start.add_argument("--machine", help="Target machine ID")
    p_start.add_argument("--path", help="Target folder on machine")
    p_start.add_argument("--create", action="store_true", help="Create folder if missing")
    p_start.add_argument("--mode", choices=["link", "sync", "sshfs"], default="link")
    p_start.add_argument(
        "--remote-ssh",
        action="store_true",
        help="Optional: SSH into remote machine to run harness there instead of locally on this Mac",
    )
    p_start.set_defaults(func=cmd_start)

    p_inst = sub.add_parser(
        "install",
        help="Install Remote Fleet MCP server, Launchpad, Global Rules, Skills, and CLI into all harnesses",
    )
    p_inst.add_argument(
        "--harnesses",
        default="",
        help="Comma-separated list (default: antigravity,claude,codex,kimi,dsh,cursor)",
    )
    p_inst.add_argument("--json", action="store_true")
    p_inst.set_defaults(func=cmd_install)

    p_mcp = sub.add_parser("mcp", help="Run the stdio Model Context Protocol (MCP) server")
    p_mcp.set_defaults(func=lambda _a: serve_stdio())

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "subcommand", None):
        parser.print_help()
        return 0
    if args.subcommand == "exec" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if args.subcommand == "exec" and not args.command:
        parser.error("`fleet exec` requires a command to run.")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
