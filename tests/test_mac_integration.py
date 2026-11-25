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
        from remote_fleet.mac_integration import uninstall_macos_launch_agent

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
            # Never emit a periodic StartInterval timer that spams machines in the background!
            self.assertNotIn("StartInterval", data)
            self.assertIn(".local/bin", data["EnvironmentVariables"]["PATH"])
            self.assertTrue(uninstall_macos_launch_agent(home_dir=fake_home))
            self.assertFalse(plist_path.exists())
            self.assertFalse(uninstall_macos_launch_agent(home_dir=fake_home))

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
        from remote_fleet.mac_integration import (
            DAEMON_SCHEMA_VERSION,
            _probe_mount_health,
            _teardown_mount_and_daemon,
        )

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

            # 2b. Daemon metadata with missing or old schema (e.g. schema=2 pre-hardening) -> "outdated_daemon"
            meta_file = daemons / "pc-C.json"
            for old_meta in (
                {"pid": 42424, "port": 51234, "host": "192.168.68.70", "mount_point": str(mp)},
                {"schema": 2, "pid": 42424, "port": 51234, "host": "192.168.68.70", "mount_point": str(mp)},
            ):
                meta_file.write_text(json.dumps(old_meta), encoding="utf-8")
                with (
                    patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                    patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                    patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                ):
                    self.assertEqual(_probe_mount_health(mp, "pc-C", timeout_sec=0.3), "outdated_daemon")

            # Write valid current-schema daemon metadata on Tailscale first
            meta_file.write_text(
                json.dumps(
                    {
                        "schema": DAEMON_SCHEMA_VERSION,
                        "pid": 42424,
                        "port": 51234,
                        "host": "100.93.148.108",
                        "mount_point": str(mp),
                    }
                ),
                encoding="utf-8",
            )

            # 3a. Returned home (Tailscale -> Home LAN) -> "endpoint_changed" immediately so we upgrade back to LAN
            with (
                patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch("remote_fleet.mac_integration.os.kill", return_value=None),
            ):
                self.assertEqual(
                    _probe_mount_health(mp, "pc-C", timeout_sec=0.3, expected_host="192.168.68.70"),
                    "endpoint_changed",
                )

            # Write valid current-schema daemon metadata on Home LAN
            meta_file.write_text(
                json.dumps(
                    {
                        "schema": DAEMON_SCHEMA_VERSION,
                        "pid": 42424,
                        "port": 51234,
                        "host": "192.168.68.70",
                        "mount_point": str(mp),
                    }
                ),
                encoding="utf-8",
            )

            # 3b. LAN mount with live LOOKUP ENOENT stays "healthy" even if expected_host is Tailscale
            #     (prevents launchd Local Network privacy hiccups from tearing down fast live LAN mounts!)
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
                    _probe_mount_health(mp, "pc-C", timeout_sec=0.3, expected_host="100.93.148.108"),
                    "healthy",
                )
                # 4. Same-host live LOOKUP returning ENOENT -> "healthy"
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
        """Verify REAL macOS `/sbin/mount` output (`(nfs, nodev, nosuid, mounted by danlinyu)` — NEVER containing
        `"soft"`) preserves ALL mounts backed by live `rclone serve nfs` daemon PIDs while unmounting ONLY
        genuinely unowned/orphaned NFS mounts and sweeping leaked orphan `rclone serve nfs` processes."""
        import json
        import signal
        import subprocess as sp
        from unittest.mock import patch
        from remote_fleet.mac_integration import DAEMON_SCHEMA_VERSION, _cleanup_legacy_nfsmounts
        from remote_fleet.mount import parse_macos_mount_output

        vol_root = "/Volumes/X10 Pro/MovedFromInternal/Projects/RemoteComputers"
        # Real macOS `/sbin/mount` output NEVER includes NFS-layer options like `soft` or `intr`!
        raw_mount = "\n".join(
            [
                "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)",
                f"127.0.0.1:/ on {vol_root}/pc/C (nfs, nodev, nosuid, mounted by danlinyu)",
                f"localhost:/ on {vol_root}/pc/D (nfs, nodev, nosuid, mounted by danlinyu)",
                f"127.0.0.1:/ on {vol_root}/spark01/Home (nfs, nodev, nosuid, mounted by danlinyu)",
            ]
        )
        parsed = parse_macos_mount_output(raw_mount)
        self.assertIn(f"{vol_root}/pc/C", parsed)
        self.assertNotIn("soft", parsed[f"{vol_root}/spark01/Home"])

        with tempfile.TemporaryDirectory() as tmp:
            daemons = Path(tmp) / "daemons"
            daemons.mkdir()
            # spark01-Home and pc-C both have live daemon PIDs; pc-D has NO live daemon (orphaned/legacy)
            (daemons / "spark01-Home.json").write_text(
                json.dumps({"schema": DAEMON_SCHEMA_VERSION, "pid": os.getpid(), "mount_point": f"{vol_root}/spark01/Home"}),
                encoding="utf-8",
            )
            (daemons / "pc-C.json").write_text(
                json.dumps({"schema": DAEMON_SCHEMA_VERSION, "pid": os.getpid(), "mount_point": f"{vol_root}/pc/C"}),
                encoding="utf-8",
            )
            unmounted_paths: list[str] = []
            killed_pids: list[tuple[int, int]] = []
            parsed_after = {k: v for k, v in parsed.items() if k != f"{vol_root}/pc/D"}

            fake_ps_out = "\n".join(
                [
                    f"  {os.getpid()} rclone serve nfs :sftp:/home/danlinyu --nfs-cache-dir /Users/danlinyu/.cache/remote-fleet/nfs-handles/spark01-Home",
                    "  88888 rclone serve nfs :sftp:/D:/ --nfs-cache-dir /Users/danlinyu/.cache/remote-fleet/nfs-handles/pc-D",
                    "  99999 rclone serve nfs :sftp:/unrelated --nfs-cache-dir /tmp/other-cache",
                ]
            )

            def fake_subproc_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
                if cmd and cmd[0].endswith("ps"):
                    return sp.CompletedProcess(cmd, 0, stdout=fake_ps_out, stderr="")
                return sp.CompletedProcess(cmd, 1, stdout="", stderr="")

            def fake_kill(pid: int, sig: int) -> None:
                if sig == 0:
                    return
                killed_pids.append((pid, int(sig)))

            with (
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration.safe_lexical_resolve", side_effect=lambda p: p),
                patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                patch("remote_fleet.mac_integration.get_macos_mount_table", side_effect=[parsed, parsed_after]),
                patch(
                    "remote_fleet.mac_integration.unmount_sshfs",
                    side_effect=lambda p: unmounted_paths.append(str(p)) or {"ok": True},
                ),
                patch("remote_fleet.mac_integration.shutil.which", return_value=None),
                patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_subproc_run),
                patch("remote_fleet.mac_integration.os.kill", side_effect=fake_kill),
            ):
                _cleanup_legacy_nfsmounts(
                    Path(vol_root),
                    raw_root=Path("/Users/danlinyu/RemoteComputers"),
                )

            # MUST unmount ONLY orphaned pc/D — NEVER live spark01/Home or pc/C!
            self.assertEqual(unmounted_paths, [f"{vol_root}/pc/D"])
            # MUST kill ONLY leaked orphan PID 88888 — NEVER live daemon or unrelated rclone!
            self.assertEqual(killed_pids, [(88888, int(signal.SIGTERM))])

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
        """Regression test for Windows PC mounting with ZERO PowerShell sessions:
        1) `build_ssh_args` disables `ControlMaster` for `os='windows'`.
        2) `_parse_sftp_windows_batch_output` discovers home + all drives (`C`..`L`) from ONE pure `sftp -b -` session.
        3) `mount_single_machine_root` on Windows NEVER calls `run_worker_op` (ZERO PowerShell/cmd/conhost processes!).
        4) `_mount_windows_all_drives` recovers transiently dropped drives (`C`, `I`, `K`) on pass 2 + restores `Home`,
           and aborts immediately on `permission_denied` without retrying remaining drives!"""
        import base64
        import subprocess as sp
        from unittest.mock import patch
        from remote_fleet.config import FleetConfig, MachineConfig, save_mounts_registry
        from remote_fleet.mac_integration import (
            _mount_windows_all_drives,
            _parse_sftp_windows_batch_output,
            mount_single_machine_root,
        )
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

        # Pure-SFTP batch parser extracts home + exact drive letters C..L (ignoring repeated /L:/ for M..Z)
        sftp_sample = "\n".join(
            [
                "Remote working directory: /C:/Users/danli",
                *[f"Remote working directory: /{ch}:/" for ch in "CDEFGHIJKL"],
                *["Remote working directory: /L:/" for _ in "MNOPQRSTUVWXYZ"],
            ]
        )
        parsed_home, parsed_letters = _parse_sftp_windows_batch_output(sftp_sample, default_user="danli")
        self.assertEqual(parsed_home, "C:/Users/danli")
        self.assertEqual(parsed_letters, list("CDEFGHIJKL"))

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

                # 1. Pure-SFTP `sftp -b -` path mounts all C..L drives with ZERO calls to `run_worker_op` (ZERO PowerShell!)
                captured_sftp_drives: list[str] = []
                captured_sftp_home: list[str] = []

                def fake_mount_win_capture(
                    m: MachineConfig, ep_d: dict[str, object], h: str, ping_d: dict[str, object], cdir: Path
                ) -> dict[str, object]:
                    captured_sftp_drives.extend(ping_d["drives"])  # type: ignore[arg-type]
                    captured_sftp_home.append(str(ping_d["home"]))
                    return {"ok": True, "machine_id": m.id, "drives": ping_d["drives"], "mount_point": str(cdir)}

                with (
                    patch.object(tr, "resolve_endpoint", return_value=live_ep),
                    patch.object(tr, "run_worker_op") as mock_worker_op,
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration.shutil.which", return_value="/usr/bin/sftp"),
                    patch(
                        "remote_fleet.mac_integration.subprocess.run",
                        return_value=sp.CompletedProcess(["/usr/bin/sftp"], 0, stdout=sftp_sample, stderr=""),
                    ),
                    patch("remote_fleet.mac_integration._mount_windows_all_drives", side_effect=fake_mount_win_capture),
                ):
                    res_sftp = mount_single_machine_root(tr, pc, base_dir)

                self.assertTrue(res_sftp["ok"], res_sftp)
                self.assertEqual(captured_sftp_drives, [f"{ch}:/" for ch in "CDEFGHIJKL"])
                self.assertEqual(captured_sftp_home, ["C:/Users/danli"])
                mock_worker_op.assert_not_called()

                # 2. When `/usr/bin/sftp` is absent, rclone SFTP fallback still recovers C, D, G with ZERO PowerShell!
                captured_fallback: list[str] = []

                def fake_mount_win_fb(
                    m: MachineConfig, ep_d: dict[str, object], h: str, ping_d: dict[str, object], cdir: Path
                ) -> dict[str, object]:
                    captured_fallback.extend(ping_d["drives"])  # type: ignore[arg-type]
                    return {"ok": True, "machine_id": m.id, "drives": ping_d["drives"], "mount_point": str(cdir)}

                with (
                    patch.object(tr, "resolve_endpoint", return_value=live_ep),
                    patch.object(tr, "run_worker_op") as mock_worker_op2,
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration.shutil.which", return_value=None),
                    patch("remote_fleet.mac_integration.os.path.exists", return_value=False),
                    patch("remote_fleet.mac_integration.find_rclone_binary", return_value="/usr/local/bin/rclone"),
                    patch(
                        "remote_fleet.mac_integration._select_verified_rclone_sftp_flags",
                        return_value=("/C:/", ["--sftp-host", "192.168.68.70"], ""),
                    ),
                    patch("remote_fleet.mac_integration._mount_windows_all_drives", side_effect=fake_mount_win_fb),
                ):
                    res_fb = mount_single_machine_root(tr, pc, base_dir)

                self.assertTrue(res_fb["ok"], res_fb)
                self.assertEqual(captured_fallback, ["C:/", "D:/", "G:/"])
                mock_worker_op2.assert_not_called()

                # 3. Transient failure on C, I, K during pass 1 is recovered on pass 2 + restores `Home` symlink!
                attempts: dict[str, int] = {}

                def fake_mount_partial(**kwargs: object) -> dict[str, object]:
                    mp = kwargs["mount_point"]
                    assert isinstance(mp, Path)
                    target = str(kwargs["remote_target"])
                    letter = target[0]
                    attempts[letter] = attempts.get(letter, 0) + 1
                    if letter in ("C", "I", "K") and attempts[letter] == 1:
                        return {"ok": False, "machine_id": "pc", "error": "transient sshd MaxStartups drop"}
                    mp.mkdir(parents=True, exist_ok=True)
                    return {
                        "ok": True,
                        "visible_count": 2,
                        "already_mounted": False,
                        "verified_sftp_flags": ["--sftp-host", "192.168.68.70"],
                        "verified_rclone_path": f"/{target}",
                    }

                pc_dir = base_dir / "pc_retry"
                with (
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_partial),
                    patch("remote_fleet.mac_integration.time.sleep", return_value=None),
                ):
                    res_retry = _mount_windows_all_drives(
                        pc,
                        {"transport": "lan", "host": "192.168.68.70"},
                        "192.168.68.70",
                        {"ok": True, "os": "windows", "home": "C:/Users/danli", "drives": [f"{ch}:/" for ch in "CDEFGHIJKL"]},
                        pc_dir,
                    )

                self.assertTrue(res_retry["ok"])
                self.assertEqual(res_retry["drives"], [f"{ch}:/" for ch in "CDEFGHIJKL"])
                self.assertTrue(res_retry["home_created"])
                self.assertNotIn("failed_drives", res_retry)
                self.assertEqual((pc_dir / "Home").readlink(), Path("C/Users/danli"))
                self.assertEqual(attempts["C"], 2)
                self.assertEqual(attempts["I"], 2)
                self.assertEqual(attempts["K"], 2)
                self.assertEqual(attempts["D"], 1)

                # 4. EPERM (`permission_denied=True`) aborts immediately on C without attempting D..L!
                perm_calls: list[str] = []

                def fake_mount_eperm(**kwargs: object) -> dict[str, object]:
                    perm_calls.append(str(kwargs["remote_target"]))
                    return {"ok": False, "machine_id": "pc", "permission_denied": True, "error": "Operation not permitted"}

                with (
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_eperm),
                    patch("remote_fleet.mac_integration.time.sleep", return_value=None),
                ):
                    res_eperm = _mount_windows_all_drives(
                        pc,
                        {"transport": "lan", "host": "192.168.68.70"},
                        "192.168.68.70",
                        {"ok": True, "os": "windows", "home": "C:/Users/danli", "drives": [f"{ch}:/" for ch in "CDEFGHIJKL"]},
                        base_dir / "pc_eperm",
                    )

                self.assertFalse(res_eperm["ok"])
                self.assertTrue(res_eperm.get("permission_denied"))
                self.assertEqual(perm_calls, ["C:/"])
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

    def test_launchd_tick_preserves_all_live_sparks_and_pc_on_real_macos_mount_output(self) -> None:
        """End-to-end regression test for the catastrophic all-disconnect bug:
        1) Real macOS `/sbin/mount` prints `(nfs, nodev, nosuid, mounted by danlinyu)` — NEVER `"soft"`.
        2) `mounts.json` may contain stale pre-symlink keys (`/Users/.../RemoteComputers/...`) alongside resolved keys.
        3) Running `mount_all_machines(..., from_launchd=True)` MUST unmount ZERO mounts and spawn ZERO SSH calls!
        4) `install_macos_launch_agent` MUST skip `launchctl bootout`/`bootstrap` when plist is unchanged & already loaded."""
        import errno
        import json
        import subprocess as sp
        from unittest.mock import patch
        from remote_fleet.config import FleetConfig, MachineConfig, save_mounts_registry
        from remote_fleet.mac_integration import DAEMON_SCHEMA_VERSION, mount_all_machines
        from remote_fleet.mount import parse_macos_mount_output
        from remote_fleet.transport import EndpointStatus, FleetTransport

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            daemons = tmp_path / "daemons"
            daemons.mkdir()
            vol_root = tmp_path / "X10 Pro" / "Projects" / "RemoteComputers"
            (vol_root / "spark01" / "Home").mkdir(parents=True)
            (vol_root / "spark01" / "raid").mkdir(parents=True)
            (vol_root / "pc" / "C").mkdir(parents=True)
            (vol_root / "pc" / "D").mkdir(parents=True)
            raw_link = tmp_path / "RemoteComputers"
            raw_link.symlink_to(vol_root)

            resolved_root = vol_root.resolve()
            raw_mount = "\n".join(
                [
                    "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)",
                    f"127.0.0.1:/ on {resolved_root}/spark01/Home (nfs, nodev, nosuid, mounted by danlinyu)",
                    f"127.0.0.1:/ on {resolved_root}/spark01/raid (nfs, nodev, nosuid, mounted by danlinyu)",
                    f"127.0.0.1:/ on {resolved_root}/pc/C (nfs, nodev, nosuid, mounted by danlinyu)",
                    f"127.0.0.1:/ on {resolved_root}/pc/D (nfs, nodev, nosuid, mounted by danlinyu)",
                ]
            )
            mount_table = parse_macos_mount_output(raw_mount)

            for stem, mid, host, rel in (
                ("spark01-Home", "spark01", "spark01.local", "spark01/Home"),
                ("spark01-raid", "spark01", "spark01.local", "spark01/raid"),
                ("pc-C", "pc", "192.168.68.70", "pc/C"),
                ("pc-D", "pc", "192.168.68.70", "pc/D"),
            ):
                (daemons / f"{stem}.json").write_text(
                    json.dumps(
                        {
                            "schema": DAEMON_SCHEMA_VERSION,
                            "pid": os.getpid(),
                            "port": 51234,
                            "host": host,
                            "machine_id": mid,
                            "mount_point": str(resolved_root / rel),
                        }
                    ),
                    encoding="utf-8",
                )

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                # Deliberately include BOTH stale pre-symlink keys AND resolved keys in mounts.json
                save_mounts_registry(
                    {
                        f"{raw_link}/spark01/Home": {"machine_id": "spark01", "remote_root": "/home/danlinyu"},
                        f"{raw_link}/spark01/raid": {"machine_id": "spark01", "remote_root": "/raid"},
                        f"{resolved_root}/spark01/Home": {"machine_id": "spark01", "remote_root": "/home/danlinyu"},
                        f"{resolved_root}/spark01/raid": {"machine_id": "spark01", "remote_root": "/raid"},
                        f"{raw_link}/pc/C": {"machine_id": "pc", "remote_root": "C:/"},
                        f"{raw_link}/pc/D": {"machine_id": "pc", "remote_root": "D:/"},
                        f"{resolved_root}/pc/C": {"machine_id": "pc", "remote_root": "C:/"},
                        f"{resolved_root}/pc/D": {"machine_id": "pc", "remote_root": "D:/"},
                    }
                )

                spark01 = MachineConfig(id="spark01", name="Spark 01", lan_host="spark01.local", user="danlinyu", os="linux")
                pc = MachineConfig(id="pc", name="PC", lan_host="192.168.68.70", user="danli", os="windows")
                cfg = FleetConfig(machines=[spark01, pc])
                tr = FleetTransport(cfg)

                def fake_resolve(m: MachineConfig, force_refresh: bool = False) -> EndpointStatus:
                    return EndpointStatus(
                        machine_id=m.id, transport="lan", host=m.lan_host, port=22, reachable=True, latency_ms=1.5
                    )

                real_lstat = os.lstat

                def fake_lstat_enoent(p: object) -> object:
                    if ".fleet-probe-" in str(p):
                        raise OSError(errno.ENOENT, "No such file")
                    return real_lstat(p)  # type: ignore[arg-type]

                unmounted: list[str] = []
                ssh_calls: list[str] = []

                with (
                    patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                    patch("remote_fleet.mount.platform.system", return_value="Darwin"),
                    patch("remote_fleet.mac_integration._daemons_dir", return_value=daemons),
                    patch("remote_fleet.mac_integration.get_macos_mount_table", return_value=mount_table),
                    patch("remote_fleet.mount.get_macos_mount_table", return_value=mount_table),
                    patch("remote_fleet.mac_integration._kill_orphan_rclone_serve_daemons"),
                    patch(
                        "remote_fleet.mac_integration.unmount_sshfs",
                        side_effect=lambda p: unmounted.append(str(p)) or {"ok": True},
                    ),
                    patch("remote_fleet.mac_integration.os.lstat", side_effect=fake_lstat_enoent),
                    patch("remote_fleet.mac_integration.shutil.which", return_value=None),
                    patch.object(tr, "resolve_all_endpoints", return_value={}),
                    patch.object(tr, "resolve_endpoint", side_effect=fake_resolve),
                    patch.object(
                        tr,
                        "run_worker_op",
                        side_effect=lambda m, req, **kw: ssh_calls.append(m.id) or {"ok": False},
                    ),
                ):
                    summary = mount_all_machines(cfg, tr, base_dir=raw_link, from_launchd=True)

                self.assertEqual(unmounted, [], f"Live mounts were unexpectedly unmounted: {unmounted}")
                self.assertEqual(ssh_calls, [], f"Expected zero SSH calls when mounts are healthy: {ssh_calls}")
                by_id = {r["machine_id"]: r for r in summary["results"]}
                self.assertTrue(by_id["spark01"]["ok"] and by_id["spark01"].get("already_mounted"))
                self.assertTrue(by_id["pc"]["ok"] and by_id["pc"].get("already_mounted"))

                # Verify install_macos_launch_agent skips bootout/bootstrap when plist is unchanged & already loaded
                fake_home = tmp_path / "home"
                fake_home.mkdir()
                # First create the plist with home_dir=fake_home
                plist_path = install_macos_launch_agent(home_dir=fake_home, load_now=False)
                self.assertTrue(plist_path.is_file())

                launchctl_cmds: list[list[str]] = []

                def fake_launchctl(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
                    launchctl_cmds.append(list(cmd))
                    # Simulate `launchctl print` succeeding (agent already loaded)
                    return sp.CompletedProcess(cmd, 0, stdout="state = waiting", stderr="")

                with (
                    patch("remote_fleet.mac_integration.Path.home", return_value=fake_home),
                    patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                    patch("remote_fleet.mac_integration.shutil.which", return_value="/bin/launchctl"),
                    patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_launchctl),
                ):
                    install_macos_launch_agent(home_dir=None, load_now=True)

                subcmds = [c[1] for c in launchctl_cmds if len(c) >= 2 and c[0] == "launchctl"]
                self.assertEqual(subcmds, ["print"])
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_macos_login_item_app_bundle_tcc_stability_and_fallback(self) -> None:
        """Verify `Remote Fleet Mount.app` compiles/signs once, preserves CDHash across updates,
        and falls back to a strictly one-shot (`RunAtLoad` only, zero `StartInterval`) opener plist."""
        import json
        import plistlib
        import subprocess as sp
        from unittest.mock import patch

        from remote_fleet.mac_integration import (
            LOGIN_APP_BUNDLE_ID,
            LOGIN_APP_EXECUTABLE,
            LOGIN_APP_NAME,
            LOGIN_APP_VERSION,
            LOGIN_FALLBACK_AGENT_LABEL,
            install_macos_login_item_app,
            uninstall_macos_login_item_app,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            vol_target = Path(tmp) / "fake_x10" / "RemoteComputers"
            vol_target.mkdir(parents=True)
            mount_link = fake_home / "RemoteComputers"
            mount_link.symlink_to(vol_target)

            invoked: list[list[str]] = []

            def fake_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
                invoked.append(list(cmd))
                if cmd and cmd[0].endswith("swiftc"):
                    out_idx = cmd.index("-o") + 1
                    out_bin = Path(cmd[out_idx])
                    out_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    out_bin.chmod(0o755)
                    return sp.CompletedProcess(cmd, 0, stdout="", stderr="")
                if cmd and cmd[0].endswith("codesign"):
                    return sp.CompletedProcess(cmd, 0, stdout="", stderr="")
                if cmd and cmd[0].endswith("xattr"):
                    return sp.CompletedProcess(cmd, 0, stdout="", stderr="")
                if cmd and cmd[0] == "osascript":
                    # Simulate System Events Automation denied -> should install one-shot fallback plist
                    return sp.CompletedProcess(cmd, 1, stdout="", stderr="Not authorized")
                return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

            with (
                patch("remote_fleet.mac_integration.Path.home", return_value=fake_home),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch(
                    "remote_fleet.mac_integration.shutil.which",
                    side_effect=lambda name: f"/usr/bin/{name}" if name in ("swiftc", "codesign", "osascript", "xattr", "launchctl") else None,
                ),
                patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_run),
            ):
                res1 = install_macos_login_item_app(home_dir=None, base_dir=mount_link, register_login_item=True)

            self.assertTrue(res1["ok"], res1)
            self.assertTrue(res1["newly_created"])
            self.assertFalse(res1["login_item_registered"])
            self.assertTrue(res1["fallback_agent_installed"])

            app_path = fake_home / "Applications" / f"{LOGIN_APP_NAME}.app"
            info_plist = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
            self.assertEqual(info_plist["CFBundleIdentifier"], LOGIN_APP_BUNDLE_ID)
            self.assertEqual(info_plist["CFBundleExecutable"], LOGIN_APP_EXECUTABLE)
            self.assertTrue(info_plist["LSUIElement"])
            self.assertFalse(info_plist["LSBackgroundOnly"])
            self.assertIn("NSRemovableVolumesUsageDescription", info_plist)
            self.assertIn("NSLocalNetworkUsageDescription", info_plist)
            self.assertEqual(
                (app_path / "Contents" / "Resources" / "version.txt").read_text(encoding="utf-8").strip(),
                LOGIN_APP_VERSION,
            )

            # Verify fallback plist is strictly one-shot (`RunAtLoad` ONLY, NO `StartInterval`)
            fb_plist_path = fake_home / "Library" / "LaunchAgents" / f"{LOGIN_FALLBACK_AGENT_LABEL}.plist"
            self.assertTrue(fb_plist_path.is_file())
            fb_plist = plistlib.loads(fb_plist_path.read_bytes())
            self.assertTrue(fb_plist["RunAtLoad"])
            self.assertNotIn("StartInterval", fb_plist)
            self.assertNotIn("KeepAlive", fb_plist)
            self.assertEqual(fb_plist["ProgramArguments"], ["/usr/bin/open", "-g", str(app_path)])

            # Verify dynamic config stores both raw symlink and resolved directory
            cfg_json = json.loads((fake_home / ".config" / "remote-fleet" / "login-mount.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg_json["mount_dir"], str(mount_link))
            self.assertEqual(cfg_json["resolved_dir"], str(vol_target.resolve()))

            # Re-running install MUST NOT recompile or re-sign the app (keeps TCC CDHash intact!)
            invoked.clear()
            alt_dir = fake_home / "OtherRemote"
            alt_dir.mkdir()
            with (
                patch("remote_fleet.mac_integration.Path.home", return_value=fake_home),
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch(
                    "remote_fleet.mac_integration.shutil.which",
                    side_effect=lambda name: f"/usr/bin/{name}" if name in ("swiftc", "codesign", "osascript", "xattr", "launchctl") else None,
                ),
                patch(
                    "remote_fleet.mac_integration.subprocess.run",
                    side_effect=lambda cmd, **kw: invoked.append(list(cmd)) or sp.CompletedProcess(cmd, 0, stdout="", stderr=""),
                ),
            ):
                res2 = install_macos_login_item_app(home_dir=None, base_dir=alt_dir, register_login_item=True)

            self.assertTrue(res2["ok"])
            self.assertFalse(res2["newly_created"])
            self.assertTrue(res2["login_item_registered"])
            self.assertFalse(any(c[0].endswith("swiftc") or c[0].endswith("codesign") for c in invoked))
            # When Login Item succeeds, fallback LaunchAgent plist must be removed
            self.assertFalse(fb_plist_path.exists())
            cfg_json2 = json.loads((fake_home / ".config" / "remote-fleet" / "login-mount.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg_json2["mount_dir"], str(alt_dir))

            # Uninstall cleans up everything cleanly
            self.assertTrue(uninstall_macos_login_item_app(home_dir=fake_home))
            self.assertFalse(app_path.exists())
            self.assertFalse((fake_home / ".config" / "remote-fleet" / "login-mount.json").exists())

    def test_safe_kill_rclone_daemon_pid_protects_recycled_pids_after_reboot(self) -> None:
        """After a Mac reboot, a stale PID in `daemons/*.json` may now belong to Finder or Dock.
        `_safe_kill_rclone_daemon_pid` must NEVER send SIGTERM to a non-rclone process."""
        import subprocess as sp
        from unittest.mock import patch

        from remote_fleet.mac_integration import _safe_kill_rclone_daemon_pid

        killed: list[int] = []

        # Case 1: PID 777 belongs to Finder -> MUST NOT kill
        with (
            patch(
                "remote_fleet.mac_integration.subprocess.run",
                return_value=sp.CompletedProcess(["ps"], 0, stdout="/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder\n", stderr=""),
            ),
            patch("remote_fleet.mac_integration.os.kill", side_effect=lambda p, s: killed.append(p)),
        ):
            _safe_kill_rclone_daemon_pid(777)
        self.assertEqual(killed, [])

        # Case 2: PID 888 is a genuine `rclone serve nfs` process -> MUST kill
        with (
            patch(
                "remote_fleet.mac_integration.subprocess.run",
                return_value=sp.CompletedProcess(["ps"], 0, stdout="/Users/u/.local/bin/rclone serve nfs :sftp:/C:/ --addr 127.0.0.1:50000\n", stderr=""),
            ),
            patch("remote_fleet.mac_integration.os.kill", side_effect=lambda p, s: killed.append(p)),
        ):
            _safe_kill_rclone_daemon_pid(888)
        self.assertEqual(killed, [888])

    def test_mount_sshfs_on_macos_delegates_to_soft_nfs_not_legacy_nfsmount(self) -> None:
        """Verify `mount_sshfs` on macOS delegates to soft `_mount_sftp_directory` instead of hard `rclone nfsmount`."""
        from unittest.mock import patch

        from remote_fleet.config import FleetConfig, MachineConfig
        from remote_fleet.mount import mount_sshfs
        from remote_fleet.transport import EndpointStatus, FleetTransport

        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "ws"
            spark = MachineConfig(id="spark01", name="Spark 01", lan_host="spark01.local", user="danlinyu", os="linux")
            cfg = FleetConfig(machines=[spark])
            tr = FleetTransport(cfg)
            fake_ep = EndpointStatus(machine_id="spark01", transport="lan", host="spark01.local", port=22, reachable=True, latency_ms=1.0)
            captured: dict[str, object] = {}

            def fake_mount_sftp(**kwargs: object) -> dict[str, object]:
                captured.update(kwargs)
                return {"ok": True, "engine": "rclone-serve-nfs-soft", "mount_point": str(local_dir)}

            with (
                patch.object(tr, "resolve_endpoint", return_value=fake_ep),
                patch.object(tr, "run_worker_op", return_value={"ok": True, "path": "/home/danlinyu/proj"}),
                patch("remote_fleet.mount.platform.system", return_value="Darwin"),
                patch("remote_fleet.mount.is_mounted", return_value=False),
                patch("remote_fleet.mount.shutil.which", side_effect=lambda n: "/usr/local/bin/rclone" if n == "rclone" else None),
                patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_sftp),
                patch("remote_fleet.mount.subprocess.run", side_effect=AssertionError("Legacy rclone nfsmount must not run on macOS!")),
            ):
                res = mount_sshfs(tr, spark, "~/proj", local_dir)

            self.assertTrue(res["ok"], res)
            self.assertEqual(res["mode"], "rclone-serve-nfs-soft")
            self.assertEqual(captured["remote_target"], "/home/danlinyu/proj")

    def test_macos_nfs_stability_options_and_spotlight_guard(self) -> None:
        """Verify hardened macOS `rclone serve nfs` + `/sbin/mount -t nfs` options, SSH keepalives,
        and Spotlight `.metadata_never_index` container protection that eliminate spurious disconnect alerts."""
        import subprocess as sp
        from unittest.mock import patch

        from remote_fleet.config import MachineConfig
        from remote_fleet.mac_integration import (
            DAEMON_SCHEMA_VERSION,
            _cleanup_unmounted_empty_dir,
            _ensure_spotlight_never_index,
            _mount_sftp_directory,
            build_rclone_sftp_ssh_command,
        )

        self.assertGreaterEqual(DAEMON_SCHEMA_VERSION, 3)

        pc = MachineConfig(
            id="pc",
            name="Home PC",
            lan_host="192.168.68.70",
            user="danli",
            os="windows",
        )
        ssh_cmd = build_rclone_sftp_ssh_command(pc, "192.168.68.70")
        self.assertIn("ConnectTimeout=5", ssh_cmd)
        self.assertIn("ServerAliveInterval=15", ssh_cmd)
        self.assertIn("ServerAliveCountMax=2", ssh_cmd)
        self.assertIn("TCPKeepAlive=yes", ssh_cmd)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            fake_home = tmp_path / "home"
            fake_home.mkdir()
            container = tmp_path / "RemoteComputers" / "pc"
            container.mkdir(parents=True)

            # Spotlight marker created on unmounted APFS container, and cleaned up with `.DS_Store` if empty
            with (
                patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                patch("remote_fleet.mac_integration.is_mounted", return_value=False),
            ):
                _ensure_spotlight_never_index(container)
            self.assertTrue((container / ".metadata_never_index").is_file())

            dead_sub = container / "Z"
            dead_sub.mkdir()
            (dead_sub / ".DS_Store").write_bytes(b"finder")
            (dead_sub / ".metadata_never_index").touch()
            with patch("remote_fleet.mac_integration.is_mounted", return_value=False):
                _cleanup_unmounted_empty_dir(dead_sub)
            self.assertFalse(dead_sub.exists())

            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                serve_cmds: list[list[str]] = []
                mount_cmds: list[list[str]] = []

                class _FakeProc:
                    pid = 54321

                    def poll(self) -> None:
                        return None

                    def terminate(self) -> None:
                        return None

                def fake_popen(cmd: list[str], **kwargs: object) -> _FakeProc:
                    serve_cmds.append(list(cmd))
                    return _FakeProc()

                def fake_run(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
                    mount_cmds.append(list(cmd))
                    return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

                # Mount secondary Windows drive D:/ (longer 15m dir-cache) and WSL Y:/ (5m dir-cache)
                for remote_target, mname, extra in (
                    ("D:/", "D", {}),
                    (
                        "wsl://Ubuntu/",
                        "Y",
                        {
                            "sftp_server_command": r"C:\Windows\System32\wsl.exe -d Ubuntu -e /usr/lib/openssh/sftp-server",
                            "treat_as_posix": True,
                            "rclone_remote_path": "/",
                            "registry_extra": {"wsl_distro": "Ubuntu", "wsl_root": "/", "windows_drive": "Y:"},
                        },
                    ),
                ):
                    mp = container / mname
                    probe_calls = {"n": 0}

                    def fake_probe(*args: object, **kwargs: object) -> str:
                        probe_calls["n"] += 1
                        return "gone" if probe_calls["n"] == 1 else "healthy"

                    with (
                        patch("remote_fleet.mac_integration.Path.home", return_value=fake_home),
                        patch("remote_fleet.mac_integration.platform.system", return_value="Darwin"),
                        patch("remote_fleet.mac_integration.find_rclone_binary", return_value="/usr/local/bin/rclone"),
                        patch("remote_fleet.mac_integration.os.path.exists", return_value=True),
                        patch("remote_fleet.mac_integration._probe_mount_health", side_effect=fake_probe),
                        patch("remote_fleet.mac_integration._teardown_mount_and_daemon"),
                        patch("remote_fleet.mac_integration._wait_tcp_ready", return_value=True),
                        patch("remote_fleet.mac_integration.is_mounted", return_value=True),
                        patch("remote_fleet.mac_integration.get_macos_mount_table", return_value=None),
                        patch("remote_fleet.mac_integration._safe_sample_entries", return_value=["home", "etc"]),
                        patch(
                            "remote_fleet.mac_integration._select_verified_rclone_sftp_flags",
                            return_value=(extra.get("rclone_remote_path", "/D:/"), ["--sftp-host", "192.168.68.70"], ""),
                        ),
                        patch("remote_fleet.mac_integration.subprocess.Popen", side_effect=fake_popen),
                        patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_run),
                    ):
                        res = _mount_sftp_directory(
                            machine=pc,
                            ep_dict={"transport": "lan", "host": "192.168.68.70"},
                            host="192.168.68.70",
                            remote_target=remote_target,
                            mount_point=mp,
                            vol_label=f"Fleet-pc-{mname}",
                            log_stem=f"pc-{mname}",
                            allow_empty_fallback=False,
                            **extra,  # type: ignore[arg-type]
                        )
                    self.assertTrue(res["ok"], res)

                self.assertEqual(len(serve_cmds), 2)
                self.assertEqual(len(mount_cmds), 2)

                # Secondary Windows HDD drive D: uses 15m dir-cache, WSL uses 5m dir-cache; both use 5m SFTP idle timeout
                d_serve, y_serve = serve_cmds
                self.assertEqual(d_serve[d_serve.index("--dir-cache-time") + 1], "15m")
                self.assertEqual(y_serve[y_serve.index("--dir-cache-time") + 1], "5m")
                for sc in serve_cmds:
                    self.assertNotIn("--attr-timeout", sc)
                    self.assertEqual(sc[sc.index("--vfs-cache-max-size") + 1], "256M")
                    self.assertEqual(sc[sc.index("--vfs-cache-max-age") + 1], "15m")
                    self.assertEqual(sc[sc.index("--sftp-idle-timeout") + 1], "5m")
                    self.assertEqual(sc[sc.index("--contimeout") + 1], "5s")
                    self.assertEqual(sc[sc.index("--timeout") + 1], "15s")
                    self.assertEqual(sc[sc.index("--low-level-retries") + 1], "3")

                # macOS `/sbin/mount -t nfs` uses IPv4 127.0.0.1:/ + hardened NFSv3 stability options (never `nobrowse`)
                for mc in mount_cmds:
                    self.assertIn("127.0.0.1:/", mc)
                    self.assertNotIn("localhost:/", mc)
                    opts = mc[mc.index("-o") + 1]
                    for req_opt in (
                        "vers=3",
                        "proto=tcp",
                        "soft",
                        "intr",
                        "deadtimeout=0",
                        "mutejukebox",
                        "noquota",
                        "noatime",
                        "acregmin=5",
                        "acregmax=60",
                        "acdirmin=15",
                        "acdirmax=120",
                    ):
                        self.assertIn(req_opt, opts)
                    self.assertNotIn("nobrowse", opts)
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_windows_wsl_discovery_mounting_and_path_mapping(self) -> None:
        """Verify end-to-end Windows WSL / Y: discovery, rclone `--sftp-server-command` preflight,
        mounting under `~/RemoteComputers/pc/Y` + `WSL -> Y` shortcut, and `load_active_project` mapping."""
        import subprocess as sp
        from unittest.mock import patch

        from remote_fleet.config import MachineConfig, load_mounts_registry, save_mounts_registry
        from remote_fleet.mac_integration import (
            _decode_windows_cli_bytes,
            _mount_windows_all_drives,
            _parse_windows_network_registry_wsl,
            _parse_wsl_list_verbose,
            _select_verified_rclone_sftp_flags,
            discover_windows_wsl_targets,
        )

        # 1. UTF-16LE decoding (with & without BOM) + UTF-8 + str
        wsl_raw_text = (
            "  NAME                   STATE           VERSION\r\n"
            "* Ubuntu                 Running         2\r\n"
            "  Debian                 Running         2\r\n"
            "  Ubuntu-24.04           Stopped         2\r\n"
            "  docker-desktop         Running         2\r\n"
            "  docker-desktop-data    Stopped         2\r\n"
            "  rancher-desktop        Stopped         2\r\n"
            "  podman-machine-default Stopped         2\r\n"
        )
        self.assertIn("Ubuntu", _decode_windows_cli_bytes(wsl_raw_text.encode("utf-16-le")))
        self.assertIn("Ubuntu", _decode_windows_cli_bytes(wsl_raw_text.encode("utf-16")))
        self.assertIn("Ubuntu", _decode_windows_cli_bytes(wsl_raw_text.encode("utf-8")))

        # 2. `wsl.exe -l -v` parser filters Docker/Rancher/Podman and places default first
        distros = _parse_wsl_list_verbose(wsl_raw_text)
        self.assertEqual([d["name"] for d in distros], ["Ubuntu", "Debian", "Ubuntu-24.04"])
        self.assertTrue(distros[0]["is_default"])

        # 3. `reg.exe query HKCU\Network /s` parser extracts WSL drives and ignores regular SMB shares
        reg_output = "\n".join(
            [
                "HKEY_CURRENT_USER\\Network\\S",
                "    RemotePath    REG_SZ    \\\\nas-box\\media",
                "",
                "HKEY_CURRENT_USER\\Network\\Y",
                "    RemotePath    REG_SZ    \\\\wsl.localhost\\Ubuntu",
                "    ProviderName    REG_SZ    Plan 9 Network Provider",
                "",
                "HKEY_CURRENT_USER\\Network\\Z",
                "    RemotePath    REG_SZ    \\\\wsl$\\Ubuntu-24.04\\home\\danli",
            ]
        )
        mapped = _parse_windows_network_registry_wsl(reg_output)
        self.assertEqual(
            mapped,
            [
                {"drive_letter": "Y", "distro": "Ubuntu", "wsl_root": "/"},
                {"drive_letter": "Z", "distro": "Ubuntu-24.04", "wsl_root": "/home/danli"},
            ],
        )

        # 4. `discover_windows_wsl_targets` merges HKCU\Network + wsl.exe -l -v + sftp-server probe
        #    and skips Stopped unmapped distros (`Ubuntu-24.04`) without cold-booting them!
        pc = MachineConfig(
            id="pc",
            name="Home PC (danlin-hud)",
            lan_host="192.168.68.70",
            user="danli",
            os="windows",
        )
        reg_y_only = "HKEY_CURRENT_USER\\Network\\Y\n    RemotePath    REG_SZ    \\\\wsl.localhost\\ubuntu\n"
        probed_cmds: list[str] = []

        def fake_discover_ssh(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[bytes]:
            remote_arg = cmd[-1]
            probed_cmds.append(remote_arg)
            if "reg.exe" in remote_arg:
                return sp.CompletedProcess(cmd, 0, stdout=reg_y_only.encode("utf-8"), stderr=b"")
            if "wsl.exe -l -v" in remote_arg:
                return sp.CompletedProcess(cmd, 0, stdout=wsl_raw_text.encode("utf-16-le"), stderr=b"")
            if "-d Ubuntu -e ls -1" in remote_arg:
                return sp.CompletedProcess(
                    cmd,
                    2,
                    stdout=b"/usr/lib/openssh/sftp-server\n",
                    stderr=b"ls: cannot access '/usr/libexec/openssh/sftp-server': No such file or directory\n",
                )
            if "-d Debian -e ls -1" in remote_arg:
                return sp.CompletedProcess(cmd, 2, stdout=b"", stderr=b"ls: cannot access ...\n")
            return sp.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")

        with (
            patch("remote_fleet.mac_integration.shutil.which", return_value="/usr/bin/ssh"),
            patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_discover_ssh),
        ):
            targets = discover_windows_wsl_targets(pc, "192.168.68.70")

        self.assertEqual([t["mount_name"] for t in targets], ["Y", "WSL-Debian"])
        self.assertFalse(any("Ubuntu-24.04" in c for c in probed_cmds))
        # Canonical distro casing from `wsl.exe -l -v` ("Ubuntu") is preserved even when registry had "ubuntu"
        self.assertEqual(targets[0]["distro"], "Ubuntu")
        self.assertEqual(targets[0]["drive_letter"], "Y")
        self.assertEqual(targets[0]["remote_uri"], "wsl://Ubuntu/")
        self.assertEqual(
            targets[0]["sftp_server_command"],
            r"C:\Windows\System32\wsl.exe -d Ubuntu -e /usr/lib/openssh/sftp-server",
        )
        self.assertEqual(targets[1]["sftp_server_command"], "")
        self.assertIn("openssh-sftp-server", targets[1]["probe_error"])

        # 5. Unmapped WSL distro mounts as `WSL`; explicit `wsl_drives` overrides drive letter & subpath
        def fake_discover_unmapped(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[bytes]:
            remote_arg = cmd[-1]
            if "reg.exe" in remote_arg:
                return sp.CompletedProcess(cmd, 1, stdout=b"", stderr=b"ERROR: The system was unable to find")
            if "wsl.exe -l -v" in remote_arg:
                return sp.CompletedProcess(
                    cmd, 0, stdout="* Ubuntu   Running   2\r\n".encode("utf-16-le"), stderr=b""
                )
            if "-d Ubuntu -e ls -1" in remote_arg:
                return sp.CompletedProcess(cmd, 0, stdout=b"/usr/lib/openssh/sftp-server\n", stderr=b"")
            return sp.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")

        with (
            patch("remote_fleet.mac_integration.shutil.which", return_value="/usr/bin/ssh"),
            patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_discover_unmapped),
        ):
            unmapped_targets = discover_windows_wsl_targets(pc, "192.168.68.70")
            pc_override = MachineConfig(
                id="pc",
                name="Home PC",
                lan_host="192.168.68.70",
                user="danli",
                os="windows",
                wsl_drives={"Y": "Ubuntu:/home/danli"},
            )
            override_targets = discover_windows_wsl_targets(pc_override, "192.168.68.70")

        self.assertEqual([t["mount_name"] for t in unmapped_targets], ["WSL"])
        self.assertEqual(override_targets[0]["mount_name"], "Y")
        self.assertEqual(override_targets[0]["wsl_root"], "/home/danli")
        self.assertEqual(override_targets[0]["remote_uri"], "wsl://Ubuntu/home/danli")

        # 6. `_select_verified_rclone_sftp_flags` with `sftp_server_command` uses native SSH (NEVER `--sftp-ssh` or `--sftp-skip-links`)
        seen_rclone_cmds: list[list[str]] = []

        def fake_wsl_lsf(cmd: list[str], **kwargs: object) -> sp.CompletedProcess[str]:
            seen_rclone_cmds.append(list(cmd))
            return sp.CompletedProcess(cmd, 0, stdout="home/\netc/\n", stderr="")

        with (
            patch("remote_fleet.mac_integration._detect_default_ssh_key", return_value="/tmp/id_ed25519"),
            patch("remote_fleet.mac_integration.subprocess.run", side_effect=fake_wsl_lsf),
        ):
            vpath, vflags, verr = _select_verified_rclone_sftp_flags(
                "/usr/local/bin/rclone",
                pc,
                "192.168.68.70",
                "/",
                allow_empty_fallback=False,
                sftp_server_command=targets[0]["sftp_server_command"],
                treat_as_posix=True,
            )
        self.assertEqual(verr, "")
        self.assertEqual(vpath, "/")
        assert vflags is not None
        self.assertIn("--sftp-server-command", vflags)
        self.assertNotIn("--sftp-ssh", vflags)
        self.assertNotIn("--sftp-skip-links", vflags)

        # 7. `_mount_windows_all_drives` mounts C, D + WSL Y + creates `WSL -> Y` symlink + maps `load_active_project`
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            pc_container = tmp_path / "RemoteComputers" / "pc"
            old_cfg_dir = os.environ.get("REMOTE_FLEET_CONFIG_DIR")
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            try:
                mount_calls: list[dict[str, object]] = []

                def fake_mount_dir(**kwargs: object) -> dict[str, object]:
                    mount_calls.append(dict(kwargs))
                    mp = kwargs["mount_point"]
                    assert isinstance(mp, Path)
                    mp.mkdir(parents=True, exist_ok=True)
                    target = str(kwargs["remote_target"])
                    if target == "C:/":
                        (mp / "Users" / "danli").mkdir(parents=True, exist_ok=True)
                    elif target == "wsl://Ubuntu/":
                        (mp / "home" / "danli" / "ml-job").mkdir(parents=True, exist_ok=True)
                    reg = load_mounts_registry()
                    entry = {"machine_id": pc.id, "remote_root": target}
                    extra = kwargs.get("registry_extra")
                    if isinstance(extra, dict):
                        entry.update({str(k): str(v) for k, v in extra.items() if v})
                    reg[str(mp.resolve())] = entry
                    save_mounts_registry(reg)
                    return {"ok": True, "visible_count": 4, "already_mounted": False}

                ping = {
                    "ok": True,
                    "os": "windows",
                    "home": "C:/Users/danli",
                    "drives": ["C:/", "D:/", "Y:/"],
                    "wsl_targets": targets,
                }
                with (
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_dir),
                    patch("remote_fleet.mac_integration.time.sleep", return_value=None),
                ):
                    res = _mount_windows_all_drives(
                        pc, {"transport": "lan", "host": "192.168.68.70"}, "192.168.68.70", ping, pc_container
                    )

                self.assertTrue(res["ok"], res)
                # Y: is claimed by WSL so regular Windows drives only mount C:/ and D:/
                self.assertEqual(res["drives"], ["C:/", "D:/"])
                self.assertEqual(len(res["wsl_mounts"]), 1)
                self.assertEqual(res["wsl_mounts"][0]["mount_name"], "Y")
                self.assertEqual(res["wsl_mounts"][0]["distro"], "Ubuntu")
                self.assertTrue(res.get("wsl_shortcut_created"))
                self.assertTrue((pc_container / "WSL").is_symlink())
                self.assertEqual((pc_container / "WSL").readlink(), Path("Y"))
                self.assertIn("Y", res["sample_entries"])
                self.assertIn("WSL", res["sample_entries"])
                # Unmapped secondary WSL-Debian lacking sftp-server is quietly skipped when Y: is mapped
                self.assertNotIn("failed_drives", res)

                # Mounted path auto-binding maps `pc/Y/...`, `pc/Y`, AND `pc/WSL/...` back to `wsl://Ubuntu/...`!
                s_sub = load_active_project(cwd=pc_container / "Y" / "home" / "danli" / "ml-job")
                assert s_sub is not None
                self.assertEqual((s_sub.machine_id, s_sub.remote_path), ("pc", "wsl://Ubuntu/home/danli/ml-job"))

                s_root = load_active_project(cwd=pc_container / "Y")
                assert s_root is not None
                self.assertEqual((s_root.machine_id, s_root.remote_path), ("pc", "wsl://Ubuntu/"))

                s_link = load_active_project(cwd=pc_container / "WSL" / "home" / "danli" / "ml-job")
                assert s_link is not None
                self.assertEqual((s_link.machine_id, s_link.remote_path), ("pc", "wsl://Ubuntu/home/danli/ml-job"))

                # Whereas an explicitly mapped drive (Y:) lacking sftp-server MUST emit the actionable install hint
                missing_y_ping = {
                    "ok": True,
                    "os": "windows",
                    "home": "C:/Users/danli",
                    "drives": ["C:/"],
                    "wsl_targets": [{**targets[0], "sftp_server": "", "sftp_server_command": "", "probe_error": targets[1]["probe_error"]}],
                }
                with (
                    patch("remote_fleet.mac_integration.is_mounted", return_value=False),
                    patch("remote_fleet.mac_integration._mount_sftp_directory", side_effect=fake_mount_dir),
                    patch("remote_fleet.mac_integration.time.sleep", return_value=None),
                ):
                    res_miss = _mount_windows_all_drives(
                        pc, {"transport": "lan", "host": "192.168.68.70"}, "192.168.68.70", missing_y_ping, tmp_path / "pc_miss"
                    )
                self.assertTrue(
                    any("openssh-sftp-server" in msg for msg in res_miss.get("failed_drives") or []),
                    res_miss.get("failed_drives"),
                )
            finally:
                if old_cfg_dir is None:
                    os.environ.pop("REMOTE_FLEET_CONFIG_DIR", None)
                else:
                    os.environ["REMOTE_FLEET_CONFIG_DIR"] = old_cfg_dir

    def test_cleanup_remote_fleet_cache_preserves_live_and_removes_stale(self) -> None:
        """Verify `cleanup_remote_fleet_cache` preserves active daemon caches while removing
        orphaned NFS/VFS caches, dead daemon JSONs, obsolete logs, temp Swift source, and legacy rclone cache."""
        import json
        from unittest.mock import patch

        from remote_fleet.mac_integration import cleanup_remote_fleet_cache

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            cache = fake_home / ".cache" / "remote-fleet"
            daemons = cache / "daemons"
            daemons.mkdir(parents=True)

            # Live daemon: pc-C (PID 11111 mocked live)
            (daemons / "pc-C.json").write_text(json.dumps({"pid": 11111}), encoding="utf-8")
            (cache / "nfs-handles" / "pc-C").mkdir(parents=True)
            (cache / "nfs-handles" / "pc-C" / "h1").write_bytes(b"live-handle")
            (cache / "vfs-cache" / "pc-C").mkdir(parents=True)
            (cache / "vfs-cache" / "pc-C" / "f1").write_bytes(b"live-vfs")
            (cache / "rclone-pc-C.log").write_text("live log\n", encoding="utf-8")

            # Dead/orphaned daemon: spark02-Home (PID 22222 mocked dead) + legacy junk
            (daemons / "spark02-Home.json").write_text(json.dumps({"pid": 22222}), encoding="utf-8")
            (cache / "nfs-handles" / "spark02-Home").mkdir(parents=True)
            (cache / "nfs-handles" / "spark02-Home" / "old").write_bytes(b"x" * 2048)
            (cache / "vfs-cache" / "spark02-Home").mkdir(parents=True)
            (cache / "vfs-cache" / "spark02-Home" / "old.bin").write_bytes(b"y" * 4096)
            (cache / "rclone-spark02-Home.log").write_text("old log\n" * 50, encoding="utf-8")
            (cache / "automount.log").write_text("legacy launchd\n", encoding="utf-8")
            (cache / "repair-sidebar-v1").write_bytes(b"bin")
            (cache / "repair_sidebar_v1.swift").write_text("import Foundation\n", encoding="utf-8")

            legacy_rclone = fake_home / ".cache" / "rclone" / "serve-nfs-handle-cache"
            legacy_rclone.mkdir(parents=True)
            (legacy_rclone / "stale").write_bytes(b"z" * 1024)

            with (
                patch("remote_fleet.mac_integration._is_live_rclone_daemon_pid", side_effect=lambda p: p == 11111),
                patch("remote_fleet.mac_integration.shutil.which", return_value=None),
            ):
                dry = cleanup_remote_fleet_cache(home_dir=fake_home, dry_run=True)
                self.assertEqual(dry["live_mounts"], ["pc-C"])
                self.assertGreater(dry["removed_count"], 5)
                self.assertGreater(dry["freed_bytes"], 7000)
                # Dry run must not delete anything
                self.assertTrue((cache / "vfs-cache" / "spark02-Home").exists())
                self.assertTrue(legacy_rclone.exists())

                res = cleanup_remote_fleet_cache(home_dir=fake_home, dry_run=False)

            self.assertEqual(res["live_mounts"], ["pc-C"])
            self.assertEqual(res["removed_count"], dry["removed_count"])
            self.assertEqual(res["freed_bytes"], dry["freed_bytes"])

            # Active live mount files preserved
            self.assertTrue((daemons / "pc-C.json").is_file())
            self.assertTrue((cache / "nfs-handles" / "pc-C" / "h1").is_file())
            self.assertTrue((cache / "vfs-cache" / "pc-C" / "f1").is_file())
            self.assertTrue((cache / "rclone-pc-C.log").is_file())
            self.assertTrue((cache / "repair-sidebar-v1").is_file())

            # Stale & obsolete files removed
            self.assertFalse((daemons / "spark02-Home.json").exists())
            self.assertFalse((cache / "nfs-handles" / "spark02-Home").exists())
            self.assertFalse((cache / "vfs-cache" / "spark02-Home").exists())
            self.assertFalse((cache / "rclone-spark02-Home.log").exists())
            self.assertFalse((cache / "automount.log").exists())
            self.assertFalse((cache / "repair_sidebar_v1.swift").exists())
            self.assertFalse(legacy_rclone.exists())


if __name__ == "__main__":
    unittest.main()
