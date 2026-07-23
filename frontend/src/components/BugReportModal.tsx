import { useCallback, useEffect, useMemo, useState, type Key } from 'react';
import { Alert, Checkbox, Form, Input, Modal, Progress, Select, Table, Tag, Typography, message } from 'antd';
import { bugreportApi, resultsApi } from '../services/api';
import { useManagerUrl } from '../lib/manager';
import { useTranslation } from '../i18n';

const { Text } = Typography;

interface StepTestEntry {
  ts: string;
  scenario: string;
  step_index: number;
  step_id: number | null;
  step_type: string;
  status: string;
  similarity_score: number | null;
  command: string;
}

interface RecentResult {
  run_folder: string;
  scenario_name: string;
  status: string;
  total_steps: number;
  failed_steps: number;
  started_at: string;
}

interface BugReportContext {
  version: string;
  boot_id: string;
  platform: string;
  hostname: string;
  step_tests: StepTestEntry[];
  recent_results: RecentResult[];
  // 로그인 사용자 — 제보자 프리필 + 제출 meta(부서/프로젝트)에 쓴다
  user?: { user_id: string; name: string; title: string; team: string; project: string; model: string } | null;
}

const STATUS_COLOR: Record<string, string> = {
  pass: 'green', fail: 'red', error: 'volcano', warning: 'orange',
};

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
  } catch {
    return iso;
  }
}

function saveBlobLocally(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export default function BugReportModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const managerUrl = useManagerUrl();
  const [form] = Form.useForm();

  const [ctx, setCtx] = useState<BugReportContext | null>(null);
  const [selectedTests, setSelectedTests] = useState<Key[]>([]);
  const [selectedRun, setSelectedRun] = useState<string>('');
  const [runSteps, setRunSteps] = useState<{ status: string; command: string; step_id: number }[]>([]);
  const [stepFrom, setStepFrom] = useState<number>(0);
  const [stepTo, setStepTo] = useState<number>(0);
  const [includeBackendLog, setIncludeBackendLog] = useState(true);
  const [busy, setBusy] = useState(false);
  const [percent, setPercent] = useState(0);
  const [phase, setPhase] = useState('');

  // 모달 오픈 시 context 로드 + 제보자 프리필
  useEffect(() => {
    if (!open) return;
    setSelectedTests([]);
    setSelectedRun('');
    setRunSteps([]);
    setPercent(0);
    setPhase('');
    // 제보자 프리필 — 로그인 사용자 기준.
    bugreportApi.context()
      .then((res) => {
        setCtx(res.data);
        const u = res.data?.user;
        if (u?.name && !form.getFieldValue('reporter')) {
          form.setFieldValue('reporter', u.team ? `${u.name}/${u.team}` : u.name);
        }
      })
      .catch(() => { setCtx(null); });
  }, [open, form]);

  // 재생 결과 선택 → 스텝 목록 로드 (기존 결과 상세 API 재사용)
  useEffect(() => {
    if (!selectedRun) { setRunSteps([]); return; }
    resultsApi.get(`${selectedRun}/result.json`)
      .then((res) => {
        const steps = res.data?.step_results || [];
        setRunSteps(steps);
        setStepFrom(0);
        setStepTo(Math.max(0, steps.length - 1));
      })
      .catch(() => setRunSteps([]));
  }, [selectedRun]);

  const stepTests = ctx?.step_tests || [];
  const testColumns = useMemo(() => [
    { title: '', dataIndex: 'ts', width: 110, render: (v: string) => <Text style={{ fontSize: 11 }}>{fmtTime(v)}</Text> },
    { title: '', dataIndex: 'scenario', ellipsis: true, render: (v: string, r: StepTestEntry) => <Text style={{ fontSize: 11 }}>{v} #{(r.step_index ?? 0) + 1}</Text> },
    { title: '', dataIndex: 'command', ellipsis: true, render: (v: string) => <Text type="secondary" style={{ fontSize: 11 }}>{v}</Text> },
    {
      title: '', dataIndex: 'status', width: 64,
      render: (v: string, r: StepTestEntry) => (
        <Tag color={STATUS_COLOR[v] || 'default'} style={{ fontSize: 10, marginRight: 0 }}>
          {v}{r.similarity_score != null ? ` ${(r.similarity_score * 100).toFixed(0)}%` : ''}
        </Tag>
      ),
    },
  ], []);

  const stepOptions = useMemo(() => runSteps.map((s, i) => ({
    value: i,
    label: `#${i + 1} [${s.status}] ${(s.command || '').slice(0, 40)}`,
  })), [runSteps]);

  const submit = useCallback(async () => {
    let values: { title: string; description?: string; reporter?: string };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }

    // 선택된 스텝 테스트들의 min~max ts 를 구간으로 사용
    let stepTestRange: { from_ts: string; to_ts: string } | null = null;
    if (selectedTests.length > 0) {
      const tss = selectedTests.map(String).sort();
      stepTestRange = { from_ts: tss[0], to_ts: tss[tss.length - 1] };
    }
    const playbackRanges = selectedRun && runSteps.length > 0
      ? [{ run_folder: selectedRun, step_from: Math.min(stepFrom, stepTo), step_to: Math.max(stepFrom, stepTo) }]
      : [];

    setBusy(true);
    setPercent(0);
    setPhase(t('bugreport.building'));
    try {
      const buildRes = await bugreportApi.build({
        title: values.title,
        description: values.description || '',
        reporter: values.reporter || '',
        include: {
          backend_log: includeBackendLog,
          step_test_range: stepTestRange,
          playback_ranges: playbackRanges,
        },
        client: { user_agent: navigator.userAgent, app_url: window.location.href },
      });
      const jobId = buildRes.data.job_id;

      // 진행률 폴링
      let jobName = 'bugreport.zip';
      for (;;) {
        await new Promise((r) => setTimeout(r, 700));
        const st = (await bugreportApi.jobStatus(jobId)).data;
        setPercent(st.percent || 0);
        setPhase(st.phase || '');
        if (st.status === 'done') { jobName = st.name || jobName; break; }
        if (st.status === 'error') throw new Error(st.error || 'build failed');
      }

      const blob: Blob = (await bugreportApi.download(jobId)).data;

      // Manager 업로드 → 실패 시 같은 blob 로컬 저장 폴백
      setPhase(t('bugreport.uploading'));
      setPercent(100);
      const meta = {
        title: values.title,
        description: values.description || '',
        reporter: values.reporter || '',
        version: ctx?.version || '',
        boot_id: ctx?.boot_id || '',
        platform: ctx?.platform || '',
        hostname: ctx?.hostname || '',
        created_at: new Date().toISOString(),
        // 로그인 사용자 — 매니저 버그리포트 목록의 부서/프로젝트 컬럼
        user_name: ctx?.user?.name || '',
        user_team: ctx?.user?.team || '',
        project: ctx?.user?.project || '',
        user_model: ctx?.user?.model || '',
      };
      try {
        const fd = new FormData();
        fd.append('meta', JSON.stringify(meta));
        fd.append('file', blob, jobName);
        const resp = await fetch(`${managerUrl}/api/bug-reports`, {
          method: 'POST',
          body: fd,
          signal: AbortSignal.timeout(60000),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        message.success(t('bugreport.uploadSuccess'));
      } catch (uploadErr) {
        console.warn('[bugreport] manager upload failed, falling back to local download:', uploadErr);
        saveBlobLocally(blob, jobName);
        message.warning(t('bugreport.uploadFailed'), 8);
      }
      form.resetFields(['title', 'description']);
      onClose();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } }; message?: string });
      message.error(`${t('bugreport.buildFailed')}: ${detail.response?.data?.detail || detail.message || e}`);
    } finally {
      setBusy(false);
    }
  }, [form, selectedTests, selectedRun, runSteps, stepFrom, stepTo, includeBackendLog, ctx, managerUrl, onClose, t]);

  return (
    <Modal
      title={t('bugreport.title')}
      open={open}
      onCancel={busy ? undefined : onClose}
      onOk={submit}
      okText={t('bugreport.submit')}
      okButtonProps={{ loading: busy }}
      cancelButtonProps={{ disabled: busy }}
      width={720}
      maskClosable={!busy}
      destroyOnClose
    >
      <Form form={form} layout="vertical" size="small" disabled={busy}>
        <Form.Item name="title" label={t('bugreport.formTitle')} rules={[{ required: true, message: t('bugreport.formTitleRequired') }]}>
          <Input maxLength={120} />
        </Form.Item>
        <Form.Item name="description" label={t('bugreport.formDesc')}>
          <Input.TextArea rows={3} placeholder={t('bugreport.formDescPlaceholder')} maxLength={2000} />
        </Form.Item>
        <Form.Item name="reporter" label={t('bugreport.formReporter')}>
          <Input placeholder={t('bugreport.formReporterPlaceholder')} maxLength={60} />
        </Form.Item>
      </Form>

      <div style={{ fontWeight: 600, marginBottom: 6 }}>{t('bugreport.recentActions')}</div>

      {/* ── 스텝 테스트 구간 선택 ── */}
      <div style={{ marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 11 }}>{t('bugreport.stepTests')} — {t('bugreport.stepTestsHint')}</Text>
        {stepTests.length === 0 ? (
          <div style={{ padding: '8px 0' }}><Text type="secondary" style={{ fontSize: 12 }}>{t('bugreport.stepTestsEmpty')}</Text></div>
        ) : (
          <Table
            size="small"
            showHeader={false}
            rowKey="ts"
            dataSource={stepTests}
            columns={testColumns}
            pagination={false}
            scroll={{ y: 160 }}
            rowSelection={{
              selectedRowKeys: selectedTests,
              onChange: setSelectedTests,
              getCheckboxProps: () => ({ disabled: busy }),
            }}
            style={{ marginTop: 4 }}
          />
        )}
      </div>

      {/* ── 시나리오 재생 구간 선택 ── */}
      <div style={{ marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 11 }}>{t('bugreport.playback')}</Text>
        {(ctx?.recent_results || []).length === 0 ? (
          <div style={{ padding: '8px 0' }}><Text type="secondary" style={{ fontSize: 12 }}>{t('bugreport.playbackEmpty')}</Text></div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
            <Select
              allowClear
              placeholder={t('bugreport.selectResult')}
              value={selectedRun || undefined}
              onChange={(v) => setSelectedRun(v || '')}
              options={(ctx?.recent_results || []).map((r) => ({
                value: r.run_folder,
                label: `${r.run_folder}  [${r.status}] ${r.failed_steps > 0 ? `fail ${r.failed_steps}` : ''}`,
              }))}
              style={{ width: '100%' }}
            />
            {selectedRun && runSteps.length > 0 && (
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Text style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{t('bugreport.playbackPickRange')} ({runSteps.length} {t('bugreport.steps')})</Text>
                <Select size="small" value={stepFrom} onChange={setStepFrom} options={stepOptions} style={{ flex: 1, minWidth: 0 }} />
                <Text type="secondary">~</Text>
                <Select size="small" value={stepTo} onChange={setStepTo} options={stepOptions} style={{ flex: 1, minWidth: 0 }} />
              </div>
            )}
          </div>
        )}
      </div>

      <div style={{ display: 'flex', gap: 16, marginBottom: 8 }}>
        <Checkbox checked={includeBackendLog} onChange={(e) => setIncludeBackendLog(e.target.checked)} disabled={busy}>
          <Text style={{ fontSize: 12 }}>{t('bugreport.includeBackendLog')}</Text>
        </Checkbox>
      </div>

      {selectedTests.length === 0 && !selectedRun && (
        <Alert type="info" showIcon message={<Text style={{ fontSize: 12 }}>{t('bugreport.noAttachment')}</Text>} style={{ marginBottom: 8 }} />
      )}

      {busy && (
        <div>
          <Progress percent={percent} size="small" status="active" />
          <Text type="secondary" style={{ fontSize: 12 }}>{phase}</Text>
        </div>
      )}
    </Modal>
  );
}
