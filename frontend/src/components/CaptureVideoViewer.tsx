import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Pagination, Segmented, Slider, Spin, Tooltip } from 'antd';
import {
  AppstoreOutlined, LeftOutlined, PictureOutlined, RightOutlined, VerticalLeftOutlined, VerticalRightOutlined,
  VideoCameraOutlined, ZoomInOutlined, ZoomOutOutlined,
} from '@ant-design/icons';
import VideoTransport from './VideoTransport';
import { encodePathSegments, resultsApi } from '../services/api';
import { useTranslation } from '../i18n';

// Webcam.Capture 스텝 영상 뷰어 — 스텝 테스트 결과창 / 시나리오 결과 상세 / Capture 일괄보기 공용.
//   영상: 결과 상세 녹화와 같은 VideoTransport 플레이어.
//   Frame으로 보기: 서버가 영상을 전 프레임 JPEG 로 분해 → 슬라이드 ↔ 그리드(한 줄 개수 조절).
//   슬라이드 단축키: ←/→ 또는 Q/E = 1프레임, A/D = 1초, Home/End = 처음/끝.
//   F/R = 다음/이전 캡처 영상 (onSwitchRecording 이 있을 때). 영상을 바꿔도 보던 모드(영상/슬라이드/그리드)는 유지.

interface FramesInfo {
  count: number;
  fps: number;
  width: number;
  height: number;
  url_prefix: string;
  start: number;
  digits: number;
}

interface Props {
  /** results/ 기준 상대경로 (StepResult.capture_video) */
  videoPath: string;
  /** 영상/슬라이드 최대 높이(px) */
  maxHeight?: number;
  /** F/R — 다음(1)/이전(-1) 캡처 영상으로 전환. 전환했으면 true */
  onSwitchRecording?: (dir: 1 | -1, wasPlaying: boolean) => boolean;
  /** 영상이 바뀌어 새로 로드될 때 자동 재생 */
  autoPlay?: boolean;
  /** 그리드 한 줄 개수 초기값 (0 = 폭에 맞춰 자동) */
  defaultGridColumns?: number;
}

const GRID_PAGE = 240;
const GRID_MAX_COLS = 24;
const GRID_AUTO_MIN_PX = 120;

const isEditableTarget = (target: EventTarget | null): boolean => {
  const el = target as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
};

export default function CaptureVideoViewer({
  videoPath, maxHeight = 460, onSwitchRecording, autoPlay = false, defaultGridColumns = 0,
}: Props) {
  const { t } = useTranslation();
  const [videoEl, setVideoEl] = useState<HTMLVideoElement | null>(null);
  const [mode, setMode] = useState<'video' | 'frames'>('video');
  const [layout, setLayout] = useState<'slide' | 'grid'>('slide');
  const [frames, setFrames] = useState<FramesInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [index, setIndex] = useState(0);
  const [gridPage, setGridPage] = useState(1);
  const [gridCols, setGridCols] = useState(defaultGridColumns);
  const rootRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  // 영상 → 프레임 전환 시 보던 시각 (프레임 정보 로드 후 해당 프레임으로 이동)
  const pendingTimeRef = useRef<number | null>(null);
  const onSwitchRef = useRef(onSwitchRecording);
  onSwitchRef.current = onSwitchRecording;

  // 영상이 바뀌면 프레임 정보만 초기화 — 모드/레이아웃/그리드 크기는 유지해 캡처끼리 같은 방식으로 비교
  useEffect(() => {
    setFrames(null);
    setError('');
    setIndex(0);
    setGridPage(1);
    pendingTimeRef.current = null;
  }, [videoPath]);

  // 프레임 모드인데 정보가 없으면 로드
  useEffect(() => {
    if (mode !== 'frames' || frames || error) return;
    let cancelled = false;
    setLoading(true);
    resultsApi.captureFrames(videoPath)
      .then(res => {
        if (cancelled) return;
        const info = res.data as FramesInfo;
        setFrames(info);
        const t0 = pendingTimeRef.current;
        pendingTimeRef.current = null;
        if (t0 != null && info.fps > 0 && info.count > 0) {
          setIndex(Math.min(info.count - 1, Math.max(0, Math.floor(t0 * info.fps + 1e-6))));
        }
      })
      .catch((e: any) => { if (!cancelled) setError(e.response?.data?.detail || t('capture.framesFailed')); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [mode, videoPath, frames, error, t]);

  const videoUrl = `/api/results/video/${encodePathSegments(videoPath)}`;
  const frameUrl = useCallback((i: number) => {
    if (!frames) return '';
    return `${frames.url_prefix}${String(i + frames.start).padStart(frames.digits, '0')}.jpg`;
  }, [frames]);
  const frameTime = (i: number) => (frames && frames.fps > 0 ? i / frames.fps : 0);

  const openFrames = () => {
    const cur = videoEl?.currentTime ?? 0;
    videoEl?.pause();
    if (frames && frames.fps > 0 && frames.count > 0) {
      setIndex(Math.min(frames.count - 1, Math.max(0, Math.floor(cur * frames.fps + 1e-6))));
    } else {
      pendingTimeRef.current = cur;
    }
    setMode('frames');
  };

  const backToVideo = () => {
    // 보던 프레임 위치에서 영상을 이어 볼 수 있게 (반 프레임 안쪽으로 seek)
    if (videoEl && frames && frames.fps > 0) {
      videoEl.currentTime = (index + 0.5) / frames.fps;
    }
    setMode('video');
  };

  const go = useCallback((i: number) => {
    if (!frames || frames.count <= 0) return;
    setIndex(Math.min(frames.count - 1, Math.max(0, i)));
  }, [frames]);

  // 프레임 모드 단축키 (영상 모드는 VideoTransport 가 처리)
  useEffect(() => {
    if (mode !== 'frames') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
      if (isEditableTarget(e.target)) return;
      if (!rootRef.current || rootRef.current.getClientRects().length === 0) return;
      let handled = true;
      const slide = layout === 'slide' && frames && frames.count > 0;
      const last = (frames?.count ?? 1) - 1;
      const sec = Math.max(1, Math.round(frames?.fps ?? 1));
      switch (e.code) {
        case 'ArrowLeft': case 'KeyQ': if (slide) setIndex(i => Math.max(0, i - 1)); else handled = false; break;
        case 'ArrowRight': case 'KeyE': if (slide) setIndex(i => Math.min(last, i + 1)); else handled = false; break;
        case 'KeyA': if (slide) setIndex(i => Math.max(0, i - sec)); else handled = false; break;
        case 'KeyD': if (slide) setIndex(i => Math.min(last, i + sec)); else handled = false; break;
        case 'Home': if (slide) setIndex(0); else handled = false; break;
        case 'End': if (slide) setIndex(last); else handled = false; break;
        case 'KeyF': if (onSwitchRef.current) { if (!e.repeat) onSwitchRef.current(1, false); } else handled = false; break;
        case 'KeyR': if (onSwitchRef.current) { if (!e.repeat) onSwitchRef.current(-1, false); } else handled = false; break;
        default: handled = false;
      }
      if (handled) {
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [mode, layout, frames]);

  // 슬라이드 넘김이 끊기지 않게 앞뒤 몇 장을 미리 받아 둔다
  useEffect(() => {
    if (mode !== 'frames' || layout !== 'slide' || !frames) return;
    for (const d of [1, 2, 3, -1]) {
      const j = index + d;
      if (j >= 0 && j < frames.count) new Image().src = frameUrl(j);
    }
  }, [mode, layout, frames, index, frameUrl]);

  // 그리드 전환 시 현재 프레임이 있는 페이지로 이동 + 스크롤
  useEffect(() => {
    if (mode !== 'frames' || layout !== 'grid') return;
    setGridPage(Math.floor(index / GRID_PAGE) + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, layout, frames]);
  useEffect(() => {
    if (mode !== 'frames' || layout !== 'grid') return;
    const el = gridRef.current?.querySelector(`[data-idx="${index}"]`);
    if (el) (el as HTMLElement).scrollIntoView({ block: 'nearest' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, layout, gridPage, gridCols]);

  // 그리드 한 줄 개수 — 자동(0)이면 현재 폭 기준 칸 수에서 출발
  const currentCols = () => {
    if (gridCols > 0) return gridCols;
    const w = gridRef.current?.clientWidth ?? 600;
    return Math.max(1, Math.floor((w + 4) / (GRID_AUTO_MIN_PX + 4)));
  };
  const zoomIn = () => setGridCols(Math.max(1, currentCols() - 1));
  const zoomOut = () => setGridCols(Math.min(GRID_MAX_COLS, currentCols() + 1));

  const noFocus = (e: React.MouseEvent) => e.preventDefault();
  const count = frames?.count ?? 0;
  const aspect = frames && frames.width > 0 && frames.height > 0 ? `${frames.width} / ${frames.height}` : '16 / 9';

  const renderSlide = () => (
    <>
      <div style={{ position: 'relative', background: '#000', borderRadius: 4, overflow: 'hidden' }}>
        <img
          src={frameUrl(index)}
          alt={`frame ${index + 1}`}
          style={{ display: 'block', margin: '0 auto', maxWidth: '100%', maxHeight, objectFit: 'contain' }}
        />
        <div style={{
          position: 'absolute', top: 6, left: 6, background: 'rgba(0,0,0,0.65)', color: '#fff',
          padding: '1px 8px', borderRadius: 10, fontSize: 12, fontFamily: 'monospace', pointerEvents: 'none',
        }}>
          #{index + 1} / {count} · {frameTime(index).toFixed(3)}s
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 2, marginTop: 4 }}>
        <Tooltip title={t('capture.first')}>
          <Button size="small" type="text" icon={<VerticalRightOutlined />} onMouseDown={noFocus} onClick={() => go(0)} />
        </Tooltip>
        <Tooltip title={t('capture.prev')}>
          <Button size="small" type="text" icon={<LeftOutlined />} onMouseDown={noFocus} onClick={() => go(index - 1)} />
        </Tooltip>
        <Slider
          min={0}
          max={Math.max(0, count - 1)}
          value={index}
          onChange={(v) => go(v as number)}
          tooltip={{ formatter: (v) => `#${(v ?? 0) + 1} · ${frameTime(v ?? 0).toFixed(3)}s` }}
          style={{ flex: 1, margin: '0 8px' }}
        />
        <Tooltip title={t('capture.next')}>
          <Button size="small" type="text" icon={<RightOutlined />} onMouseDown={noFocus} onClick={() => go(index + 1)} />
        </Tooltip>
        <Tooltip title={t('capture.last')}>
          <Button size="small" type="text" icon={<VerticalLeftOutlined />} onMouseDown={noFocus} onClick={() => go(count - 1)} />
        </Tooltip>
      </div>
    </>
  );

  const renderGrid = () => {
    const from = (gridPage - 1) * GRID_PAGE;
    const to = Math.min(count, from + GRID_PAGE);
    const big = gridCols > 0 && gridCols <= 3;
    return (
      <>
        <div
          ref={gridRef}
          style={{
            display: 'grid',
            gridTemplateColumns: gridCols > 0
              ? `repeat(${gridCols}, minmax(0, 1fr))`
              : `repeat(auto-fill, minmax(${GRID_AUTO_MIN_PX}px, 1fr))`,
            gap: 4, maxHeight: maxHeight + 40, overflowY: 'auto', padding: 2,
          }}
        >
          {Array.from({ length: to - from }, (_, k) => from + k).map(i => (
            <div
              key={i}
              data-idx={i}
              onClick={() => { setIndex(i); setLayout('slide'); }}
              style={{
                cursor: 'pointer', borderRadius: 3, overflow: 'hidden', background: '#000',
                outline: i === index ? '2px solid #1677ff' : '1px solid rgba(128,128,128,0.35)',
              }}
            >
              <img
                src={frameUrl(i)}
                alt={`frame ${i + 1}`}
                loading="lazy"
                style={{ display: 'block', width: '100%', aspectRatio: aspect, objectFit: 'contain' }}
              />
              <div style={{
                fontSize: big ? 12 : 10, textAlign: 'center', color: '#fff', background: 'rgba(0,0,0,0.6)',
                fontFamily: 'monospace', lineHeight: big ? '18px' : '15px',
              }}>
                #{i + 1} · {frameTime(i).toFixed(2)}s
              </div>
            </div>
          ))}
        </div>
        {count > GRID_PAGE && (
          <div style={{ display: 'flex', justifyContent: 'center', marginTop: 4 }}>
            <Pagination
              size="small"
              simple
              current={gridPage}
              pageSize={GRID_PAGE}
              total={count}
              onChange={setGridPage}
            />
          </div>
        )}
      </>
    );
  };

  return (
    <div ref={rootRef}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4, flexWrap: 'wrap' }}>
        {mode === 'video' ? (
          <Button size="small" icon={<PictureOutlined />} onMouseDown={noFocus} onClick={openFrames}>
            {t('capture.viewFrames')}
          </Button>
        ) : (
          <>
            <Button size="small" icon={<VideoCameraOutlined />} onMouseDown={noFocus} onClick={backToVideo}>
              {t('capture.viewVideo')}
            </Button>
            <Segmented
              size="small"
              value={layout}
              onChange={(v) => setLayout(v as 'slide' | 'grid')}
              options={[
                { value: 'slide', label: t('capture.slide'), icon: <PictureOutlined /> },
                { value: 'grid', label: t('capture.grid'), icon: <AppstoreOutlined /> },
              ]}
            />
            {layout === 'grid' && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                <Tooltip title={t('capture.zoomOut')}>
                  <Button size="small" type="text" icon={<ZoomOutOutlined />} onMouseDown={noFocus} onClick={zoomOut}
                    disabled={gridCols >= GRID_MAX_COLS} />
                </Tooltip>
                <Tooltip title={t('capture.resetCols')}>
                  <span
                    onClick={() => setGridCols(0)}
                    style={{ fontSize: 11, minWidth: 58, textAlign: 'center', cursor: 'pointer', userSelect: 'none' }}
                  >
                    {gridCols > 0 ? t('capture.columns', { n: gridCols }) : t('capture.autoCols')}
                  </span>
                </Tooltip>
                <Tooltip title={t('capture.zoomIn')}>
                  <Button size="small" type="text" icon={<ZoomInOutlined />} onMouseDown={noFocus} onClick={zoomIn}
                    disabled={gridCols === 1} />
                </Tooltip>
              </span>
            )}
          </>
        )}
        <span style={{ flex: 1 }} />
        {frames && (
          <span style={{ fontSize: 11, color: '#888' }}>
            {t('capture.framesInfo', { count: frames.count, fps: Number(frames.fps.toFixed(2)) })}
          </span>
        )}
      </div>

      <div style={{ display: mode === 'video' ? 'block' : 'none' }}>
        <VideoTransport video={videoEl} onSwitchRecording={onSwitchRecording}>
          <video
            key={videoUrl}
            ref={setVideoEl}
            src={videoUrl}
            preload="auto"
            autoPlay={autoPlay}
            style={{ width: '100%', maxHeight, background: '#000', borderRadius: 4, display: 'block' }}
          />
        </VideoTransport>
      </div>

      {mode === 'frames' && (
        loading ? (
          <div style={{ textAlign: 'center', padding: 32 }}>
            <Spin tip={t('capture.extracting')}><div style={{ height: 40 }} /></Spin>
          </div>
        ) : error ? (
          <div style={{ color: '#ff4d4f', textAlign: 'center', padding: 20, fontSize: 12 }}>{error}</div>
        ) : frames && count > 0 ? (
          layout === 'slide' ? renderSlide() : renderGrid()
        ) : null
      )}
    </div>
  );
}
