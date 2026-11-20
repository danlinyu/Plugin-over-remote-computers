"""Tests for macOS SSH config generation, ~/RemoteComputers mount path mapping, and LaunchAgent."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest

from remote_fleet.config import (
    default_home_fleet_config,
    load_active_project,
    save_mounts_registry,
)
from remote_fleet.mac_integration import (
    install_macos_launch_agent,
    sync_ssh_config,
)


class TestMacIntegration(unittest.TestCase):
    def test_ssh_config_lan_first_and_wildcard_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ssh_cfg = Path(tmp) / ".ssh" / "config"
            ssh_cfg.parent.mkdir(parents=True)
            ssh_cfg.write_text(
                "Host github.com\n    User git\n\nHost *\n    AddKeysToAgent yes\n",
                encoding="utf-8",
            )

            old_self = os.environ.get("REMOTE_FLEET_SELF")
            os.environ["REMOTE_FLEET_SELF"] = "mac-mini"
            try:
                cfg = default_home_fleet_config()
                cfg.get_machine("spark-1").tailscale_host = "spark-1.tailnet.ts.net"  # type: ignore[union-attr]
                cfg.get_machine("spark-1").user = "danlin"  # type: ignore[union-attr]

                written = sync_ssh_config(cfg, ssh_config_path=ssh_cfg)
                text = written.read_text(encoding="utf-8")

                # Self (mac-mini) excluded; spark-1, spark-2, spark-3, pc included
                self.assertNotIn("Host mac-mini\n", text)
                self.assertIn("Host spark-1\n", text)
                self.assertIn("HostName spark-1.tailnet.ts.net", text)
                self.assertIn("Match host spark-1 exec", text)
                self.assertIn("HostName spark01.local", text)
                self.assertIn("User danlin", text)
                self.assertIn("Host pc\n", text)

                # Managed block inserted BEFORE `Host *`
                self.assertLess(text.index("Host spark-1"), text.index("Host *"))
                self.assertIn("Host github.com", text)

                # Verify 0600 permissions on POSIX
                if os.name != "nt":
                    mode = stat.S_IMODE(written.stat().st_mode)
                    self.assertEqual(mode, 0o600)

                # Re-running is idempotent
                sync_ssh_config(cfg, ssh_config_path=ssh_cfg)
                text2 = written.read_text(encoding="utf-8")
                self.assertEqual(text, text2)
            finally:
                if old_self is None:
                    os.environ.pop("REMOTE_FLEET_SELF", None)
                else:
                    os.environ["REMOTE_FLEET_SELF"] = old_self

    def test_mounted_folder_auto_binds_active_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            mount_root = tmp_path / "RemoteComputers" / "spark-2"
            sub_proj = mount_root / "projects" / "my-new-agent"
            sub_proj.mkdir(parents=True)

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                save_mounts_registry(
                    {
                        str(mount_root): {
                            "machine_id": "spark-2",
                            "remote_root": "/home/danlin",
                        }
                    }
                )
                active = load_active_project(cwd=sub_proj)
                self.assertIsNotNone(active)
                assert active is not None
                self.assertEqual(active.machine_id, "spark-2")
                self.assertEqual(active.remote_path, "/home/danlin/projects/my-new-agent")
                self.assertEqual(active.source, "mounted_folder")
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_macos_launch_agent_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            plist_path = install_macos_launch_agent(home_dir=fake_home)
            self.assertTrue(plist_path.is_file())
            with open(plist_path, "rb") as fp:
                data = plistlib.load(fp)
            self.assertEqual(data["Label"], "com.remote-fleet.automount")
            self.assertIn("mount-all", data["ProgramArguments"])


if __name__ == "__main__":
    unittest.main()
