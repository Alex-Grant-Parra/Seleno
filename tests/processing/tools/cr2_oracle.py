"""rawpy's (LibRaw's) view of a CR2, for checking static/js/processing/cr2.js.

    python cr2_oracle.py <file.cr2> <out dir> [<dir with rawpy>]

Writes raw.u16 (the whole sensor, little-endian) and info.json (sizes, margins,
pattern, black and white levels, white balances, matrix). cr2_check.mjs compares.
"""
import json
import sys

if len(sys.argv) > 3:
    sys.path.insert(0, sys.argv[3])
import rawpy  # noqa: E402

path, out = sys.argv[1], sys.argv[2]
with rawpy.imread(path) as r:
    raw = r.raw_image.copy()
    s = r.sizes
    info = dict(raw_shape=list(raw.shape), top=s.top_margin, left=s.left_margin, height=s.height, width=s.width,
                desc=r.color_desc.decode(), colors=r.raw_colors_visible[:2, :2].tolist(),
                black=list(map(int, r.black_level_per_channel)), white=int(r.white_level),
                daylight=list(map(float, r.daylight_whitebalance)), camera_wb=list(map(float, r.camera_whitebalance)),
                rgb_xyz=r.rgb_xyz_matrix.tolist())
raw.astype("<u2").tofile(out + "/raw.u16")
with open(out + "/info.json", "w") as f:
    json.dump(info, f, indent=1)
print(json.dumps(info))
