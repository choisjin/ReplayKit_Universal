import { useEffect, useRef, useState } from 'react';
import { Button, Card, Col, Empty, Input, message, Progress, Row, Statistic, Table, Tag, Typography } from 'antd';
import { AppstoreOutlined, BarChartOutlined, ReloadOutlined, WarningOutlined } from '@ant-design/icons';
import { scenarioApi } from '../services/api';

// 내장 스텝 타입 → 한국어 표시 라벨 (backend StepType enum 과 대응)
const STEP_TYPE_LABELS: Record<string, string> = {
  tap: '탭', long_press: '롱프레스', swipe: '스와이프', input_text: '텍스트 입력',
  key_event: '키 이벤트', wait: '대기', adb_command: 'ADB 명령', serial_command: '시리얼 명령',
  module_command: '모듈 명령', hkmc_touch: 'HKMC 터치', hkmc_swipe: 'HKMC 스와이프',
  hkmc_key: 'HKMC 키', hkmc_long_press: 'HKMC 롱프레스', hkmc_multi_touch: 'HKMC 멀티터치',
  icas_touch: 'ICAS 터치', icas_swipe: 'ICAS 스와이프', icas_key: 'ICAS 키', icas_long_press: 'ICAS 롱프레스',
  connectwide_key: 'Connect Wide 키', multi_touch: '멀티터치', repeat_tap: '반복 탭',
  all_random: '랜덤 스트레스', image_tap: '이미지 탭',
  win_tap: 'Win 탭', win_double_click: 'Win 더블클릭', win_click_sequence: 'Win 클릭 시퀀스',
  win_long_press: 'Win 롱프레스', win_swipe: 'Win 스와이프', win_input_text: 'Win 텍스트입력',
  win_key: 'Win 키', win_key_combo: 'Win 키조합',
};

interface FuncStat {
  function: string;
  count: number;
  scenario_count: number;
  scenarios: string[];
}
interface ModuleStat {
  module: string;
  count: number;
  scenario_count: number;
  function_count: number;
  functions: FuncStat[];
}
interface StepTypeStat {
  type: string;
  count: number;
  scenario_count: number;
  scenarios: string[];
}
interface UnusedFunc {
  module: string;
  function: string;
  description: string;
}
interface UsageStats {
  generated_at: string;
  scenario_count: number;
  total_steps: number;
  step_types: StepTypeStat[];
  modules: ModuleStat[];
  unused_functions: UnusedFunc[];
  available_module_count: number;
  available_function_count: number;
  used_module_count: number;
  used_function_count: number;
}

/**
 * 시나리오 사용 통계 페이지 (관제 페이지 1단계).
 *
 * 이 PC 에 저장된 모든 시나리오(backend/scenarios/*.json)를 순회해:
 *   - 내장 스텝 타입(tap/swipe/module_command 등) 사용 빈도
 *   - 모듈 명령의 모듈·함수별 사용 빈도 (몇 개 시나리오에서 쓰였는지)
 *   - 가용 모듈 카탈로그와 교차대조한 '한 번도 안 쓰인 함수' 목록
 * 을 보여준다. 잘 안 쓰이는 기능의 개선/삭제 근거 자료로 사용.
 *
 * 접근 경로: URL hash `#stats` (메뉴 비노출, #admin/#test 와 동일 방식). 로그인 없음.
 */
export default function StatsPage() {
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [unusedFilter, setUnusedFilter] = useState('');
  const loadedRef = useRef(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await scenarioApi.usageStats();
      setStats(res.data);
      loadedRef.current = true;
    } catch (e: any) {
      message.error('통계 로드 실패: ' + (e.response?.data?.detail || e.message));
    } finally {
      setLoading(false);
    }
  };

  // #stats 로 진입(활성)했을 때 처음 한 번 로드 — 앱 부팅 시 백그라운드 낭비 호출 방지.
  useEffect(() => {
    const isActive = () => window.location.hash === '#stats';
    const maybeLoad = () => { if (isActive() && !loadedRef.current) load(); };
    maybeLoad();
    const onHash = () => maybeLoad();
    const onTab = () => maybeLoad();
    window.addEventListener('hashchange', onHash);
    window.addEventListener('tab-change', onTab);
    return () => {
      window.removeEventListener('hashchange', onHash);
      window.removeEventListener('tab-change', onTab);
    };
  }, []);

  const maxStepCount = stats?.step_types[0]?.count || 1;
  const maxModuleCount = stats?.modules[0]?.count || 1;

  // 미사용 함수 — 모듈 필터 적용
  const filteredUnused = (stats?.unused_functions || []).filter(u =>
    !unusedFilter ||
    u.module.toLowerCase().includes(unusedFilter.toLowerCase()) ||
    u.function.toLowerCase().includes(unusedFilter.toLowerCase())
  );
  // 미사용 함수 모듈별 개수 (요약 태그)
  const unusedByModule: Record<string, number> = {};
  (stats?.unused_functions || []).forEach(u => { unusedByModule[u.module] = (unusedByModule[u.module] || 0) + 1; });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
      <Card
        size="small"
        title={<span><BarChartOutlined /> <Typography.Text strong>시나리오 사용 통계</Typography.Text> <Tag color="purple">Stats</Tag></span>}
        extra={<Button icon={<ReloadOutlined />} onClick={load} loading={loading}>새로고침</Button>}
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          이 PC 에 저장된 모든 시나리오에서 쓰인 스텝 타입·모듈·함수 사용량을 집계합니다.
          가용 함수 카탈로그와 대조해 <b>한 번도 안 쓰인 함수</b>를 하단에 표시하며, 개선/삭제 판단의 근거로 활용하세요.
          접근 경로: URL hash <code>#stats</code>.
          {stats?.generated_at && <span> · 집계 시각: {new Date(stats.generated_at).toLocaleString()}</span>}
        </Typography.Paragraph>
      </Card>

      {/* 요약 통계 */}
      <Row gutter={[10, 10]}>
        <Col xs={12} sm={8} md={4}><Card size="small"><Statistic title="시나리오 수" value={stats?.scenario_count ?? 0} /></Card></Col>
        <Col xs={12} sm={8} md={4}><Card size="small"><Statistic title="총 스텝" value={stats?.total_steps ?? 0} /></Card></Col>
        <Col xs={12} sm={8} md={4}>
          <Card size="small"><Statistic title="사용 모듈" value={stats?.used_module_count ?? 0}
            suffix={<span style={{ fontSize: 12, color: '#888' }}>/ {stats?.available_module_count ?? 0}</span>} /></Card>
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Card size="small"><Statistic title="사용 함수" value={stats?.used_function_count ?? 0}
            suffix={<span style={{ fontSize: 12, color: '#888' }}>/ {stats?.available_function_count ?? 0}</span>} /></Card>
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Card size="small"><Statistic title="미사용 함수" valueStyle={{ color: (stats?.unused_functions.length || 0) > 0 ? '#fa8c16' : undefined }}
            value={stats?.unused_functions.length ?? 0} prefix={<WarningOutlined />} /></Card>
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Card size="small"><Statistic title="스텝 타입 종류" value={stats?.step_types.length ?? 0} /></Card>
        </Col>
      </Row>

      {/* 스텝 타입 사용량 */}
      <Card size="small" title={<span><AppstoreOutlined /> 스텝 타입 사용량</span>}>
        {!stats || stats.step_types.length === 0 ? (
          <Empty description={loading ? '로딩 중...' : '데이터 없음'} />
        ) : (
          <Table
            size="small"
            rowKey="type"
            pagination={false}
            dataSource={stats.step_types}
            columns={[
              {
                title: '스텝 타입', dataIndex: 'type', key: 'type',
                render: (t: string) => (
                  <span>
                    <Typography.Text strong style={{ fontSize: 12 }}>{STEP_TYPE_LABELS[t] || t}</Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 10, marginLeft: 6, fontFamily: 'monospace' }}>{t}</Typography.Text>
                  </span>
                ),
                sorter: (a: StepTypeStat, b: StepTypeStat) => (STEP_TYPE_LABELS[a.type] || a.type).localeCompare(STEP_TYPE_LABELS[b.type] || b.type),
              },
              {
                title: '사용 횟수', dataIndex: 'count', key: 'count', width: 260,
                defaultSortOrder: 'descend',
                sorter: (a: StepTypeStat, b: StepTypeStat) => a.count - b.count,
                render: (c: number) => (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ flex: 1 }}><Progress percent={Math.round((c / maxStepCount) * 100)} showInfo={false} size="small" /></div>
                    <Typography.Text strong style={{ minWidth: 40, textAlign: 'right' }}>{c}</Typography.Text>
                  </div>
                ),
              },
              {
                title: '시나리오 수', dataIndex: 'scenario_count', key: 'scenario_count', width: 110,
                sorter: (a: StepTypeStat, b: StepTypeStat) => a.scenario_count - b.scenario_count,
                render: (n: number) => <Tag>{n}</Tag>,
              },
            ]}
          />
        )}
      </Card>

      {/* 모듈 · 함수 사용량 */}
      <Card size="small" title={<span><AppstoreOutlined /> 모듈 · 함수 사용량 (module_command)</span>}
        extra={<Typography.Text type="secondary" style={{ fontSize: 11 }}>행을 펼치면 함수별 상세</Typography.Text>}>
        {!stats || stats.modules.length === 0 ? (
          <Empty description={loading ? '로딩 중...' : '모듈 명령을 쓴 시나리오 없음'} />
        ) : (
          <Table
            size="small"
            rowKey="module"
            pagination={false}
            dataSource={stats.modules}
            expandable={{
              expandedRowRender: (m: ModuleStat) => (
                <Table
                  size="small"
                  rowKey="function"
                  pagination={false}
                  dataSource={m.functions}
                  columns={[
                    { title: '함수', dataIndex: 'function', key: 'function',
                      render: (f: string) => <Typography.Text style={{ fontFamily: 'monospace', fontSize: 12 }}>{f}</Typography.Text> },
                    { title: '사용 횟수', dataIndex: 'count', key: 'count', width: 120,
                      render: (c: number) => <Typography.Text strong>{c}</Typography.Text> },
                    { title: '시나리오 수', dataIndex: 'scenario_count', key: 'sc', width: 110,
                      render: (n: number) => <Tag>{n}</Tag> },
                    { title: '사용된 시나리오', dataIndex: 'scenarios', key: 'scenarios',
                      render: (arr: string[]) => (
                        <span style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                          {arr.map(s => <Tag key={s} style={{ fontSize: 10, margin: 0 }}>{s}</Tag>)}
                        </span>
                      ) },
                  ]}
                />
              ),
            }}
            columns={[
              { title: '모듈', dataIndex: 'module', key: 'module',
                render: (m: string) => <Tag color="blue" style={{ fontFamily: 'monospace' }}>{m}</Tag>,
                sorter: (a: ModuleStat, b: ModuleStat) => a.module.localeCompare(b.module) },
              { title: '사용 횟수', dataIndex: 'count', key: 'count', width: 260,
                defaultSortOrder: 'descend',
                sorter: (a: ModuleStat, b: ModuleStat) => a.count - b.count,
                render: (c: number) => (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ flex: 1 }}><Progress percent={Math.round((c / maxModuleCount) * 100)} showInfo={false} size="small" strokeColor="#1677ff" /></div>
                    <Typography.Text strong style={{ minWidth: 40, textAlign: 'right' }}>{c}</Typography.Text>
                  </div>
                ) },
              { title: '함수 종류', dataIndex: 'function_count', key: 'fc', width: 100,
                sorter: (a: ModuleStat, b: ModuleStat) => a.function_count - b.function_count,
                render: (n: number) => <Tag>{n}</Tag> },
              { title: '시나리오 수', dataIndex: 'scenario_count', key: 'sc', width: 110,
                sorter: (a: ModuleStat, b: ModuleStat) => a.scenario_count - b.scenario_count,
                render: (n: number) => <Tag>{n}</Tag> },
            ]}
          />
        )}
      </Card>

      {/* 미사용 함수 */}
      <Card size="small"
        title={<span><WarningOutlined style={{ color: '#fa8c16' }} /> 미사용 함수 (개선/삭제 후보)</span>}
        extra={
          <Input.Search
            allowClear
            placeholder="모듈/함수 검색"
            size="small"
            style={{ width: 200 }}
            value={unusedFilter}
            onChange={(e) => setUnusedFilter(e.target.value)}
          />
        }>
        <Typography.Paragraph type="secondary" style={{ fontSize: 11, marginBottom: 8 }}>
          가용 모듈 카탈로그의 함수 중 <b>이 PC 의 어떤 시나리오에서도 module_command 로 호출되지 않은</b> 함수입니다.
          (전용 스텝 타입 tap/swipe 등으로 제공되는 동작은 카탈로그에서 이미 제외되어 있습니다.)
        </Typography.Paragraph>
        {Object.keys(unusedByModule).length > 0 && (
          <div style={{ marginBottom: 10, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {Object.entries(unusedByModule).sort((a, b) => b[1] - a[1]).map(([mod, n]) => (
              <Tag key={mod} color="orange" style={{ cursor: 'pointer' }} onClick={() => setUnusedFilter(mod)}>
                {mod}: {n}
              </Tag>
            ))}
          </div>
        )}
        {!stats || stats.unused_functions.length === 0 ? (
          <Empty description={loading ? '로딩 중...' : (stats ? '미사용 함수 없음 — 모든 함수가 최소 1회 사용됨' : '데이터 없음')} />
        ) : (
          <Table
            size="small"
            rowKey={(r: UnusedFunc) => `${r.module}.${r.function}`}
            pagination={{ pageSize: 20, size: 'small', showSizeChanger: true }}
            dataSource={filteredUnused}
            columns={[
              { title: '모듈', dataIndex: 'module', key: 'module', width: 180,
                render: (m: string) => <Tag color="blue" style={{ fontFamily: 'monospace' }}>{m}</Tag>,
                sorter: (a: UnusedFunc, b: UnusedFunc) => a.module.localeCompare(b.module) },
              { title: '함수', dataIndex: 'function', key: 'function', width: 220,
                render: (f: string) => <Typography.Text style={{ fontFamily: 'monospace', fontSize: 12 }}>{f}</Typography.Text>,
                sorter: (a: UnusedFunc, b: UnusedFunc) => a.function.localeCompare(b.function) },
              { title: '설명', dataIndex: 'description', key: 'description',
                render: (d: string) => <Typography.Text type="secondary" style={{ fontSize: 11 }}>{d || '—'}</Typography.Text> },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
