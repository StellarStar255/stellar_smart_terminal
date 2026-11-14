"""PTY fixture matching Codex's transcript-only wheel hit testing (no network/model)."""
import json
import os
import re
import sys
import tty
from pathlib import Path

tty.setraw(0)
cols, rows = os.get_terminal_size(0)
offset = 100
accepted = []
report_path = Path(sys.argv[1])


def draw():
    parts = ['\x1b[H\x1b[2JFixed transcript header']
    for row in range(1, rows - 5):
        parts.append(f'\x1b[{row + 1};1Hrow {offset + row:04d} 中文 sample')
    for row, text in enumerate(['Back to bottom', 'Ask Codex to do anything', '',
                                'Model status', 'Keyboard shortcuts'], rows - 5):
        parts.append(f'\x1b[{row + 1};1H{text}')
    os.write(1, ''.join(parts).encode())
    temporary = report_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(offset=offset, accepted=accepted)))
    temporary.replace(report_path)


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
            accepted.append(row)
            offset = max(0, min(1000, offset + (-3 if button == 64 else 3)))
            draw()
    if matches:
        pending = pending[matches[-1].end():]
    pending = pending[-128:]
