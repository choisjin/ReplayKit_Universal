import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Select, Slider, Tooltip } from 'antd';
import {
  BackwardOutlined, CaretRightOutlined, DoubleLeftOutlined, DoubleRightOutlined, ForwardOutlined, FullscreenExitOutlined, FullscreenOutlined,
  MutedOutlined, PauseOutlined, SoundOutlined, StepBackwardOutlined, StepForwardOutlined,
} from '@ant-design/icons';
import { useTranslation } from '../i18n';

// 결과 상세 녹화 영상용 컨트롤러 + 단축키.
//
// 단축키 (e.code 기준 — 한글 입력 상태에서도 동작):
//   Space = 재생/일시정지, A/D = 이동 간격(기본 1초) 뒤로/앞으로, Q/E = 1프레임 이전/다음
//   S/W = 배속 한 단계 낮춤/높임 (x0 · x1 · x2 · x4 · x8 · x10). x0 은 일시정지, x0 에서 W 는 x1 로 재생.
//   A/D·Q/E 이동량은 배속만큼 곱해진다 (x4 → 4초/4프레임, x0 은 x1 로 취급). 방향은 키가 정한다.
//   F/R = 다음/이전 영상(회차) — onSwitchRecording 이 있을 때만. 영상 끝/0초에서 자동으로 넘어가지는 않는다.

const SPEEDS = [0, 1, 2, 4, 8, 10];
const JUMP_OPTIONS = [0.5, 1, 2, 5, 10];
const DEFAULT_FRAME_SEC = 1 / 30;

const formatTime = (sec: number): string => {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`;
};

const formatRate = (r: number) => `x${r}`;

// 모달/이미지 미리보기가 떠 있으면 단축키를 양보한다.
const overlayOpen = (): boolean =>
  Array.from(document.querySelectorAll('.ant-modal-wrap, .ant-image-preview-wrap'))
    .some(el => getComputedStyle(el).display !== 'none');

const isEditableTarget = (target: EventTarget | null): boolean => {
  const el = target as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
};

interface Props {
  /** 제어할 <video> 엘리먼트 (callback ref 로 받은 현재 엘리먼트) */
  video: HTMLVideoElement | null;
  /** <video> 엘리먼트 — 전체화면 시 컨트롤러와 함께 올리기 위해 래핑한다 */
  children: React.ReactNode;
  /** 단축키 활성화 여부 */
  hotkeys?: boolean;
  /**
   * F/R 단축키·버튼으로 다음(1)/이전(-1) 영상으로 전환. wasPlaying 이면 새 영상도 이어서 재생.
   * 전환했으면 true, 인접 영상이 없으면 false.
   */
  onSwitchRecording?: (dir: 1 | -1, wasPlaying: boolean) => boolean;
}

export default function VideoTransport({ video, children, hotkeys = true, onSwitchRecording }: Props) {
  const { t } = useTranslation();
  const wrapRef = useRef<HTMLDivElement>(null);
  const onSwitchRef = useRef(onSwitchRecording);
  onSwitchRef.current = onSwitchRecording;

  const [rate, setRateState] = useState(1);
  const rateRef = useRef(1);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [muted, setMuted] = useState(false);
  const [jumpSec, setJumpSec] = useState(1);
  const [isFullscreen, setIsFullscreen] = useState(false);
  // 단축키 입력 피드백 (영상 위 잠깐 표시)
  const [osd, setOsd] = useState<string | null>(null);
  const osdTimerRef = useRef<number | null>(null);

  // 프레임 길이 추정 (requestVideoFrameCallback 의 mediaTime 간격 중앙값). 웹캠 녹화는 VFR 이다.
  const frameSamplesRef = useRef<number[]>([]);
  const lastFrameRef = useRef<{ mediaTime: number; frames: number } | null>(null);

  const setRate = useCallback((r: number) => {
    rateRef.current = r;
    setRateState(r);
  }, []);

  const showOsd = useCallback((text: string) => {
    setOsd(text);
    if (osdTimerRef.current != null) window.clearTimeout(osdTimerRef.current);
    osdTimerRef.current = window.setTimeout(() => setOsd(null), 700);
  }, []);

  const syncPlaying = useCallback(() => {
    setPlaying(!!video && !video.paused && !video.ended);
  }, [video]);

  const frameSec = useCallback((): number => {
    const samples = frameSamplesRef.current;
    if (samples.length < 3) return DEFAULT_FRAME_SEC;
    const sorted = [...samples].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  }, []);

  const startPlayback = useCallback((v: HTMLVideoElement) => {
    if (v.ended) v.currentTime = 0;
    const p = v.play();
    if (p && typeof p.catch === 'function') p.catch(() => {});
  }, []);

  const play = useCallback(() => {
    const v = video;
    if (!v) return;
    // x0(일시정지 배속)에서 재생하면 x1 로 복귀
    if (rateRef.current === 0) setRate(1);
    v.playbackRate = rateRef.current;
    startPlayback(v);
  }, [video, setRate, startPlayback]);

  const pause = useCallback(() => {
    video?.pause();
  }, [video]);

  const togglePlay = useCallback(() => {
    if (video && !video.paused && !video.ended) pause(); else play();
  }, [video, play, pause]);

  const stepSpeed = useCallback((dir: 1 | -1) => {
    const v = video;
    const idx = SPEEDS.indexOf(rateRef.current);
    const base = idx < 0 ? SPEEDS.indexOf(1) : idx;
    const prev = rateRef.current;
    const next = SPEEDS[Math.min(SPEEDS.length - 1, Math.max(0, base + dir))];
    setRate(next);
    showOsd(formatRate(next));
    if (!v) return;
    if (next === 0) {
      v.pause();
    } else {
      v.playbackRate = next;
      if (prev === 0) startPlayback(v);  // x0 → x1: 일시정지 해제
    }
  }, [video, setRate, showOsd, startPlayback]);

  const seekTo = useCallback((sec: number) => {
    const v = video;
    if (!v) return;
    const dur = Number.isFinite(v.duration) ? v.duration : Infinity;
    v.currentTime = Math.min(Math.max(0, sec), dur);
    setTime(v.currentTime);
  }, [video]);

  // F/R — 다음/이전 영상. 현재 재생 상태를 넘겨 새 영상도 같은 상태로 시작하게 한다.
  const switchRecording = useCallback((dir: 1 | -1) => {
    const cb = onSwitchRef.current;
    if (!cb) return;
    const wasPlaying = !!video && !video.paused && !video.ended;
    if (!cb(dir, wasPlaying)) showOsd(t(dir > 0 ? 'webcam.noNextRec' : 'webcam.noPrevRec'));
  }, [video, showOsd, t]);

  const jump = useCallback((dir: 1 | -1) => {
    if (!video) return;
    const sec = jumpSec * Math.max(1, rateRef.current);
    const target = video.currentTime + dir * sec;
    showOsd(`${dir < 0 ? '◀' : '▶'} ${sec}s`);
    seekTo(target);
  }, [video, jumpSec, seekTo, showOsd]);

  const stepFrame = useCallback((dir: 1 | -1) => {
    const v = video;
    if (!v) return;
    v.pause();
    const fs = frameSec();
    const n = Math.max(1, rateRef.current);
    // 현재 표시 중인 프레임의 PTS 를 기준으로 삼아야 VFR 에서도 한 프레임씩 넘어간다.
    const last = lastFrameRef.current;
    const base = last && Math.abs(last.mediaTime - v.currentTime) < fs * 1.5 ? last.mediaTime : v.currentTime;
    const target = base + dir * fs * n + (dir > 0 ? 0.001 : 0);
    showOsd(dir < 0 ? `◀ ${n}f` : `${n}f ▶`);
    seekTo(target);
  }, [video, frameSec, seekTo, showOsd]);

  const toggleMute = useCallback(() => {
    if (!video) return;
    video.muted = !video.muted;
    setMuted(video.muted);
  }, [video]);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      wrapRef.current?.requestFullscreen().catch(() => {});
    }
  }, []);

  // 엘리먼트 바인딩: 이벤트 → 상태 동기화, 프레임 길이 추정
  useEffect(() => {
    const v = video;
    if (!v) return;
    // 새 엘리먼트(회차 전환)는 x0 을 이어받지 않는다
    if (rateRef.current === 0) setRate(1);
    v.playbackRate = rateRef.current;
    frameSamplesRef.current = [];
    lastFrameRef.current = null;
    setTime(v.currentTime || 0);
    setDuration(Number.isFinite(v.duration) ? v.duration : 0);
    setMuted(v.muted);
    syncPlaying();

    const onTime = () => setTime(v.currentTime);
    const onDuration = () => setDuration(Number.isFinite(v.duration) ? v.duration : 0);
    const onPlay = () => {
      // 외부(스텝 클릭 seek 등)에서 play() 한 경우 x0 이면 x1 로 전환
      if (rateRef.current === 0) {
        setRate(1);
        v.playbackRate = 1;
      }
      syncPlaying();
    };
    const onRateChange = () => {
      if (rateRef.current > 0 && v.playbackRate !== rateRef.current) setRate(v.playbackRate);
    };
    const onVolume = () => setMuted(v.muted);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('seeked', onTime);
    v.addEventListener('loadedmetadata', onDuration);
    v.addEventListener('durationchange', onDuration);
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', syncPlaying);
    v.addEventListener('ended', syncPlaying);
    v.addEventListener('ratechange', onRateChange);
    v.addEventListener('volumechange', onVolume);

    const anyV = v as any;
    let vfcHandle: number | null = null;
    if (typeof anyV.requestVideoFrameCallback === 'function') {
      const onFrame = (_now: number, meta: { mediaTime: number; presentedFrames: number }) => {
        const last = lastFrameRef.current;
        if (last && meta.presentedFrames > last.frames && meta.mediaTime > last.mediaTime) {
          const d = (meta.mediaTime - last.mediaTime) / (meta.presentedFrames - last.frames);
          // x1 연속 재생 구간의 인접 프레임만 표본으로 사용.
          // 고배속은 화면 주사율에 막혀 표시 프레임 사이에 원본 프레임 여러 개가 건너뛰어지고
          // (x10 이면 약 5프레임), 일시정지 중 표본은 seek 점프라 둘 다 제외한다.
          const clean = !v.paused && v.playbackRate === 1;
          if (clean && meta.presentedFrames - last.frames === 1 && d >= 1 / 240 && d <= 0.5) {
            const samples = frameSamplesRef.current;
            samples.push(d);
            if (samples.length > 60) samples.shift();
          }
        }
        lastFrameRef.current = { mediaTime: meta.mediaTime, frames: meta.presentedFrames };
        setTime(meta.mediaTime);
        vfcHandle = anyV.requestVideoFrameCallback(onFrame);
      };
      vfcHandle = anyV.requestVideoFrameCallback(onFrame);
    }

    return () => {
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('seeked', onTime);
      v.removeEventListener('loadedmetadata', onDuration);
      v.removeEventListener('durationchange', onDuration);
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', syncPlaying);
      v.removeEventListener('ended', syncPlaying);
      v.removeEventListener('ratechange', onRateChange);
      v.removeEventListener('volumechange', onVolume);
      if (vfcHandle != null && typeof anyV.cancelVideoFrameCallback === 'function') {
        anyV.cancelVideoFrameCallback(vfcHandle);
      }
      setPlaying(false);
    };
  }, [video, setRate, syncPlaying]);

  useEffect(() => {
    const onFs = () => setIsFullscreen(!!document.fullscreenElement && document.fullscreenElement === wrapRef.current);
    document.addEventListener('fullscreenchange', onFs);
    return () => document.removeEventListener('fullscreenchange', onFs);
  }, []);

  useEffect(() => () => {
    if (osdTimerRef.current != null) window.clearTimeout(osdTimerRef.current);
  }, []);

  // 단축키
  useEffect(() => {
    if (!hotkeys || !video) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
      if (isEditableTarget(e.target) || overlayOpen()) return;
      // 접힌 패널/숨겨진 페이지의 영상은 조작하지 않는다
      if (!document.fullscreenElement && video.getClientRects().length === 0) return;
      let handled = true;
      switch (e.code) {
        case 'Space': if (!e.repeat) togglePlay(); break;
        case 'KeyA': jump(-1); break;
        case 'KeyD': jump(1); break;
        case 'KeyS': if (!e.repeat) stepSpeed(-1); break;
        case 'KeyW': if (!e.repeat) stepSpeed(1); break;
        case 'KeyQ': stepFrame(-1); break;
        case 'KeyE': stepFrame(1); break;
        case 'KeyF':
          if (!onSwitchRef.current) { handled = false; break; }
          if (!e.repeat) switchRecording(1);
          break;
        case 'KeyR':
          if (!onSwitchRef.current) { handled = false; break; }
          if (!e.repeat) switchRecording(-1);
          break;
        default: handled = false;
      }
      if (handled) {
        // 포커스된 버튼이 Space 로 클릭되거나 페이지가 스크롤되는 것을 막는다
        e.preventDefault();
        e.stopPropagation();
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [hotkeys, video, togglePlay, jump, stepSpeed, stepFrame, switchRecording]);

  // 컨트롤 버튼이 포커스를 가져가지 않게 해서 Space 단축키와 충돌을 막는다
  const noFocus = (e: React.MouseEvent) => e.preventDefault();
  const disabled = !video;
  const btn = (title: string, icon: React.ReactNode, onClick: () => void, extra?: React.ReactNode) => (
    <Tooltip title={title} mouseEnterDelay={0.4}>
      <Button size="small" type="text" icon={icon} disabled={disabled} onMouseDown={noFocus} onClick={onClick}>
        {extra}
      </Button>
    </Tooltip>
  );

  // 배속만큼 곱해진 실제 이동량 (안내/툴팁 표시용, x0 은 x1 로 취급)
  const mult = Math.max(1, rate);
  const effJump = jumpSec * mult;
  const hotkeyRows: [string, string][] = [
    ['Space', t('webcam.hkPlayPause')],
    ['A', t('webcam.hkJumpBack', { sec: effJump })],
    ['D', t('webcam.hkJumpFwd', { sec: effJump })],
    ['S', t('webcam.hkSpeedDown')],
    ['W', t('webcam.hkSpeedUp')],
    ['Q', t('webcam.hkFramePrev', { n: mult })],
    ['E', t('webcam.hkFrameNext', { n: mult })],
    ...(onSwitchRecording
      ? ([['R', t('webcam.hkPrevRec')], ['F', t('webcam.hkNextRec')]] as [string, string][])
      : []),
  ];

  return (
    <div>
      <style>{`
        .vt-wrap:fullscreen { background: #000; display: flex; flex-direction: column; padding: 8px; }
        .vt-wrap:fullscreen .vt-stage { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; }
        .vt-wrap:fullscreen video { max-height: 100% !important; height: 100%; object-fit: contain; margin: 0 !important; }
        .vt-wrap:fullscreen .vt-bar { color: #fff; }
        .vt-wrap:fullscreen .vt-bar .ant-btn { color: #fff; }
        .vt-key { display: inline-block; min-width: 20px; padding: 0 5px; border: 1px solid rgba(128,128,128,0.45);
          border-bottom-width: 2px; border-radius: 4px; font: 600 10px/16px monospace; text-align: center; }
      `}</style>
      <div ref={wrapRef} className="vt-wrap">
        <div className="vt-stage" style={{ position: 'relative' }} onClick={() => video && togglePlay()}>
          {children}
          {osd && (
            <div style={{
              position: 'absolute', top: 8, left: '50%', transform: 'translateX(-50%)',
              background: 'rgba(0,0,0,0.65)', color: '#fff', padding: '2px 10px', borderRadius: 12,
              fontSize: 13, fontWeight: 600, pointerEvents: 'none',
            }}>{osd}</div>
          )}
        </div>
        <div className="vt-bar" style={{ padding: '2px 2px 4px' }}>
          <Slider
            min={0}
            max={duration || 0}
            step={0.01}
            value={Math.min(time, duration || 0)}
            disabled={disabled || !duration}
            onChange={(v) => seekTo(v as number)}
            tooltip={{ formatter: (v) => formatTime(v ?? 0) }}
            style={{ margin: '4px 6px 2px' }}
          />
          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 2 }}>
            {btn(`${t('webcam.hkFramePrev', { n: mult })} (Q)`, <StepBackwardOutlined />, () => stepFrame(-1))}
            {btn(`${t('webcam.hkJumpBack', { sec: effJump })} (A)`, <BackwardOutlined />, () => jump(-1))}
            {btn(`${t('webcam.hkPlayPause')} (Space)`, playing ? <PauseOutlined /> : <CaretRightOutlined />, togglePlay)}
            {btn(`${t('webcam.hkJumpFwd', { sec: effJump })} (D)`, <ForwardOutlined />, () => jump(1))}
            {btn(`${t('webcam.hkFrameNext', { n: mult })} (E)`, <StepForwardOutlined />, () => stepFrame(1))}
            {onSwitchRecording && btn(`${t('webcam.hkPrevRec')} (R)`, <DoubleLeftOutlined />, () => switchRecording(-1))}
            {onSwitchRecording && btn(`${t('webcam.hkNextRec')} (F)`, <DoubleRightOutlined />, () => switchRecording(1))}
            <span style={{ fontSize: 11, fontFamily: 'monospace', margin: '0 4px', whiteSpace: 'nowrap' }}>
              {formatTime(time)} / {formatTime(duration)}
            </span>
            <span style={{ flex: 1 }} />
            {/* − 배속 + 는 줄바꿈 시 함께 넘어가도록 한 묶음으로 */}
            <span style={{ display: 'inline-flex', alignItems: 'center', whiteSpace: 'nowrap' }}>
              {btn(`${t('webcam.hkSpeedDown')} (S)`, null, () => stepSpeed(-1), '−')}
              <Tooltip title={t('webcam.speed')}>
                <span style={{
                  fontSize: 11, fontWeight: 700, minWidth: 34, textAlign: 'center',
                  color: rate === 0 ? '#fa541c' : rate > 1 ? '#1677ff' : undefined,
                }}>{formatRate(rate)}</span>
              </Tooltip>
              {btn(`${t('webcam.hkSpeedUp')} (W)`, null, () => stepSpeed(1), '+')}
            </span>
            <Tooltip title={t('webcam.jumpStep')}>
              <Select
                size="small"
                variant="borderless"
                value={jumpSec}
                onChange={setJumpSec}
                popupMatchSelectWidth={false}
                options={JUMP_OPTIONS.map(s => ({ value: s, label: `${s}s` }))}
                style={{ width: 58 }}
              />
            </Tooltip>
            {btn(t('webcam.mute'), muted ? <MutedOutlined /> : <SoundOutlined />, toggleMute)}
            {btn(t('webcam.fullscreen'), isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />, toggleFullscreen)}
          </div>
        </div>
      </div>
      {hotkeys && (
        <div style={{
          marginTop: 4, padding: '4px 6px', borderRadius: 4, fontSize: 11,
          background: 'rgba(128,128,128,0.08)', border: '1px dashed rgba(128,128,128,0.3)',
        }}>
          <div style={{ fontWeight: 600, marginBottom: 2 }}>⌨ {t('webcam.hotkeys')}</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', gap: '2px 8px' }}>
            {hotkeyRows.map(([k, label]) => (
              <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', overflow: 'hidden' }}>
                <span className="vt-key">{k}</span>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }} title={label}>{label}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
