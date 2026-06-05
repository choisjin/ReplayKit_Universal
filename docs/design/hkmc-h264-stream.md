# HKMC 미러링 H.264 Relay 전환 — 설계 / PoC

> 목표: HKMC 라이브 미러의 **대역폭↓ · 부드러움↑**.
> 범위: 설계 + 격리된 PoC 모듈. (main.py 와이어링은 리뷰 후 별도 단계)
> 작성: 2026-06-05

---

## 1. 배경 — 현재 파이프라인과 "폴링"의 실체

```
[디바이스 IVI]  --TCP CMD_GETIMG(BMP 1장)-->  [백엔드]  --WS JPEG bytes-->  [브라우저 <img>]
   (Agent)         요청-응답 (1프레임/요청)        screencap_bytes        DeviceContext.tsx
```

- **브라우저 ↔ 백엔드 구간은 이미 push** (`/ws/screen`, `main.py:504`). 프론트는
  `DeviceContext.tsx:272`에서 WS를 열고 JPEG 바이너리를 받아 `<img>`에 렌더.
- **백엔드 ↔ 디바이스 구간이 요청-응답.** HKMC Agent 프로토콜에는 연속 비디오
  스트림 명령이 **없다** — `CMD_GETIMG`(0x6A)로 BMP 한 장씩 받는 게 전부
  (`hkmc6th_service.py:31`). start/stop video 류 부재.

### 병목 진단
| 구간 | 비용 | 개선 가능? |
|------|------|-----------|
| 디바이스 BMP 캡처+전송 | 1920×720×3 ≈ **4MB/프레임** TCP | ❌ Agent 펌웨어 의존(LGE 내부), 못 바꿈 |
| 백엔드 JPEG 인코딩 | 프레임당 cv2 encode | △ |
| **백엔드→브라우저 JPEG** | **프레임당 독립 JPEG (intra-only)** | ✅ **H.264 inter-frame 압축으로 대체** |
| 렌더 | `<img>` blob swap | △ (H.264는 `<video>`/WebCodecs) |

→ **디바이스 FPS 상한은 못 올린다.** 하지만 정적인 IVI 화면은 프레임 간 변화가
거의 없어, **JPEG(매 프레임 독립) → H.264(inter-frame)** 전환 시 백엔드→브라우저
대역폭이 수배~수십배 줄고, `<video>`/WebCodecs의 프레임 보간·디코딩으로 체감
부드러움이 개선된다. 프론트 H.264 경로는 이미 scrcpy(ADB)용으로 검증됨.

---

## 2. 핵심 인사이트 — 프론트는 이미 H.264를 받는다

`DeviceContext.tsx`의 WS 메시지 처리:
- `{"mode":"h264", width, height}` JSON 수신 → WebCodecs `VideoDecoder` 초기화
  (미지원 시 JMuxer 폴백), `DeviceContext.tsx:301-305, 421-441`.
- 이후 **raw H.264 NAL ArrayBuffer**를 그대로 디코더에 feed, `:331-345`.
- `{"mode":"jpeg"}` 수신 시 디코더 정리하고 `<img>` 경로로 복귀, `:306-321`.

scrcpy(ADB) 백엔드가 이 경로를 쓴다. 백엔드 WS 핸들러(`main.py:838-875`)는
`scrcpy_backend.stream_h264()`가 yield하는 NAL을 그대로 `send_bytes`한다.

> **결론: 프론트·WS 핸들러 변경 거의 없이, scrcpy 백엔드와 동일한 인터페이스
> (`video_width/height`, `stream_h264()`, `close()`)를 가진 HKMC용 H.264 백엔드를
> 만들면 끝난다.** 다른 점은 단 하나 — scrcpy는 디바이스가 보낸 H.264를 **relay**
> 하지만, HKMC는 BMP를 받아 **백엔드에서 ffmpeg로 인코딩**한다.

---

## 3. 설계

### 3.1 컴포넌트
```
HkmcH264Backend (신규, scrcpy_server.ScrcpyBackend 인터페이스 미러)
 ├─ capture task  : hkmc.async_screencap_bytes(fmt="png") 루프 → ffmpeg.stdin write
 ├─ ffmpeg proc   : -f image2pipe(png) → libx264 → -f h264 (Annex-B) stdout
 ├─ relay task    : ffmpeg.stdout 읽기 → NAL 키프레임 정렬 → asyncio.Queue
 └─ stream_h264() : Queue 소비 → yield (WS 핸들러가 send_bytes)
```

PoC는 **HKMC 서비스를 수정하지 않는다.** 기존 `async_screencap_bytes(fmt="png")`
(무손실 PNG)를 캡처 소스로 쓰고, ffmpeg가 PNG 디코드 + H.264 인코드를 담당.
→ numpy/cv2 추가 의존 0, 서비스 회귀 위험 0.

> 프로덕션 최적화 메모: PNG encode(서비스) + PNG decode(ffmpeg) round-trip은
> `-f rawvideo -pix_fmt bgr24`로 디코딩된 프레임을 직접 파이프하면 제거 가능.
> PoC에서는 격리/단순성을 위해 PNG 경로 채택.

### 3.2 ffmpeg 명령 (PoC)
```
ffmpeg -hide_banner -loglevel error
  -f image2pipe -framerate {FPS} -i pipe:0
  -an
  -c:v libx264 -preset ultrafast -tune zerolatency
  -pix_fmt yuv420p
  -g {GOP} -keyint_min {GOP} -bf 0
  -x264-params repeat-headers=1:scenecut=0
  -f h264 pipe:1
```
- `-tune zerolatency -bf 0` : B-frame 제거, 저지연.
- `-g {GOP}` : 주기적 IDR — 뒤늦게 접속한 뷰어/큐 재동기 시 디코더 시작점 확보.
  정적 화면이라도 GOP마다 IDR이 흘러 stale 감지에도 충분.
- `repeat-headers=1` : 모든 IDR 앞에 SPS/PPS 부착 → 중간 join + 키프레임 정렬 안전.
- `image2pipe + -framerate` : 우리가 feed하는 PNG들을 nominal FPS PTS로 인코딩.
  캡처가 불규칙(디바이스 의존)해도 PoC 허용 범위.

### 3.3 NAL 키프레임 정렬 (scrcpy 패턴 차용)
- ffmpeg stdout은 Annex-B start code(`00 00 01` / `00 00 00 01`)로 구분된 NAL stream.
- 최초 yield는 **SPS(type 7) 또는 IDR(type 5)** 시작점부터 (디코더 clean start).
- 소비자(WS)가 느려 큐가 차면 backlog 폐기 후 다음 키프레임부터 재동기
  (`scrcpy_server.py:660-690` 패턴 동일).
- start code가 chunk 경계에 걸치는 경우 carry(최대 3바이트) 보존.

### 3.4 WS 핸들러 통합 (리뷰 후 단계 — 본 PoC 미포함)
`main.py`의 HKMC 분기(`:597-605`)를 ADB scrcpy 분기와 유사하게 확장:

```python
# 의사코드 — force_h264 또는 설정 플래그 + ffmpeg 가용 시
if is_hkmc and hkmc and hkmc.is_connected:
    if hkmc_h264_enabled and detect_ffmpeg():
        backend = backend or await ensure_hkmc_h264_backend(hkmc, screen_type, fps)
        if backend:
            if current_ws_mode != "h264":
                await ws.send_json({"mode": "h264",
                                    "width": backend.video_width,
                                    "height": backend.video_height})
                current_ws_mode = "h264"
            async for nal in backend.stream_h264():
                await ws.send_bytes(nal)
            # 에러/종료 시 backend 정리 후 JPEG 폴백으로 자연 복귀
            ...
            continue
    # 폴백: 기존 JPEG 경로 (그대로 유지)
    jpeg = await hkmc.async_screencap_bytes(screen_type=screen_type, fmt="jpeg")
    if current_ws_mode != "jpeg":
        await ws.send_json({"mode": "jpeg"}); current_ws_mode = "jpeg"
    await ws.send_bytes(jpeg)
```

핵심: **H.264 실패 시 항상 기존 JPEG로 자연 폴백.** 신규 경로가 죽어도 미러는
계속 동작 → 회귀 0 보장. ADB가 이미 같은 폴백 구조(`main.py:891-897`).

---

## 4. 단계별 롤아웃 계획
1. **(본 문서) PoC 모듈 + 단위 검증** — `hkmc_h264_encoder.py` 격리 작성.
2. 단일 경로(6th `front_center`) WS 와이어링 + `force_h264` 플래그 뒤에 게이트.
3. 실측: 대역폭(bytes/s), 체감 FPS, 첫 프레임 latency, CPU. JPEG 대비 비교.
4. screen_type 확장(rear/cluster) + 5th 적용 + 멀티뷰어 시 공유 producer 검토.
5. 기본 활성화 여부 결정 (플래그 default on).

## 5. 리스크 & 완화
| 리스크 | 완화 |
|--------|------|
| ffmpeg 미설치 환경 | `detect_ffmpeg()` None → JPEG 경로 자동 사용 (기존과 동일) |
| PC CPU 인코딩 부하 | `ultrafast` preset; 디바이스 FPS가 낮아 인코딩 프레임 수 자체가 적음 |
| 캡처 불규칙 → PTS 흔들림 | PoC 허용. 프로덕션은 wallclock PTS(`-use_wallclock_as_timestamps`) 검토 |
| 멀티뷰어 시 디바이스 N배 | 본 PoC 범위 외. 공유 producer는 4단계에서 (별도 fan-out 설계) |
| 신규 경로 회귀 | 플래그 게이트 + 실패 시 JPEG 폴백으로 미러 무중단 |

## 6. 검증 방법 (PoC)
- `detect_ffmpeg()` 가용 환경에서 실제 HKMC(또는 mock 캡처)로 backend.start()
  → `stream_h264()` 첫 yield가 SPS/IDR로 시작하는지(첫 바이트 start code + NAL
  type 7/5) 확인.
- ffmpeg stderr tail 로 인코더 정상 기동 확인.
- 프론트 미연동 단계이므로, yield된 NAL을 파일로 덤프해 `ffprobe`로 H.264
  유효성 검증 가능.
