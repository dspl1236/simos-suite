"""
flasher/lzss_compress.py — Pure Python LZSS compression for Simos ECUs

Ported from VW_Flash (bri3d/VW_Flash) lib/lzss/lzss.c (Michael Dipperstein).

Parameters matching the C implementation:
  WINDOW_SIZE = 1023   (10-bit sliding window)
  MAX_UNCODED = 2      (minimum match length to encode)
  MAX_CODED   = 65     (maximum match length)
  Bit packing: [10-bit offset][6-bit length] packed as 2 bytes
  Flag byte:   8 bits MSB-first (1=raw byte, 0=encoded reference)
  Padding:     output padded to 16-byte boundary (AES block size)
"""
from __future__ import annotations
import logging

log = logging.getLogger("SimosSuite.LZSS")

WINDOW_SIZE  = 1023
MAX_UNCODED  = 2
MAX_CODED    = 65


def lzss_compress(data: bytes) -> bytes:
    """Compress data using LZSS matching VW_Flash parameters."""
    n       = len(data)
    window  = bytearray(WINDOW_SIZE)
    win_pos = 0
    pos     = 0
    output  = bytearray()
    symbols: list = []

    def _find_match(start: int) -> tuple[int, int]:
        best_len = MAX_UNCODED
        best_off = 0
        max_len  = min(MAX_CODED, n - start)
        for back in range(1, min(WINDOW_SIZE, start + 1) + 1):
            wpos = (win_pos - back) % WINDOW_SIZE
            mlen = 0
            while mlen < max_len:
                if data[start + mlen] == window[(wpos + mlen) % WINDOW_SIZE]:
                    mlen += 1
                else:
                    break
            if mlen > best_len:
                best_len = mlen
                best_off = wpos
                if mlen == MAX_CODED:
                    break
        return best_off, best_len

    def _flush(syms: list):
        flag = 0
        for i, s in enumerate(syms):
            if s[0]:  # raw
                flag |= (1 << (7 - i))
        output.append(flag)
        for s in syms:
            if s[0]:
                output.append(s[1])
            else:
                off, mlen = s[1], s[2]
                # Pack 10-bit offset + 6-bit (length-3) into 2 bytes
                token = ((off & 0x3FF) << 6) | ((mlen - 3) & 0x3F)
                output.append((token >> 8) & 0xFF)
                output.append(token & 0xFF)

    while pos < n:
        off, mlen = _find_match(pos)

        if mlen > MAX_UNCODED:
            symbols.append((False, off, mlen))
        else:
            symbols.append((True, data[pos], 0))
            mlen = 1

        for i in range(mlen):
            if pos + i < n:
                window[win_pos] = data[pos + i]
                win_pos = (win_pos + 1) % WINDOW_SIZE
        pos += mlen

        if len(symbols) == 8:
            _flush(symbols)
            symbols.clear()

    if symbols:
        _flush(symbols)

    # Pad to 16-byte boundary
    pad = (16 - len(output) % 16) % 16
    if pad:
        output.extend(bytes(pad))

    log.debug("LZSS: %d -> %d bytes (%.1f%%)", n, len(output),
              100 * len(output) / n if n else 0)
    return bytes(output)

def lzss_decompress(data: bytes) -> bytes:
    """
    Decompress LZSS-compressed data produced by lzss_compress().
    Matches the same parameters: WINDOW_SIZE=1023, flag byte MSB-first,
    10-bit offset + 6-bit (length-3) token.

    Used by read_block() to decompress ECU upload data before returning
    raw calibration bytes to the caller.
    """
    output  = bytearray()
    window  = bytearray(WINDOW_SIZE)
    win_pos = 0
    pos     = 0
    n       = len(data)

    while pos < n:
        flag = data[pos]; pos += 1
        for bit in range(7, -1, -1):
            if pos >= n:
                break
            if flag & (1 << bit):
                # Raw literal byte
                b = data[pos]; pos += 1
                output.append(b)
                window[win_pos] = b
                win_pos = (win_pos + 1) % WINDOW_SIZE
            else:
                # Encoded reference — 2 bytes: 10-bit offset + 6-bit length
                if pos + 1 >= n:
                    break
                hi = data[pos]; pos += 1
                lo = data[pos]; pos += 1
                token  = (hi << 8) | lo
                offset = (token >> 6) & 0x3FF
                length = (token & 0x3F) + 3
                for i in range(length):
                    b = window[(offset + i) % WINDOW_SIZE]
                    output.append(b)
                    window[win_pos] = b
                    win_pos = (win_pos + 1) % WINDOW_SIZE

    log.debug("LZSS decompress: %d -> %d bytes", n, len(output))
    return bytes(output)
