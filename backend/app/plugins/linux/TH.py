"""TH 모듈 — Linux 전용 Test Harness 신호 송신 + 시각화 패널 플러그인.

원본:
  Reference/Renault_CDC_Plugin/TH_Lib.py (tkinter)
  Reference/Renault_CDC_Plugin/RVC_Performance.txt (Robot 키워드)

차이:
  - tkinter 대신 PySide6 패널을 별도 프로세스로 호스팅 (충돌 회피)
  - subprocess line-buffering 대신 raw fd byte scanner → trigger 즉시 인식

시나리오 노출 메서드 (SHELL.py 반환 규약과 동일: "FAIL: ..." 접두사로 자동 실패 처리):
  - Send(topic_name, json_path, timeout=10)              fire-and-forget
  - SendAndUpdate(topic_name, json_path, timeout=10)     trigger 감지 시 패널 점등
  - PanelShow()                                          빈 패널만 띄움
  - PanelReset()                                         패널 검정으로 리셋
  - PanelClose()                                         패널 호스트 종료
"""

from __future__ import annotations

from typing import Optional

from .common.th_panel_client import PanelClient
from .common.th_signal import THSignal


class TH:
    """Linux Test Harness 플러그인.

    인스턴스 생성 인자:
      client_dir:    client.py 가 있는 디렉터리
      th_addr:       --ip_address 로 전달할 TH 브로커 IP
      python_bin:    client.py 실행 인터프리터 (기본 python3)
      panel:         시각화 패널 사용 여부 (기본 True)
      panel_trigger: 패널 점등을 트리거할 stdout 토큰 (기본 GEAR_LEVER_ACCEPTED_T_REVERSE)
    """

    def __init__(
        self,
        client_dir: str,
        th_addr: str,
        python_bin: str = "python3",
        panel: bool = True,
        panel_trigger: str = "GEAR_LEVER_ACCEPTED_T_REVERSE",
    ):
        self._signal = THSignal(client_dir=client_dir, th_addr=th_addr, python_bin=python_bin)
        self._panel: Optional[PanelClient] = PanelClient() if panel else None
        self._trigger_bytes = panel_trigger.encode("utf-8")

    # ── 시나리오 노출 ─────────────────────────────────
    def Send(self, topic_name: str, json_path: str, timeout: int = 10) -> str:
        """패널 갱신 없이 신호만 전송.

        Returns:
          정상: "rc=<n> elapsed_ms=<x.x>\\n<stdout 마지막 1KB>"
          실패: "FAIL: ..."
        """
        try:
            sr = self._signal.send(
                topic_name=topic_name,
                json_path=json_path,
                trigger=None,
                on_trigger=None,
                timeout=float(timeout),
            )
        except (OSError, FileNotFoundError) as e:
            return f"FAIL: TH client spawn error: {e}"

        if sr.timed_out:
            return f"FAIL: TH timeout ({timeout}s) rc={sr.rc}"

        return _format_result(sr.rc, None, sr.stdout)

    def SendAndUpdate(
        self,
        topic_name: str,
        json_path: str,
        timeout: int = 10,
        trigger: Optional[str] = None,
    ) -> str:
        """원본 'Send Signal And Update Panel' 등가.

        trigger 가 stdout 에 등장하면 즉시 패널 점등. 패널이 없으면 detection 만.

        Returns:
          매치: "rc=<n> trigger_hit=<tok> e2e_ms=<x.x>"
          미매치: "FAIL: trigger '<tok>' not detected (timeout=<n>s) rc=<rc>"
        """
        trig_bytes = trigger.encode("utf-8") if trigger else self._trigger_bytes

        def _on_trig(_ts: float) -> None:
            if self._panel is not None:
                self._panel.highlight()

        try:
            sr = self._signal.send(
                topic_name=topic_name,
                json_path=json_path,
                trigger=trig_bytes,
                on_trigger=_on_trig,
                timeout=float(timeout),
            )
        except (OSError, FileNotFoundError) as e:
            return f"FAIL: TH client spawn error: {e}"

        if not sr.trigger_hit:
            tok = trig_bytes.decode("utf-8", "replace")
            return f"FAIL: trigger '{tok}' not detected (timeout={timeout}s) rc={sr.rc}"

        return _format_result(sr.rc, sr.e2e_ms, sr.stdout, trigger_hit=sr.trigger_hit)

    def PanelShow(self) -> str:
        if self._panel is None:
            return "FAIL: panel disabled at construction"
        self._panel.reset()  # show + reset
        return "ok"

    def PanelReset(self) -> str:
        if self._panel is None:
            return "FAIL: panel disabled at construction"
        self._panel.reset()
        return "ok"

    def PanelClose(self) -> str:
        if self._panel is None:
            return "ok"  # nothing to close
        self._panel.close()
        return "ok"


def _format_result(
    rc: Optional[int],
    e2e_ms: Optional[float],
    stdout_bytes: bytes,
    trigger_hit: Optional[bytes] = None,
    tail_bytes: int = 1024,
) -> str:
    parts = [f"rc={rc}"]
    if trigger_hit is not None:
        parts.append(f"trigger_hit={trigger_hit.decode('utf-8', 'replace')}")
    if e2e_ms is not None:
        parts.append(f"e2e_ms={e2e_ms:.2f}")
    header = " ".join(parts)
    tail = stdout_bytes[-tail_bytes:].decode("utf-8", "replace").strip()
    return f"{header}\n{tail}" if tail else header
