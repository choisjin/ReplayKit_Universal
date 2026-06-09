"""composite_cluster_layers 단위 테스트 (디바이스 불필요, 일회성).

배경 플레인 + 알람/정보 오버레이 플레인을 합성하는 로직을 합성 PNG 2장으로 검증.
실행: python _test_cluster_composite.py
"""
import cv2
import numpy as np

from app.services.hkmc6th_service import composite_cluster_layers, _parse_bgr

# 작은 캔버스로 테스트 (W=40, H=20). 사각형 영역 = rows 5..15, cols 10..30.
H, W = 20, 40
RY0, RY1, RX0, RX1 = 5, 15, 10, 30
GRAY = (128, 128, 128)   # 배경 (B,G,R)
RED = (0, 0, 255)        # 오버레이 알람 색 (B,G,R)


def _png(img):
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _bg():
    img = np.full((H, W, 3), GRAY, dtype=np.uint8)
    return _png(img)


def _ov_alpha():
    """RGBA: 사각형만 빨강 alpha=255, 나머지 투명(alpha=0)."""
    img = np.zeros((H, W, 4), dtype=np.uint8)
    img[RY0:RY1, RX0:RX1, 0] = RED[0]
    img[RY0:RY1, RX0:RX1, 1] = RED[1]
    img[RY0:RY1, RX0:RX1, 2] = RED[2]
    img[RY0:RY1, RX0:RX1, 3] = 255
    return _png(img)


def _ov_chroma():
    """불투명: 사각형만 빨강, 나머지 검정(키 색)."""
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[RY0:RY1, RX0:RX1] = RED
    return _png(img)


def _assert_composited(out, where):
    assert out is not None, f"{where}: out is None"
    assert out.shape == (H, W, 3), f"{where}: shape {out.shape}"
    # 사각형 안 = 빨강
    assert tuple(int(c) for c in out[10, 20]) == RED, f"{where}: center not red ({out[10,20]})"
    # 사각형 밖 = 배경 회색 유지
    assert tuple(int(c) for c in out[1, 1]) == GRAY, f"{where}: corner not gray ({out[1,1]})"


def test_alpha_mode():
    out = composite_cluster_layers(_bg(), _ov_alpha(), mode="alpha")
    _assert_composited(out, "alpha")


def test_chroma_mode():
    out = composite_cluster_layers(_bg(), _ov_chroma(), mode="chroma",
                                   key_color=(0, 0, 0), threshold=24)
    _assert_composited(out, "chroma")


def test_alpha_falls_back_to_chroma_when_no_alpha():
    # alpha 모드인데 오버레이에 알파가 없으면 chroma 경로로 폴백.
    out = composite_cluster_layers(_bg(), _ov_chroma(), mode="alpha",
                                   key_color=(0, 0, 0), threshold=24)
    _assert_composited(out, "alpha-fallback")


def test_overlay_resized_to_background():
    # 오버레이가 배경보다 작으면 배경 크기로 resize 후 합성(크래시 없이 동작).
    small = np.zeros((H // 2, W // 2, 4), dtype=np.uint8)
    small[:, :, 2] = 255  # 전체 빨강
    small[:, :, 3] = 255  # 전체 불투명
    out = composite_cluster_layers(_bg(), _png(small), mode="alpha")
    assert out is not None and out.shape == (H, W, 3)
    # 전체 오버레이라 중앙은 빨강
    assert tuple(int(c) for c in out[10, 20]) == RED


def test_empty_overlay_returns_background():
    out = composite_cluster_layers(_bg(), b"", mode="alpha")
    assert out is not None
    assert tuple(int(c) for c in out[10, 20]) == GRAY


def test_corrupt_overlay_returns_background():
    out = composite_cluster_layers(_bg(), b"not-a-png", mode="alpha")
    assert out is not None
    assert tuple(int(c) for c in out[10, 20]) == GRAY


def test_corrupt_background_returns_none():
    out = composite_cluster_layers(b"not-a-png", _ov_alpha(), mode="alpha")
    assert out is None


def test_chroma_threshold_boundary():
    # 키=검정, 오버레이 사각형을 (0,0,20) 어두운 파랑으로. threshold=24면 마스킹 안됨(배경 유지),
    # threshold=10이면 마스킹됨.
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[RY0:RY1, RX0:RX1] = (20, 0, 0)
    ov = _png(img)
    out_hi = composite_cluster_layers(_bg(), ov, mode="chroma", key_color=(0, 0, 0), threshold=24)
    assert tuple(int(c) for c in out_hi[10, 20]) == GRAY, "threshold24: should keep bg"
    out_lo = composite_cluster_layers(_bg(), ov, mode="chroma", key_color=(0, 0, 0), threshold=10)
    assert tuple(int(c) for c in out_lo[10, 20]) == (20, 0, 0), "threshold10: should overlay"


def _make_svc(**kw):
    from app.services.hkmc6th_service import HKMC6thService
    kw.setdefault("ssh_username", "root")
    return HKMC6thService("1.2.3.4", 6655, device_id="T", **kw)


def test_composite_enabled_flag():
    # 합성 = mode가 alpha/chroma 이고 SSH 자격증명이 있을 때.
    assert _make_svc(cluster_composite_mode="chroma")._composite_enabled is True
    assert _make_svc(cluster_composite_mode="alpha")._composite_enabled is True
    assert _make_svc(cluster_composite_mode="off")._composite_enabled is False
    assert _make_svc()._composite_enabled is False
    # 잘못된 mode → off 정규화 (생성자가 ssh_username은 항상 root로 보정하므로 SSH는 늘 있음)
    assert _make_svc(cluster_composite_mode="bogus").cluster_composite_mode == "off"


def test_cluster_composite_tcp_bg_plus_ssh_info():
    # 배경 = TCP CMD_GETIMG cluster(회색), 정보 = QNX SSH display(검정 위 빨강) → chroma 합성.
    svc = _make_svc(cluster_composite_mode="chroma", cluster_display="2")
    assert svc._composite_enabled is True
    seen = {}

    def fake_tcp(screen_type, timeout):
        seen["tcp"] = screen_type
        return _bg()           # 회색 배경

    def fake_one_plane(disp, timeout):
        seen["disp"] = disp
        return _ov_chroma()    # 검정 배경 위 빨강 사각형(정보)

    svc._capture_tcp_raw = fake_tcp
    svc._capture_one_plane = fake_one_plane

    out_bytes = svc._capture_cluster_composite_bytes("png", 5.0)
    assert seen["tcp"] == "cluster"
    assert seen["disp"] == "2"          # SSH 정보는 cluster_display(2)
    out = cv2.imdecode(np.frombuffer(out_bytes, np.uint8), cv2.IMREAD_COLOR)
    assert out.shape == (H, W, 3)
    assert tuple(int(c) for c in out[10, 20]) == RED   # 정보(빨강) 합성됨
    assert tuple(int(c) for c in out[1, 1]) == GRAY    # 배경(회색) 유지


def test_screencap_cluster_via_ssh_info_only():
    # 합성 off + SSH creds → 정보 단독(SSH). cluster_display 인덱스 사용 확인.
    svc = _make_svc(cluster_display="2")  # mode 기본 off
    assert svc._composite_enabled is False
    seen = {}

    def fake_one_plane(disp, timeout):
        seen["disp"] = disp
        return _ov_chroma()

    svc._capture_one_plane = fake_one_plane
    out = svc._screencap_cluster_via_ssh(fmt="png", timeout=5.0)
    assert seen["disp"] == "2"
    img = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (H, W, 3)
    assert tuple(int(c) for c in img[10, 20]) == RED  # 정보 그대로(검정 배경)


def test_ssh_failure_cooldown():
    import time as _t
    svc = _make_svc(cluster_composite_mode="chroma")
    # 초기엔 cooldown 아님
    assert svc._cluster_ssh_in_cooldown() is False
    # 인증 실패 → 긴 cooldown
    svc._note_cluster_ssh_failure(Exception("Authentication failed."))
    assert svc._cluster_ssh_in_cooldown() is True
    auth_until = svc._cluster_ssh_fail_until
    assert auth_until - _t.monotonic() > 60  # 120s급
    # 일반 오류 → 짧은 cooldown
    svc._cluster_ssh_fail_until = 0.0
    svc._note_cluster_ssh_failure(Exception("timed out"))
    assert svc._cluster_ssh_in_cooldown() is True
    other_until = svc._cluster_ssh_fail_until
    assert other_until - _t.monotonic() < 30  # 10s급
    # 인증 실패 cooldown이 일반 오류보다 훨씬 길다
    assert auth_until > other_until


def test_parse_bgr():
    assert _parse_bgr("0,0,0") == (0, 0, 0)
    assert _parse_bgr("10,20,30") == (10, 20, 30)
    assert _parse_bgr("300,-5,0") == (255, 0, 0)  # 클램프
    assert _parse_bgr("bad") == (0, 0, 0)
    assert _parse_bgr("1,2") == (0, 0, 0)
    assert _parse_bgr(None, (9, 9, 9)) == (9, 9, 9)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
