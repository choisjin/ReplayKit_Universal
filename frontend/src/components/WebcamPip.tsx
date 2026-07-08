import { useState, useRef, useCallback, useEffect } from 'react';
import { Button, ConfigProvider, Select, Slider, Switch, Tooltip, theme } from 'antd';
import {
  PlayCircleOutlined, PauseOutlined, VideoCameraOutlined,
  SettingOutlined, CloseOutlined, MinusOutlined, AppstoreOutlined,
  AudioOutlined, AudioMutedOutlined,
} from '@ant-design/icons';
import { useWebcam } from '../hooks/useWebcam';
import { useTranslation } from '../i18n';

interface WebcamDeviceLike {
  deviceId: string;
  label: string;
  kind?: string;
}

interface WebcamPipProps {
  webcam: ReturnType<typeof useWebcam>;
  onClose: () => void;
  isDark: boolean;
  onOpenCompositor?: () => void;
}

const RESOLUTION_LABELS: Record<string, string> = {
  '3840x2160': '4K', '2560x1440': 'QHD', '1920x1080': 'FHD',
  '1280x720': 'HD', '960x540': 'qHD', '640x480': 'VGA', '320x240': 'QVGA',
};

export default function WebcamPip({ webcam, onClose, isDark, onOpenCompositor }: WebcamPipProps) {
  const { t } = useTranslation();
  const {
    webcamIndex, webcamDevices, webcamOpen, webcamRecording,
    webcamSettingsOpen, setWebcamSettingsOpen, webcamCapabilities, webcamSettings,
    webcamResolution, webcamResolutions,
    handleWebcamChange, handleWebcamResolutionChange,
    startWebcamRecording, stopWebcamRecording, loadWebcamCapabilities, applyWebcamSetting,
    timestampPosition, setTimestampPosition,
    timestampColor, setTimestampColor,
    timestampFontSize, setTimestampFontSize,
    exposureAuto, setExposureAutoMode,
    audioEnabled, setAudioEnabled, audioDevice, setAudioDevice,
    audioDevices, loadAudioDevices,
  } = webcam as any;

  const [minimized, setMinimized] = useState(false);
  // 타임스탬프는 백엔드 webcam_service._apply_overlay()가 영상 프레임에 직접 그려서 송신.
  // 프론트에서 같은 정보를 또 오버레이하면 두 겹으로 겹쳐 보이므로 여기선 추가 렌더하지 않음.
  // 백엔드 그리기는 녹화 MP4에도 포함되므로 정합성 있는 동작.
  // 백엔드 WebSocket 프리뷰 — JPEG binary frame 수신 → blob URL
  const [previewUrl, setPreviewUrl] = useState<string>('');
  const previewWsRef = useRef<WebSocket | null>(null);
  const previousBlobUrlRef = useRef<string>('');
  useEffect(() => {
    if (!webcamOpen) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/webcam`);
    ws.binaryType = 'blob';
    previewWsRef.current = ws;
    ws.onopen = () => {
      try { ws.send(JSON.stringify({ fps: 15, quality: 70 })); } catch { /* ignore */ }
    };
    ws.onmessage = (ev) => {
      if (ev.data instanceof Blob) {
        const url = URL.createObjectURL(ev.data);
        // 직전 blob URL 해제
        if (previousBlobUrlRef.current) URL.revokeObjectURL(previousBlobUrlRef.current);
        previousBlobUrlRef.current = url;
        setPreviewUrl(url);
      }
    };
    ws.onerror = () => { /* 자동 정리 */ };
    return () => {
      try { ws.close(); } catch { /* ignore */ }
      if (previousBlobUrlRef.current) {
        URL.revokeObjectURL(previousBlobUrlRef.current);
        previousBlobUrlRef.current = '';
      }
      previewWsRef.current = null;
    };
  }, [webcamOpen]);

  const containerRef = useRef<HTMLDivElement>(null);

  // Dragging
  const [position, setPosition] = useState({ x: window.innerWidth - 380, y: window.innerHeight - 460 });
  const dragRef = useRef<{ startX: number; startY: number; posX: number; posY: number } | null>(null);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    dragRef.current = { startX: e.clientX, startY: e.clientY, posX: position.x, posY: position.y };
    const onMouseMove = (ev: MouseEvent) => {
      if (!dragRef.current) return;
      setPosition({
        x: dragRef.current.posX + (ev.clientX - dragRef.current.startX),
        y: dragRef.current.posY + (ev.clientY - dragRef.current.startY),
      });
    };
    const onMouseUp = () => {
      dragRef.current = null;
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
  }, [position]);

  const getContainer = useCallback(() => containerRef.current || document.body, []);

  // Theme colors
  const bg = isDark ? '#1f1f1f' : '#fff';
  const headerBg = isDark ? '#141414' : '#f0f0f0';
  const border = isDark ? '#404040' : '#d0d0d0';
  const titleColor = isDark ? '#e8e8e8' : '#333';
  const btnColor = isDark ? '#cacaca' : '#666';
  const settingsBg = isDark ? '#141414' : '#f5f5f5';
  const labelColor = isDark ? '#dcdcdc' : '#555';
  const subColor = isDark ? '#bababa' : '#999';

  return (
    <ConfigProvider theme={{ algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
      <div
        ref={containerRef}
        style={{
          position: 'fixed',
          left: position.x,
          top: position.y,
          width: 360,
          zIndex: 9999,
          borderRadius: 8,
          boxShadow: isDark ? '0 8px 32px rgba(0,0,0,0.6)' : '0 8px 32px rgba(0,0,0,0.18)',
          background: bg,
          border: `1px solid ${border}`,
        }}
      >
        {/* Header */}
        <div
          onMouseDown={onMouseDown}
          style={{
            display: 'flex', alignItems: 'center', gap: 5,
            padding: '6px 10px',
            background: headerBg,
            borderRadius: '8px 8px 0 0',
            cursor: 'grab',
            userSelect: 'none',
          }}
        >
          <VideoCameraOutlined style={{ color: '#1677ff' }} />
          <span style={{ flex: 1, fontSize: 11, fontWeight: 500, color: titleColor }}>{t('webcam.title')}</span>
          {webcamRecording && (
            <span style={{
              background: '#ff4d4f', color: '#fff', padding: '0 6px',
              borderRadius: 3, fontSize: 10, fontWeight: 'bold', lineHeight: '18px',
              animation: 'blink 1s infinite',
            }}>● REC</span>
          )}
          <Button type="text" size="small" icon={<MinusOutlined />} onClick={() => setMinimized(!minimized)}
            style={{ color: btnColor, width: 24, height: 24, padding: 0 }} />
          <Button type="text" size="small" icon={<CloseOutlined />} onClick={onClose}
            style={{ color: btnColor, width: 24, height: 24, padding: 0 }} />
        </div>

        <div style={{ padding: 6, display: minimized ? 'none' : undefined }}>
          {/* Preview (backend MJPEG polling) */}
          <div style={{ position: 'relative', marginBottom: 6 }}>
            <img
              src={previewUrl}
              alt="webcam"
              style={{ width: '100%', borderRadius: 4, background: '#000', display: 'block' }}
              onError={(e) => { (e.currentTarget as HTMLImageElement).style.visibility = 'hidden'; }}
              onLoad={(e) => { (e.currentTarget as HTMLImageElement).style.visibility = 'visible'; }}
            />
            {webcamRecording && (
              <span style={{
                position: 'absolute', top: 6, right: 6,
                background: 'rgba(255,0,0,0.85)', color: '#fff',
                padding: '1px 6px', borderRadius: 4, fontSize: 10, fontWeight: 'bold',
                animation: 'blink 1s infinite',
              }}>● REC</span>
            )}
            {/* 타임스탬프는 백엔드(영상 프레임)에서 그려져 들어오므로 별도 오버레이 안 함 */}
          </div>

          {/* Controls */}
          <div style={{ display: 'flex', gap: 3, alignItems: 'center', marginBottom: 5 }}>
            <Select
              size="small"
              value={webcamIndex}
              onChange={handleWebcamChange}
              style={{ flex: 1 }}
              placeholder={t('webcam.select')}
              getPopupContainer={getContainer}
              options={(webcamDevices as WebcamDeviceLike[]).map((d, i) => ({
                value: i,
                label: d.label || t('webcam.camera', { index: String(i) }),
              }))}
            />
            {/* 음성 녹화 opt-in 토글 — 켜져 있을 때만 녹화 mp4에 마이크 오디오 포함 */}
            <Tooltip title={audioEnabled ? t('webcam.audioOn') : t('webcam.audioOff')}>
              <Button
                size="small"
                type={audioEnabled ? 'primary' : 'default'}
                icon={audioEnabled ? <AudioOutlined /> : <AudioMutedOutlined />}
                onClick={() => setAudioEnabled(!audioEnabled)}
                disabled={webcamRecording}
              />
            </Tooltip>
            {!webcamRecording ? (
              <Button size="small" type="primary" danger icon={<PlayCircleOutlined />} onClick={startWebcamRecording}>
                {t('webcam.record')}
              </Button>
            ) : (
              <Button size="small" danger icon={<PauseOutlined />} onClick={stopWebcamRecording}
                style={{ animation: 'blink 1s infinite' }}>
                {t('webcam.recordStop')}
              </Button>
            )}
            <Button
              size="small"
              icon={<SettingOutlined />}
              onClick={() => { loadWebcamCapabilities(); loadAudioDevices(); setWebcamSettingsOpen((v: boolean) => !v); }}
              type={webcamSettingsOpen ? 'primary' : 'default'}
            />
          </div>

          {/* Settings */}
          {webcamSettingsOpen && (
            <div style={{ padding: '6px 8px', background: settingsBg, borderRadius: 6 }}>
              {webcamResolutions.length > 0 && (
                <div style={{ marginBottom: 5 }}>
                  <div style={{ fontSize: 10, marginBottom: 2, color: subColor }}>{t('webcam.resolutionSelect')}</div>
                  <Select
                    size="small"
                    value={webcamResolution || undefined}
                    onChange={handleWebcamResolutionChange}
                    style={{ width: '100%' }}
                    placeholder={t('webcam.resolutionSelect')}
                    getPopupContainer={getContainer}
                    options={(webcamResolutions as string[]).map((r: string) => {
                      const [w, h] = r.split('x');
                      return { value: r, label: RESOLUTION_LABELS[r] ? `${RESOLUTION_LABELS[r]} (${w}×${h})` : `${w}×${h}` };
                    })}
                  />
                </div>
              )}
              {/* 타임스탬프 설정 */}
              <div style={{ marginBottom: 5 }}>
                <div style={{ fontSize: 10, marginBottom: 2, color: subColor }}>{t('webcam.timestampPosition')}</div>
                <div style={{ display: 'flex', gap: 3 }}>
                  <Select
                    size="small"
                    value={timestampPosition}
                    onChange={setTimestampPosition}
                    style={{ flex: 1 }}
                    getPopupContainer={getContainer}
                    options={[
                      { value: 'top-left', label: '↖ Top Left' },
                      { value: 'top-right', label: '↗ Top Right' },
                      { value: 'bottom-left', label: '↙ Bottom Left' },
                      { value: 'bottom-right', label: '↘ Bottom Right' },
                      { value: 'off', label: t('webcam.timestampOff') },
                    ]}
                  />
                  <input
                    type="color"
                    value={timestampColor}
                    onChange={e => setTimestampColor(e.target.value)}
                    title={t('webcam.timestampColor')}
                    style={{ width: 28, height: 24, padding: 0, border: `1px solid ${border}`, borderRadius: 4, cursor: 'pointer' }}
                  />
                  <Select
                    size="small"
                    value={timestampFontSize || 24}
                    onChange={setTimestampFontSize}
                    style={{ width: 70 }}
                    getPopupContainer={getContainer}
                    options={[
                      { value: 20, label: '20px' },
                      { value: 22, label: '22px' },
                      { value: 24, label: '24px' },
                      { value: 26, label: '26px' },
                    ]}
                  />
                </div>
              </div>
              {/* 마이크(음성) 녹음 설정 — 녹화 mp4에 오디오 트랙 포함 여부 */}
              <div style={{ marginBottom: 5 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 10, marginBottom: 2 }}>
                  <span style={{ color: subColor }}>
                    <AudioOutlined style={{ marginRight: 3 }} />
                    {t('webcam.audioRecord')}
                  </span>
                  <Switch
                    size="small"
                    checked={audioEnabled}
                    onChange={(v: boolean) => setAudioEnabled(v)}
                  />
                </div>
                {audioEnabled && (
                  (audioDevices as { name: string; alt: string }[]).length > 0 ? (
                    <Select
                      size="small"
                      value={audioDevice || ''}
                      onChange={setAudioDevice}
                      style={{ width: '100%' }}
                      getPopupContainer={getContainer}
                      options={[
                        { value: '', label: t('webcam.audioAuto') },
                        ...(audioDevices as { name: string; alt: string }[]).map(d => ({
                          value: d.alt || d.name,
                          label: d.name,
                        })),
                      ]}
                    />
                  ) : (
                    <div style={{ color: subColor, fontSize: 10 }}>{t('webcam.audioNoDevices')}</div>
                  )
                )}
              </div>
              {/* 노출 설정 — 자동/수동 토글 + 슬라이더 */}
              {webcamCapabilities.exposure ? (
                <div style={{ marginBottom: 3 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 10, marginBottom: 2 }}>
                    <span style={{ color: labelColor }}>{t('webcam.exposure')}</span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                      <Button size="small"
                        type={exposureAuto ? 'primary' : 'default'}
                        onClick={() => setExposureAutoMode(!exposureAuto)}
                        style={{ height: 18, padding: '0 6px', fontSize: 9 }}>
                        {exposureAuto ? t('webcam.exposureAuto') : t('webcam.exposureManual')}
                      </Button>
                      <span style={{ color: subColor, minWidth: 30, textAlign: 'right' }}>
                        {webcamSettings.exposure ?? '-'}
                      </span>
                    </div>
                  </div>
                  <Slider
                    min={webcamCapabilities.exposure.min}
                    max={webcamCapabilities.exposure.max}
                    step={webcamCapabilities.exposure.step || 1}
                    value={webcamSettings.exposure ?? webcamCapabilities.exposure.min}
                    disabled={exposureAuto}
                    onChange={(v: number) => applyWebcamSetting('exposure', v)}
                    style={{ margin: '0 0 2px 0' }}
                  />
                </div>
              ) : webcamResolutions.length === 0 ? (
                <div style={{ color: subColor, fontSize: 10, textAlign: 'center', padding: 3 }}>{t('webcam.noSettings')}</div>
              ) : null}
              {/* 합성녹화 진입 버튼 */}
              {onOpenCompositor && (
                <div style={{ marginTop: 6, paddingTop: 6, borderTop: `1px solid ${border}` }}>
                  <Button
                    size="small"
                    icon={<AppstoreOutlined />}
                    onClick={onOpenCompositor}
                    style={{ width: '100%' }}
                  >
                    {t('compositor.button')}
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <style>{`@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }`}</style>
    </ConfigProvider>
  );
}
