import { Card, InputNumber, Select, Space, Switch, message, Typography } from 'antd';
import { useSettings } from '../context/SettingsContext';
import { useTranslation } from '../i18n';

const { Text } = Typography;

export default function SettingsPage() {
  const { settings, updateSettings } = useSettings();
  const { t } = useTranslation();

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
              value={settings.default_wait_ms}
              onChange={(v) => updateSettings({ default_wait_ms: v ?? 3000 })}
              suffix="ms"
              style={{ width: 120 }}
            />
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
                  value={settings[key]}
                  onChange={(v) => updateSettings({ [key]: v ?? 0.95 })}
                  style={{ width: 80 }}
                />
                <span style={{ color: '#888', fontSize: 11 }}>{Math.round((settings[key] ?? 0.95) * 100)}%</span>
              </Space>
            ))}
          </div>
        </Card>
      </Space>
    </div>
  );
}
