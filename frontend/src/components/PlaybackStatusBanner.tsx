import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Popconfirm, Progress, Space, Tag } from 'antd';
import { PlayCircleOutlined, StopOutlined } from '@ant-design/icons';
import { scenarioApi } from '../services/api';
import { useTranslation } from '../i18n';

interface MonitorState {
  scenario_name?: string;
  total_cycles?: number;
  current_cycle?: number;
  current_step?: number;
  total_steps?: number;
  passed?: number;
  failed?: number;
  error?: number;
}

export default function PlaybackStatusBanner() {
  const { t } = useTranslation();
  const [running, setRunning] = useState(false);
  const [monitor, setMonitor] = useState<MonitorState>({});
  const [stopping, setStopping] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchStatus = async () => {
    try {
      const r = await scenarioApi.playbackStatus();
      const data = r.data || {};
      setRunning(!!data.running);
      setMonitor(data.monitor || {});
      if (!data.running && stopping) setStopping(false);
    } catch {
      // backend 연결 실패 등 - 무시
    }
  };

  useEffect(() => {
    fetchStatus();
    timerRef.current = setInterval(fetchStatus, 3000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleStop = async () => {
    setStopping(true);
    try {
      await scenarioApi.stopPlayback();
    } catch {
      setStopping(false);
    }
    // 즉시 재조회 — 백엔드가 상태를 반영할 때까지 잠시 대기
    setTimeout(fetchStatus, 500);
  };

  if (!running) return null;

  const cur = monitor.current_cycle || 0;
  const total = monitor.total_cycles || 1;
  const step = monitor.current_step || 0;
  const totalSteps = monitor.total_steps || 0;
  // 전체 진행률 = (완료 회차 * 회차당 스텝 + 현재 스텝) / (전체 회차 * 회차당 스텝)
  const totalUnits = total * totalSteps;
  const doneUnits = Math.max(0, cur - 1) * totalSteps + step;
  const cyclePct = totalUnits > 0 ? Math.min(100, Math.floor((doneUnits / totalUnits) * 100)) : 0;
  const name = monitor.scenario_name || '-';
  const passed = monitor.passed || 0;
  const failed = monitor.failed || 0;
  const errors = monitor.error || 0;

  return (
    <Alert
      type="info"
      showIcon
      icon={<PlayCircleOutlined />}
      style={{ marginBottom: 6 }}
      message={
        <Space size="middle" wrap>
          <strong>{t('playbackBanner.running') || '재생 중'}</strong>
          <Tag color="blue">{name}</Tag>
          <span>
            {t('playbackBanner.cycle') || '회차'}: <strong>{cur}/{total}</strong>
          </span>
          <span>
            {t('playbackBanner.step') || '스텝'}: {step}/{totalSteps}
          </span>
          <Tag color="green">pass {passed}</Tag>
          {failed > 0 && <Tag color="red">fail {failed}</Tag>}
          {errors > 0 && <Tag color="orange">error {errors}</Tag>}
          <Popconfirm
            title={t('playbackBanner.stopConfirm') || '정말로 중단하시겠습니까?'}
            onConfirm={handleStop}
            okText={t('common.confirm')}
            cancelText={t('common.cancel')}
          >
            <Button danger size="small" icon={<StopOutlined />} loading={stopping}>
              {t('scenario.stop') || '중지'}
            </Button>
          </Popconfirm>
        </Space>
      }
      description={
        <Progress
          percent={cyclePct}
          size="small"
          status={failed > 0 || errors > 0 ? 'exception' : 'active'}
          style={{ marginTop: 3 }}
        />
      }
    />
  );
}
