# HILS — PX6c(PX4 커스텀 OFP) HITL 브릿지

PX6c(FMUv6C) 실제 비행 컨트롤러에 올라간 OFP(PX4 v1.15.4 기반 커스텀 펌웨어)를, 실제 기체 없이
PC의 가상 환경과 연결해 검증하는 HILS(Hardware-In-the-Loop Simulation) 구성입니다.

```
키보드/RC 입력 ──► 브릿지(Python) ──HIL_SENSOR/HIL_GPS──► PX6c(OFP)
                       ▲                                    │
                  FDM(비행동역학)  ◄──HIL_ACTUATOR_CONTROLS──┘
                       │
                  3D HUD(브라우저, 실제 지형: 용인 기흥 SRTM+OSM)
```

## ⚠️ 현재 시뮬레이션의 성격 — 반드시 읽어주세요

**현재 시뮬레이션(FDM)은 실제 기체의 움직임과 다릅니다.**
이 HILS의 현 단계 목적은 **FCC(PX6c)에서 제어 신호가 정상적으로 출력되는지**, 그리고 그 결과가
**시뮬레이션 화면에 제대로 나오는지**(입력 → OFP → 모터 명령 → 시뮬레이션 → 화면의 데이터 연계)를
확인하는 것입니다. 비행 특성이나 제어 성능을 검증하는 용도가 아니며, 화면에 보이는 기체의 거동을
실제 기체의 성능으로 해석하면 안 됩니다.

`sim/bridge/config.py`의 `FDM` 값(질량·관성·추력·감쇠 등)은 전부 **구조 검증용 추정치**입니다.
그 결과 현재 MANUAL 모드 비행에서는 OFP의 자세 PID가 이 근사 플랜트를 상대로 pitch 축 진동이
커지며 수 분 안에 기체가 뒤집히는 현상이 있습니다. 이는 OFP 결함이 아니라 시뮬레이션 모델이
실제 기체를 닮지 않아서 생기는 현상이며, 아래 데이터가 확보되면 해소할 대상입니다.

## 실제 기체를 모사하기 위해 필요한 데이터

| 순위 | 항목 | 현재 FDM 값 | 필요한 이유 |
|---|---|---|---|
| 1 | 관성 모멘트 Ixx / Iyy / Izz | 0.03 / 0.03 / 0.06 kg·m² (추정) | 자세 PID가 직접 상대하는 값 — 발산의 1순위 원인 |
| 2 | 모터(ESC+프롭) 응답 시간상수 | 0 (지연 없음) | 제어기 위상 여유를 결정 |
| 3 | 모터 추력 곡선(명령→N), 최대 추력 | 10.18 N (호버 트림 역산) | 토크 스케일 = 추력 × 암 길이 |
| 4 | 모터 위치(CG 기준 x, y, z) | 암 0.9 m, 배분행렬은 펌웨어에서 옮김 | 롤/피치 팔길이, 요 커플링 |
| 5 | 전체 질량 | 2.2 kg | 병진 동역학, 호버 추력 |
| 6 | CG 위치 | 원점 가정 | 트림 편향, 피치/롤 비대칭 |
| 7 | 공력 감쇠(회전 감쇠, 항력) | rate_damping 0.6 (임의) | 진동 감쇠 |
| 8 | (틸트로터) 틸트 서보 응답, 틸트각별 추력 방향 | tilt_tau 1.2 s | 천이 구간 |

위 8가지 외에도 **추가적인 기체 데이터가 필요합니다** (예: 배터리 전압에 따른 추력 변화, 고속·천이
구간 공력, 센서 잡음 특성 등). 요(yaw) 축은 이 기체의 `tv_control_allocator`가 모터 차동이 아니라
틸트 서보 차동으로 만들기 때문에, 틸트 기구 데이터 없이는 시뮬레이션되지 않습니다(현재 `yaw_mix=0`).

확보 방법 권장 순서:
1. **실측/CAD** (뼈대): 총 질량, 모터 위치, CG, 관성(CAD 또는 진자법) — 저렴하고 확실함.
2. **식별용 비행 로그 3~5소티** (살): 호버 + 롤/피치/요 스텝 + 상승/하강. PX4 ulog의
   `actuator_motors`↔`vehicle_angular_velocity`/`sensor_combined`로 모터 지연·감쇠·실효 추력을 피팅.
3. 1을 제약으로 두고 2로 나머지를 맞춘 뒤, 로그의 모터 명령을 FDM에 재생해 자세 재현 여부로 검증.

참고: 모터 배치·배분행렬은 FC의 `CA_ROTOR*`/`TV_*` 파라미터에서 바로 읽어 맞출 수 있습니다.

## 구성 및 실행

- 실행 환경: **Windows 네이티브 Python 3.x** (`pip install -r sim/bridge/requirements.txt`),
  PX6c는 USB(COM 포트, `config.SERIAL_PORT`)로 직결. 추후 WSL/Linux로 옮길 수 있게 포트 등은
  `config.py`에 분리되어 있습니다.
- 실행:
  ```
  cd sim/bridge
  python main.py --tag 세션이름     # --tag 는 선택(로그 폴더 라벨)
  ```
  브라우저가 자동으로 열립니다: `http://localhost:8000/quadrotor_hud_v2.html?ws=ws://localhost:8765`
- 시작 시 FC를 자동 재부팅합니다(약 10초) → EKF2 정렬 약 30초 후 조작 가능.
- 조작: `↑↓←→` 피치/롤, `A`/`D` 요, `W`/`S` 스로틀. **`S`를 누른 채(스로틀 idle) ARM 버튼이 깜박일 때
  클릭**해야 시동이 걸립니다(PX4의 MANUAL 모드 시동 조건). `RESET` = FDM 원점 초기화 + FC 재부팅
  (ARMED 중엔 거부 — 먼저 DISARM).

## 로그 (디버깅용, 항상 기록)

실행마다 `sim/bridge/logs/<YYYYMMDD_HHMMSS>[_태그]/` 에 남습니다 (저장소에는 포함되지 않음):

| 파일 | 내용 |
|---|---|
| `session.json` | config 전체, git 커밋, CAL/SIM 장치 ID, FC↔PC 시각 매핑, FC ulog 경로 |
| `console.log` | 터미널 출력 전체(타임스탬프) |
| `events.jsonl` | STATUSTEXT / COMMAND_ACK / NSH / 모드·ARM 변화 / 재부팅 / RESET |
| `snapshot.jsonl` | 5 Hz 상태(rc, armed, 모터, FDM, 센서 health) |
| `browser.jsonl` | 브라우저→브릿지 입력 원본 |
| `mavlink.tlog` | 수신 MAVLink 원본(QGC/MAVExplorer 재생 가능) |

문제가 생기면 `events.jsonl`의 `kind=statustext / mode / arm_state`를 시간순으로 보는 것이 가장 빠릅니다.

## 실제 기체 검증에서 확인된 운용 규칙 (브릿지에 반영됨)

- `MAV_CMD_DO_SET_MODE`의 param2는 HEARTBEAT 인코딩값(`1<<16`)이 아닌 **생 main_mode 값(MANUAL=1)**.
- HIL에는 착륙 판정이 없어 일반 DISARM이 거부되므로 DISARM은 **force(21196)** 로 전송.
- **시뮬레이션 리셋 = FC 리셋**: FDM만 초기화하면 EKF2가 순간이동을 겪어 `cs_mag_fault`가 래치되고
  "Compass needs calibration"으로 ARM이 영구 거부됨 → 브릿지 시작/RESET 시 FC 자동 재부팅.
- 지자기 벡터는 OFP 자체 WMM 테이블에서 원점 좌표로 계산한 값(편각 −8.745°, 복각 53.806°, 0.511 G).
- FDM 자세는 쿼터니언으로 적분(큰 각도에서도 물리적으로 유효).

## 저장소 구성

- `sim/bridge/` — HITL 브릿지(`main.py`, `mavlink_link.py`, `fdm.py`, `session_log.py`, `config.py`, …)
- `sim/quadrotor_hud_v2.html` — 3D HUD(지형 데이터 임베드)
- `sim/py/` — 지형 `WorldModel`, 타겟 드론, 순수 시뮬레이션/라이브 뷰어(예전 진입점)
- `sim/_terrain_raw/` — SRTM 고도·OSM 도로/건물 원본과 가공 스크립트
- `firmware_ref/` — 참조용 OFP 에어프레임 파일
- `ICD-*.docx`, `EICD-*.docx`, `build_*.py` — 인터페이스 제어 문서와 생성 스크립트
