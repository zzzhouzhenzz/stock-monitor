import plistlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stock_monitor.service import render_service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "config with spaces.json"
        self.config.write_text("{}", encoding="utf-8")
        self.output = self.root / "generated"
        self.repository = Path(__file__).resolve().parents[1]

    def test_macos_plist_contains_exact_arguments_and_log_paths(self):
        result = render_service(self.config, self.output, platform_name="Darwin")
        self.assertEqual(result, self.output / "com.stock-monitor.meta.plist")
        data = plistlib.loads(result.read_bytes())
        self.assertEqual(data["Label"], "com.stock-monitor.meta")
        self.assertEqual(data["ProgramArguments"], [sys.executable, str(self.repository / "monitor.py"),
                                                  "run", "--config", str(self.config)])
        self.assertEqual(data["WorkingDirectory"], str(self.repository))
        self.assertEqual(data["EnvironmentVariables"], {"PYTHONUNBUFFERED": "1"})
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(data["ThrottleInterval"], 30)
        self.assertEqual(data["StandardOutPath"], str(self.root / ".state" / "monitor.stdout.log"))
        self.assertEqual(data["StandardErrorPath"], str(self.root / ".state" / "monitor.stderr.log"))
        self.assertTrue((self.root / ".state").is_dir())
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)

    def test_linux_unit_escapes_specifiers_and_quotes_without_shell(self):
        repository = self.root / 'project with %h $HOME "quotes" \\ backslash'
        config = self.root / 'config %h ${HOME} "quoted".json'
        config.write_text("{}", encoding="utf-8")
        executable = repository / ".venv" / "bin" / "python3"
        with patch("stock_monitor.service.__file__", str(repository / "src" / "stock_monitor" / "service.py")):
            result = render_service(config, self.output, python_executable=executable, platform_name="Linux")
        text = result.read_text(encoding="utf-8")
        self.assertEqual(result.name, "stock-monitor.service")
        self.assertIn("Type=simple\n", text)
        self.assertIn("Restart=on-failure\nRestartSec=30\n", text)
        self.assertIn("WantedBy=default.target\n", text)
        self.assertIn("Environment=PYTHONUNBUFFERED=1\n", text)
        self.assertIn(f'WorkingDirectory={self.root}/project with %%h $HOME "quotes" \\ backslash/\n', text)
        command = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertTrue(command.startswith(f'ExecStart=":{self.root}/project with %%h $HOME \\"quotes\\" \\\\ backslash/'))
        self.assertIn('"run" "--config"', command)
        self.assertIn('config %%h ${HOME} \\"quoted\\".json"', command)
        self.assertNotIn("sh -c", command)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)

    def test_preserves_default_virtual_environment_interpreter_symlink(self):
        interpreter = self.root / ".venv" / "bin" / "python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        with patch("stock_monitor.service.sys.executable", str(interpreter)):
            result = render_service(self.config, self.output, platform_name="Darwin")
        self.assertEqual(plistlib.loads(result.read_bytes())["ProgramArguments"][0], str(interpreter))
        self.assertNotEqual(str(interpreter), str(interpreter.resolve()))

    def test_replaces_generated_file_with_private_permissions(self):
        result = render_service(self.config, self.output, platform_name="Linux")
        result.chmod(0o644)
        render_service(self.config, self.output, platform_name="Linux")
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        self.assertEqual(list(self.output.iterdir()), [result])

    def test_requires_existing_config_file(self):
        for config in (self.root / "missing.json", self.root):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, "existing file"):
                render_service(config, self.output, platform_name="Darwin")
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / ".state").exists())

    def test_unsupported_platform_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, "Darwin and Linux"):
            render_service(self.config, self.output, platform_name="Windows")
        self.assertFalse(self.output.exists())

    def test_control_characters_in_paths_are_rejected(self):
        for keyword, value in (("config_path", self.root / "line\nbreak.json"),
                               ("output_dir", self.root / "line\nbreak"),
                               ("python_executable", "/bin/python\r3")):
            options = {"config_path": self.config, "output_dir": self.output, "platform_name": "Linux"}
            options[keyword] = value
            with self.subTest(keyword=keyword), self.assertRaisesRegex(ValueError, "control characters"):
                render_service(**options)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
