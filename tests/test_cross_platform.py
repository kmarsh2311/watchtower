import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import librarymanager
import librarymanager_monitor
import librarymanager_core


class CrossPlatformSimulationTests(unittest.TestCase):

    def test_windows_startup_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_appdata = temp_path / "AppData" / "Roaming"
            fake_startup = fake_appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            fake_db = temp_path / "test.sqlite3"
            fake_db.touch()

            with patch.object(sys, "platform", "win32"),                  patch.dict(os.environ, {"APPDATA": str(fake_appdata)}),                  patch.object(Path, "home", return_value=temp_path):

                # 1. Check initial status
                status = librarymanager.system_startup_status()
                self.assertTrue(status["supported"])
                self.assertEqual(status["platform_label"], "Windows")
                self.assertFalse(status["enabled"])
                self.assertIn("stash-librarymanager-startup.vbs", status["path"])

                # 2. Enable startup
                server_conn = {"ApiKey": "test-key", "endpoint": "http://localhost:9999/graphql"}
                new_status = librarymanager.configure_system_startup(True, server_conn, fake_db)
                self.assertTrue(new_status["enabled"])
                self.assertTrue((fake_startup / "stash-librarymanager-startup.vbs").exists())

                # 3. Verify VBS script content and escaping
                vbs_text = (fake_startup / "stash-librarymanager-startup.vbs").read_text(encoding="utf-8")
                self.assertIn('Set WshShell = CreateObject("WScript.Shell")', vbs_text)
                self.assertIn('librarymanager_startup.py', vbs_text)
                self.assertIn('--runtime', vbs_text)
                self.assertIn('0, False', vbs_text)  # Window hidden, non-blocking

                # 4. Verify runtime JSON
                runtime_file = Path(librarymanager.__file__).with_name("startup-runtime.json")
                self.assertTrue(runtime_file.exists())
                runtime_data = json.loads(runtime_file.read_text(encoding="utf-8"))
                self.assertEqual(runtime_data["server_connection"]["ApiKey"], "test-key")
                self.assertEqual(runtime_data["database"], str(fake_db))

                # 5. Disable startup
                off_status = librarymanager.configure_system_startup(False, server_conn, fake_db)
                self.assertFalse(off_status["enabled"])
                self.assertFalse((fake_startup / "stash-librarymanager-startup.vbs").exists())
                self.assertFalse(runtime_file.exists())

    def test_linux_startup_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_autostart = temp_path / ".config" / "autostart"
            fake_db = temp_path / "test.sqlite3"
            fake_db.touch()

            with patch.object(sys, "platform", "linux"),                  patch.object(Path, "home", return_value=temp_path):

                # 1. Check initial status
                status = librarymanager.system_startup_status()
                self.assertTrue(status["supported"])
                self.assertEqual(status["platform_label"], "Linux")
                self.assertFalse(status["enabled"])
                self.assertIn("stash-librarymanager.desktop", status["path"])

                # 2. Enable startup
                server_conn = {"endpoint": "http://localhost:9999/graphql"}
                new_status = librarymanager.configure_system_startup(True, server_conn, fake_db)
                self.assertTrue(new_status["enabled"])
                self.assertTrue((fake_autostart / "stash-librarymanager.desktop").exists())

                # 3. Verify .desktop file content
                desktop_text = (fake_autostart / "stash-librarymanager.desktop").read_text(encoding="utf-8")
                self.assertIn("[Desktop Entry]", desktop_text)
                self.assertIn("Type=Application", desktop_text)
                self.assertIn("Name=Watchtower Stash Monitor", desktop_text)
                self.assertIn("librarymanager_startup.py", desktop_text)
                self.assertIn("X-GNOME-Autostart-enabled=true", desktop_text)

                # 4. Disable startup
                off_status = librarymanager.configure_system_startup(False, server_conn, fake_db)
                self.assertFalse(off_status["enabled"])
                self.assertFalse((fake_autostart / "stash-librarymanager.desktop").exists())

    def test_windows_notification_generation_and_escaping(self):
        with patch.object(sys, "platform", "win32"),              patch("librarymanager_monitor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            # Test notification with double quotes, special characters, and emojis
            test_title = 'Stash "Library" Manager'
            test_message = 'Scene "Super Title: 100% & More" was renamed!'

            librarymanager.send_system_notification(test_title, test_message)

            self.assertTrue(mock_run.called)
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "powershell")
            ps_command = args[4]
            self.assertIn("Windows.UI.Notifications.ToastNotificationManager", ps_command)
            self.assertIn('Stash `"Library`" Manager', ps_command)
            self.assertIn('Scene `"Super Title: 100% & More`" was renamed!', ps_command)

    def test_linux_notification_via_notify_send(self):
        with patch.object(sys, "platform", "linux"),              patch("librarymanager_monitor.shutil.which", return_value="/usr/bin/notify-send"), patch("librarymanager_monitor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            librarymanager.send_system_notification("Stash Alert", "Test Linux Message")

            self.assertTrue(mock_run.called)
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "notify-send")
            self.assertIn("-a", args)
            self.assertIn("Stash Library Manager", args)
            self.assertIn("Stash Alert", args)
            self.assertIn("Test Linux Message", args)

    def test_monitor_daemon_notify_cross_platform(self):
        # 1. On Windows
        with patch.object(sys, "platform", "win32"), patch("librarymanager_monitor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            librarymanager_monitor.notify(True, "New video added: test.mp4")
            self.assertTrue(mock_run.called)
            self.assertEqual(mock_run.call_args[0][0][0], "powershell")

        # 2. On Linux with notify-send
        with patch.object(sys, "platform", "linux"),              patch("librarymanager_monitor.shutil.which", return_value="/usr/bin/notify-send"), patch("librarymanager_monitor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            librarymanager_monitor.notify(True, "New video added: test.mp4")
            self.assertTrue(mock_run.called)
            self.assertEqual(mock_run.call_args[0][0][0], "notify-send")

        # 3. Disabled notification should never invoke subprocess
        with patch.object(sys, "platform", "win32"), patch("librarymanager_monitor.subprocess.run") as mock_run:
            librarymanager_monitor.notify(False, "Should not run")
            self.assertFalse(mock_run.called)


if __name__ == "__main__":
    unittest.main()
