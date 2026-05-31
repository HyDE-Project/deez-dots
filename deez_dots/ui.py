from __future__ import annotations

import itertools
import os
import re
import shutil
import sys
import threading
from typing import Optional

class UI:
    """Render user-facing status lines without changing testable output."""

    _RESET = "\033[0m"
    _GREEN = "\033[32m"
    _RED = "\033[31m"
    _BLUE = "\033[34m"
    _YELLOW = "\033[33m"
    _MAGENTA = "\033[35m"
    _CYAN = "\033[36m"
    _loader: Optional["Loader"] = None
    _output_lock = threading.RLock()

    @classmethod
    def _colors_enabled(cls) -> bool:
        term = os.getenv("TERM", "")
        return bool(sys.stdout.isatty() and term and term.lower() != "dumb" and "NO_COLOR" not in os.environ)

    @classmethod
    def can_use_loader(cls, debug: bool = False) -> bool:
        """Return True if a loader animation can be used in the current terminal session."""
        if debug:
            return False
        stdin = getattr(sys, "stdin", None)
        stdout = getattr(sys, "stdout", None)
        try:
            return bool(stdin and stdout and stdin.isatty() and stdout.isatty())
        except Exception:
            return False

    @classmethod
    def _prefix(cls, label: str, color: str) -> str:
        if not cls._colors_enabled():
            return label
        return f"{color}{label}{cls._RESET}"

    @classmethod
    def style(cls, text: str, color: str) -> str:
        """Return a color-styled string if colors are enabled."""
        return cls._prefix(text, color)

    @classmethod
    def _emit(cls, label: str, msg: str, color: str) -> None:
        with cls._output_lock:
            if cls._loader and cls._loader.is_running():
                cls._loader._clear_line_locked()
            print(f"{cls._prefix(label, color)} {msg}", flush=True)
            if cls._loader and cls._loader.is_running() and not cls._loader.is_paused():
                cls._loader._redraw_locked()

    @classmethod
    def plain(cls, msg: str = "") -> None:
        """Print a plain message to stdout, preserving loader output if active."""
        with cls._output_lock:
            if cls._loader and cls._loader.is_running():
                cls._loader._clear_line_locked()
            print(msg, flush=True)
            if cls._loader and cls._loader.is_running() and not cls._loader.is_paused():
                cls._loader._redraw_locked()

    @classmethod
    def start_loader(cls, message: str = "Working...") -> bool:
        """Start the spinner loader and return True if a new loader was created."""
        if cls._loader and cls._loader.is_running():
            cls._loader.set_message(message)
            return False
        cls._loader = Loader(message)
        cls._loader.start()
        return True

    @classmethod
    def stop_loader(cls) -> None:
        """Stop any active spinner loader and clean up its state."""
        if not cls._loader:
            return
        loader = cls._loader
        loader.stop()
        cls._loader = None

    @classmethod
    def set_loader_message(cls, message: str) -> None:
        """Update the active loader message without restarting the loader."""
        if cls._loader and cls._loader.is_running():
            cls._loader.set_message(message)

    @classmethod
    def pause_loader(cls) -> bool:
        """Pause the active loader and return True if it was paused."""
        if cls._loader and cls._loader.is_running() and not cls._loader.is_paused():
            cls._loader.pause()
            return True
        return False

    @classmethod
    def resume_loader(cls, was_paused: bool) -> None:
        """Resume the loader if it was previously paused."""
        if was_paused and cls._loader and cls._loader.is_running():
            cls._loader.resume()

    @classmethod
    def read_input(cls, prompt: str) -> str:
        """Read input from the user, temporarily pausing the loader if active."""
        was_paused = cls.pause_loader()
        try:
            return input(prompt)
        finally:
            cls.resume_loader(was_paused)

    @classmethod
    def progress(cls, msg: str) -> None:
        """Log progress text, using the loader if available or plain output otherwise."""
        if cls._loader and cls._loader.is_running():
            cls._loader.set_message(msg)
            return
        cls.info(msg)

    @classmethod
    def success(cls, msg: str) -> None:
        """Render a success message to the user."""
        cls._emit("[ok]", msg, cls._GREEN)

    @classmethod
    def error(cls, msg: str) -> None:
        """Render an error message to stdout while preserving CLI output behavior."""
        cls._emit("[err]", msg, cls._RED)

    @classmethod
    def info(cls, msg: str) -> None:
        """Render an informational message to the user."""
        cls._emit("[..]", msg, cls._BLUE)

    @classmethod
    def warn(cls, msg: str) -> None:
        """Render a warning message to the user."""
        cls._emit("[warn]", msg, cls._YELLOW)


class Loader:
    """A lightweight spinner used only for interactive, non-debug sessions."""

    _ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def __init__(self, message: str = "Working..."):
        self.message = message
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._paused = threading.Event()
        self._spinner = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
        self._colors = itertools.cycle([UI._GREEN, UI._BLUE, UI._YELLOW, UI._MAGENTA, UI._CYAN, UI._RED])
        self._last_frame = ""

    def start(self) -> None:
        """Start the loader animation thread if it is not already running."""
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the loader animation and clear any spinner output."""
        if not self._running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        with UI._output_lock:
            self._clear_line_locked()
        self._running = False

    def is_running(self) -> bool:
        """Return True if the loader animation is currently running."""
        return self._running

    def is_paused(self) -> bool:
        """Return True if the loader is currently paused."""
        return self._paused.is_set()

    def set_message(self, new_message: str) -> None:
        """Update the loader message and redraw it if active."""
        self.message = new_message
        with UI._output_lock:
            if not self.is_paused():
                self._redraw_locked()

    def pause(self) -> None:
        """Pause the loader animation and clear its current frame."""
        self._paused.set()
        with UI._output_lock:
            self._clear_line_locked()

    def resume(self) -> None:
        """Resume the loader animation after a pause."""
        self._paused.clear()
        with UI._output_lock:
            self._redraw_locked()

    def _line_width(self) -> int:
        return max(80, shutil.get_terminal_size(fallback=(80, 20)).columns)

    def _truncate_message(self, msg: str, reserved: int) -> str:
        width = self._line_width()
        if width <= reserved:
            return ""
        max_len = width - reserved
        if len(msg) <= max_len:
            return msg
        return msg[: max(0, max_len - 1)] + "…"

    def _visible_length(self, text: str) -> int:
        return len(self._ANSI_ESCAPE.sub("", text))

    def _line_count(self, frame: str) -> int:
        visible = self._visible_length(frame)
        return max(1, (visible + self._line_width() - 1) // self._line_width())

    def _clear_line_locked(self) -> None:
        lines = self._line_count(self._last_frame) if self._last_frame else 1
        for index in range(lines):
            sys.stdout.write("\r\033[2K")
            if index < lines - 1:
                sys.stdout.write("\x1b[1A")
        sys.stdout.flush()
        self._last_frame = ""

    def _redraw_locked(self) -> None:
        self._clear_line_locked()
        frame = self._format_frame(next(self._spinner))
        self._last_frame = frame
        sys.stdout.write(frame)
        sys.stdout.flush()

    def _format_frame(self, spinner: str) -> str:
        message = self._truncate_message(self.message, len(spinner) + 1)
        if UI._colors_enabled():
            color = next(self._colors)
            return f"{color}{spinner}{UI._RESET} {message}"
        return f"{spinner} {message}"

    def _animate(self) -> None:
        while not self._stop_event.is_set():
            if self.is_paused():
                self._stop_event.wait(0.05)
                continue
            frame = self._format_frame(next(self._spinner))
            with UI._output_lock:
                self._last_frame = frame
                self._clear_line_locked()
                sys.stdout.write(frame)
                sys.stdout.flush()
            self._stop_event.wait(0.1)


