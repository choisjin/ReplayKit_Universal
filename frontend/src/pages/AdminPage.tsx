import { useEffect, useState } from 'react';
import { Button, Card, Checkbox, Collapse, Divider, Empty, Input, message, Modal, Select, Space, Tag, Typography } from 'antd';
import { DeleteOutlined, LockOutlined, LogoutOutlined, PlusOutlined, ReloadOutlined, SaveOutlined, UserOutlined } from '@ant-design/icons';
import { deviceApi } from '../services/api';
import MemoryMonitor from '../components/MemoryMonitor';
import { useSettings } from '../context/SettingsContext';

// 하드코딩된 관리자 자격증명 — 실수 방지 목적의 게이트
// (클라이언트 측 검증이므로 기술적 엄격 보안 아님; 소스 확인 가능 유저 대상이 아님을 전제)
const ADMIN_ID = 'admin';
const ADMIN_PW = 'replaykitadmin';
const ADMIN_AUTH_KEY = 'rk_admin_authed';

interface Model {
  value: string;   // 표시 텍스트이자 Device ID prefix로 사용
  enabled: boolean;
  agent?: string;  // 할당된 에이전트 name (조작 방식 결정)
}

interface Project {
  name: string;
  enabled: boolean;
  models: Model[];
}

interface Agent {
  name: string;
  type: string;   // 내부 device type (adb/hkmc_agent/isap_agent/vision_camera/webcam) — 읽기 전용
  enabled: boolean;
}

interface Catalog {
  projects: Project[];
  module_visibility: Record<string, boolean>;
  agents: Agent[];
}

interface ModuleInfo {
  name: string;
  label: string;
}

/**
 * 관리자 전용 카탈로그 편집 페이지.
 *  - 프로젝트 / 모델명 콤보 편집 (DevicePage에서 사용)
 *  - 모듈 표시여부 (체크박스로 숨김 처리 가능)
 *  - 스캔 설정은 DevicePage의 "스캔 설정" 모달을 계속 사용 (중복 UI 안 만듦)
 * 접근 경로: URL hash `#admin` (메뉴에 노출되지 않음).
 */
export default function AdminPage() {
  // 로그인 게이트 — sessionStorage 기반 (탭 닫으면 재인증)
  const [authed, setAuthed] = useState(() => {
    try { return sessionStorage.getItem(ADMIN_AUTH_KEY) === '1'; } catch { return false; }
  });
  const [loginId, setLoginId] = useState('');
  const [loginPw, setLoginPw] = useState('');
  const [loginError, setLoginError] = useState('');

  const handleLogin = () => {
    if (loginId.trim() === ADMIN_ID && loginPw === ADMIN_PW) {
      try { sessionStorage.setItem(ADMIN_AUTH_KEY, '1'); } catch { /* ignore */ }
      setAuthed(true);
      setLoginError('');
      setLoginPw('');
    } else {
      setLoginError('ID 또는 비밀번호가 올바르지 않습니다.');
    }
  };

  const handleLogout = () => {
    try { sessionStorage.removeItem(ADMIN_AUTH_KEY); } catch { /* ignore */ }
    setAuthed(false);
    setLoginId('');
    setLoginPw('');
  };

  // 관제 서버 URL — 이 PC 의 재생 상태/함수통계를 매니저 서버로 보고할 대상.
  const { settings, updateSettings } = useSettings();
  const [monitorUrl, setMonitorUrl] = useState('');
  const [savingMonitor, setSavingMonitor] = useState(false);
  useEffect(() => { setMonitorUrl(settings.monitor_server_url || ''); }, [settings.monitor_server_url]);
  const saveMonitorUrl = async () => {
    setSavingMonitor(true);
    try {
      await updateSettings({ monitor_server_url: monitorUrl.trim() });
      message.success(monitorUrl.trim() ? '관제 서버 연동 저장 — 곧 연결됩니다' : '관제 서버 연동 해제됨');
    } catch (e: any) {
      message.error('저장 실패: ' + (e.response?.data?.detail || e.message));
    } finally {
      setSavingMonitor(false);
    }
  };

  const [catalog, setCatalog] = useState<Catalog>({ projects: [], module_visibility: {}, agents: [] });
  const [modules, setModules] = useState<ModuleInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  // 메모리 모니터 활성 여부 — #admin에 실제로 머물러 있을 때만 폴링
  const [memoryActive, setMemoryActive] = useState(
    typeof window !== 'undefined' && window.location.hash === '#admin'
  );
  useEffect(() => {
    const onHash = () => setMemoryActive(window.location.hash === '#admin');
    const onTab = (e: Event) => {
      const key = (e as CustomEvent).detail;
      setMemoryActive(window.location.hash === '#admin' && key === '/admin');
    };
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') setMemoryActive(false);
      else if (window.location.hash === '#admin') setMemoryActive(true);
    };
    window.addEventListener('hashchange', onHash);
    window.addEventListener('tab-change', onTab);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('hashchange', onHash);
      window.removeEventListener('tab-change', onTab);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  const load = async () => {
    setLoading(true);
    try {
      const [catRes, modRes] = await Promise.all([
        deviceApi.getCatalog(),
        deviceApi.listModules(),
      ]);
      const cat = catRes.data || {};
      setCatalog({
        projects: Array.isArray(cat.projects) ? cat.projects : [],
        module_visibility: cat.module_visibility || {},
        agents: Array.isArray(cat.agents) ? cat.agents : [],
      });
      setModules((modRes.data.modules || []).sort((a: ModuleInfo, b: ModuleInfo) => (a.label || a.name || '').localeCompare(b.label || b.name || '')));
      setDirty(false);
    } catch (e: any) {
      message.error('카탈로그 로드 실패: ' + (e.response?.data?.detail || e.message));
    } finally {
      setLoading(false);
    }
  };

  // 인증된 후에만 카탈로그 로드 (로그인 전 API 호출 방지)
  useEffect(() => { if (authed) load(); }, [authed]);

  const markDirty = () => setDirty(true);

  const save = async () => {
    setSaving(true);
    try {
      await deviceApi.saveCatalog(catalog);
      message.success('저장 완료');
      setDirty(false);
    } catch (e: any) {
      message.error('저장 실패: ' + (e.response?.data?.detail || e.message));
    } finally {
      setSaving(false);
    }
  };

  // --- Project / Model helpers ---
  const updateProject = (idx: number, patch: Partial<Project>) => {
    setCatalog(c => ({ ...c, projects: c.projects.map((p, i) => i === idx ? { ...p, ...patch } : p) }));
    markDirty();
  };
  const removeProject = (idx: number) => {
    Modal.confirm({
      title: `'${catalog.projects[idx].name}' 프로젝트 삭제`,
      content: '되돌릴 수 없습니다.',
      okText: '삭제', okType: 'danger', cancelText: '취소',
      onOk: () => {
        setCatalog(c => ({ ...c, projects: c.projects.filter((_, i) => i !== idx) }));
        markDirty();
      },
    });
  };
  const addProject = () => {
    const name = prompt('새 프로젝트 이름');
    if (!name || !name.trim()) return;
    const trimmed = name.trim();
    if (catalog.projects.some(p => p.name === trimmed)) {
      message.warning('이미 존재하는 프로젝트 이름');
      return;
    }
    setCatalog(c => ({ ...c, projects: [...c.projects, { name: trimmed, enabled: true, models: [] }] }));
    markDirty();
  };
  const updateModel = (projIdx: number, modelIdx: number, patch: Partial<Model>) => {
    setCatalog(c => ({
      ...c,
      projects: c.projects.map((p, i) => i === projIdx ? {
        ...p,
        models: p.models.map((m, mi) => mi === modelIdx ? { ...m, ...patch } : m),
      } : p),
    }));
    markDirty();
  };
  const removeModel = (projIdx: number, modelIdx: number) => {
    setCatalog(c => ({
      ...c,
      projects: c.projects.map((p, i) => i === projIdx ? {
        ...p,
        models: p.models.filter((_, mi) => mi !== modelIdx),
      } : p),
    }));
    markDirty();
  };
  const addModel = (projIdx: number) => {
    const value = prompt('모델 이름 (Device ID prefix, 예: ccRC / Gen6 Premium / GVM)');
    if (!value || !value.trim()) return;
    const trimmed = value.trim();
    const p = catalog.projects[projIdx];
    if (p.models.some(m => m.value === trimmed)) {
      message.warning('이미 존재하는 이름');
      return;
    }
    setCatalog(c => ({
      ...c,
      projects: c.projects.map((pp, i) => i === projIdx ? {
        ...pp,
        models: [...pp.models, { value: trimmed, enabled: true }],
      } : pp),
    }));
    markDirty();
  };

  // --- Module visibility ---
  const setModuleVisible = (name: string, visible: boolean) => {
    setCatalog(c => ({ ...c, module_visibility: { ...c.module_visibility, [name]: visible } }));
    markDirty();
  };
  const isModuleVisible = (name: string) => catalog.module_visibility[name] !== false; // 기본값 true

  // --- Agent ---
  const updateAgent = (idx: number, patch: Partial<Agent>) => {
    setCatalog(c => ({ ...c, agents: c.agents.map((a, i) => i === idx ? { ...a, ...patch } : a) }));
    markDirty();
  };
  const enabledAgentOptions = catalog.agents
    .filter(a => a.enabled !== false)
    .map(a => ({ label: a.name, value: a.name }));

  // ───── 로그인 화면 (인증 전) ─────
  if (!authed) {
    return (
      <div style={{ maxWidth: 340, margin: '80px auto' }}>
        <Card size="small" title={<Space><LockOutlined /><Typography.Text strong>Admin 로그인</Typography.Text></Space>}>
          <Space direction="vertical" style={{ width: '100%' }} size={10}>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              관리자 전용 페이지입니다. ID와 비밀번호를 입력하세요.
            </Typography.Text>
            <Input
              prefix={<UserOutlined />}
              placeholder="ID"
              value={loginId}
              onChange={(e) => setLoginId(e.target.value)}
              onPressEnter={handleLogin}
              autoFocus
            />
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="비밀번호"
              value={loginPw}
              onChange={(e) => setLoginPw(e.target.value)}
              onPressEnter={handleLogin}
            />
            {loginError && (
              <Typography.Text type="danger" style={{ fontSize: 12 }}>{loginError}</Typography.Text>
            )}
            <Button type="primary" block onClick={handleLogin}>로그인</Button>
          </Space>
        </Card>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
      <Card
        size="small"
        title={<Space><Typography.Text strong>디바이스 카탈로그 관리</Typography.Text><Tag color="orange">Admin</Tag></Space>}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={load} disabled={loading}>새로고침</Button>
            <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} disabled={!dirty}>저장</Button>
            <Button icon={<LogoutOutlined />} onClick={handleLogout} danger>로그아웃</Button>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          DevicePage의 프로젝트·모델 콤보와 모듈 목록에 반영됩니다. 체크 해제된 항목은 사용자에게 노출되지 않습니다.
          접근 경로: URL hash <code>#admin</code>. 메뉴에는 표시되지 않습니다.
        </Typography.Paragraph>
      </Card>

      {/* 관제 서버 연동 — 이 PC 를 매니저(관제) 서버에 등록 */}
      <Card size="small" title={<Space><Typography.Text strong>관제 서버 연동</Typography.Text><Tag color="purple">Manager</Tag></Space>}>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 8, fontSize: 12 }}>
          이 PC 의 재생 상태(시나리오/그룹·회차·pass/fail/error)와 함수 사용통계를 매니저 서버로 실시간 보고합니다.
          매니저 서버 주소를 <code>http://&lt;매니저IP&gt;:9000</code> 형식으로 입력하세요. 비우면 연동 해제됩니다.
          PC 식별은 하드웨어 머신 UID 기준이라 IP 가 바뀌어도 동일 PC 로 집계됩니다.
        </Typography.Paragraph>
        <Space.Compact style={{ width: '100%', maxWidth: 520 }}>
          <Input
            placeholder="http://192.168.0.10:9000"
            value={monitorUrl}
            onChange={(e) => setMonitorUrl(e.target.value)}
            onPressEnter={saveMonitorUrl}
          />
          <Button type="primary" icon={<SaveOutlined />} onClick={saveMonitorUrl} loading={savingMonitor}>저장</Button>
        </Space.Compact>
      </Card>

      {/* 메모리 사용량 모니터링 */}
      <MemoryMonitor active={memoryActive} />

      {/* 프로젝트 / 모델 관리 */}
      <Card size="small" title="프로젝트 · 모델 콤보"
        extra={<Button size="small" icon={<PlusOutlined />} onClick={addProject}>프로젝트 추가</Button>}>
        {catalog.projects.length === 0 ? (
          <Empty description="프로젝트 없음" />
        ) : (
          <Collapse
            defaultActiveKey={catalog.projects.map((_, i) => String(i))}
            items={catalog.projects.map((p, idx) => ({
              key: String(idx),
              label: (
                <Space>
                  <Checkbox
                    checked={p.enabled}
                    onChange={(e) => { updateProject(idx, { enabled: e.target.checked }); }}
                    onClick={(e) => e.stopPropagation()}
                  />
                  <Input
                    size="small"
                    value={p.name}
                    onChange={(e) => updateProject(idx, { name: e.target.value })}
                    onClick={(e) => e.stopPropagation()}
                    style={{ width: 160 }}
                  />
                  <Tag>{p.models.length} models</Tag>
                </Space>
              ),
              extra: (
                <Space onClick={(e) => e.stopPropagation()}>
                  <Button size="small" icon={<PlusOutlined />} onClick={() => addModel(idx)}>모델 추가</Button>
                  <Button size="small" danger icon={<DeleteOutlined />} onClick={() => removeProject(idx)} />
                </Space>
              ),
              children: (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                  {p.models.length === 0 && <Typography.Text type="secondary">모델이 없습니다.</Typography.Text>}
                  {p.models.map((m, mi) => (
                    <div key={mi} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <Checkbox
                        checked={m.enabled}
                        onChange={(e) => updateModel(idx, mi, { enabled: e.target.checked })}
                      />
                      <Input
                        size="small"
                        value={m.value}
                        placeholder="모델 이름 (Device ID prefix)"
                        style={{ width: 220 }}
                        onChange={(e) => updateModel(idx, mi, { value: e.target.value })}
                      />
                      <Typography.Text type="secondary" style={{ fontSize: 11 }}>에이전트</Typography.Text>
                      <Select
                        size="small"
                        allowClear
                        placeholder="선택"
                        value={m.agent}
                        style={{ width: 200 }}
                        options={enabledAgentOptions}
                        onChange={(v) => updateModel(idx, mi, { agent: v })}
                      />
                      <Button size="small" danger icon={<DeleteOutlined />} onClick={() => removeModel(idx, mi)} />
                    </div>
                  ))}
                </div>
              ),
            }))}
          />
        )}
      </Card>

      {/* 에이전트 관리 */}
      <Card size="small" title="에이전트 (주 디바이스 조작 방식)"
        extra={<Typography.Text type="secondary" style={{ fontSize: 11 }}>
          체크 해제하면 모델의 에이전트 선택지에서 제외됩니다. type(내부 매핑)은 고정.
        </Typography.Text>}>
        {catalog.agents.length === 0 ? (
          <Empty description="에이전트 없음" />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            {catalog.agents.map((a, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <Checkbox
                  checked={a.enabled !== false}
                  onChange={(e) => updateAgent(i, { enabled: e.target.checked })}
                />
                <Typography.Text type="secondary" style={{ fontSize: 11, minWidth: 42 }}>이름</Typography.Text>
                <Input
                  size="small"
                  value={a.name}
                  style={{ width: 200 }}
                  onChange={(e) => updateAgent(i, { name: e.target.value })}
                />
                <Typography.Text type="secondary" style={{ fontSize: 11, minWidth: 42 }}>type</Typography.Text>
                <Tag color="blue" style={{ fontFamily: 'monospace' }}>{a.type}</Tag>
                <Typography.Text type="secondary" style={{ fontSize: 10 }}>
                  {a.type === 'adb' && '— Android/IVI tap·swipe·key·screencap'}
                  {a.type === 'hkmc_agent' && '— HKMC 차량 IVI TCP 프로토콜'}
                  {a.type === 'isap_agent' && '— iSAP Agent TCP 프로토콜'}
                  {a.type === 'icas_agent' && '— VW ICAS SSH 프로토콜'}
                  {a.type === 'mib_agent' && '— VW MIB SSH+ksend 프로토콜'}
                  {a.type === 'fpk_agent' && '— VW FPK 클러스터 SSH+프레임버퍼 (캡처 전용, 조작 불가)'}
                  {a.type === 'gm_info_agent' && '— GM Info(QNX) TCP 4445 (터치·스와이프·하드키·캡처)'}
                  {a.type === 'bmw_agent' && '— BMW 후석 듀얼 디스플레이 ADB (WebOS+Android)'}
                  {a.type === 'vision_camera' && '— GigE 비전 카메라 (스크린샷 전용)'}
                  {a.type === 'webcam' && '— USB 웹캠 (관찰 전용)'}
                </Typography.Text>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 모듈 표시 여부 */}
      <Card size="small" title="모듈 표시 여부"
        extra={<Typography.Text type="secondary" style={{ fontSize: 11 }}>체크 해제하면 DevicePage 스캔/등록 UI에서 숨김</Typography.Text>}>
        {modules.length === 0 ? (
          <Empty description="등록된 모듈 없음" />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: 5 }}>
            {modules.map(m => (
              <Checkbox
                key={m.name}
                checked={isModuleVisible(m.name)}
                onChange={(e) => setModuleVisible(m.name, e.target.checked)}
              >
                <Space>
                  <Typography.Text strong style={{ fontSize: 11 }}>{m.label || m.name}</Typography.Text>
                  {m.label && m.label !== m.name && (
                    <Typography.Text type="secondary" style={{ fontSize: 10 }}>({m.name})</Typography.Text>
                  )}
                </Space>
              </Checkbox>
            ))}
          </div>
        )}
      </Card>

      <Divider style={{ margin: '4px 0' }} />

      <Card size="small" title="스캔 설정">
        <Typography.Paragraph style={{ marginBottom: 0 }}>
          <Typography.Text type="secondary">
            스캔 설정은 DevicePage 상단 "스캔 설정" 버튼에서 편집하세요. 해당 설정은 <code>backend/scan_settings.json</code>에 저장됩니다.
          </Typography.Text>
        </Typography.Paragraph>
      </Card>
    </div>
  );
}
