"""A small FITS writer and reader for 32-bit float images with one or three planes.

Files are written bottom-up (ROWORDER = 'BOTTOM-UP'), the FITS convention, so other
tools (Siril, PixInsight, DS9) show them the right way up. Reading gives top-down rows,
as a memory map, so a crop or a strip only reads those rows from disk.
"""
import os

import numpy as np

BLOCK = 2880
CARD = 80


def _format_value(value):
    if isinstance(value, bool):
        return f"{'T' if value else 'F':>20}"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):>20}"
    if isinstance(value, (float, np.floating)):
        text = f"{float(value):.10G}"
        if "." not in text and "E" not in text:
            text += ".0"
        return f"{text:>20}"
    text = str(value).replace("'", "''")
    text = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text)[:68]
    return f"'{text:<8}'"


def _card(key, value=None, comment=None):
    key = key.upper()[:8]
    if value is None:
        text = f"{key:<8}"
    else:
        text = f"{key:<8}= {_format_value(value)}"
        if comment:
            text += f" / {comment}"
    return text[:CARD].ljust(CARD)


def write_fits(path, data, header=None, rows_per_chunk=256):
    """Write a float32 image, (H, W) or (C, H, W), with extra header cards from `header`
    ({key: value} or {key: (value, comment)})."""
    if data.ndim == 2:
        planes, height, width = 1, data.shape[0], data.shape[1]
        data = data[None]
    elif data.ndim == 3:
        planes, height, width = data.shape
    else:
        raise ValueError("FITS data must be 2-D or 3-D")

    cards = [
        _card("SIMPLE", True, "conforms to FITS standard"),
        _card("BITPIX", -32, "32-bit floating point"),
        _card("NAXIS", 2 if planes == 1 else 3),
        _card("NAXIS1", width),
        _card("NAXIS2", height),
    ]
    if planes > 1:
        cards.append(_card("NAXIS3", planes))
    cards.append(_card("ROWORDER", "BOTTOM-UP", "first row is the bottom of the image"))
    for key, value in (header or {}).items():
        comment = None
        if isinstance(value, tuple):
            value, comment = value
        if value is None:
            continue
        cards.append(_card(key, value, comment))
    cards.append(_card("END"))
    head = "".join(cards)
    head += " " * (-len(head) % BLOCK)

    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(head.encode("ascii"))
        written = 0
        for plane in range(planes):
            # Bottom-up: the last image row first
            for end in range(height, 0, -rows_per_chunk):
                start = max(0, end - rows_per_chunk)
                chunk = np.asarray(data[plane, start:end], dtype=np.float32)[::-1]
                block = chunk.astype(">f4").tobytes()
                f.write(block)
                written += len(block)
        f.write(b"\0" * (-written % BLOCK))
    os.replace(tmp, path)


def read_header(path):
    """The header as {key: value}, and the byte offset of the data."""
    header = {}
    with open(path, "rb") as f:
        offset = 0
        while True:
            block = f.read(BLOCK)
            if len(block) < BLOCK:
                raise ValueError("Truncated FITS header")
            offset += BLOCK
            done = False
            for i in range(0, BLOCK, CARD):
                card = block[i:i + CARD].decode("ascii", errors="replace")
                key = card[:8].strip()
                if key == "END":
                    done = True
                    break
                if card[8:10] != "= ":
                    continue
                raw = card[10:]
                if raw.lstrip().startswith("'"):
                    text = raw.lstrip()[1:]
                    value, j = "", 0
                    while j < len(text):
                        if text[j] == "'":
                            if j + 1 < len(text) and text[j + 1] == "'":
                                value += "'"
                                j += 2
                                continue
                            break
                        value += text[j]
                        j += 1
                    header[key] = value.rstrip()
                    continue
                raw = raw.split("/")[0].strip()
                if raw in ("T", "F"):
                    header[key] = raw == "T"
                else:
                    try:
                        header[key] = int(raw)
                    except ValueError:
                        try:
                            header[key] = float(raw)
                        except ValueError:
                            header[key] = raw
            if done:
                return header, offset


def open_fits(path):
    """(header, data) where data is a read-only (C, H, W) float view, top row first.
    Slicing it reads only the rows needed."""
    header, offset = read_header(path)
    if header.get("BITPIX") != -32:
        raise ValueError("Only 32-bit float FITS files are supported")
    width, height = header["NAXIS1"], header["NAXIS2"]
    planes = header.get("NAXIS3", 1) if header.get("NAXIS", 2) == 3 else 1
    data = np.memmap(path, dtype=">f4", mode="r", offset=offset, shape=(planes, height, width))
    if header.get("ROWORDER", "BOTTOM-UP") == "BOTTOM-UP":
        data = data[:, ::-1, :]
    return header, data
