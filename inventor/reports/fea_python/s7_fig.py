# Stress colour maps (von Mises envelope over chosen load cases) on the printed parts' surfaces.
# Usage: s7_fig.py GROUP MAT out.png vmax elev azim "lc-prefix1,lc-prefix2" [part1,part2] [zoom box xmin,xmax,ymin,ymax,zmin,zmax]
import numpy as np, sys, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from matplotlib import cm, colors
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HERE

G, MAT, OUT, VMAX, ELEV, AZIM, LCS = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6]), sys.argv[7]
PARTS = sys.argv[8].split(",") if len(sys.argv) > 8 and sys.argv[8] else None
BOX = [float(v) for v in sys.argv[9].split(",")] if len(sys.argv) > 9 else None
R = np.load(os.path.join(HERE, f"res_{G}_{MAT}.npz"))
E = np.load(os.path.join(HERE, f"env_{G}_{MAT}.npz"))
lcn = [str(s) for s in E["lcnames"]]
sel = [i for i, n in enumerate(lcn) if any(n.startswith(p.strip()) for p in LCS.split(","))]
names = [str(n) for n in R["names"]]
fig = plt.figure(figsize=(9, 7.2), dpi=150)
ax = fig.add_subplot(111, projection="3d", proj_type="ortho")
cmap = plt.get_cmap("turbo"); norm = colors.Normalize(0, VMAX)
light = np.array([0.4, 0.8, 0.6]); light /= np.linalg.norm(light)
allp = []
for pid, pn in enumerate(names):
    if PARTS and pn not in PARTS: continue
    X = R[f"p{pid}_xyz"]; T = R[f"p{pid}_tris"]
    vm = np.max([E[f"p{pid}_vm_{i}"] for i in sel], axis=0)
    P = X[T]
    if BOX:
        c = P.mean(1)
        m = (c[:, 0] > BOX[0]) & (c[:, 0] < BOX[1]) & (c[:, 1] > BOX[2]) & (c[:, 1] < BOX[3]) & (c[:, 2] > BOX[4]) & (c[:, 2] < BOX[5])
        P, T = P[m], T[m]
    n = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]); n /= np.linalg.norm(n, axis=1)[:, None] + 1e-12
    shade = 0.55 + 0.45 * np.abs(n @ light)
    col = cmap(norm(vm[T].mean(1)))
    col[:, :3] *= shade[:, None]
    pc = Poly3DCollection(P, facecolors=col, edgecolors="none", linewidths=0)
    ax.add_collection3d(pc)
    allp.append(P.reshape(-1, 3))
A = np.vstack(allp)
lo, hi = A.min(0), A.max(0); mid = (lo + hi) / 2; r = (hi - lo).max() / 2
ax.set_xlim(mid[0] - r, mid[0] + r); ax.set_ylim(mid[1] - r, mid[1] + r); ax.set_zlim(mid[2] - r, mid[2] + r)
ax.set_box_aspect((1, 1, 1))
ax.view_init(elev=ELEV, azim=AZIM, vertical_axis="y")
ax.set_axis_off()
sm = cm.ScalarMappable(norm=norm, cmap=cmap)
cb = fig.colorbar(sm, ax=ax, shrink=0.6, pad=0.02)
cb.set_label(f"von Mises stress, MPa ({MAT}); red and above = {VMAX:g}+")
plt.subplots_adjust(left=0, right=0.95, top=1, bottom=0)
plt.savefig(OUT, dpi=150, facecolor="white")
print("wrote", OUT)
