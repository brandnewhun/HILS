"""
FlightSim -- 공통 비행 루프: RC 입력(sample & hold) -> Vehicle.compute_dynamics() 호출 ->
위치 적분 -> 지형/건물/경계 충돌 -> 고도 제한. 기체가 바뀌어도 이 클래스는 그대로 재사용된다.

JS 원본(quadrotor_hud.html)과의 유일한 구조적 차이: JS는 FlightSim이 window의 keydown/
keyup을 직접 훅킹했지만, 여기서는 set_rc_input()으로 외부(키보드 어댑터/게임패드/
Isaac Sim이나 Unreal의 자체 입력 시스템)가 넣어주는 값을 그대로 sample & hold 한다.
입력 소스를 무엇으로 바꾸든 이 클래스는 손댈 필요가 없다.
"""
from __future__ import annotations

from typing import Any, Callable

from world_model import WorldModel
from vehicle import Vehicle

# ICD-PXTR-HILS-001, 9.4절 통합 파라미터 목록의 전송 주기(Hz)를 그대로 사용.
ICD_RATE = {
    "D01_RC": 10,    # D-01 RC_CHANNELS (조종기->FCC, 검증용)
    "C01_POS": 20,   # C-01 GLOBAL_POSITION_INT (중간값, 10~30Hz)
    "C02_ATT": 60,   # C-02 ATTITUDE_QUATERNION (중간값, 50~100Hz)
    "C03_SPD": 15,   # C-03 VFR_HUD airspeed (중간값, 10~20Hz)
    "C04_HDG": 15,   # C-04 VFR_HUD heading (중간값, 10~20Hz)
    "C06_TILT": 20,  # C-06 HIL_TILT_STATE 재송출
}

_ZERO_RC = {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "thr": 0.0, "tilt": 0.0}


class FlightSim:
    def __init__(self, world: WorldModel):
        self.world = world
        self.map_bound = world.half_size
        self.icd_rate = dict(ICD_RATE)

        self.state: dict[str, Any] = {}
        self.rc_cmd: dict[str, float] = dict(_ZERO_RC)
        self._pending_rc: dict[str, float] = dict(_ZERO_RC)
        self._rc_accum = 0.0

        self.vehicle: Vehicle | None = None
        self.on_rc_sample: Callable[[], None] | None = None
        self.on_reset: Callable[[], None] | None = None

    # 채널 D(D-01, 조종기->FCC): 실제 입력은 매 프레임 갱신되지만, ICD 상 RC_CHANNELS 전송
    # 주기(10Hz)로만 FCC가 새 값을 수신한다고 가정한다 -- 다음 샘플링 시점까지는 이 값이
    # 그대로 유지(sample & hold)된다.
    def set_rc_input(self, pitch: float = 0.0, roll: float = 0.0, yaw: float = 0.0,
                      thr: float = 0.0, tilt: float = 0.0) -> None:
        self._pending_rc = {"pitch": pitch, "roll": roll, "yaw": yaw, "thr": thr, "tilt": tilt}

    def _apply_vehicle_initial_state(self) -> None:
        assert self.vehicle is not None
        self.state = {
            "roll": 0.0, "pitch": 0.0, "yawRate": 0.0, "heading": 0.0,
            "vN": 0.0, "vE": 0.0, "north": 0.0, "east": 0.0,
            "climbRate": 0.0, "alt": 0.0,
        }
        self.state.update(self.vehicle.initial_extra_state())

    def set_vehicle(self, vehicle: Vehicle) -> None:
        self.vehicle = vehicle
        self._apply_vehicle_initial_state()
        self.rc_cmd = dict(_ZERO_RC)
        self._pending_rc = dict(_ZERO_RC)
        self._rc_accum = 0.0

    def reset_state(self) -> None:
        self._apply_vehicle_initial_state()
        self.rc_cmd = dict(_ZERO_RC)
        self._pending_rc = dict(_ZERO_RC)
        self._rc_accum = 0.0
        if self.on_reset:
            self.on_reset()

    def _sample_rc_channel(self, dt: float) -> None:
        self._rc_accum += dt
        interval = 1.0 / self.icd_rate["D01_RC"]
        if self._rc_accum >= interval:
            self._rc_accum -= interval
            self.rc_cmd = dict(self._pending_rc)
            if self.on_rc_sample:
                self.on_rc_sample()

    def step(self, dt: float) -> None:
        assert self.vehicle is not None
        s = self.state
        self._sample_rc_channel(dt)
        self.vehicle.compute_dynamics(s, self.rc_cmd, dt)

        prev_north, prev_east = s["north"], s["east"]
        s["north"] += s["vN"] * dt
        s["east"] += s["vE"] * dt

        # 맵 범위(500m x 500m) 경계 -- 벗어나려는 방향의 속도 성분만 제거해 그 자리에서 막는다.
        b = self.map_bound
        if s["east"] > b:
            s["east"] = b
            if s["vE"] > 0: s["vE"] = 0.0
        if s["east"] < -b:
            s["east"] = -b
            if s["vE"] < 0: s["vE"] = 0.0
        if s["north"] > b:
            s["north"] = b
            if s["vN"] > 0: s["vN"] = 0.0
        if s["north"] < -b:
            s["north"] = -b
            if s["vN"] < 0: s["vN"] = 0.0

        s["alt"] += s["climbRate"] * dt

        # 지형/도로/건물 지붕 중 가장 높은 면 = 이 위치의 "바닥". 현재 고도가 그 바닥보다
        # 낮은 곳으로 수평 이동하면(완만한 경사라도) 지형/건물 옆면에 부딪힌 것으로 보고
        # 수평 이동을 취소(직전 위치로 복귀, 속도 0)한다.
        floor_at_new = self.world.floor_height_at(s["east"], s["north"])
        if floor_at_new > s["alt"] + 1e-3:
            s["north"], s["east"] = prev_north, prev_east
            s["vN"], s["vE"] = 0.0, 0.0
        elif s["alt"] < floor_at_new:
            s["alt"] = floor_at_new
            if s["climbRate"] < 0:
                s["climbRate"] = 0.0

        max_alt = self.vehicle.max_alt or 200
        if s["alt"] > max_alt:
            s["alt"] = max_alt
            if s["climbRate"] > 0:
                s["climbRate"] = 0.0
