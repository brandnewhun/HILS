"""
WorldModel -- 지형/도로/건물 데이터 및 충돌 질의 (렌더러/엔진 독립적).

HILS_ICD/sim/quadrotor_hud.html 의 WorldModel IIFE를 그대로 포팅한 것. 좌표계는
JS 원본과 동일하게 X=East, Z=-North, Y=고도(기준점 대비 상대값)이며, 이 모듈은
Three.js/matplotlib/Isaac Sim/Unreal 중 무엇을 렌더러로 쓰든 재사용 가능해야 하므로
어떤 렌더링 라이브러리도 import하지 않는다.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

ROAD_WIDTH = {"major": 5.0, "minor": 2.4}
BRIDGE_CLEARANCE = 5.5  # 실제 고가도로 교량 하부 여유고(桁下高) 근사치, m

# 실제 고가도로(overpass) 구간 표시 -- Overpass 원본(_terrain_raw/overpass_result.json)에서
# 이 도로가 bridge=yes, bridge:name=신갈제1고가차도 태그를 갖고 있음을 좌표 대조로 확인했다.
# 처리 과정에서 태그 자체는 버려졌기 때문에, 박스 안에서 유일하게 일치하는 좌표 시그니처로
# 다시 식별한다 (JS WorldModel과 동일한 방식).
KNOWN_BRIDGE_SIGNATURES = [
    {"first_pt": (73.9, -209.4), "count": 9},
]


class WorldModel:
    def __init__(self, terrain_data: dict[str, Any]):
        self.data = terrain_data
        meta = terrain_data["meta"]
        self.grid_n: int = meta["gridN"]
        self.half_size: float = meta["halfSizeM"]
        self.segs: int = self.grid_n - 1
        self.size: float = self.half_size * 2
        self.min_height: float = min(terrain_data["heights"])
        self.max_height: float = max(terrain_data["heights"])
        self.road_width = dict(ROAD_WIDTH)
        self.bridge_clearance = BRIDGE_CLEARANCE

        self._tag_bridges()
        self._precompute_buildings()
        self._road_colliders = self._build_road_colliders()

    @classmethod
    def from_json_file(cls, path: str | Path) -> "WorldModel":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def _tag_bridges(self) -> None:
        for road in self.data["roads"]:
            pts = road["pts"]
            if not pts:
                continue
            p0 = pts[0]
            for sig in KNOWN_BRIDGE_SIGNATURES:
                if (len(pts) == sig["count"]
                        and abs(p0[0] - sig["first_pt"][0]) < 0.5
                        and abs(p0[1] - sig["first_pt"][1]) < 0.5):
                    road["bridge"] = True

    # 배경 좌표계는 물리 시뮬레이션의 NED와 동일하게 X=East, Z=-North, Y=고도(기준점 대비 상대값).
    def sample_height(self, world_x: float, world_z: float) -> float:
        north = -world_z
        n, half, segs = self.grid_n, self.half_size, self.segs
        fx = min(1.0, max(0.0, (world_x / half + 1) / 2))
        fz = min(1.0, max(0.0, (north / half + 1) / 2))
        gx, gz = fx * segs, fz * segs
        ix0, iz0 = int(math.floor(gx)), int(math.floor(gz))
        ix1, iz1 = min(ix0 + 1, segs), min(iz0 + 1, segs)
        tx, tz = gx - ix0, gz - iz0
        heights = self.data["heights"]
        h00, h10 = heights[iz0 * n + ix0], heights[iz0 * n + ix1]
        h01, h11 = heights[iz1 * n + ix0], heights[iz1 * n + ix1]
        hx0 = h00 * (1 - tx) + h10 * tx
        hx1 = h01 * (1 - tx) + h11 * tx
        return hx0 * (1 - tz) + hx1 * tz

    @staticmethod
    def point_in_polygon(pe: float, pn: float, poly: list[list[float]]) -> bool:
        inside = False
        j = len(poly) - 1
        for i, (ei, ni) in enumerate(poly):
            ej, nj = poly[j]
            if (ni > pn) != (nj > pn):
                if pe < (ej - ei) * (pn - ni) / (nj - ni) + ei:
                    inside = not inside
            j = i
        return inside

    # 건물 기준고도(baseY)를 여기서 한 번만 계산해 building dict에 붙여둔다 -- 렌더러(메시
    # 배치)와 물리(지붕 충돌)가 항상 같은 값을 쓰도록 보장하기 위함.
    def _precompute_buildings(self) -> None:
        for b in self.data["buildings"]:
            pts = b["pts"]
            cx = sum(p[0] for p in pts) / len(pts)
            cn = sum(p[1] for p in pts) / len(pts)
            b["centroidEast"] = cx
            b["centroidNorth"] = cn
            b["baseY"] = self.sample_height(cx, -cn)

            # 렌더링용 바닥은 건물 외곽선을 따라 샘플링한 최저 지형보다 살짝 아래로 둔다
            # (JS WorldModel의 renderBaseY와 동일 로직). baseY 자체는 충돌/지붕 계산용으로 유지.
            min_ground = b["baseY"]
            n = len(pts)
            for i in range(n):
                p, nxt = pts[i], pts[(i + 1) % n]
                t = 0.0
                while t <= 1.0 + 1e-9:
                    e = p[0] + (nxt[0] - p[0]) * t
                    nn = p[1] + (nxt[1] - p[1]) * t
                    min_ground = min(min_ground, self.sample_height(e, -nn))
                    t += 0.25
            b["renderBaseY"] = min_ground - 0.15

    def building_roof_at(self, east: float, north: float) -> float | None:
        roof = None
        for b in self.data["buildings"]:
            if self.point_in_polygon(east, north, b["pts"]):
                top_y = b["baseY"] + b["h"]
                if roof is None or top_y > roof:
                    roof = top_y
        return roof

    def _build_road_colliders(self) -> list[dict[str, float]]:
        colliders: list[dict[str, float]] = []
        for road in self.data["roads"]:
            half_w = (self.road_width["major"] if road["tier"] == "major" else self.road_width["minor"]) / 2
            pts = road["pts"]
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                de, dn = b[0] - a[0], b[1] - a[1]
                length = math.hypot(de, dn)
                if length < 0.01:
                    continue
                cx, cn = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                colliders.append({
                    "cx": cx, "cn": cn, "half_len": length / 2, "half_w": half_w,
                    "angle": math.atan2(dn, de),
                    "surface_y": self.sample_height(cx, -cn) + 0.25,
                })
        return colliders

    def road_surface_at(self, east: float, north: float) -> float | None:
        top = None
        for rc in self._road_colliders:
            dx, dn = east - rc["cx"], north - rc["cn"]
            ca, sa = math.cos(rc["angle"]), math.sin(rc["angle"])
            along = dx * ca + dn * sa
            across = -dx * sa + dn * ca
            if abs(along) <= rc["half_len"] and abs(across) <= rc["half_w"]:
                if top is None or rc["surface_y"] > top:
                    top = rc["surface_y"]
        return top

    # 해당 지점에서 기체가 내려갈 수 있는 최저 고도(지형/도로/지붕 중 가장 높은 것).
    # 고가도로(road.bridge)는 렌더러가 상판을 띄워서 "그리는" 것과 별개로 여기서는 여전히
    # 자연 지형고도를 floor로 쓴다 -- 실제 고가도로처럼 그 아래 공간을 그대로 비행 가능하게
    # 두기 위한 의도적 단순화(JS 원본과 동일한 트레이드오프).
    def floor_height_at(self, east: float, north: float) -> float:
        floor = self.sample_height(east, -north)
        road = self.road_surface_at(east, north)
        if road is not None and road > floor:
            floor = road
        roof = self.building_roof_at(east, north)
        if roof is not None and roof > floor:
            floor = roof
        return floor
