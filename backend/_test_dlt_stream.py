"""DLTLogging 스트리밍 정본 전환 검증 (DLT 연결 없이 내부 로직만).

검증 항목:
  1. 링버퍼 초과 시 과거분이 스트림 파일에서 읽히는지 (_iter_abs_range)
  2. MarkStep 절대 인덱스 + SearchRange가 링버퍼 밖 구간을 검색하는지
  3. ClearLogs 후 SearchAll이 클리어 이전 라인을 제외하는지
  4. StopLogging(save_path)가 덤프 대신 파일 이동을 수행하는지
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.plugins import DLTLogging as M

M._RING_MAX = 10  # 테스트용 작은 링버퍼

def _append(inst, line):
    """_process_buffer의 append 경로 재현."""
    with inst._lock:
        inst._logs.append(line)
        inst._log_capture_ts.append(0.0)
        inst._msg_counter += 1
        inst._total_count += 1
        if inst._save_file:
            inst._save_file.write(line + "\n")
            inst._save_file.flush()

tmp = tempfile.mkdtemp()
inst = M.DLTLogging("127.0.0.1", 3490)
inst._logs = M.deque(maxlen=M._RING_MAX)
inst._log_capture_ts = M.deque(maxlen=M._RING_MAX)
assert not inst._open_stream_file(os.path.join(tmp, "dlt_stream.log"))

# 30줄 기록 — 링(10)에는 20~29만 남음, 0~19는 파일에서만 읽혀야 함
for i in range(30):
    _append(inst, f"LINE_{i:03d} payload")

# 1) 전체 범위 읽기 (파일+링 혼합)
got = list(inst._iter_abs_range(0, 30))
assert got == [f"LINE_{i:03d} payload" for i in range(30)], f"range mismatch: {got[:3]}..."

# 링버퍼 밖 과거만
got = list(inst._iter_abs_range(2, 5))
assert got == ["LINE_002 payload", "LINE_003 payload", "LINE_004 payload"], got

# 2) MarkStep 절대 인덱스 + SearchRange (마크 구간이 링버퍼 밖)
inst._step_marks[1] = 3   # 절대 인덱스 3
inst._step_marks[2] = 7
r = inst.SearchRange("LINE_005", 1, 2)
assert r.startswith("PASS"), r
r = inst.SearchRange("LINE_020", 1, 2)  # 구간 밖
assert r.startswith("FAIL"), r

# SearchAll — 링버퍼 밖 과거 라인도 검색
r = inst.SearchAll("LINE_001")
assert r.startswith("PASS"), r

# 3) ClearLogs — 이후 SearchAll은 과거 제외
inst.ClearLogs()
r = inst.SearchAll("LINE_005")
assert r.startswith("FAIL"), f"ClearLogs 이후에도 과거가 검색됨: {r}"
_append(inst, "AFTER_CLEAR hello")
r = inst.SearchAll("AFTER_CLEAR")
assert r.startswith("PASS"), r

# 4) StopLogging(save_path) — 이동만 수행
dst = os.path.join(tmp, "moved", "final.log")
msg = inst.StopLogging(dst)
assert os.path.exists(dst), msg
with open(dst, encoding="utf-8") as f:
    content = f.read().splitlines()
assert content[0] == "LINE_000 payload" and content[-1] == "AFTER_CLEAR hello", content[-1]
assert len(content) == 31, len(content)

print("ALL TESTS PASSED")
