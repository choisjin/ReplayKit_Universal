"""HKMC Agent 기동 상태 프로브 — 장치 문제 vs 툴 문제 판별용.

장치를 껐다 켜는 동안 이 스크립트를 돌려두면, 포트가 언제 열리고
에이전트가 핸드셰이크(NOTI_CONNECTED 13바이트)를 언제부터 보내는지
타임라인을 기록한다. 백엔드 재연결 로직과 완전히 독립적으로 동작하므로
"장치가 재기동 후 정상인데 툴이 못 붙는 것인지"를 객관적으로 가른다.

사용법 (테스트 PC에서):
    python _probe_hkmc_agent.py 192.168.105.100 6655
    python _probe_hkmc_agent.py 192.168.105.100 6655 --interval 2 --handshake-wait 3

판별 기준:
    [REFUSED]    OS/네트워크는 살아있고 포트만 닫힘 → 에이전트 미기동 (장치 문제)
    [DOWN]       타임아웃/unreachable → 장치 전원 또는 네트워크 미복구
    [TCP-ONLY]   TCP는 열리는데 핸드셰이크 미수신 → 에이전트 미초기화 (장치 문제)
    [HANDSHAKE]  13바이트 핸드셰이크 수신 → 장치 정상.
                 이 시점 이후에도 백엔드가 못 붙으면 툴 문제 (예: 60초 give-up → error)

주의: 일부 에이전트는 동시 1개 연결만 허용한다. 백엔드가 같은 장치에
연결/재연결 시도 중이면 프로브가 TCP-ONLY 로 보일 수 있으므로,
정밀 판별이 필요하면 백엔드를 잠시 끄거나 해당 디바이스를 연결해제 후 실행할 것.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from datetime import datetime

# hkmc6th_service.py connect()가 인정하는 정상 핸드셰이크 (NOTI_CONNECTED 0x5E)
KNOWN_HANDSHAKES = {
    "6161000000035e002185fd6f6f",
    "6161000000035e0000df856f6f",
}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def probe_once(host: str, port: int, connect_timeout: float, handshake_wait: float):
    """1회 프로브. (state, detail) 반환.

    state: "DOWN" | "REFUSED" | "TCP-ONLY" | "HANDSHAKE" | "HANDSHAKE?"
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout)
    try:
        sock.connect((host, port))
    except ConnectionRefusedError:
        sock.close()
        return "REFUSED", "TCP port closed (OS up, agent not listening)"
    except (socket.timeout, OSError) as e:
        sock.close()
        return "DOWN", f"no route / timeout ({type(e).__name__})"

    # TCP 연결 성공 → 핸드셰이크 대기
    sock.settimeout(handshake_wait)
    try:
        raw = sock.recv(13)
    except socket.timeout:
        sock.close()
        return "TCP-ONLY", "connected but no handshake within wait"
    except OSError as e:
        sock.close()
        return "TCP-ONLY", f"connected but recv failed ({type(e).__name__})"

    sock.close()
    if not raw:
        return "TCP-ONLY", "connected but peer closed immediately (EOF)"
    hex_val = raw.hex()
    if hex_val in KNOWN_HANDSHAKES:
        return "HANDSHAKE", f"valid NOTI_CONNECTED: {hex_val}"
    # 13바이트가 아니거나 미지의 패턴 — 에이전트 변종일 수 있으므로 hex 그대로 노출
    if len(raw) >= 7 and raw[0] == 0x61 and raw[1] == 0x61 and raw[6] == 0x5E:
        return "HANDSHAKE?", f"NOTI_CONNECTED-like but unknown bytes: {hex_val} (len={len(raw)})"
    return "TCP-ONLY", f"unexpected bytes: {hex_val} (len={len(raw)})"


def main() -> int:
    ap = argparse.ArgumentParser(description="HKMC Agent boot probe")
    ap.add_argument("host", help="device IP (e.g. 192.168.105.100)")
    ap.add_argument("port", type=int, nargs="?", default=6655, help="agent TCP port (default 6655)")
    ap.add_argument("--interval", type=float, default=2.0, help="poll interval seconds (default 2)")
    ap.add_argument("--connect-timeout", type=float, default=2.0, help="TCP connect timeout (default 2)")
    ap.add_argument("--handshake-wait", type=float, default=3.0, help="handshake wait after connect (default 3)")
    args = ap.parse_args()

    print(f"[{ts()}] probing {args.host}:{args.port} every {args.interval}s — Ctrl+C to stop")
    print(f"[{ts()}] states: DOWN(전원/망 미복구) REFUSED(OS up, 에이전트 안뜸) "
          f"TCP-ONLY(리슨만, 미초기화) HANDSHAKE(정상)")

    prev_state = None
    state_since = time.time()
    first_seen: dict[str, float] = {}
    t0 = time.time()

    try:
        while True:
            state, detail = probe_once(args.host, args.port,
                                       args.connect_timeout, args.handshake_wait)
            now = time.time()
            if state not in first_seen:
                first_seen[state] = now
            if state != prev_state:
                held = now - state_since
                transition = f" (이전 상태 {prev_state} {held:.0f}s 지속)" if prev_state else ""
                print(f"[{ts()}] +{now - t0:6.1f}s  [{state:>10}] {detail}{transition}")
                prev_state = state
                state_since = now
            else:
                # 동일 상태 지속 — 30초마다 하트비트만
                if int(now - state_since) % 30 < args.interval and now - state_since >= 30:
                    print(f"[{ts()}] +{now - t0:6.1f}s  [{state:>10}] (지속 {now - state_since:.0f}s)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    print(f"\n[{ts()}] ── 요약 ──")
    for st, at in sorted(first_seen.items(), key=lambda kv: kv[1]):
        print(f"  {st:>10}: 최초 관측 +{at - t0:.1f}s")
    if "HANDSHAKE" in first_seen:
        print("  → 장치는 HANDSHAKE 시점부터 정상. 그 이후에도 백엔드가 못 붙었다면 툴 문제"
              " (예: 'HKMC reconnect give up' 로그 확인 → status=error 60초 give-up).")
    elif "TCP-ONLY" in first_seen:
        print("  → 포트는 열렸지만 핸드셰이크 미수신 = 에이전트 미초기화 또는 동시접속 제한."
              " 백엔드를 끄고 다시 프로브해서 같은 결과면 장치(에이전트) 문제.")
    elif "REFUSED" in first_seen:
        print("  → OS는 부팅됐지만 에이전트가 리슨하지 않음 = 장치(에이전트) 문제.")
    else:
        print("  → 장치 전원/네트워크가 복구되지 않음.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
