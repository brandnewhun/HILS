"""
QGC 없이 PX4의 NSH 콘솔에 접속하는 도구 — QGC의 "MAVLink Console" 위젯이 하는 일을
그대로 재구현한 것. SERIAL_CONTROL 메시지(SERIAL_CONTROL_DEV_SHELL)로 원격 셸을 여는
표준 MAVLink 프로토콜을 쓴다.

    python mavlink_shell.py COM11 "listener sensor_accel"
    python mavlink_shell.py COM11                          # 대화형 모드

listener sensor_accel은 PX4가 실제로 sensor_accel uORB 토픽에 뭘 갖고 있는지(device_id,
timestamp, 최신값)를 그대로 보여준다 — HIGHRES_IMU 같은 MAVLink 우회 경로 없이,
accelerometerCheck.cpp가 직접 보는 것과 동일한 데이터다.
"""
from __future__ import annotations

import argparse
import sys
import time

from pymavlink import mavutil

# pymavlink가 다이얼렉트에서 그대로 노출하는 상수를 쓴다(직접 베껴 적어서 값이
# 어긋날 위험을 없앰) -- common.xml 기준: DEV_SHELL=10, RESPOND=2, EXCLUSIVE=4,
# MULTI=16.
_DEV_SHELL = mavutil.mavlink.SERIAL_CONTROL_DEV_SHELL
_FLAGS = (
    mavutil.mavlink.SERIAL_CONTROL_FLAG_RESPOND
    | mavutil.mavlink.SERIAL_CONTROL_FLAG_EXCLUSIVE
    | mavutil.mavlink.SERIAL_CONTROL_FLAG_MULTI
)


def send_shell_cmd(conn, text: str) -> None:
    data = (text + "\n").encode("ascii", "replace")
    # SERIAL_CONTROL의 data 필드는 최대 70바이트 -- 넘으면 잘라서 여러 번 보낸다.
    for i in range(0, len(data), 70):
        chunk = data[i:i + 70]
        padded = chunk + b"\x00" * (70 - len(chunk))
        conn.mav.serial_control_send(
            _DEV_SHELL, _FLAGS, 0, 0, len(chunk), list(padded),
        )


def drain_shell_output(conn, duration_s: float) -> str:
    out = []
    deadline = time.time() + duration_s
    while time.time() < deadline:
        msg = conn.recv_match(type="SERIAL_CONTROL", blocking=True, timeout=0.2)
        if msg is None:
            continue
        n = getattr(msg, "count", 0)
        raw = bytes(msg.data[:n])
        out.append(raw.decode("utf-8", "replace"))
    return "".join(out)


def run_command(conn, cmd: str, wait_s: float = 2.0) -> str:
    send_shell_cmd(conn, cmd)
    return drain_shell_output(conn, wait_s)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("connection", help="예: COM11, /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("command", nargs="?", default=None,
                         help="한 번 실행하고 종료할 명령. 생략하면 대화형 모드.")
    args = parser.parse_args()

    print(f"[mavlink_shell] 연결 시도: {args.connection}")
    conn = mavutil.mavlink_connection(args.connection, baud=args.baud)
    conn.wait_heartbeat(timeout=10)
    print(f"[mavlink_shell] 연결됨 (system {conn.target_system})")

    # 셸을 먼저 한 번 "깨워야" 프롬프트가 나온다(빈 줄 전송).
    send_shell_cmd(conn, "")
    time.sleep(0.3)
    drain_shell_output(conn, 0.3)

    if args.command:
        print(f"\n$ {args.command}")
        print(run_command(conn, args.command, wait_s=3.0))
        return

    print("[mavlink_shell] 대화형 모드 -- 명령을 입력하세요 (exit로 종료)")
    while True:
        try:
            line = input("nsh> ")
        except (EOFError, KeyboardInterrupt):
            break
        if line.strip() == "exit":
            break
        print(run_command(conn, line, wait_s=2.0))


if __name__ == "__main__":
    main()
