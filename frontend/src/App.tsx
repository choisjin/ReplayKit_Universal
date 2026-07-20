import { useEffect, useRef, useState } from 'react';
import { App as AntdApp, Button, ConfigProvider, Layout, Menu, Modal, Spin, Tooltip, message, theme } from 'antd';
import {
  BarChartOutlined,
  AppstoreOutlined,
  BookOutlined,
  CloudSyncOutlined,
  DatabaseOutlined,
  DesktopOutlined,
  FolderOpenOutlined,
  FundProjectionScreenOutlined,
  HistoryOutlined,
  LoadingOutlined,
  PlayCircleOutlined,
  SettingOutlined,
  VideoCameraOutlined,
  MessageOutlined,
  NotificationOutlined,
} from '@ant-design/icons';
import axios from 'axios';
import { deviceApi, serverApi } from './services/api';
import { DeviceProvider } from './context/DeviceContext';
import { SettingsProvider, useSettings } from './context/SettingsContext';
import { useTranslation } from './i18n';
import { useWebcam } from './hooks/useWebcam';
import DevicePage from './pages/DevicePage';
import RecordPage from './pages/RecordPage';
import ScenarioPage from './pages/ScenarioPage';
import ResultsPage from './pages/ResultsPage';
import SettingsPage from './pages/SettingsPage';
import ChangelogPage from './pages/ChangelogPage';
import AdminPage from './pages/AdminPage';
import WebcamPip from './components/WebcamPip';
import CompositorEditor from './components/CompositorEditor';
import AnnouncementBanner from './components/AnnouncementBanner';
import AnnouncementListModal from './components/AnnouncementListModal';
import { AnnouncementsProvider, useAnnouncements } from './context/AnnouncementsContext';
import PlaybackStatusBanner from './components/PlaybackStatusBanner';
import ChatWidget from './components/ChatWidget';
import { WebcamProvider } from './context/WebcamContext';

const { Sider, Content } = Layout;

// 전체 UI 스케일 80% — AntD 기본 토큰 × 0.8 (파생 토큰은 자동 스케일)
const SCALE_TOKENS = {
  fontSize: 12,          // AntD 기본 텍스트 크기
  sizeUnit: 3,           // 4  → 3.2  → 3
  sizeStep: 3,           // 4  → 3.2  → 3
  controlHeight: 26,     // 32 → 25.6 → 26
  borderRadius: 5,       // 6  → 4.8  → 5
  lineWidth: 1,
} as const;

const pageKeys = [
  { key: '/', icon: <DesktopOutlined />, labelKey: 'nav.device' as const, component: <DevicePage /> },
  { key: '/record', icon: <VideoCameraOutlined />, labelKey: 'nav.record' as const, component: <RecordPage /> },
  { key: '/scenarios', icon: <PlayCircleOutlined />, labelKey: 'nav.scenario' as const, component: <ScenarioPage /> },
  { key: '/results', icon: <BarChartOutlined />, labelKey: 'nav.results' as const, component: <ResultsPage /> },
  { key: '/settings', icon: <SettingOutlined />, labelKey: 'nav.settings' as const, component: <SettingsPage /> },
  { key: '/changelog', icon: <HistoryOutlined />, labelKey: 'nav.changelog' as const, component: <ChangelogPage /> },
];

// 메뉴에 노출되지 않는 숨김 페이지 (URL hash로 접근)
const HIDDEN_ADMIN_KEY = '/admin';

function AppContent() {
  // URL hash가 #admin이면 AdminPage로 초기화 (새로고침 시에도 유지)
  const initialKey = (typeof window !== 'undefined' && window.location.hash === '#admin') ? HIDDEN_ADMIN_KEY : '/';
  const [activeKey, setActiveKey] = useState(initialKey);

  // hash 변경 감지 — 사용자가 주소창에서 #admin / #admin 해제 시 즉시 전환
  useEffect(() => {
    const onHashChange = () => {
      if (window.location.hash === '#admin') setActiveKey(HIDDEN_ADMIN_KEY);
      else if (activeKey === HIDDEN_ADMIN_KEY) setActiveKey('/');
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [siderCollapsed, setSiderCollapsed] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [diskInfoList, setDiskInfoList] = useState<{ drive: string; free_gb: number; total_gb: number; used_percent: number }[]>([]);
  const [appVersion, setAppVersion] = useState<string>('');
  // boot_id: 백엔드 프로세스 시작 시점에 생성되는 unique id. version.txt 가 안 바뀌어도
  // 서버가 재시작되면 이 값이 바뀌므로 브라우저 reload 트리거에 사용.
  const [appBootId, setAppBootId] = useState<string>('');
  // 페이지 로드 시점의 (version, boot_id) — 이후 폴링으로 다른 값이 오면 reload.
  const initialVersionRef = useRef<string>('');
  const initialBootIdRef = useRef<string>('');
  const reloadingRef = useRef<boolean>(false);
  const { settings, uploadWebcamRecording, fetchSettings } = useSettings();
  const { openList: openAnnouncements } = useAnnouncements();
  const { t } = useTranslation();

  // 백엔드 version 또는 boot_id 가 페이지 로드 후 바뀌면 강제 새로고침.
  //   - version 변경: 빌드 업데이트 (git pull / apt upgrade)
  //   - boot_id 변경: 서버 재시작 (version 변경 없는 hotfix 도 커버)
  // 사용자에겐 1회 안내 후 reload — reloadingRef 로 무한 reload 방지.
  useEffect(() => {
    if (!appVersion && !appBootId) return;
    if (!initialVersionRef.current && !initialBootIdRef.current) {
      initialVersionRef.current = appVersion;
      initialBootIdRef.current = appBootId;
      return;
    }
    const versionChanged = appVersion && initialVersionRef.current && appVersion !== initialVersionRef.current;
    const bootChanged = appBootId && initialBootIdRef.current && appBootId !== initialBootIdRef.current;
    if ((versionChanged || bootChanged) && !reloadingRef.current) {
      reloadingRef.current = true;
      const reason = versionChanged
        ? `version ${initialVersionRef.current} → ${appVersion}`
        : `boot ${initialBootIdRef.current} → ${appBootId} (server restarted)`;
      console.log('[update] backend changed —', reason, '· reloading');
      // query string 추가로 추가 캐시 무효화 + 새 index.html 강제 fetch
      try {
        const url = new URL(window.location.href);
        url.searchParams.set('_v', appVersion || 'na');
        url.searchParams.set('_b', appBootId.slice(0, 12) || 'na');
        window.location.replace(url.toString());
      } catch {
        window.location.reload();
      }
    }
  }, [appVersion, appBootId]);

  // ── 로고 5연타 → 런처 로그 ──
  const logoClickRef = useRef<number[]>([]);
  const [logModalOpen, setLogModalOpen] = useState(false);
  const [launcherLog, setLauncherLog] = useState<string[]>([]);
  const [logDates, setLogDates] = useState<string[]>([]);
  const [logSelectedDate, setLogSelectedDate] = useState('');
  const [logSource, setLogSource] = useState<'launcher' | 'backend'>('backend');

  const loadLog = (date?: string, source?: string) => {
    const src = source ?? logSource;
    serverApi.launcherLog(1000, date, src).then(res => {
      setLauncherLog(res.data.lines || []);
      setLogDates(res.data.dates || []);
      if (!date && res.data.dates?.length > 0) setLogSelectedDate(res.data.dates[0]);
    }).catch(() => {});
  };

  const handleLogoClick = () => {
    const now = Date.now();
    const clicks = logoClickRef.current;
    clicks.push(now);
    while (clicks.length > 0 && now - clicks[0] > 2000) clicks.shift();
    if (clicks.length >= 5) {
      clicks.length = 0;
      loadLog();
      setLogModalOpen(true);
    }
  };

  // --- Backend health polling ---
  const [backendReady, setBackendReady] = useState(false);
  // 한 번이라도 ready였는지 — true면 이후 일시적 health 실패에도 페이지를 언마운트하지 않고
  // (상태 보존) 재연결 배너만 오버레이한다. 초기 콜드 스타트에서만 전체화면 스피너를 쓴다.
  const [everReady, setEverReady] = useState(false);
  const readyRef = useRef(false);
  const everReadyRef = useRef(false);
  // health 연속 실패 카운트 — 단발 타임아웃(무거운 동기작업으로 이벤트 루프가 잠깐 막힘)에
  // 페이지가 통째로 blank/리마운트되던 문제 방지. 이 임계 이상 연속 실패해야 not-ready 처리.
  const failCountRef = useRef(0);
  const HEALTH_FAIL_LIMIT = 3;
  const activeKeyRef = useRef(activeKey);
  activeKeyRef.current = activeKey;

  useEffect(() => {
    let mounted = true;

    const poll = async () => {
      if (!mounted) return;
      try {
        await axios.get('/api/health', { timeout: 5000 });
        failCountRef.current = 0;
        if (!readyRef.current) {
          readyRef.current = true;
          await fetchSettings();
          setBackendReady(true);
          setEverReady(true);
          everReadyRef.current = true;
          // 디스크 용량 조회
          serverApi.diskUsage().then(res => {
            const data = res.data;
            // 배열이면 그대로, 단일 객체면 배열로 래핑 (하위호환)
            setDiskInfoList(Array.isArray(data) ? data : [data]);
          }).catch(() => {});
          // 버전 + boot_id 조회. boot_id 가 바뀌면 서버 재시작 → useEffect 가 reload.
          serverApi.getVersion().then(res => {
            setAppVersion(res.data?.version || '');
            setAppBootId(res.data?.boot_id || '');
          }).catch(() => {});
        }
      } catch {
        // 단발 실패는 무시(이벤트 루프가 무거운 동기작업으로 잠깐 막힌 경우). 연속 실패가
        // 임계 이상일 때만 not-ready 처리 → 페이지 blank/리마운트 대신 재연결 배너로 표시.
        failCountRef.current += 1;
        if (readyRef.current && failCountRef.current >= HEALTH_FAIL_LIMIT) {
          readyRef.current = false;
          setBackendReady(false);
        }
      }
      if (mounted) {
        // 정상(연속실패 0)일 때만 10s 저빈도. 한 번이라도 실패하면 2s로 당겨
        // 임계 도달/복구를 빠르게 수렴시킨다.
        const healthy = readyRef.current && failCountRef.current === 0;
        setTimeout(poll, healthy ? 10000 : 2000);
      }
    };

    poll();
    return () => { mounted = false; };
  }, []);

  // Global webcam
  const webcam = useWebcam();
  const [webcamVisible, setWebcamVisible] = useState(false);
  // Compositor(다중 캡처 합성 녹화) 모달 — 웹캠 PiP 설정창에서 진입.
  const [compositorOpen, setCompositorOpen] = useState(false);

  // Wire up webcam upload when save dir is configured
  useEffect(() => {
    if (settings.webcam_save_dir) {
      webcam.setUploadFn(uploadWebcamRecording);
    } else {
      webcam.setUploadFn(null);
    }
  }, [settings.webcam_save_dir, webcam.setUploadFn, uploadWebcamRecording]);

  const toggleWebcam = () => {
    if (webcamVisible) {
      webcam.stopWebcam();
      setWebcamVisible(false);
    } else {
      webcam.handleWebcamToggle(['webcam']);
      setWebcamVisible(true);
    }
  };

  /** 웹캠 PiP 열기 + 실제 비디오 스트림 준비 대기. 이미 스트림 활성이면 즉시 true.
   *  deviceIndex를 전달하면 해당 index로 전환 후 오픈한다 (다른 index가 이미 열려 있으면 교체).
   */
  const ensureWebcamOpen = async (deviceIndex?: number): Promise<boolean> => {
    // 지정된 index가 있고, 현재 열린 index와 다르면 먼저 해당 index로 전환
    if (deviceIndex !== undefined && deviceIndex !== webcam.webcamIndex) {
      try {
        await webcam.startWebcam(deviceIndex);
      } catch { /* fall through */ }
      if (!webcamVisible) setWebcamVisible(true);
    } else if (webcamVisible && webcam.isStreamReady()) {
      // 이미 원하는 index로 스트림이 준비되어 있으면 즉시 성공
      return true;
    } else if (!webcamVisible) {
      // PiP 닫혀있으면 열기 (현재 webcamIndex 사용)
      webcam.handleWebcamToggle(['webcam']);
      setWebcamVisible(true);
    }
    // 실제 비디오 스트림이 live 상태가 될 때까지 대기 (최대 15초)
    for (let i = 0; i < 75; i++) {
      await new Promise(r => setTimeout(r, 200));
      if (webcam.isStreamReady()) return true;
    }
    return false;
  };

  const pages = pageKeys.map(p => ({ ...p, label: t(p.labelKey) }));
  const menuItems = pages.map(({ key, icon, label }) => ({ key, icon, label }));

  const isDark = settings.theme === 'dark';
  const themeAlgorithm = isDark ? theme.darkAlgorithm : theme.defaultAlgorithm;
  const contentBg = isDark ? '#1f1f1f' : '#e8e8e8';
  const layoutBg = isDark ? undefined : '#d0d0d0';

  // 다크 모드에서 body 에 클래스 부여 — 인라인 hardcoded gray (예: '#888') 들을
  // 글로벌 CSS 로 일괄 밝게 보정 (아래 <style> block 참조).
  useEffect(() => {
    document.body.classList.toggle('dark-theme', isDark);
    document.body.classList.toggle('light-theme', !isDark);
  }, [isDark]);

  return (
    <WebcamProvider webcam={webcam} webcamVisible={webcamVisible} ensureWebcamOpen={ensureWebcamOpen}>
    <ConfigProvider theme={{
      algorithm: themeAlgorithm,
      token: {
        ...SCALE_TOKENS,
        ...(!isDark ? {
          colorBgContainer: '#e8e8e8',
          colorBgElevated: '#e0e0e0',
          colorBgLayout: '#d0d0d0',
          colorBgBase: '#dcdcdc',
        } : {}),
      },
    }}>
      <Layout style={{ minHeight: '100vh' }}>
        <Sider collapsible collapsed={siderCollapsed} onCollapse={setSiderCollapsed} style={isDark ? undefined : { background: '#f0f0f0' }}>
          <div
            onClick={handleLogoClick}
            style={{ height: 40, margin: siderCollapsed ? '16px 8px' : 16, color: isDark ? '#fff' : '#222', fontSize: siderCollapsed ? 11 : 14, fontWeight: 'bold', textAlign: 'center', lineHeight: '40px', overflow: 'hidden', whiteSpace: 'nowrap', cursor: 'default', userSelect: 'none' }}
          >
            {siderCollapsed ? 'RK' : 'ReplayKit'}
          </div>
          <Menu
            theme={isDark ? 'dark' : 'light'}
            mode="inline"
            selectedKeys={[activeKey]}
            items={menuItems}
            onClick={async ({ key }) => {
              if (activeKey === '/record' && key !== '/record') {
                const check = (window as any).__recordPageDirtyCheck;
                if (check) { const ok = await check(); if (!ok) return; }
              }
              setActiveKey(key);
              window.dispatchEvent(new CustomEvent('tab-change', { detail: key }));
            }}
            style={isDark ? undefined : { background: '#f0f0f0' }}
          />
          <div style={{ padding: siderCollapsed ? '12px 8px' : '12px 16px', display: 'flex', flexDirection: 'column', gap: 5 }}>
            <Tooltip title={t('webcam.title')} placement="right">
              <Button
                block
                type={webcamVisible ? 'primary' : 'default'}
                icon={<VideoCameraOutlined />}
                onClick={toggleWebcam}
                style={webcamVisible && webcam.webcamRecording ? { animation: 'blink 1s infinite' } : undefined}
              >
                {!siderCollapsed && t('webcam.title')}
              </Button>
            </Tooltip>
            <Tooltip title={t('dlt.launchViewer')} placement="right">
              <Button
                block
                icon={<FundProjectionScreenOutlined />}
                onClick={async () => {
                  try {
                    await deviceApi.dltViewerLaunch();
                    message.success(t('dlt.launchViewer'));
                  } catch (e: any) {
                    message.error(e.response?.data?.detail || 'DLT Viewer launch failed');
                  }
                }}
              >
                {!siderCollapsed && t('dlt.launchViewer')}
              </Button>
            </Tooltip>
            <Tooltip title={t('server.update')} placement="right">
              <Button
                block
                icon={<CloudSyncOutlined />}
                onClick={() => {
                  Modal.confirm({
                    title: t('server.updateConfirm'),
                    content: t('server.updateDesc'),
                    okText: t('server.update'),
                    onOk: async () => {
                      message.loading({ content: t('server.updating'), key: 'update', duration: 0 });
                      try {
                        const updRes = await serverApi.updateAndRestart();
                        message.destroy('update');
                        // 백엔드의 pull 결과 — 사용자가 "업데이트 됨 / 변경 없음 / 실패" 를
                        // 명확히 인지하도록 콘솔과 화면에 표시.
                        const pull = (updRes?.data as any)?.pull || {};
                        console.log('[update] pull result:', pull);

                        // 업데이트 대기 화면 표시
                        const wrap = document.createElement('div');
                        wrap.style.cssText = 'display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap: 13px;font-family:sans-serif';
                        const title = document.createElement('div');
                        title.style.cssText = 'font-size:20px;font-weight:600';
                        title.textContent = '서버 업데이트 중...';
                        const status = document.createElement('div');
                        status.style.color = '#888';
                        status.textContent = '서버 종료 대기 중';
                        const pullInfo = document.createElement('div');
                        pullInfo.style.cssText = 'font-size:12px;color:#666;max-width:560px;text-align:center;font-family:monospace;white-space:pre-wrap';
                        if (pull.performed) {
                          pullInfo.textContent = `✓ git pull 성공 (mode: ${pull.mode}${pull.detail ? ' — ' + pull.detail : ''})`;
                          pullInfo.style.color = '#52c41a';
                        } else if (pull.mode && pull.mode !== 'none') {
                          pullInfo.textContent = `⚠ git pull 실패 (mode: ${pull.mode})\n${pull.error || pull.detail || '원인 불명'}\n→ 서버는 그대로 재시작됩니다 (코드 변경 없음)`;
                          pullInfo.style.color = '#fa8c16';
                        }
                        wrap.appendChild(title);
                        wrap.appendChild(status);
                        if (pullInfo.textContent) wrap.appendChild(pullInfo);
                        document.body.innerHTML = '';
                        document.body.appendChild(wrap);
                        // 서버가 다시 응답할 때까지 폴링
                        const delay = (ms: number) => new Promise(r => setTimeout(r, ms));
                        await delay(3000);
                        status.textContent = 'git pull + 서버 재시작 대기 중...';
                        for (let waited = 0; waited < 120; waited += 2) {
                          try {
                            const r = await fetch('/api/health', { signal: AbortSignal.timeout(2000) });
                            if (r.ok) {
                              status.textContent = '서버 준비 완료! 새로고침 중...';
                              await delay(1000);
                              window.location.reload();
                              return;
                            }
                          } catch { /* 서버 아직 안 뜸 */ }
                          await delay(2000);
                          status.textContent = '서버 재시작 대기 중... (' + (waited + 2) + 's)';
                        }
                        status.textContent = '서버 응답 없음 — 수동으로 새로고침 해주세요.';
                      } catch {
                        message.error({ content: t('server.updateFailed'), key: 'update' });
                      }
                    },
                  });
                }}
              >
                {!siderCollapsed && t('server.update')}
              </Button>
            </Tooltip>
            <Tooltip title={t('server.guide')} placement="right">
              <Button
                block
                icon={<BookOutlined />}
                onClick={() => window.open(settings.language === 'en' ? '/docs/user-guide-en.html' : '/docs/user-guide.html', '_blank')}
              >
                {!siderCollapsed && t('server.guide')}
              </Button>
            </Tooltip>
            <Tooltip title={t('server.moduleGuide')} placement="right">
              <Button
                block
                icon={<AppstoreOutlined />}
                onClick={() => window.open(settings.language === 'en' ? '/docs/module-guide-en.html' : '/docs/module-guide.html', '_blank')}
              >
                {!siderCollapsed && t('server.moduleGuide')}
              </Button>
            </Tooltip>
            <Tooltip title="Results 폴더 열기" placement="right">
              <Button
                block
                icon={<FolderOpenOutlined />}
                onClick={async () => {
                  try {
                    await serverApi.openResultsFolder();
                  } catch (e: any) {
                    message.error(e.response?.data?.detail || 'Failed to open folder');
                  }
                }}
              >
                {!siderCollapsed && 'Results 폴더'}
              </Button>
            </Tooltip>
            <Tooltip title={t('announce.title')} placement="right">
              <Button
                block
                icon={<NotificationOutlined />}
                onClick={() => openAnnouncements()}
              >
                {!siderCollapsed && t('announce.title')}
              </Button>
            </Tooltip>
            <Tooltip title={t('chat.title')} placement="right">
              <Button
                block
                icon={<MessageOutlined />}
                onClick={() => {
                  Modal.info({
                    title: '문의 안내',
                    content: (
                      <div style={{ fontSize: 12, lineHeight: 1.8 }}>
                        요청사항 및 문의사항은 제목에<br />
                        <b style={{ color: '#1677ff' }}>[ReplayKit]</b> 붙여 이슈 등록 해주세요!
                      </div>
                    ),
                    okText: '확인',
                  });
                }}
              >
                {!siderCollapsed && t('chat.title')}
              </Button>
            </Tooltip>
          </div>
          {diskInfoList.length > 0 && (
            <div style={{ padding: siderCollapsed ? '8px 4px' : '8px 16px', borderTop: '1px solid rgba(255,255,255,0.1)' }}>
              {diskInfoList.map((di) => (
              <Tooltip key={di.drive} title={`${di.drive} — ${di.free_gb} GB 사용가능 / ${di.total_gb} GB`} placement="right">
                <div style={{ fontSize: 11, color: '#888', marginBottom: diskInfoList.length > 1 ? 6 : 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                    {!siderCollapsed && <span style={{ whiteSpace: 'nowrap', minWidth: 24 }}><DatabaseOutlined /> {di.drive.replace(':', '')}</span>}
                    <div style={{ flex: 1, background: '#333', borderRadius: 4, height: 6, overflow: 'hidden' }}>
                      <div style={{ background: di.used_percent > 90 ? '#ff4d4f' : di.used_percent > 70 ? '#faad14' : '#52c41a', width: `${di.used_percent}%`, height: '100%' }} />
                    </div>
                  </div>
                  <div style={{ marginTop: 2, fontSize: 11, textAlign: 'center' }}>
                    {siderCollapsed ? `${di.free_gb}G` : `${di.free_gb} GB 사용가능`}
                  </div>
                </div>
              </Tooltip>
              ))}
            </div>
          )}
          {appVersion && (
            <div style={{
              padding: siderCollapsed ? '6px 4px' : '6px 16px',
              borderTop: '1px solid rgba(255,255,255,0.1)',
              fontSize: 10,
              color: '#888',
              textAlign: 'center',
              userSelect: 'none',
            }}>
              {appVersion}
            </div>
          )}
        </Sider>
        <Layout style={layoutBg ? { background: layoutBg } : undefined}>
          <Content style={{ margin: 6, padding: 10, background: contentBg, borderRadius: 8 }}>
            <AnnouncementBanner />
            <PlaybackStatusBanner />
            {everReady ? (
              // 한 번이라도 연결됐으면 페이지는 항상 마운트 유지(로컬 상태 보존).
              // 일시적 health 실패 시엔 언마운트 대신 얇은 재연결 배너만 띄운다 —
              // 무거운 백엔드 작업(디바이스 해제/스캔)으로 이벤트 루프가 잠깐 막혀도
              // 화면이 통째로 새로고침되던 현상 방지.
              <>
                {!backendReady && (
                  <div style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
                    padding: '6px 12px', marginBottom: 6, borderRadius: 6,
                    background: isDark ? '#3b2f1e' : '#fff7e6',
                    border: `1px solid ${isDark ? '#5c4820' : '#ffd591'}`,
                    color: isDark ? '#ffd591' : '#ad6800', fontSize: 12,
                  }}>
                    <LoadingOutlined spin />
                    {t('common.backendConnecting')}
                  </div>
                )}
                {pages.map(({ key, component }) => (
                  <div key={key} style={{ display: activeKey === key ? 'block' : 'none' }}>
                    {component}
                  </div>
                ))}
                {/* 숨김 admin 페이지 — URL hash #admin 으로 접근 */}
                <div style={{ display: activeKey === HIDDEN_ADMIN_KEY ? 'block' : 'none' }}>
                  <AdminPage />
                </div>
              </>
            ) : (
              <div style={{
                display: 'flex', flexDirection: 'column',
                justifyContent: 'center', alignItems: 'center',
                height: 'calc(100vh - 48px)', gap: 19,
              }}>
                <Spin indicator={<LoadingOutlined style={{ fontSize: 39 }} spin />} />
                <div style={{ color: '#888', fontSize: 14 }}>
                  {t('common.backendConnecting')}
                </div>
              </div>
            )}
          </Content>
        </Layout>
      </Layout>

      {webcamVisible && (
        <WebcamPip
          webcam={webcam}
          onClose={toggleWebcam}
          isDark={isDark}
          onOpenCompositor={() => setCompositorOpen(true)}
        />
      )}

      <CompositorEditor open={compositorOpen} onClose={() => setCompositorOpen(false)} isDark={isDark} />

      <AnnouncementListModal />

      <ChatWidget open={chatOpen} onClose={() => setChatOpen(false)} />

      <Modal
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span>Log</span>
            <select
              value={logSource}
              onChange={(e) => { const s = e.target.value as 'launcher' | 'backend'; setLogSource(s); setLogSelectedDate(''); loadLog('', s); }}
              style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, border: '1px solid #d9d9d9' }}
            >
              <option value="backend">Backend</option>
              <option value="launcher">Launcher</option>
            </select>
            {logDates.length > 0 && (
              <select
                value={logSelectedDate}
                onChange={(e) => { setLogSelectedDate(e.target.value); loadLog(e.target.value); }}
                style={{ fontSize: 11, padding: '2px 6px', borderRadius: 4, border: '1px solid #d9d9d9' }}
              >
                {logDates.map(d => <option key={d} value={d}>{d}</option>)}
              </select>
            )}
          </div>
        }
        open={logModalOpen}
        onCancel={() => setLogModalOpen(false)}
        footer={null}
        width={800}
      >
        <div style={{ maxHeight: 500, overflow: 'auto', background: isDark ? '#1e1e2e' : '#f5f5f5', borderRadius: 6, padding: 10 }}>
          <pre style={{ margin: 0, fontSize: 10, fontFamily: 'Consolas, monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-all', color: isDark ? '#cdd6f4' : '#333' }}>
            {launcherLog.length > 0 ? launcherLog.join('\n') : 'No logs'}
          </pre>
        </div>
      </Modal>

      <style>{`
        @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

        /* ─── 다크 모드 회색 폰트 가시성 보정 ───────────────────────────────
           인라인 style 의 hardcoded 회색 (color: #888, #999, #666 등) 을 글로벌하게
           밝게 끌어올림. attribute selector + !important 로 inline style 우선순위 극복.
           각 컴포넌트마다 isDark 분기 다시 쓰지 않고 일괄 적용. 라이트 모드에선 적용 안 됨.
        */
        body.dark-theme [style*="color: #666"],
        body.dark-theme [style*="color:#666"] { color: #a8a8a8 !important; }
        body.dark-theme [style*="color: #777"],
        body.dark-theme [style*="color:#777"] { color: #b0b0b0 !important; }
        body.dark-theme [style*="color: #888"],
        body.dark-theme [style*="color:#888"] { color: #bababa !important; }
        body.dark-theme [style*="color: #999"],
        body.dark-theme [style*="color:#999"] { color: #c2c2c2 !important; }
        body.dark-theme [style*="color: #aaa"],
        body.dark-theme [style*="color:#aaa"] { color: #cacaca !important; }
        body.dark-theme [style*="color: #bbb"],
        body.dark-theme [style*="color:#bbb"] { color: #d0d0d0 !important; }
        body.dark-theme [style*="color: #ccc"],
        body.dark-theme [style*="color:#ccc"] { color: #d6d6d6 !important; }
      `}</style>
    </ConfigProvider>
    </WebcamProvider>
  );
}

function App() {
  return (
    <ConfigProvider theme={{ algorithm: theme.darkAlgorithm, token: { ...SCALE_TOKENS } }}>
      <AntdApp>
        <SettingsProvider>
          <DeviceProvider>
            <AnnouncementsProvider>
              <AppContent />
            </AnnouncementsProvider>
          </DeviceProvider>
        </SettingsProvider>
      </AntdApp>
    </ConfigProvider>
  );
}

export default App;
