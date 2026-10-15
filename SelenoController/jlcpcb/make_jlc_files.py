"""Generate the JLCPCB assembly BOM and CPL for SELENO.kicad_pcb.

Run with KiCad's python (needs pcbnew):
  "C:/Users/alexg/AppData/Local/Programs/KiCad/10.0/bin/python.exe" -I make_jlc_files.py

Only the parts in ASSEMBLE (Alex's JLC order list, Parts.txt) go into the files;
everything else on the board (C1, LEDs, ESP32, R25-R28, connectors, test pads)
is hand-soldered and left out of both the BOM and the CPL.

Rotation: JLC places with its own (EasyEDA/LCSC) footprints. ROT_CORR was found
by matching pad numbers between each KiCad footprint and the LCSC footprint of
the ordered part (2026-10-15). Pad centres coincide for all parts, so no X/Y
offsets are needed. Re-check ROT_CORR if a part number or footprint changes.
"""
import csv, os, re, sys
import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD = os.path.join(HERE, "..", "SELENO.kicad_pcb")
BOM_OUT = os.path.join(HERE, "SELENO_v0.8_BOM_JLCPCB.csv")
CPL_OUT = os.path.join(HERE, "SELENO_v0.8_CPL_JLCPCB.csv")

# LCSC part, BOM comment, JLC package, board value must start with, refs
ASSEMBLE = [
    ("C307331", "100nF 50V X7R", "0402", "100nF",
     "C2 C3 C4 C6 C11 C12 C16 C26 C29 C32 C54"),
    ("C1532", "22nF 50V X7R", "0402", "22nF", "C28 C30"),
    ("C15195", "10nF 50V X7R", "0402", "10nF", "C36"),
    ("C1523", "1nF 50V X7R", "0402", "1nF", "C52"),
    ("C1546", "100pF 50V C0G", "0402", "100pF", "C53"),
    ("C52923", "1uF 25V X5R", "0402", "1uF", "C7"),
    ("C23630", "2.2uF 16V X5R", "0603", "2.2uF", "C27 C31"),
    ("C15850", "10uF 25V X5R", "0805", "10uF 25V", "C15 C33 C50"),
    ("C45783", "22uF 25V X5R", "0805", "22uF", "C37 C38 C43 C47 C48 C49 C51"),
    ("C13585", "10uF 50V X5R", "1206", "10uF 50V", "C35"),
    ("C25744", "10k 1%", "0402", "10k", "R6 R19 R21 R22 R23 R35 R36 R38"),
    ("C25104", "330R 1%", "0402", "330R", "R1 R2 R3 R4 R5 R39"),
    ("C25900", "4.7k 1%", "0402", "4.7k", "R7 R8 R30"),
    ("C25756", "15k 1%", "0402", "15k", "R29"),
    ("C11702", "1k 1%", "0402", "1k", "R16"),
    ("C19077408", "BZT52C10", "SOD-123", "BZT52C10", "D6"),
    ("C19077535", "SMAJ15A", "SMA(DO-214AC)", "SMAJ15A", "D7"),
    ("C8678", "SS34", "SMA(DO-214AC)", "SS34", "D8"),
    ("C207089", "2920L300/15DR", "2920", "PTC 3A", "F1"),
    ("C5298372", "FXL0840-150-M 15uH", "SMD 8.8x8.2mm", "15uH", "L2"),
    ("C15127", "AO3401A", "SOT-23", "AO3401A", "Q1"),
    ("C20526", "MMBT3904", "SOT-23", "MMBT3904", "Q2 Q3"),
    ("C639770", "MCP23017-E/ML", "QFN-28-EP(6x6)", "MCP23017", "U1"),
    ("C9864", "TPS5430DDAR", "SOIC-8-EP", "TPS5430", "U6"),
    ("C2150710", "TMC2209-LA-T", "QFN-28-EP(5x5)", "TMC2209", "U7 U8"),
    ("C6186", "AMS1117-3.3", "SOT-223", "AMS1117-3.3", "U9"),
]

# degrees CCW added to the KiCad rotation, per LCSC part
ROT_CORR = {"C15127": 180, "C20526": 180, "C6186": 180,
            "C639770": 270, "C2150710": 270}


def natkey(ref):
    m = re.match(r"([A-Z]+)(\d+)", ref)
    return (m.group(1), int(m.group(2)))


board = pcbnew.LoadBoard(BOARD)
fps = {f.GetReference(): f for f in board.GetFootprints()}

bom_rows, cpl_rows, placed, errors = [], [], set(), []
for lcsc, comment, pkg, val, refs in ASSEMBLE:
    refs = sorted(refs.split(), key=natkey)
    for ref in refs:
        fp = fps.get(ref)
        if fp is None:
            errors.append(f"{ref}: not on the board")
            continue
        if not fp.GetValue().startswith(val):
            errors.append(f"{ref}: board value '{fp.GetValue()}' != '{val}'")
        if fp.IsFlipped():
            errors.append(f"{ref}: on the bottom side")
        pos = fp.GetPosition()
        rot = (fp.GetOrientationDegrees() + ROT_CORR.get(lcsc, 0)) % 360
        cpl_rows.append([ref, f"{pcbnew.ToMM(pos.x):.4f}",
                         f"{-pcbnew.ToMM(pos.y):.4f}", "Top", f"{rot:g}"])
        placed.add(ref)
    bom_rows.append([comment, ",".join(refs), pkg, lcsc])

if errors:
    sys.exit("ERRORS:\n  " + "\n  ".join(errors))

with open(BOM_OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(["Comment", "Designator", "Footprint", "JLCPCB Part #"])
    w.writerows(bom_rows)

with open(CPL_OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL)
    w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
    w.writerows(sorted(cpl_rows, key=lambda r: natkey(r[0])))

skipped = sorted((r for r, f in fps.items()
                  if r not in placed and not r.startswith(("TP", "H"))), key=natkey)
print(f"BOM: {len(bom_rows)} lines, {len(placed)} parts -> {BOM_OUT}")
print(f"CPL: {len(cpl_rows)} placements -> {CPL_OUT}")
print("Not assembled by JLC:", " ".join(skipped))
