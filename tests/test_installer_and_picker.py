"""Tests for multi-harness installer and interactive folder picker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from remote_fleet.config import FleetConfig, MachineConfig
from remote_fleet.installer import install_all_harnesses
from remote_fleet.picker import interactive_browse_folder, interactive_pick_machine
from remote_fleet.transport import FleetTransport


class TestInstallerAndPicker(unittest.TestCase):
    def test_install_all_harnesses_into_temp_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            # Pre-populate existing Codex config to verify non-destructive TOML update
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".codex" / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

            report = install_all_harnesses(home_dir=fake_home)
            self.assertIn("antigravity", report)
            self.assertIn("claude", report)
            self.assertIn("codex", report)
            self.assertIn("kimi", report)
            self.assertIn("dsh", report)

            # Verify Codex config preserved existing key AND added remote_fleet MCP
            codex_text = (fake_home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-5"', codex_text)
            self.assertIn("[mcp_servers.remote_fleet]", codex_text)

            # Re-run installer to confirm idempotency
            install_all_harnesses(home_dir=fake_home)
            codex_text_2 = (fake_home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertEqual(codex_text_2.count("[mcp_servers.remote_fleet]"), 1)

            # Verify Claude MCP JSON & slash commands
            claude_cfg = json.loads((fake_home / ".claude.json").read_text(encoding="utf-8"))
            self.assertIn("remote-fleet", claude_cfg["mcpServers"])
            self.assertTrue((fake_home / ".claude" / "commands" / "pick-remote.md").is_file())

            # Verify Antigravity copied skill script actually runs via `.repo_src` even outside repo!
            copied_fleet_script = (
                fake_home / ".gemini" / "antigravity" / "skills" / "remote-fleet" / "scripts" / "fleet"
            )
            proc = subprocess.run(
                [str(copied_fleet_script), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("remote-fleet", proc.stdout)

    def test_interactive_picker_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            (root / "existing-repo" / ".git").mkdir(parents=True)

            old_self = os.environ.get("REMOTE_FLEET_SELF")
            os.environ["REMOTE_FLEET_SELF"] = "mac-mini"
            try:
                m = MachineConfig(
                    id="mac-mini",
                    name="Mac Mini",
                    lan_host="localhost",
                    default_roots=[str(root)],
                )
                cfg = FleetConfig(machines=[m])
                tr = FleetTransport(cfg)

                # Pick machine #1
                inputs = iter(["1"])
                chosen = interactive_pick_machine(cfg, tr, input_fn=lambda _: next(inputs), print_fn=lambda _: None)
                self.assertEqual(chosen, m)

                # Enter folder #1 (`existing-repo`) then select `.`
                browse_inputs = iter(["1", "."])
                picked = interactive_browse_folder(
                    m,
                    tr,
                    input_fn=lambda _: next(browse_inputs),
                    print_fn=lambda _: None,
                )
                self.assertIsNotNone(picked)
                assert picked is not None
                selected_path, should_create = picked
                self.assertEqual(Path(selected_path), (root / "existing-repo").resolve())
                self.assertFalse(should_create)

                # Create a new folder via 'n'
                create_inputs = iter(["n", "brand-new-project"])
                picked_new = interactive_browse_folder(
                    m,
                    tr,
                    input_fn=lambda _: next(create_inputs),
                    print_fn=lambda _: None,
                )
                assert picked_new is not None
                self.assertTrue(picked_new[1])
                self.assertTrue(picked_new[0].endswith("brand-new-project"))
            finally:
                if old_self is None:
                    os.environ.pop("REMOTE_FLEET_SELF", None)
                else:
                    os.environ["REMOTE_FLEET_SELF"] = old_self


if __name__ == "__main__":
    unittest.main()
