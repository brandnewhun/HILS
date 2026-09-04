"""
RendererBase -- 렌더러 계약. HILS_ICD/sim/quadrotor_hud.html 상단 주석의 "RENDERER 계약"을
그대로 옮긴 것이다.

이 프로젝트의 핵심 아이디어: WorldModel / Vehicle / FlightSim / TargetDrone(같은 폴더의
나머지 모듈들)은 "무엇으로 화면에 그릴지"를 전혀 모른다. 오직 이 RendererBase 계약만
알면 되고, 렌더 엔진에 종속된 코드는 전부 이 클래스를 상속하는 구현체 안에만 있으면 된다.

지금 이 저장소에는 참고용 구현체 renderer_matplotlib.MatplotlibRenderer 하나뿐이지만,
설계 의도는 나중에 NVIDIA Isaac Sim이나 Unreal Engine으로 렌더러를 새로 만들 때
world_model.py / vehicle.py / flight_sim.py / target_drone.py를 단 한 줄도 고치지 않고,
이 클래스를 상속하는 새 파일 하나(예: renderer_isaac.py, renderer_unreal.py)만 추가하면
되게 하는 것이다.

새 엔진 바인딩을 추가하는 법:
    1) 이 클래스를 상속한다.
    2) 아래 6개 abstract 메서드를 구현한다(엔진의 씬 그래프/액터 API로 변환).
    3) 필요하면 타겟 기체용 2개 메서드(set_target_geometry/update_target_from_state)도
       오버라이드한다 -- 기본 구현은 no-op이라 타겟을 안 그리는 렌더러는 그대로 둬도 된다.
    world_model.py / vehicle.py / flight_sim.py / target_drone.py는 손댈 필요가 없다.

geometry 파라미터(init/set_vehicle/set_target_geometry에서 받는 값)는 vehicle.py의
Vehicle.geometry와 정확히 같은 형식이다:
    {"bodySize": {"x":.., "y":.., "z":..}, "hasWings": bool,
     "wingSpan": float, "wingChord": float,       # hasWings=True일 때만 의미 있음
     "motors": [{"x":.., "z":.., "tiltable": bool}, ...]}
이건 특정 렌더 엔진의 메시 포맷이 아니라 "이 기체가 대략 어떻게 생겼는지"에 대한 선언적
설명이므로, 각 렌더러 구현체가 알아서 자기 엔진의 프리미티브/에셋으로 변환하면 된다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from world_model import WorldModel
from vehicle import Vehicle


class RendererBase(ABC):
    @abstractmethod
    def init(self, world: WorldModel, vehicle: Vehicle) -> None:
        """씬 초기화 -- 지형/도로/건물, 초기 기체 메시 생성 (1회)."""

    @abstractmethod
    def update_from_state(self, state: dict[str, Any]) -> None:
        """매 프레임, 플레이어 물리 상태(FlightSim.state)를 기체 위치/자세/틸트/카메라에 반영."""

    @abstractmethod
    def render(self) -> None:
        """한 프레임 그리기."""

    @abstractmethod
    def resize(self) -> None:
        """창/뷰포트 크기 변경 대응."""

    @abstractmethod
    def set_time_of_day(self, key: str) -> None:
        """'dawn'|'morning'|'noon'|'sunset'|'night' 프리셋 적용."""

    @abstractmethod
    def set_vehicle(self, vehicle: Vehicle) -> None:
        """기체가 바뀔 때 기존 기체 메시를 지우고 새 형상으로 재구성."""

    # ------------------------- 타겟 기체용 추가 계약 (선택적) -------------------------
    # 기본 구현은 no-op -- 타겟 기체를 그리지 않는 렌더러(예: 헤드리스 로그 렌더러)는
    # 오버라이드하지 않아도 된다.
    def set_target_geometry(self, geometry: dict[str, Any]) -> None:
        """타겟용 기체 메시를 (재)생성. geometry 형식은 Vehicle.geometry와 동일."""

    def update_target_from_state(self, state: dict[str, Any]) -> None:
        """매 프레임, 타겟의 물리 상태(TargetDrone.state)를 위치/자세에 반영.
        state["active"] is False면 메시를 숨긴다."""

    # ---------------------- 실기(live hardware) 액추에이터용 추가 계약 (선택적) ----------------------
    # 기본 구현은 no-op -- 시뮬레이션만 재생하는 렌더러(액추에이터 출력 자체가 없는 경우)는
    # 오버라이드하지 않아도 된다. live_vehicle_source.LiveVehicleSource가 이 형식으로
    # actuators dict를 채운다: {"servo_outputs": [PWM us, ...], "armed": bool,
    # "mode": str|None, "connected": bool}.
    def update_actuators(self, actuators: dict[str, Any]) -> None:
        """매 프레임, 실제 기체(또는 HIL 브릿지)의 액추에이터/암 상태를 HUD에 반영."""
