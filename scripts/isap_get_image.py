#!/usr/bin/env python3
"""iSAP Agent 화면 캡처 단독 스크립트 — 접속부터 이미지 저장까지 (표준 라이브러리만 사용).

ReplayKit backend/app/services/isap_agent_service.py 의 GET IMAGE 경로를 한 파일로 옮긴 것.

  1) TCP 접속 (전석 20000 / 후석L 20001 / 후석R 20002 / 클러스터 20003 / HUD 20004)
  2) NOTI_CONNECTED(0x5E) 수신 대기 — 보내지 않는 에이전트도 있어 없으면 그냥 진행
  3) GET VERSION(0xA0), GET SCREEN WIDTH/HEIGHT(0xA3) — 캡처 영역(전체 화면) 결정
  4) GET IMAGE(0x6A) 요청: Data = Left(2) Top(2) Right(2) Bottom(2) Monitor(1)
  5) 응답 payload(=이미지 파일 바이트)를 그대로 저장

패킷 형식 (송수신 공통)
  0x61 0x61 | Length(4, BE) | Cmd | SubCmd | Resp | Data... | CRC16(2, BE) | 0x6F 0x6F
  Length = len(Cmd+SubCmd+Resp+Data),  CRC16 = poly 0xC659 / init 0xFFFF (Cmd~Data 범위)
  GET IMAGE SubCmd = 포맷: 0x42 PNG / 0x43 BMP24 / 0x44 JPEG

사용 예
  python isap_get_image.py 192.168.105.100
  python isap_get_image.py 192.168.105.100 --screen cluster --format jpeg -o clu.jpg
  python isap_get_image.py 192.168.105.100 --port 20000 --screen rear_left
"""

import argparse
import socket
import sys
import time

START_BIT = 0x61
END_BIT = 0x6F

NOTI_CONNECTED = 0x5E
CMD_GETIMG = 0x6A
CMD_GETVERSION = 0xA0
CMD_GETSCREENWIDTHHEIGHT = 0xA3

IMG_FORMATS = {"png": 0x42, "bmp": 0x43, "jpeg": 0x44}
IMG_EXT = {"png": ".png", "bmp": ".bmp", "jpeg": ".jpg"}

# 화면 → Monitor 바이트 / 전용 포트 (iSAP 스펙 표 68)
MONITOR_MAP = {"front_center": 0x00, "rear_left": 0x01, "rear_right": 0x02,
               "cluster": 0x03, "hud": 0x04}
SCREEN_PORT_MAP = {"front_center": 20000, "rear_left": 20001, "rear_right": 20002,
                   "cluster": 20003, "hud": 20004}


def crc16(data: bytes) -> int:
    """CRC16, poly 0xC659 (iSAP/IVI agent 공통)."""
    crc = 0xFFFF
    for b in data:
        tmp = (b & 0xFF) ^ (crc & 0x00FF)
        for _ in range(8):
            tmp = (tmp >> 1) ^ 0xC659 if tmp & 1 else tmp >> 1
        crc = (crc >> 8) ^ tmp
    return crc


def make_packet(cmd: int, sub_cmd: int = 0, resp: int = 0, data: bytes = b"") -> bytes:
    body = bytes([cmd, sub_cmd, resp]) + bytes(data)
    c = crc16(body)
    return (bytes([START_BIT, START_BIT]) + len(body).to_bytes(4, "big") + body
            + bytes([(c >> 8) & 0xFF, c & 0xFF, END_BIT, END_BIT]))


class ISAPClient:
    def __init__(self, host: str, port: int, timeout: float = 10.0, verbose: bool = False):
        self.host, self.port, self.timeout, self.verbose = host, port, timeout, verbose
        self.sock = None

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[isap] {msg}", flush=True)

    # ---- 연결 ----
    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.log(f"TCP connected {self.host}:{self.port}")
        # NOTI_CONNECTED 는 선택적 — 최대 3초만 기다린다.
        pkt = self.recv_packet(timeout=3.0, want=NOTI_CONNECTED, required=False)
        self.log("NOTI_CONNECTED received" if pkt else "NOTI_CONNECTED 없음 — 계속 진행")

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    # ---- 송수신 ----
    def send(self, cmd: int, sub_cmd: int = 0, resp: int = 0, data: bytes = b"") -> None:
        pkt = make_packet(cmd, sub_cmd, resp, data)
        self.log(f"SEND cmd=0x{cmd:02X} sub=0x{sub_cmd:02X} len={len(pkt)} {pkt[:24].hex(' ')}")
        self.sock.sendall(pkt)

    def _recv_exact(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise socket.timeout
            self.sock.settimeout(remain)
            chunk = self.sock.recv(min(n - len(buf), 65536))
            if not chunk:
                raise ConnectionError("연결이 끊겼습니다 (EOF)")
            buf += chunk
        return bytes(buf)

    def recv_packet(self, timeout: float, want: int, required: bool = True):
        """want 명령의 응답이 올 때까지 패킷을 읽는다. 반환 (cmd, sub, resp, data)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                header = self._recv_exact(6, deadline)
                if header[0] != START_BIT or header[1] != START_BIT:
                    raise ValueError(f"잘못된 패킷 헤더: {header.hex(' ')}")
                length = int.from_bytes(header[2:6], "big")
                rest = self._recv_exact(length + 4, deadline)  # body + CRC(2) + END(2)
            except socket.timeout:
                if required:
                    raise TimeoutError(f"응답 없음 (cmd=0x{want:02X}, {timeout:.0f}s)")
                return None
            body, crc_rx = rest[:length], int.from_bytes(rest[length:length + 2], "big")
            if crc16(body) != crc_rx:
                self.log(f"CRC 불일치 (cmd=0x{body[0]:02X}) — 무시하고 사용")
            cmd, sub, resp, data = body[0], body[1], body[2], body[3:]
            self.log(f"RECV cmd=0x{cmd:02X} sub=0x{sub:02X} resp=0x{resp:02X} data={len(data)}B")
            if cmd == want:
                return cmd, sub, resp, data
            # 다른 응답(푸시 알림 등)은 건너뛴다

    # ---- 명령 ----
    def get_version(self) -> str:
        self.send(CMD_GETVERSION)
        pkt = self.recv_packet(3.0, CMD_GETVERSION, required=False)
        return pkt[3].decode("iso-8859-1", errors="replace") if pkt else ""

    def get_screen_sizes(self) -> dict:
        """int32 BE 쌍: front, rear_left, rear_right, cluster 순 (에이전트별로 일부만)."""
        self.send(CMD_GETSCREENWIDTHHEIGHT)
        pkt = self.recv_packet(3.0, CMD_GETSCREENWIDTHHEIGHT, required=False)
        sizes = {}
        if pkt:
            d = pkt[3]
            for i, name in enumerate(("front_center", "rear_left", "rear_right", "cluster")):
                o = i * 8
                if len(d) >= o + 8:
                    sizes[name] = (int.from_bytes(d[o:o + 4], "big"),
                                   int.from_bytes(d[o + 4:o + 8], "big"))
        return sizes

    def get_image(self, width: int, height: int, monitor: int, fmt: str = "png",
                  timeout: float = 10.0) -> bytes:
        data = b"".join(v.to_bytes(2, "big") for v in (0, 0, width, height)) + bytes([monitor])
        self.send(CMD_GETIMG, IMG_FORMATS[fmt], 0, data)
        return self.recv_packet(timeout, CMD_GETIMG)[3]


def main() -> int:
    ap = argparse.ArgumentParser(description="iSAP Agent 화면 캡처 (GET IMAGE)")
    ap.add_argument("host", help="iSAP Agent IP")
    ap.add_argument("--screen", default="front_center", choices=list(MONITOR_MAP),
                    help="캡처할 화면 (기본 front_center)")
    ap.add_argument("--port", type=int, default=None,
                    help="TCP 포트 (기본: 화면별 전용 포트. 후석은 보통 20000 + Monitor 바이트)")
    ap.add_argument("--format", default="png", choices=list(IMG_FORMATS))
    ap.add_argument("--size", default=None, help="캡처 영역 WxH (기본: 에이전트가 보고한 화면 크기)")
    ap.add_argument("-o", "--output", default=None, help="저장 파일 (기본 isap_<screen>_<시각>.<ext>)")
    ap.add_argument("--timeout", type=float, default=10.0, help="이미지 응답 대기(초)")
    ap.add_argument("-v", "--verbose", action="store_true", help="송수신 패킷 로그")
    args = ap.parse_args()

    screen = args.screen
    # 클러스터/HUD 는 전용 포트에서만 응답한다(포트별 독립 제어). 후석은 대개 전석 포트 공유.
    port = args.port or (SCREEN_PORT_MAP[screen] if screen in ("cluster", "hud") else 20000)
    dedicated = port == SCREEN_PORT_MAP.get(screen) and screen != "front_center"

    cli = ISAPClient(args.host, port, verbose=args.verbose)
    try:
        cli.connect()
        ver = cli.get_version()
        sizes = cli.get_screen_sizes()
        print(f"agent version : {ver or '(응답 없음)'}")
        print(f"screen sizes  : {sizes or '(응답 없음)'}")

        if args.size:
            w, h = (int(v) for v in args.size.lower().split("x"))
        else:
            # 전용 포트 에이전트는 자기 패널 크기를 front 필드로 보고한다.
            key = "front_center" if dedicated else screen
            w, h = sizes.get(key) or sizes.get("front_center") or (1920, 720)
        monitor = MONITOR_MAP[screen]
        print(f"request       : {screen} {w}x{h} monitor=0x{monitor:02X} format={args.format} (port {port})")

        t0 = time.monotonic()
        try:
            img = cli.get_image(w, h, monitor, args.format, args.timeout)
        except TimeoutError:
            if not dedicated or monitor == 0x00:
                raise
            # 전용 포트 에이전트 중 Monitor 를 0x00 으로만 받는 경우가 있다 — 1회 재시도.
            print("응답 없음 → Monitor 0x00 으로 재시도")
            img = cli.get_image(w, h, 0x00, args.format, args.timeout)
        dt = time.monotonic() - t0
    except Exception as e:
        print(f"[실패] {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        cli.close()

    if not img:
        print("[실패] 빈 이미지 응답", file=sys.stderr)
        return 1
    out = args.output or f"isap_{screen}_{time.strftime('%Y%m%d_%H%M%S')}{IMG_EXT[args.format]}"
    with open(out, "wb") as f:
        f.write(img)
    kind = ("PNG" if img[:4] == b"\x89PNG" else "JPEG" if img[:3] == b"\xff\xd8\xff"
            else "BMP" if img[:2] == b"BM" else "unknown")
    print(f"saved         : {out} ({len(img):,} bytes, {kind}, {dt:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
