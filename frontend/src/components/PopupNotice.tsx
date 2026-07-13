import { useEffect, useRef, useState } from 'react';
import { Button, Checkbox, Modal, Space, Tag, Typography } from 'antd';
import { NotificationOutlined } from '@ant-design/icons';
import { annTitle, dismissPopupsForever, dismissPopupsToday, isGuide, readDismiss, todayStr } from '../lib/manager';
import { useAnnouncements } from '../context/AnnouncementsContext';
import { useTranslation } from '../i18n';
import AnnouncementBody from './AnnouncementBody';

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
  const { t: tr, lang } = useTranslation();
  const pLabel = (p: string) =>
    tr(p === 'urgent' ? 'announce.priorityUrgent' : p === 'important' ? 'announce.priorityImportant' : 'announce.priorityNormal');
  const [open, setOpen] = useState(false);
  const [dontShowToday, setDontShowToday] = useState(false);
  const [dontShowEver, setDontShowEver] = useState(false);
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
    const ids = candidates.map((c) => c.id);
    // "다시 보지 않기"(영구)가 "오늘 하루 그만 보기"보다 우선한다.
    if (dontShowEver) dismissPopupsForever(ids);
    else if (dontShowToday) dismissPopupsToday(ids);
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
          <span>{tr('announce.title')}</span>
        </Space>
      }
      open={open}
      onCancel={handleClose}
      maskClosable={false}
      footer={
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <Space size="large">
            <Checkbox
              checked={dontShowToday}
              disabled={dontShowEver}
              onChange={(e) => setDontShowToday(e.target.checked)}
            >
              {tr('announce.dontShowToday')}
            </Checkbox>
            <Checkbox checked={dontShowEver} onChange={(e) => setDontShowEver(e.target.checked)}>
              {tr('announce.dontShowEver')}
            </Checkbox>
          </Space>
          <Space>
            <Button onClick={handleOpenList}>
              {others > 0 ? tr('announce.viewAllOthers', { n: others }) : tr('announce.viewAll')}
            </Button>
            <Button type="primary" onClick={handleClose}>
              {tr('announce.close')}
            </Button>
          </Space>
        </div>
      }
      width={640}
    >
      <Space style={{ marginBottom: 8 }}>
        <Tag color={priorityColor[top.priority] || 'blue'}>{pLabel(top.priority)}</Tag>
        {isGuide(top) && <Tag color="purple">{tr('announce.guide')}</Tag>}
        <Typography.Title level={5} style={{ margin: 0 }}>
          {annTitle(top, lang)}
        </Typography.Title>
      </Space>
      <div style={{ fontSize: 11, color: '#888', marginBottom: 8 }}>
        {new Date(top.created_at).toLocaleString(lang === 'en' ? 'en-US' : 'ko-KR')}
      </div>
      <div style={{ maxHeight: '60vh', overflow: 'auto' }}>
        <AnnouncementBody ann={top} />
      </div>
    </Modal>
  );
}
