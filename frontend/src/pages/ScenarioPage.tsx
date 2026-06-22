import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { Button, Card, Checkbox, Col, Collapse, DatePicker, Descriptions, Divider, Dropdown, Image, Input, InputNumber, List, Modal, Radio, Row, Select, Space, Splitter, Table, Tabs, Tag, Tooltip, Tree, Upload, message, notification } from 'antd';
import dayjs from 'dayjs';
import type { TreeProps } from 'antd';
import {
  PlayCircleOutlined, PauseOutlined, DeleteOutlined, EyeOutlined,
  StopOutlined, CopyOutlined,
  FolderOutlined, FolderAddOutlined, FileOutlined,
  EditOutlined, BranchesOutlined,
  DownOutlined, RightOutlined, ClearOutlined, UploadOutlined,
  ExportOutlined, ImportOutlined, CheckCircleOutlined, WarningOutlined,
} from '@ant-design/icons';
import { scenarioApi, deviceApi, resultsApi, serverApi } from '../services/api';
import { useDevice } from '../context/DeviceContext';
import { useSettings } from '../context/SettingsContext';
import { useTranslation } from '../i18n';
import type { TranslationKey } from '../i18n';
import { useWebcamContext } from '../context/WebcamContext';
import { VideoCameraOutlined } from '@ant-design/icons';
import { Resizable } from 'react-resizable';
import 'react-resizable/css/styles.css';
import { useDLTSessions } from '../hooks/useDLTSessions';
import { useSerialSessions, useLogcatSessions } from '../hooks/useSerialSessions';

const ResizableTitle = (props: any) => {
  const { onResize, width, ...restProps } = props;
  if (!width) return <th {...restProps} />;
  return (
    <Resizable width={width} height={0} handle={<span className="react-resizable-handle" onClick={(e) => e.stopPropagation()} style={{ position: 'absolute', right: -5, bottom: 0, top: 0, width: 10, cursor: 'col-resize', zIndex: 1 }} />} onResize={onResize} draggableOpts={{ enableUserSelectHack: false }}>
      <th {...restProps} />
    </Resizable>
  );
};

// 반복 재생 종료 시각 DatePicker — controlled value를 useMemo로 안정화해서
// 부모(ScenarioPage)의 빈번한 폴링 리렌더에 패널 내부 임시 선택이 옛 값으로 되돌아가지 않게 한다.
const UntilTimePicker = React.memo(({ value, onChange, placeholder, tooltip, disabled }: {
  value: string | null;
  onChange: (iso: string | null) => void;
  placeholder: string;
  tooltip: string;
  disabled: boolean;
}) => {
  // 같은 ISO 문자열이면 같은 dayjs 인스턴스를 유지 → AntD DatePicker가 외부 변경으로 오인하지 않음
  const dayValue = useMemo(() => (value ? dayjs(value) : null), [value]);
  return (
    <Tooltip title={tooltip}>
      <DatePicker
        size="small"
        showTime={{ format: 'HH:mm' }}
        format="YYYY-MM-DD HH:mm"
        placeholder={placeholder}
        value={dayValue}
        onChange={(d) => onChange(d ? d.toISOString() : null)}
        onOk={(d) => onChange(d ? d.toISOString() : null)}
        disabled={disabled}
        allowClear
        style={{ width: 200 }}
      />
    </Tooltip>
  );
});

// 기대 이미지 썸네일 (ROI/크롭/영역제외 오버레이)
const ExpectedThumbnail = React.memo(({ src, regions, color, height = 32 }: {
  src: string; regions: { x: number; y: number; width: number; height: number }[]; color: string; height?: number;
}) => {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const draw = useCallback((canvas: HTMLCanvasElement, img: HTMLImageElement, w: number, h: number) => {
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext('2d')!;
    ctx.drawImage(img, 0, 0, w, h);
    const sx = w / img.width, sy = h / img.height;
    regions.forEach(r => {
      ctx.fillStyle = color === '#ff4d4f' ? 'rgba(255,77,79,0.3)' : 'rgba(82,196,26,0.3)';
      ctx.fillRect(r.x * sx, r.y * sy, r.width * sx, r.height * sy);
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1.5, 2 * sx);
      ctx.strokeRect(r.x * sx, r.y * sy, r.width * sx, r.height * sy);
    });
  }, [regions, color]);
  useEffect(() => {
    const img = new window.Image();
    img.onload = () => { const c = canvasRef.current; if (c) { const a = img.width / img.height; draw(c, img, Math.round(height * a), height); } };
    img.src = src;
  }, [src, regions, color, height, draw]);
  return (
    <>
      <canvas ref={canvasRef} style={{ height, borderRadius: 2, cursor: 'pointer' }} onClick={() => {
        const img = new window.Image();
        img.onload = () => { const c = document.createElement('canvas'); draw(c, img, img.width, img.height); setPreviewUrl(c.toDataURL('image/png')); };
        img.src = src;
      }} />
      {previewUrl && <Image src={previewUrl} style={{ display: 'none' }} preview={{ visible: true, onVisibleChange: v => { if (!v) setPreviewUrl(null); } }} />}
    </>
  );
});

interface ScenarioDetail {
  name: string;
  description: string;
  device_serial: string;
  resolution: { width: number; height: number } | null;
  steps: any[];
  device_map: Record<string, string>;
  created_at: string;
}

interface ROI { x: number; y: number; width: number; height: number; }
interface MatchLocation { x: number; y: number; width: number; height: number; }

interface SubResultData {
  label: string;
  expected_image: string;
  score: number;
  status: string;
  match_location: MatchLocation | null;
}

interface StepResultData {
  step_id: number;
  repeat_index: number;
  // 백엔드가 보내는 실행 단위 고유 ID. 조건부이동으로 같은 step_id를 다시 방문하면
  // 매 실행마다 새 값이 부여되어 dedup이 revisit 행을 누락시키지 않음.
  // 구버전 백엔드와의 호환을 위해 optional.
  exec_seq?: number;
  timestamp: string | null;
  device_id: string;
  command: string;
  description: string;
  status: string;
  excluded_from_result?: boolean;  // 조건부이동 결과 미반영 → Status를 '분기'로 표시
  similarity_score: number | null;
  expected_image: string | null;
  expected_annotated_image: string | null;
  actual_image: string | null;
  actual_annotated_image: string | null;
  diff_image: string | null;
  roi: ROI | null;
  match_location: MatchLocation | null;
  message: string;
  delay_ms: number;
  execution_time_ms: number;
  compare_mode: string | null;
  sub_results: SubResultData[];
}

interface JumpTarget {
  scenario: number;  // group index (0-based), -1 = END
  step: number;      // step index within scenario (0-based)
}

interface StepJump {
  on_pass_goto: JumpTarget | null;
  on_fail_goto: JumpTarget | null;
  exclude_pass_from_result?: boolean;  // 체크 시 pass 결과를 최종 집계에서 제외('분기' 표시)
  exclude_fail_from_result?: boolean;  // 체크 시 fail 결과를 최종 집계에서 제외('분기' 표시)
}

interface GroupEntry {
  name: string;
  on_pass_goto: JumpTarget | null;
  on_fail_goto: JumpTarget | null;
  step_jumps?: Record<string, StepJump>;
  play_count?: number;
}

const statusColor = (s: string) =>
  s === 'pass' ? 'green' : s === 'warning' ? 'orange' : s === 'error' ? 'volcano' : s === 'branch' ? 'purple' : 'red';

// 'branch'(조건부이동 결과 미반영)는 '분기'로, 그 외는 대문자 그대로 표기
const statusLabel = (s: string, t: (k: TranslationKey) => string) =>
  s === 'branch' ? t('results.statusBranch') : s.toUpperCase();

// 결과 미반영 스텝은 status(실제 pass/fail)와 무관하게 '분기'로 표시
const effStatus = (r: { status: string; excluded_from_result?: boolean }) =>
  r.excluded_from_result ? 'branch' : r.status;

// 상세 보기용 — 분기 스텝은 어느 조건(Pass/Fail)으로 분기됐는지까지 표기
const statusDetail = (r: { status: string; excluded_from_result?: boolean }, t: (k: TranslationKey) => string) =>
  r.excluded_from_result ? `${t('results.statusBranch')} (${r.status === 'pass' ? 'PASS' : 'FAIL'})` : statusLabel(r.status, t);

const imageUrl = (path: string | null) => {
  if (!path) return null;
  let rel = path.replace(/\\/g, '/');
  // 런 폴더 내 이미지: results/{timestamp}_{name}/screenshots/... → /results-files/...
  const resultsIdx = rel.indexOf('/results/');
  if (resultsIdx >= 0) return '/results-files/' + rel.substring(resultsIdx + '/results/'.length);
  // 런 폴더 상대 경로 (timestamp_name/screenshots/...)
  if (/^\d{8}_\d{6}_/.test(rel)) return '/results-files/' + rel;
  // 기존 screenshots 폴더
  const ssIdx = rel.indexOf('/screenshots/');
  if (ssIdx >= 0) rel = rel.substring(ssIdx + '/screenshots/'.length);
  return '/screenshots/' + rel;
};

const formatDuration = (ms: number) => {
  if (ms < 1000) return `${ms}ms`;
  const sec = Math.floor(ms / 1000);
  const remain = ms % 1000;
  if (sec < 60) return `${sec}.${String(remain).padStart(3, '0').slice(0, 1)}s`;
  const min = Math.floor(sec / 60);
  const remSec = sec % 60;
  return `${min}m ${remSec}s`;
};

const formatTime = (iso: string, _lang: string = 'ko', inline = false) => {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    const date = d.toLocaleDateString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit' });
    const time = d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    if (inline) return `${date} ${time}`;
    return <>{date}<br />{time}</>;
  } catch { return iso; }
};

// 그룹 아이콘 — "G" 모양 배지 (그룹을 시나리오와 시각적으로 구분)
const GroupIcon = ({ color }: { color?: string }) => (
  <span style={{
    display: 'inline-flex',
    width: 14,
    height: 14,
    alignItems: 'center',
    justifyContent: 'center',
    background: color || '#1677ff',
    color: '#fff',
    borderRadius: 3,
    fontSize: 9,
    fontWeight: 700,
    lineHeight: 1,
    fontFamily: 'Arial, sans-serif',
  }}>G</span>
);

export default function ScenarioPage() {
  const { t, lang } = useTranslation();
  const { settings, saveExportZipToDir } = useSettings();
  const isDark = settings.theme === 'dark';
  const dltSessionHook = useDLTSessions();
  const serialSessionHook = useSerialSessions();
  const logcatSessionHook = useLogcatSessions();
  const { webcam, ensureWebcamOpen } = useWebcamContext();
  const { pauseScreenStream, resumeScreenStream, primaryDevices, auxiliaryDevices } = useDevice();
  // 시나리오 재생 중 DLT/Serial/Logcat 은 라이브 뷰어를 띄우지 않는다 — 로그가 폭주하면(예: logcat
  // 수십만 줄) 렌더링이 메인 스레드를 잡아먹어 PC 가 느려진다. 대신 "현재 logging 중"인 세션을
  // 작은 노티로 **계속 유지** 표시하고, StopLogging(세션 종료) 시 해당 노티를 **닫는다**.
  // → 사용자가 명시적으로 logging 상태를 파악 가능. 활성 세션 목록(sessions)에서 파생하므로
  //   backfill/재연결에도 일관. 로그 파일 저장은 백엔드에서 그대로 수행되므로 영향 없음.
  const _shownLogNotis = useRef<Set<string>>(new Set());
  const _reconcileLogNotis = useCallback((
    prefix: string,
    label: string,
    sessions: Array<{ session_id: string; port?: string | number; serial?: string; host?: string }>,
    addrOf: (s: { session_id: string; port?: string | number; serial?: string; host?: string }) => string,
  ) => {
    const wanted = new Map<string, string>();
    sessions.forEach((s) => {
      const addr = addrOf(s);
      const dev = [...primaryDevices, ...auxiliaryDevices].find((x: any) => x.address === addr);
      wanted.set(`${prefix}:${s.session_id}`, `${dev?.id || addr} — ${label}`);
    });
    // 활성 세션 → 노티 열기/유지 (같은 key 는 in-place 갱신, duration:0 = 자동닫힘 없음)
    wanted.forEach((desc, key) => {
      notification.open({ key, message: '🔴 Logging 중', description: desc, duration: 0, placement: 'bottomRight' });
      _shownLogNotis.current.add(key);
    });
    // 더 이상 활성이 아닌(=StopLogging 된) 세션 노티는 닫기
    Array.from(_shownLogNotis.current).forEach((key) => {
      if (key.startsWith(`${prefix}:`) && !wanted.has(key)) {
        notification.destroy(key);
        _shownLogNotis.current.delete(key);
      }
    });
  }, [primaryDevices, auxiliaryDevices]);

  useEffect(() => {
    _reconcileLogNotis('log-serial', 'Serial', serialSessionHook.sessions,
      (s) => String(s.port || '') || String(s.session_id || '').split('@')[0]);
  }, [serialSessionHook.sessions, _reconcileLogNotis]);
  useEffect(() => {
    _reconcileLogNotis('log-dlt', 'DLT', dltSessionHook.sessions,
      (s) => s.host || String(s.session_id || '').split(':')[0]);
  }, [dltSessionHook.sessions, _reconcileLogNotis]);
  useEffect(() => {
    _reconcileLogNotis('log-logcat', 'Android logcat', logcatSessionHook.sessions,
      (s) => s.serial || s.session_id);
  }, [logcatSessionHook.sessions, _reconcileLogNotis]);
  // 페이지 이탈 시 띄운 logging 노티 정리 (재진입하면 활성 세션에서 다시 표시됨)
  useEffect(() => () => {
    Array.from(_shownLogNotis.current).forEach((key) => notification.destroy(key));
    _shownLogNotis.current.clear();
  }, []);
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [selectedScenario, setSelectedScenario] = useState<ScenarioDetail | null>(null);
  const [detailVisible, setDetailVisible] = useState(false);

  // Playback
  const [playing, setPlaying] = useState(false);
  const [paused, setPaused] = useState(false);
  const [playingName, setPlayingName] = useState('');
  const [_currentStepId, setCurrentStepId] = useState<number | null>(null);
  const [stepResults, setStepResults] = useState<StepResultData[]>([]);
  const [playbackScenario, setPlaybackScenario] = useState<ScenarioDetail | null>(null);
  const [repeatCounts, setRepeatCounts] = useState<Record<string, number>>({});
  const [currentIteration, setCurrentIteration] = useState(1);
  const [totalIterations, setTotalIterations] = useState(1);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  // 트리에서 Ctrl/Shift 다중 선택된 시나리오 이름 목록 (드래그&드롭 다중 이동용).
  // selectedName(단일, 미리보기용)과 분리하여 관리: 다중 선택 시에도 마지막 클릭한 항목으로 미리보기 가능.
  const [multiSelectedNames, setMultiSelectedNames] = useState<string[]>([]);
  const [folders, setFolders] = useState<Record<string, string[]>>({});
  // 그룹 폴더 — { folderName: [groupName, ...] }
  const [groupFolders, setGroupFolders] = useState<Record<string, string[]>>({});
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; type: 'folder' | 'scenario'; name: string } | null>(null);
  const [colWidths, setColWidths] = useState<Record<string, number>>({});
  const getRepeatCount = (name: string) => repeatCounts[name] ?? 1;
  const setRepeatCount = (name: string, val: number) =>
    setRepeatCounts((prev) => ({ ...prev, [name]: val }));

  // 반복 재생 종료 시각 (시나리오/그룹별, ISO string). null이면 횟수만으로 종료.
  // 회차가 시간을 포함하면 그 회차는 완주하고 다음 회차부터 차단됨 (백엔드에서 검사).
  const [untilTimes, setUntilTimes] = useState<Record<string, string | null>>({});
  const getUntilTime = (name: string) => untilTimes[name] ?? null;
  const setUntilTime = (name: string, val: string | null) =>
    setUntilTimes((prev) => ({ ...prev, [name]: val }));

  // 백그라운드 CMD/SSH 폴링 — 활성 task_id를 함께 추적해서 취소 가능
  const bgPollTimers = useRef<ReturnType<typeof setInterval>[]>([]);
  const bgPollTaskIds = useRef<string[]>([]);
  const stopAllBgPolls = (cancelBackend: boolean = true) => {
    bgPollTimers.current.forEach(t => clearInterval(t));
    bgPollTimers.current = [];
    if (cancelBackend) {
      bgPollTaskIds.current.forEach(tid => {
        scenarioApi.cancelCmdTask(tid).catch(() => {});
      });
    }
    bgPollTaskIds.current = [];
  };
  const startBgPolling = (results: StepResultData[]) => {
    stopAllBgPolls(false);
    results.forEach((sr, idx) => {
      const m = sr.message?.match?.(/\[BG_TASK:(bg_\d+)\]/);
      if (!m) return;
      const taskId = m[1];
      // 즉시 실행 중 표시
      setStepResults(prev => {
        const u = [...prev];
        u[idx] = { ...u[idx], message: `⏳ 백그라운드 명령 실행 중...` };
        return u;
      });
      const poll = setInterval(async () => {
        try {
          const r = await scenarioApi.getCmdResult(taskId);
          if (r.data.status === 'running') {
            // 라이브 업데이트: 누적 stdout을 계속 반영 (send_command_stream)
            const liveStdout = r.data.stdout ?? '';
            if (liveStdout) {
              setStepResults(prev => {
                const u = [...prev];
                u[idx] = { ...u[idx], message: liveStdout };
                return u;
              });
            }
            return;
          }
          clearInterval(poll);
          // 서버가 계산한 final_message/final_status 사용
          const finalMsg = r.data.final_message ?? r.data.stdout ?? '';
          const finalStatus = r.data.final_status;
          setStepResults(prev => {
            const u = [...prev];
            const step = u[idx];
            u[idx] = { ...step, message: finalMsg, status: finalStatus ?? step.status };
            return u;
          });
        } catch {
          clearInterval(poll);
          setStepResults(prev => {
            const u = [...prev];
            u[idx] = { ...u[idx], message: `[BG_TASK:${taskId}] 결과 소실` };
            return u;
          });
        }
      }, 500);
      bgPollTimers.current.push(poll);
      bgPollTaskIds.current.push(taskId);
    });
  };

  // 웹캠 자동 녹화
  const [webcamAutoRecord, setWebcamAutoRecord] = useState(true);
  // 재생 직전 어떤 웹캠 index로 녹화할지 선택하는 모달.
  // pickWebcamDevice()가 이 모달을 띄우고 사용자 선택(또는 취소)을 Promise로 반환.
  const [webcamPickerOpen, setWebcamPickerOpen] = useState(false);
  const [webcamPickerDevices, setWebcamPickerDevices] = useState<{ index: number; label: string }[]>([]);
  const [webcamPickerValue, setWebcamPickerValue] = useState<number>(0);
  const webcamPickerResolveRef = useRef<((idx: number | null) => void) | null>(null);

  /** 현재 웹캠 목록을 enumerate하여 1개 이상이면 그대로 사용, 2개 이상이면 모달로 선택 받음.
   *  반환값: 선택된 device_index (null = 사용자 취소 또는 목록 비어있음) */
  const pickWebcamDevice = useCallback(async (): Promise<number | null> => {
    const list = await webcam.listWebcamDevices();
    if (!list || list.length === 0) return null;
    if (list.length === 1) return list[0].index;
    // 기본 선택: 현재 webcamIndex (없으면 첫 항목)
    const defaultIdx = list.find(d => d.index === webcam.webcamIndex)?.index ?? list[0].index;
    setWebcamPickerDevices(list);
    setWebcamPickerValue(defaultIdx);
    setWebcamPickerOpen(true);
    return new Promise<number | null>((resolve) => {
      webcamPickerResolveRef.current = resolve;
    });
  }, [webcam]);
  const webcamBlobsRef = useRef<{ repeatIndex: number; blob: Blob }[]>([]);
  const webcamRecordingActiveRef = useRef(false);
  const playbackScrollRef = useRef<HTMLDivElement>(null);

  // 재생 중 스텝 추가 시 자동 최하단 스크롤
  useEffect(() => {
    if (playing && playbackScrollRef.current) {
      playbackScrollRef.current.scrollTop = playbackScrollRef.current.scrollHeight;
    }
  }, [stepResults, playing]);

  // 시나리오 스텝 미리보기
  const [previewSteps, setPreviewSteps] = useState<any[]>([]);
  const [skipStepIds, setSkipStepIds] = useState<Set<number>>(new Set());
  const selectedNameRef = useRef(selectedName);
  selectedNameRef.current = selectedName;
  // 트리 다중 선택 anchor — Shift 범위 선택의 시작점. 일반 클릭/Ctrl 클릭 시 갱신.
  const selectionAnchorRef = useRef<string | null>(null);

  // 시나리오 선택 시 스텝 로드
  useEffect(() => {
    if (!selectedName) { setPreviewSteps([]); setSkipStepIds(new Set()); return; }
    scenarioApi.get(selectedName).then(res => {
      setPreviewSteps(res.data.steps || []);
      setSkipStepIds(new Set());
    }).catch(() => setPreviewSteps([]));
  }, [selectedName]);

  // Group play
  const [playingGroupName, setPlayingGroupName] = useState<string | null>(null);
  const [currentGroupScenario, setCurrentGroupScenario] = useState('');
  const [groupScenarioIndex, setGroupScenarioIndex] = useState(0);
  const [groupScenarioTotal, setGroupScenarioTotal] = useState(0);

  // Groups
  const [groups, setGroups] = useState<Record<string, GroupEntry[]>>({});
  const [selectedGroup, setSelectedGroup] = useState<string | null>(null);
  const [groupModalVisible, setGroupModalVisible] = useState(false);
  const [newGroupName, setNewGroupName] = useState('');
  const [scenarioStepsCache, setScenarioStepsCache] = useState<Record<string, any[]>>({});
  const [expandedEntries, setExpandedEntries] = useState<Set<string>>(new Set());
  const [groupDrag, setGroupDrag] = useState<{ gName: string; from: number; over: number | null } | null>(null);
  // 그룹 모달 좌측 트리에서 다중 선택된 시나리오들 (드래그 시 일괄 추가)
  const [modalTreeSelected, setModalTreeSelected] = useState<string[]>([]);
  // 우측 그룹 페이지 드롭 호버 중인 그룹 이름 (시각 피드백)
  const [groupDropHover, setGroupDropHover] = useState<string | null>(null);
  // 그룹 모달 트리의 폴더 필터 ('__all__' or 폴더명)
  const [modalTreeFolder, setModalTreeFolder] = useState<string>('__all__');
  const modalTreeAnchorRef = useRef<string | null>(null);
  // 그룹 트리에서 선택되어 우측 상세 패널에 표시 중인 그룹명 (모달용)
  const [selectedGroupForDetail, setSelectedGroupForDetail] = useState<string | null>(null);
  // 메인 페이지의 우측 시나리오상세 위젯에 표시할 그룹명 (그룹 트리 선택 시)
  const [groupShownInDetail, setGroupShownInDetail] = useState<string | null>(null);
  // 그룹 트리 다중 선택 (Ctrl/Shift) — 메인 페이지 / 모달 분리
  const [multiSelectedGroupsMain, setMultiSelectedGroupsMain] = useState<string[]>([]);
  const [multiSelectedGroupsModal, setMultiSelectedGroupsModal] = useState<string[]>([]);
  const groupSelectAnchorMainRef = useRef<string | null>(null);
  const groupSelectAnchorModalRef = useRef<string | null>(null);
  // 메인 페이지 폴더 필터 콤보 — 시나리오 / 그룹 별
  const [mainScenarioFolder, setMainScenarioFolder] = useState<string>('__all__');
  const [mainGroupFolder, setMainGroupFolder] = useState<string>('__all__');
  // 그룹 트리 컨텍스트 메뉴 (gfolder = 그룹 폴더, group = 그룹 자체)
  const [groupCtxMenu, setGroupCtxMenu] = useState<{ x: number; y: number; type: 'gfolder' | 'group'; name: string } | null>(null);

  // Copy
  const [copyName, setCopyName] = useState('');
  const [copyModalVisible, setCopyModalVisible] = useState(false);

  // Rename modal
  const [renameModalVisible, setRenameModalVisible] = useState(false);
  const [renameNewName, setRenameNewName] = useState('');

  // Compare modal
  const [compareStep, setCompareStep] = useState<StepResultData | null>(null);

  // 실시간 duration 카운트
  const stepStartTimeRef = useRef<number>(0);
  const [liveDuration, setLiveDuration] = useState(0);
  const liveDurationRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Device mapping
  const [deviceMapModalVisible, setDeviceMapModalVisible] = useState(false);
  const [deviceMapEditing, setDeviceMapEditing] = useState<Record<string, string>>({});
  const [deviceMapScenarioName, setDeviceMapScenarioName] = useState('');
  const [connectedDevices, setConnectedDevices] = useState<{ id: string; name: string; type: string; status: string }[]>([]);

  // Export / Import
  const [exportModalVisible, setExportModalVisible] = useState(false);
  const [exportSelectedScenarios, setExportSelectedScenarios] = useState<string[]>([]);
  const [exportSelectedGroups, setExportSelectedGroups] = useState<string[]>([]);
  const [exportAll, setExportAll] = useState(false);
  const [exportLoading, setExportLoading] = useState(false);

  const [importModalVisible, setImportModalVisible] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importPreviewData, setImportPreviewData] = useState<{ scenarios: { name: string; conflict: boolean }[]; groups: { name: string; conflict: boolean }[] } | null>(null);
  const [importResolutions, setImportResolutions] = useState<Record<string, { action: string; new_name?: string }>>({});
  const [importLoading, setImportLoading] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  // 재생 상태를 동기적으로 추적 (WS 이벤트 핸들러의 stale closure 대응)
  const playingRef = useRef(false);
  // WS 끊김 시 자동 재연결 타이머
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 수신한 가장 높은 iteration (재연결 replay 시 역행 방지)
  const maxIterationRef = useRef(0);
  // 재생 종료(정상/에러/중단) 시 호출 — 자동 재연결이 다시 살아나지 않도록 ref도 함께 정리
  const endPlaying = () => {
    playingRef.current = false;
    if (reconnectTimerRef.current) { clearTimeout(reconnectTimerRef.current); reconnectTimerRef.current = null; }
    setPlaying(false);
  };

  // --- Filtered scenarios by group ---
  // 그룹 선택 시 그룹 멤버 순서 유지 (scenarios는 알파벳순이므로 filter 대신 map 사용)
  const filteredScenarios = selectedGroup
    ? (groups[selectedGroup] || []).map((m) => m.name).filter((n) => scenarios.includes(n))
    : scenarios;

  // --- Fetches ---
  const fetchScenarios = async () => {
    try {
      const res = await scenarioApi.list();
      setScenarios(res.data.scenarios);
    } catch { message.error(t('scenario.listFailed')); }
  };

  const fetchFolders = async () => {
    try {
      const res = await scenarioApi.getFolders();
      setFolders(res.data.folders || {});
    } catch { /* ignore */ }
  };

  const fetchGroupFolders = async () => {
    try {
      const res = await scenarioApi.getGroupFolders();
      setGroupFolders(res.data.folders || {});
    } catch { /* ignore */ }
  };

  const fetchGroups = async () => {
    try {
      const res = await scenarioApi.getGroups();
      setGroups(res.data.groups);
    } catch { /* ignore */ }
  };

  const fetchScenarioStepsCache = async (names: string[], force = false) => {
    // force=true: 캐시 키 존재 여부와 무관하게 서버에서 다시 받아와 덮어씀.
    // 그룹 관리 모달 열림처럼 "지금 즉시 최신 상태가 필요한" 진입점에서 사용.
    const cache: Record<string, any[]> = { ...scenarioStepsCache };
    const toFetch = force ? names : names.filter((n) => !(n in cache));
    await Promise.all(toFetch.map(async (name) => {
      try {
        const res = await scenarioApi.get(name);
        cache[name] = res.data.steps ?? [];
      } catch { cache[name] = []; }
    }));
    setScenarioStepsCache(cache);
  };

  const formatStepLabel = (step: any, idx: number) => {
    const type = step.type || '';
    const p = step.params || {};
    let detail = '';
    if (type === 'tap') detail = `(${p.x},${p.y})`;
    else if (type === 'long_press' || type === 'hkmc_long_press' || type === 'icas_long_press') detail = `(${p.x},${p.y}) ${p.duration_ms || 1000}ms`;
    else if (type === 'swipe') detail = `(${p.x1},${p.y1})→(${p.x2},${p.y2})`;
    else if (type === 'input_text') detail = `"${p.text || ''}"`;
    else if (type === 'key_event') detail = p.keycode || '';
    else if (type === 'wait') detail = `${p.duration_ms || 1000}ms`;
    else if (type === 'adb_command') detail = p.command || '';
    else if (type === 'serial_command') detail = `"${p.data || ''}"`;
    else if (type === 'module_command') detail = `${p.function}(${p.args ? Object.values(p.args).map((v: any) => `"${v}"`).join(', ') : ''})`;
    const desc = step.description ? ` [${step.description}]` : '';
    return `#${idx + 1} ${type} ${detail}${desc}`;
  };

  useEffect(() => {
    fetchScenarios();
    fetchFolders();
    fetchGroups();
    fetchGroupFolders();

    const onTabChange = (e: Event) => {
      if ((e as CustomEvent).detail === '/scenarios') {
        fetchScenarios();
        fetchFolders();
        fetchGroups();
        fetchGroupFolders();
        if (selectedNameRef.current) {
          scenarioApi.get(selectedNameRef.current).then(res => {
            setPreviewSteps(res.data.steps || []);
          }).catch(() => {});
        }
      }
    };
    window.addEventListener('tab-change', onTabChange);
    return () => { if (wsRef.current) wsRef.current.close(); window.removeEventListener('tab-change', onTabChange); stopAllBgPolls(); };
  }, []);

  // --- Scenario CRUD ---
  const viewScenario = async (name: string) => {
    try {
      const res = await scenarioApi.get(name);
      setSelectedScenario(res.data);
      setDetailVisible(true);
    } catch { message.error(t('scenario.loadFailed')); }
  };

  const deleteScenario = async (name: string) => {
    Modal.confirm({
      title: t('scenario.deleteTitle'),
      content: t('scenario.deleteConfirm', { name }),
      onOk: async () => {
        try {
          await scenarioApi.delete(name);
          message.success(t('common.deleteComplete'));
          if (selectedName === name) setSelectedName(null);
          setMultiSelectedNames(prev => prev.filter(n => n !== name));
          fetchScenarios();
          fetchGroups();
        } catch { message.error(t('common.deleteFailed')); }
      },
    });
  };

  // --- Rename ---
  const openRenameModal = () => {
    if (!selectedName) return;
    setRenameNewName(selectedName);
    setRenameModalVisible(true);
  };

  const doRename = async () => {
    if (!selectedName || !renameNewName.trim() || renameNewName.trim() === selectedName) {
      setRenameModalVisible(false);
      return;
    }
    try {
      const oldName = selectedName;
      const newName = renameNewName.trim();
      await scenarioApi.rename(oldName, newName);
      message.success(t('scenario.renameSuccess'));
      setRenameModalVisible(false);
      setSelectedName(newName);
      setMultiSelectedNames(prev => prev.map(n => n === oldName ? newName : n));
      // 백엔드가 그룹/폴더 내 시나리오 참조도 새 이름으로 갱신하므로 함께 리페치
      fetchScenarios();
      fetchFolders();
      fetchGroups();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('scenario.renameFailed'));
    }
  };

  // --- Copy ---
  const openCopyModal = () => {
    if (!selectedName) return;
    setCopyName(selectedName + '_copy');
    setCopyModalVisible(true);
  };

  const doCopy = async () => {
    if (!selectedName || !copyName.trim()) return;
    try {
      await scenarioApi.copy(selectedName, copyName.trim());
      message.success(t('scenario.copySuccess'));
      setCopyModalVisible(false);
      fetchScenarios();
    } catch (e: any) { message.error(e?.response?.data?.detail || t('scenario.copyFailed')); }
  };

  // --- Export / Import ---
  const doExport = async () => {
    setExportLoading(true);
    try {
      // Try server-side save first if path is configured
      try {
        const path = await saveExportZipToDir(
          exportAll ? [] : exportSelectedScenarios,
          exportAll ? [] : exportSelectedGroups,
          exportAll,
        );
        setExportModalVisible(false);
        message.success(t('scenario.exportSaveComplete', { path }));
        setExportLoading(false);
        return;
      } catch (serverErr: any) {
        const status = serverErr.response?.status;
        if (status !== 400) {
          const detail = serverErr.response?.data?.detail || serverErr.message || String(serverErr);
          message.error(t('scenario.exportSaveFailed', { detail }));
          setExportLoading(false);
          return;
        }
        // 400 = path not configured, fallback to browser download
      }

      const res = await scenarioApi.exportZip(
        exportAll ? [] : exportSelectedScenarios,
        exportAll ? [] : exportSelectedGroups,
        exportAll,
      );
      const url = window.URL.createObjectURL(new Blob([res.data]));
      const a = document.createElement('a');
      a.href = url;
      const disposition = res.headers['content-disposition'] || '';
      const match = disposition.match(/filename="?(.+?)"?$/);
      a.download = match ? match[1] : 'recording_export.zip';
      a.click();
      window.URL.revokeObjectURL(url);
      setExportModalVisible(false);
      message.success(t('scenario.exportComplete'));
    } catch { message.error(t('scenario.exportFailed')); }
    setExportLoading(false);
  };

  const handleImportFile = async (file: File) => {
    setImportFile(file);
    setImportResolutions({});
    setImportPreviewData(null);
    setImportLoading(true);
    try {
      const res = await scenarioApi.importPreview(file);
      setImportPreviewData(res.data);
      // Set default resolutions
      const defaults: Record<string, { action: string; new_name?: string }> = {};
      for (const s of res.data.scenarios) {
        defaults[`s:${s.name}`] = { action: s.conflict ? 'skip' : 'import' };
      }
      for (const g of res.data.groups) {
        defaults[`g:${g.name}`] = { action: g.conflict ? 'skip' : 'import' };
      }
      setImportResolutions(defaults);
    } catch { message.error(t('scenario.importFailed')); }
    setImportLoading(false);
  };

  const doImport = async () => {
    if (!importFile || !importPreviewData) return;
    setImportLoading(true);
    try {
      const scenarioRes: Record<string, any> = {};
      const groupRes: Record<string, any> = {};
      for (const s of importPreviewData.scenarios) {
        const r = importResolutions[`s:${s.name}`] || { action: 'import' };
        scenarioRes[s.name] = r;
      }
      for (const g of importPreviewData.groups) {
        const r = importResolutions[`g:${g.name}`] || { action: 'import' };
        groupRes[g.name] = r;
      }
      const res = await scenarioApi.importApply(importFile, { scenarios: scenarioRes, groups: groupRes });
      const d = res.data;
      message.success(t('scenario.importComplete', { scenarios: String(d.imported_scenarios?.length || 0), groups: String(d.imported_groups?.length || 0) }));
      setImportModalVisible(false);
      setImportFile(null);
      setImportPreviewData(null);
      fetchScenarios();
      fetchGroups();
    } catch { message.error(t('scenario.importFailed')); }
    setImportLoading(false);
  };

  // --- Group actions ---
  const createGroup = async () => {
    if (!newGroupName.trim()) return;
    try {
      const res = await scenarioApi.createGroup(newGroupName.trim());
      setGroups(res.data.groups);
      setNewGroupName('');
      message.success(t('scenario.groupCreateSuccess'));
    } catch (e: any) { message.error(e?.response?.data?.detail || t('scenario.groupCreateFailed')); }
  };

  const deleteGroup = (gName: string) => {
    Modal.confirm({
      title: t('scenario.groupDeleteConfirm', { name: gName }),
      okText: t('common.delete'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          const res = await scenarioApi.deleteGroup(gName);
          setGroups(res.data.groups);
          if (selectedGroup === gName) setSelectedGroup(null);
          if (groupShownInDetail === gName) setGroupShownInDetail(null);
          // 그룹 폴더에서 stale 항목 자동 정리 (백엔드 get_group_folders가 prune)
          fetchGroupFolders();
          message.success(t('scenario.groupDeleteSuccess'));
        } catch { message.error(t('scenario.groupDeleteFailed')); }
      },
    });
  };

  const addToGroup = async (gName: string, sName: string) => {
    try {
      const res = await scenarioApi.addToGroup(gName, sName);
      setGroups(res.data.groups);
      fetchScenarioStepsCache([sName]);
    } catch { message.error(t('scenario.groupAddFailed')); }
  };

  const removeFromGroup = async (gName: string, index: number) => {
    try {
      const res = await scenarioApi.removeFromGroup(gName, index);
      setGroups(res.data.groups);
    } catch { message.error(t('scenario.groupRemoveFailed')); }
  };

  const reorderGroup = async (gName: string, orderedIndices: number[]) => {
    try {
      const res = await scenarioApi.reorderGroup(gName, orderedIndices);
      setGroups(res.data.groups);
    } catch { message.error(t('scenario.reorderFailed')); }
  };

  const dropInGroup = (gName: string, members: GroupEntry[], from: number, to: number) => {
    if (from === to || from < 0 || to < 0 || from >= members.length || to >= members.length) return;
    const perm = members.map((_, i) => i);
    const [moved] = perm.splice(from, 1);
    perm.splice(to, 0, moved);
    reorderGroup(gName, perm);
  };

  const updateGroupStepJumps = async (gName: string, entryIdx: number, stepId: number, on_pass_goto: JumpTarget | null, on_fail_goto: JumpTarget | null, exclude_pass_from_result = false, exclude_fail_from_result = false) => {
    try {
      const res = await scenarioApi.updateGroupStepJumps(gName, entryIdx, stepId, on_pass_goto, on_fail_goto, exclude_pass_from_result, exclude_fail_from_result);
      setGroups(res.data.groups);
    } catch { message.error(t('scenario.stepJumpFailed')); }
  };

  const updateGroupPlayCount = async (gName: string, entryIdx: number, playCount: number) => {
    const pc = Math.max(1, Math.min(999, Math.floor(Number(playCount) || 1)));
    try {
      const res = await scenarioApi.updateGroupPlayCount(gName, entryIdx, pc);
      setGroups(res.data.groups);
    } catch { message.error(t('common.saveFailed') || t('scenario.stepJumpFailed')); }
  };

  const toggleExpandEntry = (key: string) => {
    setExpandedEntries((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // 전체화면(full) 비교 모드를 사용 중이고 기대이미지까지 설정된 스텝의 id 목록.
  // compare_mode 의 기본값이 'full' 이라 단순 full 검출은 noise 가 많음 — expected_image 까지 있어야
  // 실제 비교가 일어남(=영향 받음).
  const findDeprecatedFullSteps = (steps: any[]): number[] => {
    const out: number[] = [];
    for (const s of (steps || [])) {
      if (!s) continue;
      const mode = s.compare_mode || 'full';
      if (mode === 'full' && s.expected_image) out.push(Number(s.id ?? 0));
    }
    return out;
  };

  // 전체화면 비교 deprecation 차단. 해당 스텝이 있으면 안내 모달 후 재생 차단(false 반환).
  // 사용자는 RecordPage에서 비교 모드를 다른 것으로 수정한 뒤 다시 재생해야 함.
  const confirmFullCompareDeprecation = (label: string, stepIds: number[]): Promise<boolean> => {
    if (stepIds.length === 0) return Promise.resolve(true);
    return new Promise<boolean>(resolve => {
      Modal.warning({
        title: t('record.fullScreenDeprecatedTitle'),
        content: (
          <div>
            <p>{t('record.fullScreenDeprecated')}</p>
            <p style={{ color: '#888', fontSize: 12, marginTop: 8 }}>
              {label}: step #{stepIds.slice(0, 20).join(', #')}{stepIds.length > 20 ? ` (+${stepIds.length - 20})` : ''}
            </p>
          </div>
        ),
        okText: t('common.close'),
        onOk: () => resolve(false),
      });
    });
  };

  // --- Playback ---
  const playScenario = async (name: string) => {
    let scenarioData: ScenarioDetail;
    try {
      const res = await scenarioApi.get(name);
      scenarioData = res.data;
      setPlaybackScenario(scenarioData);
    } catch { message.error(t('scenario.loadFailed')); return; }

    // 전체화면 비교(deprecated) 사용 검출 — 있으면 안내 후 사용자 선택에 따라 진행/취소.
    const fullStepIds = findDeprecatedFullSteps(scenarioData.steps);
    const proceed = await confirmFullCompareDeprecation(name, fullStepIds);
    if (!proceed) return;

    // 재생 확인 모달 표시 (디바이스 매핑 + 웹캠 녹화 설정)
    const dmap = scenarioData.device_map || {};
    // DeviceContext에서 이미 폴링된 디바이스 목록 사용 (API 재호출 불필요)
    // 연결된 디바이스만 표시 — 연결 안 된 디바이스는 드롭다운에서 제외
    const devices = [
      ...primaryDevices.map((d: any) => ({ id: d.id, name: d.name || d.id, type: d.type, status: d.status, address: d.address })),
      ...auxiliaryDevices.map((d: any) => ({ id: d.id, name: d.name || d.id, type: d.type, status: d.status, address: d.address })),
    ].filter(d => d.status === 'device' || d.status === 'connected');
    setConnectedDevices(devices);
    // 매핑 대상 alias 수집: step.device_id ∪ 저장된 device_map 키.
    // 저장된 device_map만 보면 step에서 쓰는 alias가 빠질 수 있어 모달이 비게 되고,
    // preflight 실패 후 "Change Device Map" 으로 모달을 열어도 변경할 항목이 없어진다.
    const aliases = new Set<string>();
    for (const s of (scenarioData.steps || [])) {
      if (s && s.device_id) aliases.add(String(s.device_id));
    }
    for (const k of Object.keys(dmap)) aliases.add(k);
    // 시나리오의 매핑값(이전 환경 ID)을 현재 디바이스 ID로 자동 매칭
    const resolved: Record<string, string> = {};
    for (const alias of aliases) {
      const savedId = dmap[alias];
      if (savedId) {
        const exact = devices.find(d => d.id === savedId);
        if (exact) {
          resolved[alias] = savedId;
          continue;
        }
      }
      // 저장값이 없거나 매칭 실패 → alias 동명 디바이스 우선, 없으면 alias 자체(미해결로 표시).
      const byAlias = devices.find(d => d.id === alias);
      resolved[alias] = byAlias ? byAlias.id : (savedId || alias);
    }
    setDeviceMapEditing(resolved);
    setDeviceMapScenarioName(name);
    setDeviceMapModalVisible(true);
  };

  const startPlayback = async (name: string, deviceMap: Record<string, string>) => {
    // 절전 모드 확인
    try {
      const ps = await serverApi.powerStatus();
      if (ps.data.warning) {
        // 'block' = 절전 차단 후 재생, 'cancel' = 취소
        const choice = await new Promise<'block' | 'cancel'>(resolve => {
          const modal = Modal.confirm({
            title: t('scenario.sleepWarningTitle'),
            content: (
              <div>
                <p>{ps.data.warning}</p>
                <p style={{ color: '#888', fontSize: 11 }}>{t('scenario.sleepBlockDesc')}</p>
              </div>
            ),
            okText: t('scenario.sleepBlock'),
            cancelText: t('common.cancel'),
            onOk: () => resolve('block'),
            onCancel: () => resolve('cancel'),
          });
          void modal;
        });
        if (choice === 'cancel') return;
        // 'block' → playback_service가 SetThreadExecutionState로 자동 차단
      }
    } catch { /* 조회 실패 시 무시 */ }

    pauseScreenStream();
    const repeat = getRepeatCount(name);
    const untilTime = getUntilTime(name);
    // 웹캠 자동녹화: 복수 웹캠이 있으면 사용자에게 index 선택 받기 + 웹캠 열기 + 연결 확인
    let doAutoRecord = false;
    if (webcamAutoRecord) {
      const pickedIdx = await pickWebcamDevice();
      if (pickedIdx === null) {
        message.error(t('webcam.webcamNotOpen'));
        return;
      }
      const ready = await ensureWebcamOpen(pickedIdx);
      if (!ready) {
        message.error(t('webcam.webcamNotOpen'));
        return;
      }
      doAutoRecord = true;
    }
    playingRef.current = true;
    setPlaying(true);
    setPlayingName(name);
    setStepResults([]);
    setCurrentStepId(null);
    setCurrentIteration(1);
    // until_time 모드는 총 회차 미정 → 0(=무한 표시). 일반 모드는 repeat 그대로.
    setTotalIterations(untilTime ? 0 : repeat);
    maxIterationRef.current = 0;
    webcamBlobsRef.current = [];
    webcamRecordingActiveRef.current = false;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const hasMap = Object.keys(deviceMap).length > 0;
    const skipIds = Array.from(skipStepIds);

    // WS 연결 생성. isReconnect=true면 play를 보내지 않고 subscribe만 — 백엔드가 버퍼 replay.
    const openPlaybackWS = (isReconnect: boolean) => {
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws/playback`);
      wsRef.current = ws;
      ws.onopen = () => {
        if (isReconnect) {
          // 재연결: 버퍼 replay는 서버에서 자동 수행되므로 play 재발행 금지
          ws.send(JSON.stringify({ action: 'subscribe' }));
        } else {
          setCurrentStepId(1);
          ws.send(JSON.stringify({ action: 'play', scenario: name, verify: true, repeat, ...(hasMap ? { device_map: deviceMap } : {}), ...(skipIds.length > 0 ? { skip_steps: skipIds } : {}), ...(untilTime ? { until_time: untilTime } : {}) }));
        }
      };
      ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'iteration_start') {
        // 재연결 시 buffer replay로 과거 iteration_start가 다시 올 수 있음 → 최고값 기반으로 스킵
        if (msg.iteration <= maxIterationRef.current) return;
        maxIterationRef.current = msg.iteration;
        setCurrentIteration(msg.iteration);
        // 회차별 웹캠 녹화 분리
        if (doAutoRecord && webcamRecordingActiveRef.current && msg.iteration > 1) {
          webcam.stopRecordingAuto().then((blob) => {
            webcamBlobsRef.current.push({ repeatIndex: msg.iteration - 1, blob });
            webcam.startRecordingAuto().then((ok) => { webcamRecordingActiveRef.current = ok; });
          });
        }
      } else if (msg.type === 'step_start') {
        // 첫 스텝 시작 = 디바이스 검사 통과 → 웹캠 녹화 시작
        if (doAutoRecord && !webcamRecordingActiveRef.current) {
          webcam.startRecordingAuto().then((ok) => { webcamRecordingActiveRef.current = ok; });
        }
        // 스텝 시작: running 상태로 테이블에 추가 + duration 카운트 시작
        const d = msg.data;
        const placeholder: StepResultData = {
          step_id: d.step_id, repeat_index: d.repeat_index,
          exec_seq: d.exec_seq,
          timestamp: new Date().toISOString(), device_id: d.device_id,
          command: d.command, description: d.description,
          status: 'running', similarity_score: null,
          expected_image: null, expected_annotated_image: null,
          actual_image: null, actual_annotated_image: null, diff_image: null,
          roi: null, match_location: null, message: '',
          delay_ms: d.delay_ms, execution_time_ms: 0,
          compare_mode: null, sub_results: [],
        };
        // dedup 키: exec_seq(실행 단위 고유 ID)가 있으면 그걸로, 없으면 구버전 호환 키.
        // 조건부이동 revisit은 새 exec_seq를 받으므로 새 행이 append됨.
        // 버퍼 replay는 동일 exec_seq를 다시 보내므로 정확히 매칭되어 skip.
        setStepResults((prev) => {
          if (d.exec_seq !== undefined) {
            for (let i = prev.length - 1; i >= 0; i--) {
              if (prev[i].exec_seq === d.exec_seq) return prev;
            }
            return [...prev, placeholder];
          }
          for (let i = prev.length - 1; i >= 0; i--) {
            if (prev[i].step_id === d.step_id && prev[i].repeat_index === d.repeat_index) return prev;
          }
          return [...prev, placeholder];
        });
        setCurrentStepId(d.step_id);
        // 실시간 카운터
        stepStartTimeRef.current = Date.now();
        setLiveDuration(0);
        if (liveDurationRef.current) clearInterval(liveDurationRef.current);
        liveDurationRef.current = setInterval(() => {
          setLiveDuration(Date.now() - stepStartTimeRef.current);
        }, 200);
      } else if (msg.type === 'step_result') {
        // 스텝 완료: 마지막 running 행을 실제 결과로 교체
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        const result: StepResultData = msg.data;
        setStepResults((prev) => {
          let idx = -1;
          // exec_seq가 있으면 그걸로 매칭(조건부이동 revisit 행을 정확히 찍음), 없으면 구버전 호환
          if (result.exec_seq !== undefined) {
            for (let i = prev.length - 1; i >= 0; i--) {
              if (prev[i].exec_seq === result.exec_seq) { idx = i; break; }
            }
          } else {
            for (let i = prev.length - 1; i >= 0; i--) { if (prev[i].step_id === result.step_id && prev[i].repeat_index === result.repeat_index) { idx = i; break; } }
          }
          if (idx >= 0) {
            const updated = [...prev];
            // exec_seq는 placeholder의 것을 보존(서버가 sr_data에 같은 값을 넣어 보내므로 사실상 동일)
            updated[idx] = { ...result, exec_seq: result.exec_seq ?? prev[idx].exec_seq };
            return updated;
          }
          return [...prev, result];
        });
      } else if (msg.type === 'wait_progress') {
        // Wait step의 주기적 progress — 마지막 running 행의 메시지에 표시
        const stepId = msg.step_id;
        const elapsedS = Math.floor((msg.elapsed_ms || 0) / 1000);
        const totalS = Math.floor((msg.total_ms || 0) / 1000);
        setStepResults((prev) => {
          const u = [...prev];
          for (let i = u.length - 1; i >= 0; i--) {
            if (u[i].step_id === stepId && u[i].status === 'running') {
              u[i] = { ...u[i], message: `⏳ ${elapsedS}s / ${totalS}s` };
              break;
            }
          }
          return u;
        });
      } else if (msg.type === 'playback_reset') {
        // 새 재생 시작 시 상태 초기화 (재연결 시에는 수신 안 함)
        setStepResults([]);
        setCurrentStepId(null);
      } else if (msg.type === 'until_time_reached') {
        // 지정 시각 도달 — 현재 회차까지 완주 후 종료. playback_complete가 곧 따라온다.
        message.info(t('scenario.untilTimeReached', { iteration: String(msg.iteration ?? '') }));
      } else if (msg.type === 'playback_complete') {
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        endPlaying(); setPaused(false); setCurrentStepId(null); resumeScreenStream();
        message.success(repeat > 1 ? t('scenario.playCompleteRepeat', { count: String(repeat) }) : t('scenario.playComplete'));
        ws.close();
        // 백그라운드 CMD 결과 폴링 시작
        setStepResults(prev => { startBgPolling(prev); return prev; });
        // 웹캠 녹화 종료 + 업로드
        if (doAutoRecord && webcamRecordingActiveRef.current) {
          const resultFilename = msg.result_filename || '';
          webcam.stopRecordingAuto().then(async (blob) => {
            const allBlobs = [...webcamBlobsRef.current, { repeatIndex: repeat > 1 ? repeat : 1, blob }];
            webcamRecordingActiveRef.current = false;
            if (resultFilename) {
              for (const item of allBlobs) {
                if (item.blob.size < 100) continue;
                try { await resultsApi.uploadRecording(item.blob, resultFilename, item.repeatIndex); } catch { message.error(t('webcam.uploadFailed')); }
              }
            }
            webcamBlobsRef.current = [];
          });
        }
      } else if (msg.type === 'preflight_error') {
        endPlaying(); setCurrentStepId(null);
        Modal.confirm({
          title: t('scenario.deviceCheckFailed'),
          content: (
            <div>
              <div style={{ maxHeight: 200, overflowY: 'auto', marginBottom: 10 }}>
                {(msg.errors || []).map((e: string, i: number) => (
                  <div key={i} style={{ padding: '4px 0', color: '#ff4d4f' }}>• {e}</div>
                ))}
              </div>
              <div style={{ color: '#888', fontSize: 11 }}>{t('scenario.preflightSwapHint')}</div>
            </div>
          ),
          okText: t('scenario.changeDeviceMap'),
          cancelText: t('common.close'),
          onOk: () => {
            // device_map 편집 모달 다시 열기
            setDeviceMapModalVisible(true);
          },
        });
        ws.close();
      } else if (msg.type === 'error') {
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        endPlaying(); setCurrentStepId(null); resumeScreenStream();
        message.error(msg.message); ws.close();
        if (doAutoRecord && webcamRecordingActiveRef.current) { webcam.stopRecordingAuto(); webcamRecordingActiveRef.current = false; webcamBlobsRef.current = []; }
      } else if (msg.type === 'playback_stopped') {
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        endPlaying(); setPaused(false); setCurrentStepId(null); resumeScreenStream();
        const resultFilename = msg.result_filename || '';
        if (resultFilename) {
          message.info(t('scenario.playStoppedPartial'));
          // 완료된 회차까지 웹캠 녹화 저장
          if (doAutoRecord && webcamRecordingActiveRef.current) {
            webcam.stopRecordingAuto().then(async () => {
              webcamRecordingActiveRef.current = false;
              if (webcamBlobsRef.current.length > 0) {
                for (const item of webcamBlobsRef.current) {
                  try { await resultsApi.uploadRecording(item.blob, resultFilename, item.repeatIndex); } catch { /* ignore */ }
                }
              }
              webcamBlobsRef.current = [];
            });
          }
        } else {
          message.info(t('scenario.playStopped'));
          if (doAutoRecord && webcamRecordingActiveRef.current) { webcam.stopRecordingAuto(); webcamRecordingActiveRef.current = false; webcamBlobsRef.current = []; }
        }
        ws.close();
      } else if (msg.type === 'playback_paused') {
        setPaused(true);
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        if (doAutoRecord && webcamRecordingActiveRef.current) webcam.pauseRecording();
        pauseScreenStream();
      } else if (msg.type === 'playback_resumed') {
        setPaused(false);
        if (doAutoRecord && webcamRecordingActiveRef.current) webcam.resumeRecording();
        resumeScreenStream();
      }
    };
      ws.onerror = () => {
        // onerror 자체는 연결 실패를 의미하지만, 뒤이어 onclose가 항상 호출되므로
        // 여기서는 상태 정리만 하고 재연결은 onclose에 맡긴다.
        if (!playingRef.current && liveDurationRef.current) {
          clearInterval(liveDurationRef.current);
          liveDurationRef.current = null;
        }
      };
      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        // 재생 중에 WS가 예기치 않게 끊긴 경우 — 2초 후 자동 재연결
        // (정상 종료 시에는 playback_complete/stopped/error 핸들러에서 endPlaying()이 먼저 호출되어 playingRef가 false)
        if (playingRef.current) {
          if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            if (playingRef.current) openPlaybackWS(true);
          }, 2000);
        }
      };
    };
    openPlaybackWS(false);
  };

  // --- Group playback ---
  const playGroup = async (gName: string) => {
    const members = groups[gName] || [];
    if (members.length === 0) { message.warning(t('scenario.noScenariosInGroup')); return; }

    // 멤버 시나리오들의 device_map 합집합 + steps 의 device_id 합집합을 alias 셋으로 사용.
    // 저장된 device_map만 합치면 step에서 새로 도입된 alias가 모달에서 누락되어
    // 사용자가 매핑 변경을 할 수 없게 된다.
    // 동시에 deprecated full 비교 사용도 같은 루프에서 수집 — 추가 API 호출 절약.
    const mergedMap: Record<string, string> = {};
    const aliases = new Set<string>();
    const allFullStepIds: number[] = [];
    for (const m of members) {
      try {
        const res = await scenarioApi.get(m.name);
        const dmap = res.data.device_map || {};
        Object.assign(mergedMap, dmap);
        for (const k of Object.keys(dmap)) aliases.add(k);
        for (const s of (res.data.steps || [])) {
          if (s && s.device_id) aliases.add(String(s.device_id));
        }
        const fullIds = findDeprecatedFullSteps(res.data.steps || []);
        for (const id of fullIds) allFullStepIds.push(id);
      } catch { /* ignore */ }
    }

    // 전체화면 비교(deprecated) 사용 검출 — 있으면 안내 후 사용자 선택에 따라 진행/취소.
    const proceed = await confirmFullCompareDeprecation(`group:${gName}`, allFullStepIds);
    if (!proceed) return;

    let devices: { id: string; name: string; type: string; status: string; address?: string }[] = [];
    try {
      const devRes = await deviceApi.list();
      devices = [
        ...(devRes.data.primary || []).map((d: any) => ({ id: d.id, name: d.name || d.id, type: d.type, status: d.status, address: d.address })),
        ...(devRes.data.auxiliary || []).map((d: any) => ({ id: d.id, name: d.name || d.id, type: d.type, status: d.status, address: d.address })),
      ].filter(d => d.status === 'device' || d.status === 'connected');
      setConnectedDevices(devices);
    } catch { /* ignore */ }
    const resolved: Record<string, string> = {};
    for (const alias of aliases) {
      const savedId = mergedMap[alias];
      if (savedId) {
        const exact = devices.find(d => d.id === savedId);
        if (exact) {
          resolved[alias] = savedId;
          continue;
        }
      }
      const byAlias = devices.find(d => d.id === alias);
      resolved[alias] = byAlias ? byAlias.id : (savedId || alias);
    }
    setDeviceMapEditing(resolved);
    setDeviceMapScenarioName(`group:${gName}`);
    setDeviceMapModalVisible(true);
  };

  const startGroupPlayback = async (gName: string, deviceMap: Record<string, string>) => {
    pauseScreenStream();
    const members = groups[gName] || [];
    const repeat = getRepeatCount(gName);
    const untilTime = getUntilTime(gName);
    // 웹캠 자동녹화: 복수 웹캠이 있으면 사용자에게 index 선택 받기 + 웹캠 열기 + 연결 확인
    let doAutoRecord = false;
    if (webcamAutoRecord) {
      const pickedIdx = await pickWebcamDevice();
      if (pickedIdx === null) {
        message.error(t('webcam.webcamNotOpen'));
        return;
      }
      const ready = await ensureWebcamOpen(pickedIdx);
      if (!ready) {
        message.error(t('webcam.webcamNotOpen'));
        return;
      }
      doAutoRecord = true;
    }

    playingRef.current = true;
    setPlaying(true);
    setPlayingGroupName(gName);
    setPlayingName(members[0].name);
    setPlaybackScenario({ name: gName, description: '', device_serial: '', resolution: null, steps: [], device_map: {}, created_at: '' });
    setStepResults([]);
    setCurrentStepId(null);
    setCurrentIteration(1);
    setTotalIterations(untilTime ? 0 : repeat);
    maxIterationRef.current = 0;
    setGroupScenarioIndex(0);
    setGroupScenarioTotal(members.length);
    setCurrentGroupScenario('');
    webcamBlobsRef.current = [];
    webcamRecordingActiveRef.current = false;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const hasMap = Object.keys(deviceMap).length > 0;

    const openPlaybackWS = (isReconnect: boolean) => {
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws/playback`);
      wsRef.current = ws;
      ws.onopen = () => {
        if (isReconnect) {
          ws.send(JSON.stringify({ action: 'subscribe' }));
        } else {
          ws.send(JSON.stringify({ action: 'play_group', group_name: gName, scenarios: members, verify: true, repeat, ...(hasMap ? { device_map: deviceMap } : {}), ...(untilTime ? { until_time: untilTime } : {}) }));
        }
      };
      ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'group_scenario_start') {
        setCurrentGroupScenario(msg.scenario_name);
        setGroupScenarioIndex(msg.scenario_index);
        setGroupScenarioTotal(msg.total_scenarios);
        setPlayingName(msg.scenario_name);
      } else if (msg.type === 'iteration_start') {
        // 재연결 replay 시 과거 iteration 스킵
        if (msg.iteration <= maxIterationRef.current) return;
        maxIterationRef.current = msg.iteration;
        setCurrentIteration(msg.iteration);
        // 회차별 웹캠 녹화 분리
        if (doAutoRecord && webcamRecordingActiveRef.current && msg.iteration > 1) {
          webcam.stopRecordingAuto().then((blob) => {
            webcamBlobsRef.current.push({ repeatIndex: msg.iteration - 1, blob });
            webcam.startRecordingAuto().then((ok) => { webcamRecordingActiveRef.current = ok; });
          });
        }
      } else if (msg.type === 'step_start') {
        // 첫 스텝 시작 = 디바이스 검사 통과 → 웹캠 녹화 시작
        if (doAutoRecord && !webcamRecordingActiveRef.current) {
          webcam.startRecordingAuto().then((ok) => { webcamRecordingActiveRef.current = ok; });
        }
        const d = msg.data;
        const placeholder: StepResultData = {
          step_id: d.step_id, repeat_index: d.repeat_index,
          exec_seq: d.exec_seq,
          timestamp: new Date().toISOString(), device_id: d.device_id,
          command: d.command, description: d.description,
          status: 'running', similarity_score: null,
          expected_image: null, expected_annotated_image: null,
          actual_image: null, actual_annotated_image: null, diff_image: null,
          roi: null, match_location: null, message: '',
          delay_ms: d.delay_ms, execution_time_ms: 0,
          compare_mode: null, sub_results: [],
        };
        // dedup 키: exec_seq(실행 단위 고유 ID)가 있으면 그걸로, 없으면 구버전 호환 키.
        // 그룹/조건부이동 revisit 모두 새 exec_seq를 받음 → 행이 누락되지 않음.
        setStepResults((prev) => {
          if (d.exec_seq !== undefined) {
            for (let i = prev.length - 1; i >= 0; i--) {
              if (prev[i].exec_seq === d.exec_seq) return prev;
            }
            return [...prev, placeholder];
          }
          for (let i = prev.length - 1; i >= 0; i--) {
            if (prev[i].step_id === d.step_id && prev[i].repeat_index === d.repeat_index) return prev;
          }
          return [...prev, placeholder];
        });
        setCurrentStepId(d.step_id);
        stepStartTimeRef.current = Date.now();
        setLiveDuration(0);
        if (liveDurationRef.current) clearInterval(liveDurationRef.current);
        liveDurationRef.current = setInterval(() => {
          setLiveDuration(Date.now() - stepStartTimeRef.current);
        }, 200);
      } else if (msg.type === 'step_result') {
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        const result: StepResultData = msg.data;
        setStepResults((prev) => {
          let idx = -1;
          if (result.exec_seq !== undefined) {
            for (let i = prev.length - 1; i >= 0; i--) {
              if (prev[i].exec_seq === result.exec_seq) { idx = i; break; }
            }
          } else {
            for (let i = prev.length - 1; i >= 0; i--) { if (prev[i].step_id === result.step_id && prev[i].repeat_index === result.repeat_index) { idx = i; break; } }
          }
          if (idx >= 0) {
            const updated = [...prev];
            updated[idx] = { ...result, exec_seq: result.exec_seq ?? prev[idx].exec_seq };
            return updated;
          }
          return [...prev, result];
        });
      } else if (msg.type === 'wait_progress') {
        // Wait step의 주기적 progress — 마지막 running 행의 메시지에 표시
        const stepId = msg.step_id;
        const elapsedS = Math.floor((msg.elapsed_ms || 0) / 1000);
        const totalS = Math.floor((msg.total_ms || 0) / 1000);
        setStepResults((prev) => {
          const u = [...prev];
          for (let i = u.length - 1; i >= 0; i--) {
            if (u[i].step_id === stepId && u[i].status === 'running') {
              u[i] = { ...u[i], message: `⏳ ${elapsedS}s / ${totalS}s` };
              break;
            }
          }
          return u;
        });
      } else if (msg.type === 'playback_reset') {
        setStepResults([]);
        setCurrentStepId(null);
      } else if (msg.type === 'until_time_reached') {
        // 지정 시각 도달 — 현재 회차까지 완주 후 종료
        message.info(t('scenario.untilTimeReached', { iteration: String(msg.iteration ?? '') }));
      } else if (msg.type === 'playback_complete') {
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        endPlaying(); setPaused(false); setPlayingGroupName(null); setCurrentStepId(null); resumeScreenStream();
        message.success(t('scenario.playComplete'));
        ws.close();
        // 백그라운드 CMD 결과 폴링 시작
        setStepResults(prev => { startBgPolling(prev); return prev; });
        if (doAutoRecord && webcamRecordingActiveRef.current) {
          const resultFilename = msg.result_filename || '';
          webcam.stopRecordingAuto().then(async (blob) => {
            const allBlobs = [...webcamBlobsRef.current, { repeatIndex: repeat > 1 ? repeat : 1, blob }];
            webcamRecordingActiveRef.current = false;
            if (resultFilename) {
              for (const item of allBlobs) {
                if (item.blob.size < 100) continue;
                try { await resultsApi.uploadRecording(item.blob, resultFilename, item.repeatIndex); } catch { message.error(t('webcam.uploadFailed')); }
              }
            }
            webcamBlobsRef.current = [];
          });
        }
      } else if (msg.type === 'preflight_error') {
        endPlaying(); setPlayingGroupName(null); setCurrentStepId(null);
        Modal.confirm({
          title: t('scenario.deviceCheckFailed'),
          content: (
            <div>
              <div style={{ maxHeight: 200, overflowY: 'auto', marginBottom: 10 }}>
                {(msg.errors || []).map((e: string, i: number) => (
                  <div key={i} style={{ padding: '4px 0', color: '#ff4d4f' }}>• {e}</div>
                ))}
              </div>
              <div style={{ color: '#888', fontSize: 11 }}>{t('scenario.preflightSwapHint')}</div>
            </div>
          ),
          okText: t('scenario.changeDeviceMap'),
          cancelText: t('common.close'),
          onOk: () => {
            setDeviceMapModalVisible(true);
          },
        });
        ws.close();
      } else if (msg.type === 'error') {
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        endPlaying(); setPlayingGroupName(null); setCurrentStepId(null); resumeScreenStream();
        message.error(msg.message); ws.close();
        if (doAutoRecord && webcamRecordingActiveRef.current) { webcam.stopRecordingAuto(); webcamRecordingActiveRef.current = false; webcamBlobsRef.current = []; }
      } else if (msg.type === 'playback_stopped') {
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        endPlaying(); setPaused(false); setPlayingGroupName(null); setCurrentStepId(null); resumeScreenStream();
        const resultFilename = msg.result_filename || '';
        if (resultFilename) {
          message.info(t('scenario.playStoppedPartial'));
          if (doAutoRecord && webcamRecordingActiveRef.current) {
            webcam.stopRecordingAuto().then(async () => {
              webcamRecordingActiveRef.current = false;
              if (webcamBlobsRef.current.length > 0) {
                for (const item of webcamBlobsRef.current) {
                  try { await resultsApi.uploadRecording(item.blob, resultFilename, item.repeatIndex); } catch { /* ignore */ }
                }
              }
              webcamBlobsRef.current = [];
            });
          }
        } else {
          message.info(t('scenario.playStopped'));
          if (doAutoRecord && webcamRecordingActiveRef.current) { webcam.stopRecordingAuto(); webcamRecordingActiveRef.current = false; webcamBlobsRef.current = []; }
        }
        ws.close();
      } else if (msg.type === 'playback_paused') {
        setPaused(true);
        if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
        if (doAutoRecord && webcamRecordingActiveRef.current) webcam.pauseRecording();
        pauseScreenStream();
      } else if (msg.type === 'playback_resumed') {
        setPaused(false);
        if (doAutoRecord && webcamRecordingActiveRef.current) webcam.resumeRecording();
        resumeScreenStream();
      }
    };
      ws.onerror = () => {
        if (!playingRef.current && liveDurationRef.current) {
          clearInterval(liveDurationRef.current);
          liveDurationRef.current = null;
        }
      };
      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (playingRef.current) {
          if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
          reconnectTimerRef.current = setTimeout(() => {
            reconnectTimerRef.current = null;
            if (playingRef.current) openPlaybackWS(true);
          }, 2000);
        }
      };
    };
    openPlaybackWS(false);
  };

  const stopPlayback = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'stop' }));
    } else {
      // WS가 끊긴 상태(재연결 대기 중 등)에도 재생을 확실히 중지하기 위해 REST 폴백
      scenarioApi.stopPlayback().catch(() => {});
      endPlaying();
      setPaused(false);
      setPlayingGroupName(null);
      setCurrentStepId(null);
      if (liveDurationRef.current) { clearInterval(liveDurationRef.current); liveDurationRef.current = null; }
    }
  };

  const pausePlayback = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'pause' }));
    }
  };

  const resumePlayback = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'resume' }));
    }
  };

  // --- Columns ---
  const expectedImageUrl = (scenarioName: string, filename: string | null) => {
    if (!filename) return null;
    return '/screenshots/' + scenarioName + '/' + filename;
  };

  const scenarioStepColumns = [
    { title: 'ID', dataIndex: 'id', key: 'id', width: 60 },
    { title: 'Remark', dataIndex: 'description', key: 'description', ellipsis: true },
    { title: t('common.type'), dataIndex: 'type', key: 'type', render: (val: string, row: any) => <Tag color={val === 'module_command' ? 'geekblue' : undefined}>{val === 'module_command' ? (row.params?.module || val) : val}</Tag> },
    { title: t('scenario.device'), dataIndex: 'device_id', key: 'device_id', width: 120, render: (v: string) => v ? <Tag color={v.startsWith('Android') ? 'green' : v.startsWith('Serial') ? 'purple' : 'geekblue'}>{v}</Tag> : '-' },
    {
      title: t('scenario.expectedImage'), dataIndex: 'expected_image', key: 'expected_image', width: 90,
      render: (v: string | null) => {
        const url = selectedScenario ? expectedImageUrl(selectedScenario.name, v) : null;
        return url ? <Image src={url} alt="expected" style={{ maxHeight: 60, maxWidth: 60 }} /> : '-';
      },
    },
    { title: t('scenario.parameters'), dataIndex: 'params', key: 'params', render: (p: any) => <code style={{ fontSize: 10 }}>{JSON.stringify(p)}</code> },
    { title: t('scenario.delay'), dataIndex: 'delay_after_ms', key: 'delay', width: 80, render: (v: number) => `${v}ms` },
  ];

  const playbackSteps = playbackScenario?.steps || [];
  const passCount = stepResults.filter((r) => r.status === 'pass' && !r.excluded_from_result).length;
  const failCount = stepResults.filter((r) => r.status === 'fail' && !r.excluded_from_result).length;
  const errorCount = stepResults.filter((r) => r.status === 'error' && !r.excluded_from_result).length;

  const _colTitle = (en: string, ko: string) => <div style={{ textAlign: 'center' }}>{en}<br /><span style={{ fontSize: 10, color: '#888' }}>{ko}</span></div>;
  const makeStepResultColumns = (totalRepeat: number) => [
    { title: _colTitle('Time Stamp', t('scenario.colTimestamp')), dataIndex: 'timestamp', key: 'timestamp', align: 'center' as const, render: (v: string | null) => <span style={{ fontSize: 11, lineHeight: 1.4 }}>{v ? formatTime(v, lang) : '-'}</span> },
    { title: _colTitle('Repeat', t('scenario.colCurrentTotal')), dataIndex: 'repeat_index', key: 'repeat', align: 'center' as const, render: (v: number) => totalRepeat === 0 ? `${v}/∞` : `${v}/${totalRepeat}` },
    { title: _colTitle('Step', t('scenario.colOrder')), dataIndex: 'step_id', key: 'step_id', align: 'center' as const },
    { title: _colTitle('Device', t('scenario.colDevice')), dataIndex: 'device_id', key: 'device_id', align: 'center' as const, render: (v: string) => v ? <Tag color={v.startsWith('Android') ? 'green' : v.startsWith('Serial') ? 'purple' : 'geekblue'} style={{ margin: 0 }}>{v}</Tag> : '-' },
    { title: _colTitle('Command', 'action'), dataIndex: 'command', key: 'command', width: colWidths['command'] || 200, ellipsis: true, align: 'center' as const, onHeaderCell: () => ({ width: colWidths['command'] || 200, onResize: (_e: any, { size }: any) => setColWidths(prev => ({ ...prev, command: size.width })) }), render: (v: string, r: StepResultData) => <span style={{ textAlign: 'left', display: 'block' }}>{v || r.message || '-'}</span> },
    { title: _colTitle('Remark', t('common.description')), dataIndex: 'description', key: 'description', width: colWidths['description'] || 200, ellipsis: true, align: 'center' as const, onHeaderCell: () => ({ width: colWidths['description'] || 200, onResize: (_e: any, { size }: any) => setColWidths(prev => ({ ...prev, description: size.width })) }), render: (v: string) => <span style={{ textAlign: 'left', display: 'block' }}>{v || '-'}</span> },
    { title: _colTitle('Status', t('common.result')), dataIndex: 'status', key: 'status', align: 'center' as const, render: (s: string, r: StepResultData) => s === 'running' ? <Tag color="processing">RUNNING</Tag> : <Tag color={statusColor(effStatus(r))}>{statusLabel(effStatus(r), t)}</Tag> },
    { title: _colTitle('Delay', t('scenario.colSetting')), dataIndex: 'delay_ms', key: 'delay', align: 'center' as const, render: (ms: number) => ms ? formatDuration(ms) : '-' },
    { title: _colTitle('Duration', t('scenario.colActual')), dataIndex: 'execution_time_ms', key: 'duration', align: 'center' as const, render: (ms: number, r: StepResultData) => r.status === 'running' ? <span style={{ color: '#1677ff' }}>{formatDuration(liveDuration)}</span> : formatDuration(ms) },
    { title: _colTitle('', t('scenario.compare')), key: 'compare', align: 'center' as const, render: (_: any, r: StepResultData) => {
      if (r.status === 'running') return '-';
      // 모듈 실행 결과(CMD 등)는 이미지 없이 메시지만 있을 수 있음
      const isModuleMsg = r.command?.startsWith('CMD::') || r.command?.includes('::');
      // RAND 출처 step은 message에 "[RAND]" 프리픽스 포함
      const isRandMsg = !!r.message && r.message.startsWith('[RAND]');
      const hasMsgOnly = (isModuleMsg && r.message && !r.expected_image && !r.actual_image) || isRandMsg;
      if (r.expected_image || r.actual_image || hasMsgOnly) {
        return <Button size="small" onClick={() => setCompareStep(r)}>{hasMsgOnly ? 'LOG' : t('scenario.compare')}</Button>;
      }
      return '-';
    }},
  ];

  const totalTime = (steps: StepResultData[]) => steps.reduce((sum, s) => sum + (s.execution_time_ms || 0), 0);

  const CompareImage = ({ src, roi, alt }: { src: string; roi: ROI | null; alt: string }) => {
    const cRef = useRef<HTMLCanvasElement>(null);
    const [loaded, setLoaded] = useState(false);
    useEffect(() => {
      if (!roi) { setLoaded(false); return; }
      const img = new window.Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => { const canvas = cRef.current; if (!canvas) return; canvas.width = roi.width; canvas.height = roi.height; const ctx = canvas.getContext('2d'); if (!ctx) return; ctx.drawImage(img, roi.x, roi.y, roi.width, roi.height, 0, 0, roi.width, roi.height); setLoaded(true); };
      img.onerror = () => setLoaded(false);
      img.src = src;
    }, [src, roi]);
    if (!roi) return <Image src={src} alt={alt} style={{ width: '100%' }} fallback="data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsbD0iIzMzMyIvPjx0ZXh0IHg9IjUwJSIgeT0iNTAlIiBmaWxsPSIjOTk5IiBkb21pbmFudC1iYXNlbGluZT0ibWlkZGxlIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmb250LXNpemU9IjE0Ij5ObyBJbWFnZTwvdGV4dD48L3N2Zz4=" />;
    return <canvas ref={cRef} style={{ width: '100%', borderRadius: 4, display: loaded ? 'block' : 'none' }} />;
  };

  // Group names for the selected scenario
  const scenarioGroups = (name: string) =>
    Object.entries(groups).filter(([, members]) => members.some((m) => m.name === name)).map(([g]) => g);

  return (
    <div style={{ height: 'calc(100vh - 80px)', overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
      {/* 최상단: 도구 버튼 우측 정렬 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 3, padding: '4px 0', flexShrink: 0 }}>
        <Button icon={<FolderOutlined />} size="small" onClick={() => {
          setGroupModalVisible(true);
          // 모달 open 시 멤버 시나리오 스텝을 강제 refetch — 다른 경로(RecordPage 녹화, 외부 편집)로
          // 변경된 내용까지 즉시 반영. 캐시에 이미 있어도 덮어씀.
          const allNames = Array.from(new Set(Object.values(groups).flatMap((ms) => ms.map((m) => m.name))));
          if (allNames.length > 0) fetchScenarioStepsCache(allNames, true);
        }}>{t('scenario.groupManage')}</Button>
        <Button icon={<ExportOutlined />} size="small" onClick={() => { setExportSelectedScenarios([]); setExportSelectedGroups([]); setExportAll(false); setExportModalVisible(true); }}>{t('scenario.exportTitle')}</Button>
        <Button icon={<ImportOutlined />} size="small" onClick={() => { setImportFile(null); setImportPreviewData(null); setImportModalVisible(true); }}>{t('scenario.importTitle')}</Button>
        <Button onClick={() => { fetchScenarios(); fetchGroups(); }} size="small">{t('common.refresh')}</Button>
      </div>
      <Splitter style={{ flex: 1, minHeight: 0 }}>
      <Splitter.Panel defaultSize="40%" min="20%" max="60%" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* DLT/Serial/Logcat 모두 재생 중 라이브 뷰어를 띄우지 않는다 — 로그 폭주 시 렌더링이
          메인 스레드를 잡아 PC 가 느려짐. StartLogging/StartSave 시 작은 노티만 표시 (위 useEffect).
          과거에는 DLT 활성 시 이 카드를 DLTViewer(card)로 대체했으나 동일 이유로 제거. */}
      <Card
        style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
        styles={{ body: { flex: 1, overflow: 'auto', padding: '8px 12px' } }}
        title={t('scenario.title')}
      >
        <Tabs
          activeKey={selectedGroup ?? '__all__'}
          onChange={(key) => setSelectedGroup(key === '__all__' ? null : key)}
          size="small"
          tabBarStyle={{ marginBottom: 6 }}
          items={[
            { key: '__all__', label: `${t('scenario.all')} (${scenarios.length})` },
            { key: '__groups__', label: `${t('scenario.groupLabel')} (${Object.keys(groups).length})` },
          ]}
        />

        {selectedGroup === '__groups__' ? (
          /* ===== 그룹 트리 (폴더 관리 + 드래그&드롭 + 컨텍스트 메뉴) ===== */
          (() => {
            const gFoldered = new Set<string>();
            for (const items of Object.values(groupFolders)) items.forEach(g => gFoldered.add(g));

            // 컨텍스트 메뉴 아이템 (폴더/그룹 별)
            const ctxItems = groupCtxMenu ? (
              groupCtxMenu.type === 'gfolder' ? [
                { key: 'rename', label: t('common.rename'), onClick: () => {
                  const newName = prompt(t('scenario.folderName') || '폴더 이름', groupCtxMenu.name);
                  if (newName && newName !== groupCtxMenu.name) {
                    scenarioApi.renameGroupFolder(groupCtxMenu.name, newName)
                      .then(res => setGroupFolders(res.data.folders || {}))
                      .catch((e: any) => message.error(e?.response?.data?.detail || 'Failed'));
                  }
                  setGroupCtxMenu(null);
                }},
                { key: 'delete', label: t('common.delete'), danger: true, onClick: () => {
                  scenarioApi.deleteGroupFolder(groupCtxMenu.name)
                    .then(res => setGroupFolders(res.data.folders || {}))
                    .catch(() => {});
                  setGroupCtxMenu(null);
                }},
              ] : [
                { key: 'rename', label: t('common.rename'), onClick: () => {
                  const newName = prompt(t('common.rename') || '이름 변경', groupCtxMenu.name);
                  if (newName && newName !== groupCtxMenu.name) {
                    scenarioApi.renameGroup(groupCtxMenu.name, newName).then(() => {
                      fetchGroups();
                      fetchGroupFolders();
                      if (groupShownInDetail === groupCtxMenu.name) setGroupShownInDetail(newName);
                    }).catch((e: any) => message.error(e?.response?.data?.detail || 'Failed'));
                  }
                  setGroupCtxMenu(null);
                }},
                { key: 'moveRoot', label: t('scenario.moveToRoot'), onClick: () => {
                  scenarioApi.moveGroupToFolder(groupCtxMenu.name, null)
                    .then(res => setGroupFolders(res.data.folders || {}));
                  setGroupCtxMenu(null);
                }},
                ...Object.keys(groupFolders).map(fn => ({
                  key: `move:${fn}`, label: `→ ${fn}`, onClick: () => {
                    scenarioApi.moveGroupToFolder(groupCtxMenu.name, fn)
                      .then(res => setGroupFolders(res.data.folders || {}));
                    setGroupCtxMenu(null);
                  },
                })),
                { type: 'divider' as const },
                { key: 'delete', label: t('common.delete'), danger: true, onClick: () => {
                  const name = groupCtxMenu.name;
                  Modal.confirm({
                    title: t('scenario.deleteTitle'),
                    okText: t('common.delete'),
                    okType: 'danger',
                    cancelText: t('common.cancel'),
                    onOk: () => {
                      deleteGroup(name);
                      if (groupShownInDetail === name) setGroupShownInDetail(null);
                    },
                  });
                  setGroupCtxMenu(null);
                }},
              ]
            ) : [];

            // 폴더 노드 title — 그룹 드롭 가능한 div (다중 지원)
            const renderFolderTitle = (fname: string, childCount: number) => (
              <div
                onDragOver={(e) => {
                  if (!e.dataTransfer.types.includes('application/x-group-name') && !e.dataTransfer.types.includes('application/x-group-names')) return;
                  e.preventDefault();
                  e.dataTransfer.dropEffect = 'move';
                  if (groupDropHover !== `__main_folder__${fname}`) setGroupDropHover(`__main_folder__${fname}`);
                }}
                onDragLeave={(e) => {
                  if ((e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) return;
                  if (groupDropHover === `__main_folder__${fname}`) setGroupDropHover(null);
                }}
                onDrop={async (e) => {
                  const hasMulti = e.dataTransfer.types.includes('application/x-group-names');
                  const hasSingle = e.dataTransfer.types.includes('application/x-group-name');
                  if (!hasMulti && !hasSingle) return;
                  e.preventDefault();
                  e.stopPropagation();
                  setGroupDropHover(null);
                  let names: string[] = [];
                  if (hasMulti) {
                    try { names = JSON.parse(e.dataTransfer.getData('application/x-group-names') || '[]'); } catch { /* ignore */ }
                  }
                  if (names.length === 0 && hasSingle) {
                    const single = e.dataTransfer.getData('application/x-group-name');
                    if (single) names = [single];
                  }
                  try {
                    let lastFolders: any = null;
                    for (const gn of names) {
                      const res = await scenarioApi.moveGroupToFolder(gn, fname);
                      lastFolders = res.data.folders;
                    }
                    if (lastFolders) setGroupFolders(lastFolders);
                  } catch (err: any) {
                    message.error(err?.response?.data?.detail || 'Failed to move');
                  }
                }}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 5,
                  padding: '2px 6px', borderRadius: 4,
                  background: groupDropHover === `__main_folder__${fname}` ? 'rgba(22,119,255,0.18)' : undefined,
                  border: groupDropHover === `__main_folder__${fname}` ? '1px dashed #1677ff' : '1px dashed transparent',
                }}
              >
                <span>{fname} ({childCount})</span>
              </div>
            );

            // 트리 데이터 — 폴더/그룹 모두 알파벳 오름차순
            const sortAsc = (a: string, b: string) => a.localeCompare(b, undefined, { sensitivity: 'base' });
            const treeData: any[] = [];
            const mainFlatOrder: string[] = []; // Shift 범위 선택용
            for (const fname of Object.keys(groupFolders).sort(sortAsc)) {
              const items = groupFolders[fname] || [];
              const sortedChildren = items.filter(g => g in groups).sort(sortAsc);
              const children = sortedChildren.map(gName => {
                mainFlatOrder.push(gName);
                return {
                  key: `group:${gName}`,
                  title: gName,
                  icon: <GroupIcon />,
                  isLeaf: true,
                };
              });
              treeData.push({
                key: `gfolder:${fname}`,
                title: renderFolderTitle(fname, children.length),
                icon: <FolderOutlined />,
                isLeaf: false,
                children,
              });
            }
            for (const gName of Object.keys(groups).sort(sortAsc)) {
              if (!gFoldered.has(gName)) {
                mainFlatOrder.push(gName);
                treeData.push({
                  key: `group:${gName}`,
                  title: gName,
                  icon: <GroupIcon />,
                  isLeaf: true,
                });
              }
            }
            // 폴더 필터 적용된 treeData
            const filteredTreeData = mainGroupFolder === '__all__'
              ? treeData
              : treeData.filter(node => node.key === `gfolder:${mainGroupFolder}`);
            return (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4, flexWrap: 'wrap' }}>
                  <Select size="small" value={mainGroupFolder} onChange={setMainGroupFolder} style={{ width: 120 }}>
                    <Select.Option value="__all__">{t('scenario.allScenarios')}</Select.Option>
                    {Object.keys(groupFolders).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' })).map(fn => (
                      <Select.Option key={fn} value={fn}>{fn}</Select.Option>
                    ))}
                  </Select>
                  <Button
                    size="small"
                    icon={<FolderAddOutlined />}
                    onClick={() => {
                      const name = prompt(t('scenario.folderName') || '폴더 이름');
                      if (name) {
                        scenarioApi.createGroupFolder(name)
                          .then(res => setGroupFolders(res.data.folders || {}))
                          .catch((e: any) => message.error(e?.response?.data?.detail || 'Failed'));
                      }
                    }}
                  >{t('scenario.newFolder') || '새 폴더'}</Button>
                  <span style={{ fontSize: 11, color: '#888' }}>{Object.keys(groups).length} {t('scenario.groupTree') || '그룹'}</span>
                </div>
                {filteredTreeData.length === 0 ? (
                  <div style={{ textAlign: 'center', color: '#888', padding: 16 }}>{t('scenario.noGroups')}</div>
                ) : (
                  <Dropdown
                    menu={{ items: ctxItems }}
                    open={!!groupCtxMenu}
                    onOpenChange={(v) => { if (!v) setGroupCtxMenu(null); }}
                    trigger={['contextMenu']}
                  >
                    <div onContextMenu={(e) => { if (!groupCtxMenu) e.preventDefault(); }}>
                      <Tree
                        treeData={filteredTreeData}
                        blockNode
                        showIcon
                        defaultExpandAll
                        selectedKeys={
                          multiSelectedGroupsMain.length > 0
                            ? multiSelectedGroupsMain.map(g => `group:${g}`)
                            : (groupShownInDetail ? [`group:${groupShownInDetail}`] : [])
                        }
                        multiple
                        draggable={{ icon: false, nodeDraggable: (node: any) => String(node.key).startsWith('group:') }}
                        allowDrop={() => true}
                        onDragStart={(info: any) => {
                          const k = String(info.node.key);
                          if (!k.startsWith('group:')) return;
                          const draggedName = k.replace('group:', '');
                          const names = (multiSelectedGroupsMain.includes(draggedName) && multiSelectedGroupsMain.length > 1)
                            ? multiSelectedGroupsMain
                            : [draggedName];
                          try {
                            info.event.dataTransfer.setData('application/x-group-names', JSON.stringify(names));
                            info.event.dataTransfer.setData('application/x-group-name', draggedName);
                            info.event.dataTransfer.effectAllowed = 'move';
                          } catch { /* ignore */ }
                        }}
                        onDrop={async (info: any) => {
                          const dragKey = info.dragNode?.key ? String(info.dragNode.key) : '';
                          if (!dragKey.startsWith('group:')) return;
                          const draggedName = dragKey.replace('group:', '');
                          const groupNames = (multiSelectedGroupsMain.includes(draggedName) && multiSelectedGroupsMain.length > 1)
                            ? multiSelectedGroupsMain
                            : [draggedName];
                          const dropKey = info.node?.key ? String(info.node.key) : '';
                          let targetFolder: string | null = null;
                          if (dropKey.startsWith('gfolder:')) {
                            targetFolder = dropKey.replace('gfolder:', '');
                          } else if (dropKey.startsWith('group:')) {
                            const targetName = dropKey.replace('group:', '');
                            for (const [fname, items] of Object.entries(groupFolders)) {
                              if (items.includes(targetName)) { targetFolder = fname; break; }
                            }
                          }
                          try {
                            let lastFolders: any = null;
                            for (const gn of groupNames) {
                              const res = await scenarioApi.moveGroupToFolder(gn, targetFolder);
                              lastFolders = res.data.folders;
                            }
                            if (lastFolders) setGroupFolders(lastFolders);
                          } catch (e: any) {
                            message.error(e?.response?.data?.detail || 'Failed to move');
                          }
                        }}
                        onRightClick={({ event, node }: any) => {
                          event.preventDefault();
                          const key = String(node.key);
                          if (key.startsWith('gfolder:')) {
                            setGroupCtxMenu({ x: event.clientX, y: event.clientY, type: 'gfolder', name: key.replace('gfolder:', '') });
                          } else if (key.startsWith('group:')) {
                            setGroupCtxMenu({ x: event.clientX, y: event.clientY, type: 'group', name: key.replace('group:', '') });
                          }
                        }}
                        onSelect={(_keys, info) => {
                          const ne = (info as any).nativeEvent as MouseEvent | undefined;
                          const isCtrl = !!(ne && (ne.ctrlKey || ne.metaKey));
                          const isShift = !!(ne && ne.shiftKey);
                          const clickedKey = String(info.node.key);
                          if (!clickedKey.startsWith('group:')) return;
                          const clickedName = clickedKey.replace('group:', '');
                          let next: string[];
                          if (isShift) {
                            const anchor = groupSelectAnchorMainRef.current;
                            const ai = anchor ? mainFlatOrder.indexOf(anchor) : -1;
                            const ci = mainFlatOrder.indexOf(clickedName);
                            if (ai >= 0 && ci >= 0) {
                              const [a, b] = ai <= ci ? [ai, ci] : [ci, ai];
                              next = mainFlatOrder.slice(a, b + 1);
                            } else {
                              next = [clickedName];
                              groupSelectAnchorMainRef.current = clickedName;
                            }
                          } else if (isCtrl) {
                            next = multiSelectedGroupsMain.includes(clickedName)
                              ? multiSelectedGroupsMain.filter(n => n !== clickedName)
                              : [...multiSelectedGroupsMain, clickedName];
                            groupSelectAnchorMainRef.current = clickedName;
                          } else {
                            next = [clickedName];
                            groupSelectAnchorMainRef.current = clickedName;
                          }
                          setMultiSelectedGroupsMain(next);
                          // 우측 상세는 마지막 클릭 그룹으로
                          setGroupShownInDetail(clickedName);
                          setSelectedName(null);
                          if (!playing) { setStepResults([]); setPlaybackScenario(null); }
                          const memberNames = (groups[clickedName] || []).map(m => m.name);
                          if (memberNames.length > 0) fetchScenarioStepsCache(memberNames);
                        }}
                      />
                    </div>
                  </Dropdown>
                )}
              </>
            );
          })()
        ) : (
          <>
          {/* ===== 트리 형식 시나리오 리스트 ===== */}
          {(() => {
            // 폴더에 속한 시나리오 Set
            const foldered = new Set<string>();
            for (const items of Object.values(folders)) items.forEach(n => foldered.add(n));
            // 트리 데이터 생성
            const treeData: any[] = [];
            // 폴더 노드
            for (const [fname, items] of Object.entries(folders)) {
              const existingCount = items.filter(n => scenarios.includes(n)).length;
              treeData.push({
                key: `folder:${fname}`,
                title: `${fname} (${existingCount})`,
                icon: <FolderOutlined />,
                isLeaf: false,
                children: items.filter(n => filteredScenarios.includes(n)).map(n => ({
                  key: `scenario:${n}`,
                  title: <span style={playing && (currentGroupScenario === n || playingName === n) ? { color: '#1677ff', fontWeight: 700 } : undefined}>{n}</span>,
                  icon: <FileOutlined style={playing && (currentGroupScenario === n || playingName === n) ? { color: '#1677ff' } : undefined} />,
                  isLeaf: true,
                })),
              });
            }
            // 루트 시나리오 (폴더에 속하지 않은 것)
            for (const name of filteredScenarios) {
              if (!foldered.has(name)) {
                const isPlaying = playing && (currentGroupScenario === name || playingName === name);
                treeData.push({ key: `scenario:${name}`, title: <span style={isPlaying ? { color: '#1677ff', fontWeight: 700 } : undefined}>{name}</span>, icon: <FileOutlined style={isPlaying ? { color: '#1677ff' } : undefined} />, isLeaf: true });
              }
            }

            // 트리에 보이는 시나리오의 평탄화 순서 — Shift 범위 선택 계산용.
            // 순서: 각 폴더의 시나리오 → 폴더에 속하지 않은 루트 시나리오.
            const flatScenarioOrder: string[] = [];
            for (const items of Object.values(folders)) {
              for (const n of items) {
                if (filteredScenarios.includes(n)) flatScenarioOrder.push(n);
              }
            }
            for (const name of filteredScenarios) {
              if (!foldered.has(name)) flatScenarioOrder.push(name);
            }

            const onSelect: TreeProps['onSelect'] = (_keys, info) => {
              const ne = (info as any).nativeEvent as MouseEvent | undefined;
              const isCtrl = !!(ne && (ne.ctrlKey || ne.metaKey));
              const isShift = !!(ne && ne.shiftKey);

              const clickedKey = String(info.node.key);
              const isClickedScenario = clickedKey.startsWith('scenario:');
              const clickedName = isClickedScenario ? clickedKey.replace('scenario:', '') : null;

              let finalScenarioNames: string[];

              if (isShift && clickedName) {
                // Shift: anchor → 클릭 항목 사이 전체 범위 선택 (트리 표시 순서 기준).
                // anchor가 없거나 보이지 않으면 fallback으로 클릭 항목만 선택.
                const anchor = selectionAnchorRef.current;
                const anchorIdx = anchor ? flatScenarioOrder.indexOf(anchor) : -1;
                const clickIdx = flatScenarioOrder.indexOf(clickedName);
                if (anchorIdx >= 0 && clickIdx >= 0) {
                  const [a, b] = anchorIdx <= clickIdx ? [anchorIdx, clickIdx] : [clickIdx, anchorIdx];
                  finalScenarioNames = flatScenarioOrder.slice(a, b + 1);
                } else {
                  finalScenarioNames = [clickedName];
                  selectionAnchorRef.current = clickedName;
                }
                // 주의: Shift는 anchor를 갱신하지 않음 (연속 Shift 클릭으로 범위 재조정 가능)
              } else if (isCtrl) {
                // Ctrl: 토글 — 기존 선택을 유지하면서 클릭 항목만 추가/제거
                if (clickedName) {
                  finalScenarioNames = multiSelectedNames.includes(clickedName)
                    ? multiSelectedNames.filter(n => n !== clickedName)
                    : [...multiSelectedNames, clickedName];
                  selectionAnchorRef.current = clickedName;
                } else {
                  finalScenarioNames = multiSelectedNames;
                }
              } else {
                // 일반 클릭: 다중 선택 해제, 클릭한 항목만 선택. anchor도 갱신.
                finalScenarioNames = clickedName ? [clickedName] : [];
                selectionAnchorRef.current = clickedName;
              }

              setMultiSelectedNames(finalScenarioNames);

              // 미리보기는 가장 최근 클릭한 시나리오 기준
              if (clickedName) {
                setSelectedName(clickedName);
                setGroupShownInDetail(null); // 시나리오 선택 시 우측 그룹 상세 닫기
                if (!playing) { setStepResults([]); setPlaybackScenario(null); }
              } else if (finalScenarioNames.length === 0) {
                setSelectedName(null);
              }
            };

            const onDrop: TreeProps['onDrop'] = async (info) => {
              const dragKey = info.dragNode.key as string;
              if (!dragKey.startsWith('scenario:')) return;
              const draggedName = dragKey.replace('scenario:', '');
              const dropKey = (info.node.key as string);
              // 폴더 노드 위/안에 떨어뜨림: 그 폴더로 이동
              // 다른 시나리오 위/사이에 떨어뜨림: 대상 시나리오가 속한 폴더로 (없으면 루트)
              let folderName: string | null = null;
              if (dropKey.startsWith('folder:')) {
                folderName = dropKey.replace('folder:', '');
              } else if (dropKey.startsWith('scenario:')) {
                const targetName = dropKey.replace('scenario:', '');
                for (const [fname, items] of Object.entries(folders)) {
                  if (items.includes(targetName)) { folderName = fname; break; }
                }
                // 폴더에 없으면 folderName=null (루트로)
              }

              // 드래그한 항목이 다중 선택에 포함되어 있고 2개 이상이면 전체 이동, 아니면 단일 이동.
              // (선택되지 않은 항목을 드래그한 경우는 그 항목만 이동 — 일반적인 파일 탐색기 UX)
              const targets = (multiSelectedNames.includes(draggedName) && multiSelectedNames.length > 1)
                ? multiSelectedNames
                : [draggedName];

              try {
                // 백엔드 move_to_folder는 단건 처리만 지원 → 병렬 호출 후 최종 상태 재조회
                const results = await Promise.all(
                  targets.map(n => scenarioApi.moveToFolder(n, folderName).catch(e => ({ error: e, name: n })))
                );
                const failed = results.filter((r: any) => r && r.error);
                if (failed.length === 0) {
                  // 마지막 응답의 folders로 갱신 (전체 일관성 보장 위해 한 번만 setFolders)
                  const last = results[results.length - 1] as any;
                  if (last?.data?.folders) setFolders(last.data.folders);
                  if (targets.length > 1) {
                    message.success(t('scenario.moveMultiSuccess', { count: targets.length }));
                  }
                } else {
                  message.error(`${failed.length}/${targets.length} ${t('scenario.moveFailed') || 'move failed'}`);
                  fetchFolders();
                }
              } catch {
                fetchFolders();
              }
            };

            const onRightClick = ({ event, node }: any) => {
              event.preventDefault();
              const key = node.key as string;
              const type = key.startsWith('folder:') ? 'folder' as const : 'scenario' as const;
              const name = key.replace(/^(folder|scenario):/, '');
              setContextMenu({ x: event.clientX, y: event.clientY, type, name });
            };

            const contextMenuItems = contextMenu ? (
              contextMenu.type === 'folder' ? [
                { key: 'rename', label: t('common.rename'), onClick: () => {
                  const newName = prompt(t('scenario.folderName'), contextMenu.name);
                  if (newName && newName !== contextMenu.name) {
                    scenarioApi.renameFolder(contextMenu.name, newName)
                      .then(res => setFolders(res.data.folders))
                      .catch((e: any) => message.error(e?.response?.data?.detail || t('scenario.renameFailed')));
                  }
                  setContextMenu(null);
                }},
                { key: 'delete', label: t('common.delete'), danger: true, onClick: () => {
                  scenarioApi.deleteFolder(contextMenu.name).then(res => setFolders(res.data.folders));
                  setContextMenu(null);
                }},
              ] : [
                { key: 'copy', label: t('common.copy'), onClick: () => {
                  const newName = prompt(t('common.rename'), `${contextMenu.name}_copy`);
                  if (newName) {
                    scenarioApi.copy(contextMenu.name, newName)
                      .then(() => { fetchScenarios(); fetchFolders(); })
                      .catch((e: any) => message.error(e?.response?.data?.detail || t('scenario.copyFailed')));
                  }
                  setContextMenu(null);
                }},
                { key: 'rename', label: t('common.rename'), onClick: () => {
                  const newName = prompt(t('common.rename'), contextMenu.name);
                  if (newName && newName !== contextMenu.name) {
                    const oldName = contextMenu.name;
                    scenarioApi.rename(oldName, newName)
                      .then(() => { fetchScenarios(); fetchFolders(); fetchGroups(); })
                      .catch((e: any) => message.error(e?.response?.data?.detail || t('scenario.renameFailed')));
                    if (selectedName === oldName) setSelectedName(newName);
                    setMultiSelectedNames(prev => prev.map(n => n === oldName ? newName : n));
                  }
                  setContextMenu(null);
                }},
                { key: 'moveRoot', label: t('scenario.moveToRoot'), onClick: () => {
                  scenarioApi.moveToFolder(contextMenu.name, null).then(res => setFolders(res.data.folders));
                  setContextMenu(null);
                }},
                ...Object.keys(folders).map(fn => ({
                  key: `move:${fn}`, label: `→ ${fn}`, onClick: () => {
                    scenarioApi.moveToFolder(contextMenu.name, fn).then(res => setFolders(res.data.folders));
                    setContextMenu(null);
                  },
                })),
                { type: 'divider' as const },
                { key: 'delete', label: t('common.delete'), danger: true, onClick: () => {
                  const targetName = contextMenu.name;
                  Modal.confirm({
                    title: t('scenario.deleteTitle'), okText: t('common.delete'), okType: 'danger', cancelText: t('common.cancel'),
                    onOk: () => {
                      scenarioApi.delete(targetName).then(() => { fetchScenarios(); fetchFolders(); });
                      if (selectedName === targetName) setSelectedName(null);
                      setMultiSelectedNames(prev => prev.filter(n => n !== targetName));
                    },
                  });
                  setContextMenu(null);
                }},
              ]
            ) : [];

            // 폴더 필터: 특정 폴더 선택 시 그 폴더만 표시
            const filteredTreeData = mainScenarioFolder === '__all__'
              ? treeData
              : treeData.filter(node => node.key === `folder:${mainScenarioFolder}`);
            return (
              <>
                <div style={{ display: 'flex', gap: 3, marginBottom: 3, alignItems: 'center', flexWrap: 'wrap' }}>
                  <Select size="small" value={mainScenarioFolder} onChange={setMainScenarioFolder} style={{ width: 120 }}>
                    <Select.Option value="__all__">{t('scenario.allScenarios')}</Select.Option>
                    {Object.keys(folders).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' })).map(fn => (
                      <Select.Option key={fn} value={fn}>{fn}</Select.Option>
                    ))}
                  </Select>
                  <Button size="small" icon={<FolderAddOutlined />} onClick={() => {
                    const name = prompt(t('scenario.folderName'));
                    if (name) scenarioApi.createFolder(name)
                      .then(res => setFolders(res.data.folders))
                      .catch((e: any) => message.error(e?.response?.data?.detail || 'Failed'));
                  }}>{t('scenario.newFolder')}</Button>
                </div>
                <Dropdown
                  menu={{ items: contextMenuItems }}
                  open={!!contextMenu}
                  onOpenChange={(v) => { if (!v) setContextMenu(null); }}
                  trigger={['contextMenu']}
                >
                  <div style={{ flex: 1, overflow: 'auto' }} onContextMenu={(e) => { if (!contextMenu) e.preventDefault(); }}>
                    <Tree
                      treeData={filteredTreeData}
                      multiple
                      selectedKeys={
                        multiSelectedNames.length > 0
                          ? multiSelectedNames.map(n => `scenario:${n}`)
                          : (selectedName ? [`scenario:${selectedName}`] : [])
                      }
                      onSelect={onSelect}
                      draggable={{ icon: false }}
                      onDrop={onDrop}
                      onRightClick={onRightClick}
                      showIcon
                      blockNode
                      defaultExpandAll
                    />
                    {treeData.length === 0 && <div style={{ padding: 13, textAlign: 'center', color: '#888' }}>{t('scenario.noScenarios')}</div>}
                  </div>
                </Dropdown>
              </>
            );
          })()}
          </>
        )}

      </Card>
      </Splitter.Panel>

      <Splitter.Panel style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <style>{`
        .row-pass td { background: rgba(82, 196, 26, 0.06) !important; }
        .row-fail td { background: rgba(255, 77, 79, 0.08) !important; }
        .row-error td { background: rgba(255, 77, 79, 0.06) !important; }
        .row-running td { background: rgba(22, 119, 255, 0.08) !important; }
      `}</style>

      {/* ===== 스텝 패널 (미리보기 + 재생 + 그룹 상세 통합) ===== */}
      {(selectedName && previewSteps.length > 0) || playing || ((stepResults.length > 0) && playbackScenario) || (groupShownInDetail && groups[groupShownInDetail]) ? (
        <Card
          size="small"
          style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
          title={
            // 재생 중에는 트리 선택 변경의 영향을 받지 않도록 항상 재생 분기로 진입하고
            // 타이틀 텍스트도 안정된 playingName(재생 시작 시 캡처, group은 WS로 갱신)을 우선 사용
            playing || (stepResults.length > 0 && playbackScenario) ? (
              <Space size={4} wrap>
                <span>{t('scenario.play')}: {playingGroupName ? `[${playingGroupName}]` : ''} {currentGroupScenario || playingName || playbackScenario?.name || ''}</span>
                {playingGroupName && groupScenarioTotal > 0 && <Tag color="cyan">{groupScenarioIndex}/{groupScenarioTotal} {t('scenario.title')}</Tag>}
                {totalIterations > 1 && <Tag color="purple">{currentIteration} / {totalIterations}{t('scenario.times')}</Tag>}
                {totalIterations === 0 && <Tag color="purple">{currentIteration} / ∞ {t('scenario.times')}</Tag>}
                {playing && !paused && <Tag color="processing">{t('scenario.inProgress')}</Tag>}
                {paused && <Tag color="warning">PAUSED</Tag>}
                {!playing && stepResults.length > 0 && <Tag color={failCount + errorCount > 0 ? 'red' : 'green'}>{t('scenario.complete')}</Tag>}
              </Space>
            ) : groupShownInDetail && groups[groupShownInDetail] ? (
              <Space size={4} wrap>
                <FolderOutlined style={{ color: '#1677ff' }} />
                <strong>{groupShownInDetail}</strong>
                <span style={{ fontWeight: 400 }}>— {(groups[groupShownInDetail] || []).filter(m => scenarios.includes(m.name)).length} {t('scenario.title')}</span>
                <InputNumber min={1} max={999} size="small" value={getRepeatCount(groupShownInDetail)} onChange={(v) => setRepeatCount(groupShownInDetail!, v || 1)} style={{ width: 60 }} disabled={playing} />
                <span style={{ fontSize: 11, fontWeight: 400 }}>{t('scenario.times')}</span>
                <UntilTimePicker
                  value={getUntilTime(groupShownInDetail)}
                  onChange={(iso) => setUntilTime(groupShownInDetail!, iso)}
                  placeholder={t('scenario.untilTimePlaceholder')}
                  tooltip={t('scenario.untilTimeTooltip')}
                  disabled={playing}
                />
                <Button type="primary" size="small" icon={<PlayCircleOutlined />} disabled={playing} onClick={() => playGroup(groupShownInDetail!)}>{t('scenario.play')}</Button>
              </Space>
            ) : (
              <Space size={4} wrap>
                <strong>{selectedName}</strong>
                <span style={{ fontWeight: 400 }}>— {previewSteps.length} {t('scenario.steps')}</span>
                {skipStepIds.size > 0 && <Tag color="orange">{skipStepIds.size} skip</Tag>}
                {playing && playingName === selectedName ? (
                  <Button danger size="small" icon={<StopOutlined />} onClick={stopPlayback}>{t('scenario.stop')}</Button>
                ) : (
                  <>
                    <InputNumber min={1} max={999} size="small" value={getRepeatCount(selectedName!)} onChange={(v) => setRepeatCount(selectedName!, v || 1)} style={{ width: 60 }} disabled={playing} />
                    <span style={{ fontSize: 11, fontWeight: 400 }}>{t('scenario.times')}</span>
                    <UntilTimePicker
                      value={getUntilTime(selectedName!)}
                      onChange={(iso) => setUntilTime(selectedName!, iso)}
                      placeholder={t('scenario.untilTimePlaceholder')}
                      tooltip={t('scenario.untilTimeTooltip')}
                      disabled={playing}
                    />
                    <Button type="primary" size="small" icon={<PlayCircleOutlined />} loading={playing && playingName === selectedName} disabled={playing} onClick={() => playScenario(selectedName!)}>{t('scenario.play')}</Button>
                  </>
                )}
              </Space>
            )
          }
          styles={{ ...({ body: { flex: 1, overflow: 'auto' } }), header: { flexWrap: 'wrap', height: 'auto', minHeight: 40, padding: '4px 12px' } }}
          extra={
            (playing || stepResults.length > 0) ? (
              <Space>
                {playing && !paused && <Button size="small" icon={<PauseOutlined />} onClick={pausePlayback}>일시정지</Button>}
                {playing && paused && <Button type="primary" size="small" icon={<PlayCircleOutlined />} onClick={resumePlayback}>재개</Button>}
                {playing && <Button danger size="small" icon={<StopOutlined />} onClick={stopPlayback}>{t('scenario.stop')}</Button>}
                <span>Pass: {passCount}</span><span>Fail: {failCount}</span><span>Error: {errorCount}</span><span>/ {playbackSteps.length} {t('scenario.steps')}</span>
              </Space>
            ) : undefined
          }
        >
          {(playing || stepResults.length > 0) ? (
            /* 재생 중 / 완료: 결과 테이블 (스텝만 스크롤, 자동 최하단) */
            <div style={{ flex: 1, overflow: 'auto' }}>
            <Table
              columns={makeStepResultColumns(totalIterations)}
              components={{ header: { cell: ResizableTitle } }}
              dataSource={playing ? [...stepResults].reverse() : stepResults}
              rowKey={(_r, idx) => `${idx}`}
              size="small"
              pagination={false}
              rowClassName={(r: StepResultData) => r.status === 'running' ? 'row-running' : r.excluded_from_result ? '' : r.status === 'fail' ? 'row-fail' : r.status === 'error' ? 'row-error' : r.status === 'pass' ? 'row-pass' : ''}
            />
            </div>
          ) : groupShownInDetail && groups[groupShownInDetail] ? (
            /* 그룹 상세 — 멤버 시나리오 리스트 + 펼침/조건부 이동 */
            <div style={{ flex: 1, overflow: 'auto', padding: 4 }}>
              {(() => {
                const gName = groupShownInDetail;
                const members = groups[gName] || [];
                return (
                  <List
                    size="small"
                    dataSource={members}
                    locale={{ emptyText: t('scenario.noScenarios') }}
                    renderItem={(entry, idx) => {
                      const entryKey = `${gName}:${idx}`;
                      const isExpanded = expandedEntries.has(entryKey);
                      const steps = scenarioStepsCache[entry.name] || [];
                      const stepJumps = entry.step_jumps || {};
                      const hasAnyJump = Object.keys(stepJumps).length > 0;

                      const renderJumpRow = (
                        jumpLabel: string, jumpColor: string,
                        passGoto: JumpTarget | null, failGoto: JumpTarget | null,
                        onUpdate: (pg: JumpTarget | null, fg: JumpTarget | null) => void,
                        field: 'pass' | 'fail',
                        excludeChecked: boolean, onToggleExclude: (checked: boolean) => void,
                      ) => {
                        const jump = field === 'pass' ? passGoto : failGoto;
                        const targetSteps = jump && jump.scenario >= 0 ? (scenarioStepsCache[members[jump.scenario]?.name] || []) : [];
                        return (
                          <span key={field} style={{ display: 'inline-flex', flex: '1 1 280px', minWidth: 220, gap: 3, alignItems: 'center', fontSize: 11 }}>
                            <span style={{ color: jumpColor, fontWeight: 700, flexShrink: 0 }}>{jumpLabel}</span>
                            <Select
                              size="small"
                              style={{ flex: '1 1 0', minWidth: 0 }}
                              value={jump ? jump.scenario : undefined}
                              allowClear
                              placeholder={t('scenario.nextTo')}
                              onChange={(v) => {
                                const newJump = v == null ? null : { scenario: v as number, step: 0 };
                                if (field === 'pass') onUpdate(newJump, failGoto);
                                else onUpdate(passGoto, newJump);
                              }}
                            >
                              <Select.Option value={-1}>{t('scenario.end')} (END)</Select.Option>
                              {members.map((m, mi) => (
                                <Select.Option key={mi} value={mi}>#{mi + 1} {m.name}</Select.Option>
                              ))}
                            </Select>
                            {jump && jump.scenario >= 0 && targetSteps.length > 0 && (
                              <Select
                                size="small"
                                style={{ flex: '1 1 0', minWidth: 0 }}
                                value={jump.step}
                                onChange={(stepVal) => {
                                  const newJump = { scenario: jump.scenario, step: stepVal as number };
                                  if (field === 'pass') onUpdate(newJump, failGoto);
                                  else onUpdate(passGoto, newJump);
                                }}
                              >
                                {targetSteps.map((s: any, si: number) => (
                                  <Select.Option key={si} value={si}>{formatStepLabel(s, si)}</Select.Option>
                                ))}
                              </Select>
                            )}
                            <Tooltip title={t('scenario.excludeResultTooltip')}>
                              <Checkbox
                                checked={excludeChecked}
                                onChange={(e) => onToggleExclude(e.target.checked)}
                                style={{ flexShrink: 0, fontSize: 11 }}
                              ><span style={{ fontSize: 11 }}>{t('scenario.branchMode')}</span></Checkbox>
                            </Tooltip>
                          </span>
                        );
                      };

                      const isDragOver = groupDrag && groupDrag.gName === gName && groupDrag.over === idx && groupDrag.from !== idx;
                      const dragOverFromAbove = isDragOver && (groupDrag!.from < idx);
                      const isPlayingNow = playing && currentGroupScenario === entry.name;

                      return (
                        <List.Item
                          style={{
                            display: 'block',
                            padding: '6px 0',
                            borderTop: isDragOver && !dragOverFromAbove ? '2px solid #1677ff' : undefined,
                            borderBottom: isDragOver && dragOverFromAbove ? '2px solid #1677ff' : undefined,
                            opacity: groupDrag && groupDrag.gName === gName && groupDrag.from === idx ? 0.4 : 1,
                            background: isPlayingNow ? 'rgba(22,119,255,0.15)' : undefined,
                            borderLeft: isPlayingNow ? '3px solid #1677ff' : '3px solid transparent',
                          }}
                          draggable
                          onDragStart={(e) => {
                            setGroupDrag({ gName, from: idx, over: null });
                            e.dataTransfer.effectAllowed = 'move';
                          }}
                          onDragOver={(e) => {
                            if (!groupDrag || groupDrag.gName !== gName) return;
                            e.preventDefault();
                            e.dataTransfer.dropEffect = 'move';
                            if (groupDrag.over !== idx) setGroupDrag({ ...groupDrag, over: idx });
                          }}
                          onDrop={(e) => {
                            e.preventDefault();
                            if (groupDrag && groupDrag.gName === gName) dropInGroup(gName, members, groupDrag.from, idx);
                            setGroupDrag(null);
                          }}
                          onDragEnd={() => setGroupDrag(null)}
                        >
                          {/* 헤더 — 클릭 시 시나리오로 이동하지 않고 펼침/접힘 */}
                          <div
                            style={{ display: 'flex', alignItems: 'center', gap: 3, cursor: 'pointer' }}
                            onClick={() => {
                              toggleExpandEntry(entryKey);
                              if (!isExpanded && steps.length === 0) fetchScenarioStepsCache([entry.name]);
                            }}
                          >
                            <Tag color={isPlayingNow ? 'processing' : 'blue'} style={{ margin: 0, minWidth: 24, textAlign: 'center' }}>{idx + 1}</Tag>
                            <Button size="small" type="text" style={{ padding: '0 2px', fontSize: 10 }}
                              icon={isExpanded ? <DownOutlined /> : <RightOutlined />}
                            />
                            <GroupIcon />
                            <span style={{ flex: 1, fontWeight: 500, color: isPlayingNow ? '#1677ff' : undefined }}>{entry.name}</span>
                            {!scenarios.includes(entry.name) && <Tag color="red">{t('scenario.missing')}</Tag>}
                            {hasAnyJump && <BranchesOutlined style={{ color: '#722ed1', fontSize: 11 }} />}
                            {isPlayingNow && <Tag color="blue" style={{ margin: 0, fontSize: 10 }}>▶</Tag>}
                            <span
                              onClick={(e) => e.stopPropagation()}
                              onMouseDown={(e) => e.stopPropagation()}
                              onKeyDown={(e) => e.stopPropagation()}
                              style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}
                            >
                              <Tooltip title={t('scenario.playCountTooltip')}>
                                <InputNumber
                                  size="small"
                                  min={1}
                                  max={999}
                                  value={entry.play_count ?? 1}
                                  onChange={(v) => updateGroupPlayCount(gName, idx, Number(v) || 1)}
                                  style={{ width: 56 }}
                                  disabled={playing}
                                />
                              </Tooltip>
                              <span style={{ fontSize: 10 }}>{t('scenario.times')}</span>
                            </span>
                            <span style={{ fontSize: 10 }}>{steps.length} {t('scenario.steps')}</span>
                            <Button size="small" type="text" danger icon={<DeleteOutlined />}
                              onClick={(e) => { e.stopPropagation(); removeFromGroup(gName, idx); }}
                            />
                          </div>

                          {/* 펼친 스텝 목록 — 조건부 이동 */}
                          {isExpanded && (
                            <div style={{ paddingLeft: 29, marginTop: 5, borderLeft: `2px solid ${isDark ? '#424242' : '#d9d9d9'}`, marginLeft: 14 }}>
                              <div style={{ fontSize: 10, marginBottom: 3, fontWeight: 600 }}>{t('scenario.stepConditionalJump')}:</div>
                              {steps.length === 0 && <div style={{ fontSize: 11, padding: 3 }}>{t('scenario.stepsLoading')}</div>}
                              {steps.map((step: any, si: number) => {
                                const sid = step.id;
                                const sj = stepJumps[String(sid)] || { on_pass_goto: null, on_fail_goto: null };
                                const hasSJ = sj.on_pass_goto != null || sj.on_fail_goto != null;
                                return (
                                  <div
                                    key={si}
                                    style={{ marginBottom: 3, padding: '4px 0', borderBottom: `1px solid ${isDark ? '#424242' : '#d9d9d9'}`, fontSize: 11 }}
                                  >
                                    {/* 1행: 스텝 정보 */}
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                      <Tag style={{ fontSize: 10, margin: 0, minWidth: 20, textAlign: 'center' }}>{sid}</Tag>
                                      <span style={{ flex: 1, color: hasSJ ? '#d89614' : undefined }}>{step.description || `(${step.type || 'step'})`}</span>
                                      {hasSJ && <BranchesOutlined style={{ color: '#d89614', fontSize: 10 }} />}
                                    </div>
                                    {/* 2행: P/F(+Branch Mode)/Reset */}
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 26, marginTop: 3, flexWrap: 'wrap' }}>
                                      {renderJumpRow('P→', '#52c41a', sj.on_pass_goto, sj.on_fail_goto,
                                        (pg, fg) => updateGroupStepJumps(gName, idx, sid, pg, fg, !!sj.exclude_pass_from_result, !!sj.exclude_fail_from_result), 'pass',
                                        !!sj.exclude_pass_from_result, (checked) => updateGroupStepJumps(gName, idx, sid, sj.on_pass_goto, sj.on_fail_goto, checked, !!sj.exclude_fail_from_result))}
                                      {renderJumpRow('F→', '#ff4d4f', sj.on_pass_goto, sj.on_fail_goto,
                                        (pg, fg) => updateGroupStepJumps(gName, idx, sid, pg, fg, !!sj.exclude_pass_from_result, !!sj.exclude_fail_from_result), 'fail',
                                        !!sj.exclude_fail_from_result, (checked) => updateGroupStepJumps(gName, idx, sid, sj.on_pass_goto, sj.on_fail_goto, !!sj.exclude_pass_from_result, checked))}
                                      {(hasSJ || sj.exclude_pass_from_result || sj.exclude_fail_from_result) && (
                                        <Button size="small" type="link" danger style={{ fontSize: 10, padding: 0 }}
                                          icon={<ClearOutlined />}
                                          onClick={() => updateGroupStepJumps(gName, idx, sid, null, null, false, false)}
                                        >{t('scenario.reset')}</Button>
                                      )}
                                    </div>
                                  </div>
                                );
                              })}
                            </div>
                          )}
                        </List.Item>
                      );
                    }}
                  />
                );
              })()}
            </div>
          ) : (
            /* 미리보기: 스텝 편집 테이블 */
            <div style={{ flex: 1, overflow: 'auto' }}>
              <Table
                size="small"
                pagination={false}
                dataSource={previewSteps}
                rowKey="id"
                rowClassName={(r: any) => skipStepIds.has(r.id) ? 'row-skip' : ''}
                columns={[
                  {
                    title: <Checkbox
                      checked={skipStepIds.size === 0}
                      indeterminate={skipStepIds.size > 0 && skipStepIds.size < previewSteps.length}
                      onChange={(e) => {
                        if (e.target.checked) setSkipStepIds(new Set());
                        else setSkipStepIds(new Set(previewSteps.map((s: any) => s.id)));
                      }}
                    />,
                    key: 'check', width: 32, align: 'center' as const,
                    render: (_: any, r: any) => (
                      <Checkbox
                        checked={!skipStepIds.has(r.id)}
                        onChange={(e) => {
                          setSkipStepIds(prev => {
                            const next = new Set(prev);
                            if (e.target.checked) next.delete(r.id);
                            else next.add(r.id);
                            return next;
                          });
                        }}
                      />
                    ),
                  },
                  { title: '#', dataIndex: 'id', key: 'id', width: 32, align: 'center' as const },
                  { title: 'Type', dataIndex: 'type', key: 'type', width: 'auto' as any, ellipsis: true, render: (v: string, row: any) => <Tag color={v === 'module_command' ? 'geekblue' : undefined} style={{ margin: 0 }}>{v === 'module_command' ? (row.params?.module || v) : v}</Tag> },
                  { title: 'Device', dataIndex: 'device_id', key: 'device', ellipsis: true, render: (v: string) => v ? <Tag color="blue" style={{ margin: 0 }}>{v}</Tag> : '-' },
                  { title: t('common.description'), dataIndex: 'description', key: 'desc', ellipsis: true },
                  {
                    title: 'Delay', dataIndex: 'delay_after_ms', key: 'delay', width: 80, align: 'center' as const,
                    render: (v: number, _r: any, idx: number) => {
                      const isWait = _r.type === 'wait';
                      const displayVal = isWait ? (_r.params?.duration_ms ?? v) : v;
                      return (
                        <InputNumber
                          size="small" min={0} step={100} value={displayVal} style={{ width: 70 }}
                          onChange={(val) => {
                            const updated = [...previewSteps];
                            const name = selectedName!;
                            // 그룹 디테일 뷰가 같은 시나리오를 표시 중일 때 즉시 반영되도록
                            // 저장 성공 후 캐시도 동기화. 실패 시(.catch)는 캐시 갱신 안 함 —
                            // optimistic 한 previewSteps 는 다음 fetch 때 정정됨.
                            const syncCache = () => setScenarioStepsCache((prev) => ({ ...prev, [name]: updated }));
                            if (isWait) {
                              updated[idx] = { ...updated[idx], params: { ..._r.params, duration_ms: val ?? 0 } };
                              setPreviewSteps(updated);
                              scenarioApi.updateStep(name, idx, { params: { ..._r.params, duration_ms: val ?? 0 } }).then(syncCache).catch(() => {});
                            } else {
                              updated[idx] = { ...updated[idx], delay_after_ms: val ?? 0 };
                              setPreviewSteps(updated);
                              scenarioApi.updateStep(name, idx, { delay_after_ms: val ?? 0 }).then(syncCache).catch(() => {});
                            }
                          }}
                        />
                      );
                    },
                  },
                  { title: t('scenario.compare'), key: 'img', render: (_: any, r: any) => {
                    if (!r.expected_image) return '-';
                    // 파일명에 timestamp가 포함되므로 r.id 기반 캐시 키만으로도 충분하지만, 안전하게 유지
                    const imgSrc = `/screenshots/${selectedName}/${r.expected_image}?v=${r.id}`;
                    const mode = r.compare_mode;
                    const regions: { x: number; y: number; width: number; height: number }[] = [];
                    let regionColor = '#52c41a';
                    // single_crop은 저장된 이미지 자체가 크롭 영역이므로 rect overlay를 그리지 않음
                    // (원본 좌표계 ROI를 크롭 이미지에 그리면 화면 밖으로 나가거나 잘못된 위치에 표시됨)
                    if (mode === 'multi_crop' && r.expected_images?.length) {
                      r.expected_images.forEach((ci: any) => { if (ci.roi) regions.push(ci.roi); });
                    } else if (mode === 'full_exclude' && r.exclude_rois?.length) {
                      r.exclude_rois.forEach((roi: any) => regions.push(roi));
                      regionColor = '#ff4d4f';
                    }
                    if (regions.length === 0) {
                      return <Image src={imgSrc} alt="expected" style={{ height: 32, maxWidth: 80, objectFit: 'contain', borderRadius: 2 }} preview={{ mask: false }} />;
                    }
                    return (
                      <ExpectedThumbnail src={imgSrc} regions={regions} color={regionColor} height={32} />
                    );
                  }},
                ]}
              />
              <style>{`.row-skip td { opacity: 0.35; }`}</style>
            </div>
          )}
        </Card>
      ) : (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#999' }}>
          {t('scenario.selectToView')}
        </div>
      )}
      </Splitter.Panel>
      </Splitter>

      {/* ===== 그룹 관리 모달 ===== */}
      <Modal title={t('scenario.groupManage')} open={groupModalVisible} onCancel={() => setGroupModalVisible(false)} footer={null} width={1400}
        styles={{ body: { height: '78vh', display: 'flex', flexDirection: 'column', overflow: 'hidden' } }}
      >
        <Space style={{ marginBottom: 6 }}>
          <Input
            placeholder={t('scenario.newGroupName')}
            value={newGroupName}
            onChange={(e) => setNewGroupName(e.target.value)}
            onPressEnter={createGroup}
            style={{ width: 200 }}
          />
          <Button icon={<FolderAddOutlined />} type="primary" onClick={createGroup}>{t('scenario.create')}</Button>
          <span style={{ color: '#888', fontSize: 11 }}>{t('scenario.dragHint')}</span>
        </Space>
        <Splitter style={{ flex: 1, minHeight: 0 }}>
          <Splitter.Panel defaultSize="22%" min="15%" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', paddingRight: 6 }}>
            {/* ───── 좌측: 그룹 트리 (폴더 지원) ───── */}
            {(() => {
              // 폴더에 속한 그룹 Set
              const gFoldered = new Set<string>();
              for (const items of Object.values(groupFolders)) items.forEach(g => gFoldered.add(g));

              // 그룹 노드 title 생성기 — 시나리오 드롭 가능 (그룹 자체 드래그는 AntD Tree가 처리)
              const renderGroupNodeTitle = (gName: string, members: any[]) => (
                <div
                  onDragOver={(e) => {
                    if (!e.dataTransfer.types.includes('application/x-scenario-names')) return;
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'copy';
                    if (groupDropHover !== gName) setGroupDropHover(gName);
                  }}
                  onDragLeave={(e) => {
                    if ((e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) return;
                    if (groupDropHover === gName) setGroupDropHover(null);
                  }}
                  onDrop={async (e) => {
                    if (!e.dataTransfer.types.includes('application/x-scenario-names')) return;
                    e.preventDefault();
                    e.stopPropagation();
                    setGroupDropHover(null);
                    try {
                      const raw = e.dataTransfer.getData('application/x-scenario-names');
                      const names: string[] = JSON.parse(raw || '[]');
                      for (const n of names) await addToGroup(gName, n);
                      setSelectedGroupForDetail(gName);
                    } catch { /* ignore */ }
                  }}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 5,
                    padding: '0 4px', borderRadius: 4,
                    background: groupDropHover === gName ? 'rgba(22,119,255,0.18)' : undefined,
                    border: groupDropHover === gName ? '1px dashed #1677ff' : '1px dashed transparent',
                  }}
                >
                  <span style={{ fontWeight: selectedGroupForDetail === gName ? 600 : 400 }}>{gName}</span>
                </div>
              );

              // 폴더 노드 title 생성기 — 그룹 드롭(HTML5 dataTransfer)을 받아 그 폴더로 이동 (다중 지원)
              const renderFolderNodeTitle = (fname: string, childCount: number) => (
                <div
                  onDragOver={(e) => {
                    if (!e.dataTransfer.types.includes('application/x-group-name') && !e.dataTransfer.types.includes('application/x-group-names')) return;
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    if (groupDropHover !== `__folder__${fname}`) setGroupDropHover(`__folder__${fname}`);
                  }}
                  onDragLeave={(e) => {
                    if ((e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) return;
                    if (groupDropHover === `__folder__${fname}`) setGroupDropHover(null);
                  }}
                  onDrop={async (e) => {
                    const hasMulti = e.dataTransfer.types.includes('application/x-group-names');
                    const hasSingle = e.dataTransfer.types.includes('application/x-group-name');
                    if (!hasMulti && !hasSingle) return;
                    e.preventDefault();
                    e.stopPropagation();
                    setGroupDropHover(null);
                    let names: string[] = [];
                    if (hasMulti) {
                      try { names = JSON.parse(e.dataTransfer.getData('application/x-group-names') || '[]'); } catch { /* ignore */ }
                    }
                    if (names.length === 0 && hasSingle) {
                      const single = e.dataTransfer.getData('application/x-group-name');
                      if (single) names = [single];
                    }
                    try {
                      let lastFolders: any = null;
                      for (const gn of names) {
                        const res = await scenarioApi.moveGroupToFolder(gn, fname);
                        lastFolders = res.data.folders;
                      }
                      if (lastFolders) setGroupFolders(lastFolders);
                    } catch (err: any) {
                      message.error(err?.response?.data?.detail || 'Failed to move');
                    }
                  }}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 5,
                    padding: '2px 6px', borderRadius: 4,
                    background: groupDropHover === `__folder__${fname}` ? 'rgba(22,119,255,0.18)' : undefined,
                    border: groupDropHover === `__folder__${fname}` ? '1px dashed #1677ff' : '1px dashed transparent',
                  }}
                >
                  <span>{fname}</span>
                  <Tag style={{ margin: 0 }}>{childCount}</Tag>
                </div>
              );

              // 트리 데이터: 폴더 노드 (자식으로 그룹) + 루트 그룹 — 모두 이름 알파벳 오름차순
              const groupTreeData: any[] = [];
              const sortAsc = (a: string, b: string) => a.localeCompare(b, undefined, { sensitivity: 'base' });
              const sortedFolderNames = Object.keys(groupFolders).sort(sortAsc);
              const modalFlatOrder: string[] = []; // Shift 범위 선택용 평탄화 순서
              for (const fname of sortedFolderNames) {
                const items = groupFolders[fname] || [];
                const sortedChildren = items.filter(g => g in groups).sort(sortAsc);
                const children = sortedChildren.map(gName => {
                  modalFlatOrder.push(gName);
                  return {
                    key: `group:${gName}`,
                    isLeaf: true,
                    icon: <GroupIcon />,
                    title: renderGroupNodeTitle(gName, groups[gName] || []),
                  };
                });
                groupTreeData.push({
                  key: `gfolder:${fname}`,
                  title: renderFolderNodeTitle(fname, children.length),
                  icon: <FolderOutlined />,
                  isLeaf: false,
                  children,
                });
              }
              for (const gName of Object.keys(groups).sort(sortAsc)) {
                if (!gFoldered.has(gName)) {
                  modalFlatOrder.push(gName);
                  groupTreeData.push({
                    key: `group:${gName}`,
                    isLeaf: true,
                    icon: <GroupIcon />,
                    title: renderGroupNodeTitle(gName, groups[gName] || []),
                  });
                }
              }

              // 컨텍스트 메뉴 항목
              const ctxItems = groupCtxMenu ? (
                groupCtxMenu.type === 'gfolder' ? [
                  { key: 'rename', label: t('common.rename'), onClick: () => {
                    const newName = prompt(t('scenario.folderName') || '폴더 이름', groupCtxMenu.name);
                    if (newName && newName !== groupCtxMenu.name) {
                      scenarioApi.renameGroupFolder(groupCtxMenu.name, newName)
                        .then(res => setGroupFolders(res.data.folders || {}))
                        .catch((e: any) => message.error(e?.response?.data?.detail || 'Failed'));
                    }
                    setGroupCtxMenu(null);
                  }},
                  { key: 'delete', label: t('common.delete'), danger: true, onClick: () => {
                    scenarioApi.deleteGroupFolder(groupCtxMenu.name)
                      .then(res => setGroupFolders(res.data.folders || {}))
                      .catch(() => {});
                    setGroupCtxMenu(null);
                  }},
                ] : [
                  { key: 'rename', label: t('common.rename'), onClick: () => {
                    const newName = prompt(t('common.rename') || '이름 변경', groupCtxMenu.name);
                    if (newName && newName !== groupCtxMenu.name) {
                      scenarioApi.renameGroup(groupCtxMenu.name, newName).then(() => {
                        fetchGroups();
                        fetchGroupFolders();
                        if (selectedGroupForDetail === groupCtxMenu.name) setSelectedGroupForDetail(newName);
                      }).catch((e: any) => message.error(e?.response?.data?.detail || 'Failed'));
                    }
                    setGroupCtxMenu(null);
                  }},
                  { key: 'moveRoot', label: t('scenario.moveToRoot'), onClick: () => {
                    scenarioApi.moveGroupToFolder(groupCtxMenu.name, null)
                      .then(res => setGroupFolders(res.data.folders || {}));
                    setGroupCtxMenu(null);
                  }},
                  ...Object.keys(groupFolders).map(fn => ({
                    key: `move:${fn}`, label: `→ ${fn}`, onClick: () => {
                      scenarioApi.moveGroupToFolder(groupCtxMenu.name, fn)
                        .then(res => setGroupFolders(res.data.folders || {}));
                      setGroupCtxMenu(null);
                    },
                  })),
                  { type: 'divider' as const },
                  { key: 'delete', label: t('common.delete'), danger: true, onClick: () => {
                    const name = groupCtxMenu.name;
                    Modal.confirm({
                      title: t('scenario.deleteTitle'),
                      okText: t('common.delete'),
                      okType: 'danger',
                      cancelText: t('common.cancel'),
                      onOk: () => {
                        deleteGroup(name);
                        if (selectedGroupForDetail === name) setSelectedGroupForDetail(null);
                      },
                    });
                    setGroupCtxMenu(null);
                  }},
                ]
              ) : [];

              return (
                <>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }}>
                    <span style={{ fontSize: 11, color: '#888', flex: 1 }}>{t('scenario.groupTree') || '그룹'}</span>
                    <Button
                      size="small"
                      icon={<FolderAddOutlined />}
                      onClick={() => {
                        const name = prompt(t('scenario.folderName') || '폴더 이름');
                        if (name) {
                          scenarioApi.createGroupFolder(name)
                            .then(res => setGroupFolders(res.data.folders || {}))
                            .catch((e: any) => message.error(e?.response?.data?.detail || 'Failed'));
                        }
                      }}
                    >{t('scenario.newFolder') || '새 폴더'}</Button>
                  </div>
                  <Dropdown
                    menu={{ items: ctxItems }}
                    open={!!groupCtxMenu}
                    onOpenChange={(v) => { if (!v) setGroupCtxMenu(null); }}
                    trigger={['contextMenu']}
                  >
                    <div style={{ flex: 1, overflow: 'auto', border: '1px solid #303030', borderRadius: 4, padding: 4 }} onContextMenu={(e) => { if (!groupCtxMenu) e.preventDefault(); }}>
                      <Tree
                        treeData={groupTreeData}
                        multiple
                        selectedKeys={
                          multiSelectedGroupsModal.length > 0
                            ? multiSelectedGroupsModal.map(g => `group:${g}`)
                            : (selectedGroupForDetail ? [`group:${selectedGroupForDetail}`] : [])
                        }
                        onSelect={(_keys, info) => {
                          const ne = (info as any).nativeEvent as MouseEvent | undefined;
                          const isCtrl = !!(ne && (ne.ctrlKey || ne.metaKey));
                          const isShift = !!(ne && ne.shiftKey);
                          const clickedKey = String(info.node.key);
                          if (!clickedKey.startsWith('group:')) return;
                          const clickedName = clickedKey.replace('group:', '');
                          let next: string[];
                          if (isShift) {
                            const anchor = groupSelectAnchorModalRef.current;
                            const ai = anchor ? modalFlatOrder.indexOf(anchor) : -1;
                            const ci = modalFlatOrder.indexOf(clickedName);
                            if (ai >= 0 && ci >= 0) {
                              const [a, b] = ai <= ci ? [ai, ci] : [ci, ai];
                              next = modalFlatOrder.slice(a, b + 1);
                            } else {
                              next = [clickedName];
                              groupSelectAnchorModalRef.current = clickedName;
                            }
                          } else if (isCtrl) {
                            next = multiSelectedGroupsModal.includes(clickedName)
                              ? multiSelectedGroupsModal.filter(n => n !== clickedName)
                              : [...multiSelectedGroupsModal, clickedName];
                            groupSelectAnchorModalRef.current = clickedName;
                          } else {
                            // 일반 클릭 — 다중 선택 해제 (릴리즈)
                            next = [clickedName];
                            groupSelectAnchorModalRef.current = clickedName;
                          }
                          setMultiSelectedGroupsModal(next);
                          setSelectedGroupForDetail(clickedName);
                        }}
                        draggable={{ icon: false, nodeDraggable: (node: any) => String(node.key).startsWith('group:') }}
                        allowDrop={() => true}
                        onDragStart={(info: any) => {
                          // 다중 선택에 포함된 그룹을 드래그하면 전체 셋업, 아니면 단일
                          const k = String(info.node.key);
                          if (!k.startsWith('group:')) return;
                          const draggedName = k.replace('group:', '');
                          const names = (multiSelectedGroupsModal.includes(draggedName) && multiSelectedGroupsModal.length > 1)
                            ? multiSelectedGroupsModal
                            : [draggedName];
                          try {
                            info.event.dataTransfer.setData('application/x-group-names', JSON.stringify(names));
                            info.event.dataTransfer.setData('application/x-group-name', draggedName); // 단일 폴백
                            info.event.dataTransfer.effectAllowed = 'move';
                          } catch { /* ignore */ }
                        }}
                        onDrop={async (info: any) => {
                          // 백업: AntD 내부 drop으로 그룹 → 폴더/그룹 이동 처리
                          const dragKey = info.dragNode?.key ? String(info.dragNode.key) : '';
                          if (!dragKey.startsWith('group:')) return;
                          const draggedName = dragKey.replace('group:', '');
                          const groupNames = (multiSelectedGroupsModal.includes(draggedName) && multiSelectedGroupsModal.length > 1)
                            ? multiSelectedGroupsModal
                            : [draggedName];
                          const dropKey = info.node?.key ? String(info.node.key) : '';
                          let targetFolder: string | null = null;
                          if (dropKey.startsWith('gfolder:')) {
                            targetFolder = dropKey.replace('gfolder:', '');
                          } else if (dropKey.startsWith('group:')) {
                            const targetName = dropKey.replace('group:', '');
                            for (const [fname, items] of Object.entries(groupFolders)) {
                              if (items.includes(targetName)) { targetFolder = fname; break; }
                            }
                          }
                          try {
                            let lastFolders: any = null;
                            for (const gn of groupNames) {
                              const res = await scenarioApi.moveGroupToFolder(gn, targetFolder);
                              lastFolders = res.data.folders;
                            }
                            if (lastFolders) setGroupFolders(lastFolders);
                          } catch (e: any) {
                            message.error(e?.response?.data?.detail || 'Failed to move');
                          }
                        }}
                        onRightClick={({ event, node }: any) => {
                          event.preventDefault();
                          const key = String(node.key);
                          if (key.startsWith('gfolder:')) {
                            setGroupCtxMenu({ x: event.clientX, y: event.clientY, type: 'gfolder', name: key.replace('gfolder:', '') });
                          } else if (key.startsWith('group:')) {
                            setGroupCtxMenu({ x: event.clientX, y: event.clientY, type: 'group', name: key.replace('group:', '') });
                          }
                        }}
                        blockNode
                        showIcon
                        defaultExpandAll
                      />
                      {Object.keys(groups).length === 0 && <div style={{ textAlign: 'center', color: '#888', padding: 16 }}>{t('scenario.noGroups')}</div>}
                    </div>
                  </Dropdown>
                </>
              );
            })()}
          </Splitter.Panel>
          <Splitter.Panel defaultSize="30%" min="18%" style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', paddingLeft: 6, paddingRight: 6 }}>
            {(() => {
              // ───── 중앙: 시나리오 트리 (모달 전용) ─────
              const foldered = new Set<string>();
              for (const items of Object.values(folders)) items.forEach(n => foldered.add(n));
              // 폴더 필터링
              const visible = modalTreeFolder === '__all__'
                ? scenarios
                : scenarios.filter(n => (folders[modalTreeFolder] || []).includes(n));
              const treeData: any[] = [];
              if (modalTreeFolder === '__all__') {
                for (const [fname, items] of Object.entries(folders)) {
                  const children = items.filter(n => scenarios.includes(n)).map(n => ({
                    key: `scenario:${n}`,
                    title: n,
                    icon: <FileOutlined />,
                    isLeaf: true,
                  }));
                  treeData.push({
                    key: `folder:${fname}`,
                    title: `${fname} (${children.length})`,
                    icon: <FolderOutlined />,
                    isLeaf: false,
                    selectable: false,
                    children,
                  });
                }
                for (const name of scenarios) {
                  if (!foldered.has(name)) {
                    treeData.push({ key: `scenario:${name}`, title: name, icon: <FileOutlined />, isLeaf: true });
                  }
                }
              } else {
                for (const name of visible) {
                  treeData.push({ key: `scenario:${name}`, title: name, icon: <FileOutlined />, isLeaf: true });
                }
              }
              // 평탄화된 시나리오 순서 (Shift 범위 선택)
              const flatOrder: string[] = [];
              if (modalTreeFolder === '__all__') {
                for (const items of Object.values(folders)) {
                  for (const n of items) if (scenarios.includes(n)) flatOrder.push(n);
                }
                for (const name of scenarios) if (!foldered.has(name)) flatOrder.push(name);
              } else {
                for (const name of visible) flatOrder.push(name);
              }
              return (
                <>
                  <div style={{ display: 'flex', gap: 4, marginBottom: 4, alignItems: 'center' }}>
                    <Select size="small" value={modalTreeFolder} onChange={setModalTreeFolder} style={{ width: 130 }}>
                      <Select.Option value="__all__">{t('scenario.allScenarios')}</Select.Option>
                      {Object.keys(folders).map(fn => <Select.Option key={fn} value={fn}>{fn}</Select.Option>)}
                    </Select>
                    <span style={{ fontSize: 11, color: '#888' }}>{visible.length} {t('scenario.title')}</span>
                  </div>
                  <div style={{ flex: 1, overflow: 'auto', border: '1px solid #303030', borderRadius: 4, padding: 4 }}>
                    <Tree
                      treeData={treeData}
                      multiple
                      blockNode
                      showIcon
                      defaultExpandAll
                      draggable={{ icon: false, nodeDraggable: (node: any) => String(node.key).startsWith('scenario:') || String(node.key).startsWith('folder:') }}
                      selectedKeys={modalTreeSelected.map(n => `scenario:${n}`)}
                      onSelect={(_keys, info) => {
                        const ne = (info as any).nativeEvent as MouseEvent | undefined;
                        const isCtrl = !!(ne && (ne.ctrlKey || ne.metaKey));
                        const isShift = !!(ne && ne.shiftKey);
                        const clickedKey = String(info.node.key);
                        if (!clickedKey.startsWith('scenario:')) return;
                        const clickedName = clickedKey.replace('scenario:', '');
                        let next: string[];
                        if (isShift) {
                          const anchor = modalTreeAnchorRef.current;
                          const ai = anchor ? flatOrder.indexOf(anchor) : -1;
                          const ci = flatOrder.indexOf(clickedName);
                          if (ai >= 0 && ci >= 0) {
                            const [a, b] = ai <= ci ? [ai, ci] : [ci, ai];
                            next = flatOrder.slice(a, b + 1);
                          } else {
                            next = [clickedName];
                            modalTreeAnchorRef.current = clickedName;
                          }
                        } else if (isCtrl) {
                          next = modalTreeSelected.includes(clickedName)
                            ? modalTreeSelected.filter(n => n !== clickedName)
                            : [...modalTreeSelected, clickedName];
                          modalTreeAnchorRef.current = clickedName;
                        } else {
                          next = [clickedName];
                          modalTreeAnchorRef.current = clickedName;
                        }
                        setModalTreeSelected(next);
                      }}
                      onDragStart={(info: any) => {
                        const key = String(info.node.key);
                        let names: string[];
                        if (key.startsWith('folder:')) {
                          // 폴더 드래그 — 하위 시나리오 전체를 일괄 추가
                          const fname = key.replace('folder:', '');
                          names = (folders[fname] || []).filter(n => scenarios.includes(n));
                          if (names.length === 0) return;
                        } else if (key.startsWith('scenario:')) {
                          const draggedName = key.replace('scenario:', '');
                          // 드래그한 항목이 다중 선택에 포함되어 있으면 전체, 아니면 단일
                          names = (modalTreeSelected.includes(draggedName) && modalTreeSelected.length > 1)
                            ? modalTreeSelected
                            : [draggedName];
                        } else {
                          return;
                        }
                        try {
                          info.event.dataTransfer.setData('application/x-scenario-names', JSON.stringify(names));
                          info.event.dataTransfer.setData('text/plain', names.join('\n'));
                          info.event.dataTransfer.effectAllowed = 'copy';
                        } catch { /* ignore */ }
                      }}
                    />
                    {treeData.length === 0 && <div style={{ textAlign: 'center', color: '#888', padding: 16 }}>{t('scenario.noScenarios')}</div>}
                  </div>
                </>
              );
            })()}
          </Splitter.Panel>
          <Splitter.Panel style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', paddingLeft: 6 }}>
            {/* ───── 우측: 선택된 그룹 상세 ───── */}
            {selectedGroupForDetail && groups[selectedGroupForDetail] ? (() => {
              const gName = selectedGroupForDetail;
              const members = groups[gName];
              return (
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                  {/* 헤더: 그룹명 + 멤버 수 + 삭제 버튼 (헤더 자체가 드롭존) */}
                  <div
                    onDragOver={(e) => {
                      if (!e.dataTransfer.types.includes('application/x-scenario-names')) return;
                      e.preventDefault();
                      e.dataTransfer.dropEffect = 'copy';
                      if (groupDropHover !== gName) setGroupDropHover(gName);
                    }}
                    onDragLeave={(e) => {
                      if ((e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) return;
                      if (groupDropHover === gName) setGroupDropHover(null);
                    }}
                    onDrop={async (e) => {
                      if (!e.dataTransfer.types.includes('application/x-scenario-names')) return;
                      e.preventDefault();
                      setGroupDropHover(null);
                      try {
                        const raw = e.dataTransfer.getData('application/x-scenario-names');
                        const names: string[] = JSON.parse(raw || '[]');
                        for (const n of names) await addToGroup(gName, n);
                      } catch { /* ignore */ }
                    }}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6, padding: '4px 8px', marginBottom: 4,
                      borderRadius: 4,
                      background: groupDropHover === gName ? 'rgba(22,119,255,0.15)' : 'rgba(255,255,255,0.04)',
                      border: groupDropHover === gName ? '1px dashed #1677ff' : '1px solid transparent',
                    }}
                  >
                    <FolderOutlined />
                    <span style={{ fontWeight: 600 }}>{gName}</span>
                    <Tag>{members.length}</Tag>
                    {groupDropHover === gName && <span style={{ fontSize: 10, color: '#1677ff' }}>{t('scenario.dropHere')}</span>}
                    <Button size="small" danger icon={<DeleteOutlined />} style={{ marginLeft: 'auto' }} onClick={() => { deleteGroup(gName); setSelectedGroupForDetail(null); }}>{t('common.delete')}</Button>
                  </div>
                  {/* 본문 — 멤버 리스트 + 추가 UI (전체가 드롭존) */}
                  <div
                    onDragOver={(e) => {
                      if (!e.dataTransfer.types.includes('application/x-scenario-names')) return;
                      e.preventDefault();
                      e.dataTransfer.dropEffect = 'copy';
                      if (groupDropHover !== gName) setGroupDropHover(gName);
                    }}
                    onDragLeave={(e) => {
                      if ((e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) return;
                      if (groupDropHover === gName) setGroupDropHover(null);
                    }}
                    onDrop={async (e) => {
                      if (!e.dataTransfer.types.includes('application/x-scenario-names')) return;
                      e.preventDefault();
                      setGroupDropHover(null);
                      try {
                        const raw = e.dataTransfer.getData('application/x-scenario-names');
                        const names: string[] = JSON.parse(raw || '[]');
                        for (const n of names) await addToGroup(gName, n);
                      } catch { /* ignore */ }
                    }}
                    style={{
                      flex: 1, overflow: 'auto', padding: 4,
                      outline: groupDropHover === gName ? '2px dashed #1677ff' : 'none',
                      outlineOffset: -2,
                      borderRadius: 4,
                    }}
                  >
                <List
                  size="small"
                  dataSource={members}
                  locale={{ emptyText: t('scenario.noScenarios') }}
                  renderItem={(entry, idx) => {
                    const entryKey = `${gName}:${idx}`;
                    const isExpanded = expandedEntries.has(entryKey);
                    const steps = scenarioStepsCache[entry.name] || [];
                    const stepJumps = entry.step_jumps || {};
                    const hasAnyJump = Object.keys(stepJumps).length > 0;

                    // Shared jump selector renderer — 인라인 형태 (스텝과 같은 줄)
                    const renderJumpRow = (
                      jumpLabel: string, jumpColor: string,
                      passGoto: JumpTarget | null, failGoto: JumpTarget | null,
                      onUpdate: (pg: JumpTarget | null, fg: JumpTarget | null) => void,
                      field: 'pass' | 'fail',
                      excludeChecked: boolean, onToggleExclude: (checked: boolean) => void,
                    ) => {
                      const jump = field === 'pass' ? passGoto : failGoto;
                      const targetSteps = jump && jump.scenario >= 0 ? (scenarioStepsCache[members[jump.scenario]?.name] || []) : [];
                      return (
                        <span key={field} style={{ display: 'inline-flex', gap: 3, alignItems: 'center', fontSize: 11 }}>
                          <span style={{ color: jumpColor, fontWeight: 700 }}>{jumpLabel}</span>
                          <Select
                            size="small"
                            style={{ width: 120 }}
                            value={jump ? jump.scenario : undefined}
                            allowClear
                            placeholder={t('scenario.nextTo')}
                            onChange={(v) => {
                              const newJump = v == null ? null : { scenario: v as number, step: 0 };
                              if (field === 'pass') onUpdate(newJump, failGoto);
                              else onUpdate(passGoto, newJump);
                            }}
                          >
                            <Select.Option value={-1}>{t('scenario.end')} (END)</Select.Option>
                            {members.map((m, mi) => (
                              <Select.Option key={mi} value={mi}>#{mi + 1} {m.name}</Select.Option>
                            ))}
                          </Select>
                          {jump && jump.scenario >= 0 && targetSteps.length > 0 && (
                            <Select
                              size="small"
                              style={{ width: 160 }}
                              value={jump.step}
                              onChange={(stepVal) => {
                                const newJump = { scenario: jump.scenario, step: stepVal as number };
                                if (field === 'pass') onUpdate(newJump, failGoto);
                                else onUpdate(passGoto, newJump);
                              }}
                            >
                              {targetSteps.map((s: any, si: number) => (
                                <Select.Option key={si} value={si}>{formatStepLabel(s, si)}</Select.Option>
                              ))}
                            </Select>
                          )}
                          <Tooltip title={t('scenario.excludeResultTooltip')}>
                            <Checkbox
                              checked={excludeChecked}
                              onChange={(e) => onToggleExclude(e.target.checked)}
                              style={{ flexShrink: 0, fontSize: 11 }}
                            ><span style={{ fontSize: 11 }}>{t('scenario.branchMode')}</span></Checkbox>
                          </Tooltip>
                        </span>
                      );
                    };

                    const isDragOver = groupDrag && groupDrag.gName === gName && groupDrag.over === idx && groupDrag.from !== idx;
                    const dragOverFromAbove = isDragOver && (groupDrag!.from < idx);
                    return (
                      <List.Item
                        style={{
                          display: 'block',
                          padding: '6px 0',
                          borderTop: isDragOver && !dragOverFromAbove ? '2px solid #1677ff' : undefined,
                          borderBottom: isDragOver && dragOverFromAbove ? '2px solid #1677ff' : undefined,
                          opacity: groupDrag && groupDrag.gName === gName && groupDrag.from === idx ? 0.4 : 1,
                        }}
                        draggable
                        onDragStart={(e) => {
                          setGroupDrag({ gName, from: idx, over: null });
                          e.dataTransfer.effectAllowed = 'move';
                        }}
                        onDragOver={(e) => {
                          if (!groupDrag || groupDrag.gName !== gName) return;
                          e.preventDefault();
                          e.dataTransfer.dropEffect = 'move';
                          if (groupDrag.over !== idx) setGroupDrag({ ...groupDrag, over: idx });
                        }}
                        onDrop={(e) => {
                          e.preventDefault();
                          if (groupDrag && groupDrag.gName === gName) dropInGroup(gName, members, groupDrag.from, idx);
                          setGroupDrag(null);
                        }}
                        onDragEnd={() => setGroupDrag(null)}
                      >
                        {/* 시나리오 헤더 */}
                        <div style={{ display: 'flex', alignItems: 'center', gap: 3, cursor: 'grab' }}>
                          <Tag color="blue" style={{ minWidth: 24, textAlign: 'center' }}>{idx + 1}</Tag>
                          <Button size="small" type="text" style={{ padding: '0 2px', fontSize: 10 }}
                            icon={isExpanded ? <DownOutlined /> : <RightOutlined />}
                            onClick={() => { toggleExpandEntry(entryKey); if (!isExpanded && steps.length === 0) fetchScenarioStepsCache([entry.name]); }}
                          />
                          <span style={{ flex: 1, fontWeight: 500 }}>{entry.name}</span>
                          {!scenarios.includes(entry.name) && <Tag color="red">{t('scenario.missing')}</Tag>}
                          {hasAnyJump && <BranchesOutlined style={{ color: '#722ed1', fontSize: 11 }} />}
                          <span
                            onClick={(e) => e.stopPropagation()}
                            onMouseDown={(e) => e.stopPropagation()}
                            onKeyDown={(e) => e.stopPropagation()}
                            style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}
                          >
                            <Tooltip title={t('scenario.playCountTooltip')}>
                              <InputNumber
                                size="small"
                                min={1}
                                max={999}
                                value={entry.play_count ?? 1}
                                onChange={(v) => updateGroupPlayCount(gName, idx, Number(v) || 1)}
                                style={{ width: 56 }}
                                disabled={playing}
                              />
                            </Tooltip>
                            <span style={{ fontSize: 10 }}>{t('scenario.times')}</span>
                          </span>
                          <span style={{ fontSize: 10 }}>{steps.length} {t('scenario.steps')}</span>
                          <Button size="small" type="text" danger icon={<DeleteOutlined />}
                            onClick={() => removeFromGroup(gName, idx)}
                          />
                        </div>

                        {/* 펼쳐진 스텝 목록 */}
                        {isExpanded && (
                          <div style={{ paddingLeft: 29, marginTop: 5, borderLeft: `2px solid ${isDark ? '#424242' : '#d9d9d9'}`, marginLeft: 14 }}>
                            <div style={{ fontSize: 10, marginBottom: 3, fontWeight: 600 }}>{t('scenario.stepConditionalJump')}:</div>
                            {steps.length === 0 && <div style={{ fontSize: 11, padding: 3 }}>{t('scenario.stepsLoading')}</div>}
                            {steps.map((step: any, si: number) => {
                              const sid = step.id;
                              const sj = stepJumps[String(sid)] || { on_pass_goto: null, on_fail_goto: null };
                              const hasSJ = sj.on_pass_goto != null || sj.on_fail_goto != null;
                              return (
                                <div
                                  key={si}
                                  style={{
                                    marginBottom: 3,
                                    padding: '4px 0',
                                    borderBottom: `1px solid ${isDark ? '#424242' : '#d9d9d9'}`,
                                    fontSize: 11,
                                  }}
                                >
                                  {/* 1행: 스텝 정보 */}
                                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                    <Tag style={{ fontSize: 10, margin: 0, minWidth: 20, textAlign: 'center' }}>{sid}</Tag>
                                    <span style={{ flex: 1, color: hasSJ ? '#d89614' : undefined }}>{step.description || `(${step.type || 'step'})`}</span>
                                    {hasSJ && <BranchesOutlined style={{ color: '#d89614', fontSize: 10 }} />}
                                  </div>
                                  {/* 2행: P / F(+Branch Mode) / Reset */}
                                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 26, marginTop: 3, flexWrap: 'wrap' }}>
                                    {renderJumpRow('P→', '#52c41a', sj.on_pass_goto, sj.on_fail_goto,
                                      (pg, fg) => updateGroupStepJumps(gName, idx, sid, pg, fg, !!sj.exclude_pass_from_result, !!sj.exclude_fail_from_result), 'pass',
                                      !!sj.exclude_pass_from_result, (checked) => updateGroupStepJumps(gName, idx, sid, sj.on_pass_goto, sj.on_fail_goto, checked, !!sj.exclude_fail_from_result))}
                                    {renderJumpRow('F→', '#ff4d4f', sj.on_pass_goto, sj.on_fail_goto,
                                      (pg, fg) => updateGroupStepJumps(gName, idx, sid, pg, fg, !!sj.exclude_pass_from_result, !!sj.exclude_fail_from_result), 'fail',
                                      !!sj.exclude_fail_from_result, (checked) => updateGroupStepJumps(gName, idx, sid, sj.on_pass_goto, sj.on_fail_goto, !!sj.exclude_pass_from_result, checked))}
                                    {(hasSJ || sj.exclude_pass_from_result || sj.exclude_fail_from_result) && (
                                      <Button size="small" type="link" danger style={{ fontSize: 10, padding: 0 }}
                                        icon={<ClearOutlined />}
                                        onClick={() => updateGroupStepJumps(gName, idx, sid, null, null, false, false)}
                                      >{t('scenario.reset')}</Button>
                                    )}
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </List.Item>
                    );
                  }}
                />
                  </div>
                </div>
              );
            })() : (
              <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#888', fontSize: 12 }}>
                {Object.keys(groups).length === 0 ? t('scenario.noGroups') : (t('scenario.selectGroup') || '좌측 트리에서 그룹을 선택하세요')}
              </div>
            )}
          </Splitter.Panel>
        </Splitter>
      </Modal>

      {/* ===== 복사 모달 ===== */}
      <Modal title={t('scenario.renameTitle', { name: selectedName || '' })} open={renameModalVisible} onCancel={() => setRenameModalVisible(false)} onOk={doRename} okText={t('common.change')}>
        <Input value={renameNewName} onChange={(e) => setRenameNewName(e.target.value)} placeholder={t('scenario.newScenarioName')} />
      </Modal>

      <Modal title={t('scenario.copyTitle', { name: selectedName || '' })} open={copyModalVisible} onCancel={() => setCopyModalVisible(false)} onOk={doCopy} okText={t('common.copy')}>
        <Input value={copyName} onChange={(e) => setCopyName(e.target.value)} placeholder={t('scenario.newScenarioName')} />
      </Modal>

      {/* ===== 시나리오 상세 모달 ===== */}
      <Modal title={selectedScenario?.name || t('scenario.scenarioDetail')} open={detailVisible} onCancel={() => setDetailVisible(false)} width={900} footer={null}>
        {selectedScenario && (
          <>
            <Descriptions column={2} size="small" style={{ marginBottom: 6 }}>
              <Descriptions.Item label={t('common.description')}>{selectedScenario.description || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('scenario.deviceMapping')}>
                {Object.keys(selectedScenario.device_map || {}).length > 0
                  ? Object.entries(selectedScenario.device_map).map(([alias, real]) => (
                    <Tag key={alias} color={alias.startsWith('Android') ? 'green' : alias.startsWith('Serial') ? 'purple' : 'geekblue'}>
                      {alias} → {real}
                    </Tag>
                  ))
                  : selectedScenario.device_serial || '-'}
              </Descriptions.Item>
              <Descriptions.Item label={t('scenario.resolution')}>{selectedScenario.resolution ? `${selectedScenario.resolution.width}×${selectedScenario.resolution.height}` : '-'}</Descriptions.Item>
              <Descriptions.Item label={t('scenario.stepCount')}>{selectedScenario.steps.length}</Descriptions.Item>
            </Descriptions>
            <Table columns={scenarioStepColumns} dataSource={selectedScenario.steps} rowKey="id" size="small" pagination={false} />
          </>
        )}
      </Modal>

      {/* ===== 이미지 비교 / 모듈 결과 모달 ===== */}
      <Modal title={t('scenario.stepCompare', { id: String(compareStep?.step_id) })} open={!!compareStep} onCancel={() => setCompareStep(null)} width={1100} footer={null} zIndex={1100}>
        {compareStep && (() => {
          const _msg = compareStep.message || '';
          const _hasImage = compareStep.expected_image || compareStep.actual_image;

          // 모듈 결과 메시지 박스 (CMD/DLT 등 모든 module_command 결과)
          const renderModuleMessage = () => {
            if (!_msg) return null;
            const isFail = compareStep.status === 'fail';
            return (
              <div style={{
                marginBottom: 10, padding: '8px 10px', borderRadius: 4, fontSize: 11, fontFamily: 'monospace',
                background: isFail ? '#2a1215' : '#122010',
                border: `1px solid ${isFail ? '#5c2024' : '#274916'}`,
                color: isFail ? '#ff7875' : '#95de64',
                whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 400, overflow: 'auto',
              }}>{_msg}</div>
            );
          };

          // 메시지 전용 (이미지 없음)
          if (!_hasImage && _msg) {
            return (
              <>
                <div style={{ marginBottom: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Tag color={statusColor(effStatus(compareStep))} style={{ fontSize: 12 }}>{statusDetail(compareStep, t)}</Tag>
                  <span style={{ color: '#888', marginLeft: 'auto' }}>Duration: {formatDuration(compareStep.execution_time_ms)}</span>
                </div>
                {compareStep.command && (
                  <div style={{ marginBottom: 6, padding: '6px 10px', background: '#1a1a2e', borderRadius: 4, fontFamily: 'monospace', fontSize: 11 }}>
                    <span style={{ color: '#888' }}>$ </span><span style={{ color: '#e0e0e0' }}>{compareStep.command}</span>
                  </div>
                )}
                {renderModuleMessage()}
              </>
            );
          }

          // 이미지 비교 (+ 메시지 겸용)
          return (
            <>
              <Space style={{ marginBottom: 6 }} wrap>
                <Tag color={statusColor(effStatus(compareStep))}>{statusDetail(compareStep, t)}</Tag>
                {compareStep.compare_mode && compareStep.compare_mode !== 'full' && (
                  <Tag color="purple">
                    {compareStep.compare_mode === 'single_crop' ? t('scenario.singleCrop') : compareStep.compare_mode === 'full_exclude' ? t('scenario.excludeArea') : compareStep.compare_mode === 'multi_crop' ? t('scenario.multiCrop') : compareStep.compare_mode}
                  </Tag>
                )}
                {compareStep.similarity_score != null && <span>{t('scenario.similarity')}: {(compareStep.similarity_score * 100).toFixed(2)}%</span>}
                {compareStep.match_location && <Tag color="blue">{t('scenario.matchLocation')}: ({compareStep.match_location.x},{compareStep.match_location.y}) {compareStep.match_location.width}x{compareStep.match_location.height}</Tag>}
                <span style={{ color: '#888' }}>Duration: {formatDuration(compareStep.execution_time_ms)}</span>
              </Space>
              {/* 모듈 결과 메시지 (이미지 비교와 함께) */}
              {_msg && compareStep.command && compareStep.command.includes('::') && (
                <div style={{ marginBottom: 6 }}>
                  <div style={{ marginBottom: 3, padding: '6px 10px', background: '#1a1a2e', borderRadius: 4, fontFamily: 'monospace', fontSize: 11 }}>
                    <span style={{ color: '#888' }}>$ </span><span style={{ color: '#e0e0e0' }}>{compareStep.command}</span>
                  </div>
                  {renderModuleMessage()}
                </div>
              )}
              <Row gutter={16}>
                <Col span={12}>
                  <Card size="small" title={
                    compareStep.compare_mode === 'full_exclude' ? t('scenario.expectedExclude')
                    : compareStep.compare_mode === 'multi_crop' ? t('scenario.expectedCrop')
                    : t('scenario.expectedImage2')
                  }>
                    {compareStep.expected_image ? (
                      <Image
                        src={`${imageUrl(compareStep.expected_annotated_image || compareStep.expected_image)!}?t=${Date.now()}`}
                        alt="Expected"
                        style={{ width: '100%' }}
                      />
                    ) : <div style={{ textAlign: 'center', padding: 32, color: '#666' }}>{t('scenario.noImage')}</div>}
                  </Card>
                </Col>
                <Col span={12}>
                  <Card size="small" title={t('scenario.actualImage')}>
                    {compareStep.actual_annotated_image ? <Image src={`${imageUrl(compareStep.actual_annotated_image)!}?t=${Date.now()}`} alt="Actual (annotated)" style={{ width: '100%' }} /> : compareStep.actual_image ? <CompareImage src={imageUrl(compareStep.actual_image)!} roi={compareStep.roi} alt="Actual" /> : <div style={{ textAlign: 'center', padding: 32, color: '#666' }}>{t('scenario.noImage')}</div>}
                  </Card>
                </Col>
              </Row>
              {compareStep.compare_mode === 'multi_crop' && compareStep.sub_results?.length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <Card size="small" title={t('scenario.cropDetailResult')}>
                    <Table
                      dataSource={compareStep.sub_results}
                      rowKey={(_r, idx) => `sub-${idx}`}
                      size="small"
                      pagination={false}
                      columns={[
                        { title: t('scenario.label'), dataIndex: 'label', key: 'label', render: (v: string) => v || '-' },
                        { title: t('scenario.score'), dataIndex: 'score', key: 'score', width: 100, render: (v: number) => `${(v * 100).toFixed(2)}%` },
                        { title: t('common.status'), dataIndex: 'status', key: 'status', width: 80, render: (s: string) => <Tag color={statusColor(s)}>{statusLabel(s, t)}</Tag> },
                        { title: t('scenario.matchLocation'), key: 'loc', width: 200, render: (_: any, r: SubResultData) => r.match_location ? `(${r.match_location.x},${r.match_location.y}) ${r.match_location.width}x${r.match_location.height}` : '-' },
                      ]}
                    />
                  </Card>
                </div>
              )}
              {compareStep.compare_mode === 'full_exclude' && (
                <div style={{ marginTop: 10 }}>
                  <Card size="small"><span style={{ color: '#888' }}>{t('scenario.excludeAreaDescription')}</span></Card>
                </div>
              )}
            </>
          );
        })()}
      </Modal>

      {/* ===== 디바이스 매핑 모달 ===== */}
      <Modal
        title={t('scenario.deviceMappingCheck')}
        open={deviceMapModalVisible}
        onCancel={() => setDeviceMapModalVisible(false)}
        onOk={() => {
          setDeviceMapModalVisible(false);
          const name = deviceMapScenarioName;
          if (name.startsWith('group:')) {
            startGroupPlayback(name.slice(6), deviceMapEditing);
          } else {
            startPlayback(name, deviceMapEditing);
          }
        }}
        okText={t('scenario.play')}
        width={600}
      >
        <p style={{ marginBottom: 10, color: '#888' }}>{t('scenario.deviceMappingDescription')}</p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {Object.entries(deviceMapEditing).map(([alias, realId]) => {
            const connected = connectedDevices.find(d => d.id === realId);
            const isOk = connected && connected.status !== 'offline' && connected.status !== 'disconnected';
            return (
              <div key={alias} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px', background: isOk ? 'rgba(82,196,26,0.06)' : 'rgba(255,77,79,0.06)', borderRadius: 6, border: `1px solid ${isOk ? '#52c41a33' : '#ff4d4f33'}` }}>
                <Tag color="blue" style={{ minWidth: 90, textAlign: 'center' }}>{alias}</Tag>
                <span style={{ color: '#888' }}>→</span>
                <Select
                  value={realId}
                  onChange={(val) => setDeviceMapEditing(prev => ({ ...prev, [alias]: val }))}
                  style={{ flex: 1 }}
                  size="small"
                >
                  {connectedDevices.map(d => (
                    <Select.Option key={d.id} value={d.id}>
                      <Space size={4}>
                        <Tag color={d.status === 'device' || d.status === 'connected' ? 'green' : d.status === 'offline' || d.status === 'disconnected' ? 'red' : 'default'} style={{ marginRight: 0 }}>{d.type}</Tag>
                        {d.id}
                        {d.status === 'offline' || d.status === 'disconnected' ? <span style={{ color: '#ff4d4f' }}>({t('scenario.disconnected')})</span> : null}
                      </Space>
                    </Select.Option>
                  ))}
                </Select>
                {isOk ? <CheckCircleOutlined style={{ color: '#52c41a' }} /> : <WarningOutlined style={{ color: '#faad14' }} />}
              </div>
            );
          })}
        </div>
        <Divider style={{ margin: '12px 0' }} />
        <Checkbox checked={webcamAutoRecord} onChange={(e) => setWebcamAutoRecord(e.target.checked)}>
          <VideoCameraOutlined style={{ color: webcamAutoRecord ? '#ff4d4f' : undefined, marginRight: 3 }} />
          {t('webcam.autoRecord')}
        </Checkbox>
      </Modal>

      {/* ===== 내보내기 모달 ===== */}
      <Modal
        title={t('scenario.exportTitle')}
        open={exportModalVisible}
        onCancel={() => setExportModalVisible(false)}
        onOk={doExport}
        okText={t('common.download')}
        confirmLoading={exportLoading}
        okButtonProps={{ disabled: !exportAll && exportSelectedScenarios.length === 0 && exportSelectedGroups.length === 0 }}
        width={500}
      >
        <Checkbox
          checked={exportAll}
          onChange={(e) => {
            setExportAll(e.target.checked);
            if (e.target.checked) {
              setExportSelectedScenarios([...scenarios]);
              setExportSelectedGroups(Object.keys(groups));
            } else {
              setExportSelectedScenarios([]);
              setExportSelectedGroups([]);
            }
          }}
          style={{ marginBottom: 10 }}
        >
          <strong>{t('scenario.selectAll')}</strong>
        </Checkbox>

        {Object.keys(groups).length > 0 && (
          <>
            <Divider style={{ margin: '8px 0' }}>{t('scenario.groupLabel')}</Divider>
            <Checkbox.Group
              value={exportSelectedGroups}
              onChange={(vals) => {
                setExportSelectedGroups(vals as string[]);
                // Auto-select member scenarios
                const memberNames = new Set(exportSelectedScenarios);
                (vals as string[]).forEach((gn) => {
                  (groups[gn] || []).forEach((m) => memberNames.add(m.name));
                });
                setExportSelectedScenarios([...memberNames]);
              }}
              style={{ display: 'flex', flexDirection: 'column', gap: 3 }}
            >
              {Object.entries(groups).map(([gn, members]) => (
                <Checkbox key={gn} value={gn}>
                  <FolderOutlined /> {gn} ({members.length})
                </Checkbox>
              ))}
            </Checkbox.Group>
          </>
        )}

        <Divider style={{ margin: '8px 0' }}>{t('scenario.title')}</Divider>
        <div style={{ maxHeight: 300, overflowY: 'auto' }}>
          <Checkbox.Group
            value={exportSelectedScenarios}
            onChange={(vals) => setExportSelectedScenarios(vals as string[])}
            style={{ display: 'flex', flexDirection: 'column', gap: 3 }}
          >
            {scenarios.map((sn) => (
              <Checkbox key={sn} value={sn}>{sn}</Checkbox>
            ))}
          </Checkbox.Group>
        </div>
      </Modal>

      {/* ===== 가져오기 모달 ===== */}
      <Modal
        title={t('scenario.importTitle')}
        open={importModalVisible}
        onCancel={() => { setImportModalVisible(false); setImportFile(null); setImportPreviewData(null); }}
        onOk={doImport}
        okText={t('common.import')}
        confirmLoading={importLoading}
        okButtonProps={{ disabled: !importPreviewData }}
        width={650}
      >
        {!importPreviewData ? (
          <Upload.Dragger
            accept=".zip"
            maxCount={1}
            beforeUpload={(file) => { handleImportFile(file); return false; }}
            showUploadList={false}
          >
            <p style={{ fontSize: 33, color: '#999' }}><UploadOutlined /></p>
            <p>{t('scenario.importDragText')}</p>
          </Upload.Dragger>
        ) : (
          <>
            <div style={{ marginBottom: 10 }}>
              <Tag color="blue">{importFile?.name}</Tag>
              <Button size="small" type="link" onClick={() => { setImportFile(null); setImportPreviewData(null); }}>{t('scenario.selectOtherFile')}</Button>
            </div>

            {importPreviewData.scenarios.length > 0 && (
              <>
                <Divider style={{ margin: '8px 0' }}>{t('scenario.title')} ({importPreviewData.scenarios.length})</Divider>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {importPreviewData.scenarios.map((s) => {
                    const key = `s:${s.name}`;
                    const res = importResolutions[key] || { action: 'import' };
                    return (
                      <div key={key} style={{ padding: '6px 8px', background: s.conflict ? 'rgba(255,77,79,0.06)' : 'rgba(82,196,26,0.06)', borderRadius: 6, border: `1px solid ${s.conflict ? '#ff4d4f33' : '#52c41a33'}` }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: s.conflict ? 4 : 0 }}>
                          {s.conflict ? <WarningOutlined style={{ color: '#faad14' }} /> : <CheckCircleOutlined style={{ color: '#52c41a' }} />}
                          <strong>{s.name}</strong>
                          {s.conflict && <Tag color="warning" style={{ marginLeft: 'auto' }}>{t('scenario.nameConflict')}</Tag>}
                        </div>
                        {s.conflict && (
                          <div style={{ marginLeft: 18 }}>
                            <Radio.Group
                              value={res.action}
                              onChange={(e) => setImportResolutions((prev) => ({ ...prev, [key]: { ...prev[key], action: e.target.value } }))}
                              size="small"
                            >
                              <Radio value="skip">{t('scenario.skip')}</Radio>
                              <Radio value="overwrite">{t('scenario.overwrite')}</Radio>
                              <Radio value="rename">{t('scenario.rename')}</Radio>
                            </Radio.Group>
                            {res.action === 'rename' && (
                              <Input
                                size="small"
                                placeholder={t('scenario.newNamePlaceholder')}
                                value={res.new_name || ''}
                                onChange={(e) => setImportResolutions((prev) => ({ ...prev, [key]: { ...prev[key], new_name: e.target.value } }))}
                                style={{ width: 200, marginTop: 3 }}
                              />
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            {importPreviewData.groups.length > 0 && (
              <>
                <Divider style={{ margin: '8px 0' }}>{t('scenario.groupLabel')} ({importPreviewData.groups.length})</Divider>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {importPreviewData.groups.map((g) => {
                    const key = `g:${g.name}`;
                    const res = importResolutions[key] || { action: 'import' };
                    return (
                      <div key={key} style={{ padding: '6px 8px', background: g.conflict ? 'rgba(255,77,79,0.06)' : 'rgba(82,196,26,0.06)', borderRadius: 6, border: `1px solid ${g.conflict ? '#ff4d4f33' : '#52c41a33'}` }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: g.conflict ? 4 : 0 }}>
                          {g.conflict ? <WarningOutlined style={{ color: '#faad14' }} /> : <CheckCircleOutlined style={{ color: '#52c41a' }} />}
                          <FolderOutlined /> <strong>{g.name}</strong>
                          {g.conflict && <Tag color="warning" style={{ marginLeft: 'auto' }}>{t('scenario.nameConflict')}</Tag>}
                        </div>
                        {g.conflict && (
                          <div style={{ marginLeft: 18 }}>
                            <Radio.Group
                              value={res.action}
                              onChange={(e) => setImportResolutions((prev) => ({ ...prev, [key]: { ...prev[key], action: e.target.value } }))}
                              size="small"
                            >
                              <Radio value="skip">{t('scenario.skip')}</Radio>
                              <Radio value="overwrite">{t('scenario.overwrite')}</Radio>
                              <Radio value="merge">{t('scenario.mergeTitle')}</Radio>
                              <Radio value="rename">{t('scenario.rename')}</Radio>
                            </Radio.Group>
                            {res.action === 'rename' && (
                              <Input
                                size="small"
                                placeholder={t('scenario.newNamePlaceholder')}
                                value={res.new_name || ''}
                                onChange={(e) => setImportResolutions((prev) => ({ ...prev, [key]: { ...prev[key], new_name: e.target.value } }))}
                                style={{ width: 200, marginTop: 3 }}
                              />
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </>
        )}
      </Modal>

      {/* 재생 직전 웹캠 index 선택 모달 */}
      <Modal
        open={webcamPickerOpen}
        title={<><VideoCameraOutlined style={{ marginRight: 5 }} />{t('webcam.pickDevice')}</>}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        onOk={() => {
          setWebcamPickerOpen(false);
          const resolve = webcamPickerResolveRef.current;
          webcamPickerResolveRef.current = null;
          resolve?.(webcamPickerValue);
        }}
        onCancel={() => {
          setWebcamPickerOpen(false);
          const resolve = webcamPickerResolveRef.current;
          webcamPickerResolveRef.current = null;
          resolve?.(null);
        }}
        destroyOnClose
        width={420}
      >
        <div style={{ marginBottom: 6, color: '#888', fontSize: 11 }}>
          {t('webcam.pickDeviceHint')}
        </div>
        <Radio.Group
          value={webcamPickerValue}
          onChange={(e) => setWebcamPickerValue(e.target.value)}
          style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
        >
          {webcamPickerDevices.map(d => (
            <Radio key={d.index} value={d.index}>
              <span style={{ fontSize: 11 }}>
                <Tag color="blue" style={{ marginRight: 3 }}>#{d.index}</Tag>
                {d.label}
              </span>
            </Radio>
          ))}
        </Radio.Group>
      </Modal>

      <style>{`
        .row-pass td { background: rgba(82, 196, 26, 0.08) !important; }
        .row-fail td { background: rgba(255, 77, 79, 0.12) !important; }
        .row-error td { background: rgba(255, 122, 69, 0.12) !important; }
        .row-warning td { background: rgba(250, 173, 20, 0.08) !important; }
      `}</style>
    </div>
  );
}
