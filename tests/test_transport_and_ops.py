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


if __name__ == "__main__":
    unittest.main()
