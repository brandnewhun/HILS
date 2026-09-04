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
    def __init__(self, fdm_config, world=None):
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
        # world: world_model.WorldModel — 주어지면 실제 지형/도로/건물 지붕 중 가장
        # 높은 면을 지면으로 쓴다(quadrotor_hud_v2.html이 그리는 것과 동일한 지형).
        # None이면 기존처럼 ground_alt_m 평지 가정으로 동작(하위호환).
        self.world = world
        self.reset()

    def reset(self):
        """위치/속도/자세를 원점으로 되돌린다(HUD의 RESET 버튼 -> main.py의
        reset 메시지 처리에서 호출). __init__도 이 메서드로 초기 상태를 만든다 —
        두 곳의 초기값이 서로 어긋나는 일이 없도록."""
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        # 자세의 진짜 상태는 이 쿼터니언(body FRD -> NED, [w,x,y,z])이다. roll/pitch/yaw는
        # 매 step 끝에 여기서 유도해 채우는 "표시/센서용 파생값"일 뿐이다(아래 step() 주석).
        self.quat = [1.0, 0.0, 0.0, 0.0]
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

        # ── 자세 적분: 쿼터니언 ─────────────────────────────────────────────────
        # ★ 2026-09-04 교체 ★ 예전에는 roll += p*dt, pitch += q*dt 식으로 각속도를
        # 오일러각에 직접 더했다("소각/완만한 기동 가정"). 그 가정은 자세가 수십 도를
        # 넘어가면 깨진다 — body 각속도(p,q,r)와 오일러각 변화율은 같은 것이 아니고,
        # pitch=±90° 근처에서는 특이점까지 있다. 실기 검증 중 OFP 자세 PID가 이 근사
        # 플랜트를 상대로 발산해 기체가 여러 바퀴 뒤집혔을 때(|pitch| 889° 기록), 이
        # 틀린 자세로 만든 가속도/지자기 값이 PX4 EKF2에 들어가 모순을 일으켰고, EKF2는
        # 지자기 고장(cs_mag_fault)으로 래치해 "Compass needs calibration - Land now!"
        # 와 함께 이후 ARM을 영구 거부했다(FC 재부팅 전까지). HILS의 "진실값"이 어떤
        # 자세에서도 물리적으로 옳아야 그 뒤의 검증이 의미가 있으므로 쿼터니언으로
        # 바꾼다. 발산 자체(OFP PID vs 근사 플랜트)는 이 파일이 아니라 실기 제원 반영
        # 및 OFP 쪽에서 다룰 문제로 남겨둔다 — 여기서는 자세 기하학만 올바르게 한다.
        self._integrate_attitude(dt)
        r11, r12, r13, r21, r22, r23, r31, r32, r33 = self._dcm_body_to_ned()
        self._update_euler_from_dcm(r11, r21, r31, r32, r33)

        # 추력: tilt=0(수직/MC)이면 body -Z(위), tilt=1(수평/FW)이면 body +X(전방).
        thrust_n = thrust_mix * 4.0 * self.max_thrust_motor
        s, c_ = math.sin(self.tilt * math.pi / 2.0), math.cos(self.tilt * math.pi / 2.0)
        thrust_body = (thrust_n * s / self.mass, 0.0, -thrust_n * c_ / self.mass)

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

        # 지면 충돌 — world가 주어지면 실제 지형/도로/건물 지붕 중 가장 높은 면을
        # 지면으로 쓴다(quadrotor_hud_v2.html이 그리는 지형과 동일한 값). world가
        # 없으면(단위 테스트 등) 기존 평지 가정(ground_alt_m)으로 폴백.
        floor_alt = self.world.floor_height_at(self.east, self.north) if self.world else self.ground_alt
        floor_d = -floor_alt
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

    # ── 쿼터니언 유틸 (body FRD -> NED, q=[w,x,y,z]) ─────────────────────────
    def _integrate_attitude(self, dt):
        """body 각속도 (p,q,r)로 dt 동안 자세 쿼터니언을 회전시킨다.

        dt 동안 각속도가 일정하다고 보고 회전축 ω/|ω|, 회전각 |ω|·dt 의 회전
        쿼터니언을 오른쪽에 곱한다(정확한 지수사상 — 1차 오일러 적분 q += 0.5·q⊗ω·dt
        보다 큰 각속도에서 정확하고 정규화 오차도 작다). 마지막에 정규화해 수치 오차
        누적을 막는다."""
        wx, wy, wz = self.p, self.q, self.r
        omega = math.sqrt(wx * wx + wy * wy + wz * wz)
        if omega * dt < 1e-12:
            return
        half = 0.5 * omega * dt
        s = math.sin(half) / omega
        dw, dx, dy, dz = math.cos(half), wx * s, wy * s, wz * s
        qw, qx, qy, qz = self.quat
        # q_new = q ⊗ dq (body 프레임 각속도이므로 오른쪽 곱)
        nw = qw * dw - qx * dx - qy * dy - qz * dz
        nx = qw * dx + qx * dw + qy * dz - qz * dy
        ny = qw * dy - qx * dz + qy * dw + qz * dx
        nz = qw * dz + qx * dy - qy * dx + qz * dw
        norm = math.sqrt(nw * nw + nx * nx + ny * ny + nz * nz) or 1.0
        self.quat = [nw / norm, nx / norm, ny / norm, nz / norm]

    def _dcm_body_to_ned(self):
        """쿼터니언 -> 회전행렬(body FRD -> NED). 예전 ZYX 오일러 행렬과 같은 관례
        (r31=-sin(pitch), r32=cos(pitch)sin(roll), r33=cos(pitch)cos(roll))이라
        이 아래의 추력 변환/비중력가속 변환 코드는 그대로 쓴다."""
        w, x, y, z = self.quat
        r11 = 1.0 - 2.0 * (y * y + z * z)
        r12 = 2.0 * (x * y - w * z)
        r13 = 2.0 * (x * z + w * y)
        r21 = 2.0 * (x * y + w * z)
        r22 = 1.0 - 2.0 * (x * x + z * z)
        r23 = 2.0 * (y * z - w * x)
        r31 = 2.0 * (x * z - w * y)
        r32 = 2.0 * (y * z + w * x)
        r33 = 1.0 - 2.0 * (x * x + y * y)
        return r11, r12, r13, r21, r22, r23, r31, r32, r33

    def _update_euler_from_dcm(self, r11, r21, r31, r32, r33):
        """표시/센서용 오일러각(ZYX). roll∈[-π,π], pitch∈[-π/2,π/2], yaw∈[0,2π) —
        예전 코드와 같은 범위 관례(yaw만 0..2π로 감음). pitch=±90° 특이점에서는
        roll/yaw가 유일하지 않지만 진실 상태는 쿼터니언이므로 물리 계산엔 영향 없음."""
        self.roll = math.atan2(r32, r33)
        self.pitch = -math.asin(max(-1.0, min(1.0, r31)))
        self.yaw = math.atan2(r21, r11) % (2.0 * math.pi)

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
