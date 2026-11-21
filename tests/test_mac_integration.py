"""Tests for macOS SSH config generation (with & without Tailscale), ~/RemoteComputers mount path mapping, and LaunchAgent."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest

from remote_fleet.config import (
    default_home_fleet_config,
    is_tailscale_only_host,
    load_active_project,
    save_mounts_registry,
)
from remote_fleet.mac_integration import (
    install_macos_launch_agent,
    sync_ssh_config,
)


class TestMacIntegration(unittest.TestCase):
    def test_ssh_config_lan_first_and_wildcard_ordering_when_tailscale_available(self) -> None:
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
                cfg.get_machine("spark-1").user = "danlinyu"  # type: ignore[union-attr]

                written = sync_ssh_config(cfg, ssh_config_path=ssh_cfg, tailscale_available=True)
                text = written.read_text(encoding="utf-8")

                self.assertNotIn("Host mac-mini\n", text)
                self.assertIn("Host spark-1\n", text)
                self.assertIn("HostName spark-1.tailnet.ts.net", text)
                self.assertIn("Match host spark-1 exec", text)
                self.assertIn("HostName spark01.local", text)
                self.assertIn("User danlinyu", text)

                # Managed block inserted BEFORE `Host *`
                self.assertLess(text.index("Host spark-1"), text.index("Host *"))
                self.assertIn("Host github.com", text)

                if os.name != "nt":
                    mode = stat.S_IMODE(written.stat().st_mode)
                    self.assertEqual(mode, 0o600)

                # Re-running is idempotent
                sync_ssh_config(cfg, ssh_config_path=ssh_cfg, tailscale_available=True)
                text2 = written.read_text(encoding="utf-8")
                self.assertEqual(text, text2)
            finally:
                if old_self is None:
                    os.environ.pop("REMOTE_FLEET_SELF", None)
                else:
                    os.environ["REMOTE_FLEET_SELF"] = old_self

    def test_ssh_config_pure_lan_when_tailscale_not_installed(self) -> None:
        """On a brand-new Mac Mini without Tailscale, `~/.ssh/config` must NOT fall back to 100.x.y.z IPs."""
        self.assertTrue(is_tailscale_only_host("100.85.241.120"))
        self.assertTrue(is_tailscale_only_host("spark01.tail123.ts.net"))
        self.assertFalse(is_tailscale_only_host("spark01.local"))
        self.assertFalse(is_tailscale_only_host("192.168.68.67"))

        with tempfile.TemporaryDirectory() as tmp:
            ssh_cfg = Path(tmp) / ".ssh" / "config"
            old_self = os.environ.get("REMOTE_FLEET_SELF")
            os.environ["REMOTE_FLEET_SELF"] = "mac-mini"
            try:
                cfg = default_home_fleet_config()
                cfg.get_machine("spark-1").lan_host = "spark01.local"  # type: ignore[union-attr]
                cfg.get_machine("spark-1").tailscale_host = "100.85.241.120"  # type: ignore[union-attr]

                written = sync_ssh_config(cfg, ssh_config_path=ssh_cfg, tailscale_available=False)
                text = written.read_text(encoding="utf-8")

                # Direct Home LAN HostName, no unroutable 100.85.241.120!
                self.assertIn("Host spark-1\n    HostName spark01.local", text)
                self.assertNotIn("100.85.241.120", text)
                self.assertNotIn("Match host spark-1 exec", text)
            finally:
                if old_self is None:
                    os.environ.pop("REMOTE_FLEET_SELF", None)
                else:
                    os.environ["REMOTE_FLEET_SELF"] = old_self

    def test_mounted_folder_auto_binds_active_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            mount_root = tmp_path / "RemoteComputers" / "spark03"
            sub_proj = mount_root / "projects" / "my-new-agent"
            sub_proj.mkdir(parents=True)

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                save_mounts_registry(
                    {
                        str(mount_root): {
                            "machine_id": "spark03",
                            "remote_root": "/home/danlinyu",
                        }
                    }
                )
                active = load_active_project(cwd=sub_proj)
                self.assertIsNotNone(active)
                assert active is not None
                self.assertEqual(active.machine_id, "spark03")
                self.assertEqual(active.remote_path, "/home/danlinyu/projects/my-new-agent")
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
            self.assertIn(".local/bin", data["EnvironmentVariables"]["PATH"])


if __name__ == "__main__":
    unittest.main()
