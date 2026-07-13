import { useEffect, useState } from 'react';
import { Button, Card, InputNumber, Select, Space, Switch, message, Typography } from 'antd';
import { useSettings } from '../context/SettingsContext';
import { useTranslation } from '../i18n';
import { scenarioApi } from '../services/api';
import BackupSection from '../components/BackupSection';

const { Text } = Typography;

const THRESHOLD_KEYS = [
  'threshold_full',
  'threshold_single_crop',
  'threshold_full_exclude',
  'threshold_multi_crop',
] as const;
type ThresholdKey = typeof THRESHOLD_KEYS[number];

export default function SettingsPage() {
  const { settings, updateSettings } = useSettings();
  const { t } = useTranslation();

  // 기본 wait 시간 — Apply 버튼으로만 반영. 비워도(null) 자동 초기화하지 않음.
  const [waitMs, setWaitMs] = useState<number | null>(settings.default_wait_ms);
  // 이미지 비교 임계값 — Apply 버튼으로만 반영.
  const [thresholds, setThresholds] = useState<Record<ThresholdKey, number | null>>({
    threshold_full: settings.threshold_full,
    threshold_single_crop: settings.threshold_single_crop,
    threshold_full_exclude: settings.threshold_full_exclude,
    threshold_multi_crop: settings.threshold_multi_crop,
  });

  // 설정 로드/외부 변경 시 로컬 입력값 동기화
  useEffect(() => {
    setWaitMs(settings.default_wait_ms);
  }, [settings.default_wait_ms]);
  useEffect(() => {
    setThresholds({
      threshold_full: settings.threshold_full,
      threshold_single_crop: settings.threshold_single_crop,
      threshold_full_exclude: settings.threshold_full_exclude,
      threshold_multi_crop: settings.threshold_multi_crop,
    });
  }, [settings.threshold_full, settings.threshold_single_crop, settings.threshold_full_exclude, settings.threshold_multi_crop]);

  const handleWaitApply = async () => {
    if (waitMs == null) {
      message.warning(t('common.valueRequired'));
      return;
    }
    try {
      await updateSettings({ default_wait_ms: waitMs });
      message.success(t('common.saved'));
    } catch {
      message.error(t('common.saveFailed'));
    }
  };

  const handleThresholdApply = async () => {
    const partial: Partial<Record<ThresholdKey, number>> = {};
    for (const k of THRESHOLD_KEYS) {
      if (thresholds[k] == null) {
        message.warning(t('common.valueRequired'));
        return;
      }
      partial[k] = thresholds[k] as number;
    }
    try {
      await updateSettings(partial);
      message.success(t('common.saved'));
    } catch {
      message.error(t('common.saveFailed'));
    }
  };

  const handleThemeToggle = async (checked: boolean) => {
    try {
      await updateSettings({ theme: checked ? 'dark' : 'light' });
    } catch {
      message.error(t('settings.themeChanged'));
    }
  };

  const handleLanguageChange = async (lang: 'ko' | 'en') => {
    try {
      await updateSettings({ language: lang });
    } catch {
      message.error(t('common.saveFailed'));
    }
  };

  // LGSI 전용 임시 버튼 — WoohyunBench SendAvnCan → SendCan 일괄 변환
  const [migrating, setMigrating] = useState(false);
  const handleMigrateCan = async () => {
    setMigrating(true);
    try {
      const { data } = await scenarioApi.migrateWoohyunCan();
      message.success(
        `변환 완료: 시나리오 ${data.changed_scenarios}개, 스텝 ${data.changed_steps}개`,
      );
    } catch {
      message.error('변환 실패');
    } finally {
      setMigrating(false);
    }
  };

  return (
    <div>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Card title={t('settings.language')} size="small">
          <Space>
            <Select
              value={settings.language || 'ko'}
              onChange={handleLanguageChange}
              style={{ width: 200 }}
              options={[
                { label: '한국어 (Korean)', value: 'ko' },
                { label: 'English', value: 'en' },
              ]}
            />
          </Space>
          <Text type="secondary" style={{ fontSize: 11, marginTop: 3, display: 'block' }}>
            {t('settings.languageDesc')}
          </Text>
        </Card>

        <Card title={t('settings.theme')} size="small">
          <Space>
            <Text>Light</Text>
            <Switch
              checked={settings.theme === 'dark'}
              onChange={handleThemeToggle}
              checkedChildren="Dark"
              unCheckedChildren="Light"
            />
            <Text>Dark</Text>
          </Space>
        </Card>

        <Card title={t('settings.defaultWaitTitle')} size="small">
          <Space>
            <InputNumber
              size="small"
              min={0}
              step={100}
              value={waitMs}
              onChange={(v) => setWaitMs(v)}
              onPressEnter={handleWaitApply}
              suffix="ms"
              style={{ width: 120 }}
            />
            <Button type="primary" size="small" onClick={handleWaitApply}>{t('common.apply')}</Button>
          </Space>
          <Text type="secondary" style={{ fontSize: 11, marginTop: 3, display: 'block' }}>
            {t('settings.defaultWaitDesc')}
          </Text>
        </Card>

        <Card title={t('settings.thresholdTitle')} size="small">
          <Text type="secondary" style={{ fontSize: 11, marginBottom: 10, display: 'block' }}>
            {t('settings.thresholdDesc')}
          </Text>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {([
              { key: 'threshold_full', label: t('settings.thresholdFull') },
              { key: 'threshold_single_crop', label: t('settings.thresholdCrop') },
              { key: 'threshold_full_exclude', label: t('settings.thresholdExclude') },
              { key: 'threshold_multi_crop', label: t('settings.thresholdMulti') },
            ] as const).map(({ key, label }) => (
              <Space key={key}>
                <span style={{ minWidth: 100, display: 'inline-block' }}>{label}</span>
                <InputNumber
                  size="small"
                  min={0} max={1} step={0.01}
                  value={thresholds[key]}
                  onChange={(v) => setThresholds((prev) => ({ ...prev, [key]: v }))}
                  onPressEnter={handleThresholdApply}
                  style={{ width: 80 }}
                />
                <span style={{ color: '#888', fontSize: 11 }}>
                  {thresholds[key] != null ? `${Math.round(thresholds[key]! * 100)}%` : '—'}
                </span>
              </Space>
            ))}
          </div>
          <div style={{ marginTop: 10 }}>
            <Button type="primary" size="small" onClick={handleThresholdApply}>{t('common.apply')}</Button>
          </div>
        </Card>

        <BackupSection />

        {/* LGSI 전용 임시 마이그레이션 — 기존 시나리오의 SendAvnCan 스텝을 SendCan으로 일괄 변환 */}
        <Card title="시나리오 마이그레이션 (임시)" size="small">
          <Space direction="vertical" size="small">
            <Button danger size="small" loading={migrating} onClick={handleMigrateCan}>
              only_LGSI_Change_CAN_CMD
            </Button>
            <Text type="secondary" style={{ fontSize: 11, display: 'block' }}>
              기존 시나리오의 WoohyunBench <code>SendAvnCan</code> 스텝을 <code>SendCan</code>으로 변환합니다.
              (msg_id/type/payload_hex 유지, mcu=mcu1·channel=B·repeat=0·cycle_ms=200 일괄 적용)
            </Text>
          </Space>
        </Card>
      </Space>
    </div>
  );
}
