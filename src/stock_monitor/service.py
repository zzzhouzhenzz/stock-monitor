"""Render service files for review; never install or start a service."""

import os
import platform
import plistlib
import sys
import tempfile
from pathlib import Path


def _path(value, resolve=True):
    path = Path(value).expanduser()
    for candidate in (path, path.resolve() if resolve else path.absolute()):
        if any(ord(character) < 32 or ord(character) == 127 for character in str(candidate)):
            raise ValueError("Service paths must not contain control characters or newlines")
    return candidate


def _unit_argument(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_service(config_path, output_dir, python_executable=None, platform_name=None):
    """Return the generated file path. Config must exist; no credentials are read.

    The selected Python path keeps symlinks so virtual environments still work.
    The caller installs the generated file and supplies any required environment
    variables separately. Invalid paths/platforms raise ValueError.
    """
    system = platform.system() if platform_name is None else platform_name
    if system not in ("Darwin", "Linux"):
        raise ValueError("Service files support only Darwin and Linux")
    config = _path(config_path)
    if not config.is_file():
        raise ValueError("Service config must be an existing file")
    output = _path(output_dir)
    executable = _path(sys.executable if python_executable is None else python_executable, resolve=False)
    repository = _path(Path(__file__).parents[2])
    arguments = [str(executable), str(repository / "monitor.py"), "run", "--config", str(config)]

    if system == "Darwin":
        logs = config.parent / ".state"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = plistlib.dumps({
            "Label": "com.stock-monitor.meta",
            "ProgramArguments": arguments,
            "WorkingDirectory": str(repository),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": str(logs / "monitor.stdout.log"),
            "StandardErrorPath": str(logs / "monitor.stderr.log"),
        })
        target = output / "com.stock-monitor.meta.plist"
    else:
        # ':' disables environment expansion. '%' specifiers still need escaping.
        command = " ".join(_unit_argument(argument) for argument in [":" + arguments[0], *arguments[1:]])
        # WorkingDirectory is a raw path, not a shell/ExecStart argument. A final
        # slash prevents a literal final backslash from becoming a continuation.
        directory = str(repository).replace("%", "%%") + "/"
        content = ("[Unit]\nDescription=META price and volume monitor\n\n"
                   "[Service]\nType=simple\n"
                   f"WorkingDirectory={directory}\nExecStart={command}\n"
                   "Environment=PYTHONUNBUFFERED=1\nRestart=on-failure\nRestartSec=30\n\n"
                   "[Install]\nWantedBy=default.target\n").encode("utf-8")
        target = output / "stock-monitor.service"

    output.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=output, prefix=".service-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, target)  # mkstemp creates the file with mode 0600.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
