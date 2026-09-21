/**
 * CompositorEditor
 *
 * 다중 캡처 소스(웹캠 N개 + 윈도우 N개)를 단일 캔버스에 배치/크기조정/크롭하여
 * 한 영상으로 합성 녹화하는 레이아웃 에디터 모달.
 *
 * - 좌측: 캔버스 설정, 소스 목록, 소스 추가 버튼
 * - 중앙: 캔버스 라이브 프리뷰(WS) + 박스 드래그/리사이즈
 * - 우측: 선택된 소스 속성 (위치/크기/crop/투명도/z-order)
 * - 하단: 프리셋 관리 + 캡처/녹화 버튼
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Modal, Button, Select, Input, InputNumber, Slider, Switch, Tag, Tooltip, Popconfirm,
  Space, Divider, Card, Empty, Radio, ColorPicker, App,
} from 'antd';
import type { ColorPickerProps } from 'antd';
import {
  PlusOutlined, DeleteOutlined, VideoCameraOutlined, DesktopOutlined,
  PlayCircleOutlined, PauseCircleOutlined, SaveOutlined,
} from '@ant-design/icons';
import {
  compositorApi, CompositorLayout, CompositorSourceConfig,
} from '../services/api';
import { useTranslation } from '../i18n';
import { COMPOSITOR_PRESET_EVENT, notify as notifyPresetChanged } from '../utils/compositorPreset';

interface Props {
  open: boolean;
  onClose: () => void;
  isDark: boolean;
}

interface WebcamDevice { index: number; label: string }
interface WindowProcess {
  pid: number; hwnd: number; name: string; exe_path: string;
  title: string; class_name: string; width: number; height: number;
  hidden?: boolean;  // 최소화/다른 워크스페이스 — 보이는 상태가 되면 캡처됨
}

const DEFAULT_CANVAS = {
  width: 1280, height: 720, fps: 30, background: '#000000',
  show_timestamp: true,
  timestamp_position: 'top-right' as 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right',
};

const TIMESTAMP_POSITIONS: { value: 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right'; label: string }[] = [
  { value: 'top-left', label: '↖ 좌상단' },
  { value: 'top-right', label: '↗ 우상단' },
  { value: 'bottom-left', label: '↙ 좌하단' },
  { value: 'bottom-right', label: '↘ 우하단' },
];

function genId() {
  return 'src_' + Math.random().toString(36).slice(2, 9);
}

function newWebcamSource(deviceIndex: number, label: string, canvasW: number, canvasH: number): CompositorSourceConfig {
  return {
    id: genId(),
    type: 'webcam',
    label,
    device_index: deviceIndex,
    capture_width: 1280,
    capture_height: 720,
    x: 0, y: 0,
    width: Math.min(640, canvasW),
    height: Math.min(360, canvasH),
    crop: null,
    z_order: 0,
    opacity: 1.0,
  };
}

function newWindowSource(p: WindowProcess, canvasW: number, canvasH: number): CompositorSourceConfig {
  return {
    id: genId(),
    type: 'window',
    label: p.title || p.name,
    process_name: p.name,
    title_pattern: p.title,
    hwnd: p.hwnd,
    capture_fps: 15,
    x: 0, y: 0,
    width: Math.min(p.width || 640, canvasW),
    height: Math.min(p.height || 360, canvasH),
    crop: null,
    z_order: 1,
    opacity: 1.0,
  };
}

export default function CompositorEditor({ open, onClose, isDark }: Props) {
  const { t } = useTranslation();
  const { message } = App.useApp();

  // ── Layout state ──────────────────────────────────────────
  const [canvas, setCanvas] = useState({ ...DEFAULT_CANVAS });
  const [sources, setSources] = useState<CompositorSourceConfig[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  // ── Source picker state ───────────────────────────────────
  const [webcamDevices, setWebcamDevices] = useState<WebcamDevice[]>([]);
  const [windowList, setWindowList] = useState<WindowProcess[]>([]);
  const [windowPickerOpen, setWindowPickerOpen] = useState(false);
  const [webcamPickerOpen, setWebcamPickerOpen] = useState(false);
  const [windowFilter, setWindowFilter] = useState('');
  const [windowWayland, setWindowWayland] = useState(false);

  // ── Preset state ──────────────────────────────────────────
  const [presets, setPresets] = useState<Record<string, CompositorLayout>>({});
  const [activePreset, setActivePreset] = useState<string>('');
  const [enabled, setEnabled] = useState<boolean>(false);
  const [presetName, setPresetName] = useState<string>('');

  // ── Capture/recording status ──────────────────────────────
  const [capturing, setCapturing] = useState(false);
  const [recording, setRecording] = useState(false);

  // ── Preview WS ────────────────────────────────────────────
  const [previewUrl, setPreviewUrl] = useState<string>('');
  const wsRef = useRef<WebSocket | null>(null);
  const prevBlobRef = useRef<string>('');

  const layout: CompositorLayout = useMemo(() => ({ canvas, sources }), [canvas, sources]);

  // ── Auto-apply: 레이아웃 변경 시 backend로 즉시 반영 ──────
  // - 모달 첫 진입(loadAll 직후)에는 적용하지 않도록 ready 플래그로 차단
  // - 빠른 연속 변경(드래그/리사이즈)은 debounce로 1회로 압축
  // - 캡처가 미실행이고 소스가 1개 이상이면 자동으로 start_capture 호출
  const readyRef = useRef(false);
  const applyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSentRef = useRef<string>('');
  const capturingRef = useRef(false);
  useEffect(() => { capturingRef.current = capturing; }, [capturing]);

  useEffect(() => {
    if (!open) {
      readyRef.current = false;
      return;
    }
    if (!readyRef.current) return;  // 초기 동기화 중에는 patch 발사 안 함
    if (applyTimerRef.current) clearTimeout(applyTimerRef.current);
    applyTimerRef.current = setTimeout(async () => {
      const payload = JSON.stringify(layout);
      if (payload === lastSentRef.current) return;  // 변화 없음
      lastSentRef.current = payload;
      try {
        await compositorApi.configure(layout);
        // 캡처 미실행 + 소스 존재 → 자동 시작
        if (!capturingRef.current && layout.sources.length > 0) {
          const r = await compositorApi.startCapture();
          setCapturing(true);
          const failed: string[] = r.data?.failed || [];
          if (failed.length) {
            message.warning(t('compositor.someSourcesFailed', { count: failed.length }));
          }
        }
      } catch (e: any) {
        message.error(t('compositor.applyFailed') + ': ' + (e?.response?.data?.detail || e?.message || e));
      }
    }, 200);
    return () => {
      if (applyTimerRef.current) clearTimeout(applyTimerRef.current);
    };
  }, [layout, open, message, t]);

  // ── Initial load ──────────────────────────────────────────
  const loadAll = useCallback(async () => {
    try {
      const [pr, lt, st] = await Promise.all([
        compositorApi.listPresets(),
        compositorApi.getLayout(),
        compositorApi.status(),
      ]);
      setPresets(pr.data.presets || {});
      setActivePreset(pr.data.active || '');
      setEnabled(!!pr.data.enabled);
      setPresetName(pr.data.active || '');
      // 백엔드의 현재 layout 반영 (last configure)
      if (lt.data?.canvas) {
        setCanvas({
          width: lt.data.canvas.width || DEFAULT_CANVAS.width,
          height: lt.data.canvas.height || DEFAULT_CANVAS.height,
          fps: lt.data.canvas.fps || DEFAULT_CANVAS.fps,
          background: lt.data.canvas.background || DEFAULT_CANVAS.background,
          show_timestamp: lt.data.canvas.show_timestamp !== false,
          timestamp_position: lt.data.canvas.timestamp_position || DEFAULT_CANVAS.timestamp_position,
        });
      }
      if (Array.isArray(lt.data?.sources)) setSources(lt.data.sources);
      setCapturing(!!st.data?.capturing);
      setRecording(!!st.data?.recording);
      // lastSent를 현재 백엔드 상태로 동기화 — 초기 setSources/setCanvas로 인한
      // 불필요한 재전송 방지 (auto-apply는 readyRef 가드 + lastSent 비교로 두 단계 차단)
      lastSentRef.current = JSON.stringify({
        canvas: {
          width: lt.data?.canvas?.width || DEFAULT_CANVAS.width,
          height: lt.data?.canvas?.height || DEFAULT_CANVAS.height,
          fps: lt.data?.canvas?.fps || DEFAULT_CANVAS.fps,
          background: lt.data?.canvas?.background || DEFAULT_CANVAS.background,
          show_timestamp: lt.data?.canvas?.show_timestamp !== false,
          timestamp_position: lt.data?.canvas?.timestamp_position || DEFAULT_CANVAS.timestamp_position,
        },
        sources: Array.isArray(lt.data?.sources) ? lt.data.sources : [],
      });
      // 다음 렌더 사이클에서 auto-apply 활성화
      setTimeout(() => { readyRef.current = true; }, 0);
    } catch (e: any) {
      message.error(t('compositor.loadFailed') + ': ' + (e?.message || e));
    }
  }, [message, t]);

  // ── Status polling ────────────────────────────────────────
  useEffect(() => {
    if (!open) return;
    loadAll();
    const id = setInterval(async () => {
      try {
        const r = await compositorApi.status();
        setCapturing(!!r.data?.capturing);
        setRecording(!!r.data?.recording);
      } catch { /* ignore */ }
    }, 1500);
    return () => clearInterval(id);
  }, [open, loadAll]);

  // 웹캠 PIP/재생 직전 카메라 목록에서 프리셋을 고르면 활성/사용 표시 동기화
  useEffect(() => {
    const onChanged = (ev: Event) => {
      const selected: string | null = (ev as CustomEvent).detail?.selected ?? null;
      setEnabled(!!selected);
      if (selected) setActivePreset(selected);
    };
    window.addEventListener(COMPOSITOR_PRESET_EVENT, onChanged);
    return () => window.removeEventListener(COMPOSITOR_PRESET_EVENT, onChanged);
  }, []);

  // ── Preview WS ────────────────────────────────────────────
  useEffect(() => {
    if (!open || !capturing) {
      // close ws if any
      try { wsRef.current?.close(); } catch { /* ignore */ }
      wsRef.current = null;
      if (prevBlobRef.current) { URL.revokeObjectURL(prevBlobRef.current); prevBlobRef.current = ''; }
      setPreviewUrl('');
      return;
    }
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/compositor`);
    ws.binaryType = 'blob';
    wsRef.current = ws;
    ws.onopen = () => { try { ws.send(JSON.stringify({ fps: 10, quality: 70 })); } catch { /* ignore */ } };
    ws.onmessage = (ev) => {
      if (ev.data instanceof Blob) {
        const url = URL.createObjectURL(ev.data);
        if (prevBlobRef.current) URL.revokeObjectURL(prevBlobRef.current);
        prevBlobRef.current = url;
        setPreviewUrl(url);
      }
    };
    return () => {
      try { ws.close(); } catch { /* ignore */ }
      wsRef.current = null;
      if (prevBlobRef.current) { URL.revokeObjectURL(prevBlobRef.current); prevBlobRef.current = ''; }
    };
  }, [open, capturing]);

  // ── Source operations ─────────────────────────────────────
  const updateSource = useCallback((id: string, patch: Partial<CompositorSourceConfig>) => {
    setSources(prev => prev.map(s => s.id === id ? { ...s, ...patch } : s));
  }, []);

  const removeSource = useCallback((id: string) => {
    setSources(prev => prev.filter(s => s.id !== id));
    if (selectedId === id) setSelectedId(null);
  }, [selectedId]);

  // ── Add webcam ────────────────────────────────────────────
  const openWebcamPicker = useCallback(async () => {
    try {
      const r = await compositorApi.listWebcamSources();
      setWebcamDevices(r.data.devices || []);
      setWebcamPickerOpen(true);
    } catch (e: any) {
      message.error(t('compositor.webcamEnumFailed') + ': ' + (e?.message || e));
    }
  }, [message, t]);

  const addWebcamSource = useCallback((d: WebcamDevice) => {
    setSources(prev => [...prev, newWebcamSource(d.index, d.label, canvas.width, canvas.height)]);
    setWebcamPickerOpen(false);
  }, [canvas]);

  // ── Add window ────────────────────────────────────────────
  const openWindowPicker = useCallback(async () => {
    try {
      const r = await compositorApi.listWindowSources();
      if (r.data?.available === false) {
        message.warning(t('compositor.windowUnavailable'));
        return;
      }
      setWindowList(r.data.windows || []);
      setWindowWayland(!!r.data?.wayland);
      setWindowFilter('');
      setWindowPickerOpen(true);
    } catch (e: any) {
      message.error(t('compositor.windowEnumFailed') + ': ' + (e?.message || e));
    }
  }, [message, t]);

  const addWindowSource = useCallback((p: WindowProcess) => {
    setSources(prev => [...prev, newWindowSource(p, canvas.width, canvas.height)]);
    setWindowPickerOpen(false);
  }, [canvas]);

  // ── Capture/preview lifecycle ─────────────────────────────
  const startCapture = useCallback(async () => {
    try {
      await compositorApi.configure(layout);
      const r = await compositorApi.startCapture();
      const opened: string[] = r.data?.opened || [];
      const failed: string[] = r.data?.failed || [];
      setCapturing(true);
      if (failed.length) {
        message.warning(t('compositor.someSourcesFailed', { count: failed.length }));
      } else if (opened.length) {
        message.success(t('compositor.captureStarted', { count: opened.length }));
      }
    } catch (e: any) {
      message.error(t('compositor.captureStartFailed') + ': ' + (e?.message || e));
    }
  }, [layout, message, t]);

  const stopCapture = useCallback(async () => {
    try {
      await compositorApi.stopCapture();
      setCapturing(false);
      setRecording(false);
    } catch { /* ignore */ }
  }, []);

  // ── Recording (manual test) ───────────────────────────────
  const startRecord = useCallback(async () => {
    try {
      const ts = new Date().toISOString().replace(/[:.]/g, '-');
      await compositorApi.recordStart(`manual_compositor/${ts}.mp4`);
      setRecording(true);
      message.success(t('compositor.recordStarted'));
    } catch (e: any) {
      message.error(t('compositor.recordStartFailed') + ': ' + (e?.response?.data?.detail || e?.message || e));
    }
  }, [message, t]);

  const stopRecord = useCallback(async () => {
    try {
      const r = await compositorApi.recordStop();
      setRecording(false);
      message.success(t('compositor.recordSaved', { path: r.data?.path || '' }));
    } catch (e: any) {
      message.error(e?.response?.data?.detail || e?.message || String(e));
    }
  }, [message, t]);

  // ── Presets ───────────────────────────────────────────────
  const savePreset = useCallback(async () => {
    const name = (presetName || '').trim();
    if (!name) {
      message.warning(t('compositor.presetNameRequired'));
      return;
    }
    try {
      await compositorApi.savePreset(name, layout);
      const r = await compositorApi.listPresets();
      setPresets(r.data.presets || {});
      message.success(t('compositor.presetSaved'));
    } catch (e: any) {
      message.error(e?.response?.data?.detail || e?.message || String(e));
    }
  }, [presetName, layout, message, t]);

  const loadPreset = useCallback((name: string) => {
    const p = presets[name];
    if (!p) return;
    setCanvas({ ...DEFAULT_CANVAS, ...p.canvas });
    setSources(p.sources || []);
    setPresetName(name);
    setSelectedId(null);
  }, [presets]);

  const deletePreset = useCallback(async (name: string) => {
    try {
      await compositorApi.deletePreset(name);
      const r = await compositorApi.listPresets();
      setPresets(r.data.presets || {});
      setActivePreset(r.data.active || '');
      if (presetName === name) setPresetName('');
      message.success(t('compositor.presetDeleted'));
    } catch (e: any) {
      message.error(e?.message || String(e));
    }
  }, [presetName, message, t]);

  const activate = useCallback(async (name: string, en?: boolean) => {
    try {
      await compositorApi.activatePreset(name, en);
      setActivePreset(name);
      if (en !== undefined) setEnabled(en);
      if (en ?? enabled) notifyPresetChanged(name);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || e?.message || String(e));
    }
  }, [enabled, message]);

  const toggleEnabled = useCallback(async (en: boolean) => {
    if (en && !activePreset) {
      // 활성 프리셋 미선택 + 현재 이름이 있다면 그걸 활성화
      if (presetName && presets[presetName]) {
        await activate(presetName, true);
      } else {
        message.warning(t('compositor.activatePresetFirst'));
      }
      return;
    }
    try {
      await compositorApi.activatePreset(activePreset, en);
      setEnabled(en);
      notifyPresetChanged(en ? activePreset : null);
    } catch (e: any) {
      message.error(e?.message || String(e));
    }
  }, [activePreset, presetName, presets, activate, message, t]);

  // ── Canvas drag/resize ────────────────────────────────────
  // 캔버스 디스플레이 영역(고정 비율)에서 마우스 좌표를 캔버스 좌표로 변환.
  const stageRef = useRef<HTMLDivElement>(null);
  const [stageSize, setStageSize] = useState({ w: 800, h: 450 });

  useEffect(() => {
    const calcSize = () => {
      const el = stageRef.current;
      if (!el) return;
      const containerW = el.clientWidth;
      const containerH = el.clientHeight;
      const ratio = canvas.width / canvas.height;
      let w = containerW;
      let h = w / ratio;
      if (h > containerH) {
        h = containerH;
        w = h * ratio;
      }
      setStageSize({ w: Math.max(100, Math.floor(w)), h: Math.max(100, Math.floor(h)) });
    };
    calcSize();
    window.addEventListener('resize', calcSize);
    return () => window.removeEventListener('resize', calcSize);
  }, [canvas.width, canvas.height, open]);

  const scaleX = stageSize.w / canvas.width;
  const scaleY = stageSize.h / canvas.height;

  // 드래그/리사이즈 — 단순 mousedown 핸들러
  const startDrag = useCallback((e: React.MouseEvent, src: CompositorSourceConfig, mode: 'move' | 'resize') => {
    e.stopPropagation();
    e.preventDefault();
    setSelectedId(src.id);
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = { x: src.x, y: src.y, w: src.width, h: src.height };
    const onMove = (ev: MouseEvent) => {
      const dx = (ev.clientX - startX) / scaleX;
      const dy = (ev.clientY - startY) / scaleY;
      if (mode === 'move') {
        let nx = Math.round(orig.x + dx);
        let ny = Math.round(orig.y + dy);
        nx = Math.max(0, Math.min(canvas.width - orig.w, nx));
        ny = Math.max(0, Math.min(canvas.height - orig.h, ny));
        updateSource(src.id, { x: nx, y: ny });
      } else {
        let nw = Math.max(20, Math.round(orig.w + dx));
        let nh = Math.max(20, Math.round(orig.h + dy));
        nw = Math.min(canvas.width - orig.x, nw);
        nh = Math.min(canvas.height - orig.y, nh);
        updateSource(src.id, { width: nw, height: nh });
      }
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, [scaleX, scaleY, canvas.width, canvas.height, updateSource]);

  // ── Selected source props ────────────────────────────────
  const selected = sources.find(s => s.id === selectedId) || null;

  // ── Render ────────────────────────────────────────────────
  const filteredWindows = windowList.filter(w => {
    const f = windowFilter.toLowerCase();
    return !f || w.name.toLowerCase().includes(f) || (w.title || '').toLowerCase().includes(f);
  });

  const onPickColor: ColorPickerProps['onChange'] = (color) => {
    setCanvas(c => ({ ...c, background: color.toHexString() }));
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      width="95vw"
      style={{ top: 16 }}
      styles={{ body: { padding: 8, height: 'calc(100vh - 80px)' } }}
      destroyOnHidden
      title={
        <Space>
          <span>{t('compositor.title')}</span>
          <Tag color={enabled ? 'green' : 'default'}>
            {enabled ? t('compositor.enabledTag') : t('compositor.disabledTag')}
          </Tag>
          {activePreset && <Tag color="blue">{activePreset}</Tag>}
        </Space>
      }
    >
      <div style={{ display: 'flex', height: '100%', gap: 8 }}>
        {/* ── Left panel: canvas + sources ─────────────────── */}
        <div style={{ width: 280, display: 'flex', flexDirection: 'column', gap: 8, overflow: 'auto' }}>
          <Card size="small" title={t('compositor.canvas')}>
            <div style={{ display: 'grid', gridTemplateColumns: '60px 1fr', gap: 6, alignItems: 'center' }}>
              <span>W</span>
              <InputNumber size="small" min={160} max={3840} value={canvas.width}
                onChange={v => setCanvas(c => ({ ...c, width: Number(v) || c.width }))} style={{ width: '100%' }} />
              <span>H</span>
              <InputNumber size="small" min={120} max={2160} value={canvas.height}
                onChange={v => setCanvas(c => ({ ...c, height: Number(v) || c.height }))} style={{ width: '100%' }} />
              <span>FPS</span>
              <InputNumber size="small" min={1} max={60} value={canvas.fps}
                onChange={v => setCanvas(c => ({ ...c, fps: Number(v) || c.fps }))} style={{ width: '100%' }} />
              <span>{t('compositor.background')}</span>
              <ColorPicker size="small" value={canvas.background} onChange={onPickColor} />
              <span>{t('compositor.showTimestamp')}</span>
              <Switch size="small" checked={canvas.show_timestamp} onChange={v => setCanvas(c => ({ ...c, show_timestamp: v }))} />
              <span>{t('compositor.timestampPosition')}</span>
              <Select size="small" value={canvas.timestamp_position} disabled={!canvas.show_timestamp}
                options={TIMESTAMP_POSITIONS.map(p => ({ value: p.value, label: p.label }))}
                onChange={v => setCanvas(c => ({ ...c, timestamp_position: v as typeof DEFAULT_CANVAS.timestamp_position }))}
                style={{ width: '100%' }}
              />
            </div>
          </Card>

          <Card size="small" title={t('compositor.sources')}
            extra={
              <Space size={4}>
                <Button size="small" icon={<VideoCameraOutlined />} onClick={openWebcamPicker}>
                  {t('compositor.addWebcam')}
                </Button>
                <Button size="small" icon={<DesktopOutlined />} onClick={openWindowPicker}>
                  {t('compositor.addWindow')}
                </Button>
              </Space>
            }
          >
            {sources.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('compositor.noSources')} />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {sources.map(s => (
                  <div
                    key={s.id}
                    onClick={() => setSelectedId(s.id)}
                    style={{
                      padding: '4px 6px',
                      border: `1px solid ${selectedId === s.id ? '#1677ff' : (isDark ? '#333' : '#ddd')}`,
                      borderRadius: 4, cursor: 'pointer', fontSize: 12,
                      display: 'flex', alignItems: 'center', gap: 4,
                    }}
                  >
                    {s.type === 'webcam' ? <VideoCameraOutlined /> : <DesktopOutlined />}
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {s.label || (s.type === 'webcam' ? `cam${s.device_index}` : s.process_name)}
                    </span>
                    <Tag color={s.type === 'webcam' ? 'cyan' : 'gold'} style={{ marginRight: 0 }}>
                      {s.width}×{s.height}
                    </Tag>
                    <DeleteOutlined onClick={(e) => { e.stopPropagation(); removeSource(s.id); }} />
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>

        {/* ── Center: stage ────────────────────────────────── */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <div style={{ flex: 1, position: 'relative', background: isDark ? '#1a1a1a' : '#f0f0f0',
                        display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}
               ref={stageRef}>
            <div
              style={{
                width: stageSize.w, height: stageSize.h, position: 'relative',
                background: previewUrl ? '#000' : canvas.background,
                outline: '1px solid #555',
              }}
              onMouseDown={() => setSelectedId(null)}
            >
              {previewUrl && (
                <img src={previewUrl} alt="preview"
                  style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }} />
              )}
              {/* 박스 오버레이 (드래그/리사이즈)
                  라벨은 영상에 burn-in 되지 않음 — hover/선택 시에만 편집용으로 표시. */}
              {sources.map(s => {
                const isSelected = selectedId === s.id;
                const isHovered = hoveredId === s.id;
                const showLabel = isSelected || isHovered;
                return (
                  <div
                    key={s.id}
                    onMouseDown={(e) => startDrag(e, s, 'move')}
                    onMouseEnter={() => setHoveredId(s.id)}
                    onMouseLeave={() => setHoveredId(prev => (prev === s.id ? null : prev))}
                    style={{
                      position: 'absolute',
                      left: s.x * scaleX, top: s.y * scaleY,
                      width: s.width * scaleX, height: s.height * scaleY,
                      border: `2px solid ${isSelected ? '#52c41a' : (s.type === 'webcam' ? '#13c2c2' : '#faad14')}`,
                      boxSizing: 'border-box',
                      cursor: 'move',
                      color: '#fff', fontSize: 11, padding: 2,
                      background: isSelected ? 'rgba(82,196,26,0.08)' : 'transparent',
                    }}
                  >
                    {showLabel && (
                      <div style={{ background: 'rgba(0,0,0,0.6)', padding: '0 4px', display: 'inline-block', borderRadius: 2 }}>
                        {s.label || s.id}
                      </div>
                    )}
                    {/* 우하단 리사이즈 핸들 */}
                    <div
                      onMouseDown={(e) => startDrag(e, s, 'resize')}
                      style={{
                        position: 'absolute', right: -1, bottom: -1, width: 14, height: 14,
                        background: '#52c41a', cursor: 'nwse-resize',
                      }}
                    />
                  </div>
                );
              })}
            </div>
          </div>
          {/* 캡처/녹화 버튼 — 변경사항은 자동 반영, 캡처 정지/녹화는 명시 조작 */}
          <div style={{ display: 'flex', gap: 6, padding: '6px 0', alignItems: 'center', flexWrap: 'wrap' }}>
            {capturing ? (
              <Button danger size="small" icon={<PauseCircleOutlined />} onClick={stopCapture}>
                {t('compositor.stopCapture')}
              </Button>
            ) : (
              <Button type="primary" size="small" icon={<PlayCircleOutlined />} onClick={startCapture} disabled={!sources.length}>
                {t('compositor.startCapture')}
              </Button>
            )}
            <Divider type="vertical" />
            {!recording ? (
              <Button size="small" icon={<VideoCameraOutlined />} onClick={startRecord} disabled={!capturing}>
                {t('compositor.recordTest')}
              </Button>
            ) : (
              <Button danger size="small" icon={<VideoCameraOutlined />} onClick={stopRecord}>
                {t('compositor.recordStop')}
              </Button>
            )}
            <span style={{ marginLeft: 'auto', fontSize: 11, color: '#888' }}>
              {capturing ? t('compositor.statusCapturing') : t('compositor.statusIdle')}
              {recording ? ` • ${t('compositor.statusRecording')}` : ''}
              {capturing && <Tag color="blue" style={{ marginLeft: 6 }}>{t('compositor.autoApply')}</Tag>}
            </span>
          </div>
        </div>

        {/* ── Right panel: source props + presets ──────────── */}
        <div style={{ width: 300, display: 'flex', flexDirection: 'column', gap: 8, overflow: 'auto' }}>
          <Card size="small" title={t('compositor.selectedProps')}>
            {!selected ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('compositor.selectSourceHint')} />
            ) : (
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <div>
                  <span style={{ fontSize: 11 }}>{t('compositor.label')}</span>
                  <Input size="small" value={selected.label || ''}
                    onChange={(e) => updateSource(selected.id, { label: e.target.value })} />
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '40px 1fr 40px 1fr', gap: 4, alignItems: 'center' }}>
                  <span>X</span>
                  <InputNumber size="small" min={0} max={canvas.width} value={selected.x}
                    onChange={v => updateSource(selected.id, { x: Number(v) || 0 })} style={{ width: '100%' }} />
                  <span>Y</span>
                  <InputNumber size="small" min={0} max={canvas.height} value={selected.y}
                    onChange={v => updateSource(selected.id, { y: Number(v) || 0 })} style={{ width: '100%' }} />
                  <span>W</span>
                  <InputNumber size="small" min={20} max={canvas.width} value={selected.width}
                    onChange={v => updateSource(selected.id, { width: Number(v) || 20 })} style={{ width: '100%' }} />
                  <span>H</span>
                  <InputNumber size="small" min={20} max={canvas.height} value={selected.height}
                    onChange={v => updateSource(selected.id, { height: Number(v) || 20 })} style={{ width: '100%' }} />
                </div>
                <div>
                  <span style={{ fontSize: 11 }}>Z-order</span>
                  <InputNumber size="small" value={selected.z_order ?? 0}
                    onChange={v => updateSource(selected.id, { z_order: Number(v) || 0 })} style={{ width: '100%' }} />
                </div>
                <div>
                  <span style={{ fontSize: 11 }}>{t('compositor.opacity')}: {Math.round((selected.opacity ?? 1) * 100)}%</span>
                  <Slider min={0} max={1} step={0.05}
                    value={selected.opacity ?? 1}
                    onChange={(v) => updateSource(selected.id, { opacity: v as number })} />
                </div>
                <Divider style={{ margin: '4px 0' }}>{t('compositor.crop')}</Divider>
                <Switch size="small" checkedChildren={t('compositor.cropOn')} unCheckedChildren={t('compositor.cropOff')}
                  checked={!!selected.crop}
                  onChange={(v) => updateSource(selected.id, {
                    crop: v ? { x: 0, y: 0, w: 640, h: 480 } : null,
                  })}
                />
                {selected.crop && (
                  <div style={{ display: 'grid', gridTemplateColumns: '40px 1fr 40px 1fr', gap: 4, alignItems: 'center' }}>
                    <span>X</span>
                    <InputNumber size="small" min={0} value={selected.crop.x}
                      onChange={v => updateSource(selected.id, { crop: { ...selected.crop!, x: Number(v) || 0 } })} style={{ width: '100%' }} />
                    <span>Y</span>
                    <InputNumber size="small" min={0} value={selected.crop.y}
                      onChange={v => updateSource(selected.id, { crop: { ...selected.crop!, y: Number(v) || 0 } })} style={{ width: '100%' }} />
                    <span>W</span>
                    <InputNumber size="small" min={1} value={selected.crop.w}
                      onChange={v => updateSource(selected.id, { crop: { ...selected.crop!, w: Number(v) || 1 } })} style={{ width: '100%' }} />
                    <span>H</span>
                    <InputNumber size="small" min={1} value={selected.crop.h}
                      onChange={v => updateSource(selected.id, { crop: { ...selected.crop!, h: Number(v) || 1 } })} style={{ width: '100%' }} />
                  </div>
                )}
                {selected.type === 'webcam' && (
                  <>
                    <Divider style={{ margin: '4px 0' }}>{t('compositor.captureWebcam')}</Divider>
                    <div style={{ display: 'grid', gridTemplateColumns: '60px 1fr', gap: 4 }}>
                      <span>Index</span>
                      <InputNumber size="small" value={selected.device_index ?? 0}
                        onChange={v => updateSource(selected.id, { device_index: Number(v) || 0 })} style={{ width: '100%' }} />
                      <span>W</span>
                      <InputNumber size="small" value={selected.capture_width ?? 0}
                        onChange={v => updateSource(selected.id, { capture_width: Number(v) || 0 })} style={{ width: '100%' }} />
                      <span>H</span>
                      <InputNumber size="small" value={selected.capture_height ?? 0}
                        onChange={v => updateSource(selected.id, { capture_height: Number(v) || 0 })} style={{ width: '100%' }} />
                    </div>
                  </>
                )}
                {selected.type === 'window' && (
                  <>
                    <Divider style={{ margin: '4px 0' }}>{t('compositor.captureWindow')}</Divider>
                    <div>
                      <span style={{ fontSize: 11 }}>{t('compositor.processName')}</span>
                      <Input size="small" value={selected.process_name || ''}
                        onChange={(e) => updateSource(selected.id, { process_name: e.target.value, hwnd: 0 })} />
                    </div>
                    <div>
                      <span style={{ fontSize: 11 }}>{t('compositor.titlePattern')}</span>
                      <Input size="small" value={selected.title_pattern || ''}
                        onChange={(e) => updateSource(selected.id, { title_pattern: e.target.value, hwnd: 0 })} />
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: '60px 1fr', gap: 4 }}>
                      <span>FPS</span>
                      <InputNumber size="small" min={1} max={30} value={selected.capture_fps ?? 15}
                        onChange={v => updateSource(selected.id, { capture_fps: Number(v) || 15 })} style={{ width: '100%' }} />
                    </div>
                  </>
                )}
              </Space>
            )}
          </Card>

          <Card size="small" title={t('compositor.presets')}>
            <Space direction="vertical" size={6} style={{ width: '100%' }}>
              <Input size="small" placeholder={t('compositor.presetNamePlaceholder')}
                value={presetName} onChange={(e) => setPresetName(e.target.value)} />
              <Space wrap size={4}>
                <Button size="small" icon={<SaveOutlined />} onClick={savePreset}>
                  {t('compositor.savePreset')}
                </Button>
                <Select size="small" style={{ width: 130 }} placeholder={t('compositor.loadPreset')}
                  value={undefined}
                  options={Object.keys(presets).map(n => ({ label: n, value: n }))}
                  onChange={(v) => { if (v) loadPreset(v as string); }}
                />
              </Space>
              {Object.keys(presets).length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3, maxHeight: 200, overflow: 'auto' }}>
                  {Object.keys(presets).map(name => (
                    <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11,
                                               padding: '2px 4px',
                                               background: name === activePreset ? (isDark ? '#16302b' : '#f6ffed') : 'transparent',
                                               borderRadius: 3 }}>
                      <Tooltip title={t('compositor.loadPreset')}>
                        <a onClick={() => loadPreset(name)} style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</a>
                      </Tooltip>
                      {name === activePreset && <Tag color="green" style={{ marginRight: 0 }}>{t('compositor.activeTag')}</Tag>}
                      <Tooltip title={t('compositor.activate')}>
                        <Button type="text" size="small" onClick={() => activate(name)}>★</Button>
                      </Tooltip>
                      <Popconfirm title={t('compositor.confirmDelete', { name })} onConfirm={() => deletePreset(name)}>
                        <Button type="text" size="small" icon={<DeleteOutlined />} />
                      </Popconfirm>
                    </div>
                  ))}
                </div>
              )}
              <Divider style={{ margin: '4px 0' }} />
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ flex: 1, fontSize: 12 }}>{t('compositor.useOnPlayback')}</span>
                <Switch size="small" checked={enabled} onChange={toggleEnabled} />
              </div>
              <div style={{ fontSize: 10, color: '#888', lineHeight: 1.4 }}>
                {t('compositor.useHint')}
              </div>
            </Space>
          </Card>
        </div>
      </div>

      {/* ── Webcam picker ───────────────────────────────────── */}
      <Modal
        open={webcamPickerOpen}
        title={t('compositor.pickWebcam')}
        footer={null}
        onCancel={() => setWebcamPickerOpen(false)}
        destroyOnHidden
        width={420}
      >
        {webcamDevices.length === 0 ? (
          <Empty description={t('compositor.noWebcams')} />
        ) : (
          <Radio.Group style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {webcamDevices.map(d => (
              <Button key={d.index} size="small" block onClick={() => addWebcamSource(d)} style={{ textAlign: 'left' }}>
                <Tag color="cyan">#{d.index}</Tag> {d.label}
              </Button>
            ))}
          </Radio.Group>
        )}
      </Modal>

      {/* ── Window picker ───────────────────────────────────── */}
      <Modal
        open={windowPickerOpen}
        title={t('compositor.pickWindow')}
        footer={null}
        onCancel={() => setWindowPickerOpen(false)}
        destroyOnHidden
        width={640}
      >
        <Input.Search size="small" placeholder={t('compositor.filterPlaceholder')}
          value={windowFilter} onChange={(e) => setWindowFilter(e.target.value)}
          style={{ marginBottom: 8 }} allowClear />
        {windowWayland && (
          <div style={{ fontSize: 11, color: '#faad14', marginBottom: 6, lineHeight: 1.4 }}>
            {t('compositor.waylandHint')}
          </div>
        )}
        <div style={{ maxHeight: '60vh', overflow: 'auto', display: 'flex', flexDirection: 'column', gap: 4 }}>
          {filteredWindows.map(w => (
            <Button key={w.hwnd} size="small" block onClick={() => addWindowSource(w)}
              style={{ textAlign: 'left', height: 'auto', padding: '4px 6px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, width: '100%' }}>
                <Tag color="gold" style={{ marginRight: 0 }}>{w.name}</Tag>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {w.title}
                </span>
                {w.hidden && <Tooltip title={t('compositor.hiddenWindowHint')}><Tag color="default">{t('compositor.hiddenWindow')}</Tag></Tooltip>}
                <Tag>{w.width}×{w.height}</Tag>
              </div>
            </Button>
          ))}
          {filteredWindows.length === 0 && <Empty description={t('compositor.noWindows')} />}
        </div>
      </Modal>
    </Modal>
  );
}
