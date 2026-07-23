import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Input, Modal, Select, Space, Table, Tag, Tooltip, Typography, message } from 'antd';
import { SearchOutlined, UserOutlined } from '@ant-design/icons';
import { userApi, LoginUser, LoginProject } from '../services/api';
import { useTranslation } from '../i18n';

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
  const { t } = useTranslation();
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
      message.warning(t('login.enterKeyword'));
      return;
    }
    setSearching(true);
    try {
      const res = await userApi.search(kw);
      const users = res.data.users || [];
      setResults(users);
      setTeamFilter(ALL_TEAM);
      setSelectedId('');
      if (users.length === 0) message.info(t('login.noUsersFound', { kw }));
    } catch (e: any) {
      message.error(e?.response?.data?.detail || t('login.searchFailed'));
    } finally {
      setSearching(false);
    }
  }, [keyword, t]);

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
      message.warning(t('login.selectUser'));
      return;
    }
    if (!project) {
      message.warning(t('login.selectProject'));
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
      message.error(t('login.saveFailed'));
    } finally {
      setSaving('');
    }
  };

  const columns = [
    { title: t('common.name'), dataIndex: 'name', width: 90, ellipsis: true },
    { title: t('login.colTitle'), dataIndex: 'title', width: 130, ellipsis: true },
    { title: t('login.colTeam'), dataIndex: 'team', ellipsis: true },
    { title: 'ID', dataIndex: 'user_id', width: 130, ellipsis: true },
  ];

  return (
    <Modal
      title={<span><UserOutlined /> {user ? t('login.titleChange') : t('login.titleLogin')}</span>}
      open={open}
      onCancel={onClose}
      width={640}
      maskClosable={false}
      footer={[
        <Button key="later" onClick={onClose}>
          {user ? t('common.cancel') : t('login.later')}
        </Button>,
        <Tooltip key="temp" title={t('login.tempTooltip')}>
          <Button onClick={() => confirm(true)} loading={saving === 'temp'} disabled={!selected || !project}>
            {t('login.tempLogin')}
          </Button>
        </Tooltip>,
        <Tooltip key="ok" title={t('login.keepTooltip')}>
          <Button type="primary" onClick={() => confirm(false)} loading={saving === 'keep'} disabled={!selected || !project}>
            {t('login.keepLogin')}
          </Button>
        </Tooltip>,
      ]}
    >
      {!jiraReady && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 12 }}
          message={t('login.jiraUnavailable')}
          description={t('login.jiraUnavailableDesc')}
        />
      )}

      <Space.Compact style={{ width: '100%', marginBottom: 8 }}>
        <Input
          placeholder={t('login.searchPlaceholder')}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onPressEnter={doSearch}
          disabled={!jiraReady}
          autoFocus
        />
        <Button icon={<SearchOutlined />} onClick={doSearch} loading={searching} disabled={!jiraReady}>
          {t('common.search')}
        </Button>
      </Space.Compact>

      {teams.length > 0 && (
        <div style={{ marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>{t('login.team')}</Text>
          <Select
            size="small"
            style={{ minWidth: 220, flex: 1 }}
            value={teamFilter}
            onChange={setTeamFilter}
            options={[{ label: t('login.allTeams', { count: results.length }), value: ALL_TEAM },
              ...teams.map(tm => ({ label: tm, value: tm }))]}
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
        locale={{ emptyText: t('login.emptyText') }}
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
        <Text style={{ fontSize: 12 }}>{t('login.project')}</Text>
        <Select
          style={{ minWidth: 130 }}
          value={project || undefined}
          onChange={(v) => { setProject(v); setModel(''); }}
          placeholder={t('login.projectPlaceholder')}
          options={projects.map(p => ({ label: p.name, value: p.name }))}
          showSearch
          optionFilterProp="label"
        />
        <Text style={{ fontSize: 12 }}>{t('login.model')}</Text>
        <Select
          style={{ minWidth: 150 }}
          value={model || undefined}
          onChange={(v) => setModel(v || '')}
          placeholder={t('login.modelPlaceholder')}
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
