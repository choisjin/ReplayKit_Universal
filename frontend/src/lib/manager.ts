import { useSettings } from '../context/SettingsContext';

// 매니저(ReplayKit Manager, 관리 서버) 기본 주소.
// 설정(admin_server_url)이 비어 있을 때만 이 값으로 폴백한다.
const DEFAULT_MANAGER_URL = 'http://10.176.144.70:9000';

// 가이드(type === 'guide')의 단계.
// 매니저 GuideStep 모델의 필드명이 달라질 수 있어 방어적으로 여러 키를 허용한다.
export interface GuideStep {
  text?: string;
  description?: string;
  content?: string;
  image?: string | null;
  image_data?: string | null;
}

// 매니저가 제공하는 공지 스키마 (읽기 전용 소비).
export interface Announcement {
  id: number;
  title: string;
  content: string;
  priority: string; // "normal" | "important" | "urgent"
  active: number;
  type?: string; // "notice"(기본) | "guide"
  images?: string[] | null; // notice: 다중 이미지 (base64 data URL 배열)
  steps?: GuideStep[] | null; // guide: 단계 배열
  image_data?: string | null; // 하위호환: 단일(첫) 이미지 base64 data URL
  is_popup?: number; // 1이면 시작 시 팝업으로 표시
  created_at: string;
  updated_at?: string;
}

/** 표시할 이미지 배열 — images 우선, 없으면 image_data(단일)로 폴백. */
export function announcementImages(a: Announcement): string[] {
  if (Array.isArray(a.images)) {
    const imgs = a.images.filter((s): s is string => !!s);
    if (imgs.length > 0) return imgs;
  }
  return a.image_data ? [a.image_data] : [];
}

/** 가이드 단계의 글 (필드명 방어). */
export function stepText(s: GuideStep): string {
  return s.text ?? s.description ?? s.content ?? '';
}

/** 가이드 단계의 이미지 (필드명 방어). */
export function stepImage(s: GuideStep): string | null {
  return s.image ?? s.image_data ?? null;
}

/** 가이드 형식 여부 — type==='guide' 이고 steps 가 1개 이상. */
export function isGuide(a: Announcement): boolean {
  return a.type === 'guide' && Array.isArray(a.steps) && a.steps.length > 0;
}

/** 설정의 admin_server_url 을 우선 사용하고, 비어 있으면 기본 IP 로 폴백. */
export function useManagerUrl(): string {
  const { settings } = useSettings();
  return (settings.admin_server_url || '').trim() || DEFAULT_MANAGER_URL;
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
