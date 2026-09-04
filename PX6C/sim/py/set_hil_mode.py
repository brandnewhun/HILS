"""
SYS_HITL 파라미터 전환 도구 -- 실기 Pixhawk를 HIL 모드로 켜고 끈다.

    python set_hil_mode.py COM11            # 현재 값만 읽어서 확인(변경 안 함)
    python set_hil_mode.py COM11 --on       # SYS_HITL=1 (HIL 모드 켜기)
    python set_hil_mode.py COM11 --off      # SYS_HITL=0 (평소 모드로 되돌리기)
    python set_hil_mode.py COM11 --on --reboot   # 설정 후 FC 재부팅까지

QGC의 파라미터 화면에서 손으로 바꾸는 것과 같은 동작을, 값 확인/되돌리기까지 한 번에
할 수 있게 스크립트로 만든 것이다. 쓰기 후 반드시 다시 읽어서 실제로 반영됐는지
확인한다(파라미터 쓰기는 조용히 무시될 수 있으므로).

⚠ SYS_HITL=1이 무슨 뜻인지:
  - PX4가 실제 센서(IMU/기압계/GPS)를 쓰지 않고, 외부에서 MAVLink로 주입해주는
    가짜 센서(HIL_SENSOR/HIL_GPS)만 믿는 상태가 된다. 즉 센서를 넣어주는 쪽
    (sim/bridge/main.py)이 같이 돌지 않으면 PX4는 아무 데이터도 못 받아 시동조차
    걸리지 않는다 -- 파라미터만 바꿔놓고 끝나는 게 아니다.
  - 이 모드에서는 실제 모터 PWM 출력이 나가지 않고 HIL_ACTUATOR_CONTROLS 메시지로
    대체된다. 그래서 벤치에서는 오히려 더 안전하지만,
  - 반대로 말하면 이 상태로는 실제 비행이 불가능하다. 실기 비행 전에는 반드시
    --off로 되돌리고 재부팅할 것.
"""
from __future__ import annotations

import argparse
import time

from pymavlink import mavutil

PARAM_NAME = "SYS_HITL"


def read_param(conn, name: str, timeout: float = 5.0):
    """파라미터 1개를 읽어 값을 돌려준다. 못 읽으면 None."""
    conn.mav.param_request_read_send(
        conn.target_system, conn.target_component, name.encode("ascii"), -1
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
        if msg is None:
            continue
        if msg.param_id.strip("\x00") == name:
            return msg.param_value
    return None


def write_param(conn, name: str, value: int) -> None:
    conn.mav.param_set_send(
        conn.target_system, conn.target_component,
        name.encode("ascii"), float(value),
        mavutil.mavlink.MAV_PARAM_TYPE_INT32,
    )


def reboot_fc(conn) -> None:
    conn.mav.command_long_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
        0,
        1,  # param1=1 : 오토파일럿 재부팅
        0, 0, 0, 0, 0, 0,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("connection", help="예: COM11, /dev/ttyACM0, udp:127.0.0.1:14540")
    parser.add_argument("--baud", type=int, default=115200)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true", help="SYS_HITL=1 (HIL 모드)")
    group.add_argument("--off", action="store_true", help="SYS_HITL=0 (평소 모드)")
    parser.add_argument("--reboot", action="store_true", help="설정 후 FC 재부팅")
    args = parser.parse_args()

    print(f"[set_hil_mode] 연결 시도: {args.connection}")
    conn = mavutil.mavlink_connection(args.connection, baud=args.baud)
    conn.wait_heartbeat(timeout=10)
    print(f"[set_hil_mode] 연결됨 (system {conn.target_system}, component {conn.target_component})")

    before = read_param(conn, PARAM_NAME)
    if before is None:
        print(f"[set_hil_mode] {PARAM_NAME}을(를) 읽지 못했습니다 -- 연결/펌웨어를 확인하세요.")
        return
    print(f"[set_hil_mode] 현재 {PARAM_NAME} = {int(before)}")

    if not args.on and not args.off:
        print("[set_hil_mode] (--on / --off 를 주지 않아 읽기만 했습니다)")
        return

    target = 1 if args.on else 0
    if int(before) == target:
        print(f"[set_hil_mode] 이미 {PARAM_NAME}={target} 입니다 -- 변경할 것이 없습니다.")
    else:
        print(f"[set_hil_mode] {PARAM_NAME} = {target} 로 설정 중...")
        write_param(conn, PARAM_NAME, target)
        time.sleep(0.5)

        after = read_param(conn, PARAM_NAME)
        if after is None or int(after) != target:
            print(f"[set_hil_mode] 실패 -- 되읽은 값이 {after}. 변경이 반영되지 않았습니다.")
            return
        print(f"[set_hil_mode] 확인 완료: {PARAM_NAME} = {int(after)}")

    if args.reboot:
        print("[set_hil_mode] FC 재부팅 명령 전송 (파라미터는 재부팅 후 적용됩니다)")
        reboot_fc(conn)
        time.sleep(1.0)
    else:
        print("[set_hil_mode] ※ 이 파라미터는 FC를 재부팅해야 적용됩니다 (--reboot 또는 USB 재연결)")

    if target == 1:
        print()
        print("  다음 단계: SYS_HITL=1 상태의 PX4는 실제 센서를 쓰지 않고 주입된 가짜")
        print("  센서를 기다립니다. sim/bridge/main.py(센서 주입 + FDM)를 같이 돌려야")
        print("  움직임이 나옵니다. run_live_3d.py는 센서를 주입하지 않으므로 이 모드에서는")
        print("  동작하지 않습니다.")
    else:
        print()
        print("  SYS_HITL=0 -- 평소 모드로 돌아왔습니다. 실기 센서를 다시 사용합니다.")


if __name__ == "__main__":
    main()
