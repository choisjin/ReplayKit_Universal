/**
 * 합성녹화 프리셋을 "카메라 목록"의 한 항목처럼 선택하기 위한 공용 헬퍼.
 *
 * 선택 상태의 정본은 백엔드 compositor_presets.json 의 active + enabled 이다
 * (재생 녹화는 백엔드 _compositor_session_start 가 이 값으로 합성/단일 웹캠을 결정).
 * - 프리셋 선택  → activate(name, enabled=true)
 * - 웹캠 선택    → enabled=false (active 이름은 유지)
 * WebcamPip / ScenarioPage 재생 직전 선택 모달이 같은 상태를 보도록 변경 시 이벤트를 쏜다.
 */
import { compositorApi, CompositorLayout } from '../services/api';

export const PRESET_VALUE_PREFIX = 'preset:';
export const COMPOSITOR_PRESET_EVENT = 'compositor-preset-changed';

export interface CompositorPresetState {
  presets: Record<string, CompositorLayout>;
  /** 재생 시 합성녹화로 쓰일 프리셋 이름 (enabled 가 아니면 null) */
  selected: string | null;
}

export const presetValue = (name: string) => `${PRESET_VALUE_PREFIX}${name}`;

export const parsePresetValue = (v: unknown): string | null =>
  typeof v === 'string' && v.startsWith(PRESET_VALUE_PREFIX) ? v.slice(PRESET_VALUE_PREFIX.length) : null;

export async function loadCompositorPresetState(): Promise<CompositorPresetState> {
  try {
    const r = await compositorApi.listPresets();
    const presets: Record<string, CompositorLayout> = r.data?.presets || {};
    const active: string = r.data?.active || '';
    const selected = r.data?.enabled && active && presets[active] ? active : null;
    return { presets, selected };
  } catch {
    return { presets: {}, selected: null };
  }
}

/** 선택 변경 브로드캐스트 (합성녹화 에디터의 활성/사용 토글에서도 호출) */
export function notify(selected: string | null) {
  window.dispatchEvent(new CustomEvent(COMPOSITOR_PRESET_EVENT, { detail: { selected } }));
}

/** 프리셋을 재생 녹화 대상으로 지정. */
export async function selectCompositorPreset(name: string): Promise<void> {
  await compositorApi.activatePreset(name, true);
  notify(name);
}

/** 단일 웹캠 녹화로 복귀 — 활성 프리셋 이름은 유지하고 사용만 끈다. */
export async function deselectCompositorPreset(): Promise<void> {
  // name 생략 = 백엔드가 active 이름 유지, enabled 만 off
  await compositorApi.activatePreset(undefined, false);
  notify(null);
}
