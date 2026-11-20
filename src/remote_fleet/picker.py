"""Interactive terminal picker for choosing a computer and browsing/creating folders across the fleet."""

from __future__ import annotations

import sys
from typing import Callable, List, Optional, Tuple

from remote_fleet.config import FleetConfig, MachineConfig
from remote_fleet.transport import EndpointStatus, FleetTransport


InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]


def _format_endpoint_badge(ep: EndpointStatus) -> str:
    if ep.transport == "local":
        return "[LOCAL · 0ms]"
    if ep.reachable:
        lat = f"{ep.latency_ms:.1f}ms" if ep.latency_ms is not None else "online"
        return f"[{ep.transport.upper()} {ep.host} · {lat}]"
    return "[OFFLINE / UNREACHABLE]"


def interactive_pick_machine(
    config: FleetConfig,
    transport: FleetTransport,
    input_fn: InputFn = input,
    print_fn: PrintFn = print,
) -> Optional[MachineConfig]:
    """Prompt the user to pick one of the fleet computers."""
    if not config.machines:
        print_fn("No machines configured in fleet.json.")
        return None

    print_fn("\n=== Remote Fleet: Select a Computer ===")
    ep_map = transport.resolve_all_endpoints(config.machines, force_refresh=True)
    statuses: List[Tuple[MachineConfig, EndpointStatus]] = []
    for idx, m in enumerate(config.machines, 1):
        ep = ep_map[m.id]
        statuses.append((m, ep))
        tags_str = f"  ({', '.join(m.tags)})" if m.tags else ""
        print_fn(f"  [{idx}] {m.name:<16} ({m.id:<10}) {_format_endpoint_badge(ep)}{tags_str}")

    while True:
        try:
            choice = input_fn(f"\nChoose computer [1-{len(statuses)}, or 'q' to cancel]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print_fn("")
            return None
        if choice.lower() in ("q", "quit", "exit"):
            return None
        if choice.isdigit():
            i = int(choice)
            if 1 <= i <= len(statuses):
                chosen, ep = statuses[i - 1]
                if not ep.reachable:
                    print_fn(f"  ! '{chosen.id}' appears unreachable ({ep.error}). Pick another or check Tailscale/LAN.")
                    continue
                return chosen
        by_name = config.get_machine(choice)
        if by_name:
            return by_name
        print_fn("  Invalid selection. Enter a number or machine ID.")


def interactive_browse_folder(
    machine: MachineConfig,
    transport: FleetTransport,
    initial_path: str = "",
    input_fn: InputFn = input,
    print_fn: PrintFn = print,
) -> Optional[Tuple[str, bool]]:
    """Interactively navigate folders on `machine`.

    Returns `(selected_path, should_create)` or `None` if cancelled.
    """
    current_path = initial_path
    include_hidden = False

    while True:
        res = transport.run_worker_op(
            machine,
            {
                "op": "browse_folders",
                "path": current_path,
                "include_hidden": include_hidden,
                "default_roots": machine.default_roots,
            },
        )
        if not res.get("ok"):
            print_fn(f"\n  ! Error browsing '{current_path or '~'}' on {machine.id}: {res.get('error')}")
            try:
                fallback = input_fn("  Enter another path (or '~' for home, 'q' to cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not fallback or fallback.lower() == "q":
                return None
            current_path = fallback
            continue

        resolved = res["path"]
        parent = res.get("parent") or ""
        dirs = res.get("directories") or []
        roots = res.get("roots") or []
        git_tag = " [git repo]" if res.get("is_git_repo") else ""

        print_fn(f"\n=== {machine.name} ({machine.id}): {resolved}{git_tag} ===")
        print_fn("  [.]  Pick THIS folder")
        if parent:
            print_fn(f"  [..] Go up to parent ({parent})")
        print_fn("  [n]  Create & pick a NEW project folder inside here")
        print_fn("  [r]  Jump to favorite root / drive")
        print_fn("  [p]  Type an exact path")
        print_fn(f"  [h]  Toggle hidden folders (currently {'ON' if include_hidden else 'OFF'})")

        if dirs:
            print_fn("  --- Subfolders (enter # to open, or 's#' to select directly) ---")
            for idx, d in enumerate(dirs, 1):
                g = " [git]" if d.get("is_git_repo") else ""
                print_fn(f"  [{idx:>2}] {d['name']}/{g}")
        else:
            print_fn("  (No subfolders in this directory)")

        try:
            ans = input_fn("\nAction [#, s#, '.', '..', 'n', 'r', 'p', 'q']: ").strip()
        except (EOFError, KeyboardInterrupt):
            print_fn("")
            return None

        low = ans.lower()
        if low in ("q", "quit", "exit"):
            return None
        if ans == ".":
            return resolved, False
        if ans == ".." and parent:
            current_path = parent
            continue
        if low == "h":
            include_hidden = not include_hidden
            current_path = resolved
            continue
        if low == "n":
            try:
                folder_name = input_fn("  New project folder name: ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if not folder_name:
                continue
            sep = "\\" if "\\" in resolved and "/" not in resolved else "/"
            trimmed_base = resolved.rstrip("/\\")
            new_target = f"{trimmed_base}{sep}{folder_name}"
            return new_target, True
        if low == "p":
            try:
                typed = input_fn("  Enter path on remote machine (e.g. ~/projects/app): ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if typed:
                current_path = typed
            continue
        if low == "r":
            if not roots:
                continue
            print_fn("  --- Favorite Roots / Drives ---")
            for idx, r in enumerate(roots, 1):
                status = "" if r.get("exists") else " (missing)"
                print_fn(f"  [{idx}] {r['label']} -> {r['path']}{status}")
            try:
                r_ans = input_fn("  Select root #: ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            if r_ans.isdigit() and 1 <= int(r_ans) <= len(roots):
                current_path = roots[int(r_ans) - 1]["path"]
            continue
        if low.startswith("s") and low[1:].isdigit():
            idx = int(low[1:])
            if 1 <= idx <= len(dirs):
                return dirs[idx - 1]["path"], False
        if ans.isdigit():
            idx = int(ans)
            if 1 <= idx <= len(dirs):
                current_path = dirs[idx - 1]["path"]
                continue

        print_fn("  Unrecognized choice.")
