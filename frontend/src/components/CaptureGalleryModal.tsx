import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Empty, Modal, Select, Space, Tag, Tooltip } from 'antd';
import { LeftOutlined, RightOutlined, VideoCameraOutlined } from '@ant-design/icons';
import CaptureVideoViewer from './CaptureVideoViewer';
import { useTranslation } from '../i18n';

// Capture 일괄보기 — 시나리오 결과의 전 사이클 Webcam.Capture 영상을 전체화면에서 훑어본다.
//   좌: 사이클 선택 + 캡처 목록 / 우: 선택 캡처의 영상·슬라이드·그리드(CaptureVideoViewer).
//   F/R(또는 ◀▶ 버튼) = 이전/다음 캡처. 캡처를 바꿔도 보던 모드·그리드 크기는 유지된다.

export interface CaptureItem {
  /** results/ 기준 상대경로 */
  path: string;
  cycle: number;
  stepId: number;
  description?: string;
  command?: string;
  timestamp?: string | null;
  status?: string;
  /** 그룹 결과일 때 멤버 시나리오 이름 */
  scenario?: string;
}

interface Props {
  open: boolean;
  items: CaptureItem[];
  /** 열 때 먼저 보여줄 사이클 (결과 상세에서 보던 회차) */
  initialCycle?: number;
  onClose: () => void;
}

const fmtTime = (ts?: string | null) => {
  if (!ts) return '';
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString();
};

export default function CaptureGalleryModal({ open, items, initialCycle, onClose }: Props) {
  const { t } = useTranslation();
  const [cycleFilter, setCycleFilter] = useState<number | 'all'>('all');
  const [selected, setSelected] = useState(0);
  const [autoPlay, setAutoPlay] = useState(false);
  const [viewH, setViewH] = useState(() => window.innerHeight);
  const listRef = useRef<HTMLDivElement>(null);

  const cycleCounts = useMemo(() => {
    const m = new Map<number, number>();
    for (const it of items) m.set(it.cycle, (m.get(it.cycle) || 0) + 1);
    return [...m.entries()].sort((a, b) => a[0] - b[0]);
  }, [items]);

  useEffect(() => {
    const onResize = () => setViewH(window.innerHeight);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // 열 때: 결과 상세에서 보던 회차의 첫 캡처부터 (그 회차에 없으면 전체의 첫 캡처)
  useEffect(() => {
    if (!open) return;
    const i = initialCycle != null ? items.findIndex(it => it.cycle === initialCycle) : -1;
    setAutoPlay(false);
    setSelected(i >= 0 ? i : 0);
    setCycleFilter(i >= 0 && cycleCounts.length > 1 ? initialCycle! : 'all');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const visible = useMemo(
    () => items.map((it, i) => ({ it, i })).filter(({ it }) => cycleFilter === 'all' || it.cycle === cycleFilter),
    [items, cycleFilter],
  );
  const pos = visible.findIndex(v => v.i === selected);
  const current = pos >= 0 ? items[selected] : undefined;

  const select = (i: number, play = false) => {
    setAutoPlay(play);
    setSelected(i);
  };

  const switchCapture = useCallback((dir: 1 | -1, wasPlaying: boolean) => {
    const next = visible[visible.findIndex(v => v.i === selected) + dir];
    if (!next) return false;
    setAutoPlay(wasPlaying);
    setSelected(next.i);
    return true;
  }, [visible, selected]);

  const onCycleChange = (v: number | 'all') => {
    setCycleFilter(v);
    if (v === 'all') return;
    const i = items.findIndex(it => it.cycle === v);
    if (i >= 0) select(i);
  };

  // 선택 항목이 목록에서 보이게
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector(`[data-cap="${selected}"]`);
    if (el) (el as HTMLElement).scrollIntoView({ block: 'nearest' });
  }, [open, selected, cycleFilter]);

  const noFocus = (e: React.MouseEvent) => e.preventDefault();

  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnClose
      width="100vw"
      style={{ top: 0, maxWidth: '100vw', margin: 0, paddingBottom: 0 }}
      styles={{
        content: { height: '100vh', borderRadius: 0, padding: '10px 14px', display: 'flex', flexDirection: 'column' },
        header: { marginBottom: 8 },
        body: { flex: 1, minHeight: 0 },
      }}
      title={<Space size={6}><VideoCameraOutlined />{t('capture.bulkView')}<Tag style={{ margin: 0 }}>{items.length}</Tag></Space>}
    >
      <div style={{ display: 'flex', gap: 10, height: '100%' }}>
        {/* 좌: 사이클 선택 + 캡처 목록 */}
        <div style={{ width: 280, flexShrink: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <Select
            size="small"
            value={cycleFilter}
            onChange={onCycleChange}
            showSearch
            optionFilterProp="label"
            style={{ width: '100%', marginBottom: 6 }}
            options={[
              { value: 'all', label: `${t('capture.allCycles')} (${items.length})` },
              ...cycleCounts.map(([c, n]) => ({ value: c, label: `Cycle ${c} (${n})` })),
            ]}
          />
          <div
            ref={listRef}
            style={{ flex: 1, overflowY: 'auto', border: '1px solid rgba(128,128,128,0.3)', borderRadius: 4 }}
          >
            {visible.map(({ it, i }) => (
              <div
                key={i}
                data-cap={i}
                onClick={() => select(i)}
                style={{
                  padding: '6px 8px', cursor: 'pointer', borderBottom: '1px solid rgba(128,128,128,0.15)',
                  background: i === selected ? 'rgba(22,119,255,0.18)' : undefined,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <Tag color="blue" style={{ margin: 0 }}>C{it.cycle}</Tag>
                  <span style={{ fontWeight: 600, fontSize: 12 }}>Step {it.stepId}</span>
                  {it.status && it.status !== 'pass' && (
                    <Tag color="red" style={{ margin: 0, fontSize: 10 }}>{it.status.toUpperCase()}</Tag>
                  )}
                  <span style={{ flex: 1 }} />
                  <span style={{ fontSize: 10, color: '#888' }}>{fmtTime(it.timestamp)}</span>
                </div>
                {it.scenario && <div style={{ fontSize: 11, color: '#888' }}>{it.scenario}</div>}
                {(it.description || it.command) && (
                  <div style={{ fontSize: 11, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {it.description || it.command}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* 우: 선택 캡처 */}
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
          {current ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6, minWidth: 0 }}>
                <Tooltip title={`${t('capture.prevCapture')} (R)`}>
                  <Button size="small" icon={<LeftOutlined />} onMouseDown={noFocus}
                    onClick={() => switchCapture(-1, false)} disabled={pos <= 0} />
                </Tooltip>
                <Tag color="blue" style={{ margin: 0 }}>Cycle {current.cycle}</Tag>
                <strong style={{ whiteSpace: 'nowrap' }}>Step {current.stepId}</strong>
                <span style={{ color: '#888', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {current.scenario ? `${current.scenario} · ` : ''}{current.description || current.command}
                </span>
                <span style={{ flex: 1 }} />
                <span style={{ fontSize: 12, color: '#888', whiteSpace: 'nowrap' }}>{pos + 1} / {visible.length}</span>
                <Tooltip title={`${t('capture.nextCapture')} (F)`}>
                  <Button size="small" icon={<RightOutlined />} onMouseDown={noFocus}
                    onClick={() => switchCapture(1, false)} disabled={pos >= visible.length - 1} />
                </Tooltip>
              </div>
              <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
                <CaptureVideoViewer
                  videoPath={current.path}
                  maxHeight={Math.max(240, viewH - 190)}
                  onSwitchRecording={switchCapture}
                  autoPlay={autoPlay}
                  defaultGridColumns={6}
                />
              </div>
            </>
          ) : (
            <Empty style={{ marginTop: 80 }} />
          )}
        </div>
      </div>
    </Modal>
  );
}
