import { useState } from 'react';
import { Alert, Badge, Button, Space } from 'antd';
import { NotificationOutlined, ExpandOutlined } from '@ant-design/icons';
import { useAnnouncements } from '../context/AnnouncementsContext';

/**
 * 상단 배너 — 활성 공지의 대표 1건을 띄우고, "크게보기" 로 공통 목록 모달을 연다.
 * 데이터는 AnnouncementsContext(단일 소스) 사용. 배너 닫기는 세션 한정.
 */
export default function AnnouncementBanner() {
  const { announcements, openList } = useAnnouncements();
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());

  const visible = announcements.filter((a) => !dismissed.has(a.id));
  if (visible.length === 0) return null;

  const priorityType: Record<string, 'error' | 'warning' | 'info'> = {
    urgent: 'error',
    important: 'warning',
    normal: 'info',
  };

  const top = visible[0];

  return (
    <Alert
      type={priorityType[top.priority] || 'info'}
      banner
      showIcon
      icon={<NotificationOutlined />}
      message={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Space size={8}>
            <strong>{top.title}</strong>
            <span style={{ color: 'rgba(255,255,255,0.65)', fontSize: 11 }}>
              {top.content.length > 80 ? top.content.slice(0, 80) + '...' : top.content}
            </span>
            {visible.length > 1 && (
              <Badge count={visible.length} size="small" style={{ backgroundColor: '#1677ff' }} />
            )}
          </Space>
          <Button type="text" size="small" icon={<ExpandOutlined />} onClick={openList} style={{ color: 'inherit' }}>
            크게보기
          </Button>
        </div>
      }
      closable
      onClose={() => setDismissed((prev) => new Set(prev).add(top.id))}
      style={{ marginBottom: 6, borderRadius: 6 }}
    />
  );
}
