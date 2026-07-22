import { useState } from 'react';
import { Alert, Badge, Button, Space } from 'antd';
import { NotificationOutlined } from '@ant-design/icons';
import { useAnnouncements } from '../context/AnnouncementsContext';
import { annContent, annTitle } from '../lib/manager';
import { useTranslation } from '../i18n';

/**
 * 상단 배너 — 활성 공지의 대표 1건을 띄우고, 우측 "공지사항" 버튼으로 공통 목록 모달을 연다.
 * 데이터는 AnnouncementsContext(단일 소스) 사용. 배너 닫기는 세션 한정.
 */
export default function AnnouncementBanner() {
  const { announcements, openList } = useAnnouncements();
  const { t, lang } = useTranslation();
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());

  const visible = announcements.filter((a) => !dismissed.has(a.id));
  if (visible.length === 0) return null;

  const priorityType: Record<string, 'error' | 'warning' | 'info'> = {
    urgent: 'error',
    important: 'warning',
    normal: 'info',
  };

  const top = visible[0];
  const title = annTitle(top, lang);
  const content = annContent(top, lang);

  return (
    <Alert
      type={priorityType[top.priority] || 'info'}
      banner
      showIcon
      icon={<NotificationOutlined />}
      message={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Space size={8}>
            <strong>{title}</strong>
            <span style={{ fontSize: 11, opacity: 0.7 }}>
              {content.length > 80 ? content.slice(0, 80) + '...' : content}
            </span>
            {visible.length > 1 && (
              <Badge count={visible.length} size="small" style={{ backgroundColor: '#1677ff' }} />
            )}
          </Space>
          <Button type="text" size="small" icon={<NotificationOutlined />} onClick={() => openList()} style={{ color: 'inherit' }}>
            {t('announce.title')}
          </Button>
        </div>
      }
      closable
      onClose={() => setDismissed((prev) => new Set(prev).add(top.id))}
      style={{ marginBottom: 6, borderRadius: 6 }}
    />
  );
}
