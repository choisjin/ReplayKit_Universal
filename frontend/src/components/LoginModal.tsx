import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Input, Modal, Select, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import { SearchOutlined, UserOutlined } from '@ant-design/icons';
import { userApi, LoginUser, LoginProject } from '../services/api';

const { Text } = Typography;

interface SearchUser {
  name: string;
  title: string;
  team: string;
  display_name: string;
  user_id: string;
}

interface Props {
  open: boolean;
  /** 현재 로그인 사용자 — 있으면 '변경' 모드(취소 가능), 없으면 최초 로그인 */
  user: LoginUser | null;
  onDone: (user: LoginUser, temporary: boolean) => void;
  /** 닫기(취소/나중에). 최초 로그인에서는 '나중에 하기'로만 닫힌다. */
  onClose: () => void;
}

const ALL_TEAM = '__all__';

/**
 * 로그인(사용자 식별) 모달 — 비밀번호 없이 "누가 이 PC 를 쓰는지"만 고른다.
 * Jira 유저 검색(이름/아이디/조직명)은 백엔드가 대행하고(계정 비노출),
 * 사용자 선택 + 프로젝트(HKMC/Nissan 등) 선택 후 확인하면 저장된다.
 */
export default function LoginModal({ open, user, onDone, onClose }: Props) {
  const [keyword, setKeyword] = useState('');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<SearchUser[]>([]);
  const [teamFilter, setTeamFilter] = useState(ALL_TEAM);
  const [selectedId, setSelectedId] = useState('');
  // 프로젝트/모델 선택지는 주 디바이스 카탈로그(device_catalog)에서 온다
  const [projects, setProjects] = useState<LoginProject[]>([]);
  const [jiraReady, setJiraReady] = useState(true);
  const [project, setProject] = useState('');
  const [model, setModel] = useState('');
  // 어느 버튼(유지/임시)이 진행 중인지 — 로딩 스피너를 해당 버튼에만 표시
  const [saving, setSaving] = useState<'' | 'keep' | 'temp'>('');

  // 모달 오픈 시 구성(프로젝트/모델 목록) 로드 + 상태 초기화
  useEffect(() => {
    if (!open) return;
    setKeyword('');
    setResults([]);
    setTeamFilter(ALL_TEAM);
    setSelectedId('');
    setProject(user?.project || '');
    setModel(user?.model || '');
    userApi.config()
      .then((res) => {
        setProjects(res.data.projects || []);
        setJiraReady(res.data.jira_ready);
        // 이전 프로젝트가 없으면 목록 첫 항목을 기본값으로
        if (!user?.project && (res.data.projects || []).length > 0) {
          setProject(res.data.projects[0].name);
        }
      })
      .catch(() => setJiraReady(false));
  }, [open, user]);

  const doSearch = useCallback(async () => {
    const kw = keyword.trim();
    if (!kw) {
      message.warning('검색어를 입력해주세요 (이름/아이디/조직명)');
      return;
    }
    setSearching(true);
    try {
      const res = await userApi.search(kw);
      const users = res.data.users || [];
      setResults(users);
      setTeamFilter(ALL_TEAM);
      setSelectedId('');
      if (users.length === 0) message.info(`"${kw}" 에 해당하는 사용자가 없습니다`);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || 'Jira 유저 검색에 실패했습니다');
    } finally {
      setSearching(false);
    }
  }, [keyword]);

  const teams = useMemo(
    () => Array.from(new Set(results.map(r => r.team).filter(Boolean))).sort(),
    [results]);
  const filtered = useMemo(
    () => (teamFilter === ALL_TEAM ? results : results.filter(r => r.team === teamFilter)),
    [results, teamFilter]);
  const selected = useMemo(
    () => filtered.find(r => (r.user_id || r.display_name) === selectedId) || null,
    [filtered, selectedId]);

  /** temporary=true — 임시 로그인: 이번 실행 동안만 유효, 다음 실행 시 다시 묻는다. */
  const confirm = async (temporary: boolean) => {
    if (!selected) {
      message.warning('사용자를 선택해주세요');
      return;
    }
    if (!project) {
      message.warning('프로젝트를 선택해주세요');
      return;
    }
    setSaving(temporary ? 'temp' : 'keep');
    try {
      const res = await userApi.setCurrent({
        user_id: selected.user_id,
        name: selected.name,
        title: selected.title,
        team: selected.team,
        project,
        model,
        temporary,
      });
      if (res.data.user) onDone(res.data.user, res.data.temporary);
    } catch {
      message.error('사용자 저장에 실패했습니다');
    } finally {
      setSaving('');
    }
  };

  const columns = [
    { title: '이름', dataIndex: 'name', width: 90, ellipsis: true },
    { title: '직급', dataIndex: 'title', width: 130, ellipsis: true },
    { title: '팀', dataIndex: 'team', ellipsis: true },
    { title: 'ID', dataIndex: 'user_id', width: 130, ellipsis: true },
  ];

  return (
    <Modal
      title={<span><UserOutlined /> {user ? '사용자 변경' : '로그인 — 사용자 선택'}</span>}
      open={open}
      onCancel={onClose}
      width={640}
      maskClosable={false}
      footer={[
        <Button key="later" onClick={onClose}>
          {user ? '취소' : '나중에 하기'}
        </Button>,
        <Tooltip key="temp" title="이번 실행 동안만 — 다음 실행 시 로그인 창이 다시 뜹니다">
          <Button onClick={() => confirm(true)} loading={saving === 'temp'} disabled={!selected || !project}>
            임시 로그인
          </Button>
        </Tooltip>,
        <Tooltip key="ok" title="사용자 변경 버튼을 누르기 전까지 유지됩니다">
          <Button type="primary" onClick={() => confirm(false)} loading={saving === 'keep'} disabled={!selected || !project}>
            로그인 (유지)
          </Button>
        </Tooltip>,
      ]}
    >
      {!jiraReady && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 12 }}
          message="Jira 검색을 사용할 수 없습니다"
          description="관제 서버(Manager) 설정 페이지에 Jira ID/비밀번호가 등록되어 있어야 합니다. 관리자에게 문의하세요."
        />
      )}

      <Space.Compact style={{ width: '100%', marginBottom: 8 }}>
        <Input
          placeholder="이름 / 아이디(EP로그인용 아이디) / 조직명 입력 후 Enter"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onPressEnter={doSearch}
          disabled={!jiraReady}
          autoFocus
        />
        <Button icon={<SearchOutlined />} onClick={doSearch} loading={searching} disabled={!jiraReady}>
          검색
        </Button>
      </Space.Compact>

      {teams.length > 0 && (
        <div style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>팀:</Text>
          <Select
            size="small"
            style={{ minWidth: 220, flex: 1 }}
            value={teamFilter}
            onChange={setTeamFilter}
            options={[{ label: `전체 (${results.length}명)`, value: ALL_TEAM },
              ...teams.map(t => ({ label: t, value: t }))]}
            showSearch
            optionFilterProp="label"
          />
        </div>
      )}

      <Table
        size="small"
        rowKey={(r: SearchUser) => r.user_id || r.display_name}
        dataSource={filtered}
        columns={columns}
        pagination={false}
        scroll={{ y: 240 }}
        loading={searching}
        locale={{ emptyText: '검색 결과 없음 — 이름이나 팀명으로 검색하세요' }}
        onRow={(r) => ({
          onClick: () => setSelectedId(r.user_id || r.display_name),
          onDoubleClick: () => setSelectedId(r.user_id || r.display_name),
          style: { cursor: 'pointer' },
        })}
        rowSelection={{
          type: 'radio',
          selectedRowKeys: selectedId ? [selectedId] : [],
          onChange: (keys) => setSelectedId(String(keys[0] || '')),
        }}
      />

      {/* 프로젝트/모델 — 주 디바이스 카탈로그의 선택지를 그대로 쓴다 */}
      <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Text style={{ fontSize: 12 }}>프로젝트:</Text>
        <Select
          style={{ minWidth: 130 }}
          value={project || undefined}
          onChange={(v) => { setProject(v); setModel(''); }}
          placeholder="프로젝트 선택"
          options={projects.map(p => ({ label: p.name, value: p.name }))}
          showSearch
          optionFilterProp="label"
        />
        <Text style={{ fontSize: 12 }}>모델:</Text>
        <Select
          style={{ minWidth: 150 }}
          value={model || undefined}
          onChange={(v) => setModel(v || '')}
          placeholder="모델 (선택)"
          allowClear
          disabled={!project}
          options={(projects.find(p => p.name === project)?.models || []).map(m => ({ label: m, value: m }))}
          showSearch
          optionFilterProp="label"
        />
        {selected && (
          <Tag color="blue" style={{ marginLeft: 'auto' }}>
            {selected.name}{selected.team ? ` · ${selected.team}` : ''}
          </Tag>
        )}
      </div>
    </Modal>
  );
}
