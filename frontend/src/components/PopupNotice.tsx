import { useEffect, useRef, useState } from 'react';
import { Button, Checkbox, Modal, Space, Tag, Typography } from 'antd';
import { NotificationOutlined } from '@ant-design/icons';
import { dismissPopupsToday, readDismiss, todayStr } from '../lib/manager';
import { useAnnouncements } from '../context/AnnouncementsContext';

const priorityLabel: Record<string, string> = { urgent: '긴급', important: '중요', normal: '일반' };
const priorityColor: Record<string, string> = { urgent: 'red', important: 'orange', normal: 'blue' };

/**
 * 시작 시 팝업 공지.
 * 여러 팝업 공지(is_popup === 1)가 있어도 **최신 1개만** 모달로 표시하고,
 * "전체 목록" 버튼으로 다른 공지를 골라 볼 수 있게 한다(목록 모달 오픈).
 * "오늘 하루 그만 보기" 체크 후 닫으면 오늘 표시 대상 팝업 전체가 오늘 동안 다시 뜨지 않는다
 * (이후에는 사이드바 "공지사항" 버튼으로 언제든 열람 가능).
 */
export default function PopupNotice() {
  const { announcements, openList } = useAnnouncements();
  const [open, setOpen] = useState(false);
  const [dontShowToday, setDontShowToday] = useState(false);
  const shownRef = useRef(false);

  // 시작 시 1회: 오늘 미차단인 팝업 공지가 생기면 표시.
  useEffect(() => {
    if (shownRef.current || announcements.length === 0) return;
    const t = todayStr();
    const dismiss = readDismiss();
    if (announcements.some((a) => a.is_popup === 1 && dismiss[a.id] !== t)) {
      shownRef.current = true;
      setOpen(true);
    }
  }, [announcements]);

  const t = todayStr();
  const dismiss = readDismiss();
  const candidates = announcements
    .filter((a) => a.is_popup === 1 && dismiss[a.id] !== t)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  const top = candidates[0];

  const applyDismiss = () => {
    if (dontShowToday) dismissPopupsToday(candidates.map((c) => c.id));
  };
  const handleClose = () => {
    applyDismiss();
    setOpen(false);
  };
  const handleOpenList = () => {
    applyDismiss();
    setOpen(false);
    openList();
  };

  if (!open || !top) return null;
  const others = candidates.length - 1;

  return (
    <Modal
      title={
        <Space>
          <NotificationOutlined />
          <span>공지사항</span>
        </Space>
      }
      open={open}
      onCancel={handleClose}
      maskClosable={false}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Checkbox checked={dontShowToday} onChange={(e) => setDontShowToday(e.target.checked)}>
            오늘 하루 그만 보기
          </Checkbox>
          <Space>
            <Button onClick={handleOpenList}>{others > 0 ? `전체 목록 (외 ${others}건)` : '전체 목록'}</Button>
            <Button type="primary" onClick={handleClose}>
              닫기
            </Button>
          </Space>
        </div>
      }
      width={640}
    >
      <Space style={{ marginBottom: 8 }}>
        <Tag color={priorityColor[top.priority] || 'blue'}>{priorityLabel[top.priority] || '일반'}</Tag>
        <Typography.Title level={5} style={{ margin: 0 }}>
          {top.title}
        </Typography.Title>
      </Space>
      <div style={{ fontSize: 11, color: '#888', marginBottom: 8 }}>
        {new Date(top.created_at).toLocaleString('ko-KR')}
      </div>
      {top.image_data && (
        <div style={{ margin: '8px 0' }}>
          <img
            src={top.image_data}
            alt={top.title}
            style={{ maxWidth: '100%', borderRadius: 6, display: 'block' }}
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = 'none';
            }}
          />
        </div>
      )}
      <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{top.content}</Typography.Paragraph>
    </Modal>
  );
}
