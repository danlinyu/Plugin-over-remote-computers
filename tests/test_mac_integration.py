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
            self.assertIn("--from-launchd", data["ProgramArguments"])
            self.assertTrue(data["AbandonProcessGroup"])
            self.assertEqual(data["StartInterval"], 30)
            self.assertIn(".local/bin", data["EnvironmentVariables"]["PATH"])

    def test_empty_unmounted_folder_cleanup(self) -> None:
        from remote_fleet.mac_integration import _cleanup_unmounted_empty_dir

        with tempfile.TemporaryDirectory() as tmp:
            # 1. Folder containing only Finder `.DS_Store` should be cleaned up when unmounted
            dead_dir = Path(tmp) / "spark01"
            dead_dir.mkdir()
            (dead_dir / ".DS_Store").write_bytes(b"finder")
            _cleanup_unmounted_empty_dir(dead_dir)
            self.assertFalse(dead_dir.exists())

            # 2. Folder containing real files must never be removed
            real_dir = Path(tmp) / "spark03"
            real_dir.mkdir()
            (real_dir / "keep.txt").write_text("hi", encoding="utf-8")
            _cleanup_unmounted_empty_dir(real_dir)
            self.assertTrue(real_dir.is_dir())

    def test_subnet_scan_survives_stalled_ssh_device_like_192_168_68_100(self) -> None:
        """Regression test for `192.168.68.100:22` stalling SSH past 4s (`subprocess.TimeoutExpired`)."""
        import subprocess as sp
        from unittest.mock import patch
        from remote_fleet import bootstrap

        def fake_create_connection(addr: tuple[str, int], timeout: float = 0.25) -> object:
            ip, port = addr
            if ip == "192.168.68.100" and port == 22:
                class _DummySock:
                    def __enter__(self) -> "_DummySock":
                        return self

                    def __exit__(self, *args: object) -> None:
                        return None

                return _DummySock()
            raise OSError("closed")

        with (
            patch("remote_fleet.bootstrap.socket.create_connection", side_effect=fake_create_connection),
            patch("remote_fleet.bootstrap.socket.gethostbyaddr", side_effect=OSError("no rdns")),
            patch("remote_fleet.bootstrap._query_netbios_name", return_value=""),
            patch(
                "remote_fleet.bootstrap.subprocess.run",
                side_effect=sp.TimeoutExpired(cmd=["ssh"], timeout=4),
            ),
        ):
            discovered, win_no_ssh = bootstrap._scan_subnet_for_missing_hosts(
                "192.168.68", "192.168.68.50", set(), "danlinyu"
            )
            self.assertEqual(discovered, {})
            self.assertIsNone(win_no_ssh)

    def test_probe_home_lan_skips_subnet_scan_when_sparks_and_pc_already_found(self) -> None:
        from unittest.mock import patch
        from remote_fleet import bootstrap

        def fake_tcp_open(host: str, port: int = 22, timeout: float = 0.8) -> str | None:
            mapping = {
                "spark01.local": "192.168.68.67",
                "spark03.local": "192.168.68.89",
                "spark04.local": "192.168.68.91",
            }
            return mapping.get(host)

        with (
            patch("remote_fleet.bootstrap._tcp_open", side_effect=fake_tcp_open),
            patch("remote_fleet.bootstrap.scan_arp_neighbors", return_value=[]),
            patch("remote_fleet.bootstrap._host_resolves_or_pings", return_value="192.168.68.70"),
            patch("remote_fleet.bootstrap._scan_subnet_for_missing_hosts") as mock_scan,
        ):
            found, pc_no_ssh = bootstrap.probe_home_lan_candidates("danlinyu", tailscale_running=False)
            self.assertEqual(set(found.keys()), {"spark01", "spark03", "spark04"})
            self.assertEqual(pc_no_ssh, ("192.168.68.70", "192.168.68.70"))
            mock_scan.assert_not_called()

    def test_all_timed_subprocess_calls_are_protected_and_py39_compatible(self) -> None:
        """AST guard: every `subprocess.run(..., timeout=...)` in `src/` (including embedded `REMOTE_WORKER_SOURCE`)
        MUST be inside a `try` block, and everything must parse under Python 3.9."""
        import ast
        from remote_fleet.transport import REMOTE_WORKER_SOURCE

        src_root = Path(__file__).resolve().parents[1] / "src" / "remote_fleet"
        py_files = sorted(src_root.glob("*.py"))
        self.assertGreaterEqual(len(py_files), 8)

        sources: list[tuple[str, str]] = [(p.name, p.read_text(encoding="utf-8")) for p in py_files]
        sources.append(("REMOTE_WORKER_SOURCE", REMOTE_WORKER_SOURCE))

        unprotected: list[str] = []
        for label, source in sources:
            tree = ast.parse(source, filename=label, feature_version=(3, 9))

            parent_map: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parent_map[child] = node

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    is_subproc_run = (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "run"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                    )
                    has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
                    if is_subproc_run and has_timeout:
                        cur: ast.AST | None = node
                        inside_try = False
                        while cur in parent_map:
                            parent = parent_map[cur]
                            if isinstance(parent, ast.Try) and any(
                                cur is stmt or cur in ast.walk(stmt) for stmt in parent.body
                            ):
                                inside_try = True
                                break
                            cur = parent
                        if not inside_try:
                            unprotected.append(f"{label}:{node.lineno}")

        self.assertEqual(unprotected, [], f"Unprotected timed subprocess.run calls: {unprotected}")

    def test_windows_powershell_encoded_cmd_and_sftp_preflight(self) -> None:
        import base64
        import subprocess as sp
        from unittest.mock import patch
        from remote_fleet.config import MachineConfig
        from remote_fleet.mac_integration import _select_verified_rclone_sftp_flags
        from remote_fleet.transport import FleetTransport

        win_cmd = FleetTransport._build_windows_worker_cmd()
        self.assertTrue(win_cmd.startswith("powershell -NoProfile -NonInteractive -EncodedCommand "))
        b64_part = win_cmd.split()[-1]
        # Must contain zero shell metacharacters so cmd.exe and PowerShell both pass it untouched
        self.assertRegex(b64_part, r"^[A-Za-z0-9+/=]+$")
        decoded = base64.b64decode(b64_part).decode("utf-16-le")
        self.assertIn("WindowsApps", decoded)
        self.assertIn("miniconda3", decoded)

        pc = MachineConfig(
            id="pc",
            name="Home PC (danlin-hud)",
            lan_host="192.168.68.70",
            tailscale_host="100.93.148.108",
            user="danli",
            os="windows",
        )
        calls: list[list[str]] = []

        def fake_rclone_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
            calls.append(cmd)
            # Simulate `/C:/Users/danli` failing on Windows SFTP and `C:/Users/danli` (or `:sftp:`) succeeding
            sftp_arg = cmd[2]
            if sftp_arg == ":sftp:":
                return sp.CompletedProcess(cmd, 0, "Desktop/\nDocuments/\n", "")
            return sp.CompletedProcess(cmd, 1, "", "directory not found")

        with patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_rclone_run):
            chosen_path, chosen_flags, err = _select_verified_rclone_sftp_flags(
                "/usr/local/bin/rclone", pc, "192.168.68.70", "/C:/Users/danli"
            )
        self.assertEqual(err, "")
        self.assertEqual(chosen_path, "")
        assert chosen_flags is not None
        self.assertIn("--sftp-skip-links", chosen_flags)
        self.assertIn("--sftp-shell-type", chosen_flags)

        # Drive root preflight (`allow_empty_fallback=False`) must NEVER fall back to `:sftp:` (home dir)
        drive_calls: list[str] = []

        def fake_drive_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
            drive_calls.append(cmd[2])
            if cmd[2] == ":sftp:D:/":
                return sp.CompletedProcess(cmd, 0, "Datasets/\nModels/\n", "")
            return sp.CompletedProcess(cmd, 1, "", "bad path")

        with patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_drive_run):
            drv_path, drv_flags, drv_err = _select_verified_rclone_sftp_flags(
                "/usr/local/bin/rclone", pc, "192.168.68.70", "/D:/", allow_empty_fallback=False
            )
        self.assertEqual(drv_err, "")
        self.assertEqual(drv_path, "D:/")
        self.assertIsNotNone(drv_flags)
        self.assertNotIn(":sftp:", drive_calls)

    def test_windows_multi_drive_mounts_and_path_mapping(self) -> None:
        """Verify Windows `pc` mounts EVERY drive (`C`, `D`, `E`) + `Home -> C/Users/danli`
        and maps paths from any drive or `Home` back to the exact Windows path."""
        from unittest.mock import patch
        from remote_fleet.config import MachineConfig
        from remote_fleet.mac_integration import _mount_windows_all_drives

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            pc_container = tmp_path / "RemoteComputers" / "pc"

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                pc = MachineConfig(
                    id="pc",
                    name="Home PC (danlin-hud)",
                    lan_host="192.168.68.70",
                    tailscale_host="100.93.148.108",
                    user="danli",
                    os="windows",
                )
                ping = {
                    "ok": True,
                    "os": "windows",
                    "home": "C:\\Users\\danli",
                    "drives": ["C:/", "D:/", "E:/"],
                }

                # Simulate legacy direct mount on `pc_container` being cleanly unmounted first
                legacy_mounted = {"state": True}

                def fake_is_mounted(p: Path) -> bool:
                    if p.resolve() == pc_container.resolve():
                        return legacy_mounted["state"]
                    return False

                def fake_unmount(p: Path) -> dict[str, object]:
                    if p.resolve() == pc_container.resolve():
                        legacy_mounted["state"] = False
                    return {"ok": True, "unmounted": True}

                def fake_mount_sftp_dir(
                    machine: MachineConfig,
                    ep_dict: dict[str, object],
                    host: str,
                    remote_target: str,
                    mount_point: Path,
                    vol_label: str,
                    log_stem: str,
                    allow_empty_fallback: bool = True,
                ) -> dict[str, object]:
                    mount_point.mkdir(parents=True, exist_ok=True)
                    if remote_target == "C:/":
                        (mount_point / "Users" / "danli" / "CodeBuddy").mkdir(parents=True, exist_ok=True)
                        (mount_point / "Projects" / "win-app").mkdir(parents=True, exist_ok=True)
                    elif remote_target == "D:/":
                        (mount_point / "Datasets" / "climate").mkdir(parents=True, exist_ok=True)
                    elif remote_target == "E:/":
                        (mount_point / "Scratch").mkdir(parents=True, exist_ok=True)
                    from remote_fleet.config import load_mounts_registry, save_mounts_registry
                    reg = load_mounts_registry()
                    reg[str(mount_point.resolve())] = {"machine_id": machine.id, "remote_root": remote_target}
                    save_mounts_registry(reg)
                    return {"ok": True, "visible_count": 2, "already_mounted": False}

                with (
                    patch("remote_fleet.mac_integration.is_mounted", side_effect=fake_is_mounted),
                    patch("remote_fleet.mac_integration.unmount_sshfs", side_effect=fake_unmount),
                    patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_sftp_dir),
                ):
                    res = _mount_windows_all_drives(pc, {"transport": "lan", "host": "192.168.68.70"}, "192.168.68.70", ping, pc_container)

                self.assertTrue(res["ok"], res)
                self.assertEqual(res["drives"], ["C:/", "D:/", "E:/"])
                self.assertTrue((pc_container / "Home").is_symlink())

                # 1. Folder on D: drive -> D:/Datasets/climate
                s_d = load_active_project(cwd=pc_container / "D" / "Datasets" / "climate")
                assert s_d is not None
                self.assertEqual((s_d.machine_id, s_d.remote_path), ("pc", "D:/Datasets/climate"))

                # 2. Root of E: drive -> E:/
                s_e = load_active_project(cwd=pc_container / "E")
                assert s_e is not None
                self.assertEqual((s_e.machine_id, s_e.remote_path), ("pc", "E:/"))

                # 3. Folder on C: outside Users\danli -> C:/Projects/win-app
                s_c = load_active_project(cwd=pc_container / "C" / "Projects" / "win-app")
                assert s_c is not None
                self.assertEqual((s_c.machine_id, s_c.remote_path), ("pc", "C:/Projects/win-app"))

                # 4. Folder opened through `Home` shortcut -> C:/Users/danli/CodeBuddy
                s_h = load_active_project(cwd=pc_container / "Home" / "CodeBuddy")
                assert s_h is not None
                self.assertEqual((s_h.machine_id, s_h.remote_path), ("pc", "C:/Users/danli/CodeBuddy"))
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_posix_spark_apfs_container_mount_and_inode_persistence(self) -> None:
        """Verify `spark01` uses an unmounted local APFS container (`~/RemoteComputers/spark01/{Home,Projects}`)
        whose inode NEVER changes even when unreachable — preventing macOS NSOpenPanel 'original item can't be found'."""
        from unittest.mock import patch
        from remote_fleet.config import FleetConfig, MachineConfig
        from remote_fleet.mac_integration import _mount_posix_container, mount_single_machine_root
        from remote_fleet.transport import EndpointStatus, FleetTransport

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            base_dir = tmp_path / "RemoteComputers"
            spark_container = base_dir / "spark01"
            spark_container.mkdir(parents=True)
            original_inode = spark_container.stat().st_ino

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                spark = MachineConfig(
                    id="spark01",
                    name="DGX Spark 01",
                    lan_host="spark01.local",
                    tailscale_host="100.85.241.120",
                    user="danlinyu",
                    os="linux",
                )
                tr = FleetTransport(FleetConfig(machines=[spark]))

                # 1. Even if temporarily offline, `spark01` APFS directory & inode must NEVER be deleted!
                offline_ep = EndpointStatus(
                    machine_id="spark01", transport="none", host="", port=22, reachable=False, error="offline"
                )
                with patch.object(tr, "resolve_endpoint", return_value=offline_ep):
                    off_res = mount_single_machine_root(tr, spark, base_dir)
                self.assertFalse(off_res["ok"])
                self.assertTrue(spark_container.is_dir())
                self.assertEqual(spark_container.stat().st_ino, original_inode)

                # 2. When online, detaches any legacy direct mount, keeps same container inode, mounts `Home` + `Projects`
                legacy_mounted = {"state": True}

                def fake_is_mounted(p: Path) -> bool:
                    return legacy_mounted["state"] if p.resolve() == spark_container.resolve() else False

                def fake_unmount(p: Path) -> dict[str, object]:
                    if p.resolve() == spark_container.resolve():
                        legacy_mounted["state"] = False
                    return {"ok": True}

                def fake_mount_sftp_dir(
                    machine: MachineConfig,
                    ep_dict: dict[str, object],
                    host: str,
                    remote_target: str,
                    mount_point: Path,
                    vol_label: str,
                    log_stem: str,
                    allow_empty_fallback: bool = True,
                ) -> dict[str, object]:
                    mount_point.mkdir(parents=True, exist_ok=True)
                    if remote_target == "/home/danlinyu":
                        (mount_point / "projects" / "qwen38").mkdir(parents=True, exist_ok=True)
                    from remote_fleet.config import load_mounts_registry, save_mounts_registry
                    reg = load_mounts_registry()
                    reg[str(mount_point.resolve())] = {"machine_id": machine.id, "remote_root": remote_target}
                    save_mounts_registry(reg)
                    return {"ok": True, "engine": "rclone-nfsmount", "visible_count": 3, "already_mounted": False}

                with (
                    patch("remote_fleet.mac_integration.is_mounted", side_effect=fake_is_mounted),
                    patch("remote_fleet.mac_integration.unmount_sshfs", side_effect=fake_unmount),
                    patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_sftp_dir),
                ):
                    res = _mount_posix_container(
                        spark,
                        {"transport": "lan", "host": "spark01.local"},
                        "spark01.local",
                        {"ok": True, "os": "linux", "home": "/home/danlinyu", "extra_roots": ["/data"]},
                        spark_container,
                    )

                self.assertTrue(res["ok"], res)
                self.assertEqual(spark_container.stat().st_ino, original_inode)
                self.assertTrue((spark_container / "Projects").is_symlink())
                self.assertIn("data", res["sample_entries"])

                # Path resolution from `Home/...`, `Projects/...`, extra drive `data/...`, and container root itself
                s_home = load_active_project(cwd=spark_container / "Home" / "projects" / "qwen38")
                assert s_home is not None
                self.assertEqual((s_home.machine_id, s_home.remote_path), ("spark01", "/home/danlinyu/projects/qwen38"))

                s_proj = load_active_project(cwd=spark_container / "Projects" / "qwen38")
                assert s_proj is not None
                self.assertEqual((s_proj.machine_id, s_proj.remote_path), ("spark01", "/home/danlinyu/projects/qwen38"))

                s_root = load_active_project(cwd=spark_container)
                assert s_root is not None
                self.assertEqual((s_root.machine_id, s_root.remote_path), ("spark01", "/home/danlinyu"))
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_probe_mount_health_and_safe_teardown_order(self) -> None:
        """Verify `_probe_mount_health` catches hung/stale/legacy/roamed mounts via uncached `.fleet-probe-<uuid>`
        and `_teardown_mount_and_daemon` ALWAYS unmounts BEFORE killing `rclone`."""
        import errno
        import json
        import signal
        import time
        from unittest.mock import patch
        from remote_fleet.mac_integration import _probe_mount_health, _teardown_mount_and_daemon

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daemons = tmp_path / "daemons"
            daemons.mkdir()
            mp = tmp_path / "RemoteComputers" / "pc" / "C"
            mp.mkdir(parents=True)

            # 1. Unmounted -> "gone"
            with patch("remote_fleet.mac_integration.is_mounted", return_value=False):
                self.assertEqual(_probe_mount_health(mp, "pc-C", timeout_sec=0.3), "gone")

            # 2. Mounted on Darwin without daemon metadata -> "legacy_nfsmount"
            with (
                patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
            ):
                self.assertEqual(_probe_mount_health(mp, "pc-C", timeout_sec=0.3), "legacy_nfsmount")

            # Write valid daemon metadata
            meta_file = daemons / "pc-C.json"
            meta_file.write_text(
                json.dumps({"pid": 42424, "port": 51234, "host": "192.168.68.70", "mount_point": str(mp)}),
                encoding="utf-8",
            )

            # 3. Network roamed (LAN -> Tailscale) -> "endpoint_changed"
            with (
                patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch("remote_fleet.mac_integration.os.kill", return_value=None),
            ):
                self.assertEqual(
                    _probe_mount_health(mp, "pc-C", timeout_sec=0.3, expected_host="100.93.148.108"),
                    "endpoint_changed",
                )

            # 4. Live LOOKUP returning ENOENT -> "healthy"
            def lstat_enoent(p: str) -> object:
                raise OSError(errno.ENOENT, "No such file")

            with (
                patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch("remote_fleet.mac_integration.os.kill", return_value=None),
                patch("remote_fleet.mac_integration.os.lstat", side_effect=lstat_enoent),
            ):
                self.assertEqual(
                    _probe_mount_health(mp, "pc-C", timeout_sec=0.3, expected_host="192.168.68.70"),
                    "healthy",
                )

            # 5. Live LOOKUP returning ESTALE / ETIMEDOUT -> "unresponsive"
            def lstat_estale(p: str) -> object:
                raise OSError(errno.ESTALE, "Stale NFS file handle")

            with (
                patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch("remote_fleet.mac_integration.os.kill", return_value=None),
                patch("remote_fleet.mac_integration.os.lstat", side_effect=lstat_estale),
            ):
                self.assertEqual(_probe_mount_health(mp, "pc-C", timeout_sec=0.3), "unresponsive")

            # 6. Hung lstat past deadline -> returns "unresponsive" promptly without hanging caller
            def lstat_hang(p: str) -> object:
                time.sleep(2.0)
                raise OSError(errno.ENOENT, "late")

            t0 = time.monotonic()
            with (
                patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch("remote_fleet.mac_integration.os.kill", return_value=None),
                patch("remote_fleet.mac_integration.os.lstat", side_effect=lstat_hang),
            ):
                status = _probe_mount_health(mp, "pc-C", timeout_sec=0.15)
            self.assertEqual(status, "unresponsive")
            self.assertLess(time.monotonic() - t0, 1.0)

            # 7. Safe teardown MUST call `unmount_sshfs` BEFORE `os.kill(pid, SIGTERM)`
            order: list[str] = []
            with (
                patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch(
                    "remote_fleet.mac_integration.unmount_sshfs",
                    side_effect=lambda _p: order.append("unmount") or {"ok": True},
                ),
                patch(
                    "remote_fleet.mac_integration.os.kill",
                    side_effect=lambda pid, sig: order.append(f"kill:{pid}:{int(sig)}"),
                ),
            ):
                _teardown_mount_and_daemon(mp, "pc-C")

            self.assertEqual(order, ["unmount", f"kill:42424:{int(signal.SIGTERM)}"])
            self.assertFalse(meta_file.exists())

    def test_macos_mount_table_parser_and_legacy_hard_nfs_cleanup(self) -> None:
        """Verify orphaned hard `rclone nfsmount` mounts (including symlinked `/Volumes/X10 Pro/...`) are detected
        via `/sbin/mount` (`MNT_NOWAIT`) and force-unmounted without touching live soft NFS mounts."""
        import json
        from unittest.mock import patch
        from remote_fleet.mac_integration import _cleanup_legacy_nfsmounts
        from remote_fleet.mount import parse_macos_mount_output

        vol_root = "/Volumes/X10 Pro/MovedFromInternal/Projects/RemoteComputers"
        raw_mount = "\n".join(
            [
                "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)",
                f"localhost:/ on {vol_root}/pc/C (nfs, nodev, nosuid, mounted by danlinyu)",
                f"localhost:/ on {vol_root}/pc/D (nfs, nodev, nosuid, soft, intr, mounted by danlinyu)",
                f"localhost:/ on {vol_root}/spark01/Home (nfs, nodev, nosuid, soft, intr, mounted by danlinyu)",
            ]
        )
        parsed = parse_macos_mount_output(raw_mount)
        self.assertIn(f"{vol_root}/pc/C", parsed)
        self.assertNotIn("soft", parsed[f"{vol_root}/pc/C"])
        self.assertIn("soft", parsed[f"{vol_root}/spark01/Home"])

        with tempfile.TemporaryDirectory() as tmp:
            daemons = Path(tmp) / "daemons"
            daemons.mkdir()
            # spark01-Home has a live daemon PID; pc-D has NO live daemon (orphaned); pc-C is a hard mount
            (daemons / "spark01-Home.json").write_text(
                json.dumps({"pid": os.getpid(), "mount_point": f"{vol_root}/spark01/Home"}),
                encoding="utf-8",
            )
            unmounted_paths: list[str] = []
            with (
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration.safe_lexical_resolve", side_effect=lambda p: p),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch("remote_fleet.mac_integration.get_macos_mount_table", return_value=parsed),
                patch(
                    "remote_fleet.mac_integration.unmount_sshfs",
                    side_effect=lambda p: unmounted_paths.append(str(p)) or {"ok": True},
                ),
                patch("remote_fleet.mac_integration.shutil.which", return_value=None),
            ):
                _cleanup_legacy_nfsmounts(
                    Path(vol_root),
                    raw_root=Path("/Users/danlinyu/RemoteComputers"),
                )

            self.assertEqual(
                set(unmounted_paths),
                {
                    f"{vol_root}/pc/C",
                    f"{vol_root}/pc/D",
                },
            )

    def test_windows_10_drives_reuses_verified_sftp_flags(self) -> None:
        """Verify mounting 10 drives (`C:`..`L:`) on `danlin-hud` reuses verified SFTP flags after `C:`
        so Windows `sshd` `MaxStartups` is never hit by 10 redundant preflight handshakes."""
        from unittest.mock import patch
        from remote_fleet.config import MachineConfig
        from remote_fleet.mac_integration import _mount_windows_all_drives

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            pc_container = tmp_path / "RemoteComputers" / "pc"
            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                pc = MachineConfig(
                    id="pc",
                    name="Home PC (danlin-hud)",
                    lan_host="192.168.68.70",
                    tailscale_host="100.93.148.108",
                    user="danli",
                    os="windows",
                )
                ten_drives = [f"{ch}:/" for ch in "CDEFGHIJKL"]
                ping = {"ok": True, "os": "windows", "home": "C:\\Users\\danli", "drives": ten_drives}
                seen_preverified: list[tuple[str, tuple[str, list[str]] | None]] = []

                def fake_mount_dir(**kwargs: object) -> dict[str, object]:
                    mp = kwargs["mount_point"]
                    assert isinstance(mp, Path)
                    mp.mkdir(parents=True, exist_ok=True)
                    target = str(kwargs["remote_target"])
                    pre = kwargs.get("preverified_sftp")
                    seen_preverified.append((target, pre))  # type: ignore[arg-type]
                    return {
                        "ok": True,
                        "visible_count": 3,
                        "already_mounted": False,
                        "verified_sftp_flags": ["--sftp-host", "192.168.68.70", "--sftp-skip-links"],
                        "verified_rclone_path": f"/{target}",
                    }

                with (
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_dir),
                    patch("remote_fleet.mac_integration.time.sleep", return_value=None),
                ):
                    res = _mount_windows_all_drives(
                        pc, {"transport": "lan", "host": "192.168.68.70"}, "192.168.68.70", ping, pc_container
                    )

                self.assertTrue(res["ok"])
                self.assertEqual(len(res["drives"]), 10)
                # Drive C probes once (`preverified_sftp is None`), drives D..L reuse `preverified_sftp`!
                self.assertIsNone(seen_preverified[0][1])
                for drv_target, pre_val in seen_preverified[1:]:
                    self.assertIsNotNone(pre_val, f"Expected {drv_target} to reuse preverified SFTP flags")
                    assert pre_val is not None
                    self.assertEqual(pre_val[0], f"/{drv_target}")
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_windows_no_controlmaster_fast_ping_and_sftp_fallback(self) -> None:
        """Regression test for Windows PC `ssh ... ControlMaster=auto ... powershell` 10s hang:
        1) `build_ssh_args` disables `ControlMaster` for `os='windows'`.
        2) `_build_windows_fast_ping_cmd` uses zero-stdin `DriveInfo::GetDrives()` without `Test-Path` / `.IsReady`.
        3) `mount_single_machine_root` still mounts all PC drives via pure SFTP even if PowerShell SSH times out!"""
        import base64
        from unittest.mock import patch
        from remote_fleet.config import FleetConfig, MachineConfig, save_mounts_registry
        from remote_fleet.mac_integration import mount_single_machine_root
        from remote_fleet.transport import EndpointStatus, FleetTransport

        pc = MachineConfig(
            id="pc",
            name="Home PC (danlin-hud)",
            lan_host="192.168.68.70",
            tailscale_host="100.93.148.108",
            user="danli",
            os="windows",
        )
        tr = FleetTransport(FleetConfig(machines=[pc]))
        ssh_args = tr.build_ssh_args(pc, "192.168.68.70")
        self.assertNotIn("ControlMaster=auto", ssh_args)
        self.assertIn("ControlMaster=no", ssh_args)

        fast_cmd = FleetTransport._build_windows_fast_ping_cmd()
        decoded_fast = base64.b64decode(fast_cmd.split()[-1]).decode("utf-16-le")
        self.assertIn("DriveInfo", decoded_fast)
        self.assertNotIn("ReadToEnd", decoded_fast)
        self.assertNotIn("Test-Path", decoded_fast)
        self.assertNotIn("IsReady", decoded_fast)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            base_dir = tmp_path / "RemoteComputers"
            (base_dir / "pc" / "G").mkdir(parents=True)

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                save_mounts_registry(
                    {
                        str(base_dir / "pc"): {"machine_id": "pc", "remote_root": "C:/Users/danli", "container_only": "1"},
                        str(base_dir / "pc" / "C"): {"machine_id": "pc", "remote_root": "C:/"},
                        str(base_dir / "pc" / "D"): {"machine_id": "pc", "remote_root": "D:/"},
                    }
                )
                live_ep = EndpointStatus(
                    machine_id="pc", transport="lan", host="192.168.68.70", port=22, reachable=True, latency_ms=2.0
                )
                captured_drives: list[str] = []

                def fake_mount_win(
                    m: MachineConfig, ep_d: dict[str, object], h: str, ping_d: dict[str, object], cdir: Path
                ) -> dict[str, object]:
                    captured_drives.extend(ping_d["drives"])  # type: ignore[arg-type]
                    return {"ok": True, "machine_id": m.id, "drives": ping_d["drives"], "mount_point": str(cdir)}

                with (
                    patch.object(tr, "resolve_endpoint", return_value=live_ep),
                    patch.object(tr, "run_worker_op", return_value={"ok": False, "error": "timed out after 20 seconds"}),
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration.find_rclone_binary", return_value="/usr/local/bin/rclone"),
                    patch(
                        "remote_fleet.mac_integration._select_verified_rclone_sftp_flags",
                        return_value=("/C:/", ["--sftp-host", "192.168.68.70"], ""),
                    ),
                    patch("remote_fleet.mac_integration._mount_windows_all_drives", side_effect=fake_mount_win),
                ):
                    res = mount_single_machine_root(tr, pc, base_dir)

                self.assertTrue(res["ok"], res)
                self.assertEqual(captured_drives, ["C:/", "D:/", "G:/"])
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_external_ssd_boot_race_mirror_and_pc_dhcp_drift_protection(self) -> None:
        """Verify:
        1) `mount_all_machines` never calls `mkdir` on `/Volumes/X10 Pro/...` before `X10 Pro` is mounted at login.
        2) `install_macos_launch_agent` mirrors `remote_fleet` onto internal APFS (`~/.local/share/remote-fleet/src`).
        3) `resolve_endpoint` falls back to `danlin-hud` / `danlin-hud.local` on LAN if DHCP IP `192.168.68.70` changes."""
        from unittest.mock import patch
        from remote_fleet.config import FleetConfig, MachineConfig
        from remote_fleet.mac_integration import _external_volume_not_ready, mount_all_machines
        from remote_fleet.transport import FleetTransport

        # 1. External SSD boot guard
        ext_target = Path("/Volumes/X10 Pro/MovedFromInternal/Projects/RemoteComputers")
        with (
            patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
            patch("remote_fleet.mac_integration.is_mounted", return_value=False),
        ):
            self.assertEqual(_external_volume_not_ready(ext_target), Path("/Volumes/X10 Pro"))
            with patch(
                "remote_fleet.mac_integration._resolve_mount_root",
                return_value=(ext_target, Path("/Users/danlinyu/RemoteComputers")),
            ):
                summary = mount_all_machines(FleetConfig(machines=[]), FleetTransport(FleetConfig(machines=[])), from_launchd=True)
                self.assertEqual(summary.get("waiting_for_volume"), "/Volumes/X10 Pro")

        # 2. Internal APFS runtime mirror in LaunchAgent
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            plist_path = install_macos_launch_agent(home_dir=fake_home)
            mirror_pkg = fake_home / ".local" / "share" / "remote-fleet" / "src" / "remote_fleet" / "mac_integration.py"
            self.assertTrue(mirror_pkg.is_file())
            with open(plist_path, "rb") as fp:
                pdata = plistlib.load(fp)
            self.assertIn(str(fake_home / ".local" / "share" / "remote-fleet" / "src"), pdata["EnvironmentVariables"]["PYTHONPATH"])

        # 3. DHCP IP drift on `pc` (`192.168.68.70` down -> `danlin-hud.local` reachable on LAN)
        old_self = os.environ.get("REMOTE_FLEET_SELF")
        os.environ["REMOTE_FLEET_SELF"] = "mac-mini"
        try:
            pc = MachineConfig(
                id="pc",
                name="Home PC (danlin-hud)",
                lan_host="192.168.68.70",
                tailscale_host="100.93.148.108",
                user="danli",
                os="windows",
            )
            tr = FleetTransport(FleetConfig(machines=[pc], prefer_lan=True))

            def fake_probe(host: str, port: int, timeout_sec: float) -> tuple[bool, float | None, str]:
                if host == "danlin-hud.local":
                    return True, 1.8, ""
                return False, None, "connection refused"

            with patch.object(tr, "_tcp_probe", side_effect=fake_probe):
                ep = tr.resolve_endpoint(pc, force_refresh=True)
            self.assertTrue(ep.reachable)
            self.assertEqual(ep.transport, "lan")
            self.assertEqual(ep.host, "danlin-hud.local")
        finally:
            if old_self is None:
                os.environ.pop("REMOTE_FLEET_SELF", None)
            else:
                os.environ["REMOTE_FLEET_SELF"] = old_self


if __name__ == "__main__":
    unittest.main()
