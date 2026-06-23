import { useEffect, useState } from 'react';
import { Button, Checkbox, Modal, Space, Tag, Typography } from 'antd';
import { NotificationOutlined } from '@ant-design/icons';
import {
  Announcement,
  dismissPopupsToday,
  managerImageUrl,
  readDismiss,
  todayStr,
  useManagerUrl,
} from '../lib/manager';

/**
 * 시작 시 팝업 공지.
 * 매니저(관리 서버)의 공개 API 에서 활성 공지를 가져와
 * `is_popup === 1` 이고 오늘 "그만 보기" 처리되지 않은 항목을 모달로 표시한다.
 * "오늘 하루 그만 보기" 체크 후 닫으면 그 공지들은 오늘(로컬 날짜) 동안 다시 뜨지 않는다.
 * 읽기 전용 — 수정/삭제 UI 없음.
 */
export default function PopupNotice() {
  const managerUrl = useManagerUrl();
  const [popups, setPopups] = useState<Announcement[]>([]);
  const [open, setOpen] = useState(false);
  const [dontShowToday, setDontShowToday] = useState(false);

  useEffect(() => {
    if (!managerUrl) return;
    let cancelled = false;
    // 시작 시 1회 fetch (실시간 갱신은 AnnouncementBanner 의 WebSocket 이 담당).
    fetch(`${managerUrl}/api/announcements?active_only=true`)
      .then((r) => r.json())
      .then((list: Announcement[]) => {
        if (cancelled || !Array.isArray(list)) return;
        const t = todayStr();
        const dismiss = readDismiss();
        const toShow = list.filter((a) => a.is_popup === 1 && dismiss[a.id] !== t);
        if (toShow.length > 0) {
          setPopups(toShow);
          setOpen(true);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [managerUrl]);

  const handleClose = () => {
    if (dontShowToday) dismissPopupsToday(popups.map((p) => p.id));
    setOpen(false);
  };

  if (popups.length === 0) return null;

  const priorityLabel: Record<string, string> = { urgent: '긴급', important: '중요', normal: '일반' };
  const priorityColor: Record<string, string> = { urgent: 'red', important: 'orange', normal: 'blue' };

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
          <Button type="primary" onClick={handleClose}>
            닫기
          </Button>
        </div>
      }
      width={640}
    >
      <div style={{ maxHeight: '70vh', overflow: 'auto' }}>
        {popups.map((ann, idx) => {
          const imgUrl = managerImageUrl(managerUrl, ann.image_path);
          const last = idx === popups.length - 1;
          return (
            <div
              key={ann.id}
              style={{
                marginBottom: last ? 0 : 16,
                paddingBottom: last ? 0 : 16,
                borderBottom: last ? undefined : '1px solid rgba(128,128,128,0.2)',
              }}
            >
              <Space style={{ marginBottom: 8 }}>
                <Tag color={priorityColor[ann.priority] || 'blue'}>{priorityLabel[ann.priority] || '일반'}</Tag>
                <Typography.Title level={5} style={{ margin: 0 }}>
                  {ann.title}
                </Typography.Title>
              </Space>
              {imgUrl && (
                <div style={{ margin: '8px 0' }}>
                  <img
                    src={imgUrl}
                    alt={ann.title}
                    style={{ maxWidth: '100%', borderRadius: 6, display: 'block' }}
                    // 로드 실패 시 텍스트만 표시 (깨진 이미지 숨김)
                    onError={(e) => {
                      (e.currentTarget as HTMLImageElement).style.display = 'none';
                    }}
                  />
                </div>
              )}
              <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                {ann.content}
              </Typography.Paragraph>
            </div>
          );
        })}
      </div>
    </Modal>
  );
}
