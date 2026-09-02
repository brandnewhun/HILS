"""
Px4Link -- 실제 Pixhawk 6C(또는 임의의 PX4 보드)에 MAVLink로 접속해 텔레메트리만
"읽어오는" 얇은 래퍼. HIL 센서 주입이나 명령 송신은 하지 않는다.

이건 PX4가 이미 정상(비-HIL) 모드로 떠서 실제 RC 입력을 받고, 스스로 자세를 추정하고
액추에이터를 구동하는 상황(QGC로 관찰하던 벤치 테스트와 동일)을 그대로 관측하는
용도다. 그래서 sim/bridge/mavlink_link.py(다른 세션이 만든, HIL_SENSOR를 주입하고
그 응답으로 온 HIL_ACTUATOR_CONTROLS를 자체 FDM으로 해석하는 완전한 HIL 폐루프용
모듈)와는 목적 자체가 다르다 -- 이쪽은 순수 리스너라 비행동역학모델(FDM)이 필요
없다. PX4 자신이 이미 진짜 자세/위치/액추에이터 값을 계산해서 MAVLink로 흘려보내고
있기 때문이다.

이 모듈은 pymavlink 표준 다이얼렉트(common.xml)만 사용한다 -- 커스텀 틸트 메시지가
필요 없는 것도 이쪽이 훨씬 단순한 이유 중 하나.
"""
from __future__ import annotations

import time
from typing import Any

from pymavlink import mavutil


def _new_snapshot() -> dict[str, Any]:
    return {
        "connected": False, "armed": False, "mode": None,
        "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
        "rollspeed": 0.0, "pitchspeed": 0.0, "yawspeed": 0.0,
        # LOCAL_POSITION_NED가 없는 보드(실내 벤치, GPS/광류 없음)에서는 계속 0으로
        # 남는다 -- 그래도 자세/액추에이터 관측에는 지장 없다.
        "north": 0.0, "east": 0.0, "alt": 0.0,
        "vN": 0.0, "vE": 0.0, "vD": 0.0,
        "servo_outputs": [0] * 8,  # SERVO_OUTPUT_RAW의 servo1..8_raw (PWM, μs)
        "last_update_s": 0.0,
    }


class Px4Link:
    def __init__(self, connection_string: str, baud: int = 115200):
        """connection_string 예시:
            "COM5"                  -- Windows, Pixhawk USB(CDC-ACM) 직결
            "/dev/ttyACM0"          -- Linux, USB 직결
            "udp:127.0.0.1:14540"   -- SITL이나 QGC와 같은 PC에서 UDP로 중계받을 때
        USB 직결이면 Pixhawk가 native USB 속도로 통신하므로 baud는 사실상 무시된다."""
        self.connection_string = connection_string
        self.baud = baud
        self.conn = None
        self._latest: dict[str, Any] = _new_snapshot()

    def connect(self, heartbeat_timeout: float = 10.0) -> None:
        self.conn = mavutil.mavlink_connection(self.connection_string, baud=self.baud)
        self.conn.wait_heartbeat(timeout=heartbeat_timeout)
        self._latest["connected"] = True

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn = None
        self._latest["connected"] = False

    @property
    def latest(self) -> dict[str, Any]:
        return dict(self._latest)

    def poll(self) -> None:
        """대기 중인 MAVLink 메시지를 전부 소진하며 latest 스냅샷을 갱신한다.
        매 프레임 non-blocking으로 호출하는 용도(recv_match blocking=False)."""
        if self.conn is None:
            return
        while True:
            msg = self.conn.recv_match(blocking=False)
            if msg is None:
                break
            self._apply(msg)

    def _apply(self, msg) -> None:
        t = msg.get_type()
        latest = self._latest
        latest["last_update_s"] = time.time()

        if t == "HEARTBEAT":
            latest["armed"] = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            try:
                latest["mode"] = mavutil.mode_string_v10(msg)
            except Exception:
                latest["mode"] = str(msg.custom_mode)

        elif t == "ATTITUDE":
            latest["roll"] = msg.roll
            latest["pitch"] = msg.pitch
            latest["yaw"] = msg.yaw
            latest["rollspeed"] = msg.rollspeed
            latest["pitchspeed"] = msg.pitchspeed
            latest["yawspeed"] = msg.yawspeed

        elif t == "LOCAL_POSITION_NED":
            latest["north"] = msg.x
            latest["east"] = msg.y
            latest["alt"] = -msg.z  # PX4 NED: z는 아래가 +. 우리 HUD의 alt는 위가 +.
            latest["vN"] = msg.vx
            latest["vE"] = msg.vy
            latest["vD"] = msg.vz

        elif t == "SERVO_OUTPUT_RAW":
            latest["servo_outputs"] = [getattr(msg, f"servo{i}_raw", 0) for i in range(1, 9)]

        elif t == "ACTUATOR_OUTPUT_STATUS":
            # 최신 PX4/MAVLink는 SERVO_OUTPUT_RAW 대신(또는 추가로) 이 메시지를 쓰기도
            # 한다 -- 채널 수가 더 많고 단위도 실수. 있으면 이쪽 값으로 덮어쓴다.
            actuator = getattr(msg, "actuator", None)
            if actuator:
                latest["servo_outputs"] = list(actuator[:8])
