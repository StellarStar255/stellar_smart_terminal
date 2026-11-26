"""Observe selection in a blank text column through the native display."""
import time


def capture_column(x, top, bottom):
    from PIL import ImageGrab

    image = ImageGrab.grab(bbox=(x, top, x + 1, bottom)).convert('RGB')
    return [image.getpixel((0, y)) for y in range(image.height)]


def selected_band(probe, baseline, endpoint_y):
    # The native button event and Qt's paint are asynchronous. Wait for the
    # lower edge to reach the released pointer before unblocking the PTY.
    index = endpoint_y - probe[1]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        selected = capture_column(*probe)
        color = selected[index]
        if color != baseline[index]:
            edge = index
            while edge + 1 < len(selected) and selected[edge + 1] == color:
                edge += 1
            if edge + 1 < len(selected) and selected[edge + 1] == baseline[edge + 1]:
                return edge, color
        time.sleep(.05)
    raise AssertionError('No visible selection ending at the released pointer')


def band_moved(before, baseline, after):
    # A three-row scroll must leave an upper band highlighted and restore
    # background below it. A stale frame or a vanished highlight fails.
    edge, color = before
    if after[edge] != baseline[edge]:
        return False
    remaining = [i for i in range(edge) if after[i] == color and after[i] != baseline[i]]
    return (len(remaining) >= 8 and edge - remaining[-1] >= 8
            and after[remaining[-1] + 1:edge + 1] == baseline[remaining[-1] + 1:edge + 1])
