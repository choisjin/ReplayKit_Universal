import { createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode } from 'react';
import {
  Announcement,
  isPopupDismissed,
  readDismiss,
  todayStr,
  useManagerUrl,
} from '../lib/manager';

interface AnnouncementsContextType {
  announcements: Announcement[]; // 매니저가 내려준 활성 공지 (단일 소스)
  listOpen: boolean;
  /** 시작 시 자동으로 열린 상태인지. true 면 목록 모달이 '오늘 하루 그만보기 /
   *  다시 보지 않기' 체크박스를 노출한다(수동 열람 시에는 불필요하므로 숨김). */
  popupMode: boolean;
  openList: (opts?: { popup?: boolean }) => void;
  closeList: () => void;
}

const Ctx = createContext<AnnouncementsContextType | null>(null);

/**
 * 공지사항 단일 소스 Provider.
 * 매니저(관리 서버)의 공개 API 에서 활성 공지를 1회 fetch 하고 WebSocket 으로 갱신을 구독한다.
 * 배너 / 시작 팝업 / 목록 모달 / 사이드바 버튼이 모두 이 컨텍스트를 공유한다 (중복 fetch 제거).
 */
export function AnnouncementsProvider({ children }: { children: ReactNode }) {
  const managerUrl = useManagerUrl();
  const [announcements, setAnnouncements] = useState<Announcement[]>([]);
  const [listOpen, setListOpen] = useState(false);
  const [popupMode, setPopupMode] = useState(false);
  const autoOpenedRef = useRef(false);   // 시작 자동 오픈은 세션당 1회만

  useEffect(() => {
    if (!managerUrl) return;
    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const loadOnce = () => {
      fetch(`${managerUrl}/api/announcements?active_only=true`)
        .then((r) => r.json())
        .then((data) => {
          if (!cancelled && Array.isArray(data)) setAnnouncements(data);
        })
        .catch(() => {});
    };

    const connect = () => {
      if (cancelled) return;
      try {
        ws = new WebSocket(managerUrl.replace(/^http/, 'ws') + '/ws/announcements');
        ws.onmessage = (ev) => {
          try {
            const data = JSON.parse(ev.data);
            if (data.type === 'announcements' && Array.isArray(data.announcements)) {
              setAnnouncements(data.announcements);
            }
          } catch {
            /* ignore malformed */
          }
        };
        ws.onclose = () => {
          if (!cancelled) reconnectTimer = setTimeout(connect, 5000);
        };
        ws.onerror = () => {
          try {
            ws?.close();
          } catch {
            /* ignore */
          }
        };
      } catch {
        /* ignore */
      }
    };

    loadOnce();
    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
  }, [managerUrl]);

  // 시작 시 1회: 오늘 미차단인 팝업 공지가 있으면 목록 모달을 팝업 모드로 자동 오픈.
  // (예전에는 PopupNotice 가 자체 모달을 띄웠으나, 목록 모달로 통일하면서
  //  '언제 자동으로 열지' 판단만 남아 공지 소스를 쥔 이 Provider 로 옮겼다)
  useEffect(() => {
    if (autoOpenedRef.current || announcements.length === 0) return;
    const today = todayStr();
    const dismiss = readDismiss();
    if (announcements.some((a) => a.is_popup === 1 && !isPopupDismissed(dismiss, a.id, today))) {
      autoOpenedRef.current = true;
      setPopupMode(true);
      setListOpen(true);
    }
  }, [announcements]);

  const openList = useCallback((opts?: { popup?: boolean }) => {
    setPopupMode(!!opts?.popup);
    setListOpen(true);
  }, []);
  const closeList = useCallback(() => {
    setListOpen(false);
    setPopupMode(false);
  }, []);

  return (
    <Ctx.Provider value={{ announcements, listOpen, popupMode, openList, closeList }}>
      {children}
    </Ctx.Provider>
  );
}

export function useAnnouncements(): AnnouncementsContextType {
  const c = useContext(Ctx);
  if (!c) throw new Error('useAnnouncements must be used within AnnouncementsProvider');
  return c;
}
