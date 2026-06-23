import { useEffect, useMemo, useState } from 'react';
import { Empty, Modal, Space, Tag, Typography } from 'antd';
import { NotificationOutlined } from '@ant-design/icons';
import { isGuide } from '../lib/manager';
import { useAnnouncements } from '../context/AnnouncementsContext';
import AnnouncementBody from './AnnouncementBody';

const priorityLabel: Record<string, string> = { urgent: '긴급', important: '중요', normal: '일반' };
const priorityColor: Record<string, string> = { urgent: 'red', important: 'orange', normal: 'blue' };

/**
 * 공지사항 목록 모달 (좌: 목록 / 우: 상세).
 * 사이드바 "공지사항" 버튼, 배너 "크게보기", 팝업 "전체 목록" 에서 공통으로 연다.
 * 읽기 전용 — 최신순 정렬, 열릴 때 최신 공지를 기본 선택.
 */
export default function AnnouncementListModal() {
  const { announcements, listOpen, closeList } = useAnnouncements();
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const sorted = useMemo(
    () => [...announcements].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [announcements],
  );

  // 모달 열릴 때 최신 공지를 기본 선택 (이미 유효한 선택이 있으면 유지).
  useEffect(() => {
    if (!listOpen) return;
    setSelectedId((prev) =>
      prev != null && sorted.some((a) => a.id === prev) ? prev : sorted[0]?.id ?? null,
    );
  }, [listOpen, sorted]);

  const selected = sorted.find((a) => a.id === selectedId) || null;

  return (
    <Modal
      title={
        <Space>
          <NotificationOutlined />
          <span>공지사항</span>
        </Space>
      }
      open={listOpen}
      onCancel={closeList}
      footer={null}
      width={780}
    >
      {sorted.length === 0 ? (
        <Empty description="공지사항이 없습니다" />
      ) : (
        <div style={{ display: 'flex', gap: 12, height: 460 }}>
          {/* 좌: 목록 */}
          <div
            style={{
              width: 240,
              overflow: 'auto',
              borderRight: '1px solid rgba(128,128,128,0.2)',
              paddingRight: 8,
              flexShrink: 0,
            }}
          >
            {sorted.map((a) => (
              <div
                key={a.id}
                onClick={() => setSelectedId(a.id)}
                style={{
                  padding: '8px 10px',
                  marginBottom: 4,
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: a.id === selectedId ? 'rgba(22,119,255,0.15)' : 'transparent',
                }}
              >
                <Tag color={priorityColor[a.priority] || 'blue'} style={{ marginInlineEnd: 0, marginBottom: 2 }}>
                  {priorityLabel[a.priority] || '일반'}
                </Tag>
                <div
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {a.title}
                </div>
                <div style={{ fontSize: 10, color: '#888' }}>
                  {new Date(a.created_at).toLocaleDateString('ko-KR')}
                </div>
              </div>
            ))}
          </div>
          {/* 우: 상세 */}
          <div style={{ flex: 1, overflow: 'auto' }}>
            {selected && (
              <>
                <Space style={{ marginBottom: 8 }}>
                  <Tag color={priorityColor[selected.priority] || 'blue'}>
                    {priorityLabel[selected.priority] || '일반'}
                  </Tag>
                  {isGuide(selected) && <Tag color="purple">가이드</Tag>}
                  <Typography.Title level={5} style={{ margin: 0 }}>
                    {selected.title}
                  </Typography.Title>
                </Space>
                <div style={{ fontSize: 11, color: '#888', marginBottom: 8 }}>
                  {new Date(selected.created_at).toLocaleString('ko-KR')}
                </div>
                <AnnouncementBody ann={selected} />
              </>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
