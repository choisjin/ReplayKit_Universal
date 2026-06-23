import { createContext, useCallback, useContext, useEffect, useState, ReactNode } from 'react';
import { Announcement, useManagerUrl } from '../lib/manager';

interface AnnouncementsContextType {
  announcements: Announcement[]; // 매니저가 내려준 활성 공지 (단일 소스)
  listOpen: boolean;
  openList: () => void;
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

  const openList = useCallback(() => setListOpen(true), []);
  const closeList = useCallback(() => setListOpen(false), []);

  return <Ctx.Provider value={{ announcements, listOpen, openList, closeList }}>{children}</Ctx.Provider>;
}

export function useAnnouncements(): AnnouncementsContextType {
  const c = useContext(Ctx);
  if (!c) throw new Error('useAnnouncements must be used within AnnouncementsProvider');
  return c;
}
