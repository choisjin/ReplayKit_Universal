import { createContext, useContext, useState, useEffect, ReactNode, useCallback } from 'react';
import axios from 'axios';

const api = axios.create({ baseURL: '/api' });

export type Language = 'ko' | 'en';

export interface AppSettings {
  theme: 'light' | 'dark';
  webcam_save_dir: string;
  excel_export_dir: string;
  scenario_export_dir: string;
  language: Language;
  monitor_server_url: string;
  admin_server_url: string;
  default_wait_ms: number;
  threshold_full: number;
  threshold_single_crop: number;
  threshold_full_exclude: number;
  threshold_multi_crop: number;
  threshold_match_crop: number;
  backup_enabled: boolean;
  backup_interval_minutes: number;
  backup_dir: string;
  backup_keep: number;
}

interface SettingsContextType {
  settings: AppSettings;
  loading: boolean;
  fetchSettings: () => Promise<void>;
  updateSettings: (partial: Partial<AppSettings>) => Promise<void>;
  uploadWebcamRecording: (blob: Blob, filename: string) => Promise<string>;
  saveExcelToDir: (resultFilename: string) => Promise<string>;
  saveExportZipToDir: (scenarios: string[], groups: string[], includeAll: boolean) => Promise<string>;
  browseFolder: (initialDir?: string) => Promise<string>;
}

const DEFAULT_SETTINGS: AppSettings = {
  theme: 'light',
  webcam_save_dir: '',
  excel_export_dir: '',
  scenario_export_dir: '',
  language: 'ko',
  monitor_server_url: 'http://10.176.144.50:9000',
  admin_server_url: '',
  default_wait_ms: 3000,
  threshold_full: 0.95,
  threshold_single_crop: 0.90,
  threshold_full_exclude: 0.93,
  threshold_multi_crop: 0.85,
  threshold_match_crop: 0.85,
  backup_enabled: true,
  backup_interval_minutes: 1440,
  backup_dir: '',
  backup_keep: 10,
};

const SettingsContext = createContext<SettingsContextType | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS);
  const [loading, setLoading] = useState(true);

  const fetchSettings = useCallback(async () => {
    try {
      const res = await api.get('/settings');
      setSettings({ ...DEFAULT_SETTINGS, ...res.data });
    } catch { /* ignore */ }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchSettings();
  }, [fetchSettings]);

  const updateSettings = useCallback(async (partial: Partial<AppSettings>) => {
    const res = await api.post('/settings', partial);
    setSettings({ ...DEFAULT_SETTINGS, ...res.data });
  }, []);

  const uploadWebcamRecording = useCallback(async (blob: Blob, filename: string): Promise<string> => {
    const form = new FormData();
    form.append('file', blob, filename);
    form.append('filename', filename);
    const res = await api.post('/settings/upload-webcam', form);
    return res.data.path;
  }, []);

  const saveExcelToDir = useCallback(async (resultFilename: string): Promise<string> => {
    const res = await api.post('/settings/save-excel', { result_filename: resultFilename });
    return res.data.path;
  }, []);

  const saveExportZipToDir = useCallback(async (scenarios: string[], groups: string[], includeAll: boolean): Promise<string> => {
    const res = await api.post('/settings/save-export-zip', { scenarios, groups, include_all: includeAll });
    return res.data.path;
  }, []);

  const browseFolder = useCallback(async (initialDir?: string): Promise<string> => {
    const res = await api.post('/settings/browse-folder', { initial_dir: initialDir || '' });
    return res.data.path || '';
  }, []);

  return (
    <SettingsContext.Provider value={{ settings, loading, fetchSettings, updateSettings, uploadWebcamRecording, saveExcelToDir, saveExportZipToDir, browseFolder }}>
      {children}
    </SettingsContext.Provider>
  );
}

export function useSettings() {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error('useSettings must be used within SettingsProvider');
  return ctx;
}
