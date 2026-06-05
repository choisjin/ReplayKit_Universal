"""HKMC ccIC27 에이전트가 '별도 연결을 동시 처리하는지' 판정 (일회성).

연결 A: CMD_GETIMG 캡처를 계속 돌림(각 ~1.09s).
연결 B: 그 동안 가벼운 GetVersion(부작용 없음)을 보내 응답 지연 측정.
 - B 응답이 수~수십 ms  => 에이전트 동시 처리 => 캡처/입력 연결 분리하면 끊김 해결 가능
 - B 응답이 ~1000ms    => 에이전트 내부 직렬화 => 연결 분리해도 소용 없음

ReplayKit 미러는 꺼두고 실행할 것(제3의 캡처 추가 방지).
"""
import socket
import threading
import time
import statistics

HOST = "192.168.105.100"
PORT = 6655
START = 0x61
END = 0x6F
CMD_GETIMG = 0x6A
CMD_VER = 0xA0
CMD_SCR = 0xA3
SCREEN_BITS_FRONT = 8


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
    return bytes([START, START, (n >> 24) & 0xFF, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF]
                 + agent + [(c >> 8) & 0xFF, c & 0xFF, END, END])


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed")
        buf += chunk
    return bytes(buf)


def read_packet(sock):
    hdr = recv_exact(sock, 6)
    plen = (hdr[2] << 24) | (hdr[3] << 16) | (hdr[4] << 8) | hdr[5]
    payload = recv_exact(sock, plen + 4)
    return payload[0], payload[3:plen]


def connect():
    s = socket.socket()
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(20)
    s.connect((HOST, PORT))
    recv_exact(s, 13)  # handshake
    return s


def main():
    # --- 연결 A: 캡처 루프 ---
    connA = connect()
    w, h = 2560, 720
    try:
        connA.sendall(make_packet(CMD_SCR, []))
        for _ in range(8):
            cmd, data = read_packet(connA)
            if cmd == CMD_SCR and len(data) >= 8:
                w = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
                h = (data[4] << 24) | (data[5] << 16) | (data[6] << 8) | data[7]
                break
    except Exception:
        pass
    print(f"capture screen: {w}x{h}")
    img = [0, 0, 0, 0, (w >> 8) & 0xFF, w & 0xFF, (h >> 8) & 0xFF, h & 0xFF,
           0, SCREEN_BITS_FRONT]
    getimg = make_packet(CMD_GETIMG, img)

    stop = [False]
    cap_count = [0]

    def capture_loop():
        while not stop[0]:
            try:
                connA.sendall(getimg)
                read_packet(connA)
                cap_count[0] += 1
            except Exception as e:
                print("capture loop error:", e)
                break

    tA = threading.Thread(target=capture_loop, daemon=True)
    tA.start()
    time.sleep(0.3)  # 캡처가 한창 돌아가는 상태로 만들기

    # --- 연결 B: 캡처 중 GetVersion 지연 측정 ---
    connB = connect()
    verpkt = make_packet(CMD_VER, [])
    lat = []
    print("\nB(별도 연결) GetVersion 지연 측정 (캡처 A 동시 진행 중):")
    for i in range(15):
        t0 = time.perf_counter()
        try:
            connB.sendall(verpkt)
            cmd, data = read_packet(connB)
        except Exception as e:
            print(f"  B query {i}: ERROR {e}")
            continue
        dt = (time.perf_counter() - t0) * 1000
        lat.append(dt)
        print(f"  B query {i:2d}: {dt:7.1f} ms  (resp cmd 0x{cmd:02X})")
        time.sleep(0.2)

    stop[0] = True
    time.sleep(1.3)
    print(f"\nA 캡처 완료 수: {cap_count[0]}")
    if lat:
        print(f"B 지연: avg {statistics.mean(lat):.1f} ms,  min {min(lat):.1f},  max {max(lat):.1f}")
        print("\n판정:")
        if statistics.mean(lat) < 300:
            print("  => 수~수십 ms = 에이전트 동시 처리. 캡처/입력 연결 분리하면 터치 끊김 해결 가능! ⭐")
        elif statistics.mean(lat) > 700:
            print("  => ~1초 = 에이전트 내부 직렬화. 연결 분리해도 소용 없음.")
        else:
            print("  => 중간값. 부분적 동시성. 추가 판단 필요.")
    connA.close()
    connB.close()


if __name__ == "__main__":
    main()
