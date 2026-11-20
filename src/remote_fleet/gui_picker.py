"""Native macOS GUI dialog picker (`osascript`) with terminal fallback for picking remote folders & harnesses."""

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import List, Optional, Tuple

from remote_fleet.config import FleetConfig, MachineConfig
from remote_fleet.picker import interactive_browse_folder, interactive_pick_machine
from remote_fleet.transport import FleetTransport


def has_macos_gui() -> bool:
    return platform.system().lower() == "darwin" and bool(shutil.which("osascript"))


def _osascript_choose_from_list(
    title: str, prompt: str, items: List[str]
) -> Optional[str]:
    """Display a native macOS `choose from list` dialog safely passing items via `argv`."""
    if not items:
        return None
    script = (
        "on run argv\n"
        "  set winTitle to item 1 of argv\n"
        "  set winPrompt to item 2 of argv\n"
        "  set choices to items 3 thru -1 of argv\n"
        "  set picked to choose from list choices with title winTitle with prompt winPrompt default items {item 1 of choices}\n"
        "  if picked is false then return \"\"\n"
        "  return item 1 of picked\n"
        "end run\n"
    )
    proc = subprocess.run(
        ["osascript", "-e", script, title, prompt, *items],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    val = proc.stdout.strip()
    return val if val else None


def _osascript_text_input(title: str, prompt: str, default_text: str = "") -> Optional[str]:
    script = (
        "on run argv\n"
        "  set winTitle to item 1 of argv\n"
        "  set winPrompt to item 2 of argv\n"
        "  set defVal to item 3 of argv\n"
        "  try\n"
        "    set d to display dialog winPrompt default answer defVal with title winTitle buttons {\"Cancel\", \"Create\"} default button \"Create\"\n"
        "    return text returned of d\n"
        "  on error\n"
        "    return \"\"\n"
        "  end try\n"
        "end run\n"
    )
    proc = subprocess.run(
        ["osascript", "-e", script, title, prompt, default_text],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    val = proc.stdout.strip()
    return val if val else None


def gui_pick_machine_and_folder(
    config: FleetConfig,
    transport: FleetTransport,
    preselected_machine: Optional[str] = None,
    initial_path: str = "",
) -> Optional[Tuple[MachineConfig, str, bool, Optional[str]]]:
    """Run native macOS GUI picker (or terminal fallback) returning `(machine, path, create, harness)`."""
    if not has_macos_gui():
        m = (
            config.get_machine(preselected_machine)
            if preselected_machine
            else interactive_pick_machine(config, transport)
        )
        if not m:
            return None
        picked = interactive_browse_folder(m, transport, initial_path=initial_path)
        if not picked:
            return None
        return m, picked[0], picked[1], None

    # 1. Pick Machine via native macOS list dialog
    if preselected_machine:
        chosen_machine = config.get_machine(preselected_machine)
        if not chosen_machine:
            return None
    else:
        ep_map = transport.resolve_all_endpoints(config.machines, force_refresh=True)
        labels: List[str] = []
        by_label = {}
        for m in config.machines:
            ep = ep_map[m.id]
            if ep.transport == "local":
                badge = "LOCAL"
            elif ep.reachable:
                lat = f"{ep.latency_ms:.1f}ms" if ep.latency_ms is not None else "online"
                badge = f"{ep.transport.upper()} · {lat}"
            else:
                badge = "OFFLINE"
            label = f"{m.name} ({m.id}) — [{badge}]"
            if ep.reachable:
                labels.append(label)
                by_label[label] = m

        if not labels:
            return None
        picked_label = _osascript_choose_from_list(
            "Remote Fleet — Pick a Computer",
            "Choose which computer over Home LAN / Tailscale holds the project files & compute:",
            labels,
        )
        if not picked_label or picked_label not in by_label:
            return None
        chosen_machine = by_label[picked_label]

    # 2. Browse Folders on chosen_machine
    cur_path = initial_path
    while True:
        res = transport.run_worker_op(
            chosen_machine,
            {
                "op": "browse_folders",
                "path": cur_path,
                "include_hidden": False,
                "default_roots": chosen_machine.default_roots,
            },
        )
        if not res.get("ok"):
            typed = _osascript_text_input(
                f"Remote Fleet — {chosen_machine.name}",
                f"Could not open '{cur_path or '~'}'. Enter a folder path on {chosen_machine.id}:",
                "~",
            )
            if not typed:
                return None
            cur_path = typed
            continue

        resolved = res["path"]
        parent = res.get("parent") or ""
        dirs = res.get("directories") or []

        select_cur_item = f"[Select This Folder] {resolved}"
        create_new_item = "[+ New Project Folder Here...]"
        parent_item = f"[.. Up to Parent] {parent}" if parent else ""

        menu_items = [select_cur_item, create_new_item]
        if parent_item:
            menu_items.append(parent_item)

        dir_map = {}
        for d in dirs:
            git_suffix = " [git]" if d.get("is_git_repo") else ""
            item_label = f"Folder: {d['name']}/{git_suffix}"
            menu_items.append(item_label)
            dir_map[item_label] = d["path"]

        choice = _osascript_choose_from_list(
            f"Remote Fleet — {chosen_machine.name} ({chosen_machine.id})",
            f"Browsing {resolved}\nDouble-click a subfolder to open it, or select '[Select This Folder]':",
            menu_items,
        )
        if not choice:
            return None
        if choice == select_cur_item:
            selected_path, should_create = resolved, False
            break
        if choice == create_new_item:
            new_name = _osascript_text_input(
                f"New Project on {chosen_machine.name}",
                f"Create new project folder inside {resolved}:",
                "new-project",
            )
            if not new_name:
                continue
            sep = "\\" if "\\" in resolved and "/" not in resolved else "/"
            trimmed = resolved.rstrip("/\\")
            selected_path, should_create = f"{trimmed}{sep}{new_name}", True
            break
        if parent_item and choice == parent_item:
            cur_path = parent
            continue
        if choice in dir_map:
            cur_path = dir_map[choice]
            continue

    # 3. Pick Harness to open on this Mac Mini
    harness_choices = [
        "Set Active Project Only (use in open chat)",
        "Antigravity",
        "Claude Code",
        "Codex",
        "Kimi Code",
        "DSH",
        "Reveal Linked Folder in Finder",
    ]
    h_choice = _osascript_choose_from_list(
        "Remote Fleet — Choose Harness on This Mac",
        f"Target: {chosen_machine.id}:{selected_path}\nFiles & heavy compute stay on {chosen_machine.id}; harness UI runs on this Mac:",
        harness_choices,
    )
    harness_map = {
        "Set Active Project Only (use in open chat)": None,
        "Antigravity": "antigravity",
        "Claude Code": "claude",
        "Codex": "codex",
        "Kimi Code": "kimi",
        "DSH": "dsh",
        "Reveal Linked Folder in Finder": "open",
    }
    chosen_harness = harness_map.get(h_choice or "", None)
    return chosen_machine, selected_path, should_create, chosen_harness
