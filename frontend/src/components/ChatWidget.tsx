import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Form, Input, Modal, Typography } from 'antd';
import { SendOutlined, CloseOutlined } from '@ant-design/icons';
import { useSettings } from '../context/SettingsContext';
import { useTranslation } from '../i18n';

const ADMIN_URL = 'http://10.176.144.70:9000';
const STORAGE_KEY = 'chat_session';

interface ChatMessage {
  id: number;
  from: string;
  content: string;
  created_at: string;
}

interface ChatSession {
  name: string;
  department: string;
  messages: ChatMessage[];
}

type ChatState = 'idle' | 'joining' | 'connected' | 'closed';

interface ChatWidgetProps {
  open: boolean;
  onClose: () => void;
}

function loadSession(): ChatSession | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function saveSession(session: ChatSession | null) {
  if (session) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  } else {
    localStorage.removeItem(STORAGE_KEY);
  }
}

export default function ChatWidget({ open, onClose }: ChatWidgetProps) {
  const { settings } = useSettings();
  const { t } = useTranslation();
  const adminUrl = ADMIN_URL;

  const saved = useRef(loadSession());
  const [chatState, setChatState] = useState<ChatState>(saved.current ? 'joining' : 'idle');
  const [messages, setMessages] = useState<ChatMessage[]>(saved.current?.messages || []);
  const [inputVal, setInputVal] = useState('');
  const [adminTyping, setAdminTyping] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const typingTimeout = useRef<ReturnType<typeof setTimeout>>();
  const sessionRef = useRef<{ name: string; department: string } | null>(
    saved.current ? { name: saved.current.name, department: saved.current.department } : null
  );
  const [form] = Form.useForm();

  const isDark = settings.theme === 'dark';

  // 메시지 변경 시 localStorage 저장
  useEffect(() => {
    if (sessionRef.current) {
      saveSession({ ...sessionRef.current, messages });
    }
  }, [messages]);

  // 메시지 스크롤
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const connectWs = useCallback((name: string, department: string) => {
    setChatState('joining');
    sessionRef.current = { name, department };

    const wsUrl = adminUrl.replace(/^http/, 'ws') + '/ws/chat';
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'join', name, department }));
    };

    ws.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      switch (data.type) {
        case 'joined':
          setChatState('connected');
          break;
        case 'message':
          setMessages(prev => [...prev, {
            id: Date.now(),
            from: data.from,
            content: data.content,
            created_at: data.created_at || new Date().toISOString(),
          }]);
          break;
        case 'admin_typing':
          setAdminTyping(true);
          clearTimeout(typingTimeout.current);
          typingTimeout.current = setTimeout(() => setAdminTyping(false), 2000);
          break;
        case 'closed':
          setChatState('closed');
          setMessages(prev => [...prev, {
            id: Date.now(),
            from: 'system',
            content: data.message || '채팅이 종료되었습니다.',
            created_at: new Date().toISOString(),
          }]);
          break;
        case 'error':
          setChatState('idle');
          break;
      }
    };

    ws.onclose = () => {
      setChatState(prev => prev === 'closed' ? 'closed' : 'idle');
    };
  }, [adminUrl]);

  // 저장된 세션이 있고 위젯이 열릴 때만 재접속
  useEffect(() => {
    if (open && saved.current && chatState === 'idle') {
      connectWs(saved.current.name, saved.current.department);
      saved.current = null;
    }
    return () => { if (!open) { wsRef.current?.close(); wsRef.current = null; } };
  }, [open, connectWs]);

  const handleJoin = async () => {
    const values = await form.validateFields();
    connectWs(values.name, values.department);
  };

  const handleSend = () => {
    if (!inputVal.trim() || !wsRef.current) return;
    wsRef.current.send(JSON.stringify({ type: 'message', content: inputVal.trim() }));
    setMessages(prev => [...prev, {
      id: Date.now(),
      from: 'me',
      content: inputVal.trim(),
      created_at: new Date().toISOString(),
    }]);
    setInputVal('');
  };

  const handleReset = () => {
    wsRef.current?.close();
    wsRef.current = null;
    sessionRef.current = null;
    setChatState('idle');
    setMessages([]);
    setInputVal('');
    form.resetFields();
    saveSession(null);
  };

  if (!adminUrl) return null;

  return (
    <Modal
      title={null}
      open={open}
      onCancel={onClose}
      footer={null}
      width={420}
      style={{ top: 'auto', bottom: 80, right: 24, position: 'fixed', margin: 0 }}
      styles={{ body: { padding: 0, height: 500, display: 'flex', flexDirection: 'column' } }}
      mask={false}
      closable={false}
    >
      {/* 헤더 */}
      <div style={{
        padding: '12px 16px',
        background: '#1677ff',
        color: '#fff',
        borderRadius: '8px 8px 0 0',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
      }}>
        <Typography.Text strong style={{ color: '#fff', fontSize: 13 }}>
          {t('chat.title')}
        </Typography.Text>
        <Button type="text" size="small" icon={<CloseOutlined />} onClick={onClose} style={{ color: '#fff' }} />
      </div>

      {chatState === 'idle' || chatState === 'joining' ? (
        /* 이름/부서 입력 폼 */
        <div style={{ padding: 19, flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
          <Typography.Title level={5} style={{ textAlign: 'center', marginBottom: 19 }}>
            {t('chat.enterInfo')}
          </Typography.Title>
          <Form form={form} layout="vertical">
            <Form.Item name="name" label={t('chat.name')} rules={[{ required: true, message: t('chat.nameRequired') }]}>
              <Input placeholder={t('chat.namePlaceholder')} autoFocus />
            </Form.Item>
            <Form.Item name="department" label={t('chat.department')} rules={[{ required: true, message: t('chat.deptRequired') }]}>
              <Input placeholder={t('chat.deptPlaceholder')} />
            </Form.Item>
            <Button type="primary" block size="large" loading={chatState === 'joining'} onClick={handleJoin}>
              {t('chat.startChat')}
            </Button>
          </Form>
        </div>
      ) : (
        /* 채팅 영역 */
        <>
          <div style={{ flex: 1, overflow: 'auto', padding: 10 }}>
            {messages.map(msg => (
              <div
                key={msg.id}
                style={{
                  display: 'flex',
                  justifyContent: msg.from === 'me' ? 'flex-end' : msg.from === 'system' ? 'center' : 'flex-start',
                  marginBottom: 6,
                }}
              >
                {msg.from === 'system' ? (
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>{msg.content}</Typography.Text>
                ) : (
                  <div style={{
                    maxWidth: '75%',
                    padding: '8px 12px',
                    borderRadius: 12,
                    background: msg.from === 'me'
                      ? '#1677ff'
                      : isDark ? '#303030' : '#e8e8e8',
                    color: msg.from === 'me' ? '#fff' : undefined,
                  }}>
                    {msg.from === 'admin' && (
                      <div style={{ fontSize: 10, color: isDark ? '#bababa' : '#666', marginBottom: 2 }}>{t('chat.admin')}</div>
                    )}
                    <div style={{ fontSize: 11 }}>{msg.content}</div>
                    <div style={{
                      fontSize: 9,
                      color: msg.from === 'me' ? 'rgba(255,255,255,0.5)' : isDark ? '#a8a8a8' : '#999',
                      marginTop: 2,
                      textAlign: 'right',
                    }}>
                      {new Date(msg.created_at).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}
                    </div>
                  </div>
                )}
              </div>
            ))}
            {adminTyping && (
              <div style={{ fontSize: 11, color: '#888', padding: '4px 0' }}>{t('chat.adminTyping')}</div>
            )}
            <div ref={messagesEndRef} />
          </div>

          {/* 입력 영역 */}
          {chatState === 'connected' ? (
            <div style={{ padding: '8px 12px', borderTop: `1px solid ${isDark ? '#303030' : '#d0d0d0'}`, display: 'flex', gap: 6 }}>
              <Input
                value={inputVal}
                onChange={e => {
                  setInputVal(e.target.value);
                  wsRef.current?.send(JSON.stringify({ type: 'typing' }));
                }}
                onPressEnter={handleSend}
                placeholder={t('chat.inputPlaceholder')}
                autoFocus
              />
              <Button type="primary" icon={<SendOutlined />} onClick={handleSend} disabled={!inputVal.trim()} />
            </div>
          ) : (
            <div style={{ padding: '12px', textAlign: 'center', borderTop: `1px solid ${isDark ? '#303030' : '#d0d0d0'}` }}>
              <Button onClick={handleReset}>{t('chat.newChat')}</Button>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
