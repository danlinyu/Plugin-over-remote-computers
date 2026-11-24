"""Cross-harness installer: registers Remote Fleet into Antigravity, Claude, Codex, Kimi Code, DSH, and PATH."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys
from typing import Any, Dict, List, Optional

from remote_fleet.config import default_home_fleet_config, get_config_path, save_fleet_config
from remote_fleet.mount import create_launchpad_workspace, get_repo_root


CODEX_BEGIN_MARKER = "# >>> remote-fleet >>>"
CODEX_END_MARKER = "# <<< remote-fleet <<<"

MD_BEGIN_MARKER = "<!-- remote-fleet-begin -->"
MD_END_MARKER = "<!-- remote-fleet-end -->"

GLOBAL_FLEET_INSTRUCTION_BLOCK = f"""{MD_BEGIN_MARKER}
## Remote Fleet (Home LAN & Tailscale Multi-Computer Projects)

You have the `remote-fleet` MCP server and skill installed. The user uses this machine (e.g., Mac Mini) as a fast, lightweight control interface while heavy computations and large local datasets live on their remote machines (e.g., `pc`, `spark01`, `spark03`, `spark04`, `mac-mini` — always call `fleet_list_machines` to see live configured IDs) over Home LAN / Tailscale.

- **Starting a new project or picking a remote folder**: Whenever the user starts a new project (especially from `~/Fleet/Start-Here` or without a target repo yet) or asks to open/create a project on another machine, call `fleet_list_machines(probe=true, include_load=true)` and `fleet_browse_folders`, then bind the folder with `fleet_select_project` or `fleet_create_project`.
- **Keep changes, datasets, and compute on the target machine**: Once a remote machine folder is active, perform ALL file reads/writes/edits (`fleet_read_file`, `fleet_write_file`, `fleet_edit_file`, `fleet_list_files`, `fleet_search_files`) and command execution (`fleet_run_command`) on that target machine—never leave edits stranded only on this interface computer, and never run heavy calculations locally on this interface computer.
- **Heavy calculations**: Use `fleet_start_job`, `fleet_job_status`, and `fleet_stop_job` for long-running or heavy CPU/GPU workloads on the PC or Sparks so this harness stays responsive.
{MD_END_MARKER}
"""


def _make_executable(path: Path) -> None:
    if os.name != "nt":
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _upsert_global_agent_md(md_path: Path) -> Path:
    """Idempotently insert or update the Remote Fleet rule block in a global agent markdown file."""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    existing = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    pattern = re.compile(
        re.escape(MD_BEGIN_MARKER) + r".*?" + re.escape(MD_END_MARKER) + r"\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(GLOBAL_FLEET_INSTRUCTION_BLOCK, existing)
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        extra_nl = "\n" if existing.strip() else ""
        updated = f"{existing}{sep}{extra_nl}{GLOBAL_FLEET_INSTRUCTION_BLOCK}"
    md_path.write_text(updated, encoding="utf-8")
    return md_path


def _install_bin_wrappers(home: Path, repo_root: Path, python_exe: str) -> List[str]:
    """Create `fleet` and `remote-fleet-mcp` executables in `~/.local/bin`."""
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    src_dir = repo_root / "src"
    installed: List[str] = []

    if os.name == "nt":
        fleet_cmd = bin_dir / "fleet.cmd"
        fleet_cmd.write_text(
            f'@echo off\r\nset "PYTHONPATH={src_dir};%PYTHONPATH%"\r\n"{python_exe}" -m remote_fleet %*\r\n',
            encoding="utf-8",
        )
        mcp_cmd = bin_dir / "remote-fleet-mcp.cmd"
        mcp_cmd.write_text(
            f'@echo off\r\nset "PYTHONPATH={src_dir};%PYTHONPATH%"\r\n"{python_exe}" -m remote_fleet.mcp_server %*\r\n',
            encoding="utf-8",
        )
        installed.extend([str(fleet_cmd), str(mcp_cmd)])
    else:
        fleet_sh = bin_dir / "fleet"
        fleet_sh.write_text(
            f'#!/usr/bin/env sh\nexport PYTHONPATH="{src_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\nexec "{python_exe}" -m remote_fleet "$@"\n',
            encoding="utf-8",
        )
        _make_executable(fleet_sh)

        mcp_sh = bin_dir / "remote-fleet-mcp"
        mcp_sh.write_text(
            f'#!/usr/bin/env sh\nexport PYTHONPATH="{src_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\nexec "{python_exe}" -m remote_fleet.mcp_server "$@"\n',
            encoding="utf-8",
        )
        _make_executable(mcp_sh)
        installed.extend([str(fleet_sh), str(mcp_sh)])

    return installed


def _install_launchpad_and_mac_shortcut(
    home: Path, repo_root: Path, python_exe: str
) -> List[str]:
    """Create `~/Fleet/Start-Here` launchpad and clickable macOS `Pick Remote Folder.command`."""
    fleet_dir = home / "Fleet"
    launchpad = create_launchpad_workspace(str(fleet_dir), python_executable=python_exe)
    created = [str(launchpad)]

    if os.name != "nt":
        shortcut = fleet_dir / "Pick Remote Folder.command"
        src_dir = repo_root / "src"
        shortcut.write_text(
            "#!/usr/bin/env sh\n"
            f'export PYTHONPATH="{src_dir}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
            f'exec "{python_exe}" -m remote_fleet gui\n',
            encoding="utf-8",
        )
        _make_executable(shortcut)
        created.append(str(shortcut))
    return created


def _merge_mcp_json_file(
    target_json: Path,
    python_exe: str,
    repo_root: Path,
    root_key: str = "mcpServers",
) -> Path:
    """Idempotently register `remote-fleet` inside a JSON MCP configuration file."""
    target_json.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if target_json.exists():
        try:
            raw = target_json.read_text(encoding="utf-8").strip()
            if raw:
                data = json.loads(raw)
        except Exception:
            backup = target_json.with_suffix(target_json.suffix + ".bak")
            shutil.copy2(target_json, backup)
            data = {}

    if not isinstance(data.get(root_key), dict):
        data[root_key] = {}

    data[root_key]["remote-fleet"] = {
        "command": python_exe,
        "args": ["-m", "remote_fleet.mcp_server"],
        "env": {
            "PYTHONPATH": str(repo_root / "src"),
        },
    }
    target_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target_json


def _copy_skill_bundle(dest_skill_dir: Path, repo_root: Path) -> Path:
    """Copy `skills/remote-fleet/` (SKILL.md + launcher scripts) into a harness skill directory."""
    src_skill_dir = repo_root / "skills" / "remote-fleet"
    dest_skill_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_skill_dir / "SKILL.md", dest_skill_dir / "SKILL.md")

    src_scripts = src_skill_dir / "scripts"
    if src_scripts.is_dir():
        dest_scripts = dest_skill_dir / "scripts"
        dest_scripts.mkdir(parents=True, exist_ok=True)
        for item in src_scripts.iterdir():
            if item.is_file():
                target = dest_scripts / item.name
                shutil.copy2(item, target)
                if not item.name.endswith(".cmd"):
                    _make_executable(target)
        (dest_scripts / ".repo_src").write_text(str(repo_root / "src") + "\n", encoding="utf-8")
    return dest_skill_dir / "SKILL.md"


def _update_codex_toml(codex_toml: Path, python_exe: str, repo_root: Path) -> Path:
    """Idempotently register `[mcp_servers.remote_fleet]` inside `~/.codex/config.toml`."""
    codex_toml.parent.mkdir(parents=True, exist_ok=True)
    existing = codex_toml.read_text(encoding="utf-8") if codex_toml.exists() else ""

    py_escaped = python_exe.replace("\\", "\\\\")
    src_escaped = str(repo_root / "src").replace("\\", "\\\\")

    block = (
        f"{CODEX_BEGIN_MARKER}\n"
        f"[mcp_servers.remote_fleet]\n"
        f'command = "{py_escaped}"\n'
        f'args = ["-m", "remote_fleet.mcp_server"]\n'
        f'env = {{ PYTHONPATH = "{src_escaped}" }}\n'
        f"{CODEX_END_MARKER}\n"
    )

    pattern = re.compile(
        re.escape(CODEX_BEGIN_MARKER) + r".*?" + re.escape(CODEX_END_MARKER) + r"\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing)
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        extra_nl = "\n" if existing.strip() else ""
        updated = f"{existing}{sep}{extra_nl}{block}"

    codex_toml.write_text(updated, encoding="utf-8")
    return codex_toml


def install_all_harnesses(
    home_dir: Optional[Path] = None,
    harnesses: Optional[List[str]] = None,
    python_executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Install Remote Fleet CLI, Launchpad, Global Instructions, Skills, Slash Commands, and MCP configs."""
    home = (home_dir or Path.home()).expanduser().resolve()
    repo_root = get_repo_root()
    py_exe = python_executable or sys.executable or "python3"
    selected = {
        h.lower()
        for h in (
            harnesses
            or ["antigravity", "claude", "codex", "kimi", "dsh", "cursor"]
        )
    }

    report: Dict[str, List[str]] = {
        "cli": [],
        "launchpad": [],
        "config": [],
        "antigravity": [],
        "claude": [],
        "codex": [],
        "kimi": [],
        "dsh": [],
        "cursor": [],
    }

    # 1. CLI wrappers in ~/.local/bin + ~/Fleet/Start-Here launchpad + macOS GUI shortcut
    report["cli"].extend(_install_bin_wrappers(home, repo_root, py_exe))
    report["launchpad"].extend(_install_launchpad_and_mac_shortcut(home, repo_root, py_exe))

    # 2. Ensure fleet.json exists
    cfg_path = (
        home / ".config" / "remote-fleet" / "fleet.json"
        if home_dir is not None
        else get_config_path()
    )
    if not cfg_path.exists():
        save_fleet_config(default_home_fleet_config(), cfg_path)
    report["config"].append(str(cfg_path))

    # 3. Antigravity (Skills + MCP config + Global GEMINI.md rule)
    if "antigravity" in selected:
        report["antigravity"].append(
            str(_copy_skill_bundle(home / ".gemini" / "antigravity" / "skills" / "remote-fleet", repo_root))
        )
        report["antigravity"].append(
            str(_copy_skill_bundle(home / ".gemini" / "jetski" / "skills" / "remote-fleet", repo_root))
        )
        report["antigravity"].append(
            str(_merge_mcp_json_file(home / ".gemini" / "antigravity" / "mcp_config.json", py_exe, repo_root))
        )
        report["antigravity"].append(
            str(_upsert_global_agent_md(home / ".gemini" / "GEMINI.md"))
        )

    # 4. Claude Code & Claude Desktop (Skills + Slash Commands + MCP config + Global CLAUDE.md rule)
    if "claude" in selected:
        report["claude"].append(
            str(_copy_skill_bundle(home / ".claude" / "skills" / "remote-fleet", repo_root))
        )
        src_cmds = repo_root / "commands"
        if src_cmds.is_dir():
            dest_cmds = home / ".claude" / "commands"
            dest_cmds.mkdir(parents=True, exist_ok=True)
            for cmd_md in src_cmds.glob("*.md"):
                target_md = dest_cmds / cmd_md.name
                shutil.copy2(cmd_md, target_md)
                report["claude"].append(str(target_md))

        report["claude"].append(
            str(_merge_mcp_json_file(home / ".claude.json", py_exe, repo_root))
        )
        report["claude"].append(
            str(_upsert_global_agent_md(home / ".claude" / "CLAUDE.md"))
        )

        sys_name = platform.system().lower()
        if sys_name == "darwin":
            desktop_cfg = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        elif sys_name == "windows":
            appdata = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
            desktop_cfg = appdata / "Claude" / "claude_desktop_config.json"
        else:
            desktop_cfg = home / ".config" / "Claude" / "claude_desktop_config.json"

        if desktop_cfg.parent.exists():
            report["claude"].append(str(_merge_mcp_json_file(desktop_cfg, py_exe, repo_root)))

    # 5. OpenAI Codex (Skills + ~/.agents/skills + ~/.codex/config.toml MCP + Global AGENTS.md)
    if "codex" in selected:
        report["codex"].append(
            str(_copy_skill_bundle(home / ".codex" / "skills" / "remote-fleet", repo_root))
        )
        report["codex"].append(
            str(_copy_skill_bundle(home / ".agents" / "skills" / "remote-fleet", repo_root))
        )
        report["codex"].append(
            str(_update_codex_toml(home / ".codex" / "config.toml", py_exe, repo_root))
        )
        report["codex"].append(
            str(_upsert_global_agent_md(home / ".codex" / "AGENTS.md"))
        )

    # 6. Kimi Code (Skills + MCP config + Global AGENTS.md)
    if "kimi" in selected:
        report["kimi"].append(
            str(_copy_skill_bundle(home / ".kimi" / "skills" / "remote-fleet", repo_root))
        )
        report["kimi"].append(
            str(_copy_skill_bundle(home / ".config" / "agents" / "skills" / "remote-fleet", repo_root))
        )
        report["kimi"].append(
            str(_merge_mcp_json_file(home / ".kimi" / "mcp.json", py_exe, repo_root))
        )
        report["kimi"].append(
            str(_upsert_global_agent_md(home / ".kimi" / "AGENTS.md"))
        )

    # 7. DSH (Skill bundle + Global AGENTS.md)
    if "dsh" in selected:
        report["dsh"].append(
            str(_copy_skill_bundle(home / ".dsh" / "skills" / "remote-fleet", repo_root))
        )
        report["dsh"].append(
            str(_upsert_global_agent_md(home / ".dsh" / "AGENTS.md"))
        )

    # 8. Cursor (MCP config if ~/.cursor exists or explicitly requested)
    if "cursor" in selected and ((home / ".cursor").exists() or harnesses is not None):
        report["cursor"].append(
            str(_merge_mcp_json_file(home / ".cursor" / "mcp.json", py_exe, repo_root))
        )

    return {k: v for k, v in report.items() if v}
