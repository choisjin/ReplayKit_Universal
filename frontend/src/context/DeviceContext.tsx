import { createContext, useContext, useState, useEffect, useRef, ReactNode, useCallback } from 'react';
import JMuxer from 'jmuxer';
import { deviceApi, scenarioApi } from '../services/api';
import { H264Renderer, webCodecsSupported } from '../lib/h264Renderer';

export interface ManagedDevice {
  id: string;
  type: string; // "adb" | "serial" | "module" | "hkmc_agent" | "isap_agent" | "icas_agent" | "vision_camera"
  category: string; // "primary" | "auxiliary"
  address: string;
  status: string;
  name: string;
  info: Record<string, any>;
  protected?: boolean;  // 시스템 기본 디바이스 (삭제/수정 불가)
  connect_progress?: string;  // 모듈 연결 진행 단계 (SCAR/TH 장시간 Setup, status=reconnecting 일 때)
}

interface DeviceContextType {
  primaryDevices: ManagedDevice[];
  auxiliaryDevices: ManagedDevice[];
  loading: boolean;
  fetchDevices: () => Promise<void>;
  connectDevice: (type: string, address: string, baudrate?: number, name?: string, category?: string, module?: string, connect_type?: string, extra_fields?: Record<string, any>, device_id?: string, port?: number, device_model?: string) => Promise<string>;
  disconnectDevice: (deviceId: string) => Promise<string>;
  updateDeviceLists: (data: any) => void;
  // Screenshot for a specific primary device
  screenshotDeviceId: string;
  setScreenshotDeviceId: (id: string) => void;
  screenshot: string;
  // Screenshot polling interval (ms)
  pollInterval: number;
  setPollInterval: (ms: number) => void;
  // HKMC screen type for screenshot polling
  screenType: string;
  setScreenType: (st: string) => void;
  // Force immediate screenshot refresh (call after action)
  refreshScreenshot: () => void;
  // Screen streaming alive indicator (true = frames arriving)
  screenAlive: boolean;
  // H.264 direct streaming mode
  h264Mode: boolean;
  h264Size: { width: number; height: number };
  videoRef: React.RefObject<HTMLVideoElement | null>;
  // WebCodecs H.264 렌더러 (RecordPage 의 rAF 가 drawTo 로 캔버스에 그림). 미지원 시 null.
  h264RendererRef: React.RefObject<H264Renderer | null>;
  sendControl: (msg: object) => void;
  // 실시간 FPS
  streamFps: number;
  // 시나리오 재생 중 미러링 중단 여부 (디바이스 부하 감소 — 안내 오버레이용)
  screenPausedForPlayback: boolean;
  // 화면 스트리밍 일시정지/재개 (deprecated: 재생 중단은 screenPausedForPlayback이 담당)
  pauseScreenStream: () => void;
  resumeScreenStream: () => void;
  // 디바이스 폴링 일시정지/재개
  pauseDevicePolling: () => void;
  resumeDevicePolling: () => void;
}

const DeviceContext = createContext<DeviceContextType | null>(null);

export function DeviceProvider({ children }: { children: ReactNode }) {
  const [primaryDevices, setPrimaryDevices] = useState<ManagedDevice[]>([]);
  const [auxiliaryDevices, setAuxiliaryDevices] = useState<ManagedDevice[]>([]);
  const [loading, setLoading] = useState(false);
  const [screenshotDeviceId, setScreenshotDeviceId] = useState('');
  const [screenshot, setScreenshot] = useState('');
  // 시나리오 재생 중에는 디바이스 부하를 줄이기 위해 미러 스트림을 일괄 중단한다.
  // 백엔드 재생상태(scenarioApi.playbackStatus)를 단일 소스로 폴링해 판단하므로,
  // 어느 페이지/트리거에서 시작했든·ScenarioPage 언마운트 여부와 무관하게 동작한다.
  const [screenPausedForPlayback, setScreenPausedForPlayback] = useState(false);
  const screenPausedForPlaybackRef = useRef(false);
  const [pollInterval, setPollInterval] = useState(500);
  const [screenType, setScreenType] = useState('front_center');
  const [screenAlive, setScreenAlive] = useState(false);
  const [h264Mode, setH264Mode] = useState(false);
  const [h264Size, setH264Size] = useState({ width: 1080, height: 1920 });
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const h264ModeRef = useRef(false);
  const jmuxerRef = useRef<JMuxer | null>(null);
  // WebCodecs 렌더러 (우선 경로). 미지원 브라우저는 JMuxer(<video>)로 폴백.
  const h264RendererRef = useRef<H264Renderer | null>(null);
  const h264FeedCountRef = useRef(0);  // [진단] H.264 feed 횟수 (첫 feed/주기 상태 로그용)
  const screenAliveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [streamFps, setStreamFps] = useState(0);
  const fpsCountRef = useRef(0);
  const fpsTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // FPS 계측 시작/정지
  const startFpsCounter = useCallback(() => {
    fpsCountRef.current = 0;
    if (fpsTimerRef.current) clearInterval(fpsTimerRef.current);
    fpsTimerRef.current = setInterval(() => {
      setStreamFps(fpsCountRef.current);
      fpsCountRef.current = 0;
    }, 1000);
  }, []);
  const stopFpsCounter = useCallback(() => {
    if (fpsTimerRef.current) { clearInterval(fpsTimerRef.current); fpsTimerRef.current = null; }
    setStreamFps(0);
  }, []);

  // Frame arrived → mark alive, reset 3s timeout, count fps
  const markFrameAlive = useCallback(() => {
    setScreenAlive(true);
    fpsCountRef.current += 1;
    if (screenAliveTimerRef.current) clearTimeout(screenAliveTimerRef.current);
    screenAliveTimerRef.current = setTimeout(() => setScreenAlive(false), 3000);
  }, []);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // WS 재연결 관리
  const wsRetryCountRef = useRef(0);
  const wsRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const MAX_WS_RETRIES = 3;
  const screenshotDeviceIdRef = useRef('');
  const screenTypeRef = useRef('front_center');
  const wsRef = useRef<WebSocket | null>(null);
  const prevBlobUrlRef = useRef<string>('');

  // Keep refs in sync with state for use in pollFn/refreshScreenshot
  useEffect(() => {
    screenshotDeviceIdRef.current = screenshotDeviceId;
  }, [screenshotDeviceId]);

  useEffect(() => {
    screenTypeRef.current = screenType;
  }, [screenType]);

  const updateDeviceLists = (data: any) => {
    if (data.primary) setPrimaryDevices(data.primary);
    if (data.auxiliary) setAuxiliaryDevices(data.auxiliary);
  };

  const fetchDevices = async () => {
    setLoading(true);
    try {
      const res = await deviceApi.list();
      updateDeviceLists(res.data);
    } catch { /* ignore */ }
    setLoading(false);
  };

  const connectDevice = async (type: string, address: string, baudrate?: number, name?: string, category?: string, module?: string, connect_type?: string, extra_fields?: Record<string, any>, device_id?: string, port?: number, device_model?: string): Promise<string> => {
    const res = await deviceApi.connect(type, address, baudrate, name, category, module, connect_type, extra_fields, device_id, port, device_model);
    updateDeviceLists(res.data);
    return res.data.result;
  };

  const disconnectDevice = async (deviceId: string): Promise<string> => {
    const res = await deviceApi.disconnect(deviceId);
    updateDeviceLists(res.data);
    return res.data.result;
  };

  const devicePollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const startDevicePolling = useCallback(() => {
    if (devicePollRef.current) return;
    devicePollRef.current = setInterval(fetchDevices, 10000);
  }, []);

  const pauseDevicePolling = useCallback(() => {
    if (devicePollRef.current) {
      clearInterval(devicePollRef.current);
      devicePollRef.current = null;
    }
  }, []);

  const resumeDevicePolling = useCallback(() => {
    startDevicePolling();
  }, [startDevicePolling]);

  useEffect(() => {
    fetchDevices();
    startDevicePolling();
    return () => pauseDevicePolling();
  }, []);

  // --- 디바이스 변경 시 screenType 자동 설정 ---
  const prevDeviceIdRef = useRef('');
  useEffect(() => {
    if (screenshotDeviceId === prevDeviceIdRef.current) return;
    prevDeviceIdRef.current = screenshotDeviceId;
    if (!screenshotDeviceId) return;
    const dev = primaryDevices.find(d => d.id === screenshotDeviceId);
    if (!dev) return;
    if (dev.type === 'hkmc_agent' || dev.type === 'isap_agent') {
      setScreenType('front_center');
    } else if (dev.type === 'icas_agent' || dev.type === 'mib_agent') {
      setScreenType('HU');
    } else if (dev.type === 'vision_camera' || dev.type === 'webcam') {
      setScreenType('default');
    } else if (dev.type === 'adb' && (dev.info?.displays?.length ?? 0) > 1) {
      setScreenType(String(dev.info.displays[0]?.id ?? 0));
    } else {
      setScreenType('0');
    }
  }, [screenshotDeviceId, primaryDevices]);

  // --- video 엘리먼트의 MediaSource/blob URL 강제 릴리즈 ---
  // jmuxer.destroy()는 SourceBuffer 정리만 수행하고 <video>.src 는 그대로 두어
  // MediaSource(+ 누적된 H.264 SourceBuffer 바이트)가 GC되지 않고 남는 문제 방지.
  const releaseVideoBuffer = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    try { v.pause(); } catch { /* ignore */ }
    const oldSrc = v.src;
    try {
      v.removeAttribute('src');
      v.srcObject = null;
      v.load(); // 내부 디코더/버퍼까지 리셋
    } catch { /* ignore */ }
    if (oldSrc && oldSrc.startsWith('blob:')) {
      try { URL.revokeObjectURL(oldSrc); } catch { /* ignore */ }
    }
  }, []);

  // --- WebSocket cleanup helper ---
  const closeWs = useCallback(() => {
    if (h264RendererRef.current) {
      try { h264RendererRef.current.close(); } catch { /* ignore */ }
      h264RendererRef.current = null;
    }
    if (jmuxerRef.current) {
      try { jmuxerRef.current.destroy(); } catch { /* ignore */ }
      jmuxerRef.current = null;
    }
    // ★ JMuxer destroy 직후 video 엘리먼트의 MediaSource 참조까지 끊어준다
    releaseVideoBuffer();
    h264ModeRef.current = false;
    setH264Mode(false);
    h264FeedCountRef.current = 0;
    stopFpsCounter();
    if (wsRef.current) {
      // 이전 WebSocket의 이벤트 핸들러 제거 (close 완료 전 프레임 수신 방지)
      wsRef.current.onmessage = null;
      wsRef.current.onclose = null;
      wsRef.current.onerror = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    if (prevBlobUrlRef.current) {
      URL.revokeObjectURL(prevBlobUrlRef.current);
      prevBlobUrlRef.current = '';
    }
  }, [stopFpsCounter, releaseVideoBuffer]);

  // --- Check if device is HKMC or iSAP Agent (both use TCP agent protocol) ---
  const isHkmcDevice = useCallback((deviceId: string) => {
    const dev = primaryDevices.find(d => d.id === deviceId);
    return dev?.type === 'hkmc_agent' || dev?.type === 'isap_agent';
  }, [primaryDevices]);

  // --- Check if ADB device has multi-display ---
  const hasMultiDisplay = useCallback((deviceId: string) => {
    const dev = primaryDevices.find(d => d.id === deviceId);
    return dev?.type === 'adb' && (dev.info?.displays?.length ?? 0) > 1;
  }, [primaryDevices]);

  // --- sendControl: WebSocket으로 터치/키 컨트롤 전송 ---
  const sendControl = useCallback((msg: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  // --- WebSocket screen streaming (H.264 / JPEG) ---
  // startWsStream의 최신 참조를 유지 (재연결 콜백에서 사용)
  const startWsStreamRef = useRef<((deviceId: string, st: string) => void) | null>(null);

  const startWsStream = useCallback((deviceId: string, st: string) => {
    closeWs();
    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${wsProto}//${window.location.host}/ws/screen`);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;

    // 예기치 않은 종료 시 재연결 스케줄링
    const scheduleReconnect = () => {
      if (screenshotDeviceIdRef.current !== deviceId) return;
      if (wsRetryCountRef.current >= MAX_WS_RETRIES) return;
      wsRetryCountRef.current += 1;
      const delay = 500 * wsRetryCountRef.current;
      wsRetryTimerRef.current = setTimeout(() => {
        wsRetryTimerRef.current = null;
        if (screenshotDeviceIdRef.current === deviceId && !wsRef.current) {
          startWsStreamRef.current?.(deviceId, st);
        }
      }, delay);
    };

    ws.onopen = () => {
      ws.send(JSON.stringify({ device_id: deviceId, screen_type: st }));
      startFpsCounter();
      wsRetryCountRef.current = 0; // 연결 성공 → 재시도 카운터 초기화
    };

    ws.onmessage = (event) => {
      if (typeof event.data === 'string') {
        // JSON 메시지: 모드 협상 또는 에러
        try {
          const msg = JSON.parse(event.data);
          if (msg.mode === 'h264') {
            h264ModeRef.current = true;
            setH264Mode(true);
            setH264Size({ width: msg.width || 1080, height: msg.height || 1920 });
            // 디코더는 useEffect에서 초기화 (WebCodecs 우선, JMuxer 폴백)
          } else if (msg.mode === 'jpeg') {
            // H.264 → JPEG 폴백 전환: 디코더를 정리해야 <img> 경로가 깨끗이 렌더된다.
            if (h264ModeRef.current) {
              if (h264RendererRef.current) {
                try { h264RendererRef.current.close(); } catch { /* ignore */ }
                h264RendererRef.current = null;
              }
              if (jmuxerRef.current) {
                try { jmuxerRef.current.destroy(); } catch { /* ignore */ }
                jmuxerRef.current = null;
                releaseVideoBuffer();
              }
              h264FeedCountRef.current = 0;
            }
            h264ModeRef.current = false;
            setH264Mode(false);
          } else if (msg.type === 'frame' && msg.image) {
            const mime = msg.format === 'jpeg' ? 'image/jpeg' : 'image/png';
            if (screenshotDeviceIdRef.current === deviceId) {
              setScreenshot(`data:${mime};base64,${msg.image}`);
              markFrameAlive();
            }
          }
        } catch { /* ignore */ }
      } else if (event.data instanceof ArrayBuffer) {
        if (h264ModeRef.current) {
          // H.264 NAL 데이터 — WebCodecs 렌더러 우선, 없으면 JMuxer 폴백.
          const bytes = new Uint8Array(event.data);
          if (h264RendererRef.current) {
            h264RendererRef.current.feed(bytes);
            const n = ++h264FeedCountRef.current;
            if (n === 1 || n % 300 === 0) {
              console.info(`[mirror] feed#${n} bytes=${bytes.length} hasFrame=${h264RendererRef.current.hasFrame} (WebCodecs)`);
            }
          } else if (jmuxerRef.current) {
            jmuxerRef.current.feed({ video: bytes });
          } else if (h264FeedCountRef.current === 0) {
            // 디코더 미초기화 시 데이터 드롭 (useEffect에서 곧 초기화됨) — 첫 케이스만 알림.
            console.warn('[mirror] H.264 binary arrived but decoder not ready yet (dropping until init)');
          }
          markFrameAlive();
        } else {
          // JPEG 바이너리 → Blob URL → <img>/<canvas>
          const blob = new Blob([event.data], { type: 'image/jpeg' });
          if (prevBlobUrlRef.current) {
            URL.revokeObjectURL(prevBlobUrlRef.current);
          }
          const url = URL.createObjectURL(blob);
          prevBlobUrlRef.current = url;
          if (screenshotDeviceIdRef.current === deviceId) {
            setScreenshot(url);
            markFrameAlive();
          }
        }
      }
    };

    ws.onerror = () => {
      // 에러 → 정리 후 재연결 시도
      closeWs();
      scheduleReconnect();
    };

    ws.onclose = () => {
      // closeWs()가 호출했으면 onclose=null이므로 여기 도달 = 예기치 않은 서버 종료
      closeWs(); // JMuxer, FPS 등 전체 상태 정리
      scheduleReconnect();
    };
  }, [closeWs, markFrameAlive, startFpsCounter, releaseVideoBuffer]);

  // 최신 startWsStream 참조 유지
  startWsStreamRef.current = startWsStream;

  // Prevent overlapping poll requests
  const pollInFlightRef = useRef(false);

  // Simple poll function (for non-HKMC or fallback)
  const pollFn = useCallback(async () => {
    const deviceId = screenshotDeviceIdRef.current;
    if (!deviceId) return;
    if (screenPausedForPlaybackRef.current) return;  // 재생 중 미러 부하 차단
    if (pollInFlightRef.current) return;
    pollInFlightRef.current = true;
    try {
      const res = await deviceApi.screenshot(deviceId, screenTypeRef.current);
      if (deviceId === screenshotDeviceIdRef.current && res.data.image) {
        const fmt = res.data.format || 'jpeg';
        const mime = fmt === 'jpeg' ? 'image/jpeg' : 'image/png';
        setScreenshot(`data:${mime};base64,${res.data.image}`);
        markFrameAlive();
      }
    } catch { /* ignore */ }
    pollInFlightRef.current = false;
  }, []);

  const refreshScreenshot = useCallback(async () => {
    const deviceId = screenshotDeviceIdRef.current;
    if (!deviceId) return;
    // HKMC WebSocket 연결 중이면 별도 요청 불필요 (자동 갱신)
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;
    await pollFn();
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = setInterval(pollFn, pollInterval);
    }
  }, [pollInterval, pollFn]);

  // H.264 모드 시 디코더 초기화.
  //  - WebCodecs 지원: H264Renderer(VideoDecoder) 사용 — 모든 프로파일 디코딩, <video> 불필요.
  //  - 미지원: JMuxer(MSE, <video>)로 폴백 (구형 브라우저).
  useEffect(() => {
    if (!h264Mode) return;

    if (webCodecsSupported()) {
      if (!h264RendererRef.current) {
        console.info('[mirror] init H.264 via WebCodecs VideoDecoder');
        h264RendererRef.current = new H264Renderer((msg) => console.info('[mirror]', msg));
      }
      return;
    }

    // ── 폴백: JMuxer (video 엘리먼트가 DOM에 렌더된 후 실행) ──
    console.warn('[mirror] WebCodecs 미지원 → JMuxer 폴백 (High profile 디코딩 제약 가능)');
    const initJMuxer = () => {
      if (videoRef.current && !jmuxerRef.current) {
        jmuxerRef.current = new JMuxer({
          node: videoRef.current,
          mode: 'video',
          flushingTime: 1,
          fps: 60,
          debug: false,
          onReady: () => console.info('[mirror] JMuxer onReady'),
          onError: (e: any) => console.error('[mirror] JMuxer onError', e),
        });
        const vEl = videoRef.current;
        vEl.play?.().catch((err) => console.warn('[mirror] video.play() rejected', err?.name));
      }
    };
    initJMuxer();
    if (!jmuxerRef.current) {
      const timer = setInterval(() => {
        initJMuxer();
        if (jmuxerRef.current) clearInterval(timer);
      }, 50);
      return () => clearInterval(timer);
    }
  }, [h264Mode]);

  // ──────────────────────────────────────────────────────────────
  // H.264 라이브 SourceBuffer evict guard (메모리 누적 방지 안전장치)
  // ──────────────────────────────────────────────────────────────
  // JMuxer는 받은 NAL을 SourceBuffer에 append만 하고 잘라내지 않는다.
  // 화면을 오래 켜두면 SourceBuffer 바이트가 무한정 쌓여 1GB+까지도 갈 수 있음.
  // → 10초마다 video.buffered 를 검사해서 BUFFER_TRIGGER_SEC 초과 시
  //   오래된 구간을 BUFFER_KEEP_SEC 만큼만 남기고 제거한다.
  //   SourceBuffer 직접 접근 실패 시 안전하게 1회 reinit으로 폴백.
  useEffect(() => {
    if (!h264Mode) return;
    const BUFFER_TRIGGER_SEC = 30; // 30초 이상 쌓이면 정리 시작
    const BUFFER_KEEP_SEC = 10;    // 최근 10초만 유지
    const CHECK_INTERVAL_MS = 10000;

    const findSourceBuffer = (): SourceBuffer | undefined => {
      const jm: any = jmuxerRef.current;
      if (!jm) return undefined;
      // jmuxer 버전별로 SourceBuffer 위치가 다양 — 알려진 경로를 차례로 탐색
      return (
        jm?.remuxController?.mseHandler?.sourceBuffer ??
        jm?.remuxController?.mseHandler?.sourceBuffers?.video ??
        jm?.mseHandler?.sourceBuffer ??
        jm?.mseHandler?.sourceBuffers?.video ??
        jm?.sourceBuffer
      );
    };

    const evict = () => {
      const v = videoRef.current;
      if (!v || !v.buffered || v.buffered.length === 0) return;
      const start = v.buffered.start(0);
      const end = v.buffered.end(v.buffered.length - 1);
      if (end - start < BUFFER_TRIGGER_SEC) return;

      const cutoff = Math.max(start + 0.001, end - BUFFER_KEEP_SEC);
      const sb = findSourceBuffer();
      if (sb && !sb.updating) {
        try {
          sb.remove(start, cutoff);
          // 재생 헤드가 잘려나간 구간 안에 있으면 라이브 끝으로 점프
          if (v.currentTime < cutoff) {
            try { v.currentTime = Math.max(cutoff, end - 0.1); } catch { /* ignore */ }
          }
          return;
        } catch { /* fallthrough → reinit */ }
      }
      // SourceBuffer 접근/제거 실패 → 안전하게 스트림 재초기화
      // (closeWs가 video.src까지 정리하므로 메모리는 해제됨)
      closeWs();
      const dev = screenshotDeviceIdRef.current;
      if (dev) startWsStreamRef.current?.(dev, screenTypeRef.current);
    };

    const timer = setInterval(evict, CHECK_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [h264Mode, closeWs]);

  // Screenshot source management: WebSocket for HKMC, polling for ADB
  // 디바운스로 screenType 자동 설정 완료 후 WS를 1회만 연결
  const wsDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    // 이전 디바운스/재연결 타이머 취소 + 카운터 리셋
    if (wsDebounceRef.current) {
      clearTimeout(wsDebounceRef.current);
      wsDebounceRef.current = null;
    }
    if (wsRetryTimerRef.current) {
      clearTimeout(wsRetryTimerRef.current);
      wsRetryTimerRef.current = null;
    }
    wsRetryCountRef.current = 0;

    // 기존 스트림 즉시 정리
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    closeWs();

    if (!screenshotDeviceId) {
      setScreenshot('');
      return;
    }

    // 시나리오 재생 중 — 미러 스트림 중단(마지막 프레임은 그대로 유지).
    // flag 해제 시 effect가 재실행되어 자동 재연결된다 (deps에 flag 포함).
    if (screenPausedForPlayback) {
      return;
    }

    // 100ms 디바운스: deviceId 변경 → screenType 자동 설정 → 확정 후 WS 1회 연결
    wsDebounceRef.current = setTimeout(() => {
      wsDebounceRef.current = null;
      startWsStream(screenshotDeviceId, screenType);
    }, 100);

    return () => {
      if (wsDebounceRef.current) {
        clearTimeout(wsDebounceRef.current);
        wsDebounceRef.current = null;
      }
      if (wsRetryTimerRef.current) {
        clearTimeout(wsRetryTimerRef.current);
        wsRetryTimerRef.current = null;
      }
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      closeWs();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [screenshotDeviceId, screenType, screenPausedForPlayback]);

  // 백엔드 재생상태를 단일 소스로 폴링 → 재생 중이면 미러 중단 플래그 설정.
  // (ScenarioPage가 아니라 여기서 판단하므로 어떤 경로로 재생을 시작/종료해도 일관)
  useEffect(() => {
    screenPausedForPlaybackRef.current = screenPausedForPlayback;
  }, [screenPausedForPlayback]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await scenarioApi.playbackStatus();
        if (alive) setScreenPausedForPlayback(!!(r.data && r.data.running));
      } catch {
        // 백엔드 연결 실패 등 — 무시 (미러 상태 유지)
      }
    };
    tick();
    const id = setInterval(tick, 1200);
    return () => { alive = false; clearInterval(id); };
  }, []);

  // 스텝 테스트/시나리오 스텝마다 미러를 멈췄다 재개하던 동작은 제거됨.
  // 테스트 중에는 화면을 그대로 유지하고, 전체 시나리오 재생 중에만
  // screenPausedForPlayback(백엔드 재생상태 기반)이 미러를 일괄 중단한다.
  // 기존 호출부 호환을 위해 시그니처만 no-op으로 유지.
  const pauseScreenStream = useCallback(() => { /* deprecated no-op */ }, []);
  const resumeScreenStream = useCallback(() => { /* deprecated no-op */ }, []);

  return (
    <DeviceContext.Provider value={{
      primaryDevices,
      auxiliaryDevices,
      loading,
      fetchDevices,
      connectDevice,
      disconnectDevice,
      updateDeviceLists,
      screenshotDeviceId,
      setScreenshotDeviceId,
      screenshot,
      pollInterval,
      setPollInterval,
      screenType,
      setScreenType,
      refreshScreenshot,
      screenAlive,
      h264Mode,
      h264Size,
      videoRef,
      h264RendererRef,
      sendControl,
      streamFps,
      screenPausedForPlayback,
      pauseScreenStream,
      resumeScreenStream,
      pauseDevicePolling,
      resumeDevicePolling,
    }}>
      {children}
      {/* H.264 라이브 미러용 숨겨진 <video> — JMuxer가 이 엘리먼트에 MSE로 디코딩한다.
          소비 화면(RecordPage)은 rAF로 이 video를 canvas에 그려 탭/크롭/ROI 상호작용을
          기존 canvas 경로 그대로 재사용한다. videoRef가 항상 바인딩되도록 페이지와 무관하게
          provider에 상주시킨다. autoPlay+muted+playsInline = 사용자 제스처 없이 자동 재생. */}
      <video
        ref={videoRef}
        autoPlay
        muted
        playsInline
        style={{ display: 'none' }}
      />
    </DeviceContext.Provider>
  );
}

export function useDevice() {
  const ctx = useContext(DeviceContext);
  if (!ctx) throw new Error('useDevice must be used within DeviceProvider');
  return ctx;
}
