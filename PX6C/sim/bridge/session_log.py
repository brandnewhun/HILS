# -*- coding: utf-8 -*-
"""
SessionLogger — 브릿지 실행 1회(=세션)마다 logs/<YYYYMMDD_HHMMSS>[_태그]/ 폴더를 만들고
디버깅에 필요한 모든 것을 남긴다. 플래그 없이 항상 켜진다(HILS를 계속 쓰면서 "그때 왜
그랬는지"를 나중에 재구성할 수 있어야 하므로).

  session.json   시작 시각, config 전체, git 커밋, Python/pymavlink 버전, COM 포트,
                 CAL/SIM 장치 ID, FC가 알려준 ulog 경로, FC 부팅시각<->PC 시각 매핑.
                 FC 쪽 ulog(/fs/microsd/log/*.ulg)와 대조할 때 쓰는 열쇠.
  console.log    터미널에 찍힌 모든 출력(stdout+stderr, 트레이스백 포함)을 타임스탬프
                 붙여 동시 기록(tee). 터미널에는 원문 그대로 나간다.
  events.jsonl   STATUSTEXT / COMMAND_ACK / NSH 응답 / ARM·모드 변화 / 링크 연결·단절 /
                 FC 재부팅 / RESET 등 모든 이벤트를 각각 한 줄로. 예전 recent_events
                 deque(40)는 5Hz NSH 프로브 출력에 밀려 STATUSTEXT를 잃었는데(실기에서
                 "Compass needs calibration" 시각을 못 잡음), 여기는 유실이 없다.
  snapshot.jsonl 주기(config.LOG_SNAPSHOT_HZ) 상태 스냅샷 — rc/armed/모터/FDM/센서 health/
                 메시지 카운트. 예전 DebugLogger.write()가 하던 일.
  browser.jsonl  브라우저 -> 브릿지 메시지 원본(키 입력, rc, ARM, RESET).
  mavlink.tlog   수신 MAVLink 원본 바이트(pymavlink 표준 tlog) — QGC/MAVExplorer로
                 재생·분석 가능. 기록 자체는 mavlink_link.py의 connect()가 한다
                 (재연결마다 append).

모든 레코드에 t(경과초)와 wall(ISO 시각)을 같이 넣어 파일 간, 그리고 FC ulog와 교차
대조할 수 있게 한다. 스레드 세이프(websocket asyncio 스레드 + 메인 루프).
"""
import datetime as _dt
import json
import os
import platform
import subprocess
import sys
import threading
import time


def _now_iso():
    return _dt.datetime.now().isoformat(timespec="milliseconds")


class _Tee:
    """sys.stdout/stderr 대체 — 터미널엔 원문, 파일엔 줄마다 타임스탬프를 붙여 기록.
    print()가 조각(끝에 개행 없는 문자열)으로 여러 번 write할 수 있으므로 개행이 올 때까지
    모아서 한 줄로 만든다."""

    def __init__(self, stream, fileobj, lock, t0, label):
        self._stream = stream
        self._file = fileobj
        self._lock = lock
        self._t0 = t0
        self._label = label
        self._buf = ""

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:
            pass  # 터미널 인코딩 문제(cp949 등)로 원문 출력이 실패해도 파일 기록은 계속
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._file.write("%s +%09.3f %s %s\n" % (_now_iso(), time.time() - self._t0, self._label, line))
            self._file.flush()

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")


class SessionLogger:
    def __init__(self, base_dir, tag=None):
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = stamp + ("_" + _sanitize(tag) if tag else "")
        self.dir = os.path.join(base_dir, name)
        os.makedirs(self.dir, exist_ok=True)
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._session = {}
        self._events = open(os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8")
        self._snapshots = open(os.path.join(self.dir, "snapshot.jsonl"), "a", encoding="utf-8")
        self._browser = open(os.path.join(self.dir, "browser.jsonl"), "a", encoding="utf-8")
        self._console = open(os.path.join(self.dir, "console.log"), "a", encoding="utf-8")
        self._orig_stdout = None
        self._orig_stderr = None

    # ── 경로 ─────────────────────────────────────────────────────────────────
    def path(self, filename):
        return os.path.join(self.dir, filename)

    # ── 콘솔 tee ─────────────────────────────────────────────────────────────
    def install_console_tee(self):
        """이 시점 이후의 print()/트레이스백을 console.log에도 남긴다. 가능한 한 일찍
        (시리얼 연결 시도 전에) 호출할 것."""
        self._orig_stdout, self._orig_stderr = sys.stdout, sys.stderr
        sys.stdout = _Tee(self._orig_stdout, self._console, self._lock, self.t0, "OUT")
        sys.stderr = _Tee(self._orig_stderr, self._console, self._lock, self.t0, "ERR")

    # ── session.json ─────────────────────────────────────────────────────────
    def write_session_header(self, config_module, extra=None):
        """config 전체와 환경 정보를 session.json에 쓴다. 이후 update_session()으로 필드
        추가 가능(CAL ID, ulog 경로, FC 시간 매핑 등은 나중에 알게 되므로)."""
        cfg = {}
        for k in dir(config_module):
            if not k.isupper():
                continue
            v = getattr(config_module, k)
            try:
                json.dumps(v)
                cfg[k] = v
            except (TypeError, ValueError):
                cfg[k] = repr(v)
        try:
            import pymavlink
            pymav_ver = getattr(pymavlink, "__version__", "?")
        except Exception:
            pymav_ver = "?"
        self._session = {
            "started_at": _dt.datetime.fromtimestamp(self.t0).isoformat(timespec="milliseconds"),
            "started_at_unix": self.t0,
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "log_dir": os.path.abspath(self.dir),
            "git_commit": _git_commit(os.path.dirname(os.path.abspath(__file__))),
            "python": sys.version,
            "platform": platform.platform(),
            "pymavlink": pymav_ver,
            "config": cfg,
        }
        if extra:
            self._session.update(extra)
        self._flush_session()

    def update_session(self, **fields):
        with self._lock:
            self._session.update(fields)
        self._flush_session()

    def _flush_session(self):
        with self._lock:
            tmp = self.path("session.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._session, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path("session.json"))

    # ── 레코드 ───────────────────────────────────────────────────────────────
    def _stamp(self, rec):
        rec["t"] = round(time.time() - self.t0, 3)
        rec["wall"] = _now_iso()
        return rec

    def _write(self, fileobj, rec):
        with self._lock:
            fileobj.write(json.dumps(self._stamp(rec), ensure_ascii=False) + "\n")
            fileobj.flush()

    def event(self, kind, text="", **extra):
        """kind 예: statustext, command_ack, nsh, arm_state, mode, system_time, link,
        reboot, reset, arm_request, bridge."""
        rec = {"kind": kind, "text": text}
        rec.update(extra)
        self._write(self._events, rec)

    def browser(self, msg):
        self._write(self._browser, {"msg": msg})

    def snapshot(self, rec):
        self._write(self._snapshots, rec)

    # ── mavlink_link.on_event 훅 ─────────────────────────────────────────────
    def on_link_event(self, kind, text, **extra):
        """MavlinkLink가 수신한 FCC 이벤트를 그대로 events.jsonl에 넣고, 세션 메타에 도움이
        되는 것(FC ulog 경로, FC 시간 매핑)은 session.json에도 반영한다."""
        self.event(kind, text, **extra)
        if kind == "statustext" and "[logger]" in text and ".ulg" in text:
            path = text.split("[logger]", 1)[1].strip()
            self.update_session(fc_ulog=path)
        elif kind == "system_time":
            self.update_session(fc_time_map=extra)

    def close(self):
        for f in (self._events, self._snapshots, self._browser):
            try:
                f.close()
            except Exception:
                pass
        if self._orig_stdout is not None:
            sys.stdout, sys.stderr = self._orig_stdout, self._orig_stderr
        try:
            self._console.close()
        except Exception:
            pass


def build_snapshot(link, rc_values, snap, motor_source):
    """예전 main.DebugLogger.write()의 레코드 구조를 그대로 유지(분석 스크립트 호환).
    events는 events.jsonl로 분리됐으므로 여기서 빼고, FDM 자세는 wrap된 오일러 파생값."""
    return {
        "kind": "snapshot",
        "rc": {k: round(v, 3) for k, v in rc_values.items()},
        "armed": bool(link.is_armed()),
        "link_ok": bool(link.fcc_link_ok()),
        "motors": [round(m, 4) for m in link.latest_actuator["motors"]],
        "ofp_motors": [round(m, 4) for m in link.latest_ofp_motors["motors"]],
        "motor_source": motor_source,
        "actuator_controls": [round(v, 4) for v in link.latest_actuator["all_controls"]],
        "actuator_flags": link.latest_actuator["flags"],
        "manual_control": dict(link.last_manual_control) if link.last_manual_control else None,
        "tilt_sp": round(link.latest_actuator["tilt_setpoint"], 4),
        "fdm": {
            "north": round(snap["north"], 2), "east": round(snap["east"], 2),
            "alt": round(snap["alt"], 2),
            "roll": round(snap["roll"], 3), "pitch": round(snap["pitch"], 3),
            "heading": round(snap["heading"], 3),
            "p": round(snap["p"], 3), "q": round(snap["q"], 3), "r": round(snap["r"], 3),
        },
        "msgs": dict(link.msg_counts),
        "tx": dict(link.tx_counts),
        "sensors": dict(link.sys_status_sensors),
        "hires_imu": dict(link.latest_highres_imu),
    }


def _sanitize(tag):
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(tag))[:40]


def _git_commit(repo_dir):
    """현재 커밋 해시(짧게). git 명령이 실패하면(Windows Python이 UNC 경로
    \\\\wsl.localhost\\... 에서 git을 못 쓰는 경우 등) .git/HEAD를 직접 읽어 폴백."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir,
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    # 폴백: 상위 폴더로 올라가며 .git 찾기
    d = os.path.abspath(repo_dir)
    for _ in range(6):
        git_dir = os.path.join(d, ".git")
        if os.path.isdir(git_dir):
            try:
                head = open(os.path.join(git_dir, "HEAD"), encoding="utf-8").read().strip()
                if head.startswith("ref:"):
                    ref = head.split(" ", 1)[1].strip()
                    ref_path = os.path.join(git_dir, *ref.split("/"))
                    if os.path.exists(ref_path):
                        return open(ref_path, encoding="utf-8").read().strip()[:7]
                    packed = os.path.join(git_dir, "packed-refs")
                    if os.path.exists(packed):
                        for line in open(packed, encoding="utf-8"):
                            if line.strip().endswith(" " + ref):
                                return line.split()[0][:7]
                    return None
                return head[:7]
            except Exception:
                return None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None
