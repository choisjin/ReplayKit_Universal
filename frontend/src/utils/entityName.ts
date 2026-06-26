// 시나리오 / 그룹 / 폴더 이름 검증 유틸.
// 백엔드 backend/app/services/recording_service.py 의 INVALID_NAME_CHARS 와 반드시 동기화할 것.
//
// 금지 사유:
//  - '/' '\\'         : 파일·디렉터리 경로와 URL 라우팅을 깨뜨림
//  - ': * ? " < > |'  : Windows 파일명 금지 문자
//  - '#'              : URL fragment 로 해석되어 경로 파라미터가 잘림 (GET /scenario/a#b → /scenario/a)
export const INVALID_NAME_CHARS = ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '#'];

// 사용자 안내용 표시 문자열 (예: "/ \ : * ? " < > | #")
export const INVALID_NAME_CHARS_DISPLAY = INVALID_NAME_CHARS.join(' ');

/**
 * 이름에 포함된 금지 문자(및 제어문자) 목록을 중복 없이 반환한다.
 * 문제가 없으면 빈 배열.
 */
export function findInvalidNameChars(name: string): string[] {
  const found = new Set<string>();
  for (const ch of name) {
    if (INVALID_NAME_CHARS.includes(ch) || ch.charCodeAt(0) < 32) {
      found.add(ch);
    }
  }
  return [...found];
}

/** 이름이 금지 문자를 포함하지 않으면 true. (빈 문자열 검사는 호출측에서 별도 처리) */
export function isValidEntityName(name: string): boolean {
  return findInvalidNameChars(name).length === 0;
}
