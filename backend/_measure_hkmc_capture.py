"""HKMC ccIC27 캡처 병목 측정 (일회성 진단).

PC에서 직접 6655로 CMD_GETIMG를 보내, 한 프레임을:
  - t_first (요청 → 첫 응답 바이트)  = 디바이스 캡처 지연 + 왕복 latency
  - t_xfer  (첫 바이트 → 마지막 바이트) = 4MB 전송 시간
  - total
로 분리 측정한다. 정확한 측정을 위해 ReplayKit 미러는 꺼두는 게 좋다(에이전트 캡처 직렬화).
"""
import socket
import time

HOST = "192.168.105.100"
PORT = 6655
START = 0x61
END = 0x6F
CMD_GETIMG = 0x6A
CMD_SCR = 0xA3
SCREEN_BITS_FRONT = 8
ITERS = 10


def crc16(data):
    crc = 0xFFFF
    key = 0xC659
    for b in data:
        tmp = (b & 0xFF) ^ (crc & 0xFF)
        for _ in range(8):
            tmp = (tmp >> 1) ^ key if tmp & 1 else tmp >> 1
        crc = (crc >> 8) ^ tmp
    return crc


def make_packet(cmd, data):
    agent = [cmd, 0, 0] + list(data)
    c = crc16(agent)
    n = len(agent)
    pkt = ([START, START, (n >> 24) & 0xFF, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF]
           + agent + [(c >> 8) & 0xFF, c & 0xFF, END, END])
    return bytes(pkt)


def recv_exact(sock, n, t_first_holder=None):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed")
        if t_first_holder is not None and t_first_holder[0] is None:
            t_first_holder[0] = time.perf_counter()
        buf += chunk
    return bytes(buf)


def read_packet(sock):
    """returns (cmd, data_bytes, t_first, t_done). t_first = first response byte."""
    tf = [None]
    hdr = recv_exact(sock, 6, tf)
    plen = (hdr[2] << 24) | (hdr[3] << 16) | (hdr[4] << 8) | hdr[5]
    payload = recv_exact(sock, plen + 4)
    t_done = time.perf_counter()
    cmd = payload[0]
    data = payload[3:plen]  # cmd,sub,resp 다음부터 (plen-3)
    return cmd, data, tf[0], t_done


def main():
    s = socket.socket()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(15)
    s.connect((HOST, PORT))
    hs = recv_exact(s, 13)
    print(f"handshake: {hs.hex()}")

    # screen size
    w, h = 1920, 720
    try:
        s.sendall(make_packet(CMD_SCR, []))
        for _ in range(8):
            cmd, data, tf, td = read_packet(s)
            if cmd == CMD_SCR and len(data) >= 8:
                w = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
                h = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7]
                break
    except Exception as e:
        print(f"screen size query failed ({e}); using default {w}x{h}")
    print(f"screen size: {w}x{h}")

    img_data = [0, 0, 0, 0,
                (w >> 8) & 0xFF, w & 0xFF,
                (h >> 8) & 0xFF, h & 0xFF,
                (SCREEN_BITS_FRONT >> 8) & 0xFF, SCREEN_BITS_FRONT & 0xFF]
    pkt = make_packet(CMD_GETIMG, img_data)

    print(f"\n{'iter':>4} {'capture(ms)':>12} {'xfer(ms)':>10} {'total(ms)':>10} {'bytes':>9} {'MB/s':>7}")
    caps, xfers, totals = [], [], []
    for i in range(ITERS):
        t0 = time.perf_counter()
        s.sendall(pkt)
        cmd, data, t_first, t_done = read_packet(s)
        if cmd != CMD_GETIMG:
            print(f"  (unexpected cmd 0x{cmd:02X}, retrying)")
            continue
        cap = (t_first - t0) * 1000
        xfer = (t_done - t_first) * 1000
        total = (t_done - t0) * 1000
        nbytes = len(data)
        mbps = (nbytes / 1e6) / (xfer / 1000) if xfer > 0 else 0
        print(f"{i:>4} {cap:>12.1f} {xfer:>10.1f} {total:>10.1f} {nbytes:>9} {mbps:>7.1f}")
        caps.append(cap); xfers.append(xfer); totals.append(total)

    if totals:
        n = len(totals)
        print(f"\n--- avg over {n} ---")
        print(f"capture (req->first byte): {sum(caps)/n:7.1f} ms   (min {min(caps):.1f})")
        print(f"transfer (first->last):    {sum(xfers)/n:7.1f} ms")
        print(f"total per frame:           {sum(totals)/n:7.1f} ms  => {1000*n/sum(totals):.2f} fps ceiling")
    s.close()


if __name__ == "__main__":
    main()
