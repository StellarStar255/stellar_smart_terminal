"""PTY fixture matching Codex's transcript-only wheel hit testing (no network/model)."""
import json
import os
import re
import sys
import time
from pathlib import Path

if os.name == 'nt':
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    # ConPTY's child is a console application. Receive VT input immediately
    # and write UTF-8 control sequences without the CRT converting newlines.
    for fd in (0, 1):
        msvcrt.setmode(fd, os.O_BINARY)
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            # The Windows venv launcher or test capture can redirect stdio.
            # Bind this test viewer directly to its attached pseudoconsole.
            name = 'CONIN$' if fd == 0 else 'CONOUT$'
            device = os.open(name, os.O_RDWR | os.O_BINARY)
            try:
                os.dup2(device, fd)
            finally:
                os.close(device)
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
            if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                raise ctypes.WinError(ctypes.get_last_error())
        if fd == 0:
            mode.value = (mode.value & ~0x7) | 0x200  # raw + ENABLE_VIRTUAL_TERMINAL_INPUT
        else:
            mode.value |= 0x5  # processed output + ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not kernel.SetConsoleMode(handle, mode):
            raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.SetConsoleOutputCP(65001):
        raise ctypes.WinError(ctypes.get_last_error())
else:
    import tty
    tty.setraw(0)

cols, rows = os.get_terminal_size(1)
offset = 100
accepted = []
report_path = Path(sys.argv[1])
delay_last_frame = '--delay-last-frame' in sys.argv[2:]
delay_down = '--delay-down-frame' in sys.argv[2:]


def draw():
    parts = ['\x1b[H\x1b[2JFixed transcript header']
    for row in range(1, rows - 5):
        parts.append(f'\x1b[{row + 1};1Hrow {offset + row:04d} 中文 sample')
    for row, text in enumerate(['• Waiting for background terminal (esc to interrupt)',
                                '  └ gh run watch 123', '↓ Back to bottom · esc',
                                'Model status', 'Keyboard shortcuts'], rows - 5):
        parts.append(f'\x1b[{row + 1};1H{text}')
    os.write(1, ''.join(parts).encode())
    temporary = report_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(rows=rows, offset=offset, accepted=accepted)))
    # A concurrent Windows reader can briefly hold the destination without
    # delete sharing. Keep the update atomic while its read handle closes.
    for attempt in range(20):
        try:
            temporary.replace(report_path)
            break
        except PermissionError:
            if os.name != 'nt' or attempt == 19:
                raise
            time.sleep(.01)


os.write(1, b'\x1b[?1049h\x1b[?1000;1002;1003;1006h\x1b[?1003l')
draw()
pending = b''
pattern = re.compile(rb'\x1b\[<(64|65);(\d+);(\d+)M')
while True:
    data = os.read(0, 4096)
    if not data:
        break
    pending += data
    matches = list(pattern.finditer(pending))
    for match in matches:
        button, col, row = map(int, match.groups())
        # Outside transcript + no forwarded native drag => Codex ignores the wheel.
        if 1 <= row - 1 < rows - 5:
            if ((delay_last_frame and offset <= 100 - 2 * rows)
                    or (delay_down and offset >= 100 + 2 * rows)):
                # Acknowledge the wheel now, but draw only after mouse-up.
                report_path.with_suffix('.pending').touch()
                while not report_path.with_suffix('.release').exists():
                    time.sleep(.005)
                delay_last_frame = delay_down = False
            accepted.append(row)
            offset = max(0, min(1000, offset + (-3 if button == 64 else 3)))
            draw()
    if matches:
        pending = pending[matches[-1].end():]
    pending = pending[-128:]
