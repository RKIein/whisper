"""
Build Script — package whisper dictation as a Windows .exe.

Usage:
    python build.py          # Build the .exe
    python build.py --clean  # Remove previous build artifacts first

Prerequisites:
    pip install pyinstaller

Output:
    dist/WhisperDictation/    — folder with the .exe and all dependencies
"""

import os
import shutil
import subprocess
import sys

APP_NAME = "WhisperDictation"
ENTRY_POINT = "app.py"
ICON_FILE = "assets/icon.ico"  # Optional — will skip if missing

# Modules that PyInstaller sometimes misses
HIDDEN_IMPORTS = [
    "faster_whisper",
    "ctranslate2",
    "sounddevice",
    "pystray",
    "pynput",
    "pynput.keyboard",
    "pynput.keyboard._win32",
    "pynput.mouse",
    "pynput.mouse._win32",
    "customtkinter",
    "pyperclip",
    "PIL",
    "numpy",
    "torch",
    "torchaudio",
]

# Data files to include (source, dest_in_bundle)
DATA_FILES = [
    # customtkinter needs its themes bundled
]


def find_customtkinter_path():
    """Find customtkinter install path for bundling."""
    try:
        import customtkinter
        return os.path.dirname(customtkinter.__file__)
    except ImportError:
        return None


def build():
    """Run PyInstaller with the right flags."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--windowed",           # No console window
        "--noconfirm",          # Overwrite previous build
        "--clean",              # Clean PyInstaller cache
    ]

    # Add icon if it exists
    if os.path.exists(ICON_FILE):
        cmd.extend(["--icon", ICON_FILE])

    # Hidden imports
    for imp in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", imp])

    # Bundle customtkinter theme data
    ctk_path = find_customtkinter_path()
    if ctk_path:
        cmd.extend(["--add-data", f"{ctk_path};customtkinter"])

    # Data files
    for src, dest in DATA_FILES:
        if os.path.exists(src):
            cmd.extend(["--add-data", f"{src};{dest}"])

    # Entry point
    cmd.append(ENTRY_POINT)

    print(f"\n  Building {APP_NAME}...")
    print(f"  Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"\n  Build successful!")
        print(f"  Output: dist/{APP_NAME}/")
        print(f"  Run:    dist/{APP_NAME}/{APP_NAME}.exe")
        print(f"\n  Note: Models are NOT bundled. They download on first run")
        print(f"  to ~/.cache/huggingface/hub/ (~150-500 MB depending on config).\n")
    else:
        print(f"\n  Build failed with code {result.returncode}")
        sys.exit(1)


def clean():
    """Remove build artifacts."""
    for d in ["build", "dist", "__pycache__"]:
        if os.path.isdir(d):
            shutil.rmtree(d)
            print(f"  Removed {d}/")

    spec = f"{APP_NAME}.spec"
    if os.path.exists(spec):
        os.remove(spec)
        print(f"  Removed {spec}")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()

    build()
