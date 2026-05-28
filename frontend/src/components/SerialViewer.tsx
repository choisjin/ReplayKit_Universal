import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Tabs, Tag, Tooltip } from 'antd';
import { CloseOutlined, DownloadOutlined, ClearOutlined } from '@ant-design/icons';
import { useTranslation } from '../i18n';

export interface SerialSessionInfo {
  session_id: string;     // "{port}@{bps}" 형식
  port?: string;
  bps?: number;
  save_path?: string;
  started_at?: number;
}

export interface SerialViewerProps {
  sessions: SerialSessionInfo[];
  onClose?: () => void;
  mode?: 'modal' | 'card';
  theme?: 'light' | 'dark';
}

const MAX_LOG_LINES = 50000;

/**
 * Serial 로그 뷰어.
 *
 * - sessions 배열로 활성 세션을 받아 탭으로 표시
 * - 각 세션 탭 선택 시 /ws/serial-log/{session_id} 구독
 * - AutoScroll 토글, 라인 ring buffer(MAX_LOG_LINES)
 */
const SerialViewer: React.FC<SerialViewerProps> = ({ sessions, onClose, mode = 'modal', theme = 'dark' }) => {
  const { t } = useTranslation();

  const [activeSid, setActiveSid] = useState<string | null>(sessions[0]?.session_id ?? null);
  const [logsBySession, setLogsBySession] = useState<Record<string, string[]>>({});
  const [autoScroll, setAutoScroll] = useState(true);

  const logEndRef = useRef<HTMLDivElement>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (sessions.length === 0) {
      setActiveSid(null);
      return;
    }
    if (!activeSid || !sessions.find((s) => s.session_id === activeSid)) {
      setActiveSid(sessions[0].session_id);
    }
  }, [sessions, activeSid]);

  useEffect(() => {
    if (!activeSid) {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      return;
    }

    setLogsBySession((prev) => (prev[activeSid] ? prev : { ...prev, [activeSid]: [] }));

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const encodedSid = encodeURIComponent(activeSid);
    const url = `${protocol}//${window.location.host}/ws/serial-log/${encodedSid}`;
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
      } catch { /* ignore */ }
    };
    ws.onerror = () => { /* server ping keeps alive */ };

    return () => {
      try { ws.close(); } catch { /* ignore */ }
      if (wsRef.current === ws) wsRef.current = null;
    };
  }, [activeSid]);

  useEffect(() => {
    if (!autoScroll || !activeSid) return;
    const el = logEndRef.current;
    if (el) el.scrollIntoView({ block: 'end' });
  }, [logsBySession, autoScroll, activeSid]);

  const currentLogs = useMemo(() => (activeSid ? logsBySession[activeSid] || [] : []), [logsBySession, activeSid]);

  const clearLogs = useCallback(() => {
    if (!activeSid) return;
    setLogsBySession((prev) => ({ ...prev, [activeSid]: [] }));
  }, [activeSid]);

  const downloadLogs = useCallback(() => {
    if (!activeSid) return;
    const lines = currentLogs.join('\n');
    const blob = new Blob([lines], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `serial_${activeSid.replace(/[^a-zA-Z0-9]/g, '_')}_${Date.now()}.log`;
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
          <span style={{ fontWeight: 600 }}>{'Serial 로그 뷰어'}</span>
          {onClose && <Button size="small" icon={<CloseOutlined />} onClick={onClose} />}
        </div>
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#888' }}>
          {'활성 Serial 세션이 없습니다.'}
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
      <div style={{ padding: '8px 12px', borderBottom: `1px solid ${borderColor}`, display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ fontWeight: 600 }}>{'Serial 로그 뷰어'}</span>
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
        {currentLogs.length === 0 ? (
          <div style={{ color: '#888' }}>{t('dltViewer.waiting') || '로그 수신 대기 중…'}</div>
        ) : (
          currentLogs.map((ln, i) => <div key={i}>{ln}</div>)
        )}
        <div ref={logEndRef} />
      </div>
    </div>
  );
};

export default SerialViewer;
