"""
LiveVehicleSource -- 실제 Pixhawk의 MAVLink 텔레메트리(Px4Link)를 FlightSim.state와
정확히 같은 모양의 dict로 변환해주는 어댑터.

이렇게 분리해두는 이유: renderer_base/renderer_matplotlib는 "state가 이 필드들
(roll/pitch/yawRate/heading/vN/vE/north/east/climbRate/alt/tilt)을 가진 dict"라는
것만 알면 되고, 그 state가 FlightSim의 물리 시뮬레이션에서 나왔는지 실제 하드웨어
텔레메트리에서 나왔는지는 몰라도 된다. 그래서 시뮬레이션 재생과 실기 관측을 같은
렌더러 코드로 그대로 전환할 수 있다 -- run_demo.py는 FlightSim을, run_live.py는
이 클래스를 renderer에 넘기는 것 말고는 거의 동일한 루프 구조를 쓴다.
"""
from __future__ import annotations

import math
from typing import Any

from px4_link import Px4Link


class LiveVehicleSource:
    def __init__(self, link: Px4Link):
        self.link = link
        self.state: dict[str, Any] = {
            "roll": 0.0, "pitch": 0.0, "yawRate": 0.0, "heading": 0.0,
            "vN": 0.0, "vE": 0.0, "north": 0.0, "east": 0.0,
            "climbRate": 0.0, "alt": 0.0, "tilt": 0.0,
        }
        # 물리 상태(state)와는 별개로, 실제 액추에이터 출력을 HUD에 같이 보여주기 위한
        # 부가 정보 -- renderer_base의 선택적 확장 훅 update_actuators()로 전달된다.
        self.actuators: dict[str, Any] = {"servo_outputs": [0] * 8, "armed": False, "mode": None, "connected": False}

    def poll(self) -> None:
        self.link.poll()
        latest = self.link.latest

        s = self.state
        s["roll"] = latest["roll"]
        s["pitch"] = latest["pitch"]
        s["yawRate"] = latest["yawspeed"]
        s["heading"] = latest["yaw"] % (2 * math.pi)
        s["north"] = latest["north"]
        s["east"] = latest["east"]
        s["alt"] = latest["alt"]
        s["vN"] = latest["vN"]
        s["vE"] = latest["vE"]
        s["climbRate"] = -latest["vD"]
        # tilt: 표준 MAVLink에는 틸트각 필드가 없다 -- 커스텀 다이얼렉트(HIL_TILT_STATE)를
        # 붙이기 전까지는 항상 0(수직)으로 둔다. sim/bridge 쪽에 이미 그 다이얼렉트 연동
        # 골격이 있으니, 필요해지면 여기서 값만 채워 넣으면 된다.
        s["tilt"] = 0.0

        a = self.actuators
        a["servo_outputs"] = latest["servo_outputs"]
        a["armed"] = latest["armed"]
        a["mode"] = latest["mode"]
        a["connected"] = latest["connected"]
