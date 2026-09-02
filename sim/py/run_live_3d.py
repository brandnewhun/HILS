"""
실기(real PX6c) 연결 + 3D(Three.js) 시각화 실행 진입점.

QGC 없이, 실제 Pixhawk가 (HIL이 아니라 평소처럼) RC 입력을 받아 계산한 자세/위치/
액추에이터 출력을 그대로 읽어서, 이미 만들어둔 3D 씬(지형/도로/건물/오버패스/타겟
드론까지 다 있는 sim/quadrotor_hud_v2.html)에 표시한다.

구성 (전부 이미 있는 조각을 그대로 이어붙인 것 -- 새 3D 코드는 한 줄도 안 씀):
    px4_link.py            Pixhawk MAVLink 텔레메트리 리스너 (읽기 전용)
    live_vehicle_source.py 그 텔레메트리를 FlightSim.state와 같은 모양으로 변환
    telemetry_ws_server.py 위 상태를 WebSocket으로 브라우저에 브로드캐스트
    sim/quadrotor_hud_v2.html  이미 있는 3D HUD(다른 세션이 만듦, BridgeLink로
                            WebSocket 텔레메트리를 받게 되어 있음) -- 이 스크립트가
                            로컬 HTTP로 서빙하고 브라우저를 자동으로 띄운다.

나중에 렌더러를 AirSim/Unreal/NVIDIA Isaac으로 바꾸고 싶다면, 이 스크립트에서
"WebSocket으로 내보내는 payload"(telemetry_ws_server.build_payload가 만드는 dict)
형식만 유지한 채 TelemetryWsServer 대신 그 엔진의 데이터 소스로 갈아끼우면 된다 --
px4_link/live_vehicle_source(텔레메트리 읽기)는 손댈 필요가 없다. 반대로 렌더링을
그대로 두고 Python 쪽 시뮬레이션(run_demo.py의 FlightSim)으로 다시 돌리고 싶으면
이 스크립트 대신 run_demo.py를 쓰면 된다 -- 두 진입점은 서로 독립적이다.

    python run_live_3d.py COM11
    python run_live_3d.py /dev/ttyACM0
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from px4_link import Px4Link
from live_vehicle_source import LiveVehicleSource
from telemetry_ws_server import TelemetryWsServer, build_payload

SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # sim/py/ -> sim/
HTML_FILE = "quadrotor_hud_v2.html"

WS_HOST, WS_PORT = "localhost", 8765
HTTP_PORT = 8000
POLL_HZ = 60.0
BROADCAST_HZ = 30.0


class SharedState:
    """px4_link 폴링(메인 스레드 쓰기)과 telemetry_ws_server의 asyncio 브로드캐스트
    스레드(읽기) 사이의 유일한 접점 -- 잠금 없이 dict를 그대로 공유하면 브로드캐스트
    도중 절반만 갱신된 값을 읽을 수 있어(드물지만) 락으로 스냅샷을 통째로 교체한다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._payload: dict = {}

    def update(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload

    def read(self) -> dict:
        with self._lock:
            return dict(self._payload)


class _Utf8HtmlHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler는 .html에 'text/html'만 보내고 charset을 안 붙인다 --
    브라우저가 인코딩을 추측하다 quadrotor_hud_v2.html의 한글 UI 텍스트가 깨진다.
    UTF-8을 명시해서 그 문제를 없앤다."""

    def guess_type(self, path):
        ctype = super().guess_type(path)
        if ctype == "text/html":
            return "text/html; charset=utf-8"
        return ctype


def start_static_http_server(directory: str, port: int) -> None:
    handler = lambda *args, **kwargs: _Utf8HtmlHandler(*args, directory=directory, **kwargs)
    httpd = socketserver.TCPServer(("localhost", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="static-http", daemon=True)
    thread.start()
    print(f"[run_live_3d] 정적 파일 서버 시작: http://localhost:{port}/ (문서 루트: {directory})")


def run(connection_string: str, baud: int) -> None:
    link = Px4Link(connection_string, baud=baud)
    print(f"[run_live_3d] Pixhawk 연결 시도: {connection_string} (baud={baud})")
    link.connect()
    print("[run_live_3d] 연결됨 -- ATTITUDE/SERVO_OUTPUT_RAW 대기 중")

    source = LiveVehicleSource(link)
    shared = SharedState()

    start_static_http_server(SIM_DIR, HTTP_PORT)

    ws_server = TelemetryWsServer(WS_HOST, WS_PORT, BROADCAST_HZ, shared.read)
    ws_server.start()

    url = f"http://localhost:{HTTP_PORT}/{HTML_FILE}?ws=ws://{WS_HOST}:{WS_PORT}"
    print(f"[run_live_3d] 브라우저를 엽니다: {url}")
    webbrowser.open(url)

    period = 1.0 / POLL_HZ
    print("[run_live_3d] 폴링 루프 시작 (Ctrl+C로 종료)")
    try:
        while True:
            t0 = time.perf_counter()
            source.poll()
            shared.update(build_payload(source.state, source.actuators))
            time.sleep(max(0.0, period - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        print("\n[run_live_3d] 종료합니다.")
    finally:
        link.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("connection", help="예: COM11, /dev/ttyACM0, udp:127.0.0.1:14540")
    parser.add_argument("--baud", type=int, default=115200, help="USB 직결이면 무시됨(native 속도)")
    args = parser.parse_args()
    run(args.connection, args.baud)


if __name__ == "__main__":
    main()
