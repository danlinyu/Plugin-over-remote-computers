"""Integration tests for LAN->Tailscale endpoint fallback, SSH worker execution, file ops, and sync."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest

from remote_fleet.config import FleetConfig, MachineConfig
from remote_fleet.mount import prepare_local_workspace, sync_workspace
from remote_fleet.transport import FleetTransport


class TestTransportAndOps(unittest.TestCase):
    def test_lan_to_tailscale_tcp_fallback(self) -> None:
        # Bind an ephemeral port on 127.0.0.1 to act as a live Tailscale SSH endpoint
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        stop_evt = threading.Event()

        def _accept_loop() -> None:
            srv.settimeout(0.2)
            while not stop_evt.is_set():
                try:
                    conn, _ = srv.accept()
                    conn.close()
                except socket.timeout:
                    continue
                except OSError:
                    break

        t = threading.Thread(target=_accept_loop, daemon=True)
        t.start()
        try:
            # LAN points to unroutable/closed port, Tailscale points to 127.0.0.1:port
            # Note: avoid literal "127.0.0.1" on machine fields triggering self-detection by setting REMOTE_FLEET_SELF
            old_self = os.environ.get("REMOTE_FLEET_SELF")
            os.environ["REMOTE_FLEET_SELF"] = "mac-mini"
            try:
                m = MachineConfig(
                    id="spark-1",
                    name="DGX Spark 1",
                    lan_host="127.0.0.254",  # closed on port `port` (Wait: on Linux 127.0.0.254 routes to loopback too! Use unresolvable `.invalid` host for closed LAN)
                    tailscale_host="127.0.0.1",
                    port=port,
                )
                m.lan_host = "unreachable-lan-host.invalid"
                cfg = FleetConfig(machines=[m], prefer_lan=True, lan_probe_timeout_ms=200)
                tr = FleetTransport(cfg)
                ep = tr.resolve_endpoint(m, force_refresh=True)
                self.assertTrue(ep.reachable)
                self.assertEqual(ep.transport, "tailscale")
                self.assertEqual(ep.host, "127.0.0.1")
            finally:
                if old_self is None:
                    os.environ.pop("REMOTE_FLEET_SELF", None)
                else:
                    os.environ["REMOTE_FLEET_SELF"] = old_self
        finally:
            stop_evt.set()
            srv.close()

    def test_remote_ssh_worker_and_sync_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote_home = tmp_path / "remote_spark_home"
            (remote_home / "projects").mkdir(parents=True)

            # Create a mock `ssh` executable that executes the remote command locally with HOME=remote_home
            mock_ssh = tmp_path / "mock_ssh.py"
            mock_ssh.write_text(
                "#!/usr/bin/env python3\n"
                "import os, subprocess, sys\n"
                "# Last argument is the remote command string\n"
                "remote_cmd = sys.argv[-1]\n"
                "env = os.environ.copy()\n"
                f"env['HOME'] = {repr(str(remote_home))}\n"
                "proc = subprocess.run(['/bin/sh', '-c', remote_cmd], env=env)\n"
                "sys.exit(proc.returncode)\n",
                encoding="utf-8",
            )
            mock_ssh.chmod(mock_ssh.stat().st_mode | stat.S_IXUSR)

            old_env = {
                k: os.environ.get(k)
                for k in ("REMOTE_FLEET_SSH_BIN", "REMOTE_FLEET_SKIP_TCP_PROBE", "REMOTE_FLEET_SELF")
            }
            os.environ["REMOTE_FLEET_SSH_BIN"] = str(mock_ssh)
            os.environ["REMOTE_FLEET_SKIP_TCP_PROBE"] = "1"
            os.environ["REMOTE_FLEET_SELF"] = "mac-mini"

            try:
                spark = MachineConfig(
                    id="spark-1",
                    name="DGX Spark 1",
                    lan_host="spark-1.local",
                    tailscale_host="spark-1",
                    os="linux",
                    default_roots=["~/projects", "~"],
                )
                cfg = FleetConfig(
                    machines=[spark],
                    fleet_mount_dir=str(tmp_path / "LocalFleet"),
                )
                tr = FleetTransport(cfg)

                # 1. Create a Python project on remote spark-1
                proj = tr.run_worker_op(
                    spark,
                    {
                        "op": "inspect_project",
                        "path": "~/projects/vision-trainer",
                        "create_if_missing": True,
                        "init_git": True,
                        "template": "python",
                    },
                )
                self.assertTrue(proj["ok"], proj)
                self.assertTrue(proj["created"])
                self.assertTrue(proj["is_git_repo"])

                # 2. Browse ~/projects on spark-1 and confirm vision-trainer appears as [git]
                browsed = tr.run_worker_op(
                    spark,
                    {"op": "browse_folders", "path": "~/projects", "default_roots": spark.default_roots},
                )
                self.assertTrue(browsed["ok"])
                dir_names = [d["name"] for d in browsed["directories"]]
                self.assertIn("vision-trainer", dir_names)

                # 3. Write, edit, read, search, and run command on remote spark-1
                w = tr.run_worker_op(
                    spark,
                    {
                        "op": "write_file",
                        "base_dir": proj["path"],
                        "path": "src/vision_trainer/train.py",
                        "content": "EPOCHS = 10\nprint(f'epochs={EPOCHS}')\n",
                    },
                )
                self.assertTrue(w["ok"])

                ed = tr.run_worker_op(
                    spark,
                    {
                        "op": "edit_file",
                        "base_dir": proj["path"],
                        "path": "src/vision_trainer/train.py",
                        "old_text": "EPOCHS = 10",
                        "new_text": "EPOCHS = 50",
                    },
                )
                self.assertTrue(ed["ok"])
                self.assertIn("+EPOCHS = 50", ed["diff"])

                rd = tr.run_worker_op(
                    spark,
                    {"op": "read_file", "base_dir": proj["path"], "path": "src/vision_trainer/train.py"},
                )
                self.assertIn("EPOCHS = 50", rd["content"])

                srch = tr.run_worker_op(
                    spark,
                    {"op": "search_files", "base_dir": proj["path"], "query": "EPOCHS = 50"},
                )
                self.assertEqual(len(srch["matches"]), 1)

                cmd_res = tr.run_command(
                    spark,
                    command="python3 src/vision_trainer/train.py",
                    cwd=proj["path"],
                )
                self.assertTrue(cmd_res["ok"], cmd_res)
                self.assertEqual(cmd_res["stdout"].strip(), "epochs=50")

                # 4. Pull remote files to local workspace and push an edit back
                local_ws_dir = tmp_path / "LocalFleet" / "spark-1" / "vision-trainer"
                pulled = sync_workspace(tr, spark, proj["path"], local_ws_dir, direction="pull")
                self.assertTrue(pulled["ok"], pulled)
                local_train = local_ws_dir / "src" / "vision_trainer" / "train.py"
                self.assertTrue(local_train.is_file())
                self.assertTrue((local_ws_dir / ".remote-fleet.json").is_file())
                self.assertTrue((local_ws_dir / ".mcp.json").is_file())

                local_train.write_text("EPOCHS = 100\nprint(f'epochs={EPOCHS}')\n", encoding="utf-8")
                pushed = sync_workspace(tr, spark, proj["path"], local_ws_dir, direction="push")
                self.assertTrue(pushed["ok"], pushed)

                verify_remote = tr.run_command(
                    spark,
                    command="python3 src/vision_trainer/train.py",
                    cwd=proj["path"],
                )
                self.assertEqual(verify_remote["stdout"].strip(), "epochs=100")

                # 5. Zero-FUSE link workspace creation
                linked = prepare_local_workspace(cfg, tr, spark, proj["path"], mode="link")
                self.assertTrue(linked["ok"])
                self.assertTrue((Path(linked["local_path"]) / "AGENTS.md").is_file())
                self.assertTrue((Path(linked["local_path"]) / "CLAUDE.md").is_file())
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_windows_watchdog_readline_devnull_and_idempotent_failover(self) -> None:
        import base64
        import subprocess as sp
        from unittest.mock import patch

        from remote_fleet.transport import EndpointStatus

        def decode_ps(cmd: str) -> str:
            self.assertIn("-EncodedCommand ", cmd)
            b64 = cmd.split("-EncodedCommand ", 1)[1].strip()
            return base64.b64decode(b64).decode("utf-16-le")

        # 1. Fast ping & worker must include in-process watchdog and NEVER use `ReadToEnd()`
        ping_ps = decode_ps(FleetTransport._build_windows_fast_ping_cmd(timeout_sec=15))
        worker_ps = decode_ps(FleetTransport._build_windows_worker_cmd(timeout_sec=25))
        for script, expected_ms in ((ping_ps, "20000"), (worker_ps, "30000")):
            self.assertIn("[powershell]::Create()", script)
            self.assertIn("Get-CimInstance Win32_Process", script)
            self.assertIn("taskkill.exe /F /T /PID", script)
            self.assertIn("[Environment]::Exit(124)", script)
            self.assertIn(expected_ms, script)
            self.assertIn("$__rfWd.Stop(); $__rfWd.Dispose()", script)
            self.assertNotIn("ReadToEnd", script)
        self.assertIn("[Console]::In.ReadLine()", worker_ps)

        # 2. Windows `run_command` wraps in watchdog, passes `stdin=DEVNULL`, and checks Set-Location failure
        pc = MachineConfig(
            id="pc",
            name="Windows PC",
            lan_host="192.168.68.70",
            tailscale_host="100.93.148.108",
            user="danli",
            os="windows",
        )
        cfg = FleetConfig(machines=[pc])
        tr = FleetTransport(cfg)
        ep_lan = EndpointStatus(machine_id="pc", transport="lan", host="192.168.68.70", port=22, reachable=True, latency_ms=1.0)
        captured_kwargs: dict[str, object] = {}
        captured_cmd: list[str] = []

        def fake_subproc(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
            captured_cmd[:] = list(cmd)
            captured_kwargs.update(kwargs)
            return sp.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        with (
            patch.object(tr, "resolve_endpoint", return_value=ep_lan),
            patch("remote_fleet.transport.subprocess.run", side_effect=fake_subproc),
        ):
            r = tr.run_command(pc, command="dir", cwd="C:/Users/danli", timeout_sec=10)
        self.assertTrue(r["ok"])
        self.assertEqual(captured_kwargs.get("stdin"), sp.DEVNULL)
        run_ps = decode_ps(captured_cmd[-1])
        self.assertIn("[powershell]::Create()", run_ps)
        self.assertIn("15000", run_ps)
        self.assertIn("if (-not $?) {", run_ps)

        # 3. POSIX `run_command` wraps with `timeout -k 5` watchdog when available
        spark = MachineConfig(id="spark01", name="Spark 01", lan_host="spark01.local", user="danlinyu", os="linux")
        ep_spark = EndpointStatus(machine_id="spark01", transport="lan", host="spark01.local", port=22, reachable=True, latency_ms=1.0)
        with (
            patch.object(tr, "resolve_endpoint", return_value=ep_spark),
            patch("remote_fleet.transport.subprocess.run", side_effect=fake_subproc),
        ):
            r2 = tr.run_command(spark, command="echo hi", cwd="~", timeout_sec=20)
        self.assertTrue(r2["ok"])
        self.assertIn("timeout -k 5 25", captured_cmd[-1])
        self.assertEqual(captured_kwargs.get("stdin"), sp.DEVNULL)

        # 4. LAN -> Tailscale failover ONLY retries idempotent ops, NEVER mutating ops like `write_file`
        calls: list[tuple[str, str]] = []

        def fake_exec(m: MachineConfig, ep: EndpointStatus, req: dict, timeout_sec: int) -> dict:
            calls.append((str(req["op"]), ep.transport))
            if ep.transport == "lan":
                return {"ok": False, "error": "timeout"}
            return {"ok": True, "endpoint": ep.to_dict()}

        with (
            patch.object(tr, "resolve_endpoint", return_value=ep_lan),
            patch.object(tr, "_exec_worker_on_endpoint", side_effect=fake_exec),
            patch.object(tr, "_tcp_probe", return_value=(True, 5.0, "")),
            patch("remote_fleet.transport.is_local_tailscale_running", return_value=True),
        ):
            res_read = tr.run_worker_op(pc, {"op": "read_file", "path": "a.txt"})
            self.assertTrue(res_read["ok"])
            self.assertEqual(calls, [("read_file", "lan"), ("read_file", "tailscale")])

            calls.clear()
            res_write = tr.run_worker_op(pc, {"op": "write_file", "path": "a.txt", "content": "x"})
            self.assertFalse(res_write["ok"])
            self.assertEqual(calls, [("write_file", "lan")])

            calls.clear()
            res_create = tr.run_worker_op(pc, {"op": "inspect_project", "path": "~/p", "create_if_missing": True})
            self.assertFalse(res_create["ok"])
            self.assertEqual(calls, [("inspect_project", "lan")])

    def test_worker_windows_rg_parsing_empty_old_text_and_pid_alive(self) -> None:
        import subprocess as sp
        from unittest.mock import patch

        from remote_fleet.transport import REMOTE_WORKER_SOURCE, _run_local_worker

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sample = tmp_path / "demo.py"
            sample.write_text("hello = 1\n", encoding="utf-8")

            # 1. `edit_file` must reject empty `old_text` cleanly instead of inserting between every character
            ed_err = _run_local_worker(
                {"op": "edit_file", "base_dir": str(tmp_path), "path": "demo.py", "old_text": "", "new_text": "X"}
            )
            self.assertFalse(ed_err["ok"])
            self.assertIn("non-empty", ed_err["error"])
            self.assertEqual(sample.read_text(encoding="utf-8"), "hello = 1\n")

            # 2. `search_files` must parse Windows `C:\path\file.py:42:val = 1:2` rg output intact
            fake_rg_stdout = "C:\\Users\\danli\\repo\\app.py:42:cfg = {'a': 'b'}\n"
            with (
                patch("shutil.which", return_value="/usr/bin/rg"),
                patch("subprocess.run", return_value=sp.CompletedProcess(["rg"], 0, stdout=fake_rg_stdout, stderr="")),
            ):
                srch = _run_local_worker({"op": "search_files", "base_dir": str(tmp_path), "query": "cfg"})
            self.assertTrue(srch["ok"], srch)
            self.assertEqual(len(srch["matches"]), 1)
            self.assertEqual(srch["matches"][0]["file"], "C:\\Users\\danli\\repo\\app.py")
            self.assertEqual(srch["matches"][0]["line"], 42)
            self.assertEqual(srch["matches"][0]["text"], "cfg = {'a': 'b'}")

            # 3. Cross-platform `_pid_alive` via `job_status` detects live vs dead PIDs cleanly
            import json as _json

            jobs_root = tmp_path / ".cache" / "remote-fleet" / "jobs"
            (jobs_root / "live").mkdir(parents=True)
            (jobs_root / "dead").mkdir(parents=True)
            (jobs_root / "live" / "meta.json").write_text(
                _json.dumps({"job_id": "live", "pid": os.getpid(), "command": "sleep", "cwd": str(tmp_path), "started_at": "2026-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            (jobs_root / "dead" / "meta.json").write_text(
                _json.dumps({"job_id": "dead", "pid": -5, "command": "sleep", "cwd": str(tmp_path), "started_at": "2026-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(tmp_path)
            try:
                st_live = _run_local_worker({"op": "job_status", "job_id": "live"})
                st_dead = _run_local_worker({"op": "job_status", "job_id": "dead"})
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            self.assertTrue(st_live["ok"] and st_live["running"], st_live)
            self.assertTrue(st_dead["ok"] and not st_dead["running"], st_dead)

    def test_windows_wsl_worker_and_run_command_end_to_end(self) -> None:
        """Verify Windows WSL routing for `wsl://`, `\\\\wsl.localhost\\`, and registered `Y:/` drive aliases,
        including live execution of the zero-quote `/bin/sh` trampoline in real `/bin/sh`."""
        import base64
        import json as _json
        import re
        import subprocess as sp
        from unittest.mock import patch

        from remote_fleet.config import parse_wsl_path, save_mounts_registry
        from remote_fleet.transport import EndpointStatus

        # 1. Path parsing round-trips
        self.assertEqual(parse_wsl_path("wsl://Ubuntu/home/danli/job"), ("Ubuntu", "/home/danli/job"))
        self.assertEqual(parse_wsl_path("wsl://Ubuntu/"), ("Ubuntu", "/"))
        self.assertEqual(parse_wsl_path("wsl://Ubuntu"), ("Ubuntu", "~"))
        self.assertEqual(parse_wsl_path(r"\\wsl.localhost\Ubuntu\home\danli"), ("Ubuntu", "/home/danli"))
        self.assertEqual(parse_wsl_path(r"\\wsl$\Ubuntu-24.04"), ("Ubuntu-24.04", "/"))
        self.assertIsNone(parse_wsl_path("C:/Users/danli"))

        def decode_ps(cmd: str) -> str:
            self.assertIn("-EncodedCommand ", cmd)
            b64 = cmd.split("-EncodedCommand ", 1)[1].strip()
            return base64.b64decode(b64).decode("utf-16-le")

        def extract_trampoline(ps_script: str) -> str:
            m = re.search(r"-e /bin/sh -c '([^']+)'", ps_script)
            self.assertIsNotNone(m, ps_script)
            assert m is not None
            tramp = m.group(1)
            # Zero single quotes, double quotes, or backslashes so PowerShell 5.1 / cmd.exe never mangle it
            self.assertNotIn("'", tramp)
            self.assertNotIn('"', tramp)
            self.assertNotIn("\\", tramp)
            return tramp

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            wsl_fs = tmp_path / "fake_wsl_home"
            proj_dir = wsl_fs / "work" / "sim"
            proj_dir.mkdir(parents=True)

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                save_mounts_registry(
                    {
                        str(tmp_path / "RemoteComputers" / "pc" / "Y"): {
                            "machine_id": "pc",
                            "remote_root": "wsl://Ubuntu/",
                            "wsl_distro": "Ubuntu",
                            "wsl_root": "/",
                            "windows_drive": "Y:",
                        }
                    }
                )
                pc = MachineConfig(
                    id="pc",
                    name="Home PC",
                    lan_host="192.168.68.70",
                    user="danli",
                    os="windows",
                )
                tr = FleetTransport(FleetConfig(machines=[pc]))
                ep_lan = EndpointStatus(
                    machine_id="pc", transport="lan", host="192.168.68.70", port=22, reachable=True, latency_ms=1.5
                )

                captured_ps: list[str] = []
                real_run = sp.run

                def fake_wsl_ssh_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
                    ps_script = decode_ps(cmd[-1])
                    captured_ps.append(ps_script)
                    self.assertIn("C:\\Windows\\System32\\wsl.exe", ps_script)
                    self.assertIn("-d 'Ubuntu'", ps_script)
                    self.assertIn("[powershell]::Create()", ps_script)
                    trampoline = extract_trampoline(ps_script)
                    env = os.environ.copy()
                    env["HOME"] = str(wsl_fs)
                    # Actually execute the extracted `/bin/sh` trampoline locally!
                    stdin_data = kwargs.get("input")
                    if isinstance(stdin_data, str):
                        # Emulate PowerShell piping `$raw` with CRLF into `wsl.exe`
                        return real_run(
                            ["/bin/sh", "-c", trampoline],
                            input=stdin_data.rstrip("\n") + "\r\n",
                            capture_output=True,
                            text=True,
                            env=env,
                            timeout=15,
                            check=False,
                        )
                    return real_run(
                        ["/bin/sh", "-c", trampoline],
                        stdin=sp.DEVNULL,
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=15,
                        check=False,
                    )

                with (
                    patch.object(tr, "resolve_endpoint", return_value=ep_lan),
                    patch("remote_fleet.transport.subprocess.run", side_effect=fake_wsl_ssh_run),
                ):
                    # 2. Write + read a file via `wsl://Ubuntu/...`
                    w_res = tr.run_worker_op(
                        pc,
                        {
                            "op": "write_file",
                            "base_dir": f"wsl://Ubuntu{proj_dir}",
                            "path": "hello.txt",
                            "content": "from-wsl\n",
                        },
                    )
                    self.assertTrue(w_res["ok"], w_res)
                    self.assertEqual(w_res.get("wsl_distro"), "Ubuntu")
                    self.assertEqual(w_res["path"], f"wsl://Ubuntu{proj_dir}/hello.txt")

                    # 3. Read the same file via Windows `Y:/...` drive alias!
                    r_res = tr.run_worker_op(
                        pc,
                        {
                            "op": "read_file",
                            "base_dir": f"Y:{proj_dir}",
                            "path": "hello.txt",
                        },
                    )
                    self.assertTrue(r_res["ok"], r_res)
                    self.assertEqual(r_res["content"], "from-wsl\n")
                    self.assertEqual(r_res.get("wsl_distro"), "Ubuntu")

                    # 4. `run_command` inside `\\wsl.localhost\Ubuntu\...` executes real bash inside WSL!
                    unc_cwd = "\\\\wsl.localhost\\Ubuntu" + str(proj_dir).replace("/", "\\")
                    cmd_res = tr.run_command(
                        pc,
                        command="cat hello.txt && echo ran-in-wsl",
                        cwd=unc_cwd,
                        timeout_sec=15,
                    )
                    self.assertTrue(cmd_res["ok"], cmd_res)
                    self.assertEqual(cmd_res.get("wsl_distro"), "Ubuntu")
                    self.assertEqual(cmd_res["cwd"], f"wsl://Ubuntu{proj_dir}")
                    self.assertEqual(cmd_res["stdout"].splitlines(), ["from-wsl", "ran-in-wsl"])

                    # 5. Detached WSL job gets `wsl__Ubuntu__job-...` ID and `job_status` auto-routes back to Ubuntu!
                    j_start = tr.run_worker_op(
                        pc,
                        {
                            "op": "start_job",
                            "cwd": f"wsl://Ubuntu{proj_dir}",
                            "command": "echo wsl-job-done",
                        },
                    )
                    self.assertTrue(j_start["ok"], j_start)
                    jid = str(j_start["job_id"])
                    self.assertTrue(jid.startswith("wsl__Ubuntu__job-"), jid)

                    j_stat = tr.run_worker_op(pc, {"op": "job_status", "job_id": jid, "tail_lines": 20})
                    self.assertTrue(j_stat["ok"], j_stat)
                    self.assertEqual(j_stat.get("wsl_distro"), "Ubuntu")

                # 6. Regular Windows `browse_folders` appends registered `Y: (WSL Ubuntu)` to `roots`
                def fake_win_browse(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
                    payload = {
                        "ok": True,
                        "path": "C:\\Users\\danli",
                        "directories": [],
                        "roots": [{"label": "C:", "path": "C:/", "exists": True}],
                    }
                    return sp.CompletedProcess(cmd, 0, stdout=_json.dumps(payload) + "\n", stderr="")

                with (
                    patch.object(tr, "resolve_endpoint", return_value=ep_lan),
                    patch("remote_fleet.transport.subprocess.run", side_effect=fake_win_browse),
                ):
                    b_res = tr.run_worker_op(pc, {"op": "browse_folders", "path": "C:/Users/danli"})
                self.assertTrue(b_res["ok"], b_res)
                root_paths = {r["path"]: r["label"] for r in b_res["roots"]}
                self.assertIn("wsl://Ubuntu/", root_paths)
                self.assertEqual(root_paths["wsl://Ubuntu/"], "Y: (WSL Ubuntu)")
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir


if __name__ == "__main__":
    unittest.main()
