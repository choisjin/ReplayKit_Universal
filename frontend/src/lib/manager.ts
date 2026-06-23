import { useSettings } from '../context/SettingsContext';

// 매니저(ReplayKit Manager, 관리 서버) 기본 주소.
// 설정(admin_server_url)이 비어 있을 때만 이 값으로 폴백한다.
const DEFAULT_MANAGER_URL = 'http://10.176.144.70:9000';

// 매니저가 제공하는 공지 스키마 (읽기 전용 소비).
export interface Announcement {
  id: number;
  title: string;
  content: string;
  priority: string; // "normal" | "important" | "urgent"
  active: number;
  image_path?: string | null; // 매니저 이미지 폴더 내 파일명. 있으면 이미지 URL 구성
  is_popup?: number; // 1이면 시작 시 팝업으로 표시
  created_at: string;
  updated_at?: string;
}

/** 설정의 admin_server_url 을 우선 사용하고, 비어 있으면 기본 IP 로 폴백. */
export function useManagerUrl(): string {
  const { settings } = useSettings();
  return (settings.admin_server_url || '').trim() || DEFAULT_MANAGER_URL;
}

/** image_path → `${base}/images/<파일명>` URL. 없으면 null. */
export function managerImageUrl(base: string, imagePath?: string | null): string | null {
  if (!imagePath) return null;
  return `${base.replace(/\/$/, '')}/images/${imagePath}`;
}

// ── "오늘 하루 그만 보기" 영구 저장 (localStorage) ──
// 저장 형태: { [공지id]: "YYYY-MM-DD" }
const DISMISS_KEY = 'popup_dismiss';

/** 로컬 날짜 "YYYY-MM-DD". */
export function todayStr(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function readDismiss(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(DISMISS_KEY) || '{}');
  } catch {
    return {};
  }
}

/** 주어진 공지 id 들을 오늘 날짜로 "그만 보기" 처리. */
export function dismissPopupsToday(ids: number[]): void {
  const d = readDismiss();
  const t = todayStr();
  ids.forEach((id) => {
    d[id] = t;
  });
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify(d));
  } catch {
    /* 저장 실패는 무시 (시크릿 모드 등) */
  }
}
