"""Tests for detached remote background jobs, load metrics, dataset-safe sync, and Start-Here launchpad."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import time
import unittest

from remote_fleet.config import FleetConfig, MachineConfig
from remote_fleet.installer import install_all_harnesses
from remote_fleet.mcp_server import RemoteFleetMCPServer
from remote_fleet.mount import sync_workspace
from remote_fleet.transport import FleetTransport


class TestJobsDatasetSyncAndLaunchpad(unittest.TestCase):
    def test_launchpad_and_global_agent_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            # Pre-seed ~/.claude/CLAUDE.md to ensure existing user rules are preserved
            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".claude" / "CLAUDE.md").write_text("# My Custom Claude Rules\n", encoding="utf-8")

            report = install_all_harnesses(home_dir=fake_home)
            self.assertIn("launchpad", report)

            launchpad_dir = fake_home / "Fleet" / "Start-Here"
            self.assertTrue((launchpad_dir / "GEMINI.md").is_file())
            self.assertTrue((launchpad_dir / "CLAUDE.md").is_file())
            self.assertTrue((launchpad_dir / "AGENTS.md").is_file())
            self.assertTrue((launchpad_dir / ".mcp.json").is_file())

            if os.name != "nt":
                self.assertTrue((fake_home / "Fleet" / "Pick Remote Folder.command").is_file())

            claude_md = (fake_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("# My Custom Claude Rules", claude_md)
            self.assertIn("fleet_list_machines", claude_md)

            # Idempotent re-run
            install_all_harnesses(home_dir=fake_home)
            claude_md_after = (fake_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertEqual(claude_md_after.count("<!-- remote-fleet-begin -->"), 1)
            self.assertTrue((fake_home / ".gemini" / "GEMINI.md").is_file())
            self.assertTrue((fake_home / ".codex" / "AGENTS.md").is_file())
            self.assertTrue((fake_home / ".kimi" / "AGENTS.md").is_file())

    def test_detached_jobs_load_metrics_and_dataset_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote_home = tmp_path / "spark2_home"
            proj_dir = remote_home / "projects" / "heavy-sim"
            (proj_dir / "src").mkdir(parents=True)
            (proj_dir / "datasets").mkdir(parents=True)
            (proj_dir / "checkpoints").mkdir(parents=True)

            (proj_dir / "src" / "sim.py").write_text("print('sim ready')\n", encoding="utf-8")
            (proj_dir / "datasets" / "huge.parquet").write_text("fake-parquet-bytes", encoding="utf-8")
            (proj_dir / "checkpoints" / "epoch1.safetensors").write_text("fake-weights", encoding="utf-8")

            mock_ssh = tmp_path / "mock_ssh.py"
            mock_ssh.write_text(
                "#!/usr/bin/env python3\n"
                "import os, subprocess, sys\n"
                "remote_cmd = sys.argv[-1]\n"
                "env = os.environ.copy()\n"
                f"env['HOME'] = {repr(str(remote_home))}\n"
                "proc = subprocess.run(['/bin/sh', '-c', remote_cmd], env=env)\n"
                "sys.exit(proc.returncode)\n",
                encoding="utf-8",
            )
            mock_ssh.chmod(mock_ssh.stat().st_mode | stat.S_IXUSR)

            saved_env = {
                k: os.environ.get(k)
                for k in (
                    "REMOTE_FLEET_SSH_BIN",
                    "REMOTE_FLEET_SKIP_TCP_PROBE",
                    "REMOTE_FLEET_SELF",
                    "REMOTE_FLEET_CONFIG_DIR",
                )
            }
            os.environ["REMOTE_FLEET_SSH_BIN"] = str(mock_ssh)
            os.environ["REMOTE_FLEET_SKIP_TCP_PROBE"] = "1"
            os.environ["REMOTE_FLEET_SELF"] = "mac-mini"
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(tmp_path / "cfg")

            try:
                spark2 = MachineConfig(
                    id="spark-2",
                    name="DGX Spark 2",
                    lan_host="spark-2.local",
                    os="linux",
                    default_roots=["~/projects"],
                )
                cfg = FleetConfig(machines=[spark2], fleet_mount_dir=str(tmp_path / "MacFleet"))
                tr = FleetTransport(cfg)

                # 1. Verify dataset-safe sync skips .parquet and .safetensors while syncing code
                mac_ws = tmp_path / "MacFleet" / "spark-2" / "heavy-sim"
                pulled = sync_workspace(tr, spark2, "~/projects/heavy-sim", mac_ws, direction="pull")
                self.assertTrue(pulled["ok"], pulled)
                self.assertTrue((mac_ws / "src" / "sim.py").is_file())
                self.assertFalse((mac_ws / "datasets" / "huge.parquet").exists())
                self.assertFalse((mac_ws / "checkpoints" / "epoch1.safetensors").exists())
                # Confirm original datasets remain untouched on remote spark-2
                self.assertTrue((proj_dir / "datasets" / "huge.parquet").is_file())

                # 2. Verify MCP server load metrics + detached background job start/status/stop
                mcp = RemoteFleetMCPServer(config=cfg)
                machines_res = mcp.call_tool("fleet_list_machines", {"probe": True, "include_load": True})
                self.assertFalse(machines_res["isError"])
                self.assertIn("cpu_count", machines_res["content"][0]["text"])

                mcp.call_tool(
                    "fleet_select_project",
                    {"machine": "spark-2", "path": "~/projects/heavy-sim"},
                )

                # Start quick background job
                start_res = mcp.call_tool(
                    "fleet_start_job",
                    {"command": "python3 -c \"import time; print('step-1'); time.sleep(0.2); print('done-calc')\""},
                )
                self.assertFalse(start_res["isError"], start_res)
                import json as _json
                job_info = _json.loads(start_res["content"][0]["text"])
                job_id = job_info["job_id"]

                # Wait briefly for completion and check logs + exit_code
                deadline = time.time() + 5.0
                final_status = None
                while time.time() < deadline:
                    st_res = mcp.call_tool("fleet_job_status", {"job_id": job_id})
                    st_data = _json.loads(st_res["content"][0]["text"])
                    if not st_data["running"] and st_data["exit_code"] is not None:
                        final_status = st_data
                        break
                    time.sleep(0.15)

                self.assertIsNotNone(final_status)
                assert final_status is not None
                self.assertEqual(final_status["exit_code"], 0)
                self.assertIn("done-calc", final_status["log_tail"])
            finally:
                for k, v in saved_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
