import { useEffect, useState } from 'react';

// 실험적(테스트 전용) 모듈 — `#test` 모드에서만 디바이스/모듈/함수 목록에 노출.
// 별도 로그인 없이 나만 쓰는 실험 기능 게이트. DevicePage/RecordPage 가 공유한다.
export const TEST_ONLY_MODULES = new Set<string>(['Frame_Check']);

/**
 * URL hash `#test` 여부를 추적하는 훅 (AdminPage 의 `#admin` 과 동일한 방식).
 *
 * 실험적 기능(예: Frame_Check 모듈/함수)을 별도 로그인 없이 나만 쓰는
 * "테스트 모드"에서만 노출/사용 가능하게 하는 용도. 주소창에 `#test` 를 붙이면
 * 즉시 켜지고, 지우면 즉시 꺼진다(hashchange 감지). SPA 메뉴 이동은 hash 를
 * 건드리지 않으므로 세션 동안 페이지를 넘나들어도 유지된다.
 *
 * 사용 예:
 *   const testMode = useTestMode();
 *   if (testMode) { ...실험적 UI... }
 */
export function useTestMode(): boolean {
  const [testMode, setTestMode] = useState<boolean>(
    typeof window !== 'undefined' && window.location.hash === '#test'
  );

  useEffect(() => {
    const onHashChange = () => setTestMode(window.location.hash === '#test');
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  return testMode;
}
