import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Input, InputNumber, Space, Tabs, Tag, Tooltip, message } from 'antd';
import { CloseOutlined, DownloadOutlined, SearchOutlined, ClearOutlined } from '@ant-design/icons';
import { useTranslation } from '../i18n';

export interface DLTSessionInfo {
  session_id: string;
  host?: string;
  port?: number;
  save_path?: string;
  started_at?: number;
}

export interface DLTViewerProps {
  /** 열려있을 활성 세션 목록. 비어있으면 "세션 없음" 표시 */
  sessions: DLTSessionInfo[];
  /** 모달로 사용할 때 닫기 버튼 노출 여부 */
  onClose?: () => void;
  /** 'card' 모드일 때 컨테이너 스타일 조정 */
  mode?: 'modal' | 'card';
  /** 라이트/다크 테마 */
  theme?: 'light' | 'dark';
}

type SearchMode = 'all' | 'section';

interface SearchResult {
  keyword: string;
  count: number;
  matches: string[];
  mode: SearchMode;
  from_step?: number;
  to_step?: number;
}

const MAX_LOG_LINES = 50000;

/**
 * DLT 로그 뷰어.
 *
 * - sessions 배열로 활성 세션을 받아 탭으로 표시
 * - 각 세션 탭 선택 시 /ws/dlt/{session_id} 구독
 * - 상단 검색바: SearchAll / SearchSection(스텝 구간)
 * - AutoScroll 토글, 라인 ring buffer(MAX_LOG_LINES)
 */
const DLTViewer: React.FC<DLTViewerProps> = ({ sessions, onClose, mode = 'modal', theme = 'dark' }) => {
  const { t } = useTranslation();

  const [activeSid, setActiveSid] = useState<string | null>(sessions[0]?.session_id ?? null);
  const [logsBySession, setLogsBySession] = useState<Record<string, string[]>>({});
  const [autoScroll, setAutoScroll] = useState(true);
  const [searchKw, setSearchKw] = useState('');
  const [searchMode, setSearchMode] = useState<SearchMode>('all');
  const [fromStep, setFromStep] = useState<number>(1);
  const [toStep, setToStep] = useState<number>(999);
  const [searchResult, setSearchResult] = useState<SearchResult | null>(null);
  const [searching, setSearching] = useState(false);

  const logEndRef = useRef<HTMLDivElement>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // sessions가 바뀌면 activeSid 보정
  useEffect(() => {
    if (sessions.length === 0) {
      setActiveSid(null);
      return;
    }
    if (!activeSid || !sessions.find((s) => s.session_id === activeSid)) {
      setActiveSid(sessions[0].session_id);
    }
  }, [sessions, activeSid]);

  // 활성 세션 변경 시 WebSocket 연결
  useEffect(() => {
    if (!activeSid) {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      return;
    }

    // 해당 세션에 이미 로그 버퍼가 있으면 유지, 없으면 빈 배열
    setLogsBySession((prev) => (prev[activeSid] ? prev : { ...prev, [activeSid]: [] }));

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const encodedSid = encodeURIComponent(activeSid);
    const url = `${protocol}//${window.location.host}/ws/dlt/${encodedSid}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'backfill' && Array.isArray(msg.logs)) {
          setLogsBySession((prev) => ({ ...prev, [activeSid]: msg.logs.slice(-MAX_LOG_LINES) }));
        } else if (msg.type === 'log' && typeof msg.line === 'string') {
          setLogsBySession((prev) => {
            const cur = prev[activeSid] || [];
            const next = cur.length >= MAX_LOG_LINES ? [...cur.slice(-MAX_LOG_LINES + 1), msg.line] : [...cur, msg.line];
            return { ...prev, [activeSid]: next };
          });
        }
      } catch {
        /* ignore */
      }
    };
    ws.onerror = () => {
      /* keep open — server ping keeps it alive */
    };

    return () => {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      if (wsRef.current === ws) wsRef.current = null;
    };
  }, [activeSid]);

  // AutoScroll
  useEffect(() => {
    if (!autoScroll || !activeSid) return;
    const el = logEndRef.current;
    if (el) el.scrollIntoView({ block: 'end' });
  }, [logsBySession, autoScroll, activeSid]);

  const currentLogs = useMemo(() => (activeSid ? logsBySession[activeSid] || [] : []), [logsBySession, activeSid]);

  const runSearch = useCallback(async () => {
    if (!activeSid || !searchKw.trim()) {
      setSearchResult(null);
      return;
    }
    setSearching(true);
    try {
      const encSid = encodeURIComponent(activeSid);
      const url = searchMode === 'all' ? `/api/dlt/${encSid}/search-all` : `/api/dlt/${encSid}/search-section`;
      const body: any = { keyword: searchKw.trim(), max_results: 500 };
      if (searchMode === 'section') {
        body.from_step = fromStep;
        body.to_step = toStep;
      }
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const errText = await res.text();
        message.error(errText || 'DLT search failed');
        setSearchResult(null);
        return;
      }
      const json = await res.json();
      setSearchResult({
        keyword: json.keyword || searchKw,
        count: json.count || 0,
        matches: json.matches || [],
        mode: searchMode,
        from_step: searchMode === 'section' ? fromStep : undefined,
        to_step: searchMode === 'section' ? toStep : undefined,
      });
    } catch (e: any) {
      message.error(e?.message || 'DLT search error');
    } finally {
      setSearching(false);
    }
  }, [activeSid, searchKw, searchMode, fromStep, toStep]);

  const clearLogs = useCallback(() => {
    if (!activeSid) return;
    setLogsBySession((prev) => ({ ...prev, [activeSid]: [] }));
    setSearchResult(null);
  }, [activeSid]);

  const downloadLogs = useCallback(() => {
    if (!activeSid) return;
    const lines = currentLogs.join('\n');
    const blob = new Blob([lines], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${activeSid.replace(/[^a-zA-Z0-9]/g, '_')}_${Date.now()}.log`;
    a.click();
    URL.revokeObjectURL(url);
  }, [activeSid, currentLogs]);

  const isDark = theme === 'dark';
  const bg = isDark ? '#111' : '#fff';
  const logBg = isDark ? '#000' : '#fafafa';
  const logColor = isDark ? '#e8e8e8' : '#333';
  const borderColor = isDark ? '#333' : '#e0e0e0';

  if (sessions.length === 0) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', flex: 1, width: '100%', height: '100%', background: bg, padding: 13 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontWeight: 600 }}>{t('dltViewer.title') || 'DLT 로그 뷰어'}</span>
          {onClose && <Button size="small" icon={<CloseOutlined />} onClick={onClose} />}
        </div>
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#888' }}>
          {t('dltViewer.noSessions') || '활성 DLT 세션이 없습니다.'}
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        flex: 1,
        width: '100%',
        height: '100%',
        background: bg,
        overflow: 'hidden',
        border: mode === 'card' ? `1px solid ${borderColor}` : undefined,
        borderRadius: mode === 'card' ? 4 : undefined,
      }}
    >
      {/* 헤더 + 탭 */}
      <div style={{ padding: '8px 12px', borderBottom: `1px solid ${borderColor}`, display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ fontWeight: 600 }}>{t('dltViewer.title') || 'DLT 로그 뷰어'}</span>
        <Tag color="processing">{sessions.length} {t('dltViewer.sessions') || '세션'}</Tag>
        <div style={{ flex: 1 }} />
        <Tooltip title={t('dltViewer.autoScroll') || 'AutoScroll'}>
          <Button size="small" type={autoScroll ? 'primary' : 'default'} onClick={() => setAutoScroll((v) => !v)}>
            {autoScroll ? '▼' : '⏸'} Auto
          </Button>
        </Tooltip>
        <Tooltip title={t('dltViewer.clear') || '지우기'}>
          <Button size="small" icon={<ClearOutlined />} onClick={clearLogs} />
        </Tooltip>
        <Tooltip title={t('dltViewer.download') || '다운로드'}>
          <Button size="small" icon={<DownloadOutlined />} onClick={downloadLogs} />
        </Tooltip>
        {onClose && <Button size="small" icon={<CloseOutlined />} onClick={onClose} />}
      </div>

      <Tabs
        size="small"
        activeKey={activeSid ?? undefined}
        onChange={(k) => setActiveSid(k)}
        tabBarStyle={{ padding: '0 12px', margin: 0 }}
        items={sessions.map((s) => ({
          key: s.session_id,
          label: (
            <span>
              <span style={{ fontFamily: 'monospace' }}>{s.session_id}</span>
              <span style={{ color: '#888', marginLeft: 3, fontSize: 10 }}>
                ({(logsBySession[s.session_id] || []).length})
              </span>
            </span>
          ),
        }))}
      />

      {/* 검색바 */}
      <div
        style={{
          padding: '8px 12px',
          borderBottom: `1px solid ${borderColor}`,
          display: 'flex',
          gap: 6,
          alignItems: 'center',
          flexWrap: 'wrap',
        }}
      >
        <Space.Compact size="small">
          <Button type={searchMode === 'all' ? 'primary' : 'default'} onClick={() => setSearchMode('all')}>
            {t('dltViewer.searchAll') || 'Search All'}
          </Button>
          <Button type={searchMode === 'section' ? 'primary' : 'default'} onClick={() => setSearchMode('section')}>
            {t('dltViewer.searchSection') || 'Search Section'}
          </Button>
        </Space.Compact>
        {searchMode === 'section' && (
          <Space.Compact size="small">
            <InputNumber size="small" min={0} value={fromStep} onChange={(v) => setFromStep(v || 0)} style={{ width: 70 }} placeholder="from" />
            <InputNumber size="small" min={0} value={toStep} onChange={(v) => setToStep(v || 0)} style={{ width: 70 }} placeholder="to" />
          </Space.Compact>
        )}
        <Input
          size="small"
          style={{ flex: 1, minWidth: 200 }}
          value={searchKw}
          onChange={(e) => setSearchKw(e.target.value)}
          onPressEnter={runSearch}
          placeholder={t('dltViewer.keywordPlaceholder') || '키워드 (공백=AND)'}
          prefix={<SearchOutlined />}
          allowClear
        />
        <Button size="small" type="primary" loading={searching} onClick={runSearch} disabled={!searchKw.trim()}>
          {t('dltViewer.search') || '검색'}
        </Button>
        {searchResult && (
          <Tag color={searchResult.count > 0 ? 'success' : 'default'}>
            {searchResult.count} {t('dltViewer.matches') || '건'}
          </Tag>
        )}
      </div>

      {/* 본체: 로그 or 검색결과 */}
      <div
        ref={logContainerRef}
        style={{
          flex: 1,
          overflow: 'auto',
          background: logBg,
          color: logColor,
          fontFamily: 'Consolas, "Courier New", monospace',
          fontSize: 11,
          padding: '6px 8px',
          whiteSpace: 'pre',
          lineHeight: 1.4,
        }}
      >
        {searchResult ? (
          <>
            <div style={{ color: '#888', marginBottom: 3, whiteSpace: 'normal' }}>
              {searchResult.mode === 'section'
                ? `[Section ${searchResult.from_step}~${searchResult.to_step}] '${searchResult.keyword}' — ${searchResult.count} 건`
                : `[All] '${searchResult.keyword}' — ${searchResult.count} 건`}
              <Button size="small" type="link" onClick={() => setSearchResult(null)}>
                {t('dltViewer.backToLive') || '실시간으로 복귀'}
              </Button>
            </div>
            {searchResult.matches.length === 0 ? (
              <div style={{ color: '#888' }}>{t('dltViewer.noMatches') || '매칭된 로그가 없습니다.'}</div>
            ) : (
              searchResult.matches.map((ln, i) => <div key={i}>{ln}</div>)
            )}
          </>
        ) : (
          <>
            {currentLogs.length === 0 ? (
              <div style={{ color: '#888' }}>{t('dltViewer.waiting') || '로그 수신 대기 중…'}</div>
            ) : (
              currentLogs.map((ln, i) => <div key={i}>{ln}</div>)
            )}
            <div ref={logEndRef} />
          </>
        )}
      </div>
    </div>
  );
};

export default DLTViewer;
