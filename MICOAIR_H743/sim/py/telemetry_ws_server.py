"""
TelemetryWsServer -- state_provider() 콜백이 주는 dict를 주기적으로 JSON으로
WebSocket 클라이언트(브라우저의 quadrotor_hud_v2.html)에 뿌리고, 반대로 브라우저가
보내는 메시지(키보드 RC 입력)는 on_client_message 콜백으로 넘겨주는 양방향 모듈.

sim/bridge/telemetry_hub.py(다른 세션이 만든, HIL 브릿지 전용 -- 수신 전용)와 설계
원칙은 같다 -- "텔레메트리가 어디서 오는지/입력을 받아서 뭘 하는지" 전혀 모르고 그냥
콜백만 연결한다. sim/py를 sim/bridge에 의존시키지 않기 위해 여기 별도로 자체
구현했다.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
from typing import Any, Callable


def finite(value: Any, fallback: float = 0.0) -> float:
    """표준 JSON은 NaN/Inf를 모른다 -- 브라우저 쪽 JSON.parse가 그 값을 만나면 패킷
    전체를 조용히 버리므로(quadrotor_hud_v2.html의 BridgeLink.onmessage try/catch),
    여기서 미리 막아두는 게 안전하다."""
    return value if isinstance(value, (int, float)) and math.isfinite(value) else fallback


class TelemetryWsServer:
    def __init__(self, host: str, port: int, broadcast_hz: float, state_provider: Callable[[], dict[str, Any]],
                 on_client_message: Callable[[dict[str, Any]], None] | None = None):
        self.host = host
        self.port = port
        self.period = 1.0 / broadcast_hz
        self.state_provider = state_provider  # () -> dict, 스레드 세이프해야 함
        # 브라우저(BridgeLink.send())가 보낸 메시지 1건마다 호출된다. asyncio 스레드에서
        # 호출되므로, 이 콜백 안에서 다른 스레드의 상태를 건드릴 땐 그쪽에서 락 등으로
        # 스레드 세이프하게 처리할 것(run_live_3d.py의 RcInputState가 그렇게 되어 있음).
        self.on_client_message = on_client_message
        self._clients: set = set()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telemetry-ws-server", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        import websockets
        print(f"[telemetry_ws_server] WebSocket 서버 시작: ws://{self.host}:{self.port}")
        async with websockets.serve(self._handler, self.host, self.port):
            await self._broadcast_loop()

    async def _handler(self, websocket, *_args) -> None:
        # *_args: websockets 라이브러리 버전에 따라 handler(ws) 또는 handler(ws, path)로
        # 호출되므로 둘 다 받아들이도록 가변인자로 둔다.
        self._clients.add(websocket)
        try:
            async for raw in websocket:
                if not self.on_client_message:
                    continue
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict):
                    self.on_client_message(msg)
        finally:
            self._clients.discard(websocket)

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(self.period)
            if not self._clients:
                continue
            payload = json.dumps(self.state_provider())
            dead = []
            for ws in list(self._clients):
                try:
                    await ws.send(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)


def build_payload(state: dict[str, Any], actuators: dict[str, Any]) -> dict[str, Any]:
    """quadrotor_hud_v2.html의 FlightSim.applyTelemetry()가 기대하는 필드명과 1:1
    대응. 추가로 armed/mode/servoOutputs를 얹어 실기 액추에이터 패널도 채운다."""
    return {
        "north": finite(state["north"]),
        "east": finite(state["east"]),
        "alt": finite(state["alt"]),
        "roll": finite(state["roll"]),
        "pitch": finite(state["pitch"]),
        "heading": finite(state["heading"]),
        "yawRate": finite(state["yawRate"]),
        "vN": finite(state["vN"]),
        "vE": finite(state["vE"]),
        "climbRate": finite(state["climbRate"]),
        "tilt": finite(state.get("tilt", 0.0)),
        "armed": bool(actuators.get("armed")),
        "mode": actuators.get("mode"),
        "servoOutputs": list(actuators.get("servo_outputs") or []),
    }
