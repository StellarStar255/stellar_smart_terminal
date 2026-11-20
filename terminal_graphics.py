"""Kitty graphics operations used by Codex's local PNG previews."""

import base64
import binascii
from collections import OrderedDict
from dataclasses import dataclass

# Codex matches "kitty" in TERM_PROGRAM to select inline PNG transmission.
GRAPHICS_TERM_PROGRAM = 'StellarSmartTerminal-kitty'
_MAX_ENCODED_BYTES = 4 * 1024 * 1024
_MAX_PIXELS = 2 * 1024 * 1024
_MAX_CACHE_BYTES = 16 * 1024 * 1024
_MAX_IMAGES = 32


@dataclass(frozen=True)
class Placement:
    column: int
    row: int
    columns: int
    rows: int


class TerminalGraphics:
    """Bounded PNG data and placements owned by one terminal screen."""

    def __init__(self):
        self.images = OrderedDict()
        self.placements = {}
        self._transfer = None
        self._cache_bytes = 0

    def clear(self):
        self.clear_placements()
        self.images.clear()
        self._cache_bytes = 0

    def clear_placements(self):
        self.placements.clear()
        self._transfer = None

    def handle(self, command, column, row):
        controls, separator, payload = command.partition(';')
        if not separator:
            return
        fields = dict(field.split('=', 1) for field in controls.split(',') if '=' in field)
        action = fields.get('a')
        if action == 'd':
            self._delete(fields)
        elif action == 'p':
            self._place(fields, column, row)
        elif action == 'T' and fields.get('t', 'd') == 'd' and fields.get('f') == '100':
            self._transfer = (fields, column, row, bytearray())
            self._receive(fields, payload)
        elif action is None and fields.keys() == {'m'}:
            self._receive(fields, payload)
        else:
            self._transfer = None

    def _receive(self, fields, payload):
        if self._transfer is None:
            return
        initial, column, row, encoded = self._transfer
        if len(encoded) + len(payload) > _MAX_ENCODED_BYTES or not payload.isascii():
            self._transfer = None
            return
        encoded.extend(payload.encode('ascii'))
        if fields.get('m') == '1':
            return
        self._transfer = None
        image_id = self._number(initial, 'i')
        if not image_id or not encoded:
            return
        image = self._decode_png(encoded)
        if image is None:
            return
        self._remove_image(image_id)
        self.images[image_id] = image
        self._cache_bytes += image.sizeInBytes()
        while len(self.images) > _MAX_IMAGES or self._cache_bytes > _MAX_CACHE_BYTES:
            self._remove_image(next(iter(self.images)))
        self._place(initial, column, row)

    @staticmethod
    def _decode_png(encoded):
        # Keep terminal_screen usable without importing Qt.
        from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
        from PyQt6.QtGui import QImageReader

        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None
        source = QBuffer()
        source.setData(QByteArray(data))
        source.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(source, b'png')
        size = reader.size()
        width, height = size.width(), size.height()
        if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= _MAX_PIXELS):
            return None
        image = reader.read()
        return None if image.isNull() else image

    def _place(self, fields, column, row):
        image_id = self._number(fields, 'i')
        columns = self._number(fields, 'c')
        rows = self._number(fields, 'r')
        if (fields.get('U') != '1' and image_id in self.images
                and 0 < columns <= 4096 and 0 < rows <= 4096):
            self.placements[image_id] = Placement(column, row, columns, rows)

    def _remove_image(self, image_id):
        image = self.images.pop(image_id, None)
        if image is not None:
            self._cache_bytes -= image.sizeInBytes()
        self.placements.pop(image_id, None)

    def _delete(self, fields):
        target = fields.get('d', 'a')
        image_id = self._number(fields, 'i')
        if target == 'I':
            self._remove_image(image_id)
        elif target == 'i':
            self.placements.pop(image_id, None)
        elif target == 'A':
            self.clear()
        elif target == 'a':
            self.clear_placements()

    @staticmethod
    def _number(fields, key):
        value = fields.get(key, '')
        return int(value) if value.isascii() and value.isdecimal() and len(value) <= 10 else 0
