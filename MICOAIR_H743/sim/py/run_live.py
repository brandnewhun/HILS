"""
실기(Micoair H743 V2, PX4 OFP) 연결 실행 진입점 -- QGC 없이, 실제 Pixhawk가 RC 입력을 받아 계산한
자세/액추에이터 출력을 그대로 Quadrotor HUD 시뮬레이터(Python 포트)에 표시한다.

전제: PX4는 HIL 모드가 아니라 평소처럼(SYS_HITL=0) 떠 있고, 실제 RC 입력을 받아
스스로 자세를 추정하고 액추에이터를 구동하는 중이다(모터 무장 여부는 상관없음).
이 스크립트는 그 결과를 MAVLink로 "읽기만" 한다 -- HIL_SENSOR 주입이나 명령 송신은
전혀 하지 않는다.

    python run_live.py COM5
    python run_live.py udp:127.0.0.1:14540

world_model/vehicle/flight_sim/target_drone/renderer_base/renderer_matplotlib는
run_demo.py(시뮬레이션 재생)와 완전히 동일한 코드를 그대로 재사용한다 -- 물리 상태를
"어디서 가져오는지"만 FlightSim -> LiveVehicleSource로 바뀔 뿐이다.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from world_model import WorldModel
from vehicle import VEHICLE_FACTORIES
from px4_link import Px4Link
from live_vehicle_source import LiveVehicleSource
from renderer_matplotlib import MatplotlibRenderer

TERRAIN_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terrain_data.json")
POLL_HZ = 30.0


def run_live(connection_string: str, baud: int, vehicle_id: str) -> None:
    world = WorldModel.from_json_file(TERRAIN_JSON)
    vehicle = VEHICLE_FACTORIES[vehicle_id]()

    link = Px4Link(connection_string, baud=baud)
    print(f"[run_live] Pixhawk 연결 시도: {connection_string} (baud={baud})")
    link.connect()
    print("[run_live] 연결됨 -- ATTITUDE/SERVO_OUTPUT_RAW 대기 중 (QGC는 켜져 있어도 무방, "
          "같은 포트를 동시에 열 수는 없음)")

    source = LiveVehicleSource(link)

    renderer = MatplotlibRenderer()
    renderer.init(world, vehicle)

    period = 1.0 / POLL_HZ
    try:
        while True:
            t0 = time.perf_counter()
            source.poll()
            renderer.update_from_state(source.state)
            renderer.update_actuators(source.actuators)
            renderer.render()
            renderer._plt.pause(max(0.001, period - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        print("\n[run_live] 종료합니다.")
    finally:
        link.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("connection", help="예: COM5, /dev/ttyACM0, udp:127.0.0.1:14540")
    parser.add_argument("--baud", type=int, default=115200, help="USB 직결이면 무시됨(native 속도)")
    parser.add_argument("--vehicle", choices=list(VEHICLE_FACTORIES), default="quadrotor",
                         help="HUD에 표시할 기체 형상(물리 계산에는 영향 없음 -- 실기 값을 그대로 표시)")
    args = parser.parse_args()
    run_live(args.connection, args.baud, args.vehicle)


if __name__ == "__main__":
    main()
