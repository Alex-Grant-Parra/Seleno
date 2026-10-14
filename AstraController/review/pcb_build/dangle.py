"""Remove tracks KiCad reports as dangling (one pass; run until it prints 0)."""
import sys
import pcbnew
path = sys.argv[1]
b = pcbnew.LoadBoard(path)
b.BuildConnectivity()
conn = b.GetConnectivity()
dangling = [t for t in b.GetTracks() if t.Type() in (pcbnew.PCB_TRACE_T, pcbnew.PCB_VIA_T) and conn.TestTrackEndpointDangling(t, False)]
for t in dangling:
    b.Remove(t)
if dangling:
    pcbnew.SaveBoard(path, b)
print(len(dangling))
