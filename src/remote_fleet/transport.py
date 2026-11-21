"""Transport layer: LAN-first / Tailscale-fallback endpoint resolution, local & SSH execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import difflib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from remote_fleet.config import (
    FleetConfig,
    MachineConfig,
    TailscaleNodeInfo,
    is_local_tailscale_running,
    is_self_machine,
    is_tailscale_only_host,
)


IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    "target",
    "dist",
    "build",
}


@dataclass
class EndpointStatus:
    """Reachability and transport details for a machine."""

    machine_id: str
    transport: str  # "local" | "lan" | "tailscale" | "unreachable"
    host: str
    port: int
    reachable: bool
    latency_ms: Optional[float] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "transport": self.transport,
            "host": self.host,
            "port": self.port,
            "reachable": self.reachable,
            "latency_ms": round(self.latency_ms, 2) if self.latency_ms is not None else None,
            "error": self.error,
        }


# Embedded pure-Python worker executed identically in-process (for local machine)
# or over SSH stdin (for remote Linux, macOS, WSL, or Windows machines with Python).
REMOTE_WORKER_SOURCE = r'''
import difflib
import fnmatch
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", "target", "dist", "build",
}

def _expand(p: str) -> Path:
    if not p or p == "~":
        return Path.home().resolve()
    expanded = os.path.expanduser(os.path.expandvars(p))
    return Path(expanded).resolve()

def _resolve_target(base_dir: str, rel_or_abs: str) -> Path:
    if not rel_or_abs:
        return _expand(base_dir)
    expanded = os.path.expanduser(os.path.expandvars(rel_or_abs))
    p = Path(expanded)
    if p.is_absolute():
        return p.resolve()
    return (_expand(base_dir) / p).resolve()

def _windows_drives():
    if os.name != "nt":
        return []
    drives = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(root)
    return drives

def handle(req: dict) -> dict:
    op = req.get("op")
    if op == "ping":
        return {
            "ok": True,
            "hostname": socket.gethostname(),
            "os": platform.system().lower(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "home": str(Path.home()),
        }

    if op == "browse_folders":
        raw_path = (req.get("path") or "").strip()
        include_hidden = bool(req.get("include_hidden", False))
        default_roots = req.get("default_roots") or ["~/projects", "~"]
        resolved_roots = []
        for r in default_roots:
            rp = _expand(r)
            resolved_roots.append({"label": r, "path": str(rp), "exists": rp.is_dir()})
        for drv in _windows_drives():
            resolved_roots.append({"label": drv, "path": drv, "exists": True})

        if not raw_path:
            # Pick first existing default root or home
            target = Path.home().resolve()
            for item in resolved_roots:
                if item["exists"]:
                    target = Path(item["path"]).resolve()
                    break
        else:
            target = _expand(raw_path)

        if not target.exists():
            return {
                "ok": False,
                "error": f"Path does not exist: {target}",
                "requested_path": str(target),
                "roots": resolved_roots,
            }
        if not target.is_dir():
            target = target.parent

        dirs = []
        files = []
        try:
            for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
                if not include_hidden and entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    is_git = (entry / ".git").exists()
                    dirs.append({
                        "name": entry.name,
                        "path": str(entry),
                        "is_git_repo": is_git,
                    })
                elif entry.is_file():
                    files.append(entry.name)
        except PermissionError as exc:
            return {
                "ok": False,
                "error": f"Permission denied reading {target}: {exc}",
                "path": str(target),
                "roots": resolved_roots,
            }

        parent = str(target.parent) if target.parent != target else ""
        return {
            "ok": True,
            "path": str(target),
            "parent": parent,
            "is_git_repo": (target / ".git").exists(),
            "directories": dirs,
            "file_count": len(files),
            "sample_files": files[:30],
            "roots": resolved_roots,
        }

    if op == "inspect_project":
        target = _expand(req["path"])
        create_if_missing = bool(req.get("create_if_missing", False))
        init_git = bool(req.get("init_git", False))
        template = (req.get("template") or "").strip().lower()

        created = False
        if not target.exists():
            if not create_if_missing:
                return {"ok": False, "error": f"Folder does not exist: {target}"}
            target.mkdir(parents=True, exist_ok=True)
            created = True

        if not target.is_dir():
            return {"ok": False, "error": f"Path is not a directory: {target}"}

        if template == "python" and not (target / "pyproject.toml").exists():
            pkg_name = re.sub(r"[^a-zA-Z0-9_]", "_", target.name.lower()).strip("_") or "app"
            (target / "pyproject.toml").write_text(
                f'[project]\nname = "{target.name}"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n',
                encoding="utf-8",
            )
            (target / "README.md").write_text(f"# {target.name}\n", encoding="utf-8")
            src_dir = target / "src" / pkg_name
            src_dir.mkdir(parents=True, exist_ok=True)
            (src_dir / "__init__.py").write_text("", encoding="utf-8")
        elif template == "node" and not (target / "package.json").exists():
            (target / "package.json").write_text(
                json.dumps({"name": target.name.lower(), "version": "0.1.0", "private": True}, indent=2) + "\n",
                encoding="utf-8",
            )
            (target / "README.md").write_text(f"# {target.name}\n", encoding="utf-8")

        if init_git and not (target / ".git").exists() and shutil.which("git"):
            subprocess.run(["git", "init"], cwd=str(target), capture_output=True, text=True, check=False)

        is_git = (target / ".git").exists()
        git_branch = ""
        git_status = ""
        if is_git and shutil.which("git"):
            try:
                b = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=str(target), capture_output=True, text=True, timeout=3, check=False
                )
                if b.returncode == 0:
                    git_branch = b.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                pass
            try:
                s = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=str(target), capture_output=True, text=True, timeout=3, check=False
                )
                if s.returncode == 0:
                    git_status = "\n".join(s.stdout.splitlines()[:20])
            except (subprocess.TimeoutExpired, OSError):
                pass

        gpu_info = []
        if shutil.which("nvidia-smi"):
            try:
                g = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=3, check=False
                )
                if g.returncode == 0 and g.stdout.strip():
                    gpu_info = [line.strip() for line in g.stdout.splitlines() if line.strip()]
            except (subprocess.TimeoutExpired, OSError):
                gpu_info = ["GPU busy (nvidia-smi timed out)"]

        entries = []
        for e in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:40]:
            entries.append(e.name + ("/" if e.is_dir() else ""))

        return {
            "ok": True,
            "path": str(target),
            "created": created,
            "hostname": socket.gethostname(),
            "os": platform.system().lower(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "is_git_repo": is_git,
            "git_branch": git_branch,
            "git_status_short": git_status,
            "gpus": gpu_info,
            "entries": entries,
        }

    if op == "list_files":
        root = _resolve_target(req.get("base_dir", "~"), req.get("path", ""))
        max_depth = int(req.get("max_depth", 3))
        include_hidden = bool(req.get("include_hidden", False))
        max_entries = int(req.get("max_entries", 500))
        if not root.exists():
            return {"ok": False, "error": f"Path does not exist: {root}"}
        if root.is_file():
            return {"ok": True, "root": str(root), "items": [{"path": root.name, "type": "file", "size": root.stat().st_size}]}

        items = []
        truncated = False

        def _walk(cur: Path, depth: int):
            nonlocal truncated
            if depth > max_depth or truncated:
                return
            try:
                children = sorted(cur.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except PermissionError:
                return
            for child in children:
                if len(items) >= max_entries:
                    truncated = True
                    return
                if not include_hidden and child.name.startswith("."):
                    continue
                if child.is_dir() and child.name in IGNORED_DIRS and not include_hidden:
                    continue
                rel = str(child.relative_to(root)).replace("\\", "/")
                if child.is_dir():
                    items.append({"path": rel + "/", "type": "dir"})
                    _walk(child, depth + 1)
                elif child.is_file():
                    try:
                        sz = child.stat().st_size
                    except OSError:
                        sz = 0
                    items.append({"path": rel, "type": "file", "size": sz})

        _walk(root, 1)
        return {"ok": True, "root": str(root), "items": items, "truncated": truncated}

    if op == "read_file":
        target = _resolve_target(req.get("base_dir", "~"), req["path"])
        if not target.exists():
            return {"ok": False, "error": f"File not found: {target}"}
        if not target.is_file():
            return {"ok": False, "error": f"Not a regular file: {target}"}
        raw = target.read_bytes()
        if b"\x00" in raw[:8192]:
            return {"ok": False, "error": f"Binary file cannot be read as text ({len(raw)} bytes): {target}"}
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        total = len(lines)
        start = int(req.get("start_line") or 1)
        end = int(req.get("end_line") or total)
        start = max(1, start)
        end = min(total, max(start, end)) if total > 0 else 0
        sliced = "".join(lines[start - 1 : end]) if total > 0 else ""
        return {
            "ok": True,
            "path": str(target),
            "size_bytes": len(raw),
            "total_lines": total,
            "start_line": start if total > 0 else 0,
            "end_line": end,
            "content": sliced,
        }

    if op == "write_file":
        target = _resolve_target(req.get("base_dir", "~"), req["path"])
        content = str(req.get("content", ""))
        if req.get("create_dirs", True):
            target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        target.write_bytes(data)
        return {
            "ok": True,
            "path": str(target),
            "bytes_written": len(data),
            "lines_written": len(content.splitlines()),
        }

    if op == "edit_file":
        target = _resolve_target(req.get("base_dir", "~"), req["path"])
        if not target.is_file():
            return {"ok": False, "error": f"File not found: {target}"}
        old_text = str(req["old_text"])
        new_text = str(req["new_text"])
        replace_all = bool(req.get("replace_all", False))
        original = target.read_text(encoding="utf-8")
        count = original.count(old_text)
        if count == 0:
            return {"ok": False, "error": f"old_text not found in {target}"}
        if count > 1 and not replace_all:
            return {
                "ok": False,
                "error": f"old_text matched {count} times in {target}; provide more surrounding context or set replace_all=true",
            }
        updated = original.replace(old_text, new_text) if replace_all else original.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")
        diff_lines = list(
            difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=f"a/{target.name}",
                tofile=f"b/{target.name}",
                lineterm="",
            )
        )
        return {
            "ok": True,
            "path": str(target),
            "replacements": count if replace_all else 1,
            "diff": "\n".join(diff_lines[:120]),
        }

    if op == "search_files":
        root = _resolve_target(req.get("base_dir", "~"), req.get("path", ""))
        query = str(req["query"])
        file_pattern = str(req.get("file_pattern") or "").strip()
        max_results = int(req.get("max_results") or 100)
        if not root.exists():
            return {"ok": False, "error": f"Search path does not exist: {root}"}

        matches = []
        if shutil.which("rg"):
            cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--max-count", str(max_results)]
            if file_pattern:
                cmd.extend(["--glob", file_pattern])
            cmd.extend([query, str(root)])
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
                for line in proc.stdout.splitlines():
                    if len(matches) >= max_results:
                        break
                    parts = line.split(":", 2)
                    if len(parts) == 3:
                        fpath, lno, snippet = parts
                        try:
                            rel = str(Path(fpath).resolve().relative_to(root)).replace("\\", "/")
                        except Exception:
                            rel = fpath
                        matches.append({"file": rel, "line": int(lno) if lno.isdigit() else 1, "text": snippet[:300]})
                return {"ok": True, "root": str(root), "matches": matches, "engine": "ripgrep"}
            except (subprocess.TimeoutExpired, OSError):
                pass

        try:
            pattern = re.compile(query)
        except re.error:
            pattern = re.compile(re.escape(query))

        def _search_dir(cur: Path):
            if len(matches) >= max_results:
                return
            try:
                entries = sorted(cur.iterdir(), key=lambda x: x.name.lower())
            except PermissionError:
                return
            for entry in entries:
                if len(matches) >= max_results:
                    return
                if entry.name.startswith(".") or (entry.is_dir() and entry.name in IGNORED_DIRS):
                    continue
                if entry.is_dir():
                    _search_dir(entry)
                elif entry.is_file():
                    if file_pattern and not fnmatch.fnmatch(entry.name, file_pattern):
                        continue
                    try:
                        raw = entry.read_bytes()
                        if len(raw) > 2_000_000 or b"\x00" in raw[:4096]:
                            continue
                        text = raw.decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    rel = str(entry.relative_to(root)).replace("\\", "/")
                    for idx, line in enumerate(text.splitlines(), 1):
                        if pattern.search(line):
                            matches.append({"file": rel, "line": idx, "text": line[:300]})
                            if len(matches) >= max_results:
                                return

        if root.is_file():
            text = root.read_text(encoding="utf-8", errors="replace")
            for idx, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append({"file": root.name, "line": idx, "text": line[:300]})
        else:
            _search_dir(root)
        return {"ok": True, "root": str(root), "matches": matches, "engine": "python"}

    if op == "machine_metrics":
        import time as _time
        cpu_count = os.cpu_count() or 1
        load_1m = None
        load_5m = None
        if hasattr(os, "getloadavg"):
            try:
                l1, l5, _ = os.getloadavg()
                load_1m = round(l1, 2)
                load_5m = round(l5, 2)
            except OSError:
                pass
        disk = shutil.disk_usage(str(Path.home()))
        gpus = []
        if shutil.which("nvidia-smi"):
            try:
                g = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                if g.returncode == 0:
                    for line in g.stdout.splitlines():
                        if line.strip():
                            gpus.append(line.strip())
            except (subprocess.TimeoutExpired, OSError):
                gpus.append("GPU busy (nvidia-smi timed out)")
        return {
            "ok": True,
            "hostname": socket.gethostname(),
            "os": platform.system().lower(),
            "cpu_count": cpu_count,
            "load_1m": load_1m,
            "load_5m": load_5m,
            "disk_free_gb": round(disk.free / (1024 ** 3), 1),
            "disk_total_gb": round(disk.total / (1024 ** 3), 1),
            "gpus": gpus,
        }

    if op == "start_job":
        import signal as _signal
        import time as _time
        import uuid as _uuid

        cwd_path = _expand(req.get("cwd") or "~")
        if not cwd_path.is_dir():
            return {"ok": False, "error": f"Working directory does not exist: {cwd_path}"}
        command = str(req["command"])
        low_priority = bool(req.get("low_priority", False))
        job_id = str(req.get("job_id") or f"job-{int(_time.time())}-{_uuid.uuid4().hex[:6]}")
        jobs_root = Path.home() / ".cache" / "remote-fleet" / "jobs"
        job_dir = jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        log_file = job_dir / "output.log"
        meta_file = job_dir / "meta.json"
        done_file = job_dir / "done.json"
        runner_file = job_dir / "runner.py"

        actual_cmd = f"nice -n 10 {command}" if (low_priority and os.name == "posix") else command
        runner_src = (
            "import json, os, subprocess, time\n"
            f"log_path = {repr(str(log_file))}\n"
            f"done_path = {repr(str(done_file))}\n"
            f"cwd = {repr(str(cwd_path))}\n"
            f"cmd = {repr(actual_cmd)}\n"
            "with open(log_path, 'wb') as lf:\n"
            "    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT)\n"
            "    rc = proc.wait()\n"
            "with open(done_path, 'w', encoding='utf-8') as df:\n"
            "    json.dump({'exit_code': rc, 'finished_at': time.time()}, df)\n"
        )
        runner_file.write_text(runner_src, encoding="utf-8")

        popen_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(cwd_path),
            "close_fds": True,
        }
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            popen_kwargs["creationflags"] = flags
        else:
            popen_kwargs["start_new_session"] = True

        child = subprocess.Popen([sys.executable, str(runner_file)], **popen_kwargs)
        meta = {
            "job_id": job_id,
            "pid": child.pid,
            "command": command,
            "cwd": str(cwd_path),
            "started_at": _time.time(),
            "log_path": str(log_file),
        }
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {"ok": True, **meta}

    if op in ("job_status", "job_logs", "stop_job"):
        import signal as _signal
        job_id = str(req["job_id"])
        job_dir = Path.home() / ".cache" / "remote-fleet" / "jobs" / job_id
        meta_file = job_dir / "meta.json"
        done_file = job_dir / "done.json"
        log_file = job_dir / "output.log"
        if not meta_file.is_file():
            return {"ok": False, "error": f"Unknown job_id '{job_id}' on {socket.gethostname()}"}
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        pid = int(meta["pid"])

        done_info = json.loads(done_file.read_text(encoding="utf-8")) if done_file.is_file() else None
        running = False
        if done_info is None:
            try:
                os.kill(pid, 0)
                running = True
            except OSError:
                running = False

        if op == "stop_job":
            if running:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
                    else:
                        os.killpg(pid, _signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid, _signal.SIGTERM)
                    except Exception:
                        pass
            return {"ok": True, "job_id": job_id, "stopped": True, "was_running": running}

        tail_lines = int(req.get("tail_lines", 60))
        log_tail = ""
        if log_file.is_file():
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-tail_lines:])

        return {
            "ok": True,
            "job_id": job_id,
            "pid": pid,
            "command": meta.get("command"),
            "cwd": meta.get("cwd"),
            "running": running,
            "exit_code": done_info.get("exit_code") if done_info else None,
            "log_path": str(log_file),
            "log_tail": log_tail,
        }

    if op == "list_jobs":
        jobs_root = Path.home() / ".cache" / "remote-fleet" / "jobs"
        if not jobs_root.is_dir():
            return {"ok": True, "jobs": []}
        jobs = []
        for entry in sorted(jobs_root.iterdir(), reverse=True)[:30]:
            mf = entry / "meta.json"
            df = entry / "done.json"
            if not mf.is_file():
                continue
            meta = json.loads(mf.read_text(encoding="utf-8"))
            done_info = json.loads(df.read_text(encoding="utf-8")) if df.is_file() else None
            running = False
            if done_info is None:
                try:
                    os.kill(int(meta["pid"]), 0)
                    running = True
                except OSError:
                    running = False
            jobs.append({
                "job_id": meta["job_id"],
                "pid": meta["pid"],
                "command": meta.get("command"),
                "cwd": meta.get("cwd"),
                "running": running,
                "exit_code": done_info.get("exit_code") if done_info else None,
            })
        return {"ok": True, "jobs": jobs}

    return {"ok": False, "error": f"Unknown worker op: {op}"}
'''


def _run_local_worker(req: Dict[str, Any]) -> Dict[str, Any]:
    """Execute `REMOTE_WORKER_SOURCE` directly in the current Python process."""
    ns: Dict[str, Any] = {}
    exec(REMOTE_WORKER_SOURCE, ns)
    return ns["handle"](req)


class FleetTransport:
    """Manages LAN/Tailscale endpoint probing, local execution, and SSH remote execution."""

    def __init__(
        self,
        config: FleetConfig,
        tailscale_self: Optional[TailscaleNodeInfo] = None,
        endpoint_cache_ttl_sec: float = 30.0,
    ) -> None:
        self.config = config
        self.tailscale_self = tailscale_self
        self.endpoint_cache_ttl_sec = endpoint_cache_ttl_sec
        self._endpoint_cache: Dict[str, Tuple[float, EndpointStatus]] = {}

    def _ssh_binary(self) -> str:
        return os.environ.get("REMOTE_FLEET_SSH_BIN") or shutil.which("ssh") or "ssh"

    @staticmethod
    def _tcp_probe(host: str, port: int, timeout_sec: float) -> Tuple[bool, Optional[float], str]:
        if not host:
            return False, None, "empty host"
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout_sec):
                elapsed_ms = (time.monotonic() - start) * 1000.0
                return True, elapsed_ms, ""
        except Exception as exc:
            return False, None, str(exc)

    def resolve_endpoint(
        self, machine: MachineConfig, force_refresh: bool = False
    ) -> EndpointStatus:
        """Resolve whether `machine` is local, reachable via Home LAN, or reachable via Tailscale."""
        now = time.monotonic()
        if not force_refresh and machine.id in self._endpoint_cache:
            cached_at, cached_ep = self._endpoint_cache[machine.id]
            if now - cached_at < self.endpoint_cache_ttl_sec and cached_ep.reachable:
                return cached_ep

        if is_self_machine(machine, self.tailscale_self):
            ep = EndpointStatus(
                machine_id=machine.id,
                transport="local",
                host="localhost",
                port=0,
                reachable=True,
                latency_ms=0.0,
            )
            self._endpoint_cache[machine.id] = (now, ep)
            return ep

        # Custom SSH binary override (e.g. testing or proxy wrapper): skip raw TCP check if requested
        if os.environ.get("REMOTE_FLEET_SKIP_TCP_PROBE") == "1":
            chosen_host = (
                (machine.lan_host or machine.tailscale_host)
                if self.config.prefer_lan
                else (machine.tailscale_host or machine.lan_host)
            )
            transport_kind = (
                "lan"
                if (chosen_host == machine.lan_host and machine.lan_host)
                else "tailscale"
            )
            ep = EndpointStatus(
                machine_id=machine.id,
                transport=transport_kind,
                host=chosen_host or machine.id,
                port=machine.port,
                reachable=True,
                latency_ms=0.5,
            )
            self._endpoint_cache[machine.id] = (now, ep)
            return ep

        candidates: List[Tuple[str, str, float]] = []
        base_lan_timeout = max(0.25, self.config.lan_probe_timeout_ms / 1000.0)
        # macOS Bonjour multicast DNS (`.local`) can take ~0.5-1.1s on a cold cache lookup
        lan_timeout = (
            max(1.25, base_lan_timeout)
            if (machine.lan_host and machine.lan_host.lower().endswith(".local"))
            else base_lan_timeout
        )
        ts_timeout = max(0.5, float(self.config.ssh_connect_timeout_sec))

        if self.config.prefer_lan:
            if machine.lan_host:
                candidates.append(("lan", machine.lan_host, lan_timeout))
            if machine.tailscale_host and machine.tailscale_host != machine.lan_host:
                candidates.append(("tailscale", machine.tailscale_host, ts_timeout))
        else:
            if machine.tailscale_host:
                candidates.append(("tailscale", machine.tailscale_host, ts_timeout))
            if machine.lan_host and machine.lan_host != machine.tailscale_host:
                candidates.append(("lan", machine.lan_host, lan_timeout))

        errors: List[str] = []
        local_ts_checked = False
        local_ts_running = False
        for transport_name, host, timeout_s in candidates:
            if is_tailscale_only_host(host):
                if not local_ts_checked:
                    local_ts_running = (self.tailscale_self is not None) or is_local_tailscale_running()
                    local_ts_checked = True
                if not local_ts_running:
                    errors.append(f"{transport_name}({host}): skipped (Tailscale not installed/running on this computer)")
                    continue
            ok, latency_ms, err = self._tcp_probe(host, machine.port, timeout_s)
            if ok:
                ep = EndpointStatus(
                    machine_id=machine.id,
                    transport=transport_name,
                    host=host,
                    port=machine.port,
                    reachable=True,
                    latency_ms=latency_ms,
                )
                self._endpoint_cache[machine.id] = (now, ep)
                return ep
            errors.append(f"{transport_name}({host}:{machine.port}): {err}")

        fallback_host = machine.lan_host or machine.tailscale_host or machine.id
        ep = EndpointStatus(
            machine_id=machine.id,
            transport="unreachable",
            host=fallback_host,
            port=machine.port,
            reachable=False,
            error="; ".join(errors) if errors else "No LAN or Tailscale host configured",
        )
        return ep

    def resolve_all_endpoints(
        self, machines: List[MachineConfig], force_refresh: bool = False
    ) -> Dict[str, EndpointStatus]:
        """Probe all machines in parallel so offline machines never add sequential latency."""
        if not machines:
            return {}
        workers = min(8, len(machines))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                m.id: pool.submit(self.resolve_endpoint, m, force_refresh)
                for m in machines
            }
            return {mid: fut.result() for mid, fut in futures.items()}

    def build_ssh_args(
        self,
        machine: MachineConfig,
        host: str,
        use_control_master: bool = True,
    ) -> List[str]:
        """Construct OpenSSH invocation arguments for `machine` at `host`."""
        args = [
            self._ssh_binary(),
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.config.ssh_connect_timeout_sec}",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if use_control_master and os.name == "posix" and not os.environ.get("REMOTE_FLEET_SSH_BIN"):
            args.extend(
                [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    "ControlPersist=60",
                    "-o",
                    "ControlPath=/tmp/rf-ssh-%C",
                ]
            )
        if machine.port and machine.port != 22:
            args.extend(["-p", str(machine.port)])
        if machine.ssh_identity_file:
            args.extend(["-i", os.path.expanduser(machine.ssh_identity_file)])
        if machine.ssh_extra_args:
            args.extend(machine.ssh_extra_args)

        target = f"{machine.user}@{host}" if machine.user else host
        args.append(target)
        return args

    def run_worker_op(
        self, machine: MachineConfig, req: Dict[str, Any], timeout_sec: int = 30
    ) -> Dict[str, Any]:
        """Execute a structured filesystem/project operation on `machine` (local or remote SSH)."""
        ep = self.resolve_endpoint(machine)
        if ep.transport == "local":
            res = _run_local_worker(req)
            res["endpoint"] = ep.to_dict()
            return res

        if not ep.reachable:
            return {
                "ok": False,
                "error": (
                    f"Machine '{machine.id}' ({machine.name}) is currently unreachable over "
                    f"Home LAN ({machine.lan_host or 'n/a'}) and Tailscale ({machine.tailscale_host or 'n/a'}). "
                    f"Details: {ep.error}"
                ),
                "endpoint": ep.to_dict(),
            }

        envelope = json.dumps({"code": REMOTE_WORKER_SOURCE, "req": req})

        # Bootstrap command compatible with POSIX shells (Linux/macOS) and Windows (CMD/PowerShell)
        if machine.os == "windows":
            remote_py_cmd = (
                'python -c "import json,sys; d=json.loads(sys.stdin.read()); '
                'ns={}; exec(d[\'code\'], ns); print(json.dumps(ns[\'handle\'](d[\'req\'])))"'
            )
        else:
            py_snippet = (
                "import json,sys; d=json.loads(sys.stdin.read()); "
                "ns={}; exec(d['code'], ns); print(json.dumps(ns['handle'](d['req'])))"
            )
            remote_py_cmd = (
                "PY=$(command -v python3 || command -v /opt/homebrew/bin/python3 || "
                "command -v /usr/local/bin/python3 || command -v python); "
                f"\"$PY\" -c {shlex.quote(py_snippet)}"
            )

        ssh_cmd = [*self.build_ssh_args(machine, ep.host), remote_py_cmd]
        try:
            proc = subprocess.run(
                ssh_cmd,
                input=envelope,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {
                "ok": False,
                "error": f"SSH execution timed out/failed on '{machine.id}' ({ep.transport}:{ep.host}): {exc}",
                "endpoint": ep.to_dict(),
            }

        # If POSIX shell lookup failed on an `os="auto"` host that turns out to be Windows OpenSSH:
        if proc.returncode != 0 and machine.os == "auto":
            win_cmd = (
                'python -c "import json,sys; d=json.loads(sys.stdin.read()); '
                'ns={}; exec(d[\'code\'], ns); print(json.dumps(ns[\'handle\'](d[\'req\'])))"'
            )
            ssh_win = [*self.build_ssh_args(machine, ep.host), win_cmd]
            try:
                proc2 = subprocess.run(
                    ssh_win,
                    input=envelope,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
                if proc2.returncode == 0 and proc2.stdout.strip():
                    proc = proc2
                    machine.os = "windows"
            except (subprocess.TimeoutExpired, OSError):
                pass

        if proc.returncode != 0:
            stderr = proc.stderr.strip() or proc.stdout.strip()
            return {
                "ok": False,
                "error": f"SSH execution failed on '{machine.id}' ({ep.transport}:{ep.host}): {stderr}",
                "endpoint": ep.to_dict(),
            }

        stdout = proc.stdout.strip()
        last_line = stdout.splitlines()[-1] if stdout else ""
        try:
            parsed = json.loads(last_line)
            parsed["endpoint"] = ep.to_dict()
            return parsed
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": f"Unexpected non-JSON response from '{machine.id}': {stdout[:400]}",
                "endpoint": ep.to_dict(),
            }

    def run_command(
        self,
        machine: MachineConfig,
        command: str,
        cwd: str = "~",
        timeout_sec: int = 120,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute an arbitrary shell command on `machine` inside `cwd`."""
        ep = self.resolve_endpoint(machine)
        start = time.monotonic()

        if ep.transport == "local":
            expanded_cwd = os.path.expanduser(os.path.expandvars(cwd or "~"))
            if not os.path.isdir(expanded_cwd):
                return {
                    "ok": False,
                    "error": f"Working directory does not exist on local machine: {expanded_cwd}",
                    "endpoint": ep.to_dict(),
                }
            merged_env = os.environ.copy()
            if env:
                merged_env.update({str(k): str(v) for k, v in env.items()})
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=expanded_cwd,
                    env=merged_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                )
                elapsed_ms = (time.monotonic() - start) * 1000.0
                return {
                    "ok": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "cwd": expanded_cwd,
                    "duration_ms": round(elapsed_ms, 1),
                    "endpoint": ep.to_dict(),
                }
            except subprocess.TimeoutExpired as exc:
                return {
                    "ok": False,
                    "exit_code": -1,
                    "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                    "stderr": f"Command timed out after {timeout_sec}s",
                    "cwd": expanded_cwd,
                    "endpoint": ep.to_dict(),
                }

        if not ep.reachable:
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Machine '{machine.id}' is unreachable: {ep.error}",
                "endpoint": ep.to_dict(),
            }

        if machine.os == "windows":
            env_prefix = ""
            if env:
                assignments = [
                    f"$env:{k}='{str(v).replace(chr(39), chr(39)*2)}'"
                    for k, v in env.items()
                ]
                env_prefix = "; ".join(assignments) + "; "
            target_cwd = cwd.replace("~", "$HOME") if cwd.startswith("~") else cwd
            ps_script = f"Set-Location -Path \"{target_cwd}\"; {env_prefix}{command}"
            remote_cmd = f"powershell -NoProfile -NonInteractive -Command {shlex.quote(ps_script)}"
        else:
            env_prefix = ""
            if env:
                exports = [
                    f"export {k}={shlex.quote(str(v))}"
                    for k, v in env.items()
                    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k))
                ]
                if exports:
                    env_prefix = "; ".join(exports) + "; "
            if cwd in ("", "~"):
                cd_expr = "cd ~"
            elif cwd.startswith("~/"):
                cd_expr = f"cd ~/{shlex.quote(cwd[2:])}"
            else:
                cd_expr = f"cd {shlex.quote(cwd)}"
            # Load common user PATH locations (/opt/homebrew/bin, ~/.local/bin, ~/.cargo/bin) for non-login SSH shells
            path_init = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; '
            remote_cmd = f"{path_init}{cd_expr} && {{ {env_prefix}{command}; }}"

        ssh_cmd = [*self.build_ssh_args(machine, ep.host), remote_cmd]
        try:
            proc = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "cwd": cwd,
                "duration_ms": round(elapsed_ms, 1),
                "endpoint": ep.to_dict(),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                "stderr": f"Remote command timed out after {timeout_sec}s on '{machine.id}'",
                "cwd": cwd,
                "endpoint": ep.to_dict(),
            }
