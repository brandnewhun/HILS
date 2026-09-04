"""
TargetDrone -- 추격 훈련용 타겟 기체 (플레이어가 계속 쫓아가며 조종 연습을 하는 대상).

FlightSim은 RC 입력 샘플링까지 포함한 "플레이어 전용" 클래스라, 그대로 두 번째 인스턴스를
만들면 같은 입력을 공유해버려서 플레이어 조종을 덮어쓰게 된다. 타겟은 그 정밀도가 필요
없으므로(등속도 비행이면 충분) FlightSim과 완전히 분리된 단순 운동 모델로 만들었다.
WorldModel(지형/도로/건물 질의)만 공유해서 재사용한다. JS 원본과 동일한 로직.

TARGET PROFILE 계약:
    label                 : 표시용 이름
    geometry              : 렌더러가 그릴 형상 (Vehicle.geometry와 동일 형식)
    cruise_speed          : 자동비행 기본 순항속도(m/s)
    max_speed / min_speed : 등속도 운동의 상/하한(m/s)
    turn_rate_max         : rad/s -- 자동/수동 공통 선회율 상한
    climb_rate_max        : m/s
    ceiling               : m -- 이 타겟의 최대 운용고도
    can_hover             : True=쿼드형(뱅크 없이 제자리 선회 가능),
                             False=고정익형(뱅크턴만 가능, 실속방지 최소속도 min_speed 적용)
    bank_max              : rad -- 고정익 전용 시각적 뱅크각 상한(can_hover=False일 때만 사용)

조종 방식 두 가지:
    mode="auto"   -- 지도(500m) 안에서 무작위로 방위각/고도를 재설정해가며 자율비행(랜덤워크).
    mode="manual" -- set_manual_input()으로 외부에서 주입한 turn/climb/speed로 직접 조종.

JS 원본과의 구조적 차이: manual 모드에서 키보드를 직접 훅킹하지 않고 set_manual_input()으로
입력을 주입받는다 -- 입력 소스(키보드/게임패드/다른 엔진의 자체 입력 시스템)를 갈아끼울 때
이 클래스를 건드릴 필요가 없게 하기 위함.

알려진 단순화(JS 원본과 동일, 나중에 필요하면 이 모듈만 고쳐서 개선 가능):
    - 지형/도로/건물 "지붕" 충돌만 반영한다. 플레이어처럼 건물 옆면에 부딪혀 정지하는
      수평 충돌은 넣지 않았다 -- 낮은 고도로 접근하면 지붕 위로 자연스럽게 "떠오르는"
      정도로만 처리된다.
    - 경계(500m) 근처에서는 안쪽으로 편향된 방향을 우선 골라 자연스럽게 방향을 틀게 하고,
      그래도 경계에 닿으면 좌표를 강제로 눌러 넣는다.
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable

from world_model import WorldModel

_DEG = math.pi / 180

TARGET_PROFILES: dict[str, dict[str, Any]] = {
    "quad": {
        "label": "쿼드콥터",
        "geometry": {
            "bodySize": {"x": 1.0, "y": 0.32, "z": 1.5}, "hasWings": False,
            "motors": [
                {"x": 0.9, "z": -0.9, "tiltable": False},
                {"x": -0.9, "z": -0.9, "tiltable": False},
                {"x": 0.9, "z": 0.9, "tiltable": False},
                {"x": -0.9, "z": 0.9, "tiltable": False},
            ],
        },
        "cruiseSpeed": 15, "maxSpeed": 50, "minSpeed": 0,
        "turnRateMax": 90 * _DEG, "climbRateMax": 5, "ceiling": 150,
        "canHover": True, "bankMax": 0.0,
    },
    "fixedwing": {
        "label": "고정익",
        "geometry": {
            "bodySize": {"x": 0.9, "y": 0.34, "z": 2.2}, "hasWings": True,
            "wingSpan": 3.2, "wingChord": 0.55,
            "motors": [
                {"x": 1.0, "z": 0.0, "tiltable": False},
                {"x": -1.0, "z": 0.0, "tiltable": False},
            ],
        },
        "cruiseSpeed": 22, "maxSpeed": 50, "minSpeed": 12,
        "turnRateMax": 30 * _DEG, "climbRateMax": 4, "ceiling": 150,
        "canHover": False, "bankMax": 45 * _DEG,
    },
}


def _wrap_pi(a: float) -> float:
    two_pi = math.pi * 2
    return ((a + math.pi) % two_pi + two_pi) % two_pi - math.pi


class TargetDrone:
    def __init__(self, world: WorldModel):
        self.world = world
        self.profile_key = "quad"
        self.mode = "auto"  # "auto" | "manual"
        self.on_profile_change: Callable[[dict[str, Any]], None] | None = None

        self.state: dict[str, Any] = {
            "active": True,          # False면 렌더러가 메시를 숨긴다
            "east": 0.0, "north": 0.0, "alt": 40.0, "heading": 0.0,
            "speed": 0.0, "bank": 0.0,  # bank는 고정익 전용 시각 효과(쿼드는 항상 0)
            "tilt": 0.0,               # 현재 두 프로필 다 사용 안 하지만 렌더러 계약과 맞추기 위해 유지
        }
        # 자동비행(랜덤워크)용 내부 목표값 -- 일정 시간마다 새로 뽑는다.
        self._auto = {"headingTarget": 0.0, "altTarget": 40.0, "retargetTimer": 0.0}
        self._turn_cmd_norm = 0.0  # -1..1, "지금 얼마나 세게 선회 중인가" -- 뱅크 시각화용
        self._manual_input = {"turn": 0.0, "climb": 0.0, "speed": 0.0}

    @property
    def profile(self) -> dict[str, Any]:
        return TARGET_PROFILES[self.profile_key]

    def set_manual_input(self, turn: float = 0.0, climb: float = 0.0, speed: float = 0.0) -> None:
        """manual 모드일 때 매 프레임 반영할 입력 -- turn/climb/speed는 -1..1 정규화 값."""
        self._manual_input = {"turn": turn, "climb": climb, "speed": speed}

    # 무작위 재목표 설정 -- 지도 경계(500m)에 가까울수록 중심 쪽으로 편향된 방향을 골라서
    # 타겟이 경계에 자꾸 갇히지 않고 자연스럽게 안쪽으로 돌아오게 한다.
    def _pick_new_auto_targets(self, p: dict[str, Any]) -> None:
        s = self.state
        dist_from_center = math.hypot(s["east"], s["north"])
        toward_center = math.atan2(-s["east"], -s["north"])
        half = self.world.half_size
        edge_bias = min(1.0, max(0.0, (dist_from_center - half * 0.55) / (half * 0.4)))
        random_heading = random.random() * math.pi * 2 - math.pi
        self._auto["headingTarget"] = _wrap_pi(toward_center * edge_bias + random_heading * (1 - edge_bias))

        floor_now = self.world.floor_height_at(s["east"], s["north"])
        alt_low = max(floor_now + 15, 20)
        alt_high = min(p["ceiling"] - 10, alt_low + 80)
        self._auto["altTarget"] = alt_low + random.random() * max(5, alt_high - alt_low)
        self._auto["retargetTimer"] = 4 + random.random() * 6  # 4~10초마다 재설정

    def _step_auto(self, p: dict[str, Any], dt: float) -> None:
        self._auto["retargetTimer"] -= dt
        if self._auto["retargetTimer"] <= 0:
            self._pick_new_auto_targets(p)

        s = self.state
        diff = _wrap_pi(self._auto["headingTarget"] - s["heading"])
        max_step = p["turnRateMax"] * dt
        applied = max(-max_step, min(max_step, diff))
        s["heading"] = _wrap_pi(s["heading"] + applied)
        self._turn_cmd_norm = applied / max_step if max_step > 0 else 0.0

        alt_diff = self._auto["altTarget"] - s["alt"]
        alt_step = p["climbRateMax"] * dt
        if abs(alt_diff) <= alt_step:
            s["alt"] = self._auto["altTarget"]
        else:
            s["alt"] += math.copysign(alt_step, alt_diff)

        s["speed"] += (p["cruiseSpeed"] - s["speed"]) * min(1.0, dt / 1.0)

    def _step_manual(self, p: dict[str, Any], dt: float) -> None:
        s = self.state
        turn_in = self._manual_input["turn"]
        climb_in = self._manual_input["climb"]
        spd_in = self._manual_input["speed"]

        self._turn_cmd_norm = turn_in
        s["heading"] = _wrap_pi(s["heading"] + turn_in * p["turnRateMax"] * dt)
        s["alt"] += climb_in * p["climbRateMax"] * dt
        s["speed"] += spd_in * (p["maxSpeed"] - p["minSpeed"]) * 0.4 * dt

    def step(self, dt: float) -> None:
        if not self.state["active"]:
            return
        p = self.profile
        if self.mode == "auto":
            self._step_auto(p, dt)
        else:
            self._step_manual(p, dt)

        s = self.state
        s["speed"] = max(p["minSpeed"], min(p["maxSpeed"], s["speed"]))
        s["bank"] = 0.0 if p["canHover"] else max(-p["bankMax"], min(p["bankMax"], self._turn_cmd_norm * p["bankMax"]))

        # 등속도 운동: 매 프레임 heading 방향으로 speed만큼 전진(가감속 모델 없음).
        s["east"] += math.sin(s["heading"]) * s["speed"] * dt
        s["north"] += math.cos(s["heading"]) * s["speed"] * dt

        # 경계(500m) 안전장치 -- auto 모드는 위 edge_bias 조향으로 대부분 걸리지 않지만,
        # manual 조종이나 극단적인 경우를 대비해 최후 수단으로 좌표를 눌러 넣는다.
        b = self.world.half_size
        s["east"] = max(-b, min(b, s["east"]))
        s["north"] = max(-b, min(b, s["north"]))

        # 지형/도로/건물 지붕 중 가장 높은 면(WorldModel 공용 질의) 아래로는 내려가지 않는다.
        floor_here = self.world.floor_height_at(s["east"], s["north"])
        if s["alt"] < floor_here:
            s["alt"] = floor_here
        if s["alt"] > p["ceiling"]:
            s["alt"] = p["ceiling"]

    def set_profile(self, key: str) -> None:
        if key not in TARGET_PROFILES or key == self.profile_key:
            return
        self.profile_key = key
        self.state["bank"] = 0.0
        self._turn_cmd_norm = 0.0
        # 형상만 바뀌고 위치/고도/속도는 그대로 유지 -- 비행 중 형상을 바꿔도 자연스럽게 이어진다.
        if self.on_profile_change:
            self.on_profile_change(TARGET_PROFILES[key])

    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "manual"):
            return
        self.mode = mode
        if mode == "auto":
            self._pick_new_auto_targets(self.profile)

    def respawn(self, player_state: dict[str, Any] | None = None) -> None:
        """지도 안 임의 위치로 재배치. player_state(FlightSim.state)를 넘기면 플레이어와
        최소 60m 거리를 두어 바로 코앞에 나타나지 않게 한다(생략 가능 -- 완전 무작위 배치)."""
        p = self.profile
        half = self.world.half_size
        e = n = 0.0
        tries = 0
        while True:
            e = (random.random() * 2 - 1) * half * 0.8
            n = (random.random() * 2 - 1) * half * 0.8
            tries += 1
            if player_state is None:
                break
            if math.hypot(e - player_state["east"], n - player_state["north"]) >= 60 or tries >= 20:
                break

        s = self.state
        s["east"], s["north"] = e, n
        s["heading"] = random.random() * math.pi * 2 - math.pi
        s["alt"] = max(self.world.floor_height_at(e, n) + 30, 30)
        s["speed"] = p["cruiseSpeed"]
        s["bank"] = 0.0
        self._turn_cmd_norm = 0.0
        s["active"] = True
        self._pick_new_auto_targets(p)
