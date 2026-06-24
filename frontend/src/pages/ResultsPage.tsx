import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Card, Collapse, Col, Descriptions, Image, Input, InputNumber, Modal, Row, Select, Space, Spin, Table, Tag, Tooltip, message } from 'antd';
import { DeleteOutlined, DownloadOutlined, ExpandOutlined, EyeOutlined, FileTextOutlined, FolderOpenOutlined, PlayCircleOutlined, ReloadOutlined, ScissorOutlined, SearchOutlined, ShrinkOutlined, VideoCameraOutlined } from '@ant-design/icons';
import { resultsApi, scenarioApi } from '../services/api';
import { useSettings } from '../context/SettingsContext';
import { useTranslation } from '../i18n';
import type { TranslationKey } from '../i18n';

interface ResultSummary {
  filename: string;
  scenario_name: string;
  status: string;
  total_steps: number;
  total_repeat: number;
  passed_steps: number;
  failed_steps: number;
  warning_steps: number;
  error_steps: number;
  started_at: string;
  finished_at: string;
}

// 같은 타임스탬프를 공유하는 결과 묶음 (그룹 재생 또는 반복 실행)
interface ResultGroup {
  key: string; // 타임스탬프
  timestamp: string;
  items: ResultSummary[];
  status: string; // 전체 상태 (하나라도 fail이면 fail)
  scenario_names: string;
  total_repeat: number;
}

interface RecordingItem {
  filename: string;
  size: number;
  url: string;
  started_at?: string | null;
}

interface MatchLocation {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface SubResultDetail {
  label: string;
  expected_image: string;
  score: number;
  status: string;
  match_location: MatchLocation | null;
}

interface StepResultDetail {
  step_id: number;
  repeat_index: number;
  timestamp: string | null;
  device_id: string;
  command: string;
  description: string;
  status: string;
  similarity_score: number | null;
  expected_image: string | null;
  expected_annotated_image: string | null;
  actual_image: string | null;
  actual_annotated_image: string | null;
  diff_image: string | null;
  roi: { x: number; y: number; width: number; height: number } | null;
  match_location: MatchLocation | null;
  message: string;
  delay_ms: number;
  execution_time_ms: number;
  compare_mode: string | null;
  sub_results: SubResultDetail[];
  parent_step_id?: number | null;  // sync 모드 fail_on_keyword가 trigger한 인라인 fail의 parent
  fail_index?: number | null;       // 같은 parent 내 1-based 순번 (Fail_Count_N)
  excluded_from_result?: boolean;   // 조건부이동 결과 미반영 → Status를 '분기'로 표시
}

interface ResultDetail {
  scenario_name: string;
  device_serial: string;
  status: string;
  total_steps: number;
  total_repeat: number;
  passed_steps: number;
  failed_steps: number;
  warning_steps: number;
  error_steps: number;
  step_results: StepResultDetail[];
  started_at: string;
  finished_at: string;
  stopped_at_iteration?: number | null;
  stopped_at_step?: number | null;
}

const statusColor = (s: string) =>
  s === 'pass' ? 'green'
    : s === 'warning' ? 'orange'
    : s === 'error' ? 'volcano'
    : s === 'stopped' ? 'default'
    : s === 'branch' ? 'purple'
    : 'red';

// 'branch'(조건부이동 결과 미반영)는 '분기'로, 그 외는 대문자 그대로 표기
const statusText = (s: string, t: (k: TranslationKey) => string) =>
  s === 'branch' ? t('results.statusBranch') : s.toUpperCase();

// 결과 미반영 스텝은 status(실제 pass/fail)와 무관하게 '분기'로 표시
const effStatus = (r: { status: string; excluded_from_result?: boolean }) =>
  r.excluded_from_result ? 'branch' : r.status;

// 상세 보기용 — 분기 스텝은 어느 조건(Pass/Fail)으로 분기됐는지까지 표기
const statusDetail = (r: { status: string; excluded_from_result?: boolean }, t: (k: TranslationKey) => string) =>
  r.excluded_from_result ? `${t('results.statusBranch')} (${r.status === 'pass' ? 'PASS' : 'FAIL'})` : statusText(r.status, t);

const imageUrl = (path: string | null) => {
  if (!path) return null;
  let rel = path.replace(/\\/g, '/');
  const resultsIdx = rel.indexOf('/results/');
  if (resultsIdx >= 0) return '/results-files/' + rel.substring(resultsIdx + '/results/'.length);
  if (/^\d{8}_\d{6}_/.test(rel)) return '/results-files/' + rel;
  const idx = rel.indexOf('/screenshots/');
  if (idx >= 0) {
    rel = rel.substring(idx + '/screenshots/'.length);
  }
  return '/screenshots/' + rel;
};

// Draws match-location boxes on the expected image for multi_crop results
const AnnotatedOverlay = React.memo(({ subResults, expectedImage }: {
  subResults: SubResultDetail[];
  expectedImage: string;
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);

  useEffect(() => {
    const img = new window.Image();
    img.onload = () => {
      setSize({ w: img.width, h: img.height });
      const canvas = canvasRef.current;
      if (!canvas) return;
      // canvas overlays on the image; match its display size via parent
      canvas.width = img.width;
      canvas.height = img.height;
      const ctx = canvas.getContext('2d')!;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      subResults.forEach((sr, i) => {
        const loc = sr.match_location;
        if (!loc) return;
        const color = sr.status === 'pass' ? '#52c41a' : sr.status === 'warning' ? '#faad14' : '#ff4d4f';
        ctx.strokeStyle = color;
        ctx.lineWidth = 3;
        ctx.strokeRect(loc.x, loc.y, loc.width, loc.height);
        ctx.fillStyle = color.replace(')', ',0.15)').replace('rgb', 'rgba').replace('#', '');
        // Use simpler fill
        ctx.globalAlpha = 0.15;
        ctx.fillStyle = color;
        ctx.fillRect(loc.x, loc.y, loc.width, loc.height);
        ctx.globalAlpha = 1;
        // Label
        ctx.fillStyle = color;
        ctx.font = 'bold 24px sans-serif';
        ctx.fillText(`${sr.label || `#${i + 1}`} ${(sr.score * 100).toFixed(0)}%`, loc.x + 4, loc.y + 28);
      });
    };
    img.src = expectedImage + `?t=${Date.now()}`;
  }, [subResults, expectedImage]);

  if (!size) return null;
  return (
    <canvas
      ref={canvasRef}
      style={{
        position: 'absolute', top: 0, left: 0, width: '100%', height: '100%',
        pointerEvents: 'none',
      }}
    />
  );
});

// 내보내기 버튼 — 진행 중에는 비활성화 + 좌→우 색 채우기로 진행률(%) 표시
const ExportProgressButton: React.FC<{
  progress?: { percent: number; phase: string };
  onClick: () => void;
  size?: 'small' | 'middle' | 'large';
  children?: React.ReactNode;
}> = ({ progress, onClick, size, children }) => {
  const busy = !!progress;
  const pct = Math.max(0, Math.min(100, progress?.percent ?? 0));
  return (
    <Button
      size={size}
      icon={<DownloadOutlined />}
      disabled={busy}
      onClick={onClick}
      title={busy ? `${progress?.phase ?? ''} (${pct}%)` : undefined}
      style={busy ? { position: 'relative', overflow: 'hidden' } : undefined}
    >
      {busy && (
        <span
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            bottom: 0,
            width: `${pct}%`,
            background: 'rgba(24,144,255,0.30)',
            transition: 'width 0.3s ease',
            pointerEvents: 'none',
            zIndex: 0,
          }}
        />
      )}
      <span style={{ position: 'relative', zIndex: 1 }}>
        {busy ? `${pct}%` : children}
      </span>
    </Button>
  );
};

export default function ResultsPage() {
  const { settings } = useSettings();
  const { t, lang } = useTranslation();
  // 내보내기 진행 상태: filename → { percent, phase } (진행 중인 항목만 존재)
  const [exportProgress, setExportProgress] = useState<Record<string, { percent: number; phase: string }>>({});
  // HTML 생성(재생성) 진행 중 여부
  const [htmlGenLoading, setHtmlGenLoading] = useState(false);
  const [results, setResults] = useState<ResultSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<ResultDetail | null>(null);
  const [detailFilename, setDetailFilename] = useState('');
  const [detailVisible, setDetailVisible] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [compareStep, setCompareStep] = useState<StepResultDetail | null>(null);

  // 가상 스크롤 테이블 높이 (모달은 top:20 으로 윈도우에 고정 → innerHeight 기반 계산).
  // 수천 스텝도 보이는 행만 렌더하도록 virtual Table 에 numeric scroll.y 필요.
  const [detailTableY, setDetailTableY] = useState(() =>
    typeof window !== 'undefined' ? Math.max(240, window.innerHeight - 320) : 480);
  useEffect(() => {
    const onResize = () => setDetailTableY(Math.max(240, window.innerHeight - 320));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // 그룹 상세 뷰 (사이클별 통합)
  const [groupDetail, setGroupDetail] = useState<ResultDetail[] | null>(null);
  const [groupDetailCycle, setGroupDetailCycle] = useState(1);

  // 백그라운드 CMD/SSH 폴링 (task_id도 함께 추적해서 취소 가능)
  const bgPollTimers = useRef<ReturnType<typeof setInterval>[]>([]);
  const bgPollTaskIds = useRef<string[]>([]);
  const stopAllResultBgPolls = (cancelBackend: boolean = true) => {
    bgPollTimers.current.forEach(t => clearInterval(t));
    bgPollTimers.current = [];
    if (cancelBackend) {
      bgPollTaskIds.current.forEach(tid => {
        scenarioApi.cancelCmdTask(tid).catch(() => {});
      });
    }
    bgPollTaskIds.current = [];
  };

  // 선택 삭제 + 필터
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [scenarioFilter, setScenarioFilter] = useState('');

  // Webcam recordings
  const [recordings, setRecordings] = useState<RecordingItem[]>([]);
  const [webcamPanelOpen, setWebcamPanelOpen] = useState(false);
  const [webcamExpanded, setWebcamExpanded] = useState(false);
  const [activeRecUrl, setActiveRecUrl] = useState('');
  // Blob URL을 video src로 사용한다.
  // Why: 서버(StaticFiles)가 HTTP Range 응답 헤더를 안 주는 케이스에서
  // 브라우저가 video.seekable을 [0,0]으로 두고 seek를 0으로 snap시키는 문제가 있어,
  // fetch → Blob → ObjectURL로 메모리 리소스화하면 seekable이 항상 [0, duration]이 된다.
  const [activeRecBlobUrl, setActiveRecBlobUrl] = useState('');
  const blobUrlMapRef = useRef<Map<string, string>>(new Map());
  const [activeRecRepeat, setActiveRecRepeat] = useState(1);
  const detailVideoRef = useRef<HTMLVideoElement>(null);
  // 보류 중인 seek 요청. seekToStep이 항상 여기에 기록하고,
  // (1) 비디오 onCanPlay/onLoadedMetadata 핸들러, (2) useEffect 후속 처리에서 적용.
  // URL 전환·패널 마운트·readyState 지연 등의 race를 모두 흡수한다.
  // recUrl: 이 seek가 어떤 녹화에 속하는지 — 다른 비디오에 잘못 적용되는 것을 방지.
  const pendingSeekRef = useRef<{ offset: number; recUrl: string; applied: boolean } | null>(null);
  // 새 seek 요청이 발생할 때마다 증가시켜 useEffect를 트리거한다.
  const [pendingSeekTick, setPendingSeekTick] = useState(0);
  const [currentPlayingStepId, setCurrentPlayingStepId] = useState<number | null>(null);
  const [trimFile, setTrimFile] = useState<string | null>(null);
  const [trimStart, setTrimStart] = useState(0);
  const [trimEnd, setTrimEnd] = useState(0);

  // 녹화 파일을 회차 번호 기준 숫자 정렬 (문자열 정렬이면 r1, r10, r11, r2... 로 뒤죽박죽).
  const cycleIndexOf = (filename: string): number => {
    const m = filename.match(/(?:webcam|composite)_r(\d+)\.(?:webm|mp4)$/);
    return m ? parseInt(m[1], 10) : Number.MAX_SAFE_INTEGER;
  };
  const sortRecordingsByCycle = (recs: RecordingItem[]): RecordingItem[] =>
    [...recs].sort((a, b) => cycleIndexOf(a.filename) - cycleIndexOf(b.filename));

  const fetchRecordings = async (resultFilename: string) => {
    try {
      const res = await resultsApi.listRecordings(resultFilename);
      const recs = sortRecordingsByCycle(res.data.recordings || []);
      setRecordings(recs);
      if (recs.length > 0) {
        setActiveRecUrl(recs[0].url);
        const ci = cycleIndexOf(recs[0].filename);
        setActiveRecRepeat(ci === Number.MAX_SAFE_INTEGER ? 1 : ci);
      } else {
        setActiveRecUrl('');
      }
    } catch { setRecordings([]); }
  };

  // 그룹/단일 공통: 현재 사이클의 전체 스텝 목록 반환
  const getAllStepsForRepeat = useCallback((repeatIdx: number): StepResultDetail[] => {
    if (groupDetail && groupDetail.length > 0) {
      const allSteps: StepResultDetail[] = [];
      for (const d of groupDetail) {
        allSteps.push(...d.step_results.filter(s => (s.repeat_index || 1) === repeatIdx));
      }
      return allSteps;
    }
    if (detail) {
      return detail.step_results.filter(s => (s.repeat_index || 1) === repeatIdx);
    }
    return [];
  }, [detail, groupDetail]);

  // 비디오 내 step의 정확한 오프셋(초) 계산.
  // 1순위: 녹화 시작 wall-clock(rec.started_at)이 있으면 step.timestamp - rec.started_at.
  //       녹화는 첫 스텝보다 먼저 시작되고 마지막 스텝보다 늦게 끝나므로 이 방식이 가장 정확.
  // 2순위(레거시 폴백): 첫 스텝 timestamp를 비디오 0초로 가정 (구 결과와의 호환).
  const computeSeekOffsetSec = useCallback((
    step: StepResultDetail,
    rec: { started_at?: string | null } | undefined,
    repeatIdx: number,
  ): number | null => {
    if (!step.timestamp) return null;
    const stepTime = new Date(step.timestamp).getTime();
    if (!Number.isFinite(stepTime)) return null;

    // 1순위: 사이드카가 제공한 녹화 시작 시각
    if (rec?.started_at) {
      const recStart = new Date(rec.started_at).getTime();
      if (Number.isFinite(recStart)) {
        return Math.max(0, (stepTime - recStart) / 1000);
      }
    }

    // 2순위: 첫 스텝 timestamp 기반 폴백 (구 데이터 호환). 스케일링은 부정확하므로 사용하지 않음.
    const sameRepeatSteps = getAllStepsForRepeat(repeatIdx);
    const firstStep = sameRepeatSteps[0];
    if (!firstStep?.timestamp) return null;
    const firstTime = new Date(firstStep.timestamp).getTime();
    if (!Number.isFinite(firstTime)) return null;
    return Math.max(0, (stepTime - firstTime) / 1000);
  }, [getAllStepsForRepeat]);

  // seek 실제 적용 — 비디오와 보류 중인 seek가 같은 녹화에 속하고 readyState가 충분할 때만.
  // video가 아직 mount 안 됐거나 metadata 미로드면 짧게 폴링하여 재시도. 이로써 패널 첫 오픈 시
  // (1) onLoadedMetadata 이벤트가 React listener 부착 전에 이미 발화한 race
  // (2) ref 할당 타이밍 차이 모두 흡수.
  const seekRetryTimerRef = useRef<number | null>(null);
  const tryApplyPendingSeek = useCallback(() => {
    if (seekRetryTimerRef.current != null) {
      window.clearTimeout(seekRetryTimerRef.current);
      seekRetryTimerRef.current = null;
    }
    const pending = pendingSeekRef.current;
    if (!pending || pending.applied) return;
    const video = detailVideoRef.current;

    const scheduleRetry = () => {
      // 최대 약 10초까지 retry. 시나리오 종료 직후 녹화 파일이 OS 파일시스템 캐시에서
      // 완전히 보이기 전 시점에 첫 로드가 시작될 수 있어 충분한 여유 필요.
      const attempts = (pending as any)._attempts || 0;
      if (attempts >= 100) {
        // 10초 동안 readyState가 안 올라온 케이스 — 디코드 실패가 onError 전에 갇혔거나,
        // 코덱 미지원으로 브라우저가 조용히 포기한 상황. video.error 가 있으면 그 사유를,
        // 없으면 일반 메시지를 사용자에게 노출.
        const v = detailVideoRef.current;
        const code = v?.error?.code;
        const codeLabel = code === 1 ? 'ABORTED' : code === 2 ? 'NETWORK'
          : code === 3 ? 'DECODE' : code === 4 ? 'SRC_NOT_SUPPORTED'
          : code != null ? `unknown(${code})` : 'timeout';
        console.log('[seek-debug] EXHAUSTED — retry limit reached', {
          readyState: v?.readyState, networkState: v?.networkState, errorCode: code,
        });
        message.error(`녹화 영상을 로드하지 못했습니다 (${codeLabel}). 파일이 손상되었거나 브라우저가 지원하지 않는 코덱일 수 있습니다 — 재녹화 후 다시 시도하세요.`);
        pendingSeekRef.current = null;
        return;  // 100 * 100ms = 10s
      }
      (pending as any)._attempts = attempts + 1;
      seekRetryTimerRef.current = window.setTimeout(() => {
        seekRetryTimerRef.current = null;
        tryApplyPendingSeek();
      }, 100);
    };

    if (!video) {
      console.log('[seek-debug] WAIT no-video');
      scheduleRetry();
      return;
    }
    // 비디오 src가 아직 보류 중인 녹화로 전환되지 않았으면 대기.
    // src는 보통 blob:URL이므로 blobUrlMap에서 expected blob을 찾아 매칭한다.
    const currentSrc = video.currentSrc || video.src || '';
    const expectedBlob = pending.recUrl ? blobUrlMapRef.current.get(pending.recUrl) : undefined;
    const srcReady = !pending.recUrl
      || (expectedBlob ? currentSrc === expectedBlob : currentSrc.indexOf(pending.recUrl) !== -1);
    // 브라우저가 이미 로딩 중(NETWORK_LOADING=2)이면 절대 건드리지 않는다.
    // load()를 호출하면 진행 중인 metadata 로딩이 reset돼서 오히려 더 느려짐.
    // IDLE/EMPTY/NO_SOURCE 상태에서 readyState < 1이면(= 브라우저가 loading을 멈춘 상태)
    // 첫 1회만 video.load()를 호출해 강제 trigger.
    if (
      srcReady &&
      video.readyState < 1 &&
      video.networkState !== 2 /* NETWORK_LOADING */ &&
      !(pending as any)._loadCalled
    ) {
      (pending as any)._loadCalled = true;
      try { video.load(); } catch { /* ignore */ }
    }
    if (!srcReady) {
      console.log('[seek-debug] WAIT src-not-ready', { currentSrc, want: pending.recUrl });
      scheduleRetry();
      return;
    }
    if (video.readyState < 1) {
      // 처음 10회만 자세히 로깅 — 그 후에는 1초당 1회만 출력하여 콘솔 잠식 방지.
      // networkState 의미: 0=EMPTY 1=IDLE 2=LOADING 3=NO_SOURCE. 3 이 보이면 브라우저가 디코드 불가로 판단한 상태.
      const attempts = (pending as any)._attempts || 0;
      if (attempts < 10 || attempts % 10 === 0) {
        const errCode = video.error?.code;
        console.log('[seek-debug] WAIT readyState<1',
          'rs=' + video.readyState,
          'ns=' + video.networkState,
          'err=' + (errCode != null ? errCode : 'none'),
          'src=' + (video.currentSrc ? video.currentSrc.slice(0, 60) : '(empty)'));
      }
      scheduleRetry();
      return;
    }

    const videoDuration = video.duration;
    const hasDuration = Number.isFinite(videoDuration) && videoDuration > 0;
    const seekTime = hasDuration
      ? Math.min(pending.offset, Math.max(0, videoDuration - 0.05))
      : pending.offset;

    // 브라우저가 seekable 범위를 아직 확보하지 못했다면 set해도 0으로 snap된다.
    // preload="metadata"만 끝난 시점엔 seekable이 비거나 [0,0]에 머무르는 케이스가 있으므로,
    // target time이 seekable 범위 내인지 확인하고 아니면 polling 재시도.
    let seekableCovers = false;
    const seekableRangesStr: string[] = [];
    const bufferedRangesStr: string[] = [];
    try {
      for (let i = 0; i < video.seekable.length; i++) {
        const s = video.seekable.start(i);
        const e = video.seekable.end(i);
        seekableRangesStr.push(`[${s.toFixed(3)},${e.toFixed(3)}]`);
        if (seekTime >= s - 0.01 && seekTime <= e + 0.01) {
          seekableCovers = true;
        }
      }
      for (let i = 0; i < video.buffered.length; i++) {
        bufferedRangesStr.push(`[${video.buffered.start(i).toFixed(3)},${video.buffered.end(i).toFixed(3)}]`);
      }
    } catch { seekableCovers = true; /* seekable 접근 실패 시 일단 진행 */ }
    if (!seekableCovers) {
      const attempts = (pending as any)._attempts || 0;
      // 너무 많이 찍히지 않도록 1초마다(10회마다) 한 번씩만 출력
      if (attempts % 10 === 0) {
        console.log('[seek-debug] WAIT seekable-not-covered', {
          seekTime, readyState: video.readyState, networkState: video.networkState,
          seekable: seekableRangesStr.join(','),
          buffered: bufferedRangesStr.join(','),
          duration: video.duration,
          attempts,
        });
      }
      scheduleRetry();
      return;
    }

    const beforeCT = video.currentTime;
    if (Number.isFinite(seekTime) && seekTime >= 0) {
      try { video.currentTime = seekTime; } catch { /* ignore */ }
    }
    // [DEBUG] seek 적용 직후 상태 — 임시 진단용. 원인 파악 후 제거.
    console.log('[seek-debug] APPLY', {
      offset: pending.offset, seekTime, before: beforeCT, after: video.currentTime,
      duration: video.duration, readyState: video.readyState, networkState: video.networkState,
    });
    pending.applied = true;
    pendingSeekRef.current = null;
  }, []);

  const seekToStep = (step: StepResultDetail) => {
    if ((!detail && !groupDetail) || recordings.length === 0) {
      message.info('녹화 영상이 없습니다');
      return;
    }
    if (!webcamPanelOpen) setWebcamPanelOpen(true);

    const targetRepeat = step.repeat_index || 1;
    let rec = recordings.find(r => cycleIndexOf(r.filename) === targetRepeat);
    if (!rec) {
      const fallback = recordings[0];
      if (!fallback) { message.info(`Cycle ${targetRepeat} 녹화가 없습니다`); return; }
      message.info(`Cycle ${targetRepeat} 녹화가 없어 ${fallback.filename}을(를) 사용합니다`);
      rec = fallback;
    }

    const offsetSec = computeSeekOffsetSec(step, rec, targetRepeat);
    // [DEBUG] step 클릭 시 계산된 offset과 컨텍스트 — 임시 진단용. 원인 파악 후 제거.
    console.log('[seek-debug] CLICK', {
      step_id: step.step_id,
      step_ts: step.timestamp,
      rec_started_at: rec.started_at,
      rec_url: rec.url,
      offsetSec,
      panelOpen: webcamPanelOpen,
      activeRecUrl,
    });
    if (offsetSec == null) return;

    // 항상 pendingSeekRef에 기록. URL 변경/패널 마운트/readyState 지연 등의 race를
    // useEffect와 비디오 이벤트 핸들러가 모두 흡수한다.
    pendingSeekRef.current = { offset: offsetSec, recUrl: rec.url, applied: false };
    setActiveRecUrl(rec.url);
    setActiveRecRepeat(targetRepeat);
    // pendingSeekTick 갱신 → useEffect에서 React 커밋 후 시도.
    setPendingSeekTick(t => t + 1);

    // 같은 URL이고 비디오가 이미 준비되었다면 즉시 시도(저지연). 실패해도 effect/onCanPlay가 재시도.
    if (rec.url === activeRecUrl) {
      tryApplyPendingSeek();
    }
  };

  // activeRecUrl이 바뀔 때마다 해당 파일을 fetch해서 Blob URL로 변환.
  // 이렇게 해야 video.seekable이 정상적으로 [0,duration] 범위를 가진다 (Range 응답 무관).
  useEffect(() => {
    if (!activeRecUrl) {
      setActiveRecBlobUrl('');
      return;
    }
    const cached = blobUrlMapRef.current.get(activeRecUrl);
    if (cached) {
      setActiveRecBlobUrl(cached);
      return;
    }
    let cancelled = false;
    fetch(activeRecUrl)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.blob().then(b => ({ blob: b, contentType: r.headers.get('content-type') || '' }));
      })
      .then(({ blob, contentType }) => {
        if (cancelled) return;
        // 빈 파일/0바이트 응답은 디코드 불가 — 명시적으로 에러 처리해서 readyState<1 무한 폴링 방지.
        if (blob.size === 0) {
          console.error('[video] blob empty — recording file likely missing or zero bytes', { url: activeRecUrl });
          message.error('녹화 파일이 비어 있습니다 (0 bytes)');
          setActiveRecBlobUrl('');
          return;
        }
        console.log('[video] blob loaded', { url: activeRecUrl, size: blob.size, contentType });
        const blobUrl = URL.createObjectURL(blob);
        blobUrlMapRef.current.set(activeRecUrl, blobUrl);
        setActiveRecBlobUrl(blobUrl);
      })
      .catch(err => {
        if (cancelled) return;
        console.warn('[video] blob fetch failed, falling back to direct URL', err);
        // 실패 시 직접 URL 사용 (seek 안 될 수 있지만 재생은 됨)
        setActiveRecBlobUrl(activeRecUrl);
      });
    return () => { cancelled = true; };
  }, [activeRecUrl]);

  // 컴포넌트 unmount 시 blob URL 해제 (메모리 leak 방지).
  useEffect(() => {
    const map = blobUrlMapRef.current;
    return () => {
      map.forEach(url => { try { URL.revokeObjectURL(url); } catch { /* ignore */ } });
      map.clear();
    };
  }, []);

  // React 커밋 후 보류 중인 seek 적용 시도. 패널 마운트/URL 변경/리렌더 모두 커버.
  useEffect(() => {
    tryApplyPendingSeek();
  }, [pendingSeekTick, activeRecBlobUrl, webcamPanelOpen, tryApplyPendingSeek]);

  // <video onCanPlay> / <video onLoadedMetadata> 콜백 — URL 변경 후 비디오 로드 완료 시 pending seek 적용.
  // pendingSeekRef.offset은 seekToStep에서 이미 "비디오 내 절대 시간(초)"으로 계산되어 있다.
  const handleVideoCanPlay = useCallback(() => {
    tryApplyPendingSeek();
  }, [tryApplyPendingSeek]);

  // <video onError> — 디코드 실패(예: 코덱 미지원, 파일 손상) 시 호출.
  // pendingSeekRef를 비우고 재시도 타이머도 취소하여 [seek-debug] WAIT readyState<1 무한 폴링을 막는다.
  // 사용자에겐 메시지로 원인 노출 (mp4v fourcc → libx264 미적용 또는 파일 손상 의심).
  const handleVideoError = useCallback((e: React.SyntheticEvent<HTMLVideoElement>) => {
    const v = e.currentTarget;
    const code = v.error?.code;
    const msg = v.error?.message || '';
    // MediaError codes: 1=ABORTED, 2=NETWORK, 3=DECODE, 4=SRC_NOT_SUPPORTED
    const codeLabel = code === 1 ? 'ABORTED' : code === 2 ? 'NETWORK' : code === 3 ? 'DECODE' : code === 4 ? 'SRC_NOT_SUPPORTED' : `unknown(${code})`;
    console.error('[video] decode/load error', { code, codeLabel, message: msg, src: v.currentSrc || v.src });
    if (code === 3 || code === 4) {
      message.error(`녹화 파일을 재생할 수 없습니다 (${codeLabel}) — ffmpeg 인코딩 없이 저장된 mp4v 파일이거나 손상된 파일일 수 있습니다.`);
    } else {
      message.error(`녹화 파일 로드 실패 (${codeLabel})`);
    }
    // 진행 중인 seek 시도 중단
    pendingSeekRef.current = null;
    if (seekRetryTimerRef.current != null) {
      window.clearTimeout(seekRetryTimerRef.current);
      seekRetryTimerRef.current = null;
    }
  }, []);

  // 비디오 재생 시 현재 스텝 실시간 하이라이트.
  // started_at 사이드카가 있으면 video time → wall-clock으로 직접 변환하여 정확히 매칭.
  // 없으면 첫 스텝 timestamp를 비디오 0초로 가정하는 레거시 휴리스틱으로 폴백.
  const handleVideoTimeUpdate = useCallback(() => {
    const video = detailVideoRef.current;
    if (!video || (!detail && !groupDetail)) return;
    const currentTime = video.currentTime;
    const sameRepeatSteps = getAllStepsForRepeat(activeRecRepeat);
    if (sameRepeatSteps.length === 0) return;

    // 활성 녹화 메타에서 started_at 조회 (1순위)
    const activeRec = recordings.find(r => r.url === activeRecUrl);
    const recStartIso = activeRec?.started_at || null;
    const recStartMs = recStartIso ? new Date(recStartIso).getTime() : NaN;
    const hasRecStart = Number.isFinite(recStartMs);

    // 폴백 기준점: 첫 스텝 timestamp (구 데이터 호환)
    const firstStep = sameRepeatSteps[0];
    if (!firstStep?.timestamp) return;
    const firstTime = new Date(firstStep.timestamp).getTime();

    // 현재 video 시간을 wall-clock(ms) 또는 첫 스텝 기준 오프셋(sec)으로 변환
    let matchedStep: StepResultDetail | null = null;
    for (let i = sameRepeatSteps.length - 1; i >= 0; i--) {
      const s = sameRepeatSteps[i];
      if (!s.timestamp) continue;
      if (hasRecStart) {
        const stepOffset = (new Date(s.timestamp).getTime() - recStartMs) / 1000;
        if (currentTime >= stepOffset - 0.5) {
          matchedStep = s;
          break;
        }
      } else {
        const stepOffset = (new Date(s.timestamp).getTime() - firstTime) / 1000;
        if (currentTime >= stepOffset - 0.5) {
          matchedStep = s;
          break;
        }
      }
    }
    setCurrentPlayingStepId(matchedStep?.step_id ?? null);
  }, [detail, groupDetail, activeRecRepeat, getAllStepsForRepeat, recordings, activeRecUrl]);

  const handleVideoPauseOrEnd = useCallback(() => {
    setCurrentPlayingStepId(null);
  }, []);

  const fetchResults = async () => {
    setLoading(true);
    try {
      const res = await resultsApi.list();
      setResults(res.data.results);
    } catch {
      message.error(t('results.listFailed'));
    }
    setLoading(false);
  };

  const viewGroupDetail = async (group: ResultGroup) => {
    setDetailLoading(true);
    setDetailVisible(true);
    setDetail(null);
    setGroupDetail(null);
    setGroupDetailCycle(1);
    setDetailFilename(group.items[0].filename);
    try {
      const details: ResultDetail[] = [];
      for (const item of group.items) {
        const res = await resultsApi.get(item.filename);
        details.push(res.data);
      }
      setGroupDetail(details);
      // 모든 시나리오의 녹화 파일을 합쳐서 로드
      const allRecs: any[] = [];
      for (const item of group.items) {
        try {
          const recRes = await resultsApi.listRecordings(item.filename);
          allRecs.push(...(recRes.data.recordings || []));
        } catch { /* ignore */ }
      }
      // 중복 제거 (같은 파일명) + 회차 숫자 정렬
      const seen = new Set<string>();
      const uniqueRecs = sortRecordingsByCycle(
        allRecs.filter(r => { if (seen.has(r.filename)) return false; seen.add(r.filename); return true; })
      );
      setRecordings(uniqueRecs);
      if (uniqueRecs.length > 0) {
        setActiveRecUrl(uniqueRecs[0].url);
        const ci = cycleIndexOf(uniqueRecs[0].filename);
        setActiveRecRepeat(ci === Number.MAX_SAFE_INTEGER ? 1 : ci);
      } else {
        setActiveRecUrl('');
      }
    } catch {
      message.error(t('results.detailFailed'));
    }
    setDetailLoading(false);
  };

  const viewDetail = async (filename: string) => {
    setDetailLoading(true);
    setDetailFilename(filename);
    setDetailVisible(true);
    setDetail(null);
    try {
      const res = await resultsApi.get(filename);
      setDetail(res.data);
      fetchRecordings(filename);
    } catch {
      message.error(t('results.detailFailed'));
    }
    setDetailLoading(false);
  };

  const deleteResult = (filename: string) => {
    Modal.confirm({
      title: t('results.deleteTitle'),
      content: t('results.deleteConfirm', { name: filename }),
      okText: t('common.delete'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await resultsApi.delete(filename);
          message.success(t('common.deleteComplete'));
          fetchResults();
          if (detailFilename === filename) {
            setDetailVisible(false);
            setDetail(null);
          }
        } catch {
          message.error(t('common.deleteFailed'));
        }
      },
    });
  };

  // 잡 진행률을 done/error 까지 폴링하며 exportProgress 갱신
  const pollExportJob = (jobId: string, filename: string): Promise<void> =>
    new Promise((resolve, reject) => {
      const tick = async () => {
        try {
          const { data } = await resultsApi.exportJobStatus(jobId);
          setExportProgress((prev) =>
            prev[filename]
              ? { ...prev, [filename]: { percent: data.percent ?? 0, phase: data.phase ?? '' } }
              : prev,
          );
          if (data.status === 'done') { resolve(); return; }
          if (data.status === 'error') { reject(new Error(data.error || 'export failed')); return; }
          setTimeout(tick, 500);
        } catch (e) {
          reject(e);
        }
      };
      tick();
    });

  const exportBundle = async (filename: string) => {
    if (exportProgress[filename]) return; // 이미 진행 중이면 무시
    setExportProgress((prev) => ({ ...prev, [filename]: { percent: 0, phase: '준비 중' } }));
    try {
      // 1) 백그라운드 잡 시작
      const { data: started } = await resultsApi.exportBundle(filename);
      const jobId: string = started.job_id;
      // 2) 진행률 폴링
      await pollExportJob(jobId, filename);
      // 3) 완료된 ZIP 다운로드
      const res = await resultsApi.exportJobDownload(jobId);
      const blob = new Blob([res.data], { type: 'application/zip' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const baseName = filename.replace('/result.json', '').replace('.json', '');
      a.href = url;
      a.download = `${baseName}.zip`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      message.success(t('results.exportBundleComplete', { path: `${baseName}.zip`, count: '1' }));
    } catch (e: any) {
      message.error(e.response?.data?.detail || e.message || t('results.exportBundleFailed'));
    } finally {
      setExportProgress((prev) => {
        const next = { ...prev };
        delete next[filename];
        return next;
      });
    }
  };

  const openFolder = async (filename: string) => {
    try {
      await resultsApi.openFolder(filename);
    } catch { /* ignore */ }
  };

  // 상세 모달 'HTML 생성' — 해당 결과의 result.html을 현재 코드로 재생성
  const regenerateHtml = async (filename: string) => {
    setHtmlGenLoading(true);
    try {
      const { data } = await resultsApi.regenerateHtml(filename);
      message.success(t('results.generateHtmlComplete', { path: data.path }));
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('results.generateHtmlFailed'));
    } finally {
      setHtmlGenLoading(false);
    }
  };

  useEffect(() => {
    fetchResults();
    const onTabChange = (e: Event) => {
      if ((e as CustomEvent).detail === '/results') {
        fetchResults();
      }
    };
    window.addEventListener('tab-change', onTabChange);
    return () => window.removeEventListener('tab-change', onTabChange);
  }, []);

  // 결과 상세 열릴 때 BG_TASK 마커 감지 → 폴링
  useEffect(() => {
    // 이전 폴링 정리 (detail 변경이므로 backend cancel은 안 함 — 이전 detail의 태스크는 그대로 둠)
    stopAllResultBgPolls(false);

    if (!detail || !detailFilename) return;

    detail.step_results.forEach((sr, idx) => {
      const bgMatch = sr.message?.match?.(/\[BG_TASK:(bg_\d+)\]/);
      if (!bgMatch) return;

      const taskId = bgMatch[1];
      // 즉시 "실행 중" 표시
      setDetail(prev => {
        if (!prev) return prev;
        const updated = { ...prev, step_results: [...prev.step_results] };
        updated.step_results[idx] = { ...updated.step_results[idx], message: `⏳ ${t('record.cmdRunning')}...` };
        return updated;
      });

      const poll = setInterval(async () => {
        try {
          const r = await scenarioApi.getCmdResult(taskId);
          if (r.data.status === 'running') {
            // 라이브 업데이트: 누적 stdout을 계속 반영 (send_command_stream)
            const liveStdout = r.data.stdout ?? '';
            if (liveStdout) {
              setDetail(prev => {
                if (!prev) return prev;
                const updated = { ...prev, step_results: [...prev.step_results] };
                updated.step_results[idx] = { ...updated.step_results[idx], message: liveStdout };
                return updated;
              });
            }
            return;
          }
          clearInterval(poll);

          // 서버가 계산한 final_message/final_status 사용
          const finalMsg = r.data.final_message ?? r.data.stdout ?? '';
          const finalStatus = r.data.final_status as 'pass' | 'fail' | null | undefined;

          setDetail(prev => {
            if (!prev) return prev;
            const updated = { ...prev, step_results: [...prev.step_results] };
            const step = updated.step_results[idx];
            const newStatus = finalStatus ?? step.status;
            updated.step_results[idx] = { ...step, message: finalMsg, status: newStatus };

            // status가 fail로 바뀐 경우 카운트 재계산
            if (finalStatus === 'fail' && step.status !== 'fail') {
              updated.failed_steps += 1;
              if (step.status === 'pass') updated.passed_steps = Math.max(0, updated.passed_steps - 1);
              else if (step.status === 'warning') updated.warning_steps = Math.max(0, updated.warning_steps - 1);
              if (updated.failed_steps > 0 || updated.error_steps > 0) updated.status = 'fail';
            }

            // 백엔드에 영구 저장
            resultsApi.updateStepResult(detailFilename, idx, finalMsg, newStatus).catch(() => {});
            return updated;
          });
        } catch (err: any) {
          clearInterval(poll);
          // 404 = 태스크가 서버 메모리에 없음 (서버 재시작 등)
          if (err?.response?.status === 404) {
            const lostMsg = `[BG_TASK:${taskId}] 결과 소실 (서버 재시작)`;
            setDetail(prev => {
              if (!prev) return prev;
              const updated = { ...prev, step_results: [...prev.step_results] };
              updated.step_results[idx] = { ...updated.step_results[idx], message: lostMsg };
              return updated;
            });
            resultsApi.updateStepResult(detailFilename, idx, lostMsg).catch(() => {});
          }
        }
      }, 1000);
      bgPollTimers.current.push(poll);
      bgPollTaskIds.current.push(taskId);
    });

    return () => {
      stopAllResultBgPolls();
    };
  }, [detail?.scenario_name, detailFilename, detailVisible]);

  const totalTime = (stepResults: StepResultDetail[]) =>
    stepResults.reduce((sum, s) => sum + (s.execution_time_ms || 0), 0);

  const formatDuration = (ms: number) => {
    if (ms < 1000) return `${ms}ms`;
    const sec = Math.floor(ms / 1000);
    const remain = ms % 1000;
    if (sec < 60) return `${sec}.${String(remain).padStart(3, '0').slice(0, 1)}s`;
    const min = Math.floor(sec / 60);
    const remSec = sec % 60;
    return `${min}m ${remSec}s`;
  };

  const formatTime = (iso: string, inline = false) => {
    if (!iso) return '-';
    try {
      const d = new Date(iso);
      const date = d.toLocaleDateString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit' });
      const time = d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
      if (inline) return `${date} ${time}`;
      return <>{date}<br />{time}</>;
    } catch {
      return iso;
    }
  };

  const deleteSelected = () => {
    if (selectedRowKeys.length === 0) return;
    // 선택된 그룹의 모든 파일명 수집
    const filesToDelete: string[] = [];
    for (const key of selectedRowKeys) {
      const group = groupedResults.find(g => g.key === key);
      if (group) {
        filesToDelete.push(...group.items.map(i => i.filename));
      }
    }
    Modal.confirm({
      title: t('results.deleteTitle'),
      content: `${selectedRowKeys.length}${t('results.deleteSelectedConfirm')} (${filesToDelete.length} files)`,
      okText: t('common.delete'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        for (const fn of filesToDelete) {
          try { await resultsApi.delete(fn); } catch { /* skip */ }
        }
        message.success(t('common.deleteComplete'));
        setSelectedRowKeys([]);
        fetchResults();
      },
    });
  };

  // 시나리오 이름 필터링 + 검색
  const filteredResults = scenarioFilter
    ? results.filter(r => r.scenario_name.toLowerCase().includes(scenarioFilter.toLowerCase()))
    : results;

  // 시나리오 이름 목록 (필터 드롭다운용)
  const scenarioNames = [...new Set(results.map(r => r.scenario_name))].sort();

  // 파일명에서 타임스탬프 추출: ScenarioName_YYYYMMDD_HHMMSS.json → YYYYMMDD_HHMMSS
  const extractTimestamp = (filename: string): string => {
    const m = filename.match(/(\d{8}_\d{6})\.json$/);
    return m ? m[1] : filename;
  };

  // 같은 타임스탬프의 결과를 묶음으로 그룹화
  const groupedResults: ResultGroup[] = React.useMemo(() => {
    const map = new Map<string, ResultSummary[]>();
    for (const r of filteredResults) {
      const ts = extractTimestamp(r.filename);
      if (!map.has(ts)) map.set(ts, []);
      map.get(ts)!.push(r);
    }
    return Array.from(map.entries()).map(([ts, items]) => {
      // 저장된 status 필드뿐 아니라 카운터(failed_steps/error_steps)도 확인 — 백엔드가
      // 어떤 경로에서 status="pass"로 저장하면서 failed_steps>0을 동시에 쓰는 일이
      // 발생할 경우, 카운터를 신뢰해 사용자가 "PASS인데 빨간 카운트"로 혼란을 겪지 않게 함.
      // (조건부이동으로 인한 revisit이 카운터에는 누적되지만 status 결정 시점에 빠지는
      // 케이스 등을 화면 단에서 방어)
      const hasAnyFail = items.some(i =>
        i.status === 'fail' || i.status === 'error' ||
        (i.failed_steps || 0) > 0 || (i.error_steps || 0) > 0
      );
      const hasWarning = items.some(i => i.status === 'warning' || (i.warning_steps || 0) > 0);
      const names = [...new Set(items.map(i => i.scenario_name))];
      return {
        key: ts,
        timestamp: items[0].started_at,
        items,
        status: hasAnyFail ? 'fail' : hasWarning ? 'warning' : 'pass',
        scenario_names: names.join(', '),
        total_repeat: Math.max(...items.map(i => i.total_repeat || 1)),
      };
    });
  }, [filteredResults]);

  const groupColumns = [
    {
      title: t('results.execTime'),
      key: 'time',
      width: 200,
      render: (_: any, g: ResultGroup) => <span style={{ fontSize: 11, lineHeight: 1.4 }}>{formatTime(g.timestamp)}</span>,
      sorter: (a: ResultGroup, b: ResultGroup) => (a.timestamp || '').localeCompare(b.timestamp || ''),
      defaultSortOrder: 'descend' as const,
    },
    {
      title: t('results.scenario'),
      key: 'name',
      render: (_: any, g: ResultGroup) => (
        <Space size={4} wrap>
          <span>{g.scenario_names}</span>
          {g.items.length > 1 && <Tag color="blue">{g.items.length} {t('results.scenarios')}</Tag>}
          {g.total_repeat > 1 && <Tag color="purple">{g.total_repeat}x</Tag>}
        </Space>
      ),
      sorter: (a: ResultGroup, b: ResultGroup) => a.scenario_names.localeCompare(b.scenario_names),
    },
    {
      title: t('common.status'),
      key: 'status',
      width: 90,
      render: (_: any, g: ResultGroup) => <Tag color={statusColor(g.status)}>{g.status.toUpperCase()}</Tag>,
      filters: [
        { text: 'PASS', value: 'pass' },
        { text: 'FAIL', value: 'fail' },
        { text: 'WARNING', value: 'warning' },
      ],
      onFilter: (value: any, g: ResultGroup) => g.status === value,
    },
    {
      title: t('common.result'),
      key: 'counts',
      width: 180,
      render: (_: any, g: ResultGroup) => {
        const p = g.items.reduce((s, i) => s + i.passed_steps, 0);
        const f = g.items.reduce((s, i) => s + i.failed_steps, 0);
        const w = g.items.reduce((s, i) => s + i.warning_steps, 0);
        const e = g.items.reduce((s, i) => s + i.error_steps, 0);
        const total = g.items.reduce((s, i) => s + i.total_steps, 0);
        return (
          <Space size={4}>
            <Tag color="green">{p}P</Tag>
            <Tag color="red">{f}F</Tag>
            {w > 0 && <Tag color="orange">{w}W</Tag>}
            {e > 0 && <Tag color="volcano">{e}E</Tag>}
            <span style={{ color: '#888' }}>/ {total}</span>
          </Space>
        );
      },
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 160,
      render: (_: any, g: ResultGroup) => {
        if (g.items.length === 1) {
          const r = g.items[0];
          return (
            <Space size={4}>
              <Button size="small" icon={<FolderOpenOutlined />} onClick={() => openFolder(r.filename)} />
              <Button size="small" icon={<EyeOutlined />} onClick={() => viewDetail(r.filename)}>{t('common.details')}</Button>
              <ExportProgressButton size="small" progress={exportProgress[r.filename]} onClick={() => exportBundle(r.filename)} />
              <Button size="small" danger icon={<DeleteOutlined />} onClick={() => deleteResult(r.filename)} />
            </Space>
          );
        }
        return (
          <Space size={4}>
            <Button size="small" icon={<EyeOutlined />} onClick={() => viewGroupDetail(g)}>{t('common.details')}</Button>
            <Button size="small" danger icon={<DeleteOutlined />} onClick={() => {
              Modal.confirm({
                title: t('results.deleteTitle'),
                onOk: async () => {
                  for (const item of g.items) {
                    try { await resultsApi.delete(item.filename); } catch { /* ignore */ }
                  }
                  fetchResults();
                },
              });
            }} />
          </Space>
        );
      },
    },
  ];

  const _colTitle = (en: string, ko: string) => <div style={{ textAlign: 'center' }}>{en}<br /><span style={{ fontSize: 10, color: '#888' }}>{ko}</span></div>;
  // 필터용: 현재 표시 데이터에서 고유값 추출
  const _allSteps: StepResultDetail[] = detail?.step_results || (groupDetail ? groupDetail.flatMap(d => d.step_results || []) : []);
  const _uniqueStatuses = [...new Set(_allSteps.map(s => effStatus(s)).filter(Boolean))].sort();
  const _uniqueDevices = [...new Set(_allSteps.map(s => s.device_id).filter(Boolean))].sort();
  const _uniqueRepeats = [...new Set(_allSteps.map(s => s.repeat_index ?? 1))].sort((a, b) => a - b);

  const stepColumns = ([
    {
      title: _colTitle('Time Stamp', t('results.timestamp')),
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 95,
      align: 'center' as const,
      render: (v: string | null) => <span style={{ fontSize: 11, lineHeight: 1.4 }}>{v ? formatTime(v) : '-'}</span>,
      _hide: false,
    },
    {
      title: _colTitle('Repeat', t('results.repeat')),
      key: 'repeat',
      width: 80,
      align: 'center' as const,
      filters: _uniqueRepeats.map(r => ({ text: `#${r}`, value: r })),
      onFilter: (value: any, record: any) => (record.repeat_index ?? 1) === value,
      render: (_: any, r: StepResultDetail) => {
        const total = detail?.total_repeat || (groupDetail ? Math.max(...groupDetail.map(d => d.total_repeat || 1)) : 1);
        return `${r.repeat_index ?? 1}/${total}`;
      },
      _hide: false,
    },
    {
      title: _colTitle('Step', t('results.step')),
      dataIndex: 'step_id',
      key: 'step_id',
      width: 90,
      align: 'center' as const,
      render: (_: any, r: any) => {
        // 인라인 runtime fail (sync 모드 fail_on_keyword 결과)은 Fail_Count_N으로 표시.
        if (r.parent_step_id != null && r.fail_index != null) {
          return <span style={{ color: '#ff4d4f' }}>↳ Fail_Count_{r.fail_index}</span>;
        }
        return r._seq || r.step_id;
      },
      _hide: false,
    },
    {
      title: _colTitle('Device', t('results.deviceCol')),
      dataIndex: 'device_id',
      key: 'device_id',
      width: 120,
      align: 'center' as const,
      filters: _uniqueDevices.map(d => ({ text: d, value: d })),
      onFilter: (value: any, record: any) => (record.device_id || '') === value,
      render: (v: string) => v || '-',
      _hide: false,
    },
    {
      title: _colTitle('Command', 'action'),
      dataIndex: 'command',
      key: 'command',
      width: 200,
      ellipsis: true,
      align: 'center' as const,
      render: (v: string, r: StepResultDetail) => {
        // module_command 결과에 message가 있으면 툴팁으로 표시
        const isModuleStep = v?.includes('::');
        if (isModuleStep && r.message) {
          if (r.message.match(/\[BG_TASK:/)) return <span>{v} <Tag color="processing">BG</Tag></span>;
          if (r.message.startsWith('⏳')) return <span>{v} <Tag color="processing">⏳</Tag></span>;
          return <Tooltip title={<pre style={{ margin: 0, fontSize: 10, whiteSpace: 'pre-wrap', maxHeight: 200, overflow: 'auto' }}>{r.message}</pre>}><span>{v}</span></Tooltip>;
        }
        return <span style={{ textAlign: 'left', display: 'block' }}>{v || r.message || '-'}</span>;
      },
      _hide: false,
    },
    {
      title: _colTitle('Remark', t('results.remark')),
      dataIndex: 'description',
      key: 'description',
      width: 200,
      ellipsis: true,
      align: 'center' as const,
      render: (v: string) => <span style={{ textAlign: 'left', display: 'block' }}>{v || '-'}</span>,
      _hide: webcamExpanded,
    },
    {
      title: _colTitle('Status', t('results.resultCol')),
      dataIndex: 'status',
      key: 'status',
      width: 100,
      align: 'center' as const,
      filters: _uniqueStatuses.map(s => ({ text: statusText(s, t), value: s })),
      onFilter: (value: any, record: any) => effStatus(record) === value,
      defaultFilteredValue: null,
      render: (_s: string, record: StepResultDetail) => <Tag color={statusColor(effStatus(record))} style={{ margin: 0 }}>{statusText(effStatus(record), t)}</Tag>,
      _hide: false,
    },
    {
      title: _colTitle('Delay', t('results.delaySet')),
      dataIndex: 'delay_ms',
      key: 'delay',
      width: 90,
      align: 'center' as const,
      render: (v: number) => v ? formatDuration(v) : '-',
      _hide: webcamExpanded,
    },
    {
      title: _colTitle('Duration', t('results.duration')),
      dataIndex: 'execution_time_ms',
      key: 'duration',
      width: 100,
      align: 'center' as const,
      render: (v: number) => formatDuration(v),
      _hide: webcamExpanded,
    },
    {
      title: _colTitle('', t('scenario.compare')),
      key: 'compare',
      width: 130,
      align: 'center' as const,
      render: (_: any, r: StepResultDetail) => {
        // 모듈 명령(cmd, adb_send 등)의 출력값을 LOG 버튼으로 노출
        const isModuleMsg = !!r.command?.includes('::');
        const isRandMsg = !!r.message && r.message.startsWith('[RAND]');
        const hasMsg = (isModuleMsg && !!r.message) || isRandMsg;
        const hasImage = !!(r.expected_image || r.actual_image);
        if (!hasMsg && !hasImage) return '-';
        return (
          <Space size={4}>
            {hasImage && <Button size="small" onClick={() => setCompareStep(r)}>{t('scenario.compare')}</Button>}
            {hasMsg && <Button size="small" onClick={() => setCompareStep(r)}>LOG</Button>}
          </Space>
        );
      },
      _hide: false,
    },
  ] as any[]).filter((c: any) => !c._hide);

  return (
    <div>
      <Card
        title={t('results.title')}
        extra={
          <Space>
            <Input
              placeholder={t('common.search')}
              prefix={<SearchOutlined />}
              value={scenarioFilter}
              onChange={(e) => setScenarioFilter(e.target.value)}
              allowClear
              style={{ width: 200 }}
              size="small"
            />
            {selectedRowKeys.length > 0 && (
              <Button danger size="small" icon={<DeleteOutlined />} onClick={deleteSelected}>
                {t('common.delete')} ({selectedRowKeys.length})
              </Button>
            )}
            <Button
              size="small"
              onClick={async () => {
                Modal.confirm({
                  title: t('results.migrateLegacyTitle'),
                  content: t('results.migrateLegacyDesc'),
                  okText: t('results.migrateLegacyOk'),
                  onOk: async () => {
                    const hide = message.loading(t('results.migrating'), 0);
                    try {
                      const res = await resultsApi.migrateLegacy();
                      hide();
                      message.success(t('results.migrateComplete', { count: String(res.data.migrated) }));
                      if (res.data.errors?.length) {
                        Modal.warning({ title: t('results.migrateErrors'), content: res.data.errors.join('\n') });
                      }
                      fetchResults();
                    } catch {
                      hide();
                      message.error(t('results.migrateFailed'));
                    }
                  },
                });
              }}
            >
              {t('results.migrateLegacy')}
            </Button>
            <Button icon={<ReloadOutlined />} onClick={fetchResults} loading={loading} size="small">
              {t('common.refresh')}
            </Button>
          </Space>
        }
      >
        <Table
          columns={groupColumns as any}
          dataSource={groupedResults}
          rowKey="key"
          size="small"
          pagination={{ pageSize: 15, showSizeChanger: true }}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
          expandable={{
            expandedRowRender: (g: ResultGroup) => g.items.length > 1 ? (
              <div style={{ padding: '4px 0' }}>
                {g.items.map((r, idx) => (
                  <div key={r.filename} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 8px', borderBottom: idx < g.items.length - 1 ? '1px solid #f0f0f0' : undefined }}>
                    <Tag style={{ margin: 0 }}>{idx + 1}</Tag>
                    <span style={{ flex: 1 }}>{r.scenario_name}</span>
                    <Tag color={statusColor(r.status)}>{r.status.toUpperCase()}</Tag>
                    <Space size={4}>
                      <Tag color="green">{r.passed_steps}P</Tag>
                      <Tag color="red">{r.failed_steps}F</Tag>
                      {r.total_repeat > 1 && <Tag color="purple">{r.total_repeat}x</Tag>}
                    </Space>
                    <Button size="small" icon={<FolderOpenOutlined />} onClick={() => openFolder(r.filename)} />
                    <Button size="small" icon={<EyeOutlined />} onClick={() => viewDetail(r.filename)}>{t('common.details')}</Button>
                    <ExportProgressButton size="small" progress={exportProgress[r.filename]} onClick={() => exportBundle(r.filename)} />
                  </div>
                ))}
              </div>
            ) : null,
            rowExpandable: (g: ResultGroup) => g.items.length > 1,
          }}
        />
      </Card>

      {/* Detail report modal */}
      <Modal
        title={
          <Space>
            <span>{detail?.scenario_name || t('scenario.resultDetail')}</span>
            {detail && <Tag color={statusColor(detail.status)}>{detail.status.toUpperCase()}</Tag>}
          </Space>
        }
        open={detailVisible}
        onCancel={() => { setDetailVisible(false); setWebcamPanelOpen(false); setWebcamExpanded(false); setCurrentPlayingStepId(null); setGroupDetail(null); }}
        width="90vw"
        style={{ top: 20 }}
        footer={
          <Space>
            <Button
              icon={<FolderOpenOutlined />}
              onClick={() => detailFilename && openFolder(detailFilename)}
            >
              {t('results.openFolder')}
            </Button>
            <Button
              icon={<FileTextOutlined />}
              loading={htmlGenLoading}
              onClick={() => detailFilename && regenerateHtml(detailFilename)}
            >
              {t('results.generateHtml')}
            </Button>
            <ExportProgressButton
              progress={detailFilename ? exportProgress[detailFilename] : undefined}
              onClick={() => detailFilename && exportBundle(detailFilename)}
            >
              {t('results.exportBundle')}
            </ExportProgressButton>
            <Button
              danger
              icon={<DeleteOutlined />}
              onClick={() => detailFilename && deleteResult(detailFilename)}
            >
              {t('common.delete')}
            </Button>
          </Space>
        }
      >
        {detailLoading && !detail && !groupDetail && (
          <div style={{ textAlign: 'center', padding: '60px 0' }}>
            <Spin size="large" tip={t('results.loading')} />
          </div>
        )}
        {groupDetail && groupDetail.length > 0 && (() => {
          const totalRepeat = Math.max(...groupDetail.map(d => d.total_repeat || 1));
          // 현재 사이클의 스텝들을 시나리오 순서대로 합침 (연번 부여)
          const cycleSteps: (StepResultDetail & { _seq?: number; _scenarioName?: string })[] = [];
          let seq = 0;
          for (const d of groupDetail) {
            const stepsForCycle = d.step_results.filter(sr => sr.repeat_index === groupDetailCycle);
            for (const s of stepsForCycle) {
              seq++;
              cycleSteps.push({ ...s, _seq: seq, _scenarioName: d.scenario_name });
            }
          }
          const cycleBranch = cycleSteps.filter(s => s.excluded_from_result).length;
          // 결과 미반영('분기') 스텝은 pass/fail/error 어디에도 집계하지 않음
          const cyclePass = cycleSteps.filter(s => !s.excluded_from_result && s.status === 'pass').length;
          const cycleFail = cycleSteps.filter(s => !s.excluded_from_result && s.status === 'fail').length;
          const cycleWarn = cycleSteps.filter(s => !s.excluded_from_result && s.status === 'warning').length;
          const cycleErr = cycleSteps.filter(s => !s.excluded_from_result && s.status !== 'pass' && s.status !== 'fail' && s.status !== 'warning').length;
          return (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10 }}>
                <span style={{ fontWeight: 600 }}>Cycle:</span>
                {Array.from({ length: totalRepeat }, (_, i) => i + 1).map(c => {
                  // 어떤 시나리오라도 이 cycle에서 중단됐으면 마킹
                  const stoppedHere = groupDetail.some(d => d.stopped_at_iteration === c);
                  return (
                    <Button key={c} size="small" type={groupDetailCycle === c ? 'primary' : 'default'} onClick={() => setGroupDetailCycle(c)} danger={stoppedHere}>
                      {c}{stoppedHere ? ' ⏹' : ''}
                    </Button>
                  );
                })}
                <span style={{ marginLeft: 'auto', color: '#888' }}>
                  {groupDetail.map(d => d.scenario_name).join(' → ')}
                </span>
              </div>
              <Space size={8} style={{ marginBottom: 10 }}>
                <Tag color="green">{cyclePass} Pass</Tag>
                <Tag color="red">{cycleFail} Fail</Tag>
                {cycleWarn > 0 && <Tag color="orange">{cycleWarn} Warning</Tag>}
                {cycleErr > 0 && <Tag color="volcano">{cycleErr} Error</Tag>}
                {cycleBranch > 0 && <Tag color="purple">{cycleBranch} {t('results.statusBranch')}</Tag>}
                <span style={{ color: '#888' }}>/ {cycleSteps.length} steps</span>
              </Space>
              {/* 웹캠 패널 */}
              {recordings.length > 0 && (
                <Collapse
                  activeKey={webcamPanelOpen ? ['webcam'] : []}
                  onChange={(keys) => setWebcamPanelOpen(keys.includes('webcam'))}
                  style={{ marginBottom: 10 }}
                  items={[{
                    key: 'webcam',
                    label: <Space><VideoCameraOutlined /> {t('webcam.recordings')} ({recordings.length})</Space>,
                    children: (
                      <div>
                        <Space style={{ marginBottom: 6 }}>
                          {recordings.map((rec, i) => {
                            const m = rec.filename.match(/webcam_r(\d+)\.(?:webm|mp4)$/);
                            const recCycle = m ? parseInt(m[1]) : i + 1;
                            return (
                              <Button key={rec.filename} size="small"
                                type={activeRecUrl === rec.url ? 'primary' : 'default'}
                                onClick={() => { setActiveRecUrl(rec.url); setActiveRecRepeat(recCycle); setGroupDetailCycle(recCycle); }}
                              >
                                Cycle {recCycle}
                              </Button>
                            );
                          })}
                        </Space>
                        {activeRecBlobUrl && <video key={activeRecBlobUrl} ref={detailVideoRef} src={activeRecBlobUrl} controls preload="auto" onLoadedMetadata={handleVideoCanPlay} onCanPlay={handleVideoCanPlay} onTimeUpdate={handleVideoTimeUpdate} onPause={handleVideoPauseOrEnd} onEnded={handleVideoPauseOrEnd} onError={handleVideoError} style={{ width: '100%', maxHeight: 400 }} />}
                      </div>
                    ),
                  }]}
                />
              )}
              <Table
                columns={stepColumns as any}
                dataSource={cycleSteps}
                rowKey={(r: any) => `${r._seq || r.step_id}_${r.repeat_index}_${r.device_id}`}
                size="small"
                virtual
                pagination={false}
                scroll={{ x: 1205, y: detailTableY }}
                rowClassName={(r: any, idx: number) => {
                  const statusCls = r.excluded_from_result ? '' : r.status === 'pass' ? 'row-pass' : r.status === 'fail' ? 'row-fail' : r.status === 'error' ? 'row-error' : '';
                  // 시나리오 경계 (이전 스텝과 시나리오명 다르면)
                  const prevScenario = idx > 0 ? (cycleSteps[idx - 1] as any)?._scenarioName : null;
                  const boundary = prevScenario && prevScenario !== r._scenarioName ? 'scenario-boundary' : '';
                  return `${statusCls} ${boundary}`.trim();
                }}
              />
            </>
          );
        })()}
        {!groupDetail && detail && (
          <>
            <Descriptions
              bordered
              size="small"
              column={4}
              style={{ marginBottom: 13 }}
            >
              <Descriptions.Item label={t('results.scenario')}>{detail.scenario_name}</Descriptions.Item>
              <Descriptions.Item label={t('scenario.device')}>{detail.device_serial || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('scenario.startTime')}>{formatTime(detail.started_at, true)}</Descriptions.Item>
              <Descriptions.Item label={t('scenario.endTime')}>{formatTime(detail.finished_at, true)}</Descriptions.Item>
              <Descriptions.Item label={t('results.totalExecTime')}>
                <strong>{formatDuration(totalTime(detail.step_results))}</strong>
              </Descriptions.Item>
              <Descriptions.Item label="Repeat">{detail.total_repeat}{t('results.times')}</Descriptions.Item>
              <Descriptions.Item label={t('common.result')}>
                <Space size={4}>
                  <Tag color="green">{detail.passed_steps} Pass</Tag>
                  <Tag color="red">{detail.failed_steps} Fail</Tag>
                  {detail.error_steps > 0 && <Tag color="volcano">{detail.error_steps} Error</Tag>}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label={t('common.status')}>
                <Space size={4} wrap>
                  <Tag color={statusColor(detail.status)} style={{ fontSize: 12 }}>
                    {detail.status.toUpperCase()}
                  </Tag>
                  {detail.status === 'stopped' && detail.stopped_at_iteration != null && (
                    <Tag color="orange" style={{ fontSize: 11 }}>
                      Cycle {detail.stopped_at_iteration} 중단
                      {detail.stopped_at_step != null ? ` @ Step ${detail.stopped_at_step}` : ''}
                    </Tag>
                  )}
                </Space>
              </Descriptions.Item>
            </Descriptions>

            <div style={{ display: 'flex', gap: 6, maxHeight: 'calc(90vh - 200px)', overflow: 'hidden' }}>
              {/* 좌측: 웹캠 녹화 패널 (접힘/펼침) */}
              {recordings.length > 0 && (
                <div style={{ width: webcamPanelOpen ? (webcamExpanded ? '60%' : 300) : 36, flexShrink: 0, transition: 'width 0.2s' }}>
                  {webcamPanelOpen ? (
                    <Card
                      size="small"
                      title={<Space size={4}><VideoCameraOutlined />{t('webcam.recordings')}</Space>}
                      extra={
                        <Space size={0}>
                          <Button type="text" size="small" icon={webcamExpanded ? <ShrinkOutlined /> : <ExpandOutlined />}
                            onClick={() => setWebcamExpanded(!webcamExpanded)} style={{ fontSize: 10 }} />
                          <Button type="text" size="small" onClick={() => { setWebcamPanelOpen(false); setWebcamExpanded(false); }} style={{ fontSize: 10 }}>✕</Button>
                        </Space>
                      }
                      bodyStyle={{ padding: 5 }}
                    >
                      <video
                        key={activeRecBlobUrl}
                        ref={detailVideoRef}
                        src={activeRecBlobUrl}
                        controls
                        preload="auto"
                        onLoadedMetadata={handleVideoCanPlay}
                        onCanPlay={handleVideoCanPlay}
                        onTimeUpdate={handleVideoTimeUpdate}
                        onPause={handleVideoPauseOrEnd}
                        onEnded={handleVideoPauseOrEnd}
                        onError={handleVideoError}
                        style={{ width: '100%', borderRadius: 4, background: '#000', display: 'block', marginBottom: 5 }}
                      />
                      {recordings.length > 1 && (
                        <Select
                          size="small"
                          value={activeRecRepeat}
                          onChange={(v) => {
                            const rec = recordings.find(r => cycleIndexOf(r.filename) === v);
                            if (rec) { setActiveRecUrl(rec.url); setActiveRecRepeat(v); }
                          }}
                          style={{ width: '100%', marginBottom: 5 }}
                          options={recordings.map(r => {
                            const ri = cycleIndexOf(r.filename);
                            return { value: ri, label: `${t('webcam.repeat')} ${ri}  (${(r.size / 1024 / 1024).toFixed(1)} MB)` };
                          })}
                        />
                      )}
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                        {recordings.map((rec) => {
                          const ci = cycleIndexOf(rec.filename);
                          const ri = ci === Number.MAX_SAFE_INTEGER ? '?' : String(ci);
                          const isActive = rec.url === activeRecUrl;
                          return (
                            <div key={rec.filename} style={{
                              display: 'flex', alignItems: 'center', gap: 3, fontSize: 10,
                              padding: '2px 4px', borderRadius: 4,
                              background: isActive ? 'var(--accent-light, #e6f4ff)' : 'transparent',
                              border: isActive ? '1px solid var(--accent, #1677ff)' : '1px solid transparent',
                              cursor: 'pointer',
                            }}
                              onClick={() => { setActiveRecUrl(rec.url); setActiveRecRepeat(ci === Number.MAX_SAFE_INTEGER ? 1 : ci); }}
                            >
                              <Tag color={isActive ? 'processing' : 'blue'} style={{ margin: 0, fontSize: 9 }}>R{ri}</Tag>
                              <span style={{ flex: 1, color: isActive ? 'var(--accent, #1677ff)' : '#888', fontWeight: isActive ? 600 : 400 }}>{(rec.size / 1024 / 1024).toFixed(1)}MB</span>
                              <Tooltip title={t('webcam.trimSave')}>
                                <Button size="small" type="text" icon={<ScissorOutlined />} style={{ padding: '0 4px', height: 20 }}
                                  onClick={() => {
                                    setTrimFile(rec.filename);
                                    setTrimStart(0);
                                    // 비디오 길이를 임시 video 요소로 가져와 trimEnd 초기화
                                    const tmpVideo = document.createElement('video');
                                    tmpVideo.src = `/recordings/${rec.filename}`;
                                    tmpVideo.onloadedmetadata = () => { setTrimEnd(Math.round(tmpVideo.duration * 10) / 10); tmpVideo.src = ''; };
                                    tmpVideo.onerror = () => setTrimEnd(0);
                                  }} />
                              </Tooltip>
                              <Tooltip title={t('common.delete')}>
                                <Button size="small" type="text" danger icon={<DeleteOutlined />} style={{ padding: '0 4px', height: 20 }}
                                  onClick={() => Modal.confirm({
                                    title: t('webcam.deleteConfirm'), okType: 'danger',
                                    onOk: async () => {
                                      await resultsApi.deleteRecording(rec.filename);
                                      message.success(t('webcam.deleteSuccess'));
                                      // 삭제된 녹화가 현재 재생 중이면 URL 초기화
                                      if (rec.url === activeRecUrl) setActiveRecUrl('');
                                      fetchRecordings(detailFilename);
                                    },
                                  })} />
                              </Tooltip>
                            </div>
                          );
                        })}
                      </div>
                    </Card>
                  ) : (
                    <Tooltip title={t('webcam.recordings')} placement="right">
                      <Button
                        type="text"
                        icon={<VideoCameraOutlined />}
                        onClick={() => setWebcamPanelOpen(true)}
                        style={{ writingMode: 'vertical-rl', height: 'auto', padding: '8px 4px', fontSize: 11 }}
                      >
                        {t('webcam.recordings')} ({recordings.length})
                      </Button>
                    </Tooltip>
                  )}
                </div>
              )}

              {/* 우측: 스텝 결과 테이블 (스크롤) */}
              <div style={{ flex: 1, minWidth: 0, overflow: 'auto' }}>
                <Table
                  columns={stepColumns}
                  dataSource={detail.step_results}
                  rowKey={(r: StepResultDetail) => `${r.step_id}_${r.repeat_index}`}
                  size="small"
                  virtual
                  scroll={{ x: 1205, y: detailTableY }}
                  pagination={false}
                  rowClassName={(r: StepResultDetail) => {
                    const statusCls = r.excluded_from_result ? '' :
                      r.status === 'fail' ? 'result-row-fail' :
                      r.status === 'error' ? 'result-row-error' :
                      r.status === 'warning' ? 'result-row-warning' : '';
                    const playingCls = currentPlayingStepId === r.step_id && (r.repeat_index || 1) === activeRecRepeat ? 'result-row-playing' : '';
                    return [statusCls, playingCls].filter(Boolean).join(' ');
                  }}
                  onRow={(r) => ({
                    onClick: () => { if (recordings.length > 0) seekToStep(r); },
                    style: recordings.length > 0 ? { cursor: 'pointer' } : undefined,
                  })}
                />
              </div>
            </div>
          </>
        )}
      </Modal>

      {/* Trim modal */}
      <Modal
        title={t('webcam.trimSave')}
        open={!!trimFile}
        onCancel={() => setTrimFile(null)}
        onOk={async () => {
          if (!trimFile || trimEnd <= trimStart) return;
          try {
            const res = await resultsApi.trimRecording(trimFile, trimStart, trimEnd);
            message.success(t('webcam.trimSuccess'));
            setTrimFile(null);
            fetchRecordings(detailFilename);
          } catch (e: any) {
            message.error(e.response?.data?.detail || t('webcam.trimFailed'));
          }
        }}
        okText={t('webcam.trimSave')}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <div>
            <video src={trimFile ? `/recordings/${trimFile}` : undefined} controls style={{ width: '100%', borderRadius: 4 }} />
          </div>
          <Space>
            <span>{t('webcam.trimStart')}:</span>
            <InputNumber min={0} step={0.1} value={trimStart} onChange={(v) => setTrimStart(v || 0)} style={{ width: 100 }} />
            <span>{t('webcam.trimEnd')}:</span>
            <InputNumber min={0} step={0.1} value={trimEnd} onChange={(v) => setTrimEnd(v || 0)} style={{ width: 100 }} />
          </Space>
        </Space>
      </Modal>

      {/* Image comparison modal */}
      <Modal
        title={t('results.stepCompare', { id: String(compareStep?.step_id || '') })}
        open={!!compareStep}
        onCancel={() => setCompareStep(null)}
        width={1100}
        footer={null}
      >
        {compareStep && (() => {
          // 모듈 명령(cmd, adb_send 등)의 출력값 표시 — 이미지 비교와 동일 모달에서 함께 보여준다.
          const _msg = compareStep.message || '';
          const _hasImage = !!(compareStep.expected_image || compareStep.actual_image);
          const _isModuleCmd = !!compareStep.command?.includes('::');
          const _showLog = !!_msg && (_isModuleCmd || _msg.startsWith('[RAND]') || !_hasImage);
          const _isFail = compareStep.status === 'fail';
          const renderLogBlock = () => (
            <div style={{ marginBottom: 10 }}>
              {compareStep.command && (
                <div style={{ marginBottom: 4, padding: '6px 10px', background: '#1a1a2e', borderRadius: 4, fontFamily: 'monospace', fontSize: 11 }}>
                  <span style={{ color: '#888' }}>$ </span><span style={{ color: '#e0e0e0' }}>{compareStep.command}</span>
                </div>
              )}
              <div style={{
                padding: '8px 10px', borderRadius: 4, fontSize: 11, fontFamily: 'monospace',
                background: _isFail ? '#2a1215' : '#122010',
                border: `1px solid ${_isFail ? '#5c2024' : '#274916'}`,
                color: _isFail ? '#ff7875' : '#95de64',
                whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 400, overflow: 'auto',
              }}>{_msg}</div>
            </div>
          );
          return (
          <>
            <Space style={{ marginBottom: 13 }} wrap>
              <Tag color={statusColor(effStatus(compareStep))}>{statusDetail(compareStep, t)}</Tag>
              {compareStep.compare_mode && compareStep.compare_mode !== 'full' && (
                <Tag color="purple">
                  {compareStep.compare_mode === 'single_crop' ? t('results.singleCrop')
                    : compareStep.compare_mode === 'full_exclude' ? t('results.excludeArea')
                    : compareStep.compare_mode === 'multi_crop' ? t('results.multiCrop')
                    : compareStep.compare_mode}
                </Tag>
              )}
              {compareStep.similarity_score != null && (
                <span>
                  {t('results.similarity')}: {(compareStep.similarity_score * 100).toFixed(2)}%
                </span>
              )}
              {compareStep.match_location && (
                <Tag color="blue">
                  {t('results.matchLocation')}: ({compareStep.match_location.x},{compareStep.match_location.y})
                  {' '}{compareStep.match_location.width}x{compareStep.match_location.height}
                </Tag>
              )}
              <span style={{ color: '#888' }}>Duration: {formatDuration(compareStep.execution_time_ms)}</span>
            </Space>
            {_showLog && renderLogBlock()}
            {_hasImage && (
            <Row gutter={16}>
              <Col span={12}>
                <Card size="small" title={
                  compareStep.compare_mode === 'full_exclude' ? t('results.expectedExclude')
                  : compareStep.compare_mode === 'multi_crop' ? t('results.expectedCrop')
                  : t('results.expectedImage')
                }>
                  {compareStep.expected_image ? (
                    <div style={{ position: 'relative' }}>
                      <Image
                        src={`${imageUrl(compareStep.expected_annotated_image || compareStep.expected_image)!}?t=${Date.now()}`}
                        alt="Expected"
                        style={{ width: '100%' }}
                      />
                      {/* Overlay annotations for multi_crop when no pre-rendered annotated image */}
                      {!compareStep.expected_annotated_image && compareStep.compare_mode === 'multi_crop' && compareStep.sub_results?.length > 0 && (
                        <AnnotatedOverlay subResults={compareStep.sub_results} expectedImage={imageUrl(compareStep.expected_image)!} />
                      )}
                    </div>
                  ) : (
                    <div style={{ textAlign: 'center', padding: 32, color: '#666' }}>{t('common.noImage')}</div>
                  )}
                </Card>
              </Col>
              <Col span={12}>
                <Card size="small" title={t('results.actualImage')}>
                  {compareStep.actual_annotated_image ? (
                    <Image
                      src={`${imageUrl(compareStep.actual_annotated_image)!}?t=${Date.now()}`}
                      alt="Actual (annotated)"
                      style={{ width: '100%' }}
                    />
                  ) : compareStep.actual_image ? (
                    <Image
                      src={`${imageUrl(compareStep.actual_image)!}?t=${Date.now()}`}
                      alt="Actual"
                      style={{ width: '100%' }}
                    />
                  ) : (
                    <div style={{ textAlign: 'center', padding: 32, color: '#666' }}>{t('common.noImage')}</div>
                  )}
                </Card>
              </Col>
            </Row>
            )}
            {compareStep.compare_mode === 'full_exclude' && (
              <div style={{ marginTop: 10 }}>
                <Card size="small" title={t('results.excludeAreaCompare')}>
                  <Space wrap>
                    <Tag color="red">{t('results.excludeAreaApplied')}</Tag>
                    <span style={{ fontSize: 11, color: '#ccc' }}>{compareStep.message}</span>
                  </Space>
                  <div style={{ marginTop: 6, fontSize: 11, color: '#888' }}>
                    {t('results.excludeAreaDesc')}
                  </div>
                </Card>
              </div>
            )}
            {compareStep.compare_mode === 'multi_crop' && compareStep.sub_results?.length > 0 && (
              <div style={{ marginTop: 10 }}>
                <Card size="small" title={t('results.cropResults', { count: String(compareStep.sub_results.length) })}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid #303030' }}>
                        <th style={{ padding: '4px 8px', textAlign: 'left' }}>#</th>
                        <th style={{ padding: '4px 8px', textAlign: 'left' }}>{t('results.label')}</th>
                        <th style={{ padding: '4px 8px', textAlign: 'center' }}>{t('common.status')}</th>
                        <th style={{ padding: '4px 8px', textAlign: 'right' }}>{t('results.similarity')}</th>
                        <th style={{ padding: '4px 8px', textAlign: 'right' }}>{t('results.matchLocation')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {compareStep.sub_results.map((sr, si) => (
                        <tr key={si} style={{ borderBottom: '1px solid #222' }}>
                          <td style={{ padding: '4px 8px' }}>{si + 1}</td>
                          <td style={{ padding: '4px 8px' }}>{sr.label || '-'}</td>
                          <td style={{ padding: '4px 8px', textAlign: 'center' }}>
                            <Tag color={statusColor(sr.status)}>{sr.status.toUpperCase()}</Tag>
                          </td>
                          <td style={{ padding: '4px 8px', textAlign: 'right' }}>
                            {(sr.score * 100).toFixed(2)}%
                          </td>
                          <td style={{ padding: '4px 8px', textAlign: 'right' }}>
                            {sr.match_location
                              ? `(${sr.match_location.x},${sr.match_location.y}) ${sr.match_location.width}x${sr.match_location.height}`
                              : '-'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              </div>
            )}
          </>
          );
        })()}
      </Modal>

      <style>{`
        .result-row-fail td { background: rgba(255, 77, 79, 0.08) !important; }
        .result-row-error td { background: rgba(255, 122, 69, 0.08) !important; }
        .result-row-warning td { background: rgba(250, 173, 20, 0.08) !important; }
        .result-row-playing td { background: rgba(22, 119, 255, 0.18) !important; box-shadow: inset 3px 0 0 #1677ff; }
        @keyframes playingPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
        .result-row-playing td:first-child { animation: playingPulse 1.5s infinite; }
        .scenario-boundary td { border-top: 2px solid #1677ff !important; }
      `}</style>
    </div>
  );
}
