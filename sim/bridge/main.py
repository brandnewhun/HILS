# -*- coding: utf-8 -*-
"""
HILS 브릿지 진입점 — config.py에서 설정을 읽어 아래 4개 독립 모듈을 그냥 순서대로
연결만 한다(이 파일 자체에는 로직을 넣지 않는다. 로직을 바꾸려면 해당 모듈만 고칠 것):

  MavlinkLink(mavlink_link.py)   — Pixhawk와의 EICD-01 물리/논리 인터페이스
  FlightDynamicsModel(fdm.py)    — ENV의 비행동역학 계산
  TelemetryHub(telemetry_hub.py) — Channel C를 브라우저(quadrotor_hud_v2.html)로 전달
  RcSource(rc_source.py)        — Channel D(조종기 입력)를 어디서 가져올지(스크립트/
                                   수동/실제 외부 송신기)를 config.RC_SOURCE_MODE로 선택

실행:
    cd HILS_ICD/sim/bridge
    pip install -r requirements.txt
    python main.py
그 다음 브라우저에서 quadrotor_hud_v2.html을 열면(기본 ws://localhost:8765로 접속)
"BRIDGE 연결됨" 표시와 함께 텔레메트리가 들어오기 시작한다.
"""
import http.server
import json
import math
import os
import queue
import socketserver
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from fdm import FlightDynamicsModel
from mavlink_link import MavlinkLink, SIM_DEVICE_IDS
from rc_source import BrowserRcSource, ScriptedRcSource, create_rc_source
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


SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # bridge/ -> sim/
HUD_FILE = "quadrotor_hud_v2.html"
HTTP_PORT = 8000


class _Utf8HtmlHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler는 .html에 charset을 안 붙여서 브라우저가 인코딩을
    추측하다 한글 UI가 깨진다. UTF-8을 명시한다."""

    def guess_type(self, path):
        ctype = super().guess_type(path)
        return "text/html; charset=utf-8" if ctype == "text/html" else ctype

    def log_message(self, *args):
        pass  # 브릿지 콘솔에는 [FCC] 진단 메시지만 보이도록 HTTP 접근 로그는 숨긴다


def serve_hud_and_open_browser():
    """HUD를 로컬 HTTP로 띄우고 브라우저를 연다.

    예전에는 사용자가 quadrotor_hud_v2.html을 직접 file://로 열어야 했는데, 그러면
    "브릿지는 도는데 화면이 아예 안 뜬다"는 상태가 되기 쉬웠다(브라우저가 접속하지
    않으면 RC 입력도 영원히 0이라, 조종이 안 되는 원인으로도 이어진다).
    """
    try:
        handler = lambda *a, **kw: _Utf8HtmlHandler(*a, directory=SIM_DIR, **kw)
        httpd = socketserver.TCPServer(("localhost", HTTP_PORT), handler)
    except OSError as e:
        print("[main] HTTP 서버를 못 열었습니다(%s) - 브라우저를 직접 여세요." % e)
        return
    threading.Thread(target=httpd.serve_forever, name="hud-http", daemon=True).start()
    url = "http://localhost:%d/%s?ws=ws://%s:%d" % (HTTP_PORT, HUD_FILE, config.WS_HOST, config.WS_PORT)
    print("[main] HUD 화면: %s" % url)
    try:
        webbrowser.open(url)
    except Exception:
        print("[main] 브라우저 자동 실행 실패 - 위 주소를 직접 여세요.")


class DebugLogger:
    """진단용 JSONL 로거 — 한 줄에 한 스냅샷. --log 로 켠다.

    HIL에서 "안 움직인다"의 원인을 좁히려면 아래 넷을 같이 봐야 한다:
      rc     : 브라우저에서 온 조종 입력이 실제로 브릿지까지 왔는가
      armed  : PX4가 실제로 시동 상태인가 (버튼을 눌렀는지와 별개)
      motors : PX4가 HIL_ACTUATOR_CONTROLS로 돌려준 모터 명령 — 이게 계속 0이면
               FDM에 추력이 안 들어가니 당연히 안 움직인다
      msgs   : 메시지 종류별 수신 건수 — HIL_ACTUATOR_CONTROLS가 아예 0이면
               PX4가 HIL 모드로 안 떠 있다는 뜻
    events에는 PX4가 보낸 STATUSTEXT/COMMAND_ACK(시동 거부 사유)이 쌓인다.
    """

    def __init__(self, path):
        self.path = path
        self._f = open(path, "w", encoding="utf-8")
        self._t0 = time.time()
        self._events_written = 0
        self._lock = threading.Lock()

    def _write_record(self, rec):
        """WebSocket 스레드와 main 스레드가 같은 JSONL에 안전하게 기록한다."""
        with self._lock:
            self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._f.flush()

    def write_header(self, info):
        """첫 줄에 실행 환경 정보(캘리브레이션 ID 등)를 한 번만 기록."""
        rec = {"t": 0.0, "header": info}
        self._write_record(rec)

    def record_client_message(self, msg):
        """브라우저가 보낸 키/RC/ARM 이벤트를 수신 즉시 같은 시간축에 기록한다."""
        fields = ("type", "action", "key", "browser_ms", "pitch", "roll", "yaw", "thr", "armed")
        safe_msg = {key: msg[key] for key in fields if key in msg}
        self._write_record({
            "t": round(time.time() - self._t0, 3),
            "kind": "browser_input",
            "message": safe_msg,
        })

    def write(self, link, rc_values, snap):
        events = list(link.recent_events)
        new_events = events[self._events_written:]
        self._events_written = len(events)
        rec = {
            "t": round(time.time() - self._t0, 2),
            "kind": "snapshot",
            "rc": {k: round(v, 3) for k, v in rc_values.items()},
            "armed": bool(link.is_armed()),
            "link_ok": bool(link.fcc_link_ok()),
            "motors": [round(m, 4) for m in link.latest_actuator["motors"]],
            "actuator_controls": [round(v, 4) for v in link.latest_actuator["all_controls"]],
            "actuator_flags": link.latest_actuator["flags"],
            "manual_control": dict(link.last_manual_control) if link.last_manual_control else None,
            "tilt_sp": round(link.latest_actuator["tilt_setpoint"], 4),
            "fdm": {
                "north": round(snap["north"], 2), "east": round(snap["east"], 2),
                "alt": round(snap["alt"], 2),
                "roll": round(snap["roll"], 3), "pitch": round(snap["pitch"], 3),
                "heading": round(snap["heading"], 3),
            },
            "msgs": dict(link.msg_counts),
            "tx": dict(link.tx_counts),
            "sensors": dict(link.sys_status_sensors),
            "hires_imu": dict(link.latest_highres_imu),
            "events": new_events,
        }
        self._write_record(rec)  # Ctrl+C로 끊어도 지금까지 기록이 남도록

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


def main():
    log_path = None
    if "--log" in sys.argv:
        idx = sys.argv.index("--log")
        log_path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "bridge_debug.jsonl"

    print("[main] Pixhawk 연결 시도: %s (baud=%s)" % (config.SERIAL_PORT, config.SERIAL_BAUD))
    link = MavlinkLink(config)
    connect_with_retry(link)
    print("[main] 시리얼 포트 오픈 완료 — HIL_ACTUATOR_CONTROLS/HEARTBEAT 대기 중...")

    # 진단 A — PX4가 캘리브레이션에 등록해둔 센서 장치 ID를 읽어, HIL 시뮬레이션 센서의
    # 장치 ID와 같은지 대조한다. 다르면 PX4는 "등록된 그 센서"가 데이터를 안 준다고 보고
    # 'No valid data from Accel 0'으로 시동을 거부한다 — 이게 지금 의심 중인 원인.
    cal_ids = {}
    print("[main] 센서 캘리브레이션 장치 ID 조회 중...")
    for pname in ("CAL_ACC0_ID", "CAL_GYRO0_ID", "CAL_MAG0_ID", "CAL_BARO0_ID"):
        value = link.read_param(pname)
        cal_ids[pname] = int(value) if value is not None else None
        print("    %-13s = %s" % (pname, cal_ids[pname]))
    print("[main] HIL 시뮬레이션 센서가 쓰는 장치 ID(PX4 내부 상수):")
    for label, dev_id in SIM_DEVICE_IDS.items():
        match = [k for k, v in cal_ids.items() if v == dev_id]
        print("    %-34s = %-10d %s" % (label, dev_id, ("<= %s 와 일치" % ",".join(match)) if match else "(일치하는 CAL_*_ID 없음)"))

    dynamics = FlightDynamicsModel(config.FDM)
    rc_source = create_rc_source(config.RC_SOURCE_MODE, config.RC_SOURCE_OPTIONS)
    print("[main] RC 소스: %s" % config.RC_SOURCE_MODE)
    print("[main] PX4 조종 입력: %s (%.0f Hz)" %
          (config.CONTROL_INPUT_PROTOCOL, config.RATES_HZ["rc"]))
    shared = SharedState()

    logger = DebugLogger(log_path) if log_path else None
    if logger:
        logger.write_header({"cal_ids": cal_ids, "sim_device_ids": SIM_DEVICE_IDS,
                             "control_input_protocol": config.CONTROL_INPUT_PROTOCOL})
        print("[main] debug log -> %s" % os.path.abspath(log_path))

    # 브라우저 -> 브릿지 방향 메시지 처리. RC_SOURCE_MODE="browser"일 때는 키보드 입력을
    # rc_source로 흘려보내고, ARM/DISARM 요청은 어느 모드에서든 그대로 실기에 전달한다.
    # (요청은 asyncio 스레드에서 들어오므로, 여기서 직접 MAVLink를 쓰지 않고 큐에 넣어
    #  메인 루프가 보내게 한다 — 시리얼 쓰기를 한 스레드로 몰아 경합을 피하기 위함.)
    arm_requests = queue.Queue()

    def on_client_message(msg):
        if logger:
            logger.record_client_message(msg)
        mtype = msg.get("type")
        if mtype == "rc" and isinstance(rc_source, BrowserRcSource):
            rc_source.on_message(msg)
        elif mtype == "arm":
            arm_requests.put(bool(msg.get("armed")))

    hub = TelemetryHub(config.WS_HOST, config.WS_PORT, config.WS_BROADCAST_HZ, shared.read,
                       on_client_message=on_client_message)
    hub.start()
    serve_hud_and_open_browser()

    log_next_due = 0.0

    period = {k: 1.0 / v for k, v in config.RATES_HZ.items()}
    next_due = {k: 0.0 for k in period}

    vehicle_id = "tiltrotor" if config.CUSTOM_TILT_DIALECT_ENABLED else "quadrotor"

    # 참고: Windows의 time.sleep()/time.time() 해상도는 ms 단위 근사이므로 RATES_HZ의
    # 250Hz 등은 "목표치"이지 하드 리얼타임 보장은 아니다 — 구조 검증 용도로는 충분하나,
    # 정밀 타이밍이 꼭 필요해지면 이 루프를 별도 스레드+고해상도 타이머로 바꿀 것.
    last_t = time.perf_counter()
    # ARM 성공 후 일정 시간 뒤 딱 한 번 더 물어볼 질문 예약 시각(초, time.time() 기준).
    # commander check 등은 ARM "클릭 순간"의 상태만 보여주는데, 실제 궁금한 건 그 뒤
    # 스로틀을 올렸을 때 PX4 믹서/제어배분(Control Allocation)이 실제로 무엇을
    # 출력했는가다 — actuator_outputs(최종 PWM/서보 값)와 control_allocator status
    # (제어효과 행렬 및 출력 채널이 실제로 어떤 함수(모터/서보)에 매핑됐는지)를 보면
    # HIL_ACTUATOR_CONTROLS.controls[0:4](=우리가 "motors"로 읽는 자리)가 계속 0인 게
    # "아직 안 밟아서"인지 "이 커스텀 틸트로터 믹서에서 모터가 애초에 다른 채널
    # 번호에 있어서"인지 구분할 수 있다.
    pending_probe_at = None
    print("[main] 메인 루프 시작 (Ctrl+C로 종료)")
    try:
        while True:
            now = time.perf_counter()
            dt = max(1e-4, min(0.02, now - last_t))
            last_t = now

            try:
                link.poll_incoming()

                if isinstance(rc_source, ScriptedRcSource):
                    rc_source.advance(dt)

                # 브라우저 ARM/DISARM 버튼 요청 처리 — 시리얼 쓰기는 이 스레드에서만.
                while not arm_requests.empty():
                    want_armed = arm_requests.get_nowait()
                    print("[main] ARM 요청: %s" % ("ARM" if want_armed else "DISARM"))
                    link.send_arm(want_armed)
                    if want_armed:
                        # ARM을 시도하는 바로 그 순간 PX4에게 직접 물어본다. 브릿지를 끄고
                        # 따로 확인하면 당연히 오래된 값만 보이므로, 이 순간이어야 의미가 있다.
                        #   commander check -- 거부 사유 전체 목록(추측 없이 이게 정답지)
                        #   listener sensor_mag/sensor_baro -- accel은 신선함이 확인됐고,
                        #     지금 남은 건 mag/baro의 STALE TIMEOUT 뿐이라 이 둘을 본다.
                        for probe in ("commander check", "listener sensor_mag", "listener sensor_baro"):
                            link.send_shell_cmd(probe)
                        # 3초 뒤(그 사이 사용자가 스로틀을 밟을 시간) 믹서 출력을 확인.
                        pending_probe_at = time.time() + 3.0

                if pending_probe_at is not None and time.time() >= pending_probe_at:
                    pending_probe_at = None
                    for probe in ("listener actuator_outputs", "control_allocator status"):
                        link.send_shell_cmd(probe)

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
                        elif key == "rc":
                            rc_values = rc_source.read()
                            if config.CONTROL_INPUT_PROTOCOL == "manual_control":
                                link.send_manual_control(rc_values)
                            elif config.CONTROL_INPUT_PROTOCOL == "rc_override":
                                link.send_rc_override(rc_values)
                            else:
                                raise ValueError(
                                    "알 수 없는 CONTROL_INPUT_PROTOCOL: %r "
                                    "(manual_control/rc_override 중 하나)" %
                                    config.CONTROL_INPUT_PROTOCOL
                                )

                shared.update(build_vis_payload(
                    snap,
                    vehicle_id,
                    armed=link.is_armed(),
                    link_ok=link.fcc_link_ok(),
                ))

                if logger is not None and wall_now >= log_next_due:
                    log_next_due = wall_now + 0.2   # 5Hz
                    logger.write(link, rc_source.read(), snap)
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
    finally:
        if logger is not None:
            logger.close()
            print("[main] debug log saved: %s" % os.path.abspath(log_path))


if __name__ == "__main__":
    main()
