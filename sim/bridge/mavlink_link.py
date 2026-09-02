# -*- coding: utf-8 -*-
"""
MavlinkLink — EICD-01(FCC ↔ ENV) 물리/논리 인터페이스를 담당하는 유일한 모듈.
pymavlink 연결 열기/heartbeat/HIL_SENSOR/HIL_GPS 송신(Channel A)과
HIL_ACTUATOR_CONTROLS 수신(Channel B)만 다룬다. FDM이나 WebSocket 쪽은 전혀
모른다 — main.py가 이 모듈이 만든 값을 fdm.py에 넘기고, fdm.py의 결과를 다시
이 모듈로 넘겨 송신하는 식으로만 연결된다(모듈 간 결합을 최소화).

커스텀 메시지(HIL_TILT_STATE, HIL_TILT_ACTUATOR_CONTROLS)는 표준 MAVLink
dialect에 없다. config.CUSTOM_TILT_DIALECT_ENABLED가 True인데 실제 pymavlink에
해당 메시지가 없으면(=아직 mavgen으로 커스텀 dialect를 만들어 붙이지 않은
상태) 경고만 남기고 조용히 건너뛴다 — 그동안도 쿼드콥터(틸트 없음) 경로는
완전히 정상 동작한다.
"""
import inspect
import math
import time

import geo


def _send_filtered(send_fn, **kwargs):
    """pymavlink 버전에 따라 HIL_SENSOR/HIL_GPS의 'id'(instance) 필드가 있거나
    없을 수 있다(dialect 개정 시점 차이). send_fn이 실제로 받는 인자만 걸러서
    호출해, 설치된 pymavlink 버전이 달라도 이 모듈을 고치지 않고 넘어가게 한다."""
    try:
        accepted = set(inspect.signature(send_fn).parameters.keys())
    except (TypeError, ValueError):
        accepted = None
    if accepted is not None:
        kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    send_fn(**kwargs)


class MavlinkLink:
    def __init__(self, config):
        self.config = config
        self.conn = None
        self._warned_no_tilt_dialect = False

        # Channel B(FCC->ENV) 최신값 캐시 — recv_loop()가 갱신하고 main.py가 읽어간다.
        self.latest_actuator = {
            "motors": [0.0, 0.0, 0.0, 0.0],
            "tilt_setpoint": 0.0,
            "time_usec": 0,
            "received": False,
        }
        self.last_heartbeat_from_fcc = None
        self._fcc_armed = False

    def connect(self):
        from pymavlink import mavutil
        c = self.config
        self.conn = mavutil.mavlink_connection(
            c.SERIAL_PORT,
            baud=c.SERIAL_BAUD,
            source_system=c.MAV_SOURCE_SYSTEM,
            source_component=c.MAV_SOURCE_COMPONENT,
        )
        return self.conn

    # ── Channel A: ENV -> FCC (송신) ──────────────────────────────────────────
    def send_heartbeat(self):
        from pymavlink import mavutil
        self.conn.mav.heartbeat_send(
            type=mavutil.mavlink.MAV_TYPE_GCS,
            autopilot=mavutil.mavlink.MAV_AUTOPILOT_INVALID,
            base_mode=0,
            custom_mode=0,
            system_status=mavutil.mavlink.MAV_STATE_ACTIVE,
        )

    @staticmethod
    def _mag_body_ned_fixed(roll, pitch, heading):
        """단순화된 지자기 모델 — 실측 지자기 대신 대표적인 NED 지자기 벡터
        (한반도 대략치, gauss)를 자세로 회전시켜 body 좌표로 변환한다."""
        mag_ned = (0.28, -0.03, 0.45)
        cr, sr = math.cos(roll), math.sin(roll)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cy, sy = math.cos(heading), math.sin(heading)
        # NED -> body(FRD): body_to_ned의 전치(회전행렬은 직교행렬이므로 전치=역행렬)
        r11, r12, r13 = cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr
        r21, r22, r23 = sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr
        r31, r32, r33 = -sp, cp * sr, cp * cr
        n, e, d = mag_ned
        bx = r11 * n + r21 * e + r31 * d
        by = r12 * n + r22 * e + r32 * d
        bz = r13 * n + r23 * e + r33 * d
        return bx, by, bz

    def send_hil_sensor(self, fdm_snapshot):
        from pymavlink import mavutil
        c = self.config
        time_usec = int(time.time() * 1e6)
        fx, fy, fz = fdm_snapshot["specific_force_body"]
        mx, my, mz = self._mag_body_ned_fixed(
            fdm_snapshot["roll"], fdm_snapshot["pitch"], fdm_snapshot["heading"]
        )
        alt_amsl = fdm_snapshot["alt"] + c.ORIGIN_ELEV_M
        abs_pressure = geo.isa_pressure_hpa(alt_amsl)
        temperature = geo.isa_temperature_c(alt_amsl)
        speed = math.hypot(fdm_snapshot["vN"], fdm_snapshot["vE"])
        diff_pressure = 0.5 * 1.225 * speed * speed / 100.0  # 대략치(hPa), [TBD-ADS 센서사양]

        fields_updated = getattr(mavutil.mavlink, "HIL_SENSOR_UPDATED_FLAGS_ALL", 0x1FFF)

        _send_filtered(
            self.conn.mav.hil_sensor_send,
            time_usec=time_usec,
            xacc=fx, yacc=fy, zacc=fz,
            xgyro=fdm_snapshot["p"], ygyro=fdm_snapshot["q"], zgyro=fdm_snapshot["r"],
            xmag=mx, ymag=my, zmag=mz,
            abs_pressure=abs_pressure,
            diff_pressure=diff_pressure,
            pressure_alt=alt_amsl,
            temperature=temperature,
            fields_updated=fields_updated,
            id=0,
        )

    def send_hil_gps(self, fdm_snapshot):
        c = self.config
        time_usec = int(time.time() * 1e6)
        lat, lon = geo.ned_to_latlon(
            fdm_snapshot["north"], fdm_snapshot["east"], c.ORIGIN_LAT_DEG, c.ORIGIN_LON_DEG
        )
        alt_amsl = fdm_snapshot["alt"] + c.ORIGIN_ELEV_M
        vn_cms = int(max(-32000, min(32000, fdm_snapshot["vN"] * 100)))
        ve_cms = int(max(-32000, min(32000, fdm_snapshot["vE"] * 100)))
        vd_cms = int(max(-32000, min(32000, fdm_snapshot["vD"] * 100)))
        speed_cms = int(math.hypot(fdm_snapshot["vN"], fdm_snapshot["vE"]) * 100)
        cog_cdeg = int((math.degrees(math.atan2(fdm_snapshot["vE"], fdm_snapshot["vN"])) % 360) * 100)

        _send_filtered(
            self.conn.mav.hil_gps_send,
            time_usec=time_usec,
            fix_type=3,
            lat=int(lat * 1e7),
            lon=int(lon * 1e7),
            alt=int(alt_amsl * 1000),
            eph=100, epv=100,
            vel=speed_cms,
            vn=vn_cms, ve=ve_cms, vd=vd_cms,
            cog=cog_cdeg,
            satellites_visible=10,
            id=0,
        )

    def send_hil_tilt_state(self, fdm_snapshot):
        """커스텀 메시지 — mavgen으로 생성한 dialect가 conn.mav에 붙어있을 때만 전송.
        아직 없으면 최초 1회만 경고를 남기고 이후로는 조용히 건너뛴다."""
        if not self.config.CUSTOM_TILT_DIALECT_ENABLED:
            return
        send_fn = getattr(self.conn.mav, "hil_tilt_state_send", None)
        if send_fn is None:
            if not self._warned_no_tilt_dialect:
                print(
                    "[mavlink_link] CUSTOM_TILT_DIALECT_ENABLED=True 지만 pymavlink에 "
                    "hil_tilt_state_send()가 없습니다 — PX4 커스텀 펌웨어 저장소의 "
                    "메시지 정의(XML)를 mavgen으로 생성해 연결하기 전까지 틸트 상태는 "
                    "전송하지 않습니다(쿼드콥터 경로에는 영향 없음)."
                )
                self._warned_no_tilt_dialect = True
            return
        tilt_deg = fdm_snapshot["tilt"] * 90.0
        send_fn(
            time_usec=int(time.time() * 1e6),
            angle=[tilt_deg, tilt_deg, tilt_deg, tilt_deg],
            angular_velocity=[0.0, 0.0, 0.0, 0.0],
            current_ma=[0, 0, 0, 0],
            temperature_c=[25.0, 25.0, 25.0, 25.0],
        )

    # ── Channel B: FCC -> ENV (수신) ──────────────────────────────────────────
    def poll_incoming(self, on_telemetry_message=None):
        """넌블로킹으로 대기 중인 메시지를 전부 처리한다. main.py의 루프에서 매 틱 호출.
        on_telemetry_message(msg)는 (선택) Channel C를 "브릿지 FDM 진실값" 대신
        "Pixhawk 자체 EKF2 추정치"로 보고 싶을 때 GLOBAL_POSITION_INT 등을 넘겨받는 훅."""
        while True:
            msg = self.conn.recv_match(blocking=False)
            if msg is None:
                break
            mtype = msg.get_type()
            if mtype == "HEARTBEAT":
                # 이 연결은 Pixhawk 1대와의 점대점(serial) 링크이므로, 여기 들어오는
                # HEARTBEAT는 항상 FCC가 보낸 것이다(수신 쪽에는 우리 자신이 보낸
                # HEARTBEAT가 되돌아올 경로가 없음 — sysid로 걸러낼 필요가 없고,
                # 걸러내면 오히려 PX4 기본 sysid(=1)가 우리 쪽 기본값과 같을 때
                # 헬스체크가 항상 실패하는 버그가 된다).
                self.last_heartbeat_from_fcc = time.time()
                # MAV_MODE_FLAG_SAFETY_ARMED(0x80) 비트로 실제 Arm 여부를 판정한다.
                # (이전 버전은 "액추에이터 메시지를 한 번이라도 받았는가"를 armed로
                # 오인했는데, PX4는 Disarm 상태에서도 HIL_ACTUATOR_CONTROLS를 계속
                # 보내므로 그 플래그는 한 번 true가 되면 계속 true로 고정되는 버그였음.)
                self._fcc_armed = bool(msg.base_mode & 0x80)
            elif mtype == "HIL_ACTUATOR_CONTROLS":
                controls = list(msg.controls)
                self.latest_actuator["motors"] = controls[0:4]
                self.latest_actuator["time_usec"] = msg.time_usec
                self.latest_actuator["received"] = True
            elif mtype == "HIL_TILT_ACTUATOR_CONTROLS":
                # 커스텀 다이얼렉트가 붙어있으면 자동으로 여기 잡힌다(별도 등록 불필요).
                angles = list(getattr(msg, "angle", [0.0, 0.0, 0.0, 0.0]))
                self.latest_actuator["tilt_setpoint"] = (sum(angles) / len(angles)) / 90.0 if angles else 0.0
            elif on_telemetry_message is not None:
                on_telemetry_message(msg)

    def fcc_link_ok(self, timeout_s=1.5):
        """EICD-01 3.1.5절 페일세이프 판단 기준(HEARTBEAT 1.5초 미수신)과 동일 임계값."""
        if self.last_heartbeat_from_fcc is None:
            return False
        return (time.time() - self.last_heartbeat_from_fcc) < timeout_s

    def is_armed(self):
        """가장 최근 HEARTBEAT의 MAV_MODE_FLAG_SAFETY_ARMED 비트. HEARTBEAT를 아직
        한 번도 못 받았으면(따라서 fcc_link_ok()도 False) 항상 False."""
        return self._fcc_armed and self.fcc_link_ok()
