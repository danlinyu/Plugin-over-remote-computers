"""Unit tests for fleet configuration, Tailscale peer discovery, self-detection, and workspace markers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from remote_fleet.config import (
    ActiveProjectState,
    default_home_fleet_config,
    find_workspace_marker,
    is_self_machine,
    merge_tailscale_into_config,
    parse_tailscale_status_json,
    save_fleet_config,
    FleetConfig,
)


class TestConfigAndDiscovery(unittest.TestCase):
    def test_default_five_machine_fleet(self) -> None:
        cfg = default_home_fleet_config()
        ids = [m.id for m in cfg.machines]
        self.assertEqual(ids, ["mac-mini", "pc", "spark-1", "spark-2", "spark-3"])
        self.assertIsNotNone(cfg.get_machine("spark-2"))
        self.assertIsNotNone(cfg.get_machine("DGX Spark 3"))
        self.assertIsNotNone(cfg.get_machine("mac-mini.local".removesuffix(".local")))

    def test_tailscale_status_parsing_and_merge(self) -> None:
        sample_ts = {
            "Self": {
                "HostName": "mac-mini",
                "DNSName": "mac-mini.tail1234.ts.net.",
                "TailscaleIPs": ["100.101.102.1", "fd7a:115c:a1e0::1"],
                "OS": "macOS",
                "Online": True,
            },
            "Peer": {
                "nodekey:1": {
                    "HostName": "spark-1",
                    "DNSName": "spark-1.tail1234.ts.net.",
                    "TailscaleIPs": ["100.101.102.11"],
                    "OS": "linux",
                    "Online": True,
                },
                "nodekey:2": {
                    "HostName": "pc",
                    "DNSName": "pc.tail1234.ts.net.",
                    "TailscaleIPs": ["100.101.102.20"],
                    "OS": "windows",
                    "Online": True,
                },
                "nodekey:3": {
                    "HostName": "iphone",
                    "DNSName": "iphone.tail1234.ts.net.",
                    "TailscaleIPs": ["100.101.102.99"],
                    "OS": "iOS",
                    "Online": True,
                },
                "nodekey:4": {
                    "HostName": "nas-box",
                    "DNSName": "nas-box.tail1234.ts.net.",
                    "TailscaleIPs": ["100.101.102.50"],
                    "OS": "linux",
                    "Online": True,
                },
            },
        }
        nodes = parse_tailscale_status_json(sample_ts)
        # iOS device should be filtered out; 4 computers kept
        self.assertEqual(len(nodes), 4)

        cfg = default_home_fleet_config()
        merged, self_node = merge_tailscale_into_config(cfg, nodes)
        self.assertIsNotNone(self_node)
        assert self_node is not None
        self.assertEqual(self_node.hostname, "mac-mini")

        spark1 = merged.get_machine("spark-1")
        assert spark1 is not None
        self.assertEqual(spark1.tailscale_host, "spark-1.tail1234.ts.net")

        pc = merged.get_machine("pc")
        assert pc is not None
        self.assertEqual(pc.os, "windows")

        # Unlisted Tailscale peer `nas-box` should also be discovered
        nas = merged.get_machine("nas-box")
        self.assertIsNotNone(nas)

        # Self detection via Tailscale self node
        mac = merged.get_machine("mac-mini")
        assert mac is not None
        self.assertTrue(is_self_machine(mac, tailscale_self=self_node))
        self.assertFalse(is_self_machine(spark1, tailscale_self=self_node))

    def test_workspace_marker_lookup_in_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "Fleet" / "spark-1" / "demo-project"
            sub = ws / "src" / "pkg"
            sub.mkdir(parents=True)
            state = ActiveProjectState(machine_id="spark-1", remote_path="/home/user/demo-project")
            (ws / ".remote-fleet.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")

            found = find_workspace_marker(sub)
            self.assertIsNotNone(found)
            assert found is not None
            _, resolved_state = found
            self.assertEqual(resolved_state.machine_id, "spark-1")
            self.assertEqual(resolved_state.remote_path, "/home/user/demo-project")
            self.assertEqual(resolved_state.local_workspace_path, str(ws.resolve()))


if __name__ == "__main__":
    unittest.main()
