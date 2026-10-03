"""Kiana v2.13.0.0 六维雷达评估图（对比 v2.10.5 接手时基线）— 标准写法"""
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

dims = ["采集功能", "隐私防护", "反反爬", "稳定韧性", "性能效能", "易用交付"]
old = [8.5, 6.0, 5.0, 6.5, 6.0, 7.0]
new = [9.5, 9.2, 8.5, 9.5, 8.2, 9.3]

N = len(dims)
theta = np.linspace(0, 2 * np.pi, N, endpoint=False)

fig = plt.figure(figsize=(9, 8), facecolor="#12151c")
ax = fig.add_subplot(111, polar=True)
ax.set_facecolor("#161a22")
ax.set_theta_offset(np.pi / 2)   # 采集功能在正上
ax.set_theta_direction(-1)

o = np.array(old + old[:1])
n = np.array(new + new[:1])
tt = np.append(theta, theta[0])

ax.plot(tt, o, color="#7a8494", linewidth=1.6, linestyle="--", label="接手时 v2.10.5")
ax.fill(tt, o, color="#7a8494", alpha=0.12)
ax.plot(tt, n, color="#4FA3E8", linewidth=2.4, label="现状 v2.13.0.0")
ax.fill(tt, n, color="#4FA3E8", alpha=0.28)

for ang, v in zip(theta, new):
    ax.text(ang, v + 0.55, f"{v}", color="#8FC7F5", fontsize=13, fontweight="bold", ha="center")
for ang, v in zip(theta, old):
    ax.text(ang, v - 0.55, f"{v}", color="#9aa4b2", fontsize=9.5, ha="center")

ax.set_xticks(theta)
ax.set_xticklabels(dims, color="#E6EDF3", fontsize=14, fontweight="bold")
ax.set_ylim(0, 10)
ax.set_yticks([2, 4, 6, 8, 10])
ax.set_yticklabels(["2", "4", "6", "8", "10"], color="#5a6472", fontsize=9)
ax.grid(color="#2a313c", linewidth=0.8)
ax.spines["polar"].set_color("#2a313c")

ax.set_title("Kiana 六维战力评估  ·  v2.10.5 接手 → v2.13.0.0",
             color="#E6EDF3", fontsize=16, fontweight="bold", pad=28)
ax.legend(loc="lower center", bbox_to_anchor=(0.5, -0.14), ncol=2,
          frameon=False, fontsize=12, labelcolor="#C9D1D9")

out = str(Path(__file__).resolve().parent.parent / "tests" / "assets" / "radar_v213.png")
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print("SAVED:", out)
