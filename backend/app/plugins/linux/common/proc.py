"""저지연 subprocess 헬퍼.

`subprocess.Popen + readline()` 은 stdout 의 line-buffering 까지 기다리기 때문에
trigger 토큰이 같은 줄에 들어와도 줄바꿈이 나기 전까지는 인지가 늦어진다.
TH 패널 점등 지연을 ms 단위로 맞추려면 raw fd 단위로 읽고 byte 스캐너를 돌려야 한다.

API:
  - spawn(cmd, ...)            -> Popen  (raw byte stdout, line-buffer 무시)
  - scan_until(p, tokens, ...) -> ScanResult  (토큰 매칭 시 즉시 콜백)
  - terminate(p, grace)        -> 프로세스 그룹 단위 종료
"""

from __future__ import annotations

import errno
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ScanResult:
    """scan_until 결과.

    rc: 프로세스 종료코드. timeout 으로 끊긴 경우 None.
    stdout: 누적 byte 출력. trigger 콜백 호출과 무관하게 끝까지 보관.
    trigger_hit: 매칭된 토큰 (없으면 None).
    trigger_ts: 토큰 감지 monotonic 타임스탬프 (없으면 None).
    e2e_ms: spawn 시작 ~ trigger_ts 까지 경과 ms (없으면 None).
    timed_out: 타임아웃으로 끊긴 경우 True.
    """

    rc: Optional[int]
    stdout: bytes = b""
    trigger_hit: Optional[bytes] = None
    trigger_ts: Optional[float] = None
    e2e_ms: Optional[float] = None
    timed_out: bool = False


def spawn(
    cmd: list[str],
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> subprocess.Popen:
    """Linux 한정. bufsize=0 + start_new_session=True + raw bytes stdout."""
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        text=False,
        start_new_session=True,
    )


def scan_until(
    p: subprocess.Popen,
    tokens: list[bytes],
    on_match: Optional[Callable[[bytes, float], None]] = None,
    timeout: Optional[float] = None,
    spawn_ts: Optional[float] = None,
    read_size: int = 4096,
) -> ScanResult:
    """프로세스 stdout 을 byte 단위로 읽으며 토큰을 스캔.

    - 토큰 발견 시 on_match(token, monotonic_ts) 즉시 호출.
    - 첫 매칭 이후에도 stdout 은 EOF 까지 계속 읽어 stdout 필드에 누적한다.
      (콜백이 한 번만 호출되어야 하므로 매칭 후 on_match=None 으로 비운다.)
    - timeout 초과 시 SIGTERM/SIGKILL 로 종료하고 timed_out=True.
    - 빈 토큰/None 토큰은 무시. tokens 가 비어있으면 그냥 EOF 까지 읽기.

    spawn_ts 가 주어지면 e2e_ms 자동 계산.
    """
    if spawn_ts is None:
        spawn_ts = time.monotonic()
    deadline = (spawn_ts + timeout) if timeout is not None else None

    valid_tokens = [t for t in tokens if t]
    max_tok = max((len(t) for t in valid_tokens), default=0)

    fd = p.stdout.fileno() if p.stdout is not None else None
    poller = select.poll() if fd is not None else None
    if poller is not None:
        poller.register(fd, select.POLLIN)

    captured = bytearray()
    scan_buf = bytearray()  # rolling buffer: 최대 (read_size + max_tok) 크기 유지
    hit_tok: Optional[bytes] = None
    hit_ts: Optional[float] = None
    e2e_ms: Optional[float] = None
    timed_out = False

    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            poll_ms = int(remaining * 1000) + 1
        else:
            poll_ms = -1  # 무한 대기

        if fd is None or poller is None:
            break

        events = poller.poll(poll_ms)
        if not events:
            # 타임아웃
            timed_out = True
            break

        try:
            chunk = os.read(fd, read_size)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                continue
            break

        if not chunk:
            # EOF
            break

        captured.extend(chunk)

        if on_match is not None and valid_tokens:
            scan_buf.extend(chunk)
            for tok in valid_tokens:
                idx = scan_buf.find(tok)
                if idx >= 0:
                    hit_tok = tok
                    hit_ts = time.monotonic()
                    e2e_ms = (hit_ts - spawn_ts) * 1000.0
                    try:
                        on_match(tok, hit_ts)
                    except Exception:
                        # 콜백 예외는 스캔을 멈추지 않는다. 캡처는 계속.
                        pass
                    on_match = None  # 한 번만
                    valid_tokens = []
                    scan_buf.clear()
                    break
            else:
                # rolling: 가장 긴 토큰 길이 -1 만큼만 lookback 유지
                if max_tok > 1 and len(scan_buf) > max_tok:
                    del scan_buf[: len(scan_buf) - (max_tok - 1)]

    if timed_out:
        terminate(p)
        rc = p.returncode
    else:
        try:
            rc = p.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            terminate(p)
            rc = p.returncode

    # PIPE read end 를 명시적으로 닫는다 — 매 호출 fd 누수 → 장시간 후 EMFILE 방지.
    if p.stdout is not None:
        try:
            p.stdout.close()
        except OSError:
            pass

    return ScanResult(
        rc=rc,
        stdout=bytes(captured),
        trigger_hit=hit_tok,
        trigger_ts=hit_ts,
        e2e_ms=e2e_ms,
        timed_out=timed_out,
    )


def terminate(p: subprocess.Popen, grace: float = 1.0) -> None:
    """프로세스 그룹 단위 종료. SIGTERM 후 grace 초 대기 → SIGKILL."""
    if p.poll() is not None:
        return
    try:
        pgid = os.getpgid(p.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            p.terminate()
    except (ProcessLookupError, OSError):
        return

    try:
        p.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            p.kill()
    except (ProcessLookupError, OSError):
        pass

    try:
        p.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
