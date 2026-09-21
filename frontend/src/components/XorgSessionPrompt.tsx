/**
 * Linux Wayland 세션 감지 시 Xorg 전환 유도 모달 (실행 시 1회).
 *
 * Wayland 에서는 네이티브 앱(터미널/Firefox 등)이 X11 로 열거·캡처·입력되지 않아
 * 합성녹화 윈도우 소스와 WinControl 이 동작하지 않는다.
 * 1단계: [Xorg로 설정] → 백엔드가 pkexec 로 GDM WaylandEnable=false 반영 (관리자 암호창)
 * 2단계: [지금 재부팅]
 * "나중에" 는 이번 실행(탭 세션) 동안만 숨김 — 다음 실행 시 다시 표시.
 */
import { useEffect, useState } from 'react';
import { App, Button, Modal, Steps } from 'antd';
import { WarningOutlined } from '@ant-design/icons';
import { serverApi } from '../services/api';
import { useTranslation } from '../i18n';

const DISMISS_KEY = 'replaykit.xorgPromptDismissed';

interface SessionStatus {
  linux: boolean;
  needs_action: boolean;
  session_type?: string;
  conf_path?: string;
  xorg_configured?: boolean;
  can_auto?: boolean;
}

export default function XorgSessionPrompt() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [status, setStatus] = useState<SessionStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [busy, setBusy] = useState<'' | 'config' | 'reboot'>('');

  useEffect(() => {
    try {
      if (sessionStorage.getItem(DISMISS_KEY)) return;
    } catch { /* ignore */ }
    serverApi.displaySession()
      .then(r => {
        const s: SessionStatus = r.data;
        if (!s?.linux || !s.needs_action) return;
        setStatus(s);
        setConfigured(!!s.xorg_configured);
        setOpen(true);
      })
      .catch(() => { /* 구버전 백엔드 등 — 무시 */ });
  }, []);

  const dismiss = () => {
    try { sessionStorage.setItem(DISMISS_KEY, '1'); } catch { /* ignore */ }
    setOpen(false);
  };

  const configure = async () => {
    setBusy('config');
    try {
      const r = await serverApi.enableXorg();
      if (r.data?.ok) {
        setConfigured(true);
        message.success(t('xorg.configured'));
      } else if (r.data?.cancelled) {
        message.warning(t('xorg.cancelled'));
      }
    } catch (e: any) {
      message.error(t('xorg.failed') + ': ' + (e?.response?.data?.detail || e?.message || e));
    } finally {
      setBusy('');
    }
  };

  const reboot = async () => {
    setBusy('reboot');
    try {
      await serverApi.rebootSystem();
      message.loading(t('xorg.rebooting'), 0);
    } catch (e: any) {
      message.error(t('xorg.rebootFailed') + ': ' + (e?.response?.data?.detail || e?.message || e));
      setBusy('');
    }
  };

  if (!status) return null;
  const canAuto = !!status.can_auto;

  return (
    <Modal
      open={open}
      title={<><WarningOutlined style={{ color: '#faad14', marginRight: 6 }} />{t('xorg.title')}</>}
      onCancel={dismiss}
      maskClosable={false}
      width={520}
      footer={[
        <Button key="later" onClick={dismiss} disabled={!!busy}>{t('xorg.later')}</Button>,
        canAuto && !configured && (
          <Button key="config" type="primary" loading={busy === 'config'} onClick={configure}>
            {t('xorg.configure')}
          </Button>
        ),
        canAuto && configured && (
          <Button key="reboot" type="primary" danger loading={busy === 'reboot'} onClick={reboot}>
            {t('xorg.reboot')}
          </Button>
        ),
      ]}
    >
      <p style={{ fontSize: 13 }}>{t('xorg.desc')}</p>
      {canAuto ? (
        <Steps
          direction="vertical"
          size="small"
          current={configured ? 1 : 0}
          items={[
            { title: t('xorg.configure'), description: t('xorg.step1', { path: status.conf_path || '/etc/gdm3/custom.conf' }) },
            { title: t('xorg.reboot'), description: t('xorg.step2') },
          ]}
        />
      ) : (
        <p style={{ fontSize: 12, color: '#888' }}>{t('xorg.manual')}</p>
      )}
    </Modal>
  );
}
