# -*- coding: utf-8 -*-
"""
HILS 브릿지 설정 — 이 파일 하나만 고치면 대부분의 운용 파라미터가 바뀌도록 모아둔 곳.
(ICD-PXTR-HILS-001 / EICD-PXTR-HILS-001 참조)

포트/주소/물리상수를 코드 여기저기 흩어놓지 않고 전부 이 모듈에 모아, 실제 장비
(Pixhawk COM 포트, 모터 추력 등)가 바뀌어도 다른 모듈은 손대지 않게 하는 것이 목적.
"""

# ── EICD-01: FCC ↔ ENV(본 브릿지) 물리 인터페이스 ──────────────────────────────
# Windows 장치관리자(포트, COM & LPT)에서 Pixhawk가 잡는 COM 번호로 바꿔서 사용.
SERIAL_PORT = "COM11"

# Pixhawk 자체 USB 포트(가상 CDC-ACM)는 USB native 속도라 baudrate가 무시된다.
# TELEM 포트를 USB-시리얼(FTDI 등) 변환기로 뺄 경우에만 EICD-01 3.1.1절 값(921600)을 쓴다.
SERIAL_BAUD = 115200  # USB 포트 연결 시 pymavlink 요구사항상 값 자체는 있어야 하나 무시됨

# 우리(ENV/시뮬레이터) 쪽 MAVLink 식별자. PX4 기본 sysid가 1이므로, 겹치지 않게
# 반드시 1이 아닌 값을 쓴다(점대점 시리얼이라 실사용에 지장은 없지만, sysid로
# 메시지 출처를 구분해야 하는 로깅/다중 링크 확장 시 꼬이지 않게 하기 위함).
MAV_SOURCE_SYSTEM = 2
MAV_SOURCE_COMPONENT = 101  # MAV_COMP_ID_ONBOARD_COMPUTER 대역

# ── Channel A (ENV→FCC, HIL 센서 주입) 전송 주기 — EICD-01 3.1.3절 "권장" 값 ──
RATES_HZ = {
    "heartbeat": 1,
    "imu": 250,       # HIL_SENSOR (accel/gyro/mag/baro 한 메시지에 포함)
    "gps": 10,        # HIL_GPS
    "tilt_state": 20, # HIL_TILT_STATE(신규, 커스텀 다이얼렉트 있을 때만) — ICD C-06 재송출 주기와 정합
    "rc": 10,         # RC_CHANNELS_OVERRIDE(Channel D) — HILS_ICD/sim/quadrotor_hud.html의
                       # ICD_RATE.D01_RC와 동일한 10Hz(ICD-PXTR-HILS-001 9.4절)
    "actuator_probe": 5,  # 임시 SIM 어댑터: OFP uORB actuator_motors 조회
}

# OFP 수정 전 검증용 모터 입력 소스. 현재 OFP의 HIL_ACTUATOR_CONTROLS[0..3]은
# 실제 actuator_motors와 연결되어 있지 않아 0으로 들어온다. "nsh_actuator_motors"는
# 브리지가 NSH listener로 OFP의 진짜 motor vector를 읽어 FDM에 넣는다.
# OFP의 HIL MAVLink 매핑이 수정된 뒤에는 "hil_controls"로 되돌린다.
MOTOR_SOURCE_MODE = "nsh_actuator_motors"  # "nsh_actuator_motors" | "hil_controls"

# PX4에 키보드 조종값을 넣는 MAVLink 메시지 형식.
# 이전에 PX6C에서 검증된 MANUAL_CONTROL(joystick) 경로를 기본값으로 사용한다.
# RC_CHANNELS_OVERRIDE는 OFP 구성에 따라 무시될 수 있으므로, 그 경로를 검증할
# 목적일 때에만 "rc_override"로 바꾼다.
CONTROL_INPUT_PROTOCOL = "manual_control"  # "manual_control" | "rc_override"

# ── Channel D (ENV→FCC, 조종기 입력) 소스 선택 — rc_source.create_rc_source()가 이 값
# 하나로 소스를 고른다. main.py나 mavlink_link.py는 손댈 필요 없음(rc_source.py 참조).
#   "manual"          — 코드/콘솔에서 직접 값을 넣는 최소 구현
#   "browser"         — 브라우저(quadrotor_hud_v2.html)의 키보드 입력을 WebSocket으로
#                        받아서 그대로 사용 (지금 기본값 — 사람이 직접 조종하며 확인)
#   "scripted"        — 정해진 (시각, 입력) 시퀀스를 반복 재생 (사람 없는 자동 검증용)
#   "serial_receiver" — 실제 외부 송신기+리시버를 SIM PC에 연결했을 때 쓸 자리
#                        (아직 미구현 — rc_source.py의 SerialReceiverRcSource 참조)
RC_SOURCE_MODE = "browser"

RC_SOURCE_OPTIONS = {
    # "serial_receiver" 모드일 때만 의미 있음 — 실제 리시버가 정해지면 채울 것.
    "port": None,
    "protocol": "sbus",
    # "scripted" 모드일 때 재생할 입력 시퀀스 — OFP RC 파이프라인이 살아있는지 확인하는
    # 최소 시나리오: 스로틀 조금 줬다가, 피치를 줬다가, 요를 줘 본다.
    "keyframes": [
        (0.0, {}),
        (3.0, {"thr": 0.3}),
        (8.0, {"pitch": 0.3, "thr": 0.1}),
        (13.0, {"yaw": 0.4, "thr": 0.1}),
        (18.0, {}),
    ],
}

# ── Channel C(FCC→VIS) 대신 이 브릿지가 자체 FDM 진실값을 그대로 내보내는 주기.
# (실제 Pixhawk의 EKF2 추정치를 보고 싶으면 telemetry_hub.py의 SOURCE를 "mavlink"로 바꾸면 됨)
WS_BROADCAST_HZ = 30

# ── WebSocket 서버 (브라우저 quadrotor_hud_v2.html이 접속하는 주소) ───────────
WS_HOST = "localhost"
WS_PORT = 8765

# ── 지도 원점 — quadrotor_hud_v2.html의 TERRAIN_DATA.meta와 반드시 일치시킬 것.
# (다르면 브라우저 로컬 NED와 실제 GPS 좌표가 어긋나 지형/건물과 안 맞게 표시된다)
ORIGIN_LAT_DEG = 37.27908611111111
ORIGIN_LON_DEG = 127.10344722222221
ORIGIN_ELEV_M = 68.0

# ── FDM(비행동역학모델) 물리 상수 — "구조 검증용 근사치". 실제 기체 제원이 나오면
# 이 값들만 갱신하면 된다(적분 로직 자체는 fdm.py에 그대로 둬도 됨).
FDM = {
    "mass_kg": 2.2,
    "gravity": 9.81,
    "inertia_xx": 0.03,   # roll axis, kg*m^2
    "inertia_yy": 0.03,   # pitch axis
    "inertia_zz": 0.06,   # yaw axis
    "arm_len_m": 0.9,     # 회전축 중심~모터 거리(대략, quad geometry 참고)
    "max_thrust_per_motor_n": 12.0,  # 모터 1개 100% 명령 시 추력(N) — [TBD-모터사양]
    "yaw_torque_coeff": 0.05,        # 반작용 토크 계수(추력 대비 비율) — [TBD-모터사양]
    "rate_damping": 0.6,             # 각속도 감쇠(안정화용, 공력 모델 대체 근사치)
    "tilt_tau_s": 1.2,               # 틸트 서보 응답 시정수(초) — v1 HUD 값과 동일
    "ground_alt_m": 0.0,             # 브릿지 FDM은 평지 가정(실 지형은 브라우저 WorldModel에만 있음)
}

# ── HIL_SENSOR 주입값에 더할 가우시안 노이즈(표준편차, 각 축 독립) ─────────────
# FDM은 완벽한 수식값을 만들지만, 실제 센서는 항상 잡음이 섞인다. 여기 시그마를
# 전부 0으로 두면 정지 상태에서 mag/baro 값이 연속으로 완전히 동일해지고, PX4
# DataValidator(firmware: src/modules/sensors/data_validator/DataValidator.cpp)가
# "같은 값이 VALUE_EQUAL_COUNT_DEFAULT(=100)번 연속"이면 고장난(stuck) 센서로
# 보고 ERROR_FLAG_STALE_DATA를 세운다 -> MAG/BARO STALE -> Arming denied.
# accel/gyro는 HIL에서 failover 보고 자체가 꺼져 있어(voted_sensors_update.cpp
# `&& !_hil_enabled`) 당장은 안 걸리지만, EKF 안정성을 위해 함께 넣는다.
# 시그마는 PX4 EKF2 기본 기대 잡음(EKF2_{ACC,GYR,MAG,BARO}_NOISE)보다 한참 작게
# 잡아 추정 성능에는 영향이 없고 "완전 동일값"만 깨도록 한다.
SENSOR_NOISE = {
    "accel_mss": 0.02,     # m/s^2 (EKF2_ACC_NOISE 기본 0.35의 ~6%)
    "gyro_rads": 0.002,    # rad/s (EKF2_GYR_NOISE 기본 0.015의 ~13%)
    "mag_gauss": 0.002,    # gauss (지자기 크기 ~0.53G의 ~0.4%)
    "baro_hpa": 0.02,      # hPa (EKF2_BARO_NOISE 기본 3.5m 상당보다 훨씬 작음)
}

# 커스텀 MAVLink 메시지(HIL_TILT_STATE / HIL_TILT_ACTUATOR_CONTROLS)는 표준
# common.xml에 없다. 실제 PX4 커스텀 펌웨어 저장소의 메시지 정의(XML)를 mavgen으로
# 생성해 mavlink_link.py에 연결하기 전까지는 비활성화 — 그동안 tilt는 항상 0(수직/MC)
# 명령만 받는 것으로 간주한다(쿼드콥터 경로는 이 플래그와 무관하게 항상 완전히 동작).
CUSTOM_TILT_DIALECT_ENABLED = False
