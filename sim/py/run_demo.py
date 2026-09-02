"""
Quadrotor HUD -- Python 포트 실행 진입점.

    python run_demo.py                 대화형 실행 (matplotlib 창 + 키보드 조종)
    python run_demo.py --demo out_dir   헤드리스 데모: 정해진 입력으로 N초 비행하며
                                        PNG 프레임을 out_dir에 저장 (디스플레이 불필요,
                                        자동 검증/CI용)

조작(대화형 모드, HTML 버전과 동일한 키맵):
    화살표     피치/롤       W/S        스로틀(상승/하강)
    A/D        요(yaw)       Q/E        틸트(틸트로터 전용)
    R          리셋          1/2        기체 전환(쿼드콥터/틸트로터)
    T          타겟 모드 전환(자동<->수동)   I/K/J/L, U/O   타겟 수동 조종(고도/선회/속도)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from world_model import WorldModel
from vehicle import VEHICLE_FACTORIES
from flight_sim import FlightSim
from target_drone import TargetDrone
from renderer_matplotlib import MatplotlibRenderer

TERRAIN_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terrain_data.json")
DT = 1.0 / 30.0  # 물리 스텝 간격(s) -- HTML 버전의 requestAnimationFrame과 유사한 30Hz


def build_sim() -> tuple[WorldModel, FlightSim, TargetDrone]:
    world = WorldModel.from_json_file(TERRAIN_JSON)
    sim = FlightSim(world)
    sim.set_vehicle(VEHICLE_FACTORIES["quadrotor"]())
    target = TargetDrone(world)
    target.respawn(sim.state)
    return world, sim, target


def run_headless_demo(out_dir: str, seconds: float = 12.0) -> None:
    """디스플레이 없이 물리+렌더링이 실제로 맞물려 도는지 확인하기 위한 스크립트 비행.
    이륙 -> 전진 순항 -> 좌선회 -> 착륙 순으로 고정된 RC 입력을 흘려보내고, 주요 시점마다
    프레임을 PNG로 저장한다."""
    import matplotlib
    matplotlib.use("Agg")  # 디스플레이 없는 환경(CI/자동 검증)에서도 안전하게 그림만 저장

    os.makedirs(out_dir, exist_ok=True)
    world, sim, target = build_sim()

    renderer = MatplotlibRenderer()
    renderer.init(world, sim.vehicle)
    renderer.set_target_geometry(target.profile["geometry"])

    steps = int(seconds / DT)
    snapshot_every = max(1, steps // 6)

    for i in range(steps):
        t = i * DT
        if t < 2.0:
            sim.set_rc_input(thr=1.0)                 # 이륙
        elif t < 6.0:
            sim.set_rc_input(pitch=0.6, thr=0.15)      # 전진 순항(고도 소폭 유지 상승)
        elif t < 9.0:
            sim.set_rc_input(pitch=0.4, yaw=0.5)       # 좌선회
        else:
            sim.set_rc_input(thr=-0.3)                 # 하강

        sim.step(DT)
        target.step(DT)

        if i % snapshot_every == 0 or i == steps - 1:
            renderer.update_from_state(sim.state)
            renderer.update_target_from_state(target.state)
            renderer.render()
            frame_path = os.path.join(out_dir, f"frame_{i:05d}_t{t:05.1f}s.png")
            renderer.save_frame(frame_path)
            print(f"t={t:5.1f}s  alt={sim.state['alt']:6.2f}  "
                  f"N={sim.state['north']:7.1f} E={sim.state['east']:7.1f}  "
                  f"saved {frame_path}")

    print("headless demo done.")


def run_interactive() -> None:
    """실제 화면(디스플레이)이 있는 환경에서 실행하는 대화형 모드. matplotlib의
    key_press_event/key_release_event로 키 상태를 추적해 매 프레임 FlightSim/TargetDrone에
    입력을 흘려보낸다."""
    world, sim, target = build_sim()
    renderer = MatplotlibRenderer()
    renderer.init(world, sim.vehicle)
    renderer.set_target_geometry(target.profile["geometry"])

    pressed: set[str] = set()

    def on_key(event, down: bool) -> None:
        if event.key is None:
            return
        key = event.key.lower()
        if down:
            pressed.add(key)
        else:
            pressed.discard(key)

        if down and key == "r":
            sim.reset_state()
        if down and key == "1":
            sim.set_vehicle(VEHICLE_FACTORIES["quadrotor"]())
            renderer.set_vehicle(sim.vehicle)
        if down and key == "2":
            sim.set_vehicle(VEHICLE_FACTORIES["tiltrotor"]())
            renderer.set_vehicle(sim.vehicle)
        if down and key == "t":
            target.set_mode("manual" if target.mode == "auto" else "auto")

    renderer.fig.canvas.mpl_connect("key_press_event", lambda e: on_key(e, True))
    renderer.fig.canvas.mpl_connect("key_release_event", lambda e: on_key(e, False))

    def in_(k: str) -> float:
        return 1.0 if k in pressed else 0.0

    def tick() -> bool:
        sim.set_rc_input(
            pitch=in_("up") - in_("down"),
            roll=in_("right") - in_("left"),
            yaw=in_("d") - in_("a"),
            thr=in_("w") - in_("s"),
            tilt=in_("e") - in_("q"),
        )
        target.set_manual_input(
            turn=in_("l") - in_("j"),
            climb=in_("i") - in_("k"),
            speed=in_("o") - in_("u"),
        )
        sim.step(DT)
        target.step(DT)
        renderer.update_from_state(sim.state)
        renderer.update_target_from_state(target.state)
        renderer.render()
        return True

    timer = renderer.fig.canvas.new_timer(interval=int(DT * 1000))
    timer.add_callback(tick)
    timer.start()
    renderer._plt.show()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", metavar="OUT_DIR", default=None,
                         help="헤드리스 데모 실행, 프레임을 OUT_DIR에 PNG로 저장")
    parser.add_argument("--seconds", type=float, default=12.0, help="--demo일 때 비행 시간(s)")
    args = parser.parse_args()

    if args.demo:
        run_headless_demo(args.demo, args.seconds)
    else:
        run_interactive()


if __name__ == "__main__":
    main()
