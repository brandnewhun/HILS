# -*- coding: utf-8 -*-
"""
RcSource — Channel D(조종기 입력 -> FCC)의 "값이 어디서 오는가"를 갈아끼울 수 있게
분리한 모듈. main.py/mavlink_link.py는 RcSource.read()가 돌려주는 정규화값(dict)만
알면 되고, 그 값이 스크립트/키보드/실제 외부 송신기 중 어디서 왔는지는 전혀 모른다.

    RC 계약: read() -> {"pitch","roll","yaw","thr","tilt"}  (전부 -1.0 ~ 1.0 정규화값)
    HILS_ICD/sim/quadrotor_hud.html의 rcCmd, sim/py/flight_sim.py의 set_rc_input()과
    동일한 정규화 규약이다 — 브라우저 시뮬레이터/Python 포트/이 브릿지가 전부 같은
    -1..1 스케일을 쓰므로, 나중에 어느 쪽 RC 소스를 어디에 꽂아도 값 해석이 갈리지 않는다.

지금(HILS 벤치 검증 단계) 쓸 구현:
    ManualRcSource   — set()으로 값을 밀어넣는 가장 단순한 소스. 코드/콘솔에서 직접 조작.
    ScriptedRcSource — 정해진 (시각, 입력) 시퀀스를 재생 — 사람 없이 반복 가능한 자동
                       검증(예: "피치 스틱을 이만큼 줬을 때 OFP가 이만큼 반응하는가")에 적합.

나중에(실제 외부 송신기를 FCC 벤치에 연결할 때) 쓸 구현:
    SerialReceiverRcSource — 아직 미완성 자리표시자. 외부 송신기+리시버를 SIM PC에
    시리얼/USB로 연결하는 방식(예: SBUS-USB 어댑터, ELRS 리시버의 CRSF-USB 변환 등)을
    쓰면, "실제 송신기로 준 신호를 브릿지가 그대로 읽어서 동일한 RC_CHANNELS_OVERRIDE
    경로로 재전송"하는 구조를 그대로 재사용할 수 있다 — main.py/mavlink_link.py는
    한 글자도 안 바뀐다. 실제 수신기가 정해지면 이 클래스의 __init__/read()만 채우면 됨.

    (참고: 리시버를 Micoair H743 V2의 RC UART(/dev/ttyS5)에 직결하는 경우는 이 모듈과 무관하다 — 그건
    OFP가 하드웨어에서 직접 받는 완전히 별개의 경로다. 이 모듈은 "송신기 신호를 일단
    SIM PC로 가져와서, 지금 쓰고 있는 MAVLink 링크로 얹어 보낸다"는 경로에만 쓴다.)

main.py에서 어떤 소스를 쓸지는 config.RC_SOURCE_MODE 하나로 고른다(코드 수정 없이
"scripted" <-> "manual" <-> "serial_receiver" 전환).
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Any

_ZERO = {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "thr": 0.0, "tilt": 0.0}


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class RcSource(ABC):
    @abstractmethod
    def read(self) -> dict[str, float]:
        """현 시점의 RC 입력을 {"pitch","roll","yaw","thr","tilt"} (-1..1)로 반환."""

    def close(self) -> None:
        """포트/리소스를 쓰는 구현체(예: SerialReceiverRcSource)를 위한 훅. 기본은 no-op."""


class ManualRcSource(RcSource):
    """가장 단순한 소스 — set()으로 넣은 값을 read()가 그대로 돌려준다.
    콘솔에서 직접 값을 찔러보거나, 다른 스레드/프로세스가 값을 갱신하는 용도로 쓴다."""

    def __init__(self):
        self._values = dict(_ZERO)

    def set(self, pitch: float = 0.0, roll: float = 0.0, yaw: float = 0.0,
            thr: float = 0.0, tilt: float = 0.0) -> None:
        self._values = {
            "pitch": _clamp(pitch), "roll": _clamp(roll), "yaw": _clamp(yaw),
            "thr": _clamp(thr), "tilt": _clamp(tilt),
        }

    def read(self) -> dict[str, float]:
        return dict(self._values)


class BrowserRcSource(RcSource):
    """브라우저(quadrotor_hud_v2.html의 RcTx 모듈)가 WebSocket으로 보내온 키보드 입력을
    그대로 돌려주는 소스. telemetry_hub가 수신한 메시지를 on_message()로 밀어넣어주고,
    main.py는 다른 소스와 똑같이 read()만 호출한다.

    ManualRcSource와 거의 같지만 두 가지가 다르다:
      - 락으로 보호한다. 값을 쓰는 쪽은 telemetry_hub의 asyncio 스레드이고 읽는 쪽은
        main 루프라서, 잠금 없이 dict를 공유하면 절반만 갱신된 값을 읽을 수 있다.
      - 페일세이프가 있다. 브라우저 탭을 닫거나 WebSocket이 끊기면 새 메시지가 안 오는데,
        마지막 입력이 그대로 남아 계속 조종 명령으로 나가면 위험하다. timeout_s 동안
        새 메시지가 없으면 중립(0)을 돌려준다.

    기대하는 메시지 형식(브라우저가 보내는 것): {"type":"rc", "pitch":..,"roll":..,
    "yaw":..,"thr":..}  (tilt는 아직 브라우저가 안 보내므로 0으로 둔다)
    """

    def __init__(self, timeout_s: float = 1.0):
        self._lock = threading.Lock()
        self._values = dict(_ZERO)
        self._last_update_s = 0.0
        self._timeout_s = timeout_s

    def on_message(self, msg: dict[str, Any]) -> None:
        """telemetry_hub가 브라우저 메시지 1건마다 호출한다. rc 타입이 아니면 무시."""
        if not isinstance(msg, dict) or msg.get("type") != "rc":
            return
        with self._lock:
            for key in ("pitch", "roll", "yaw", "thr", "tilt"):
                value = msg.get(key)
                if isinstance(value, (int, float)):
                    self._values[key] = _clamp(float(value))
            self._last_update_s = time.time()

    def read(self) -> dict[str, float]:
        with self._lock:
            if self._last_update_s > 0 and (time.time() - self._last_update_s) > self._timeout_s:
                return dict(_ZERO)
            return dict(self._values)


class ScriptedRcSource(RcSource):
    """정해진 (t_start_sec, rc_dict) 키프레임을 재생하는 소스 — 사람이 스틱을 안 잡아도
    같은 입력을 반복 재현할 수 있어 "OFP가 이 입력에 이렇게 반응하는가" 자동 검증에 쓴다.

    keyframes 예:
        [(0.0, {"thr": 1.0}),                 # 0초: 스로틀 풀로
         (2.0, {"pitch": 0.5, "thr": 0.1}),    # 2초: 전진 + 고도유지
         (6.0, {"pitch": 0.3, "yaw": 0.4}),    # 6초: 좌선회
         (9.0, {})]                            # 9초: 중립(전부 0)
    각 구간의 값은 다음 키프레임 전까지 그대로 유지(sample & hold)된다."""

    def __init__(self, keyframes: list[tuple[float, dict[str, float]]]):
        self._keyframes = sorted(keyframes, key=lambda kv: kv[0])
        self._t = 0.0

    def advance(self, dt: float) -> None:
        self._t += dt

    def read(self) -> dict[str, float]:
        current = dict(_ZERO)
        for t_start, values in self._keyframes:
            if t_start > self._t:
                break
            current.update(values)
        return {k: _clamp(v) for k, v in current.items()}


class SerialReceiverRcSource(RcSource):
    """★ 미구현 자리표시자 ★ — 실제 외부 송신기+리시버를 SIM PC에 연결했을 때 쓸 자리.

    실제로 채우려면(리시버/프로토콜이 정해진 뒤):
        1) __init__에서 해당 포트를 열고(예: pyserial), 프로토콜 파서를 초기화한다.
        2) read()에서 최신 채널 프레임을 파싱해 -1..1로 정규화해 반환한다.
           - SBUS: 172~1811 카운트가 보통 -1..1에 대응(리시버 스펙에 따라 오프셋 다를 수 있음)
           - CRSF(ELRS/Crossfire): 172~1811 카운트, 위와 유사하지만 프레임 구조가 다름
           - PPM: 채널별 1000~2000us 펄스폭
        3) 어떤 물리 채널이 pitch/roll/yaw/thr/tilt인지의 매핑도 여기서 정의한다
           (송신기 쪽 채널 배정과 반드시 일치시킬 것).

    지금은 연결이 안 돼 있으므로 항상 중립값(전부 0)을 반환한다 — main.py가 이 클래스를
    골라도 프로그램이 죽지 않고 "아직 준비 안 됨"으로 안전하게 동작한다."""

    def __init__(self, port: str | None = None, protocol: str = "sbus"):
        self.port = port
        self.protocol = protocol
        self._warned = False

    def read(self) -> dict[str, float]:
        if not self._warned:
            # 이 print()는 콘솔 인코딩이 cp949(예: 구형 cmd.exe)인 환경에서도 안전하게
            # 출력되도록 em-dash 등 비ASCII 특수문자를 피한다.
            print(
                "[rc_source] SerialReceiverRcSource는 아직 미구현입니다 "
                f"(port={self.port!r}, protocol={self.protocol!r}). 항상 중립값을 반환합니다. "
                "실제 리시버가 정해지면 rc_source.py의 이 클래스만 채우면 됩니다."
            )
            self._warned = True
        return dict(_ZERO)


def create_rc_source(mode: str, options: dict[str, Any] | None = None) -> RcSource:
    """config.RC_SOURCE_MODE 문자열 하나로 소스를 고르는 팩토리 — main.py는 이 함수만
    호출하면 되고, 소스 종류가 늘어나도 main.py를 고칠 필요가 없다."""
    options = options or {}
    if mode == "manual":
        return ManualRcSource()
    if mode == "browser":
        return BrowserRcSource(float(options.get("timeout_s", 1.0)))
    if mode == "scripted":
        return ScriptedRcSource(options.get("keyframes", []))
    if mode == "serial_receiver":
        return SerialReceiverRcSource(options.get("port"), options.get("protocol", "sbus"))
    raise ValueError(
        f"알 수 없는 RC_SOURCE_MODE: {mode!r} "
        "(manual/browser/scripted/serial_receiver 중 하나)"
    )
