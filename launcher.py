"""
Launcher — run the app's helper windows/tools as separate processes.

tkinter windows can't share the main thread with pystray, so dialogs and the
lecture library run as their own process. When frozen with PyInstaller there
are no .py files, so the exe is re-launched with a "--tool" flag instead
(see the dispatch at the bottom of app.py).
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))

TOOLS = {
    "lecture_dialog": "lecture_dialog.py",
    "library":        "library.py",
    "calendar_setup": "calendar_setup.py",
    "lecture":        "lecture.py",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tool_command(tool: str, *args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, f"--{tool}", *args]
    return [sys.executable, os.path.join(APP_DIR, TOOLS[tool]), *args]


def spawn(tool: str, *args: str) -> subprocess.Popen | None:
    """Start a tool without waiting for it."""
    try:
        return subprocess.Popen(tool_command(tool, *args), cwd=APP_DIR,
                                creationflags=_NO_WINDOW)
    except Exception as e:
        logger.error(f"Failed to launch {tool}: {e}")
        return None


def run(tool: str, *args: str, timeout: float | None = None) -> int:
    """Run a tool and wait for it to exit. Returns the exit code."""
    try:
        return subprocess.run(tool_command(tool, *args), cwd=APP_DIR,
                              creationflags=_NO_WINDOW, timeout=timeout).returncode
    except Exception as e:
        logger.error(f"Failed to run {tool}: {e}")
        return -1


def open_path(path: str):
    """Open a file or folder with the system's default app."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        logger.error(f"Could not open {path}: {e}")
