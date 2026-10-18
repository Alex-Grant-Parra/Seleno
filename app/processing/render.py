"""Full-size exports of a developed stack: JPEG, PNG, 16-bit TIFF, or a linear FITS crop.

The master is read from its FITS file a strip at a time (a memory map), so a 21 MP
export needs little memory beyond the finished 8 or 16-bit picture.
"""
import os

import numpy as np

from . import develop, fitsio

FORMATS = ("jpeg", "png", "tiff", "fits")


def run(spec, progress):
    from PIL import Image

    fmt = spec.get("format", "jpeg")
    if fmt not in FORMATS:
        raise ValueError(f"Unknown export format {fmt}")
    resolved = spec["resolved"]
    _, master = fitsio.open_fits(spec["master"])
    _, height, width = master.shape
    y0, y1, x0, x1 = develop.crop_box(height, width, resolved["params"]["crop"])
    out_h, out_w = y1 - y0, x1 - x0
    out_path = spec["output"]
    tmp_path = out_path + ".part"

    if fmt == "fits":
        progress.sub(0.1, "Writing the linear FITS")
        fitsio.write_fits(tmp_path, master[:, y0:y1, x0:x1], {"CREATOR": "Seleno", "IMAGETYP": "Master light (cropped)"})
        os.replace(tmp_path, out_path)
        return {"kind": "render", "format": fmt, "width": out_w, "height": out_h, "bytes": os.path.getsize(out_path)}

    dtype = np.uint16 if fmt == "tiff" else np.uint8
    picture = np.zeros((out_h, out_w, 3), dtype=dtype)

    def read_rows(r0, r1):
        block = np.array(master[:, y0 + r0:y0 + r1, x0:x1], dtype=np.float32)
        return np.moveaxis(block, 0, 2)

    def write_rows(r0, r1, rgb):
        picture[r0:r1] = develop.to_uint16(rgb) if dtype == np.uint16 else develop.to_uint8(rgb)
        progress.sub(0.9 * r1 / out_h, "Developing at full size")

    develop.apply_strips(read_rows, out_h, out_w, resolved, write_rows)
    progress.sub(0.92, "Saving")
    if fmt == "tiff":
        import tifffile
        tifffile.imwrite(tmp_path, picture, photometric="rgb", compression="zlib", metadata=None,
                         software="Seleno")
    elif fmt == "png":
        Image.fromarray(picture).save(tmp_path, "PNG", optimize=False, compress_level=6)
    else:
        Image.fromarray(picture).save(tmp_path, "JPEG", quality=int(spec.get("quality", 95)), subsampling=0)
    os.replace(tmp_path, out_path)
    return {"kind": "render", "format": fmt, "width": out_w, "height": out_h, "bytes": os.path.getsize(out_path)}
