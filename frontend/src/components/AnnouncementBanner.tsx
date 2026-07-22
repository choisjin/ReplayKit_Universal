import { useState } from 'react';
import { Alert, Badge, Button, Space } from 'antd';
import { NotificationOutlined } from '@ant-design/icons';
import { useAnnouncements } from '../context/AnnouncementsContext';
import { annContent, annTitle } from '../lib/manager';
import { useTranslation } from '../i18n';

/**
 * 상단 배너 — 활성 공지의 대표 1건을 띄운다. 배너 닫기는 세션 한정.
 * "공지사항" 버튼은 배너 밖 우측에 상시 노출 — 배너를 닫거나 활성 공지가
 * 없어도 목록 모달을 열 수 있어야 한다 (사이드바 버튼 대체).
 */
export default function AnnouncementBanner() {
  const { announcements, openList } = useAnnouncements();
  const { t, lang } = useTranslation();
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());

  const visible = announcements.filter((a) => !dismissed.has(a.id));

  const priorityType: Record<string, 'error' | 'warning' | 'info'> = {
    urgent: 'error',
    important: 'warning',
    normal: 'info',
  };

  const top = visible[0];

  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 6, marginBottom: 6 }}>
      {top && (
        <Alert
          type={priorityType[top.priority] || 'info'}
          banner
          showIcon
          icon={<NotificationOutlined />}
          message={
            <Space size={8}>
              <strong>{annTitle(top, lang)}</strong>
              <span style={{ fontSize: 11, opacity: 0.7 }}>
                {annContent(top, lang).length > 80 ? annContent(top, lang).slice(0, 80) + '...' : annContent(top, lang)}
              </span>
              {visible.length > 1 && (
                <Badge count={visible.length} size="small" style={{ backgroundColor: '#1677ff' }} />
              )}
            </Space>
          }
          closable
          onClose={() => setDismissed((prev) => new Set(prev).add(top.id))}
          style={{ flex: 1, borderRadius: 6 }}
        />
      )}
      <Button size="small" icon={<NotificationOutlined />} onClick={() => openList()}>
        {t('announce.title')}
      </Button>
    </div>
  );
}
