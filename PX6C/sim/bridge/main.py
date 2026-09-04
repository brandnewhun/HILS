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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py"))

import config
from fdm import FlightDynamicsModel
from mavlink_link import MavlinkLink, SIM_DEVICE_IDS
from rc_source import BrowserRcSource, ScriptedRcSource, create_rc_source
from telemetry_hub import TelemetryHub
from world_model import WorldModel
from session_log import SessionLogger, build_snapshot


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


# PX4 Commander.cpp의 _is_throttle_low = (manual_control_setpoint.throttle < -0.8f)와
# 정확히 같은 값. send_manual_control()의 z 왕복 변환(-1..1 -> 0..1000 -> (z/1000)*2-1)이
# 항등식이라(unit -> unit) rc_values["thr"]가 곧 PX4가 보게 될 throttle 값과 같다 —
# 그래서 실기에 물어보지 않고 여기서 그대로 판정해도 된다.
ARM_THROTTLE_LOW_THRESHOLD = -0.8


def build_vis_payload(fdm_snapshot, vehicle_id, armed, link_ok, arm_ready, reset_denied_at):
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
        # 지금 이 순간 ARM을 눌러도 PX4가 "high throttle"로 거부하지 않을 스로틀
        # 상태인지 — HUD가 이 값으로 ARM 버튼을 깜박여 "지금 누르면 된다"를 알려준다.
        "armReady": bool(arm_ready),
        # RESET이 "ARMED라서 무시됨"으로 거부된 시각(time.time(), 없으면 0) — 값이
        # 바뀔 때만 HUD가 "먼저 DISARM" 안내를 잠깐 띄운다. 예전엔 터미널에만 찍혀서
        # 화면상으로는 RESET을 눌러도 아무 반응이 없는 것처럼 보였다.
        "resetDeniedAt": reset_denied_at,
    }


def connect_with_retry(link, retry_delay_s=3.0):
    while True:
        try:
            link.connect()
            return
        except Exception as e:
            print("[main] 시리얼 연결 실패(%s: %s) — %.0f초 후 재시도" % (type(e).__name__, e, retry_delay_s))
            link._emit("link", "connect failed: %s: %s" % (type(e).__name__, e))
            time.sleep(retry_delay_s)


def reboot_fcc_and_reconnect(link, settle_s=2.0, heartbeat_wait_s=30.0):
    """FC(PX6c)를 NSH `reboot`으로 재부팅하고, USB 시리얼이 다시 올라와 첫 HEARTBEAT가
    올 때까지 재연결을 기다린다. 이유는 config.REBOOT_FCC_ON_START 주석 참조(FDM을
    초기화할 때 FC의 EKF2도 같이 초기화하지 않으면 지자기 고장이 래치됨)."""
    print("[main] FC 재부팅 요청(NSH reboot) — 시뮬레이션과 FC 내부 상태(EKF2)를 함께 초기화")
    link._emit("reboot", "request")  # 모듈 함수라 session이 없어 link의 이벤트 훅으로 기록
    try:
        link.send_shell_cmd("reboot")
        time.sleep(0.5)  # 명령이 시리얼로 실제로 나갈 시간
    except Exception as e:
        print("[main] reboot 명령 전송 실패(%s: %s) — 재연결만 시도" % (type(e).__name__, e))
        link._emit("reboot", "send failed: %s" % type(e).__name__)
    try:
        if link.conn is not None:
            link.conn.close()
    except Exception:
        pass
    time.sleep(settle_s)  # USB CDC 포트가 사라졌다가 다시 잡히는 시간
    connect_with_retry(link)
    deadline = time.time() + heartbeat_wait_s
    while time.time() < deadline:
        # 재부팅 타이밍에 따라 포트가 "일단 열렸다가" FC가 실제로 리셋되는 순간 다시
        # 끊길 수 있다(Windows USB CDC 재열거). 그때 wait_heartbeat 안의 시리얼 read가
        # 예외를 던지므로(실기에서 SerialException으로 크래시 확인), 여기서 잡아
        # 포트를 닫고 다시 열어 계속 기다린다.
        try:
            if link.conn.wait_heartbeat(timeout=3) is not None:
                print("[main] FC 재부팅 완료 — HEARTBEAT 수신, 재연결됨")
                link._emit("reboot", "done: heartbeat received")
                return
        except Exception as e:
            print("[main] 재부팅 중 시리얼 단절(%s) — 포트 재오픈" % type(e).__name__)
            link._emit("link", "serial lost during reboot: %s" % type(e).__name__)
            try:
                if link.conn is not None:
                    link.conn.close()
            except Exception:
                pass
            time.sleep(1.0)
            connect_with_retry(link)
    print("[main] FC 재부팅 후 %.0f초 동안 HEARTBEAT를 못 받았습니다 — 그대로 진행(메인 루프가 계속 재시도)" % heartbeat_wait_s)


SIM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # bridge/ -> sim/
HUD_FILE = "quadrotor_hud_v2.html"
HTTP_PORT = 8000


def load_world():
    """config.TERRAIN_DATA_PATH에서 WorldModel을 만든다. 실패하면 None을 반환해
    FDM이 기존 평지 가정으로 동작하게 한다(지형 없이도 HIL 배선 검증은 계속 가능해야 함)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.TERRAIN_DATA_PATH)
    try:
        world = WorldModel.from_json_file(path)
        print("[main] 지형 로드 완료: %s (건물 %d개, 도로 %d개)" %
              (path, len(world.data["buildings"]), len(world.data["roads"])))
        return world
    except Exception as e:
        print("[main] 지형 로드 실패(%s: %s) — 평지(ground_alt_m) 가정으로 계속 진행" %
              (type(e).__name__, e))
        return None


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


# (예전 DebugLogger는 session_log.SessionLogger로 대체됐다 — 항상 켜지는 세션 폴더에
#  console.log / events.jsonl / snapshot.jsonl / browser.jsonl / mavlink.tlog / session.json.
#  HIL에서 "안 움직인다"를 좁힐 때 보는 것: snapshot의 rc(입력 도달), armed(실제 시동),
#  ofp_motors(FDM에 들어간 모터), msgs(HIL_ACTUATOR_CONTROLS 수신 여부); events의 STATUSTEXT
#  (시동 거부 사유)/mode(MANUAL<->POSCTL 전환)/arm_state.)


def main():
    # --tag <이름>: 세션 로그 폴더명에 붙는 라벨(예: --tag hover-test). 예전 --log <파일>은
    # 세션 로깅으로 대체되어 더 이상 필요 없다(주면 안내만 하고 무시).
    tag = None
    if "--tag" in sys.argv:
        idx = sys.argv.index("--tag")
        tag = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
    if "--log" in sys.argv:
        print("[main] 참고: --log 는 더 이상 필요 없습니다 — 로그는 항상 %s/<세션>/ 에 남습니다(--tag 로 라벨만 지정)"
              % config.LOG_DIR)

    # 세션 로거는 가장 먼저 — 시리얼 연결 재시도 메시지부터 console.log에 남도록.
    session = SessionLogger(os.path.join(os.path.dirname(os.path.abspath(__file__)), config.LOG_DIR), tag=tag)
    session.install_console_tee()
    session.write_session_header(config)
    print("[main] 세션 로그 폴더: %s" % session.dir)

    print("[main] Pixhawk 연결 시도: %s (baud=%s)" % (config.SERIAL_PORT, config.SERIAL_BAUD))
    link = MavlinkLink(config)
    link.on_event = session.on_link_event          # FCC 이벤트(STATUSTEXT/ACK/NSH/모드/ARM)를 events.jsonl로
    if getattr(config, "LOG_MAVLINK_TLOG", True):
        link.tlog_path = session.path("mavlink.tlog")  # 수신 MAVLink 원본(재연결마다 append)
    connect_with_retry(link)
    print("[main] 시리얼 포트 오픈 완료 — HIL_ACTUATOR_CONTROLS/HEARTBEAT 대기 중...")
    if getattr(config, "REBOOT_FCC_ON_START", False):
        reboot_fcc_and_reconnect(link)

    # 진단 B — MANUAL 비행모드로 전환해 둔다. 이 기체 전용 tv_att_control이
    # NAVIGATION_STATE_MANUAL이 아니면 스틱 스로틀을 무시하고 위치/자세
    # 컨트롤러의 thrust_body[2](유효한 위치·속도 추정치가 있어야 의미 있는 값)를
    # 대신 쓴다 — 실기에서 스로틀을 끝까지 눌러도 모터가 안 움직인 원인이 이거였다.
    link.send_set_manual_mode()

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

    world = load_world()
    dynamics = FlightDynamicsModel(config.FDM, world=world)
    rc_source = create_rc_source(config.RC_SOURCE_MODE, config.RC_SOURCE_OPTIONS)
    print("[main] RC 소스: %s" % config.RC_SOURCE_MODE)
    print("[main] PX4 조종 입력: %s (%.0f Hz)" %
          (config.CONTROL_INPUT_PROTOCOL, config.RATES_HZ["rc"]))
    print("[main] FDM 모터 소스: %s" % config.MOTOR_SOURCE_MODE)
    shared = SharedState()

    session.update_session(cal_ids=cal_ids, sim_device_ids=SIM_DEVICE_IDS,
                           control_input_protocol=config.CONTROL_INPUT_PROTOCOL,
                           motor_source_mode=config.MOTOR_SOURCE_MODE)

    # 브라우저 -> 브릿지 방향 메시지 처리. RC_SOURCE_MODE="browser"일 때는 키보드 입력을
    # rc_source로 흘려보내고, ARM/DISARM 요청은 어느 모드에서든 그대로 실기에 전달한다.
    # (요청은 asyncio 스레드에서 들어오므로, 여기서 직접 MAVLink를 쓰지 않고 큐에 넣어
    #  메인 루프가 보내게 한다 — 시리얼 쓰기를 한 스레드로 몰아 경합을 피하기 위함.)
    arm_requests = queue.Queue()
    reset_requests = queue.Queue()

    def on_client_message(msg):
        session.browser(msg)  # 브라우저 -> 브릿지 원본(키/rc/arm/reset) 전부 browser.jsonl로
        mtype = msg.get("type")
        if mtype == "rc" and isinstance(rc_source, BrowserRcSource):
            rc_source.on_message(msg)
        elif mtype == "arm":
            arm_requests.put(bool(msg.get("armed")))
        elif mtype == "reset":
            reset_requests.put(True)

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
    last_reset_denied_at = 0.0
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
                    session.event("arm_request", "ARM" if want_armed else "DISARM",
                                  rc=rc_source.read(), armed_now=link.is_armed())
                    if want_armed:
                        link.send_set_manual_mode()  # 다른 경로로 모드가 바뀌었어도 매 ARM마다 복구
                    link.send_arm(want_armed)
                    if want_armed:
                        # ARM을 시도하는 바로 그 순간 PX4에게 직접 물어본다. 브릿지를 끄고
                        # 따로 확인하면 당연히 오래된 값만 보이므로, 이 순간이어야 의미가 있다.
                        #   commander check -- 거부 사유 전체 목록(추측 없이 이게 정답지)
                        #   listener sensor_mag/sensor_baro -- accel은 신선함이 확인됐고,
                        #     지금 남은 건 mag/baro의 STALE TIMEOUT 뿐이라 이 둘을 본다.
                        #   listener estimator_status -- "Yaw estimate error"의 실제 판정 근거인
                        #     mag_test_ratio 실측값(estimatorCheck.cpp의 COM_ARM_EKF_YAW 비교값)을
                        #     확인하기 위한 진단용 조회. OFP는 안 건드림.
                        #   listener vehicle_status -- EKF2가 정상이어도 nav_state가 MANUAL로
                        #     유지되는지(POSCTL로 자동 복귀하는지)를 EKF2 상태와 독립적으로 확인.
                        #   listener manual_control_setpoint -- "Arming denied: high throttle"
                        #     (Commander.cpp의 _is_throttle_low = throttle < -0.8) 판정 시점에
                        #     PX4가 실제로 들고 있던 throttle 값을 직접 확인하기 위함.
                        for probe in ("commander check", "listener sensor_mag", "listener sensor_baro",
                                      "listener estimator_status", "listener vehicle_status",
                                      "listener manual_control_setpoint"):
                            link.send_shell_cmd(probe)
                        # 3초 뒤(그 사이 사용자가 스로틀을 밟을 시간) 믹서 출력을 확인.
                        pending_probe_at = time.time() + 3.0

                if pending_probe_at is not None and time.time() >= pending_probe_at:
                    pending_probe_at = None
                    for probe in ("listener actuator_outputs", "control_allocator status"):
                        link.send_shell_cmd(probe)

                # RESET 처리 — 위치/자세의 진실은 이 FDM에 있으므로(브라우저는 표시만
                # 함) 여기서 직접 되돌린다. 날고 있는 도중 순간이동하듯 보이지 않도록
                # DISARM 상태일 때만 받아들인다. 거부됐을 때 터미널에만 찍고 넘어가면
                # 화면에서는 RESET을 눌러도 아무 반응이 없는 것처럼 보이므로(실사용에서
                # 실제로 이렇게 헷갈렸음), last_reset_denied_at을 텔레메트리에 실어
                # HUD가 "먼저 DISARM" 안내를 띄우게 한다.
                while not reset_requests.empty():
                    reset_requests.get_nowait()
                    if link.is_armed():
                        last_reset_denied_at = time.time()
                        print("[main] RESET 요청 무시 — 먼저 DISARM 하세요")
                        session.event("reset", "denied: armed")
                    else:
                        dynamics.reset()
                        print("[main] FDM을 원점으로 리셋했습니다")
                        session.event("reset", "fdm reset to origin")
                        if getattr(config, "REBOOT_FCC_ON_RESET", False):
                            # FDM만 원점으로 되돌리면 FC의 EKF2는 이전 위치/자세를 기억한
                            # 채 "순간이동"을 겪어 지자기 고장이 래치된다 — FC도 같이 초기화.
                            # 이 동안(~10초) 메인 루프가 멈춰 HUD 텔레메트리도 잠시 멈춘다.
                            reboot_fcc_and_reconnect(link)
                            last_t = time.perf_counter()

                motors = link.sim_motors(config.MOTOR_SOURCE_MODE)
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
                        elif key == "actuator_probe":
                            # OFP 변경 없이 control allocator의 진짜 모터 벡터를 FDM에
                            # 묶는 임시 어댑터. HIL controls 표준 매핑이 확보되면 제거 가능.
                            if config.MOTOR_SOURCE_MODE == "nsh_actuator_motors":
                                link.send_shell_cmd("listener actuator_motors")

                # rc_values는 바로 위 "rc" 틱에서 실제로 PX4에 송신한 값과 같다(이번
                # 반복이 rc 틱이 아니었다면 가장 최근 송신값) — 화면에 "지금 누르면
                # 된다"고 보여주는 시점은 PX4가 실제로 받은 값과 일치해야 하므로, 아직
                # 전송 안 된 더 최신 키 상태를 따로 다시 읽지 않고 이 값을 그대로 쓴다.
                arm_ready = rc_values.get("thr", 0.0) < ARM_THROTTLE_LOW_THRESHOLD
                shared.update(build_vis_payload(
                    snap,
                    vehicle_id,
                    armed=link.is_armed(),
                    link_ok=link.fcc_link_ok(),
                    arm_ready=arm_ready,
                    reset_denied_at=last_reset_denied_at,
                ))

                if wall_now >= log_next_due:
                    log_next_due = wall_now + 1.0 / max(0.1, getattr(config, "LOG_SNAPSHOT_HZ", 5))
                    session.snapshot(build_snapshot(link, rc_source.read(), snap, config.MOTOR_SOURCE_MODE))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # USB 케이블이 빠지거나 OS 시리얼 오류가 나도 브릿지 프로세스 전체가
                # 죽지 않게 한다 — FDM 상태(위치/자세 등)는 유지한 채 재연결만 시도.
                # 이 창(재연결 시도 중)에는 브라우저에도 새 텔레메트리가 안 나가고,
                # PX4는 EICD-01 3.1.5절 페일세이프(HIL_SENSOR 500ms 미수신)를 스스로
                # 타게 된다 — 그게 이 상황에서 기대되는 정상 동작이다.
                print("[main] MAVLink I/O 오류(%s: %s) — 재연결 시도" % (type(e).__name__, e))
                session.event("link", "io error: %s: %s" % (type(e).__name__, e))
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
        session.event("bridge", "shutdown (KeyboardInterrupt)")
    finally:
        log_dir = session.dir
        session.close()  # stdout/stderr 원복 후
        print("[main] 세션 로그 저장 완료: %s" % log_dir)


if __name__ == "__main__":
    main()
