"""Run under Xvfb. Exercise the installed .deb through X11 and its clipboard."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from installed_drag_visual import band_moved, capture_column, selected_band


def output(*args):
    return subprocess.check_output(args, text=True, timeout=10).strip()


def wait_for(process, predicate, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Installed application exited with {process.returncode}")
        if predicate():
            return
        time.sleep(.05)
    raise TimeoutError(description)


def main():
    with tempfile.TemporaryDirectory(prefix='stellar-installed-drag-') as directory:
        data = Path(directory)
        fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/transcript_mouse_viewer.py'
        shutil.copyfile(fixture, data / 'viewer.py')
        config = {
            'onboarding_shown': True, 'workspace_restore_enabled': False,
            'last_preset_index': 0,
            'presets': [{'name': 'Drag test', 'commands': ['/usr/bin/python3 -u viewer.py wheel.json --delay-down-frame']}],
        }
        (data / '.smart_terminal_config.json').write_text(json.dumps(config))
        report = data / 'wheel.json'
        process = subprocess.Popen(['/usr/bin/stellar-smart-terminal', '--working-dir', directory],
                                   env=dict(os.environ, STELLAR_DATA_DIR=directory, QT_QPA_PLATFORM='xcb'))
        try:
            wait_for(process, report.exists, 'Test session did not start')
            rows = json.loads(report.read_text())['rows']
            wid = output('xdotool', 'search', '--onlyvisible', '--pid', str(process.pid)).splitlines()[-1]
            output('xdotool', 'windowmove', wid, '100', '100', 'windowfocus', wid)
            geometry = dict(line.split('=', 1) for line in
                            output('xdotool', 'getwindowgeometry', '--shell', wid).splitlines())
            x, y, width, height = (int(geometry[k]) for k in ('X', 'Y', 'WIDTH', 'HEIGHT'))
            x = str(x + int(.70 * width))
            end_y = y + int(.69 * height)
            probe = (int(x) - 16, y + int(.40 * height), y + int(.75 * height))
            baseline = capture_column(*probe)
            output('xdotool', 'mousemove', x, str(y + int(.62 * height)), 'mousedown', '1',
                   'mousemove', x, str(y + height + 20))
            try:
                wait_for(process, report.with_suffix('.pending').exists,
                         'Dragging past the window did not scroll two pages')
                output('xdotool', 'mousemove', x, str(end_y))
            finally:
                output('xdotool', 'mouseup', '1')
            before = selected_band(probe, baseline, end_y)
            offset = json.loads(report.read_text())['offset']
            report.with_suffix('.release').touch()
            wait_for(process, lambda: json.loads(report.read_text())['offset'] > offset, 'Missing late PTY frame')
            wait_for(process, lambda: band_moved(before, baseline, capture_column(*probe)),
                     'Visible selection did not follow the delayed redraw')
            output('xdotool', 'key', 'ctrl+c')
            # xdotool returns before Qt has necessarily handled the key event
            # and claimed the X11 clipboard. Wait for the Unicode text target.
            def text_available():
                targets = subprocess.run(
                    ['xclip', '-selection', 'clipboard', '-t', 'TARGETS', '-o'],
                    capture_output=True, timeout=10)
                return targets.returncode == 0 and b'UTF8_STRING' in targets.stdout.splitlines()

            wait_for(process, text_available, 'Copied text was not offered on the X11 clipboard')
            lines = output('xclip', '-selection', 'clipboard', '-t', 'UTF8_STRING', '-o').splitlines()
            # The two endpoints may be partial rows. Every interior row must
            # retain both its number and its Chinese text, in sequence.
            interior = lines[1:-1]
            if len(interior) <= rows:
                raise AssertionError(f'Only {len(interior)} complete interior rows for a {rows}-row terminal')
            start = re.fullmatch(r'row (\d+) 中文 sample', interior[0])
            if not start:
                raise AssertionError(f'Unexpected first interior row: {interior[0]!r}')
            first = int(start[1])
            if first >= offset + 1:
                raise AssertionError("Clipboard omitted rows scrolled out before release")
            expected = [f'row {i:04d} 中文 sample' for i in range(first, first + len(interior))]
            if interior != expected:
                raise AssertionError('Installed application copied missing, altered, or out-of-order rows')
            print(f'PASS: installed .deb copied {len(interior)} consecutive rows via X11')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
