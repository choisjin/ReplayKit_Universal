import { useCallback, useEffect, useState } from 'react';
import {
  Button, Card, Dropdown, Input, InputNumber, Modal, Popconfirm,
  Select, Space, Switch, Table, Tag, Typography, message,
} from 'antd';
import {
  CloudDownloadOutlined, DeleteOutlined, FolderOpenOutlined,
  ReloadOutlined, SaveOutlined,
} from '@ant-design/icons';
import { useSettings } from '../context/SettingsContext';
import { useTranslation } from '../i18n';
import { backupApi } from '../services/api';

const { Text } = Typography;

interface BackupItem {
  id: string;
  created_at: string;
  reason: string;
  counts?: { scenarios?: number; screenshots?: number; groups?: number };
  scenarios?: string[];
  size: number;
  locations: string[];
}

function fmtSize(bytes: number): string {
  if (!bytes) return '-';
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function fmtTime(iso: string): string {
  if (!iso) return '-';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export default function BackupSection() {
  const { settings, updateSettings, browseFolder } = useSettings();
  const { t } = useTranslation();

  const [backups, setBackups] = useState<BackupItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [keep, setKeep] = useState<number | null>(settings.backup_keep);
  const [dirInput, setDirInput] = useState<string>(settings.backup_dir);

  // 개별 시나리오 복원 모달
  const [scenModal, setScenModal] = useState<{ id: string; scenarios: string[] } | null>(null);
  const [pickedScenario, setPickedScenario] = useState<string | undefined>();
  const [restoring, setRestoring] = useState(false);

  useEffect(() => { setKeep(settings.backup_keep); }, [settings.backup_keep]);
  useEffect(() => { setDirInput(settings.backup_dir); }, [settings.backup_dir]);

  const commitDir = () => {
    if (dirInput !== settings.backup_dir) updateSettings({ backup_dir: dirInput });
  };

  const loadBackups = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await backupApi.list();
      setBackups(data || []);
    } catch {
      /* 목록 로드 실패는 조용히 — 빈 목록 */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadBackups(); }, [loadBackups]);

  const handleCreate = async () => {
    setCreating(true);
    try {
      const { data } = await backupApi.create('manual');
      if (data.status === 'skipped') message.info(t('settings.backupSkipped'));
      else message.success(t('settings.backupCreated'));
      await loadBackups();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || t('common.saveFailed'));
    } finally {
      setCreating(false);
    }
  };

  const handleBrowse = async () => {
    try {
      const p = await browseFolder(settings.backup_dir || '');
      if (p) await updateSettings({ backup_dir: p });
    } catch {
      message.error(t('settings.folderSelectFailed'));
    }
  };

  const handleRestoreFull = (id: string, mode: 'merge' | 'replace') => {
    Modal.confirm({
      title: mode === 'replace' ? t('settings.restoreFullReplace') : t('settings.restoreFullMerge'),
      content: mode === 'replace' ? t('settings.restoreReplaceConfirm') : t('settings.restoreMergeConfirm'),
      okText: t('settings.restore'),
      okButtonProps: mode === 'replace' ? { danger: true } : undefined,
      cancelText: t('common.cancel'),
      onOk: async () => {
        try {
          await backupApi.restoreFull(id, mode);
          message.success(t('settings.restoreDone'));
          await loadBackups();
        } catch (e: any) {
          message.error(e?.response?.data?.detail || t('settings.restoreFailed'));
        }
      },
    });
  };

  const openScenarioModal = async (id: string) => {
    try {
      const { data } = await backupApi.preview(id);
      setScenModal({ id, scenarios: (data.scenarios || []).map((s: any) => s.name) });
      setPickedScenario(undefined);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || t('settings.restoreFailed'));
    }
  };

  const handleRestoreScenario = async () => {
    if (!scenModal || !pickedScenario) return;
    setRestoring(true);
    try {
      await backupApi.restoreScenario(scenModal.id, pickedScenario);
      message.success(t('settings.restoreDone'));
      setScenModal(null);
      await loadBackups();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || t('settings.restoreFailed'));
    } finally {
      setRestoring(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await backupApi.remove(id);
      await loadBackups();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || t('common.saveFailed'));
    }
  };

  const reasonTag = (reason: string) => {
    if (reason === 'auto') return <Tag color="blue">{t('settings.backupReasonAuto')}</Tag>;
    if (reason === 'manual') return <Tag color="green">{t('settings.backupReasonManual')}</Tag>;
    if (reason === 'pre-restore') return <Tag color="orange">{t('settings.backupReasonPre')}</Tag>;
    return <Tag>{reason}</Tag>;
  };

  const columns = [
    {
      title: t('settings.backupColTime'), dataIndex: 'created_at', key: 'time',
      render: (v: string) => <span style={{ fontSize: 12 }}>{fmtTime(v)}</span>,
    },
    {
      title: t('settings.backupColReason'), dataIndex: 'reason', key: 'reason',
      width: 90, render: reasonTag,
    },
    {
      title: t('settings.backupColScenarios'), key: 'scenarios', width: 90, align: 'center' as const,
      render: (_: any, r: BackupItem) => r.counts?.scenarios ?? (r.scenarios?.length ?? 0),
    },
    {
      title: t('settings.backupColSize'), dataIndex: 'size', key: 'size',
      width: 90, align: 'right' as const, render: (v: number) => <span style={{ fontSize: 12 }}>{fmtSize(v)}</span>,
    },
    {
      title: t('settings.backupColLocation'), key: 'loc', width: 110,
      render: (_: any, r: BackupItem) => (
        <Space size={2}>
          <Tag color="default" style={{ marginInlineEnd: 0 }}>{t('settings.backupLocInternal')}</Tag>
          {r.locations && r.locations.length > 1 && (
            <Tag color="purple">{t('settings.backupLocExternal')}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: '', key: 'actions', width: 190,
      render: (_: any, r: BackupItem) => (
        <Space size={4}>
          <Dropdown
            menu={{
              items: [
                { key: 'merge', label: t('settings.restoreFullMerge'), onClick: () => handleRestoreFull(r.id, 'merge') },
                { key: 'replace', label: t('settings.restoreFullReplace'), danger: true, onClick: () => handleRestoreFull(r.id, 'replace') },
                { type: 'divider' as const },
                { key: 'scenario', label: t('settings.restoreScenario'), onClick: () => openScenarioModal(r.id) },
              ],
            }}
          >
            <Button size="small" type="primary">{t('settings.restore')}</Button>
          </Dropdown>
          <Button
            size="small" icon={<CloudDownloadOutlined />} title={t('settings.backupDownload')}
            href={backupApi.downloadUrl(r.id)}
          />
          <Popconfirm
            title={t('settings.backupDeleteConfirm')}
            okText={t('common.delete')} cancelText={t('common.cancel')}
            onConfirm={() => handleDelete(r.id)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const intervalOpts = [
    { value: 60, label: t('settings.int1h') },
    { value: 360, label: t('settings.int6h') },
    { value: 720, label: t('settings.int12h') },
    { value: 1440, label: t('settings.intDaily') },
    { value: 10080, label: t('settings.intWeekly') },
  ];

  return (
    <Card title={t('settings.backupTitle')} size="small">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {/* 자동 백업 설정 */}
        <div>
          <Space wrap>
            <Text strong>{t('settings.backupAuto')}</Text>
            <Switch
              checked={settings.backup_enabled}
              onChange={(v) => updateSettings({ backup_enabled: v })}
            />
            <Select
              size="small"
              disabled={!settings.backup_enabled}
              value={settings.backup_interval_minutes}
              style={{ width: 140 }}
              onChange={(v) => updateSettings({ backup_interval_minutes: v })}
              options={intervalOpts}
            />
            <span style={{ color: '#888', fontSize: 12 }}>{t('settings.backupInterval')}</span>
          </Space>
          <Text type="secondary" style={{ fontSize: 11, marginTop: 3, display: 'block' }}>
            {t('settings.backupAutoDesc')}
          </Text>
        </div>

        {/* 외부 저장 폴더 */}
        <div>
          <Space.Compact style={{ width: '100%', maxWidth: 560 }}>
            <Input
              size="small"
              placeholder={t('settings.backupDir')}
              value={dirInput}
              onChange={(e) => setDirInput(e.target.value)}
              onBlur={commitDir}
              onPressEnter={commitDir}
            />
            <Button size="small" icon={<FolderOpenOutlined />} onClick={handleBrowse} />
          </Space.Compact>
          <Text type="secondary" style={{ fontSize: 11, marginTop: 3, display: 'block' }}>
            {t('settings.backupDirDesc')}
          </Text>
        </div>

        {/* 보존 개수 + 지금 백업 */}
        <Space wrap>
          <span style={{ fontSize: 12 }}>{t('settings.backupKeep')}</span>
          <InputNumber
            size="small" min={1} max={200} value={keep} style={{ width: 80 }}
            onChange={(v) => setKeep(v)}
            onBlur={() => { if (keep != null && keep !== settings.backup_keep) updateSettings({ backup_keep: keep }); }}
          />
          <Button
            size="small" type="primary" icon={<SaveOutlined />}
            loading={creating} onClick={handleCreate}
          >
            {t('settings.backupNow')}
          </Button>
        </Space>
        <Text type="secondary" style={{ fontSize: 11, marginTop: -8, display: 'block' }}>
          {t('settings.backupKeepDesc')}
        </Text>

        {/* 백업 목록 */}
        <div>
          <Space style={{ marginBottom: 6 }}>
            <Text strong>{t('settings.backupList')}</Text>
            <Button size="small" icon={<ReloadOutlined />} onClick={loadBackups}>
              {t('settings.backupRefresh')}
            </Button>
          </Space>
          <Table
            size="small"
            rowKey="id"
            loading={loading}
            dataSource={backups}
            columns={columns}
            pagination={backups.length > 10 ? { pageSize: 10 } : false}
            locale={{ emptyText: t('settings.backupEmpty') }}
          />
        </div>
      </Space>

      {/* 개별 시나리오 복원 모달 */}
      <Modal
        open={!!scenModal}
        title={t('settings.restoreScenario')}
        onCancel={() => setScenModal(null)}
        onOk={handleRestoreScenario}
        okText={t('settings.restore')}
        cancelText={t('common.cancel')}
        okButtonProps={{ disabled: !pickedScenario, loading: restoring }}
      >
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
          {t('settings.pickScenario')}
        </Text>
        <Select
          showSearch
          style={{ width: '100%' }}
          placeholder={t('settings.pickScenario')}
          value={pickedScenario}
          onChange={setPickedScenario}
          options={(scenModal?.scenarios || []).map((s) => ({ value: s, label: s }))}
          filterOption={(input, opt) => (opt?.label as string).toLowerCase().includes(input.toLowerCase())}
        />
      </Modal>
    </Card>
  );
}
