"""
Audio Player — play a lecture's audio.mp3 inside the app.

Uses Windows' built-in MCI player (winmm), so nothing has to be decoded or
bundled: play, pause, seek and playback speed come for free. Call it from
the Tk thread only. On other systems `available` is False and the library
falls back to the system's audio app.
"""

import ctypes
import itertools
import logging
import sys

logger = logging.getLogger(__name__)

_ids = itertools.count(1)


class AudioPlayer:

    available = sys.platform == "win32"

    def __init__(self):
        self._alias = f"lecture{next(_ids)}"
        self._open = False
        self.path: str | None = None
        self.duration_ms = 0
        self.speed = 1.0

    def _cmd(self, command: str) -> str:
        buf = ctypes.create_unicode_buffer(256)
        err = ctypes.windll.winmm.mciSendStringW(command, buf, 256, 0)
        if err:
            msg = ctypes.create_unicode_buffer(256)
            ctypes.windll.winmm.mciGetErrorStringW(err, msg, 256)
            raise OSError(f"{msg.value} ({command.split()[0]})")
        return buf.value

    def load(self, path: str):
        """Open a file (closes the previous one). Raises OSError if it can't play."""
        self.close()
        self._cmd(f'open "{path}" type mpegvideo alias {self._alias}')
        self._open = True
        self.path = path
        self._cmd(f"set {self._alias} time format milliseconds")
        self.duration_ms = int(self._cmd(f"status {self._alias} length") or 0)
        self.set_speed(self.speed)

    @property
    def loaded(self) -> bool:
        return self._open

    @property
    def playing(self) -> bool:
        return self._open and self._cmd(f"status {self._alias} mode") == "playing"

    def position_ms(self) -> int:
        return int(self._cmd(f"status {self._alias} position") or 0) if self._open else 0

    def play(self, from_ms: int | None = None):
        if not self._open:
            return
        if from_ms is None:
            if self.position_ms() >= self.duration_ms - 200:
                from_ms = 0                     # at the end → start over
            else:
                self._cmd(f"play {self._alias}")
                return
        self._cmd(f"play {self._alias} from {max(0, min(int(from_ms), self.duration_ms))}")

    def pause(self):
        if self._open:
            self._cmd(f"pause {self._alias}")

    def seek(self, ms: int):
        """Jump; keeps playing if it was playing."""
        if not self._open:
            return
        if self.playing:
            self.play(ms)
        else:
            self._cmd(f"seek {self._alias} to {max(0, min(int(ms), self.duration_ms))}")

    def set_speed(self, speed: float):
        self.speed = speed
        if self._open:
            try:
                self._cmd(f"set {self._alias} speed {int(speed * 1000)}")
            except OSError as e:
                logger.warning(f"Playback speed not supported: {e}")

    def close(self):
        if self._open:
            try:
                self._cmd(f"close {self._alias}")
            except OSError:
                pass
        self._open = False
        self.path = None
        self.duration_ms = 0
