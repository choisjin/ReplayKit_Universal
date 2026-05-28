import { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, Space, Spin, Table, Tag, Typography, message } from 'antd';
import { BranchesOutlined, ReloadOutlined, TagOutlined } from '@ant-design/icons';
import { serverApi } from '../services/api';
import { useSettings } from '../context/SettingsContext';
import { useTranslation } from '../i18n';

interface Commit {
  hash: string;
  short_hash: string;
  author: string;
  email: string;
  date: string;
  message: string;
}

export default function ChangelogPage() {
  const { t } = useTranslation();
  const { settings } = useSettings();
  const isDark = settings.theme === 'dark';

  const [loading, setLoading] = useState(true);
  const [commits, setCommits] = useState<Commit[]>([]);
  const [branch, setBranch] = useState('');
  const [tags, setTags] = useState<string[]>([]);
  const [search, setSearch] = useState('');

  const [serverNote, setServerNote] = useState<string>('');
  const [fetchWarning, setFetchWarning] = useState<string>('');
  const [source, setSource] = useState<string>('');

  const load = async (fetch = false) => {
    setLoading(true);
    try {
      const res = await serverApi.gitLog(200, fetch);
      const data: any = res.data || {};
      setCommits(data.commits || []);
      setBranch(data.branch || '');
      setTags(data.tags || []);
      setServerNote(data.note || '');
      setFetchWarning(data.fetch_warning || '');
      setSource(data.source || '');
      // 진단 정보를 console 에 항상 남김 — 4초 info popup 은 놓치기 쉽다.
      if (data.note) console.warn('[changelog] note:', data.note);
      if (data.fetch_warning) console.warn('[changelog] fetch_warning:', data.fetch_warning);
      if (data.source) console.log('[changelog] source:', data.source);
    } catch (e) {
      // axios HTTP 에러 (서버 5xx / 네트워크 단절) — 진짜 실패 케이스
      console.error('[changelog] load failed', e);
      message.error(t('changelog.loadFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(true);

    const handler = (e: Event) => {
      if ((e as CustomEvent).detail === '/changelog') load(true);
    };
    window.addEventListener('tab-change', handler);
    return () => window.removeEventListener('tab-change', handler);
  }, []);

  const filtered = search
    ? commits.filter(c =>
        c.message.toLowerCase().includes(search.toLowerCase()) ||
        c.short_hash.toLowerCase().includes(search.toLowerCase()) ||
        c.author.toLowerCase().includes(search.toLowerCase())
      )
    : commits;

  // 커밋 메시지 prefix로 타입 태그 표시
  const typeTag = (msg: string) => {
    const m = msg.match(/^(feat|fix|refactor|docs|style|test|chore|perf|ci|build)[:(]/i);
    if (!m) return null;
    const type = m[1].toLowerCase();
    const colors: Record<string, string> = {
      feat: 'blue', fix: 'red', refactor: 'orange', docs: 'green',
      style: 'purple', test: 'cyan', chore: 'default', perf: 'gold',
      ci: 'geekblue', build: 'lime',
    };
    return <Tag color={colors[type] || 'default'} style={{ marginRight: 5 }}>{type}</Tag>;
  };

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));
    if (days === 0) return '오늘';
    if (days === 1) return '어제';
    if (days < 7) return `${days}일 전`;
    return d.toLocaleDateString('ko-KR', { year: 'numeric', month: '2-digit', day: '2-digit' });
  };

  const columns = [
    {
      title: t('changelog.hash'),
      dataIndex: 'short_hash',
      key: 'hash',
      width: 90,
      render: (v: string) => (
        <Typography.Text code copyable={{ text: v }} style={{ fontSize: 11 }}>{v}</Typography.Text>
      ),
    },
    {
      title: t('changelog.message'),
      dataIndex: 'message',
      key: 'message',
      render: (v: string) => (
        <span>
          {typeTag(v)}
          <span>{v.replace(/^(feat|fix|refactor|docs|style|test|chore|perf|ci|build)[:(]\s*/i, '')}</span>
        </span>
      ),
    },
    {
      title: t('changelog.author'),
      dataIndex: 'author',
      key: 'author',
      width: 120,
      render: (v: string) => <span style={{ color: isDark ? '#91caff' : '#1677ff' }}>{v}</span>,
    },
    {
      title: t('changelog.date'),
      dataIndex: 'date',
      key: 'date',
      width: 110,
      render: (v: string) => <span style={{ color: '#888', fontSize: 11 }}>{formatDate(v)}</span>,
    },
  ];

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <Typography.Title level={4} style={{ marginBottom: 13 }}>{t('changelog.title')}</Typography.Title>

      <Space style={{ marginBottom: 13 }} wrap>
        <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => load(true)}>
          {t('changelog.refresh')}
        </Button>
        <Tag icon={<BranchesOutlined />} color="processing">{branch}</Tag>
        <span style={{ color: '#888', fontSize: 11 }}>{t('changelog.totalCommits')}: {commits.length}</span>
        {tags.length > 0 && (
          <>
            <TagOutlined style={{ color: '#888', marginLeft: 6 }} />
            {tags.slice(0, 5).map(tag => (
              <Tag key={tag} color="gold">{tag}</Tag>
            ))}
            {tags.length > 5 && <span style={{ color: '#888', fontSize: 11 }}>+{tags.length - 5}</span>}
          </>
        )}
      </Space>

      {(serverNote || fetchWarning) && (
        <Alert
          type={commits.length === 0 ? 'warning' : 'info'}
          showIcon
          closable
          style={{ marginBottom: 13 }}
          message={t(source === 'bundled' ? 'changelog.offlineSnapshot' : 'changelog.fetchIssue')}
          description={
            <div style={{ fontSize: 11 }}>
              {serverNote && <div>{serverNote}</div>}
              {fetchWarning && <div style={{ color: '#888', marginTop: 4, fontFamily: 'monospace' }}>fetch: {fetchWarning}</div>}
            </div>
          }
        />
      )}

      <Card size="small" style={{ marginBottom: 13 }}>
        <Input.Search
          placeholder={t('changelog.search')}
          allowClear
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </Card>

      <Spin spinning={loading}>
        <Table
          dataSource={filtered}
          columns={columns}
          rowKey="hash"
          size="small"
          pagination={{ pageSize: 30, showSizeChanger: true, pageSizeOptions: ['20', '30', '50', '100'] }}
          locale={{ emptyText: t('changelog.noCommits') }}
        />
      </Spin>
    </div>
  );
}
