"""Spec-compliant MCP (Model Context Protocol) stdio server for Remote Fleet."""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from remote_fleet import __version__
from remote_fleet.config import (
    ActiveProjectState,
    FleetConfig,
    MachineConfig,
    load_active_project,
    load_fleet_config,
    save_active_project,
)
from remote_fleet.mount import compute_local_workspace_dir, prepare_local_workspace, sync_workspace
from remote_fleet.transport import FleetTransport


SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}


SERVER_INSTRUCTIONS = (
    "Remote Fleet lets the user run this harness smoothly on their local interface machine (e.g., Mac Mini) "
    "while picking a folder, keeping all project files/changes/datasets, and running computations on any "
    "computer across their Home LAN or Tailscale (PC, DGX Spark 1, Spark 2, Spark 3, Mac Mini). "
    "When starting a new project, call `fleet_list_machines(probe=true, include_load=true)` and `fleet_browse_folders`, "
    "then `fleet_select_project` (or `fleet_create_project`). Once a remote project is selected, ALWAYS use "
    "`fleet_read_file`, `fleet_write_file`, `fleet_edit_file`, `fleet_list_files`, `fleet_search_files`, "
    "`fleet_run_command`, and `fleet_start_job` (for heavy/long calculations) so all changes and compute "
    "happen on the target computer without slowing down the local interface machine."
)


TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "fleet_list_machines",
        "description": (
            "List all computers in the Home LAN / Tailscale fleet (Mac Mini, PC, Sparks, plus "
            "any auto-discovered Tailscale nodes), showing whether each machine is LOCAL, reachable "
            "over Home LAN, or reachable over Tailscale, along with latency, optional CPU/GPU load, and active project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "probe": {
                    "type": "boolean",
                    "description": "Probe live LAN/Tailscale connectivity and latency (default true).",
                    "default": True,
                },
                "include_load": {
                    "type": "boolean",
                    "description": "Also query CPU load, free disk, and GPU utilization on online machines (default false).",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "fleet_browse_folders",
        "description": (
            "Browse directories on any computer in the fleet so the user can pick a project folder. "
            "Omit `path` to start at the computer's default project root (or `~`)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {
                    "type": "string",
                    "description": "Machine ID or name (e.g. 'spark-1', 'spark-2', 'spark-3', 'pc', 'mac-mini').",
                },
                "path": {
                    "type": "string",
                    "description": "Folder path to inspect on the target machine (optional; defaults to default root or `~`).",
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include dot-directories such as `.config` (default false).",
                    "default": False,
                },
            },
            "required": ["machine"],
        },
    },
    {
        "name": "fleet_select_project",
        "description": (
            "Select an existing (or optionally newly created) folder on a fleet computer as the active "
            "project workspace. Returns project details (git branch/status, OS, Python, GPUs, top files)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {
                    "type": "string",
                    "description": "Target machine ID or name (e.g. 'spark-1', 'pc', 'mac-mini').",
                },
                "path": {
                    "type": "string",
                    "description": "Folder path on the target machine (e.g. '~/projects/my-app').",
                },
                "create_if_missing": {
                    "type": "boolean",
                    "description": "Create the directory if it does not yet exist (default false).",
                    "default": False,
                },
                "create_local_link": {
                    "type": "boolean",
                    "description": "Also create/update a local linked folder under ~/Fleet/<machine>/<project> (default true).",
                    "default": True,
                },
            },
            "required": ["machine", "path"],
        },
    },
    {
        "name": "fleet_create_project",
        "description": (
            "Create a new project folder on any computer in the fleet ( optionally initializing git "
            "and a starter layout) and set it as the active project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "machine": {
                    "type": "string",
                    "description": "Target machine ID or name (e.g. 'spark-2', 'pc', 'mac-mini').",
                },
                "path": {
                    "type": "string",
                    "description": "New project directory path on that machine (e.g. '~/projects/new-repo').",
                },
                "init_git": {
                    "type": "boolean",
                    "description": "Initialize a git repository if not already initialized (default true).",
                    "default": True,
                },
                "template": {
                    "type": "string",
                    "enum": ["empty", "python", "node"],
                    "description": "Optional starter project scaffold ('empty', 'python', or 'node').",
                    "default": "empty",
                },
            },
            "required": ["machine", "path"],
        },
    },
    {
        "name": "fleet_current_project",
        "description": "Show the currently selected fleet machine and remote project folder.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "fleet_list_files",
        "description": "List files and directories inside the active project (or an explicit machine + path).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative subpath inside the active project, or an absolute path (default '').",
                    "default": "",
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override (defaults to active project's machine).",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum directory traversal depth (default 3).",
                    "default": 3,
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include dotfiles and ignored build directories (default false).",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "fleet_read_file",
        "description": "Read a text file from the active project (or a specified machine), optionally slicing by 1-based line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative to active project root, or absolute).",
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Optional 1-indexed starting line.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Optional 1-indexed ending line (inclusive).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "fleet_write_file",
        "description": "Create or overwrite a text file on the active project's computer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative to active project root, or absolute).",
                },
                "content": {
                    "type": "string",
                    "description": "Complete UTF-8 text content to write.",
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
                "create_dirs": {
                    "type": "boolean",
                    "description": "Create parent directories automatically if missing (default true).",
                    "default": True,
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "fleet_edit_file",
        "description": "Surgically replace exact text in an existing file on the active project's computer and return a unified diff.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (relative to active project root, or absolute).",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact existing substring to replace.",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences instead of requiring a unique match (default false).",
                    "default": False,
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "fleet_search_files",
        "description": "Search file contents by regex or substring on the active project's computer (uses ripgrep when available).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Regex or text pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional subdirectory or file path relative to project root.",
                    "default": "",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional filename glob filter (e.g. '*.py', '*.ts').",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matches to return (default 100).",
                    "default": 100,
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fleet_run_command",
        "description": (
            "Execute a shell command directly on the target computer (local or over Home LAN / Tailscale SSH) "
            "inside the active project folder. Ideal for git, tests, builds, package managers, and GPU jobs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute on the target computer.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory override (defaults to active project root).",
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override (defaults to active project's machine).",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default 120).",
                    "default": 120,
                },
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Optional environment variables to export for the command.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "fleet_sync_project",
        "description": "Sync files between the remote computer and a local mirror directory (`pull` or `push`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["pull", "push"],
                    "description": "'pull' remote files into local workspace, or 'push' local edits to remote.",
                },
                "local_path": {
                    "type": "string",
                    "description": "Optional local directory override (defaults to ~/Fleet/<machine>/<project>).",
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
                "remote_path": {
                    "type": "string",
                    "description": "Optional remote path override.",
                },
                "delete": {
                    "type": "boolean",
                    "description": "Delete extraneous destination files when using rsync (default false).",
                    "default": False,
                },
            },
            "required": ["direction"],
        },
    },
    {
        "name": "fleet_start_job",
        "description": (
            "Start a heavy calculation, training run, or long-running command detached in the background "
            "on the target computer (PC or Spark). Returns immediately with `job_id` so the harness on the "
            "Mac never blocks or slows down."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to launch in the background on the target computer.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory override (defaults to active project root).",
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
                "low_priority": {
                    "type": "boolean",
                    "description": "Run under `nice -n 10` so interactive SSH stays snappy (default false).",
                    "default": False,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "fleet_job_status",
        "description": (
            "Check status, exit code, and recent log output of a detached background job (`job_id`) "
            "on the target computer, or omit `job_id` to list recent background jobs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Background job ID returned by `fleet_start_job` (omit to list jobs).",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Number of recent log lines to return (default 60).",
                    "default": 60,
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
            },
        },
    },
    {
        "name": "fleet_stop_job",
        "description": "Terminate a running detached background job (`job_id`) on the target computer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Background job ID to stop.",
                },
                "machine": {
                    "type": "string",
                    "description": "Optional machine override.",
                },
            },
            "required": ["job_id"],
        },
    },
]


class RemoteFleetMCPServer:
    """Stateful MCP server handling machine discovery, folder selection, file ops, and remote execution."""

    def __init__(self, config: Optional[FleetConfig] = None) -> None:
        if config is not None:
            self.config = config
            self.tailscale_self = None
        else:
            self.config, self.tailscale_self = load_fleet_config(include_tailscale=True)
        self.transport = FleetTransport(self.config, self.tailscale_self)
        self.session_project: Optional[ActiveProjectState] = load_active_project()

    def reload_config(self) -> None:
        self.config, self.tailscale_self = load_fleet_config(include_tailscale=True)
        self.transport = FleetTransport(self.config, self.tailscale_self)

    def _resolve_machine_and_base(
        self, machine_arg: Optional[str] = None
    ) -> Tuple[Optional[MachineConfig], str, Optional[str]]:
        """Resolve target MachineConfig and base project directory."""
        if machine_arg:
            m = self.config.get_machine(machine_arg)
            if not m:
                available = ", ".join(x.id for x in self.config.machines)
                return None, "~", f"Unknown machine '{machine_arg}'. Available machines: {available}"
            base = (
                self.session_project.remote_path
                if (self.session_project and self.session_project.machine_id == m.id)
                else "~"
            )
            return m, base, None

        active = self.session_project or load_active_project()
        if not active:
            available = ", ".join(x.id for x in self.config.machines)
            return (
                None,
                "~",
                (
                    "No remote project is currently selected. First call `fleet_list_machines` / "
                    "`fleet_browse_folders` and then `fleet_select_project` (or pass `machine` explicitly). "
                    f"Configured machines: {available}"
                ),
            )
        self.session_project = active
        m = self.config.get_machine(active.machine_id)
        if not m:
            return None, "~", f"Active project references unknown machine '{active.machine_id}'."
        return m, active.remote_path, None

    @staticmethod
    def _tool_result(payload: Any, is_error: bool = False) -> Dict[str, Any]:
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        args = arguments or {}

        if name == "fleet_list_machines":
            probe = bool(args.get("probe", True))
            include_load = bool(args.get("include_load", False))
            ep_map = (
                self.transport.resolve_all_endpoints(self.config.machines, force_refresh=True)
                if (probe or include_load)
                else {}
            )
            machines_out = []
            for m in self.config.machines:
                entry: Dict[str, Any] = {
                    "id": m.id,
                    "name": m.name,
                    "lan_host": m.lan_host,
                    "tailscale_host": m.tailscale_host,
                    "os": m.os,
                    "default_roots": m.default_roots,
                    "tags": m.tags,
                }
                if m.id in ep_map:
                    ep = ep_map[m.id]
                    entry["status"] = ep.to_dict()
                    if include_load and ep.reachable:
                        metrics = self.transport.run_worker_op(m, {"op": "machine_metrics"}, timeout_sec=6)
                        if metrics.get("ok"):
                            entry["load"] = {
                                "cpu_count": metrics.get("cpu_count"),
                                "load_1m": metrics.get("load_1m"),
                                "load_5m": metrics.get("load_5m"),
                                "disk_free_gb": metrics.get("disk_free_gb"),
                                "gpus": metrics.get("gpus"),
                            }
                machines_out.append(entry)
            active = self.session_project or load_active_project()
            return self._tool_result(
                {
                    "machines": machines_out,
                    "active_project": active.to_dict() if active else None,
                }
            )

        if name == "fleet_browse_folders":
            machine_name = str(args.get("machine") or "").strip()
            m = self.config.get_machine(machine_name)
            if not m:
                available = ", ".join(x.id for x in self.config.machines)
                return self._tool_result(
                    f"Unknown machine '{machine_name}'. Available machines: {available}",
                    is_error=True,
                )
            res = self.transport.run_worker_op(
                m,
                {
                    "op": "browse_folders",
                    "path": str(args.get("path") or ""),
                    "include_hidden": bool(args.get("include_hidden", False)),
                    "default_roots": m.default_roots,
                },
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name in ("fleet_select_project", "fleet_create_project"):
            machine_name = str(args.get("machine") or "").strip()
            raw_path = str(args.get("path") or "").strip()
            m = self.config.get_machine(machine_name)
            if not m:
                available = ", ".join(x.id for x in self.config.machines)
                return self._tool_result(
                    f"Unknown machine '{machine_name}'. Available machines: {available}",
                    is_error=True,
                )
            create_flag = True if name == "fleet_create_project" else bool(args.get("create_if_missing", False))
            init_git = bool(args.get("init_git", name == "fleet_create_project"))
            template = str(args.get("template") or "empty") if name == "fleet_create_project" else ""

            res = self.transport.run_worker_op(
                m,
                {
                    "op": "inspect_project",
                    "path": raw_path,
                    "create_if_missing": create_flag,
                    "init_git": init_git,
                    "template": "" if template == "empty" else template,
                },
            )
            if not res.get("ok"):
                return self._tool_result(res, is_error=True)

            resolved_path = res["path"]
            local_ws = ""
            if bool(args.get("create_local_link", True)):
                ws_info = prepare_local_workspace(self.config, self.transport, m, resolved_path, mode="link")
                if ws_info.get("ok"):
                    local_ws = str(ws_info.get("local_path") or "")

            state = ActiveProjectState(
                machine_id=m.id,
                remote_path=resolved_path,
                local_workspace_path=local_ws,
                source="session",
            )
            self.session_project = state
            save_active_project(state)
            res["active_project"] = state.to_dict()
            return self._tool_result(res, is_error=False)

        if name == "fleet_current_project":
            active = self.session_project or load_active_project()
            if not active:
                return self._tool_result(
                    {"active_project": None, "message": "No active project selected yet."}
                )
            m = self.config.get_machine(active.machine_id)
            ep = self.transport.resolve_endpoint(m).to_dict() if m else None
            return self._tool_result({"active_project": active.to_dict(), "endpoint": ep})

        if name == "fleet_list_files":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            res = self.transport.run_worker_op(
                m,
                {
                    "op": "list_files",
                    "base_dir": base,
                    "path": str(args.get("path") or ""),
                    "max_depth": int(args.get("max_depth", 3)),
                    "include_hidden": bool(args.get("include_hidden", False)),
                },
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_read_file":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            res = self.transport.run_worker_op(
                m,
                {
                    "op": "read_file",
                    "base_dir": base,
                    "path": str(args["path"]),
                    "start_line": args.get("start_line"),
                    "end_line": args.get("end_line"),
                },
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_write_file":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            res = self.transport.run_worker_op(
                m,
                {
                    "op": "write_file",
                    "base_dir": base,
                    "path": str(args["path"]),
                    "content": str(args["content"]),
                    "create_dirs": bool(args.get("create_dirs", True)),
                },
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_edit_file":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            res = self.transport.run_worker_op(
                m,
                {
                    "op": "edit_file",
                    "base_dir": base,
                    "path": str(args["path"]),
                    "old_text": str(args["old_text"]),
                    "new_text": str(args["new_text"]),
                    "replace_all": bool(args.get("replace_all", False)),
                },
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_search_files":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            res = self.transport.run_worker_op(
                m,
                {
                    "op": "search_files",
                    "base_dir": base,
                    "path": str(args.get("path") or ""),
                    "query": str(args["query"]),
                    "file_pattern": str(args.get("file_pattern") or ""),
                    "max_results": int(args.get("max_results", 100)),
                },
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_run_command":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            cwd = str(args.get("cwd") or "").strip()
            if not cwd:
                cwd = base
            elif not cwd.startswith(("/", "~")) and not (len(cwd) > 2 and cwd[1] == ":"):
                cwd = f"{base.rstrip('/')}/{cwd}"
            res = self.transport.run_command(
                m,
                command=str(args["command"]),
                cwd=cwd,
                timeout_sec=int(args.get("timeout_seconds", 120)),
                env=args.get("env"),
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_sync_project":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            remote_path = str(args.get("remote_path") or base)
            local_override = str(args.get("local_path") or "").strip()
            local_dir = (
                compute_local_workspace_dir(self.config, m, remote_path)
                if not local_override
                else compute_local_workspace_dir(self.config, m, remote_path).__class__(local_override)
            )
            res = sync_workspace(
                self.transport,
                m,
                remote_path=remote_path,
                local_dir=local_dir,
                direction=str(args["direction"]),
                delete=bool(args.get("delete", False)),
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_start_job":
            m, base, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            cwd = str(args.get("cwd") or "").strip() or base
            res = self.transport.run_worker_op(
                m,
                {
                    "op": "start_job",
                    "command": str(args["command"]),
                    "cwd": cwd,
                    "low_priority": bool(args.get("low_priority", False)),
                },
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_job_status":
            m, _, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            job_id = str(args.get("job_id") or "").strip()
            if not job_id:
                res = self.transport.run_worker_op(m, {"op": "list_jobs"})
            else:
                res = self.transport.run_worker_op(
                    m,
                    {
                        "op": "job_status",
                        "job_id": job_id,
                        "tail_lines": int(args.get("tail_lines", 60)),
                    },
                )
            return self._tool_result(res, is_error=not res.get("ok", False))

        if name == "fleet_stop_job":
            m, _, err = self._resolve_machine_and_base(args.get("machine"))
            if err or not m:
                return self._tool_result(err, is_error=True)
            res = self.transport.run_worker_op(
                m,
                {"op": "stop_job", "job_id": str(args["job_id"])},
            )
            return self._tool_result(res, is_error=not res.get("ok", False))

        return self._tool_result(f"Unknown tool: {name}", is_error=True)

    def handle_request(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process a single JSON-RPC 2.0 request/notification and return a JSON-RPC response (or None)."""
        method = msg.get("method", "")
        req_id = msg.get("id")

        # Notifications do not carry an `id` and must not receive a response
        if "id" not in msg:
            return None

        try:
            if method == "initialize":
                params = msg.get("params") or {}
                requested_ver = str(params.get("protocolVersion") or "2024-11-05")
                negotiated_ver = (
                    requested_ver if requested_ver in SUPPORTED_PROTOCOL_VERSIONS else "2024-11-05"
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": negotiated_ver,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"subscribe": False, "listChanged": False},
                            "prompts": {"listChanged": False},
                        },
                        "serverInfo": {
                            "name": "remote-fleet",
                            "version": __version__,
                        },
                        "instructions": SERVER_INSTRUCTIONS,
                    },
                }

            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SCHEMA}}

            if method == "tools/call":
                params = msg.get("params") or {}
                tool_name = str(params.get("name") or "")
                tool_args = params.get("arguments") or {}
                result = self.call_tool(tool_name, tool_args)
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            if method == "resources/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "resources": [
                            {
                                "uri": "fleet://machines",
                                "name": "Configured Fleet Machines",
                                "description": "Computers across your Home LAN and Tailscale",
                                "mimeType": "application/json",
                            },
                            {
                                "uri": "fleet://active-project",
                                "name": "Active Fleet Project",
                                "description": "Currently selected machine and project folder",
                                "mimeType": "application/json",
                            },
                        ]
                    },
                }

            if method == "resources/read":
                uri = str((msg.get("params") or {}).get("uri") or "")
                if uri == "fleet://machines":
                    text = json.dumps(self.config.to_dict(), indent=2)
                elif uri == "fleet://active-project":
                    active = self.session_project or load_active_project()
                    text = json.dumps(active.to_dict() if active else None, indent=2)
                else:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"Unknown resource URI: {uri}"},
                    }
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [{"uri": uri, "mimeType": "application/json", "text": text}]
                    },
                }

            if method == "prompts/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "prompts": [
                            {
                                "name": "pick-remote-project",
                                "description": "Browse all Home LAN / Tailscale computers and pick or create a project folder.",
                                "arguments": [
                                    {
                                        "name": "machine",
                                        "description": "Optional machine name (e.g. spark-1, pc, mac-mini)",
                                        "required": False,
                                    }
                                ],
                            },
                            {
                                "name": "fleet-status",
                                "description": "Check reachability and latency of all fleet computers across Home LAN and Tailscale.",
                                "arguments": [],
                            },
                        ]
                    },
                }

            if method == "prompts/get":
                params = msg.get("params") or {}
                pname = str(params.get("name") or "")
                pargs = params.get("arguments") or {}
                if pname == "pick-remote-project":
                    hint = pargs.get("machine")
                    user_text = (
                        f"Please browse folders on `{hint}` using `fleet_browse_folders` and help me pick or create a project folder."
                        if hint
                        else "Please list my Home LAN / Tailscale computers with `fleet_list_machines`, show which ones are online, and help me browse and pick (or create) a project folder on one of them."
                    )
                elif pname == "fleet-status":
                    user_text = "Please run `fleet_list_machines` with `probe=true` and `fleet_current_project` to summarize the live status of my fleet."
                else:
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"Unknown prompt: {pname}"},
                    }
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "messages": [
                            {"role": "user", "content": {"type": "text", "text": user_text}}
                        ]
                    },
                }

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal server error: {exc}"},
            }


def _read_message(stream: BinaryIO) -> Tuple[Optional[Dict[str, Any]], str]:
    """Read a JSON-RPC message supporting both newline-delimited JSON and Content-Length framing."""
    while True:
        line = stream.readline()
        if not line:
            return None, "ndjson"
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            # Consume remaining headers up to blank line
            while True:
                hdr = stream.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            body = stream.read(length)
            if not body:
                return None, "lsp"
            return json.loads(body.decode("utf-8")), "lsp"
        return json.loads(stripped.decode("utf-8")), "ndjson"


def _write_message(stream: BinaryIO, payload: Dict[str, Any], framing: str) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if framing == "lsp":
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        stream.write(header + encoded)
    else:
        stream.write(encoded + b"\n")
    stream.flush()


def serve_stdio(
    in_stream: Optional[BinaryIO] = None, out_stream: Optional[BinaryIO] = None
) -> int:
    """Run the MCP server loop over stdio."""
    reader = in_stream or sys.stdin.buffer
    writer = out_stream or sys.stdout.buffer
    server = RemoteFleetMCPServer()

    while True:
        try:
            msg, framing = _read_message(reader)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"[remote-fleet-mcp] Invalid JSON on stdin: {exc}\n")
            continue
        if msg is None:
            break
        response = server.handle_request(msg)
        if response is not None:
            _write_message(writer, response, framing)
    return 0


def main() -> int:
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
