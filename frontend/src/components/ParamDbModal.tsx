import React, { useState } from 'react';
import { AutoComplete, Button, Empty, Input, Modal, Popconfirm, Space, Table, Tabs, Tooltip, Upload, message } from 'antd';
import { DatabaseOutlined, DownloadOutlined, ImportOutlined, InboxOutlined, PlusOutlined } from '@ant-design/icons';
import { paramDbApi } from '../services/api';
import { useTranslation } from '../i18n';

/**
 * 모듈 함수 파라미터 DB — 함수 선택 드롭다운 우측의 "DB / 가져오기 / 내보내기" 버튼 묶음.
 *
 * DB 는 서버(backend/param_db/<module>/<function>.csv)에 저장되어 같은 서버 사용자끼리
 * 공유된다. CSV 의 Sheet 열 값이 모달의 카테고리 탭으로 표시되고, Description 열이
 * 목록의 기준(맨 왼쪽) 열이다. "불러오기"를 누르면 해당 행의 인자+설명으로 스텝이
 * 자동 추가된다(onLoadRow).
 */

interface ParamDbRow {
  index: number;
  description: string;
  args: Record<string, string>;
}

interface ParamDbSheet {
  name: string;
  rows: ParamDbRow[];
}

interface ParamDbData {
  exists: boolean;
  headers: string[];
  param_names: string[];
  sheets: ParamDbSheet[];
  total: number;
}

interface Props {
  module: string;
  func: string;
  /** 현재 파라미터 입력값 — "현재 입력값 저장"에 사용 */
  currentArgs: Record<string, string>;
  /** DB 행 불러오기 → 입력 필드 채우기 + 스텝 자동 추가 */
  onLoadRow: (args: Record<string, string>, description: string) => void;
  isDark?: boolean;
}

const ParamDbButtons: React.FC<Props> = ({ module, func, currentArgs, onLoadRow, isDark }) => {
  const { t } = useTranslation();
  const [dbOpen, setDbOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [data, setData] = useState<ParamDbData | null>(null);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [activeSheet, setActiveSheet] = useState<string>('');
  // 현재 입력값 저장 폼
  const [saveOpen, setSaveOpen] = useState(false);
  const [saveDesc, setSaveDesc] = useState('');
  const [saveSheet, setSaveSheet] = useState('');
  const [saving, setSaving] = useState(false);
  // 행 수정 폼 (index = 파일 내 절대 인덱스)
  const [editing, setEditing] = useState<{ index: number; description: string; sheet: string; args: Record<string, string> } | null>(null);
  const [updating, setUpdating] = useState(false);

  const fetchData = async (): Promise<ParamDbData | null> => {
    setLoading(true);
    try {
      const res = await paramDbApi.get(module, func);
      setData(res.data);
      return res.data;
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('paramDb.loadDbFailed'));
      return null;
    } finally {
      setLoading(false);
    }
  };

  const openDb = async () => {
    setDbOpen(true);
    setSaveOpen(false);
    setEditing(null);
    const d = await fetchData();
    if (d && d.sheets.length > 0) setActiveSheet(d.sheets[0].name);
  };

  const handleExport = async () => {
    try {
      const res = await paramDbApi.exportCsv(module, func);
      const filename = `${module}.${func}.csv`;
      // Chrome 계열: 저장 위치 선택 다이얼로그 (원하는 폴더에 복사본 생성)
      const picker = (window as any).showSaveFilePicker;
      if (typeof picker === 'function') {
        try {
          const handle = await picker({
            suggestedName: filename,
            types: [{ description: 'CSV', accept: { 'text/csv': ['.csv'] } }],
          });
          const writable = await handle.createWritable();
          await writable.write(res.data);
          await writable.close();
          return;
        } catch (err: any) {
          if (err?.name === 'AbortError') return; // 사용자가 취소
          // picker 실패 시 일반 다운로드 폴백
        }
      }
      const url = URL.createObjectURL(res.data);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('paramDb.exportFailed'));
    }
  };

  const handleImport = async (file: File) => {
    setImporting(true);
    try {
      const res = await paramDbApi.importCsv(module, func, file);
      message.success(t('paramDb.importDone', { count: res.data.total }));
      (res.data.warnings || []).forEach((w: string) => message.warning(w, 6));
      setImportOpen(false);
      // DB 모달이 열려 있으면 갱신
      if (dbOpen) {
        const d = await fetchData();
        if (d && d.sheets.length > 0) setActiveSheet(d.sheets[0].name);
      }
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('paramDb.importFailed'));
    } finally {
      setImporting(false);
    }
  };

  const handleSaveCurrent = async () => {
    if (!saveDesc.trim()) {
      message.warning(t('paramDb.descRequired'));
      return;
    }
    setSaving(true);
    try {
      await paramDbApi.addRow(module, func, {
        sheet: saveSheet.trim(),
        description: saveDesc.trim(),
        args: currentArgs,
      });
      message.success(t('paramDb.saved'));
      setSaveDesc('');
      setSaveOpen(false);
      const d = await fetchData();
      // 저장한 시트 탭으로 이동
      if (d) setActiveSheet(saveSheet.trim() || 'Default');
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('paramDb.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const handleUpdateRow = async () => {
    if (!editing) return;
    if (!editing.description.trim()) {
      message.warning(t('paramDb.descRequired'));
      return;
    }
    setUpdating(true);
    try {
      await paramDbApi.updateRow(module, func, {
        index: editing.index,
        sheet: editing.sheet.trim(),
        description: editing.description.trim(),
        args: editing.args,
      });
      message.success(t('paramDb.updated'));
      const target = editing.sheet.trim() || 'Default';
      setEditing(null);
      await fetchData();
      setActiveSheet(target);
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('paramDb.updateFailed'));
    } finally {
      setUpdating(false);
    }
  };

  const handleDeleteRow = async (index: number) => {
    try {
      await paramDbApi.deleteRow(module, func, index);
      // 삭제로 절대 인덱스가 밀리므로 수정 중이던 행 참조는 폐기
      setEditing(null);
      await fetchData();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('paramDb.deleteFailed'));
    }
  };

  // 값은 잘라내지 않는다 — 전체를 표시하되, 아주 긴 값만 420px 에서 줄바꿈.
  // (x: 'max-content' 스크롤과 조합: 열은 내용 폭만큼 넓어지고 초과분은 가로 스크롤)
  const cellStyle: React.CSSProperties = {
    fontFamily: 'monospace',
    fontSize: 11,
    maxWidth: 420,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-all',
    display: 'inline-block',
    verticalAlign: 'top',
  };

  const buildColumns = (headers: string[], sheetName: string) => [
    {
      title: t('paramDb.description'),
      dataIndex: 'description',
      key: '__desc',
      fixed: 'left' as const,
      width: 180,
      render: (v: string) => (
        <span style={{ fontWeight: 600, wordBreak: 'break-word' }}>{v}</span>
      ),
    },
    ...headers.map(h => ({
      title: h,
      key: h,
      render: (_: any, row: ParamDbRow) => (
        <span style={cellStyle}>{row.args[h]}</span>
      ),
    })),
    {
      title: '',
      key: '__actions',
      fixed: 'right' as const,
      width: 168,
      render: (_: any, row: ParamDbRow) => (
        <Space size={4}>
          <Button size="small" type="primary" onClick={() => onLoadRow({ ...row.args }, row.description)}>
            {t('paramDb.load')}
          </Button>
          <Button
            size="small"
            onClick={() => {
              setSaveOpen(false);
              setEditing({
                index: row.index,
                description: row.description,
                sheet: sheetName === 'Default' ? '' : sheetName,
                args: { ...row.args },
              });
            }}
          >
            {t('paramDb.edit')}
          </Button>
          <Popconfirm title={t('paramDb.deleteConfirm')} onConfirm={() => handleDeleteRow(row.index)}>
            <Button size="small" danger>{t('paramDb.delete')}</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const renderSheetTable = (sheet: ParamDbSheet) => (
    <Table
      size="small"
      rowKey="index"
      dataSource={sheet.rows}
      columns={buildColumns(data?.headers || [], sheet.name)}
      pagination={sheet.rows.length > 50 ? { pageSize: 50, size: 'small' } : false}
      scroll={{ x: 'max-content', y: 400 }}
    />
  );

  const existingSheets = (data?.sheets || []).map(s => s.name).filter(n => n !== 'Default');

  return (
    <>
      <Space.Compact size="small">
        <Tooltip title={t('paramDb.dbTooltip')}>
          <Button size="small" icon={<DatabaseOutlined />} onClick={openDb}>DB</Button>
        </Tooltip>
        <Tooltip title={t('paramDb.importTooltip')}>
          <Button size="small" icon={<ImportOutlined />} onClick={() => setImportOpen(true)}>{t('paramDb.import')}</Button>
        </Tooltip>
        <Tooltip title={t('paramDb.exportTooltip')}>
          <Button size="small" icon={<DownloadOutlined />} onClick={handleExport}>{t('paramDb.export')}</Button>
        </Tooltip>
      </Space.Compact>

      {/* DB 목록 모달 */}
      <Modal
        title={<span><DatabaseOutlined /> {t('paramDb.title', { name: `${module}.${func}` })}</span>}
        open={dbOpen}
        onCancel={() => setDbOpen(false)}
        footer={null}
        width={860}
        destroyOnClose
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {/* 현재 입력값 저장 */}
          {saveOpen ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 8, borderRadius: 6, background: isDark ? '#1a2332' : '#f0f7ff' }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <Input
                  size="small"
                  placeholder={t('paramDb.descPlaceholder')}
                  value={saveDesc}
                  onChange={e => setSaveDesc(e.target.value)}
                  onPressEnter={handleSaveCurrent}
                  style={{ flex: 2 }}
                  autoFocus
                />
                <AutoComplete
                  size="small"
                  placeholder={t('paramDb.sheetPlaceholder')}
                  value={saveSheet}
                  onChange={v => setSaveSheet(v)}
                  options={existingSheets.map(s => ({ value: s }))}
                  style={{ flex: 1 }}
                />
                <Button size="small" type="primary" loading={saving} onClick={handleSaveCurrent}>{t('common.save')}</Button>
                <Button size="small" onClick={() => setSaveOpen(false)}>{t('common.cancel')}</Button>
              </div>
              {/* 저장될 현재 입력값 미리보기 */}
              <div style={{ fontSize: 11, color: isDark ? '#8bb4e0' : '#1677ff' }}>{t('paramDb.currentValues')}</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                {(data?.headers.length ? data.headers : Object.keys(currentArgs)).map(h => (
                  <span
                    key={h}
                    style={{
                      fontFamily: 'monospace', fontSize: 11, padding: '1px 6px', borderRadius: 4,
                      background: isDark ? '#111a26' : '#fff',
                      border: `1px solid ${isDark ? '#2a3a50' : '#d6e8fc'}`,
                      wordBreak: 'break-all',
                    }}
                  >
                    <span style={{ color: isDark ? '#8bb4e0' : '#1677ff' }}>{h}</span>
                    {' = '}
                    {currentArgs[h] ?? ''}
                  </span>
                ))}
              </div>
            </div>
          ) : !editing ? (
            <Button size="small" icon={<PlusOutlined />} style={{ alignSelf: 'flex-start' }} onClick={() => { setEditing(null); setSaveOpen(true); setSaveSheet(activeSheet === 'Default' ? '' : activeSheet); }}>
              {t('paramDb.saveCurrent')}
            </Button>
          ) : null}

          {/* 행 수정 폼 */}
          {editing && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 8, borderRadius: 6, background: isDark ? '#2a2416' : '#fffbe6', border: `1px solid ${isDark ? '#4a3f1e' : '#ffe58f'}` }}>
              <div style={{ fontSize: 11, fontWeight: 600 }}>{t('paramDb.editRow')}</div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <Input
                  size="small"
                  placeholder={t('paramDb.descPlaceholder')}
                  value={editing.description}
                  onChange={e => setEditing(prev => prev && { ...prev, description: e.target.value })}
                  style={{ flex: 2 }}
                  autoFocus
                />
                <AutoComplete
                  size="small"
                  placeholder={t('paramDb.sheetPlaceholder')}
                  value={editing.sheet}
                  onChange={v => setEditing(prev => prev && { ...prev, sheet: v })}
                  options={existingSheets.map(s => ({ value: s }))}
                  style={{ flex: 1 }}
                />
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {(data?.headers.length ? data.headers : Object.keys(editing.args)).map(h => (
                  <div key={h} style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 140, flex: '1 1 140px', maxWidth: 260 }}>
                    <span style={{ fontSize: 10, fontFamily: 'monospace', color: isDark ? '#b0a060' : '#ad8b00' }}>{h}</span>
                    <Input
                      size="small"
                      value={editing.args[h] ?? ''}
                      onChange={e => setEditing(prev => prev && { ...prev, args: { ...prev.args, [h]: e.target.value } })}
                      style={{ fontFamily: 'monospace', fontSize: 11 }}
                    />
                  </div>
                ))}
              </div>
              <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                <Button size="small" onClick={() => setEditing(null)}>{t('common.cancel')}</Button>
                <Button size="small" type="primary" loading={updating} onClick={handleUpdateRow}>{t('common.save')}</Button>
              </div>
            </div>
          )}

          {loading ? null : !data || data.total === 0 ? (
            <Empty description={t('paramDb.empty')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : data.sheets.length > 1 ? (
            <Tabs
              size="small"
              activeKey={data.sheets.some(s => s.name === activeSheet) ? activeSheet : data.sheets[0].name}
              onChange={setActiveSheet}
              items={data.sheets.map(s => ({
                key: s.name,
                label: `${s.name} (${s.rows.length})`,
                children: renderSheetTable(s),
              }))}
            />
          ) : (
            renderSheetTable(data.sheets[0])
          )}
        </div>
      </Modal>

      {/* CSV 가져오기 모달 (드래그앤드롭) */}
      <Modal
        title={t('paramDb.importTitle', { name: `${module}.${func}` })}
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        footer={null}
        width={480}
        destroyOnClose
      >
        <Upload.Dragger
          accept=".csv"
          multiple={false}
          showUploadList={false}
          disabled={importing}
          beforeUpload={(file) => { handleImport(file as unknown as File); return false; }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">{t('paramDb.dragHint')}</p>
          <p className="ant-upload-hint">{t('paramDb.importReplaceHint')}</p>
        </Upload.Dragger>
      </Modal>
    </>
  );
};

export default ParamDbButtons;
