"""
Vehicle 계약 -- 기체(OFP) 모듈. 자세/추력/틸트 등 "이 기체가 어떻게 나는가"만 담당한다.
위치 적분과 지형/건물 충돌은 FlightSim이 공통 처리하므로 이 모듈은 손대지 않는다.

VEHICLE 계약 (JS 원본과 동일):
    id, label            : 식별자 / 표시용 이름
    max_alt              : 이 기체의 최대 운용고도(m)
    geometry             : 렌더러가 형상을 그릴 때 참고하는 선언적 설명(렌더 엔진 비의존적)
                            {bodySize:{x,y,z}, hasWings, wingSpan, wingChord,
                             motors:[{x,z,tiltable}, ...]}
    control_hint         : 범례에 추가로 표시할 조작법 문자열 (없으면 None)
    initial_extra_state() : 기체 고유 상태 필드의 초기값 dict (예: 틸트로터의 {"tilt":0})
    compute_dynamics(state, rc_cmd, dt)
        rc_cmd = {"pitch","roll","yaw","thr","tilt"} (-1..1 정규화 입력)을 받아
        state["pitch"/"roll"/"yawRate"/"heading"/"vN"/"vE"/"climbRate"(+기체 고유 필드)를
        in-place로 갱신한다.

새 기체(예: 별도 고정익)를 추가하려면 create_xxx_vehicle() 팩토리 함수 하나를 이 계약대로
작성하면 된다. FlightSim/렌더러는 손댈 필요 없다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Vehicle:
    id: str
    label: str
    max_alt: float
    geometry: dict[str, Any]
    compute_dynamics: Callable[[dict[str, Any], dict[str, float], float], None]
    initial_extra_state: Callable[[], dict[str, Any]] = field(default=lambda: {})
    control_hint: str | None = None


def _lerp1(cur: float, target: float, dt: float, tau: float) -> float:
    return cur + (target - cur) * min(1.0, dt / tau)


def _wrap_2pi(angle: float) -> float:
    two_pi = math.pi * 2
    return (angle % two_pi + two_pi) % two_pi


# ==============================================================================
# VEHICLE 모듈 #1: 쿼드콥터 (MC)
# ==============================================================================
def create_quadrotor_vehicle() -> Vehicle:
    g = 9.81
    max_tilt = math.radians(35)
    att_tau = 0.15
    yaw_rate_max = math.radians(150)
    yaw_tau = 0.2
    climb_up_max = 3.0
    climb_dn_max = 1.5
    z_tau = 0.2
    xy_vel_max = 12.0
    drag_k = (g * math.tan(max_tilt)) / xy_vel_max

    def compute_dynamics(state: dict[str, Any], rc: dict[str, float], dt: float) -> None:
        pitch_target = rc["pitch"] * max_tilt
        roll_target = rc["roll"] * max_tilt
        yaw_rate_target = rc["yaw"] * yaw_rate_max
        climb_target = rc["thr"] * climb_up_max if rc["thr"] > 0 else rc["thr"] * climb_dn_max

        state["pitch"] = _lerp1(state["pitch"], pitch_target, dt, att_tau)
        state["roll"] = _lerp1(state["roll"], roll_target, dt, att_tau)
        state["yawRate"] = _lerp1(state["yawRate"], yaw_rate_target, dt, yaw_tau)
        state["climbRate"] = _lerp1(state["climbRate"], climb_target, dt, z_tau)

        state["heading"] = _wrap_2pi(state["heading"] + state["yawRate"] * dt)

        a_bx = g * math.tan(state["pitch"])
        a_by = g * math.tan(state["roll"])
        c_h, s_h = math.cos(state["heading"]), math.sin(state["heading"])
        a_n = a_bx * c_h - a_by * s_h
        a_e = a_bx * s_h + a_by * c_h

        state["vN"] += (a_n - drag_k * state["vN"]) * dt
        state["vE"] += (a_e - drag_k * state["vE"]) * dt

        state["tilt"] = 0.0  # 쿼드콥터는 틸트 기구가 없음 -- 항상 수직 로터

    return Vehicle(
        id="quadrotor",
        label="쿼드콥터 (MC)",
        max_alt=200,
        geometry={
            "bodySize": {"x": 1.0, "y": 0.32, "z": 1.5},
            "hasWings": False,
            "motors": [
                {"x": 0.9, "z": -0.9, "tiltable": False},
                {"x": -0.9, "z": -0.9, "tiltable": False},
                {"x": 0.9, "z": 0.9, "tiltable": False},
                {"x": -0.9, "z": 0.9, "tiltable": False},
            ],
        },
        compute_dynamics=compute_dynamics,
        initial_extra_state=lambda: {"tilt": 0.0},
        control_hint=None,
    )


# ==============================================================================
# VEHICLE 모듈 #2: 틸트로터 (VTOL) -- 집단 틸트각(0=수직/MC, 1=수평/FW)에 따라 멀티콥터식
# 벡터추력과 고정익식 추력-양력 모델을 선형 블렌딩하는 단순화 모델. 실제 tv_att_control/
# tv_pos_control 수준의 정밀 전이 모델은 아니고, 구조 검증용 근사치 (JS 원본과 동일).
# ==============================================================================
def create_tiltrotor_vehicle() -> Vehicle:
    g = 9.81
    max_tilt_att = math.radians(30)
    att_tau = 0.18
    yaw_rate_max = math.radians(120)
    yaw_tau = 0.25
    climb_up_max = 4.0
    climb_dn_max = 2.0
    z_tau = 0.25
    xy_vel_max_mc = 10.0
    drag_k_mc = (g * math.tan(max_tilt_att)) / xy_vel_max_mc
    fw_cruise_max = 35.0          # 고정익 모드 최고속도(m/s)
    fw_thrust_accel_max = 6.0     # 고정익 모드 최대 추력가속(m/s^2)
    fw_drag_k = fw_thrust_accel_max / (fw_cruise_max ** 2)
    stall_speed = 8.0              # 이 속도 미만이면 고정익 양력 상실(단순화)
    tilt_tau = 1.2                 # 틸트 액추에이터 응답(실제 서보처럼 느림)
    tilt_rate = 0.6                # Q/E 입력 -> 목표 틸트 변화 속도(1/s, 0..1 스케일)

    def clamp01(v: float) -> float:
        return max(0.0, min(1.0, v))

    def compute_dynamics(state: dict[str, Any], rc: dict[str, float], dt: float) -> None:
        # 집단 틸트각: Q/E로 목표치를 증감시키고, 실제 서보처럼 시간지연을 두고 따라간다.
        state["tiltTarget"] = clamp01(state.get("tiltTarget", 0.0) + rc.get("tilt", 0.0) * dt * tilt_rate)
        state["tilt"] = _lerp1(state.get("tilt", 0.0), state["tiltTarget"], dt, tilt_tau)
        tilt = state["tilt"]

        pitch_target = rc["pitch"] * max_tilt_att * (1 - 0.3 * tilt)
        roll_target = rc["roll"] * max_tilt_att
        yaw_rate_target = rc["yaw"] * yaw_rate_max * (1 - 0.5 * tilt)

        state["pitch"] = _lerp1(state["pitch"], pitch_target, dt, att_tau)
        state["roll"] = _lerp1(state["roll"], roll_target, dt, att_tau)
        state["yawRate"] = _lerp1(state["yawRate"], yaw_rate_target, dt, yaw_tau)
        state["heading"] = _wrap_2pi(state["heading"] + state["yawRate"] * dt)

        speed = math.hypot(state["vN"], state["vE"])

        # MC 성분(tilt=0에서 100%): 자세각 기반 벡터추력 -- 쿼드콥터와 동일 원리
        mc_accel_fwd = g * math.tan(state["pitch"])
        mc_accel_lat = g * math.tan(state["roll"])

        # FW 성분(tilt=1에서 100%): 스로틀=전방추력(속도제곱 항력으로 최고속도 제한), 롤=선회
        thr = rc["thr"] if rc["thr"] > 0 else 0.0  # 고정익 모드는 역추력 없음(단순화)
        fw_accel_fwd = thr * fw_thrust_accel_max - fw_drag_k * speed * speed
        fw_accel_lat = g * math.tan(state["roll"]) * 0.6

        a_bx = mc_accel_fwd * (1 - tilt) + fw_accel_fwd * tilt
        a_by = mc_accel_lat * (1 - tilt) + fw_accel_lat * tilt

        c_h, s_h = math.cos(state["heading"]), math.sin(state["heading"])
        a_n = a_bx * c_h - a_by * s_h
        a_e = a_bx * s_h + a_by * c_h

        drag_k = drag_k_mc * (1 - tilt)  # FW 모드는 위 항력항이 이미 처리하므로 추가 감쇠 불필요
        state["vN"] += (a_n - drag_k * state["vN"]) * dt
        state["vE"] += (a_e - drag_k * state["vE"]) * dt

        # 승강: MC는 스로틀이 곧 상승률, FW는 실속속도 이상에서만 피치로 양력(단순화), 이하는 강하
        mc_climb = rc["thr"] * climb_up_max if rc["thr"] > 0 else rc["thr"] * climb_dn_max
        fw_climb = rc["pitch"] * (speed / 20) * 4.0 if speed > stall_speed else -2.0
        climb_target = mc_climb * (1 - tilt) + fw_climb * tilt
        state["climbRate"] = _lerp1(state["climbRate"], climb_target, dt, z_tau)

    return Vehicle(
        id="tiltrotor",
        label="틸트로터 (VTOL)",
        max_alt=300,
        geometry={
            "bodySize": {"x": 0.9, "y": 0.34, "z": 2.2},
            "hasWings": True, "wingSpan": 3.2, "wingChord": 0.55,
            "motors": [
                {"x": 1.1, "z": -0.5, "tiltable": True},
                {"x": -1.1, "z": -0.5, "tiltable": True},
                {"x": 1.1, "z": 0.9, "tiltable": True},
                {"x": -1.1, "z": 0.9, "tiltable": True},
            ],
        },
        compute_dynamics=compute_dynamics,
        initial_extra_state=lambda: {"tilt": 0.0, "tiltTarget": 0.0},
        control_hint="Q/E: 틸트(수직-수평)",
    )


VEHICLE_FACTORIES: dict[str, Callable[[], Vehicle]] = {
    "quadrotor": create_quadrotor_vehicle,
    "tiltrotor": create_tiltrotor_vehicle,
}
