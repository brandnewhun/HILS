# -*- coding: utf-8 -*-
"""
TelemetryHub — Channel C(FCC→VIS) 텔레메트리를 WebSocket으로 브라우저
(quadrotor_hud_v2.html)에 뿌려주는 모듈. mavlink_link/fdm이 무엇을 하는지는
전혀 모르고, "state_provider() 콜백이 주는 dict를 주기적으로 JSON으로 보낸다"만
담당한다 — 나중에 텔레메트리 소스를 바꿔도(예: 브릿지 FDM 진실값 대신 Pixhawk의
GLOBAL_POSITION_INT/ATTITUDE_QUATERNION 실측 스트림) 이 모듈은 손댈 필요가 없다.

브라우저(quadrotor_hud_v2.html)는 수신 전용 클라이언트이므로, 이 서버는 클라이언트가
보내는 메시지는 읽기만 하고 무시한다(RC/조종 입력 Channel D는 이 링크를 타지 않음).
"""
import asyncio
import json
import threading


class TelemetryHub:
    def __init__(self, host, port, broadcast_hz, state_provider, on_client_message=None):
        self.host = host
        self.port = port
        self.period = 1.0 / broadcast_hz
        self.state_provider = state_provider  # () -> dict, 스레드 세이프해야 함
        # 브라우저 -> 브릿지 방향 메시지(키보드 RC 입력, ARM 요청) 1건마다 호출되는 콜백.
        # asyncio 스레드에서 호출되므로, 콜백 쪽에서 스레드 세이프하게 처리해야 한다
        # (rc_source.BrowserRcSource가 락으로 그렇게 되어 있음).
        self.on_client_message = on_client_message
        self._clients = set()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="telemetry-hub", daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        import websockets
        print("[telemetry_hub] WebSocket 서버 시작: ws://%s:%d" % (self.host, self.port))
        async with websockets.serve(self._handler, self.host, self.port):
            await self._broadcast_loop()

    async def _handler(self, websocket, *_args):
        # *_args: websockets 라이브러리 버전에 따라 handler(ws) 또는 handler(ws, path)로
        # 호출되므로 둘 다 받아들이도록 가변인자로 둔다.
        self._clients.add(websocket)
        try:
            async for raw in websocket:
                if not self.on_client_message:
                    continue  # 콜백을 안 걸었으면 기존처럼 수신 전용으로 동작(무시)
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict):
                    self.on_client_message(msg)
        finally:
            self._clients.discard(websocket)

    async def _broadcast_loop(self):
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
