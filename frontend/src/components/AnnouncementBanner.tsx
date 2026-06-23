import { useEffect, useRef, useState } from 'react';
import { Alert, Badge, Button, Modal, Space, Tag, Typography } from 'antd';
import { NotificationOutlined, ExpandOutlined } from '@ant-design/icons';
import { Announcement, managerImageUrl, useManagerUrl } from '../lib/manager';

export default function AnnouncementBanner() {
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const wsRef = useRef<WebSocket | null>(null);

  const adminUrl = useManagerUrl();

  useEffect(() => {
    if (!adminUrl) return;

    // REST로 초기 공지 로드
    fetch(`${adminUrl}/api/announcements?active_only=true`)
      .then(r => r.json())
      .then(data => setAnnouncements(data))
      .catch(() => {});

    // WebSocket으로 실시간 업데이트 구독
    const wsUrl = adminUrl.replace(/^http/, 'ws') + '/ws/announcements';
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      if (data.type === 'announcements' && data.announcements) {
        setAnnouncements(data.announcements);
      }
    };

    ws.onclose = () => {
      // 재연결 시도
      setTimeout(() => {
        if (wsRef.current === ws) {
          wsRef.current = null;
        }
      }, 5000);
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, [adminUrl]);

  const visibleAnnouncements = announcements.filter(a => !dismissed.has(a.id));

  if (!adminUrl || visibleAnnouncements.length === 0) return null;

  const priorityType: Record<string, 'error' | 'warning' | 'info'> = {
    urgent: 'error',
    important: 'warning',
    normal: 'info',
  };

  const topAnn = visibleAnnouncements[0];

  return (
    <>
      <Alert
        type={priorityType[topAnn.priority] || 'info'}
        banner
        showIcon
        icon={<NotificationOutlined />}
        message={
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Space size={8}>
              <strong>{topAnn.title}</strong>
              <span style={{ color: 'rgba(255,255,255,0.65)', fontSize: 11 }}>
                {topAnn.content.length > 80 ? topAnn.content.slice(0, 80) + '...' : topAnn.content}
              </span>
              {visibleAnnouncements.length > 1 && (
                <Badge count={visibleAnnouncements.length} size="small" style={{ backgroundColor: '#1677ff' }} />
              )}
            </Space>
            <Button
              type="text"
              size="small"
              icon={<ExpandOutlined />}
              onClick={() => setDetailOpen(true)}
              style={{ color: 'inherit' }}
            >
              크게보기
            </Button>
          </div>
        }
        closable
        onClose={() => setDismissed(prev => new Set(prev).add(topAnn.id))}
        style={{ marginBottom: 6, borderRadius: 6 }}
      />

      <Modal
        title={
          <Space>
            <NotificationOutlined />
            <span>공지사항</span>
            <Badge count={visibleAnnouncements.length} size="small" />
          </Space>
        }
        open={detailOpen}
        onCancel={() => setDetailOpen(false)}
        footer={<Button onClick={() => setDetailOpen(false)}>닫기</Button>}
        width={700}
      >
        {visibleAnnouncements.map(ann => {
          const priorityLabel: Record<string, string> = { urgent: '긴급', important: '중요', normal: '일반' };
          const priorityColor: Record<string, string> = { urgent: 'red', important: 'orange', normal: 'blue' };
          const imgUrl = managerImageUrl(adminUrl, ann.image_path);
          return (
            <div key={ann.id} style={{ marginBottom: 13, padding: 13, background: 'rgba(255,255,255,0.04)', borderRadius: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                <Space>
                  <Tag color={priorityColor[ann.priority]}>{priorityLabel[ann.priority]}</Tag>
                  <Typography.Title level={5} style={{ margin: 0 }}>{ann.title}</Typography.Title>
                </Space>
                <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                  {new Date(ann.created_at).toLocaleString('ko-KR')}
                </Typography.Text>
              </div>
              {imgUrl && (
                <div style={{ margin: '6px 0' }}>
                  <img
                    src={imgUrl}
                    alt={ann.title}
                    style={{ maxWidth: '100%', borderRadius: 6, display: 'block' }}
                    onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
                  />
                </div>
              )}
              <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                {ann.content}
              </Typography.Paragraph>
            </div>
          );
        })}
      </Modal>
    </>
  );
}
