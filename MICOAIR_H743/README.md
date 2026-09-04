# HILS — Micoair H743 V2 (PX4 커스텀 OFP) HITL 브릿지

PX6c(FMUv6C) 기반 HILS(`../PX6C/`)를 FCC **Micoair H743 V2**로 옮긴 판입니다. 브릿지의 동작 원리와
운용 규칙은 PX6C 판과 같으므로 여기서는 **보드가 바뀌어 달라진 것과, 아직 확정되지 않아 확인이
필요한 것**만 적습니다. 시뮬레이션의 성격(실기 거동과 다름), 실제 기체 모사에 필요한 8가지 데이터,
로그 구조, 조작법은 `../PX6C/README.md`를 그대로 참조하세요.

```
키보드/RC 입력 ──► 브릿지(Python) ──HIL_SENSOR/HIL_GPS──► Micoair H743 V2 (PX4 OFP)
                       ▲                                        │
                  FDM(비행동역학)  ◄──HIL_ACTUATOR_CONTROLS / NSH──┘
                       │
                  3D HUD(브라우저, 실제 지형: 용인 기흥 SRTM+OSM)
```

## 1. 이 판이 전제하는 것 (확인 필요 — 다르면 알려주세요)

| # | 가정 | 근거 / 다를 경우 |
|---|---|---|
| A | OFP는 여전히 **PX4 기반**이며 Micoair H743 V2용으로 빌드됨 | ArduPilot에는 HIL_SENSOR 주입 경로가 없어 이 브릿지를 쓸 수 없음. 브릿지가 시작 시 HEARTBEAT.autopilot으로 검사해 PX4가 아니면 중단 |
| B | PX4 보드 타깃은 `micoair_h743-v2` | 이 타깃은 **PX4 1.16.0부터** 존재하고 1.15.x에는 백포트되지 않았음. 현 OFP(1.15.4)는 1.16의 `boards/micoair/h743-v2` 폴더를 이식해 빌드하는 방식(PX4 포럼 사례상 동작)이라고 가정. 1.16으로 리베이스하거나 MicoAir 제공 1.15.2를 쓰면 README와 `config.FCC_BOARD` 주석을 갱신 |
| C | PC 연결은 **USB-C(CDC-ACM) 직결** | 보드 설정에 `CDCACM_AUTOSTART`가 있어 PX6c와 동일하게 COM 포트로 잡힘. TELEM UART로 바꾸면 `config.SERIAL_PORT/BAUD`와 OFP `MAV_x_CONFIG`를 함께 수정 |
| D | 보드에 **SD카드 장착** | 이 보드는 파라미터를 `/fs/microsd/params`에 저장함(PX6c의 FRAM과 다름). SD가 없으면 `SYS_HITL`, `SYS_AUTOSTART`가 재부팅마다 초기화되고, 브릿지는 시작/RESET마다 FC를 재부팅하므로 HIL이 풀림 |

## 2. 보드 교체로 달라진 점

**브릿지 코드에서 바뀐 것은 거의 없습니다.** HIL_SENSOR/HIL_GPS 주입, MANUAL_CONTROL(throttle 0..1000),
`DO_SET_MODE` param2=1, force DISARM(21196), NSH `reboot`/`listener actuator_motors`, SIM 장치 ID
(1310988 / 197388 / 6620172) 대조는 전부 PX4 내부 규약이라 보드와 무관합니다. 달라진 것:

- `sim/bridge/config.py`에 **"FCC 보드 식별" 절** 추가: `FCC_BOARD`, `EXPECTED_AUTOPILOT`(PX4),
  `EXPECTED_SYS_HITL`, `ABORT_ON_WRONG_AUTOPILOT`.
- `sim/bridge/main.py`에 **`identify_fcc()`** 추가: 시작(재부팅 후) 5초 안에 HEARTBEAT.autopilot,
  `AUTOPILOT_VERSION`(펌웨어 버전·보드 ID·git 해시), `SYS_HITL`, `SYS_AUTOSTART`를 읽어
  `logs/<세션>/session.json`의 `fcc_identity`에 기록. PX4가 아니면 종료, `SYS_HITL≠1`이면 SD카드 경고.
- `sim/bridge/mavlink_link.py`: `request_autopilot_version()`, `fcc_identity`, `AUTOPILOT_VERSION` 디코드.
- `firmware_ref/.../9001_airbility_tiltvtol`: `@board micoair_h743-v2` + 보드 차이 주석.
- ICD/EICD 문서와 생성 스크립트, 지형 원본(`_terrain_raw`)은 복제하지 않고 `../PX6C/`를 공유합니다.

### Micoair H743 V2 확인된 제원 (PX4 `boards/micoair/h743-v2` 및 제조사 자료 기준)

| 항목 | 값 | HILS 관점 메모 |
|---|---|---|
| MCU | STM32H743VIT6, 480 MHz, 2 MB | — |
| IMU | BMI088 + BMI270 | HIL(SYS_HITL=1)에서는 실물 센서를 시작하지 않으므로 무관 |
| 기압 / 지자기 | SPL06 / QMC5883L(온보드) | 동일 |
| UART | 8개 — TEL1=ttyS0, TEL2=ttyS3, TEL3=ttyS4, TEL4=ttyS7, GPS1=ttyS2, GPS2=ttyS1, RC=ttyS5, URT6=ttyS6 | 틸트 Dynamixel(RS-485)용 UART 배정 미정, 외장 트랜시버 필요 |
| PWM | 10~11ch (1~8 DShot 가능), PX4IO 없음 | 모터 4개는 충분 |
| USB | USB-C, CDC-ACM 자동 시작 | 기존처럼 COM 포트 직결 |
| 파라미터 | SD카드 `/fs/microsd/params` | **SD 필수** (위 D) |
| 출고 상태 | ArduPilot 펌웨어 + ArduPilot 부트로더 | PX4 부트로더를 DFU(STM32CubeProgrammer)로 먼저 올려야 함 |
| 기본 빌드 | `pwm_out_sim` 미포함(FMUv6C 기본도 동일) | HIL_ACTUATOR_CONTROLS 경로는 OFP 빌드 설정에 달림 → 당분간 `MOTOR_SOURCE_MODE="nsh_actuator_motors"` 유지 |

## 3. 처음 연결할 때 절차

1. 보드 BOOT 버튼을 누른 채 USB 연결(DFU) → STM32CubeProgrammer로 전체 삭제 후 **PX4 부트로더** 기록.
2. QGC → Firmware → Advanced → Custom firmware file 로 **OFP(micoair_h743-v2 빌드)** 기록.
3. **SD카드 장착** 후 부팅. NSH(QGC MAVLink Console)에서:
   ```
   param set SYS_HITL 1
   param set SYS_AUTOSTART 9001      # OFP 에어프레임 번호에 맞게
   param save
   reboot
   ```
4. Windows 장치관리자에서 COM 번호 확인 → `sim/bridge/config.py`의 `SERIAL_PORT` 수정.
5. 실행:
   ```
   cd MICOAIR_H743/sim/bridge
   pip install -r requirements.txt
   python main.py --tag first-micoair
   ```
   콘솔의 `[main] FC 식별 중...` 아래 네 줄(autopilot / firmware / SYS_HITL / SYS_AUTOSTART)이
   `PX4 / 1.15.x / 1 / 9001`로 나오면 정상. 이후 조작·ARM 절차는 `../PX6C/README.md`와 동일.

## 4. 아직 필요한 데이터 (보드 교체 관련)

1. **OFP 포팅 방식 확정** — 1.15.4 + 1.16 보드 폴더 이식 / 1.16 리베이스 / MicoAir 제공 1.15.2 중 무엇인지.
2. **연결 방식** — USB-C 직결(COM 번호만) 또는 TELEM UART(어느 TELx, baud, OFP `MAV_x_CONFIG`).
3. **틸트·러더베이터 Dynamixel RS-485 배정 UART** 와 외장 트랜시버 사양.
4. **새 보드에 OFP를 올린 뒤 콘솔 출력** — `ver all`, `param show CAL_*_ID SYS_HITL SYS_AUTOSTART MAV_*_CONFIG`.
   첫 세션의 `logs/<세션>/session.json`(`cal_ids`, `fcc_identity`)을 보내주셔도 됩니다.
5. **SD카드 장착 여부**.
6. 기체 물리 데이터 8항목(`../PX6C/README.md` 표)은 보드와 무관하게 여전히 미확보 상태입니다.

## 5. 저장소 구성 (이 폴더)

- `sim/bridge/` — HITL 브릿지(PX6C 판 복사 + 위 2절 수정)
- `sim/py/`, `sim/*.html` — 지형 WorldModel·타겟 드론·3D HUD(PX6C 판과 동일, 브릿지 실행에 필요)
- `firmware_ref/` — 참조용 OFP 에어프레임 초안(`@board micoair_h743-v2`)
- 로그는 `sim/bridge/logs/<세션>/`에 남고 저장소에는 올라가지 않습니다.

참고 자료: [MicoAir743v2 제품 페이지](https://micoair.com/flightcontroller_micoair743v2/),
[MicoAir 펌웨어 설치 가이드](https://micoair.com/docs/loading-firmware-micoair743/),
[PX4 포럼: v1.15.4 on MicoAir H743 v2](https://discuss.px4.io/t/need-help-flashing-px4-v1-15-4-on-micoair-h743-v2-qgc-not-finding-firmware/47833),
[PX4 boards/micoair/h743-v2](https://github.com/PX4/PX4-Autopilot/tree/main/boards/micoair/h743-v2),
[PX4 HITL 문서](https://docs.px4.io/main/en/simulation/hitl)
