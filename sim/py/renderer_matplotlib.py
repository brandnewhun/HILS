"""
MatplotlibRenderer -- RendererBase의 참고용(reference) 구현체.

Three.js 버전(quadrotor_hud.html) 수준의 3D 렌더링이 아니라, 지형/도로/건물/기체 위치를
위에서 내려다보는 단순한 2D 탑다운 그림이다. 목적은 두 가지뿐이다:
    1) world_model/vehicle/flight_sim/target_drone 로직이 실제로 잘 도는지 눈으로 확인.
    2) RendererBase 계약을 실제로 구현한 예시를 하나 남겨서, 나중에 Isaac Sim/Unreal용
       렌더러를 만들 때 "이 계약을 이렇게 채우면 되는구나"를 참고할 수 있게 함.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from renderer_base import RendererBase
from world_model import WorldModel
from vehicle import Vehicle

# 시간대 프리셋 -- Three.js 버전의 SKY_PRESETS와 동일한 5종. 2D 탑다운 뷰라 "하늘"은
# 없으므로, 배경/지도 밖 여백 색만 바꿔서 시간대 분위기를 낸다.
SKY_PRESETS = {
    "dawn":    {"bg": "#2a3550", "fg": "#c7d2e6"},
    "morning": {"bg": "#5f8fc4", "fg": "#0c1c2e"},
    "noon":    {"bg": "#eaf3fb", "fg": "#0c1c2e"},
    "sunset":  {"bg": "#c06a45", "fg": "#2a140a"},
    "night":   {"bg": "#0a0e14", "fg": "#5eead4"},
}

_TERRAIN_CMAP = "YlOrBr"  # 흙색 계열 -- HTML 버전 v13에서 채택한 "연한 갈색" 팔레트와 맞춤


class MatplotlibRenderer(RendererBase):
    def __init__(self, figsize: tuple[float, float] = (7.5, 7.5)):
        self._figsize = figsize
        self.fig = None
        self.ax = None
        self.world: WorldModel | None = None
        self._craft_patch = None
        self._target_patch = None
        self._hud_text = None
        self._time_key = "noon"

    # ---------------------------------------------------------------- init --
    def init(self, world: WorldModel, vehicle: Vehicle) -> None:
        import matplotlib.pyplot as plt  # noqa: PLC0415 (지연 import -- 헤드리스 환경 배려)
        import matplotlib.patches as patches

        self._plt = plt
        self._patches = patches
        self.world = world

        self.fig, self.ax = plt.subplots(figsize=self._figsize)
        self.ax.set_aspect("equal")
        half = world.half_size
        self.ax.set_xlim(-half * 1.05, half * 1.05)
        self.ax.set_ylim(-half * 1.05, half * 1.05)
        self.ax.set_xlabel("East (m)")
        self.ax.set_ylabel("North (m)")

        self._draw_terrain()
        self._draw_roads()
        self._draw_buildings()

        (craft_poly,) = self.ax.plot([], [], color="#0aa89a", linewidth=1.2, zorder=5)
        self._craft_patch = craft_poly
        (target_poly,) = self.ax.plot([], [], color="#e05b3c", linewidth=1.2, zorder=5)
        self._target_patch = target_poly
        self._target_patch.set_visible(False)

        self._hud_text = self.ax.text(
            0.02, 0.98, "", transform=self.ax.transAxes,
            va="top", ha="left", family="monospace", fontsize=9,
        )
        self.set_time_of_day(self._time_key)
        self.set_vehicle(vehicle)

    def _draw_terrain(self) -> None:
        assert self.world is not None
        n = self.world.grid_n
        half = self.world.half_size
        heights = np.array(self.world.data["heights"]).reshape(n, n)
        # sample_height()와 동일한 인덱싱: k = iz*n + ix, east = (2*ix/segs - 1)*half,
        # north = (2*iz/segs - 1)*half.
        axis = np.linspace(-half, half, n)
        east_grid, north_grid = np.meshgrid(axis, axis)
        self.ax.contourf(east_grid, north_grid, heights, levels=20, cmap=_TERRAIN_CMAP, zorder=0)

    def _draw_roads(self) -> None:
        assert self.world is not None
        for road in self.world.data["roads"]:
            pts = road["pts"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            is_bridge = road.get("bridge", False)
            color = "#3a4148" if road["tier"] == "major" else "#6c766f"
            width = 2.2 if road["tier"] == "major" else 1.0
            self.ax.plot(xs, ys, color=color, linewidth=width,
                         linestyle="--" if is_bridge else "-", zorder=1)

    def _draw_buildings(self) -> None:
        assert self.world is not None
        heights = [b["h"] for b in self.world.data["buildings"]]
        h_min, h_max = min(heights), max(heights)
        for b in self.world.data["buildings"]:
            t = (b["h"] - h_min) / max(1.0, h_max - h_min)
            shade = 0.25 + 0.45 * t  # 낮은 건물은 밝게, 높은 건물은 어둡게
            poly = self._patches.Polygon(
                b["pts"], closed=True,
                facecolor=(shade, shade * 0.95, shade * 0.9),
                edgecolor="#141a1e", linewidth=0.6, zorder=2,
            )
            self.ax.add_patch(poly)

    # ------------------------------------------------------------ contract --
    def update_from_state(self, state: dict[str, Any]) -> None:
        self._craft_patch.set_data(*_heading_triangle(state["east"], state["north"], state["heading"]))
        self._hud_text.set_text(
            f"HDG {math.degrees(state['heading']) % 360:5.1f}  "
            f"ALT {state['alt']:6.1f} m\n"
            f"SPD {math.hypot(state['vN'], state['vE']):5.1f} m/s  "
            f"N {state['north']:7.1f}  E {state['east']:7.1f}"
        )

    def render(self) -> None:
        if self.fig is None:
            return
        self.fig.canvas.draw_idle()
        try:
            self.fig.canvas.flush_events()
        except Exception:
            pass  # 헤드리스(Agg) 백엔드에는 flush_events가 없을 수 있음 -- 무시해도 안전

    def save_frame(self, path: str) -> None:
        """헤드리스 환경(디스플레이 없음)에서 현재 프레임을 PNG로 저장 -- 자동 테스트/CI용."""
        assert self.fig is not None
        self.fig.savefig(path, dpi=110)

    def resize(self) -> None:
        pass  # matplotlib 창 크기는 툴킷이 알아서 처리 -- 특별히 할 일 없음

    def set_time_of_day(self, key: str) -> None:
        preset = SKY_PRESETS.get(key)
        if preset is None or self.fig is None:
            return
        self._time_key = key
        self.fig.patch.set_facecolor(preset["bg"])
        self._hud_text.set_color(preset["fg"])

    def set_vehicle(self, vehicle: Vehicle) -> None:
        self._craft_patch.set_label(vehicle.label)

    def set_target_geometry(self, geometry: dict[str, Any]) -> None:
        pass  # 형상 표현은 현재 삼각형 마커 하나로 통일 -- geometry는 미사용

    def update_target_from_state(self, state: dict[str, Any]) -> None:
        self._target_patch.set_visible(bool(state.get("active", False)))
        if state.get("active", False):
            self._target_patch.set_data(*_heading_triangle(state["east"], state["north"], state["heading"]))


def _heading_triangle(east: float, north: float, heading: float, size: float = 8.0):
    """기체 위치(east,north)에 heading 방향을 가리키는 작은 삼각형 외곽선 좌표를 만든다.
    forward 방향 벡터는 flight_sim/target_drone과 동일하게 (sin(heading), cos(heading))."""
    fwd = (math.sin(heading), math.cos(heading))
    right = (math.cos(heading), -math.sin(heading))
    tip = (east + fwd[0] * size, north + fwd[1] * size)
    left_wing = (east - fwd[0] * size * 0.5 + right[0] * size * 0.5,
                 north - fwd[1] * size * 0.5 + right[1] * size * 0.5)
    right_wing = (east - fwd[0] * size * 0.5 - right[0] * size * 0.5,
                  north - fwd[1] * size * 0.5 - right[1] * size * 0.5)
    xs = [tip[0], left_wing[0], right_wing[0], tip[0]]
    ys = [tip[1], left_wing[1], right_wing[1], tip[1]]
    return xs, ys
