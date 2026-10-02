import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Popconfirm, Progress, Space, Tag } from 'antd';
import { ExperimentOutlined, PlayCircleOutlined, StopOutlined } from '@ant-design/icons';
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

interface StepTestState {
  scenario_name?: string;
  step_id?: number;
  command?: string;
  started_at?: string;
  stopping?: boolean;
}

/** 스텝 테스트 시작 시 RecordPage 가 발행 — 폴링 주기를 기다리지 않고 바로 상태바를 띄운다. */
export const STEP_TEST_EVENT = 'replaykit:step-test';

const POLL_IDLE_MS = 3000;
const POLL_STEP_TEST_MS = 1000;

export default function PlaybackStatusBanner() {
  const { t } = useTranslation();
  const [running, setRunning] = useState(false);
  const [monitor, setMonitor] = useState<MonitorState>({});
  const [stopping, setStopping] = useState(false);
  const [stepTest, setStepTest] = useState<StepTestState | null>(null);
  const [stepTestStopping, setStepTestStopping] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchStatus = async (): Promise<boolean> => {
    try {
      const r = await scenarioApi.playbackStatus();
      const data = r.data || {};
      setRunning(!!data.running);
      setMonitor(data.monitor || {});
      // 재생이 끝나면 '중지 중' 표시 해제. (이 함수는 마운트 시 타이머에 캡처된 클로저라
      // stopping 값을 조건으로 쓰면 항상 초기값 false 로 남아 다음 재생에서도 스피너가 고착됨)
      if (!data.running) setStopping(false);
      const st: StepTestState | null = data.step_test || null;
      setStepTest(st);
      setStepTestStopping(!!st?.stopping);
      return !!st;
    } catch {
      // backend 연결 실패 등 - 무시
      return false;
    }
  };

  useEffect(() => {
    let alive = true;
    // 스텝 테스트 진행 중엔 1초, 평소엔 3초 주기로 폴링 (setTimeout 체인 — 주기 가변)
    const tick = async () => {
      const stepTestActive = await fetchStatus();
      if (!alive) return;
      timerRef.current = setTimeout(tick, stepTestActive ? POLL_STEP_TEST_MS : POLL_IDLE_MS);
    };
    const kick = () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      tick();
    };
    tick();
    window.addEventListener(STEP_TEST_EVENT, kick);
    return () => {
      alive = false;
      window.removeEventListener(STEP_TEST_EVENT, kick);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleStopStepTest = async () => {
    setStepTestStopping(true);
    try {
      await scenarioApi.stopTestStep();
    } catch {
      setStepTestStopping(false);
    }
    setTimeout(fetchStatus, 300);
  };

  const handleStop = async () => {
    setStopping(true);
    try {
      // status === 'stopping' 이면 현재 스텝이 끝날 때까지 백엔드 재생 태스크가 살아 있다.
      // 폴링이 running=false 를 볼 때까지 '중지 중' 표시를 유지한다.
      await scenarioApi.stopPlayback();
    } catch {
      setStopping(false);
    }
    // 즉시 재조회 — 백엔드가 상태를 반영할 때까지 잠시 대기
    setTimeout(fetchStatus, 500);
  };

  if (!running) {
    if (!stepTest) return null;
    return (
      <Alert
        type="info"
        showIcon
        icon={<ExperimentOutlined />}
        style={{ marginBottom: 6 }}
        message={
          <Space size="middle" wrap>
            <strong>
              {stepTestStopping
                ? (t('playbackBanner.stepTestStopping') || '스텝 테스트 중지 중 (동작은 수행, 대기 생략)')
                : (t('playbackBanner.stepTestRunning') || '스텝 테스트 중')}
            </strong>
            {stepTest.scenario_name && <Tag color="blue">{stepTest.scenario_name}</Tag>}
            {stepTest.step_id != null && (
              <span>
                {t('playbackBanner.step') || '스텝'}: <strong>{stepTest.step_id}</strong>
              </span>
            )}
            {stepTest.command && (
              <span style={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>{stepTest.command}</span>
            )}
            <Popconfirm
              title={t('playbackBanner.stepTestStopConfirm') || '스텝 테스트를 중단하시겠습니까?'}
              onConfirm={handleStopStepTest}
              okText={t('common.confirm')}
              cancelText={t('common.cancel')}
            >
              <Button danger size="small" icon={<StopOutlined />} loading={stepTestStopping} disabled={stepTestStopping}>
                {t('scenario.stop') || '중지'}
              </Button>
            </Popconfirm>
          </Space>
        }
      />
    );
  }

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
          <strong>
            {stopping
              ? (t('playbackBanner.stopping') || '중지 중 (현재 스텝 종료 대기)')
              : (t('playbackBanner.running') || '재생 중')}
          </strong>
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
            <Button danger size="small" icon={<StopOutlined />} loading={stopping} disabled={stopping}>
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
