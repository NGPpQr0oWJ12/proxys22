"""Small terminal renderer; plain output remains readable in redirected logs."""
import atexit
import os
import shutil
import sys
import textwrap
import time


def size_text(value):
    return f'{value / (1024 * 1024):.1f}'.replace('.', ',') + ' МБ'


def windows_color(stream):
    """Enable VT output on this console; restore its previous mode on exit."""
    try:
        import ctypes
        import msvcrt
        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        mode = ctypes.c_uint32()
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 5 == 5:
            return True
        if not kernel.SetConsoleMode(handle, mode.value | 5):
            return False
        atexit.register(kernel.SetConsoleMode, handle, mode.value)
        return True
    except (ImportError, AttributeError, OSError, ValueError):
        return False


class Console:
    def __init__(self, stream=None, error_stream=None):
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        self.tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        self.color = self.tty and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
        if os.name == 'nt' and self.color:
            self.color = windows_color(self.stream)
        self.width = max(36, min(84, shutil.get_terminal_size((80, 24)).columns - 2))
        self.active = None

    def paint(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def end_progress(self):
        if self.active is not None:
            self.active.finish()

    def line(self, text='', code=None, error=False):
        self.end_progress()
        print(self.paint(text, code) if code else text,
              file=self.error_stream if error else self.stream, flush=True)

    def panel(self, title, lines=()):
        self.line()
        self.line('┌' + '─' * (self.width - 2) + '┐', '36')
        for text in (title, *lines):
            for row in textwrap.wrap(str(text), self.width - 6, break_long_words=False) or ['']:
                # Long file paths are printed outside panels by the callers.
                self.line('│  ' + row.ljust(self.width - 6) + '  │', '36' if text == title else None)
        self.line('└' + '─' * (self.width - 2) + '┘', '36')

    def heading(self, title):
        self.line()
        self.line(title, '1;36')
        self.line('─' * min(self.width, max(28, len(title))), '36')

    def step(self, number, total, message):
        self.line(f'[{number}/{total}] {message}', '1;36')

    def success(self, message):
        self.line('✓ ' + message, '32')

    def warning(self, message):
        self.line('! ' + message, '33')

    def error(self, message):
        self.line('Ошибка: ' + message, '31', error=True)

    def prompt(self, message, default=None):
        self.end_progress()
        suffix = f' [{default}]' if default is not None else ''
        self.line(message + suffix, '1')
        return input('  > ').strip() or (default if default is not None else '')

    def confirm(self, message, expected):
        self.line()
        self.warning(message)
        answer = input(self.paint('  > ', '1;33'))
        return answer.strip().casefold() == expected.strip().casefold()

    def progress(self, label):
        return Progress(self, label)


class Progress:
    def __init__(self, console, label):
        self.console, self.label = console, label
        self.last_bytes, self.bucket = -1, -1
        self.last_draw = 0.0
        self.started = time.monotonic()
        self.finished = False

    def __call__(self, completed, total):
        if total <= 0:
            return
        completed = max(0, min(completed, total))
        if self.finished and completed == 0:
            self.last_bytes, self.bucket, self.finished = -1, -1, False
            self.started = time.monotonic()
        if self.finished or completed < self.last_bytes:
            return
        self.last_bytes = completed
        percent = completed * 100 // total
        now = time.monotonic()
        if self.console.tty:
            if percent < 100 and self.bucket >= 0 and now - self.last_draw < 0.1:
                return
        elif percent // 10 == self.bucket and percent != 100:
            return
        self.bucket, self.last_draw = percent // 10, now
        bar_width = min(22, max(10, self.console.width // 4))
        filled = percent * bar_width // 100
        bar = '█' * filled + '░' * (bar_width - filled)
        text = f'{self.label} [{bar}] {percent:3d}%  {size_text(completed)} / {size_text(total)}'
        if self.console.width < 70:
            text = f'{self.label} {percent:3d}%  {size_text(completed)} / {size_text(total)}'
        if self.console.active is not None and self.console.active is not self:
            self.console.end_progress()
        self.console.active = self
        if self.console.tty:
            prefix = '\r\033[2K' if self.console.color else '\r'
            print(prefix + self.console.paint(text.ljust(self.console.width), '36'), end='', file=self.console.stream, flush=True)
        else:
            print(text, file=self.console.stream, flush=True)
        if percent == 100:
            self.finish()

    def finish(self):
        if self.console.active is self:
            if self.console.tty:
                print(file=self.console.stream, flush=True)
            self.console.active = None
        self.finished = True
