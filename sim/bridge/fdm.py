# -*- coding: utf-8 -*-
"""
FlightDynamicsModel — HITL 폐루프의 "ENV(환경 시뮬레이터)" 쪽 비행동역학 모델.

역할: PX4가 보낸 액추에이터 명령(Channel B: HIL_ACTUATOR_CONTROLS의 모터 추력 ×4,
(선택) 틸트 목표각)을 입력으로 받아 강체 운동을 적분하고, 그 결과로
(1) PX4에 다시 주입할 가상 센서값(Channel A)의 재료(가속도/각속도/자세/위치)와
(2) 화면(VIS) 표시용 텔레메트리(Channel C)를 만들어낸다.

다른 프로젝트 파일들과 마찬가지로 "구조 검증용 근사치"를 목표로 한다 — 실제
공력/모터 특성 데이터가 없는 상태에서 폐루프 자체가 정상 동작하는지 확인하는
용도이며, 정밀 비행역학 모델이 아니다. 실측 데이터가 생기면 config.py의 FDM
딕셔너리 값과 _mix_quad_x()의 모터 배치 가정만 갱신하면 된다.
"""
import math


class FlightDynamicsModel:
    def __init__(self, fdm_config):
        c = fdm_config
        self.mass = c["mass_kg"]
        self.g = c["gravity"]
        self.Ixx = c["inertia_xx"]
        self.Iyy = c["inertia_yy"]
        self.Izz = c["inertia_zz"]
        self.arm = c["arm_len_m"]
        self.max_thrust_motor = c["max_thrust_per_motor_n"]
        self.yaw_k = c["yaw_torque_coeff"]
        self.damping = c["rate_damping"]
        self.tilt_tau = c["tilt_tau_s"]
        self.ground_alt = c["ground_alt_m"]

        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self.p = 0.0
        self.q = 0.0
        self.r = 0.0
        self.vn = 0.0
        self.ve = 0.0
        self.vd = 0.0
        self.north = 0.0
        self.east = 0.0
        self.pos_d = 0.0  # +아래(NED). 고도는 -pos_d.
        self.tilt = 0.0
        self._last_specific_force = (0.0, 0.0, -self.g)

    @property
    def alt(self):
        return -self.pos_d

    @staticmethod
    def _mix_quad_x(m):
        """
        controls[0..3] -> (roll_mix, pitch_mix, yaw_mix, thrust_mix).

        예전에는 일반 쿼드-X 관례(대각쌍이 같은 회전방향)를 그냥 가정했었는데,
        이 기체는 일반 쿼드가 아니라 커스텀 틸트로터라 실제 배분 행렬이 다르다.
        실기 연동 중 사용자가 순수 스로틀만 올렸는데도 SIM에서 강한 롤이
        발생해 실기 자세추정기가 진짜로 "Attitude failure (roll)"을 띄우며
        disarm되는 걸 보고, 이 기체 전용 배분 모듈
        (src/modules/tv_control_allocator/TiltVtolControlAllocator.cpp, hover
        시 tilt~=0인 구간)의 실제 4x2 배분 행렬(b_inv)을 펌웨어 소스에서 직접
        확인해 아래로 고쳤다(OFP는 안 건드림 — 그 행렬을 그대로 옮겨 온
        SIM 쪽 근사치일 뿐):

            b_inv = [[ 0.25, -0.25],   # motor0: +pitch, -roll
                     [-0.25, -0.25],   # motor1: -pitch, -roll
                     [-0.25,  0.25],   # motor2: -pitch, +roll
                     [ 0.25,  0.25]]   # motor3: +pitch, +roll

        위 행렬을 역으로 풀면 pitch = (m0+m3)-(m1+m2), roll = (m2+m3)-(m0+m1).
        (예전 코드는 이 두 축을 서로 바꿔서 쓰고 있었다 — "roll_mix"라고 이름
        붙인 식이 실제로는 pitch 차동이었다.)

        요(yaw)는 이 배분식에서 tilt=0(완전 호버)일 때는 아예 등장하지 않는다
        — 펌웨어 소스를 보면 저속/호버 구간의 요는 모터 추력 차동이 아니라
        틸트 서보 각도 차동(tilt_servo_cmd)으로 만든다. 우리는 지금
        actuator_motors(추력 4개)만 읽고 틸트 서보값은 안 읽어오므로, 이
        SIM에서는 요를 물리적으로 재현할 수 없다 — yaw_mix는 항상 0으로 둔다
        (없는 신호를 그럴듯하게 지어내는 것보다 안 만드는 쪽을 택함).
        """
        m0, m1, m2, m3 = m
        thrust_mix = (m0 + m1 + m2 + m3) / 4.0
        pitch_mix = (m0 + m3) - (m1 + m2)
        roll_mix = (m2 + m3) - (m0 + m1)
        yaw_mix = 0.0
        return roll_mix, pitch_mix, yaw_mix, thrust_mix

    def step(self, dt, motors, tilt_setpoint=0.0):
        """motors: 길이 4의 정규화 추력 명령(-1..1 또는 NaN=disarm). 반환값 없음 —
        결과는 snapshot()으로 조회한다."""
        m = []
        for v in list(motors)[:4]:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                v = 0.0
            m.append(max(0.0, min(1.0, v)))
        while len(m) < 4:
            m.append(0.0)

        roll_mix, pitch_mix, yaw_mix, thrust_mix = self._mix_quad_x(m)

        k_tilt = min(1.0, dt / max(1e-3, self.tilt_tau))
        self.tilt += (tilt_setpoint - self.tilt) * k_tilt

        torque_unit = self.max_thrust_motor * self.arm
        roll_torque = roll_mix * torque_unit - self.damping * self.p
        pitch_torque = pitch_mix * torque_unit - self.damping * self.q
        yaw_torque = yaw_mix * self.max_thrust_motor * self.yaw_k - self.damping * self.r

        self.p += (roll_torque / self.Ixx) * dt
        self.q += (pitch_torque / self.Iyy) * dt
        self.r += (yaw_torque / self.Izz) * dt

        # 자세 적분 — 쿼터니언이 아닌 각속도->오일러각 직접 적분(소각/완만한 기동 가정).
        self.roll += self.p * dt
        self.pitch += self.q * dt
        self.yaw = (self.yaw + self.r * dt) % (2.0 * math.pi)

        # 추력: tilt=0(수직/MC)이면 body -Z(위), tilt=1(수평/FW)이면 body +X(전방).
        thrust_n = thrust_mix * 4.0 * self.max_thrust_motor
        s, c_ = math.sin(self.tilt * math.pi / 2.0), math.cos(self.tilt * math.pi / 2.0)
        thrust_body = (thrust_n * s / self.mass, 0.0, -thrust_n * c_ / self.mass)

        cr, sr = math.cos(self.roll), math.sin(self.roll)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        # body(FRD) -> NED, ZYX(yaw-pitch-roll) 회전행렬
        r11, r12, r13 = cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr
        r21, r22, r23 = sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr
        r31, r32, r33 = -sp, cp * sr, cp * cr

        bx, by, bz = thrust_body
        an = r11 * bx + r12 * by + r13 * bz
        ae = r21 * bx + r22 * by + r23 * bz
        ad = r31 * bx + r32 * by + r33 * bz + self.g  # 중력(NED +아래) 가산

        self.vn += an * dt
        self.ve += ae * dt
        self.vd += ad * dt

        self.north += self.vn * dt
        self.east += self.ve * dt
        self.pos_d += self.vd * dt

        # 평지 가정 지면 충돌(실제 지형/건물은 브라우저 WorldModel에만 있음 — 3.1의
        # v2 HUD 쪽 설명 참조). 여기서는 "땅 밑으로 내려가지 않는다"만 보장.
        floor_d = -self.ground_alt
        if self.pos_d > floor_d:
            self.pos_d = floor_d
            if self.vd > 0:
                self.vd = 0.0

        # ── 가속도계가 읽는 값(specific force = 비중력 가속) ─────────────────────
        # 예전에는 추력만 그대로 넣었는데, 그러면 지면에 앉아 시동도 안 건 기체가
        # (0,0,0) = "자유낙하 중"을 계속 보고하게 된다. 실제 가속도계는 정지 상태에서
        # 중력 반작용 약 1g를 읽으므로, PX4는 그 값을 물리적으로 불가능하다고 보고
        # "Preflight Fail: No valid data from Accel 0"으로 시동을 거부한다.
        #
        # 정의대로 "실제 가속 - 중력"을 body 좌표로 변환해서 넣는다:
        #   지면 정지  -> (0,0,-g)      = 1g (정상)
        #   정지 호버  -> (0,0,-g)      = 1g (추력이 중력을 상쇄하므로 실제 가속 0)
        #   자유낙하   -> (0,0,0)       = 0g
        on_ground = (self.pos_d >= floor_d - 1e-9) and (ad >= 0.0)
        if on_ground:
            # 지면 반력/마찰이 받쳐주므로 실제 가속은 0 — 미끄러지지 않게 속도도 정지.
            self.vn = self.ve = self.vd = 0.0
            a_n = a_e = a_d = 0.0
        else:
            a_n, a_e, a_d = an, ae, ad

        sf_n, sf_e, sf_d = a_n, a_e, a_d - self.g
        # NED -> body(FRD)는 body->NED 회전행렬의 전치.
        self._last_specific_force = (
            r11 * sf_n + r21 * sf_e + r31 * sf_d,
            r12 * sf_n + r22 * sf_e + r32 * sf_d,
            r13 * sf_n + r23 * sf_e + r33 * sf_d,
        )

    def snapshot(self):
        """mavlink_link.py(HIL_SENSOR/HIL_GPS 생성)와 telemetry_hub.py(브라우저 전송)가
        공통으로 읽는 현재 진실(truth) 상태."""
        return {
            "roll": self.roll,
            "pitch": self.pitch,
            "heading": self.yaw,
            "yawRate": self.r,
            "p": self.p,
            "q": self.q,
            "r": self.r,
            "vN": self.vn,
            "vE": self.ve,
            "vD": self.vd,
            "climbRate": -self.vd,
            "north": self.north,
            "east": self.east,
            "alt": self.alt,
            "tilt": self.tilt,
            "specific_force_body": self._last_specific_force,
        }
