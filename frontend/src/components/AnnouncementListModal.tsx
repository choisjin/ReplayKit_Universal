import { useEffect, useMemo, useState } from 'react';
import { Button, Checkbox, Empty, Modal, Space, Tag, Typography } from 'antd';
import { NotificationOutlined } from '@ant-design/icons';
import {
  annTitle,
  dismissPopupsForever,
  dismissPopupsToday,
  isGuide,
  isPopupDismissed,
  readDismiss,
  todayStr,
} from '../lib/manager';
import { useAnnouncements } from '../context/AnnouncementsContext';
import { useTranslation } from '../i18n';
import AnnouncementBody from './AnnouncementBody';

const priorityColor: Record<string, string> = { urgent: 'red', important: 'orange', normal: 'blue' };

/**
 * 공지사항 목록 모달 (좌: 목록 / 우: 상세).
 * 사이드바 "공지사항" 버튼, 배너 "크게보기", 그리고 **시작 시 팝업**이 모두 이 모달을 쓴다.
 * 예전에는 시작 팝업만 단일 공지를 보여주는 별도 모달이었으나, 여러 공지를 오가며
 * 보기 어려워 목록형으로 통일했다.
 *
 * popupMode(시작 시 자동 오픈)일 때만 '오늘 하루 그만보기 / 다시 보지 않기' 를 노출한다.
 * 수동 열람(사이드바·배너)에서는 차단할 대상이 없어 혼란만 주므로 숨긴다.
 */
export default function AnnouncementListModal() {
  const { announcements, listOpen, popupMode, closeList } = useAnnouncements();
  const { t, lang } = useTranslation();
  const pLabel = (p: string) =>
    t(p === 'urgent' ? 'announce.priorityUrgent' : p === 'important' ? 'announce.priorityImportant' : 'announce.priorityNormal');
  const dateLocale = lang === 'en' ? 'en-US' : 'ko-KR';
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [dontShowToday, setDontShowToday] = useState(false);
  const [dontShowEver, setDontShowEver] = useState(false);

  const sorted = useMemo(
    () => [...announcements].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [announcements],
  );

  // 팝업 모드에서 차단 대상 — 아직 오늘 차단되지 않은 팝업 공지들.
  const popupCandidates = useMemo(() => {
    if (!popupMode) return [];
    const today = todayStr();
    const dismiss = readDismiss();
    return sorted.filter((a) => a.is_popup === 1 && !isPopupDismissed(dismiss, a.id, today));
  }, [popupMode, sorted]);

  // 모달 열릴 때 기본 선택 — 팝업 모드면 '띄우려던 최신 팝업 공지', 아니면 최신 공지.
  useEffect(() => {
    if (!listOpen) return;
    const fallback = (popupMode ? popupCandidates[0]?.id : undefined) ?? sorted[0]?.id ?? null;
    setSelectedId((prev) => (prev != null && sorted.some((a) => a.id === prev) ? prev : fallback));
  }, [listOpen, popupMode, popupCandidates, sorted]);

  // 닫혔다 다시 열릴 때 체크 상태가 남지 않도록 초기화
  useEffect(() => {
    if (!listOpen) {
      setDontShowToday(false);
      setDontShowEver(false);
    }
  }, [listOpen]);

  const selected = sorted.find((a) => a.id === selectedId) || null;

  const handleClose = () => {
    if (popupMode) {
      const ids = popupCandidates.map((c) => c.id);
      // "다시 보지 않기"(영구)가 "오늘 하루 그만 보기"보다 우선한다.
      if (dontShowEver) dismissPopupsForever(ids);
      else if (dontShowToday) dismissPopupsToday(ids);
    }
    closeList();
  };

  return (
    <Modal
      title={
        <Space>
          <NotificationOutlined />
          <span>{t('announce.title')}</span>
        </Space>
      }
      open={listOpen}
      onCancel={handleClose}
      maskClosable={!popupMode}
      footer={
        popupMode ? (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Space size="large">
              <Checkbox
                checked={dontShowToday}
                disabled={dontShowEver}
                onChange={(e) => setDontShowToday(e.target.checked)}
              >
                {t('announce.dontShowToday')}
              </Checkbox>
              <Checkbox checked={dontShowEver} onChange={(e) => setDontShowEver(e.target.checked)}>
                {t('announce.dontShowEver')}
              </Checkbox>
            </Space>
            <Button type="primary" onClick={handleClose}>
              {t('announce.close')}
            </Button>
          </div>
        ) : null
      }
      // 기존 780×460 의 1.5배. 작은 화면에서 잘리지 않도록 뷰포트 기준 상한을 함께 둔다.
      width="min(1170px, 94vw)"
    >
      {sorted.length === 0 ? (
        <Empty description={t('announce.empty')} />
      ) : (
        <div style={{ display: 'flex', gap: 12, height: 'min(690px, 70vh)' }}>
          {/* 좌: 목록 */}
          <div
            style={{
              width: 360,
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
                <Space size={4} style={{ marginBottom: 2 }}>
                  <Tag color={priorityColor[a.priority] || 'blue'} style={{ marginInlineEnd: 0 }}>
                    {pLabel(a.priority)}
                  </Tag>
                  {isGuide(a) && <Tag color="purple" style={{ marginInlineEnd: 0 }}>{t('announce.guide')}</Tag>}
                </Space>
                <div
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {annTitle(a, lang)}
                </div>
                <div style={{ fontSize: 10, color: '#888' }}>
                  {new Date(a.created_at).toLocaleDateString(dateLocale)}
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
                    {pLabel(selected.priority)}
                  </Tag>
                  {isGuide(selected) && <Tag color="purple">{t('announce.guide')}</Tag>}
                  <Typography.Title level={5} style={{ margin: 0 }}>
                    {annTitle(selected, lang)}
                  </Typography.Title>
                </Space>
                <div style={{ fontSize: 11, color: '#888', marginBottom: 8 }}>
                  {new Date(selected.created_at).toLocaleString(dateLocale)}
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
