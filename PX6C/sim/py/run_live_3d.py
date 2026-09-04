"""
실기(real PX6c) 연결 + 3D(Three.js) 시각화 + 키보드 조종 실행 진입점.

QGC 없이: (1) 브라우저에서 화살표/WASD로 입력한 조종값을 실제 Pixhawk의 OFP(PX4
펌웨어)에 MANUAL_CONTROL로 그대로 보내고, (2) 그 결과 PX4가 계산한 실제 자세/위치/
액추에이터 출력을 다시 읽어서, 이미 만들어둔 3D 씬(지형/도로/건물/오버패스/타겟
드론까지 다 있는 sim/quadrotor_hud_v2.html)에 표시한다. 완전한 폐루프: 브라우저
입력 -> 실기 -> 실기 출력 -> 브라우저 표시.

구성 (전부 이미 있는 조각을 그대로 이어붙인 것 -- 새 3D 코드는 한 줄도 안 씀):
    px4_link.py            Pixhawk MAVLink 링크 -- 텔레메트리 수신 + MANUAL_CONTROL 송신
    live_vehicle_source.py 텔레메트리를 FlightSim.state와 같은 모양으로 변환
    telemetry_ws_server.py 상태는 브라우저로 브로드캐스트, 브라우저의 키보드 입력은
                            반대 방향으로 받아온다(양방향)
    sim/quadrotor_hud_v2.html  이미 있는 3D HUD(다른 세션이 만듦) -- 이번에 키보드
                            RC 입력 캡처 + BridgeLink.send()를 추가했다. 이 스크립트가
                            로컬 HTTP로 서빙하고 브라우저를 자동으로 띄운다.

나중에 렌더러를 AirSim/Unreal/NVIDIA Isaac으로 바꾸고 싶다면, 이 스크립트에서
"WebSocket으로 주고받는 JSON 형식"(텔레메트리는 build_payload(), RC 입력은
{"type":"rc", pitch, roll, yaw, thr})만 유지한 채 TelemetryWsServer 대신 그 엔진의
데이터 소스/싱크로 갈아끼우면 된다 -- px4_link/live_vehicle_source는 손댈 필요가
없다. 반대로 렌더링을 그대로 두고 Python 쪽 시뮬레이션(run_demo.py의 FlightSim)으로
다시 돌리고 싶으면 이 스크립트 대신 run_demo.py를 쓰면 된다 -- 두 진입점은 서로
독립적이다.

⚠ 프로펠러를 뺀 상태(또는 안전한 고정 지그)에서 먼저 테스트할 것. 처음 켰을 때
PX4가 이 조종 입력을 받아들이려면 COM_RC_IN_MODE가 "Joystick"(또는 동급)으로
설정되어 있어야 한다 -- px4_link.py의 send_manual_control() 문서 참조.

    python run_live_3d.py COM11
    python run_live_3d.py /dev/ttyACM0
    python run_live_3d.py COM11 --log debug.jsonl   # 진단 로그까지 남기기

--log를 주면 "브라우저가 보낸 조종 입력 / 실기로 실제 보낸 MANUAL_CONTROL / 실기가
돌려준 텔레메트리 / MAVLink 메시지 종류별 수신 건수"를 한 파일에 시간순으로 기록한다.
브라우저 쪽을 따로 캡처할 필요 없이 이 파일 하나만 보면 어느 구간이 끊겼는지 알 수 있다.
"""
from __future__ import annotations

import argparse
import http.server
import json
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
RC_SEND_HZ = 10.0  # ICD D-01(RC_CHANNELS) 채널과 동일한 샘플-홀드 주기(Hz)
LOG_HZ = 5.0       # --log 진단 기록 주기(Hz) -- 사람이 눈으로 훑기 좋은 정도


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


class RcInputState:
    """브라우저(BridgeLink.send())가 보내는 키보드 RC 입력의 최신값 -- telemetry_ws_server의
    asyncio 스레드(쓰기)와 메인 폴링 루프(읽기) 사이의 접점. 링크가 끊기거나 브라우저
    창을 닫으면 새 메시지가 안 오므로, 마지막 입력이 그대로 유지되지 않도록 run()의
    메인 루프에서 별도로 링크 나이(age)를 체크해 페일세이프로 0으로 되돌린다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rc = {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "thr": 0.0}
        self._last_update_s = 0.0

    def on_message(self, msg: dict) -> None:
        if msg.get("type") != "rc":
            return
        with self._lock:
            for k in ("pitch", "roll", "yaw", "thr"):
                v = msg.get(k)
                if isinstance(v, (int, float)):
                    self._rc[k] = max(-1.0, min(1.0, float(v)))
            self._last_update_s = time.time()

    def read(self, timeout_s: float = 1.0) -> dict:
        """timeout_s 동안 새 메시지가 없으면(브라우저 창을 닫았거나 WebSocket이 끊긴
        경우) 안전하게 중립(0)을 반환한다 -- 마지막 입력값으로 계속 조종되는 것을 막는
        페일세이프."""
        with self._lock:
            if time.time() - self._last_update_s > timeout_s and self._last_update_s > 0:
                return {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "thr": 0.0}
            return dict(self._rc)


class DebugLogger:
    """진단용 JSONL 로거 -- 한 줄에 한 스냅샷씩 기록한다.

    이 프로세스는 양쪽 데이터를 다 갖고 있다(브라우저가 보낸 RC 입력 + Pixhawk가 보낸
    텔레메트리). 그래서 브라우저 콘솔을 따로 캡처할 필요 없이 여기 한 파일만 보면
    "입력이 들어왔는지 / 실기로 보냈는지 / 실기가 뭘 돌려줬는지"를 시간순으로 대조할 수
    있다. 특히 msgs(메시지 종류별 누적 카운트)를 보면 LOCAL_POSITION_NED처럼 아예 안
    오는 메시지가 있는지 바로 드러난다.

    기록 형식(한 줄 = JSON 1개):
      t     : 시작 후 경과 시간(s)
      rc_in : 브라우저에서 마지막으로 받은 조종 입력(-1..1)
      mc    : 실제로 실기에 보낸 MANUAL_CONTROL 정수값(x/y/z/r), 아직 안 보냈으면 null
      tlm   : 실기가 돌려준 값(자세/위치/액추에이터/armed/mode)
      msgs  : MAVLink 메시지 종류별 누적 수신 건수
    """

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "w", encoding="utf-8")
        self._t0 = time.time()

    def write(self, rc_in: dict, link: Px4Link, source: LiveVehicleSource) -> None:
        latest = link.latest
        rec = {
            "t": round(time.time() - self._t0, 2),
            "rc_in": rc_in,
            "mc": link.last_manual_control,
            "tlm": {
                "roll": round(latest["roll"], 4),
                "pitch": round(latest["pitch"], 4),
                "yaw": round(latest["yaw"], 4),
                "north": round(latest["north"], 3),
                "east": round(latest["east"], 3),
                "alt": round(latest["alt"], 3),
                "armed": latest["armed"],
                "mode": latest["mode"],
                "servo": latest["servo_outputs"],
            },
            "msgs": dict(link.msg_counts),
        }
        self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._f.flush()  # 도중에 Ctrl+C로 끊어도 지금까지 기록이 남도록 매번 flush

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


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


def run(connection_string: str, baud: int, log_path: str | None = None) -> None:
    link = Px4Link(connection_string, baud=baud)
    print(f"[run_live_3d] Pixhawk 연결 시도: {connection_string} (baud={baud})")
    link.connect()
    print("[run_live_3d] 연결됨 -- ATTITUDE/SERVO_OUTPUT_RAW 대기 중")

    source = LiveVehicleSource(link)
    shared = SharedState()
    rc_input = RcInputState()

    start_static_http_server(SIM_DIR, HTTP_PORT)

    ws_server = TelemetryWsServer(WS_HOST, WS_PORT, BROADCAST_HZ, shared.read,
                                   on_client_message=rc_input.on_message)
    ws_server.start()

    url = f"http://localhost:{HTTP_PORT}/{HTML_FILE}?ws=ws://{WS_HOST}:{WS_PORT}"
    print(f"[run_live_3d] 브라우저를 엽니다: {url}")
    webbrowser.open(url)

    logger = None
    if log_path:
        logger = DebugLogger(log_path)
        print(f"[run_live_3d] 진단 로그 기록 중: {os.path.abspath(log_path)}")

    period = 1.0 / POLL_HZ
    rc_period = 1.0 / RC_SEND_HZ
    log_period = 1.0 / LOG_HZ
    rc_accum = 0.0
    log_accum = 0.0
    rc = {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "thr": 0.0}
    print("[run_live_3d] 폴링 루프 시작 -- 브라우저 키보드 입력이 실기로 전송됩니다 (Ctrl+C로 종료)")
    try:
        while True:
            t0 = time.perf_counter()
            source.poll()
            shared.update(build_payload(source.state, source.actuators))

            rc_accum += period
            if rc_accum >= rc_period:
                rc_accum -= rc_period
                rc = rc_input.read()
                link.send_manual_control(rc["pitch"], rc["roll"], rc["yaw"], rc["thr"])

            if logger is not None:
                log_accum += period
                if log_accum >= log_period:
                    log_accum -= log_period
                    logger.write(rc, link, source)

            time.sleep(max(0.0, period - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        print("\n[run_live_3d] 종료합니다.")
    finally:
        if logger is not None:
            logger.close()
            print(f"[run_live_3d] 로그 저장 완료: {os.path.abspath(log_path)}")
        link.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("connection", help="예: COM11, /dev/ttyACM0, udp:127.0.0.1:14540")
    parser.add_argument("--baud", type=int, default=115200, help="USB 직결이면 무시됨(native 속도)")
    parser.add_argument("--log", metavar="PATH", default=None,
                         help="진단용 JSONL 로그 파일 경로 (예: --log debug.jsonl). "
                              "브라우저 입력/실기 송신값/실기 회신값/MAVLink 메시지 카운트를 "
                              "한 파일에 시간순으로 기록한다.")
    args = parser.parse_args()
    run(args.connection, args.baud, args.log)


if __name__ == "__main__":
    main()
