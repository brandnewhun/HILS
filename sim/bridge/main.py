# -*- coding: utf-8 -*-
"""
HILS 브릿지 진입점 — config.py에서 설정을 읽어 아래 3개 독립 모듈을 그냥 순서대로
연결만 한다(이 파일 자체에는 로직을 넣지 않는다. 로직을 바꾸려면 해당 모듈만 고칠 것):

  MavlinkLink(mavlink_link.py)   — Pixhawk와의 EICD-01 물리/논리 인터페이스
  FlightDynamicsModel(fdm.py)    — ENV의 비행동역학 계산
  TelemetryHub(telemetry_hub.py) — Channel C를 브라우저(quadrotor_hud_v2.html)로 전달

실행:
    cd HILS_ICD/sim/bridge
    pip install -r requirements.txt
    python main.py
그 다음 브라우저에서 quadrotor_hud_v2.html을 열면(기본 ws://localhost:8765로 접속)
"BRIDGE 연결됨" 표시와 함께 텔레메트리가 들어오기 시작한다.
"""
import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from fdm import FlightDynamicsModel
from mavlink_link import MavlinkLink
from telemetry_hub import TelemetryHub


class SharedState:
    """main 루프(쓰기)와 telemetry_hub의 asyncio 스레드(읽기) 사이의 유일한 접점."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = {}

    def update(self, snapshot):
        with self._lock:
            self._snapshot = snapshot

    def read(self):
        with self._lock:
            return dict(self._snapshot)


def _finite(value, fallback=0.0):
    """FDM이 발산해 NaN/Inf가 나와도 그대로 JSON에 실어보내지 않는다 — 표준 JSON은
    NaN을 모른다. 브라우저 쪽 JSON.parse가 그 값을 그냥 조용히 통째로 실패시켜
    패킷 자체가 버려지므로(applyTelemetry 쪽 catch), 여기서 미리 막는 게 안전하다."""
    return value if isinstance(value, (int, float)) and math.isfinite(value) else fallback


def build_vis_payload(fdm_snapshot, vehicle_id, armed, link_ok):
    """quadrotor_hud_v2.html의 FlightSim.applyTelemetry()가 기대하는 필드명과 1:1 대응.
    필드를 추가/변경하면 그쪽 HTML의 applyTelemetry() 화이트리스트도 같이 맞춰줄 것."""
    return {
        "north": _finite(fdm_snapshot["north"]),
        "east": _finite(fdm_snapshot["east"]),
        "alt": _finite(fdm_snapshot["alt"]),
        "roll": _finite(fdm_snapshot["roll"]),
        "pitch": _finite(fdm_snapshot["pitch"]),
        "heading": _finite(fdm_snapshot["heading"]),
        "yawRate": _finite(fdm_snapshot["yawRate"]),
        "vN": _finite(fdm_snapshot["vN"]),
        "vE": _finite(fdm_snapshot["vE"]),
        "climbRate": _finite(fdm_snapshot["climbRate"]),
        "tilt": _finite(fdm_snapshot["tilt"]),
        "vehicle": vehicle_id,
        "armed": bool(armed),
        "linkOk": bool(link_ok),
    }


def connect_with_retry(link, retry_delay_s=3.0):
    while True:
        try:
            link.connect()
            return
        except Exception as e:
            print("[main] 시리얼 연결 실패(%s: %s) — %.0f초 후 재시도" % (type(e).__name__, e, retry_delay_s))
            time.sleep(retry_delay_s)


def main():
    print("[main] Pixhawk 연결 시도: %s (baud=%s)" % (config.SERIAL_PORT, config.SERIAL_BAUD))
    link = MavlinkLink(config)
    connect_with_retry(link)
    print("[main] 시리얼 포트 오픈 완료 — HIL_ACTUATOR_CONTROLS/HEARTBEAT 대기 중...")

    dynamics = FlightDynamicsModel(config.FDM)
    shared = SharedState()

    hub = TelemetryHub(config.WS_HOST, config.WS_PORT, config.WS_BROADCAST_HZ, shared.read)
    hub.start()

    period = {k: 1.0 / v for k, v in config.RATES_HZ.items()}
    next_due = {k: 0.0 for k in period}

    vehicle_id = "tiltrotor" if config.CUSTOM_TILT_DIALECT_ENABLED else "quadrotor"

    # 참고: Windows의 time.sleep()/time.time() 해상도는 ms 단위 근사이므로 RATES_HZ의
    # 250Hz 등은 "목표치"이지 하드 리얼타임 보장은 아니다 — 구조 검증 용도로는 충분하나,
    # 정밀 타이밍이 꼭 필요해지면 이 루프를 별도 스레드+고해상도 타이머로 바꿀 것.
    last_t = time.perf_counter()
    print("[main] 메인 루프 시작 (Ctrl+C로 종료)")
    try:
        while True:
            now = time.perf_counter()
            dt = max(1e-4, min(0.02, now - last_t))
            last_t = now

            try:
                link.poll_incoming()

                motors = link.latest_actuator["motors"]
                tilt_sp = link.latest_actuator["tilt_setpoint"]
                dynamics.step(dt, motors, tilt_sp)
                snap = dynamics.snapshot()

                wall_now = time.time()
                for key, p in period.items():
                    if wall_now >= next_due[key]:
                        next_due[key] = wall_now + p
                        if key == "heartbeat":
                            link.send_heartbeat()
                        elif key == "imu":
                            link.send_hil_sensor(snap)
                        elif key == "gps":
                            link.send_hil_gps(snap)
                        elif key == "tilt_state":
                            link.send_hil_tilt_state(snap)

                shared.update(build_vis_payload(
                    snap,
                    vehicle_id,
                    armed=link.is_armed(),
                    link_ok=link.fcc_link_ok(),
                ))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # USB 케이블이 빠지거나 OS 시리얼 오류가 나도 브릿지 프로세스 전체가
                # 죽지 않게 한다 — FDM 상태(위치/자세 등)는 유지한 채 재연결만 시도.
                # 이 창(재연결 시도 중)에는 브라우저에도 새 텔레메트리가 안 나가고,
                # PX4는 EICD-01 3.1.5절 페일세이프(HIL_SENSOR 500ms 미수신)를 스스로
                # 타게 된다 — 그게 이 상황에서 기대되는 정상 동작이다.
                print("[main] MAVLink I/O 오류(%s: %s) — 재연결 시도" % (type(e).__name__, e))
                try:
                    if link.conn is not None:
                        link.conn.close()
                except Exception:
                    pass
                connect_with_retry(link)
                last_t = time.perf_counter()

            time.sleep(0.001)
    except KeyboardInterrupt:
        print("\n[main] 종료합니다.")


if __name__ == "__main__":
    main()
