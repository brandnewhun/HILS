# HILS — FCC별 HITL 브릿지

PX4 커스텀 OFP를 실제 비행 컨트롤러(FCC)에 올린 채 PC의 가상 환경과 연결해 검증하는
HILS(Hardware-In-the-Loop Simulation) 저장소입니다. FCC 보드별로 폴더를 나눕니다.

| 폴더 | FCC | 상태 |
|---|---|---|
| [`PX6C/`](PX6C/README.md) | Pixhawk 6C (PX4 FMUv6C), PX4 v1.15.4 기반 OFP | 실기 연동 검증 완료(2026-09-04). ICD/EICD 문서 원본, 지형 원본 데이터, 코드 리뷰도 여기에 있음 |
| [`MICOAIR_H743/`](MICOAIR_H743/README.md) | Micoair H743 V2, PX4 OFP(`micoair_h743-v2` 타깃) | 2026-09-04 착수. 브릿지 복사 + 보드 식별 진단 추가, 실기 미검증 |

**현재 시뮬레이션(FDM)은 실제 기체의 움직임과 다릅니다.** 두 판 모두 FCC에서 제어 신호가 정상적으로
출력되는지, 그 결과가 시뮬레이션 화면에 제대로 나오는지를 확인하는 용도이며, 실제 기체를 모사하려면
`PX6C/README.md`에 정리된 8가지(+추가) 기체 데이터가 필요합니다.

실행 환경은 Windows 네이티브 Python 3.x이며, 각 폴더의 `sim/bridge/`에서 `python main.py`로 실행합니다.
자세한 절차와 운용 규칙은 각 폴더의 README를 참조하세요.
