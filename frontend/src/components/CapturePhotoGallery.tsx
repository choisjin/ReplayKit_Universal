import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Empty, Image, Modal, Select, Space, Tag, Tooltip } from 'antd';
import { CameraOutlined, ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons';
import { useTranslation } from '../i18n';

// Capture 일괄보기 — 결과 런 폴더 Capture/ 아래의 모든 사진(미러 화면 Capture 버튼 저장물)을
// 전체화면 그리드로 훑어본다. 사이클 폴더(c{N})로 회차를 구분하고, 한 줄 개수(배열)를 조절할 수 있다.
// 클릭하면 antd Image 미리보기로 크게 본다. (Webcam.Capture 영상/프레임 슬라이드 뷰어는
// CaptureGalleryModal/CaptureVideoViewer 에 그대로 보관 — `#test` 모드에서만 노출)

export interface CapturePhoto {
  name: string;
  /** results/ 기준 상대경로 */
  rel_path: string;
  url: string;
  /** 0 = 사이클 폴더 없음 */
  cycle: number;
  size: number;
  mtime: string;
  /** 그룹 결과일 때 멤버 시나리오 이름 */
  scenario?: string;
}

interface Props {
  open: boolean;
  items: CapturePhoto[];
  /** 열 때 먼저 보여줄 사이클 (결과 상세에서 보던 회차) */
  initialCycle?: number;
  onClose: () => void;
  /** 그리드 한 줄 개수 초기값 (0 = 폭에 맞춰 자동) */
  defaultGridColumns?: number;
}

const GRID_MAX_COLS = 24;
const GRID_AUTO_MIN_PX = 160;

const fmtTime = (ts?: string | null) => {
  if (!ts) return '';
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleTimeString();
};

export default function CapturePhotoGallery({ open, items, initialCycle, onClose, defaultGridColumns = 6 }: Props) {
  const { t } = useTranslation();
  const [cycleFilter, setCycleFilter] = useState<number | 'all'>('all');
  const [gridCols, setGridCols] = useState(defaultGridColumns);
  const gridRef = useRef<HTMLDivElement>(null);

  const cycleCounts = useMemo(() => {
    const m = new Map<number, number>();
    for (const it of items) m.set(it.cycle, (m.get(it.cycle) || 0) + 1);
    return [...m.entries()].sort((a, b) => a[0] - b[0]);
  }, [items]);

  // 열 때: 결과 상세에서 보던 회차 (그 회차에 사진이 없으면 전체)
  useEffect(() => {
    if (!open) return;
    const has = initialCycle != null && items.some(it => it.cycle === initialCycle);
    setCycleFilter(has && cycleCounts.length > 1 ? initialCycle! : 'all');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const visible = useMemo(
    () => items.filter(it => cycleFilter === 'all' || it.cycle === cycleFilter),
    [items, cycleFilter],
  );

  // 그리드 한 줄 개수 — 자동(0)이면 현재 폭 기준 칸 수에서 출발
  const currentCols = () => {
    if (gridCols > 0) return gridCols;
    const w = gridRef.current?.clientWidth ?? 900;
    return Math.max(1, Math.floor((w + 6) / (GRID_AUTO_MIN_PX + 6)));
  };
  const zoomIn = () => setGridCols(Math.max(1, currentCols() - 1));
  const zoomOut = () => setGridCols(Math.min(GRID_MAX_COLS, currentCols() + 1));
  const noFocus = (e: React.MouseEvent) => e.preventDefault();
  const big = gridCols > 0 && gridCols <= 3;

  const cycleLabel = (c: number) => (c > 0 ? `Cycle ${c}` : t('capture.temp'));

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
        body: { flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' },
      }}
      title={<Space size={6}><CameraOutlined />{t('capture.bulkView')}<Tag style={{ margin: 0 }}>{items.length}</Tag></Space>}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        <Select
          size="small"
          value={cycleFilter}
          onChange={(v) => setCycleFilter(v)}
          showSearch
          optionFilterProp="label"
          style={{ width: 220 }}
          options={[
            { value: 'all', label: `${t('capture.allCycles')} (${items.length})` },
            ...cycleCounts.map(([c, n]) => ({ value: c, label: `${cycleLabel(c)} (${n})` })),
          ]}
        />
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>
          <Tooltip title={t('capture.zoomOut')}>
            <Button size="small" type="text" icon={<ZoomOutOutlined />} onMouseDown={noFocus} onClick={zoomOut}
              disabled={gridCols >= GRID_MAX_COLS} />
          </Tooltip>
          <Tooltip title={t('capture.resetCols')}>
            <span
              onClick={() => setGridCols(0)}
              style={{ fontSize: 11, minWidth: 58, textAlign: 'center', cursor: 'pointer', userSelect: 'none' }}
            >
              {gridCols > 0 ? t('capture.columns', { n: gridCols }) : t('capture.autoCols')}
            </span>
          </Tooltip>
          <Tooltip title={t('capture.zoomIn')}>
            <Button size="small" type="text" icon={<ZoomInOutlined />} onMouseDown={noFocus} onClick={zoomIn}
              disabled={gridCols === 1} />
          </Tooltip>
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: '#888' }}>{visible.length} / {items.length}</span>
      </div>

      {visible.length === 0 ? (
        <Empty style={{ marginTop: 80 }} description={t('capture.noPhotos')} />
      ) : (
        <div
          ref={gridRef}
          style={{
            flex: 1, minHeight: 0, overflowY: 'auto', padding: 2,
            display: 'grid', alignContent: 'start', gap: 6,
            gridTemplateColumns: gridCols > 0
              ? `repeat(${gridCols}, minmax(0, 1fr))`
              : `repeat(auto-fill, minmax(${GRID_AUTO_MIN_PX}px, 1fr))`,
          }}
        >
          <Image.PreviewGroup>
            {visible.map(it => (
              <div
                key={it.rel_path}
                style={{
                  borderRadius: 4, overflow: 'hidden', background: '#000',
                  border: '1px solid rgba(128,128,128,0.35)', display: 'flex', flexDirection: 'column',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', aspectRatio: '16 / 9', overflow: 'hidden' }}>
                  <Image
                    src={it.url}
                    alt={it.name}
                    loading="lazy"
                    style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
                    wrapperStyle={{ width: '100%', height: '100%' }}
                    preview={{ mask: false }}
                  />
                </div>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 4, padding: '2px 6px',
                  color: '#fff', background: 'rgba(0,0,0,0.65)', fontSize: big ? 12 : 10, lineHeight: big ? '18px' : '15px',
                }}>
                  <Tag color={it.cycle > 0 ? 'blue' : 'default'} style={{ margin: 0, fontSize: big ? 11 : 9, lineHeight: big ? '16px' : '14px', padding: '0 4px' }}>
                    {it.cycle > 0 ? `C${it.cycle}` : 'T'}
                  </Tag>
                  <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={it.rel_path}>
                    {it.scenario ? `${it.scenario} · ` : ''}{it.name}
                  </span>
                  <span style={{ flexShrink: 0, opacity: 0.8 }}>{fmtTime(it.mtime)}</span>
                </div>
              </div>
            ))}
          </Image.PreviewGroup>
        </div>
      )}
    </Modal>
  );
}
