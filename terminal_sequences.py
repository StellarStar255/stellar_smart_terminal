"""Bounded framing for escape sequences before terminal-specific filtering.

PTY reads may end anywhere. Emit complete control sequences so filters and
query responders see the same input regardless of read boundaries.
"""


class TerminalSequenceBuffer:
    MAX_SEQUENCE_CHARS = 16 * 1024

    def __init__(self):
        self._pending = []
        self._kind = ''
        self._previous_escape = False
        self._discard = False

    def feed(self, text):
        # Most terminal traffic is ordinary text; avoid scanning it in Python.
        if not self._kind and '\x1b' not in text:
            return text
        output = []
        i = 0
        while i < len(text):
            if not self._kind:
                escape = text.find('\x1b', i)
                if escape < 0:
                    output.append(text[i:])
                    break
                output.append(text[i:escape])
                self._kind = 'escape'
                self._pending = ['\x1b']
                i = escape + 1
                continue
            else:
                char = text[i]
                if char == '\x1b' and self._kind != 'string':
                    self._kind = 'escape'
                    self._pending = [char]
                    self._discard = False
                    i += 1
                    continue
                if not self._discard:
                    self._pending.append(char)
                done = False
                if self._kind == 'escape':
                    if char == '[':
                        self._kind = 'csi'
                    elif char in ']P_^':
                        self._kind = 'string'
                    elif ' ' <= char <= '/':
                        self._kind = 'intermediate'
                    else:
                        done = True
                elif self._kind == 'csi':
                    done = '@' <= char <= '~'
                elif self._kind == 'intermediate':
                    done = '0' <= char <= '~'
                else:
                    done = char == '\x07' or (self._previous_escape and char == '\\')
                # CAN/SUB abort any pending sequence.
                done = done or char in '\x18\x1a'
                if done:
                    if not self._discard:
                        output.append(''.join(self._pending))
                    self._pending = []
                    self._kind = ''
                    self._discard = False
                    self._previous_escape = False
                else:
                    if len(self._pending) > self.MAX_SEQUENCE_CHARS:
                        self._pending = []
                        self._discard = True
                    self._previous_escape = char == '\x1b'
            i += 1
        return ''.join(output)
