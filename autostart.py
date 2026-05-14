"""
Auto-start management — add/remove from Windows startup via the registry.

Uses HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run so no admin rights needed.
Launches via wscript + .vbs for correct working directory and no console window.
"""

import logging
import os
import winreg

logger = logging.getLogger(__name__)

_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "WhisperDictation"


def _get_launch_command() -> str:
    """Build the command that Windows will run at login."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    vbs_path = os.path.join(app_dir, "WhisperDictation.vbs")
    return f'wscript.exe "{vbs_path}"'


def is_enabled() -> bool:
    """Check if auto-start is currently enabled."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _APP_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def enable():
    """Add to Windows startup."""
    try:
        cmd = _get_launch_command()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        logger.info(f"Auto-start enabled: {cmd}")
        return True
    except Exception as e:
        logger.error(f"Failed to enable auto-start: {e}")
        return False


def disable():
    """Remove from Windows startup."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, _APP_NAME)
            logger.info("Auto-start disabled")
        except FileNotFoundError:
            pass  # Already removed
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as e:
        logger.error(f"Failed to disable auto-start: {e}")
        return False


def set_enabled(enabled: bool):
    """Enable or disable auto-start."""
    if enabled:
        return enable()
    else:
        return disable()
