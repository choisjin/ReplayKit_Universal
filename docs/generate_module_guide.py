"""module_guides.json에서 module-guide.html을 자동 생성하는 스크립트.

사용법:
  python docs/generate_module_guide.py          # 한국어 + 영어 동시 생성
  python docs/generate_module_guide.py --lang ko # 한국어만
  python docs/generate_module_guide.py --lang en # 영어만

module_guides.json을 수정한 후 이 스크립트를 실행하면
docs/module-guide.html 및 docs/module-guide-en.html이 자동으로 갱신됩니다.
"""

import json
import sys
from pathlib import Path
from html import escape

REPO_ROOT = Path(__file__).resolve().parent.parent
GUIDES_PATH = REPO_ROOT / "backend" / "app" / "services" / "module_guides.json"
GUIDES_PATH_EN = REPO_ROOT / "backend" / "app" / "services" / "module_guides_en.json"
OUTPUT_PATH = Path(__file__).resolve().parent / "module-guide.html"
OUTPUT_PATH_EN = Path(__file__).resolve().parent / "module-guide-en.html"


def compute_visible_map(module_names: list[str]) -> dict[str, set[str]]:
    """앱이 실제로 사용자에게 노출하는 함수 집합을 모듈별로 계산.

    module_service.get_module_functions()를 단일 소스로 사용한다. 이 함수는
    화이트리스트(per_module_included)·제외 목록(per_module_excluded)·가상 모듈
    정책을 모두 반영하므로, 가이드 HTML이 앱 스텝 드롭다운과 정확히 일치하게 된다.

    반환: {module_name: {노출 함수명, ...}}. 어떤 모듈이 맵에 없으면(=introspection
    실패 또는 import 실패) 그 모듈은 필터 없이 전체 함수를 표시한다(안전 폴백 — 도구가
    플러그인을 못 불러도 가이드가 비어버리지 않도록).
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from backend.app.services.module_service import get_module_functions
    except Exception as e:
        print(f"[warn] module_service import 실패 — 노출 필터 미적용(전체 함수 표시): {e}")
        return {}

    vmap: dict[str, set[str]] = {}
    for name in module_names:
        try:
            exposed = {f["name"] for f in get_module_functions(name)}
        except Exception as e:
            print(f"[warn] {name}: get_module_functions 실패 — 전체 표시 폴백 ({e})")
            continue
        if exposed:
            vmap[name] = exposed
        else:
            # 문서화된 모듈이 0개 노출이면 introspection 실패로 간주 → 폴백(전체 표시)
            print(f"[warn] {name}: 노출 함수 0개(introspection 실패 추정) — 전체 표시 폴백")
    return vmap

# 모듈 카테고리 분류 (id, ko_label, en_label, modules)
CATEGORIES = [
    ("bench", "벤치 장비", "Test Bench", ["IVIQEBenchIOClient", "CCIC_BENCH", "BENCH", "SP25Bench", "SmartBench", "WoohyunBench"]),
    ("can", "CAN 통신", "CAN Communication", ["CAN", "CANoe_RBS", "CANoe_Ctrl", "CANAT", "PCANClient"]),
    ("comm", "통신 (시리얼/SSH/UART/DLT)", "Communication (Serial/SSH/UART/DLT)", ["SerialPlugin", "SerialLogging", "Uart", "Ignition", "SSHManager", "DLTLogging", "DLTViewer"]),
    ("system", "시스템 & 유틸리티", "System & Utilities", ["Android", "CMD", "COMMON_WINDOWS", "TigrisCheck"]),
]

# 언어별 텍스트
I18N = {
    "ko": {
        "html_lang": "ko",
        "title": "ReplayKit 모듈 가이드",
        "sidebar_title": "모듈 가이드",
        "heading": "모듈 가이드",
        "heading_sub": "ReplayKit에서 사용 가능한 모든 모듈의 함수 및 파라미터 가이드",
        "intro": '이 문서는 ReplayKit의 <strong>모듈 명령(module_command)</strong> 스텝에서 사용할 수 있는 모든 모듈과 함수를 설명합니다. '
                 '코딩 지식 없이도 각 함수가 어떤 장비를 제어하고, 어떤 값을 넣어야 하는지 확인할 수 있습니다.',
        "tip_title": "앱 내 가이드",
        "tip_body": "스텝 추가 시 함수를 선택하면 설명이 자동으로 표시됩니다. 이 문서는 전체 목록을 한눈에 보거나 인쇄하여 참고하기 위한 것입니다.",
        "overview": "전체 모듈 목록",
        "th_module": "모듈",
        "th_desc": "설명",
        "th_funcs": "함수 수",
        "th_func": "함수",
        "th_param": "파라미터",
        "no_param": "(없음)",
        "total": "총 <strong>{modules}개</strong> 모듈, <strong>{funcs}개</strong> 함수가 등록되어 있습니다.",
        "theme_title": "테마 전환",
        "cat_label_idx": 1,  # index into CATEGORIES tuple for label
    },
    "en": {
        "html_lang": "en",
        "title": "ReplayKit Module Guide",
        "sidebar_title": "Module Guide",
        "heading": "Module Guide",
        "heading_sub": "Complete function and parameter reference for all ReplayKit modules",
        "intro": 'This document describes all modules and functions available in ReplayKit\'s <strong>module_command</strong> step type. '
                 'You can see what each function controls and what parameters to provide, without any coding knowledge.',
        "tip_title": "In-App Guide",
        "tip_body": "When adding a step, selecting a function will automatically display its description. This document is for browsing the full list or printing as a reference.",
        "overview": "All Modules",
        "th_module": "Module",
        "th_desc": "Description",
        "th_funcs": "Functions",
        "th_func": "Function",
        "th_param": "Parameters",
        "no_param": "(none)",
        "total": "Total <strong>{modules}</strong> modules, <strong>{funcs}</strong> functions registered.",
        "theme_title": "Toggle theme",
        "cat_label_idx": 2,  # index into CATEGORIES tuple for label
    },
}

# 연결 타입 한국어
CONNECT_LABELS = {
    "serial": "시리얼 (RS-232)",
    "socket": "소켓 (TCP/UDP)",
    "can": "CAN 인터페이스",
    "vision_camera": "GigE Vision",
    "none": "연결 불필요",
}


def generate_html(data: dict, lang: str = "ko", visible_map: dict[str, set[str]] | None = None) -> str:
    t = I18N[lang]
    cat_label_idx = t["cat_label_idx"]
    visible_map = visible_map or {}
    modules = {k: v for k, v in data.items() if k != "_meta"}

    def _visible_funcs(mod_name: str, funcs: dict) -> dict:
        """앱에 실제 노출되는 함수만 남긴다. 맵에 없는 모듈은 전체 표시(폴백)."""
        allowed = visible_map.get(mod_name)
        if allowed is None:
            return funcs
        return {fn: info for fn, info in funcs.items() if fn in allowed}

    # 카테고리에 속하지 않은 모듈 수집
    cats = [list(c) for c in CATEGORIES]  # mutable copy
    categorized = set()
    for c in cats:
        categorized.update(c[3])
    uncategorized = [n for n in modules if n not in categorized]
    if uncategorized:
        cats.append(["etc", "기타", "Others", uncategorized])

    # 빈 카테고리(현재 활성 모듈이 하나도 없는) 제거 — 사이드바에 hollow 항목 방지
    cats = [c for c in cats if any(name in modules for name in c[3])]

    # --- TOC 생성 ---
    toc_lines = []
    toc_lines.append(f'<a href="#top" class="toc-h2">{t["heading"]}</a>')
    toc_lines.append(f'<a href="#overview" class="toc-h3">{t["overview"]}</a>')
    for c in cats:
        cat_id, cat_label = c[0], c[cat_label_idx]
        toc_lines.append(f'<a href="#cat-{cat_id}" class="toc-h2">{cat_label}</a>')
        for name in c[3]:
            if name in modules:
                toc_lines.append(f'<a href="#mod-{name}" class="toc-h3">{name}</a>')
    toc_html = "\n      ".join(toc_lines)

    # --- 모듈 섹션 생성 ---
    sections = []

    # 전체 목록 테이블
    overview_rows = []
    total_funcs = 0
    for name, mod in modules.items():
        fc = len(_visible_funcs(name, mod.get("functions", {})))
        total_funcs += fc
        desc = escape(mod.get("_description", "").split(" — ")[-1] if " — " in mod.get("_description", "") else mod.get("_description", ""))
        overview_rows.append(f'<tr><td><a href="#mod-{name}"><strong>{name}</strong></a></td><td>{desc}</td><td style="text-align:center">{fc}</td></tr>')

    overview_table = "\n".join(overview_rows)

    # 카테고리별 모듈 상세
    for c in cats:
        cat_id, cat_label, names = c[0], c[cat_label_idx], c[3]
        cat_sections = []
        for name in names:
            if name not in modules:
                continue
            mod = modules[name]
            desc = escape(mod.get("_description", ""))
            funcs = _visible_funcs(name, mod.get("functions", {}))

            # 함수 테이블
            func_rows = []
            for fname, finfo in funcs.items():
                fdesc = escape(finfo.get("description", ""))
                params = finfo.get("params", {})
                if params:
                    param_parts = []
                    for pname, pdesc in params.items():
                        param_parts.append(f"<code>{pname}</code>: {escape(pdesc)}")
                    params_html = "<br>".join(param_parts)
                else:
                    params_html = f'<span style="color:var(--text-muted)">{t["no_param"]}</span>'

                func_rows.append(f"""<tr>
<td><code>{fname}</code></td>
<td>{fdesc}</td>
<td style="font-size:12px">{params_html}</td>
</tr>""")

            func_table = "\n".join(func_rows)

            cat_sections.append(f"""
<h3 id="mod-{name}">{name}</h3>
<div class="callout callout-info">{desc}</div>
<table>
<thead><tr><th style="width:220px">{t["th_func"]}</th><th>{t["th_desc"]}</th><th style="width:40%">{t["th_param"]}</th></tr></thead>
<tbody>
{func_table}
</tbody>
</table>
""")

        sections.append(f"""
<h2 id="cat-{cat_id}">{cat_label}</h2>
{"".join(cat_sections)}
""")

    content_html = "\n".join(sections)

    return f"""<!DOCTYPE html>
<html lang="{t['html_lang']}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{t['title']}</title>
<style>
  :root {{
    --bg: #ffffff;
    --bg-alt: #f6f8fa;
    --bg-code: #f0f2f5;
    --text: #24292f;
    --text-muted: #57606a;
    --border: #d0d7de;
    --accent: #0969da;
    --accent-light: #ddf4ff;
    --success: #1a7f37;
    --success-bg: #dafbe1;
    --warning: #9a6700;
    --warning-bg: #fff8c5;
    --danger: #cf222e;
    --danger-bg: #ffebe9;
    --shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  [data-theme="dark"] {{
    --bg: #0d1117;
    --bg-alt: #161b22;
    --bg-code: #1c2128;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --border: #30363d;
    --accent: #58a6ff;
    --accent-light: #1a2332;
    --success: #3fb950;
    --success-bg: #0d2818;
    --warning: #d29922;
    --warning-bg: #2a1f00;
    --danger: #f85149;
    --danger-bg: #3d1014;
    --shadow: 0 1px 3px rgba(0,0,0,0.3);
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, 'Noto Sans KR', sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.7; font-size: 15px;
  }}
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{
    position: fixed; top: 0; left: 0; width: 280px; height: 100vh;
    overflow-y: auto; background: var(--bg-alt); border-right: 1px solid var(--border);
    padding: 24px 16px; z-index: 100; transition: transform 0.3s;
  }}
  .sidebar-logo {{ font-size: 20px; font-weight: 700; color: var(--accent); margin-bottom: 4px; }}
  .sidebar-version {{ font-size: 12px; color: var(--text-muted); margin-bottom: 20px; }}
  .sidebar nav a {{
    display: block; padding: 5px 10px; color: var(--text-muted); text-decoration: none;
    font-size: 13.5px; border-radius: 6px; transition: background 0.15s, color 0.15s;
  }}
  .sidebar nav a:hover, .sidebar nav a.active {{ background: var(--accent-light); color: var(--accent); }}
  .sidebar nav .toc-h2 {{ font-weight: 600; margin-top: 12px; }}
  .sidebar nav .toc-h3 {{ padding-left: 22px; font-size: 12.5px; }}
  .main {{ margin-left: 280px; max-width: 1600px; padding: 40px 48px 80px; flex: 1; }}
  .theme-toggle {{
    position: fixed; top: 16px; right: 20px; background: var(--bg-alt);
    border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px;
    cursor: pointer; font-size: 18px; z-index: 200; transition: background 0.2s;
  }}
  .theme-toggle:hover {{ background: var(--accent-light); }}
  .hamburger {{
    display: none; position: fixed; top: 14px; left: 14px; background: var(--accent);
    color: #fff; border: none; border-radius: 8px; width: 40px; height: 40px;
    font-size: 22px; cursor: pointer; z-index: 300;
  }}
  .overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 90; }}
  h1 {{ font-size: 32px; font-weight: 800; margin-bottom: 8px; letter-spacing: -0.5px; }}
  h1 small {{ font-size: 14px; font-weight: 400; color: var(--text-muted); display: block; margin-top: 4px; }}
  h2 {{
    font-size: 24px; font-weight: 700; margin-top: 56px; margin-bottom: 16px;
    padding-bottom: 8px; border-bottom: 2px solid var(--border); scroll-margin-top: 24px;
  }}
  h3 {{
    font-size: 18px; font-weight: 600; margin-top: 36px; margin-bottom: 10px; scroll-margin-top: 24px;
  }}
  h4 {{ font-size: 15px; font-weight: 600; margin-top: 20px; margin-bottom: 8px; }}
  p {{ margin-bottom: 12px; }}
  ul, ol {{ margin: 8px 0 16px 24px; }}
  li {{ margin-bottom: 4px; }}
  .callout {{
    padding: 14px 18px; border-radius: 8px; margin: 16px 0; font-size: 14px; border-left: 4px solid;
  }}
  .callout-info {{ background: var(--accent-light); border-color: var(--accent); }}
  .callout-success {{ background: var(--success-bg); border-color: var(--success); }}
  .callout-warning {{ background: var(--warning-bg); border-color: var(--warning); }}
  code {{
    background: var(--bg-code); padding: 2px 6px; border-radius: 4px;
    font-size: 13px; font-family: 'Cascadia Code', 'Consolas', monospace;
  }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0 20px; font-size: 14px; }}
  th, td {{ padding: 10px 14px; text-align: left; border: 1px solid var(--border); }}
  th {{ background: var(--bg-alt); font-weight: 600; white-space: nowrap; }}
  td {{ vertical-align: top; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600;
  }}
  .badge-blue {{ background: var(--accent-light); color: var(--accent); }}
  .badge-green {{ background: var(--success-bg); color: var(--success); }}

  @media (max-width: 900px) {{
    .sidebar {{ transform: translateX(-100%); }}
    .sidebar.open {{ transform: translateX(0); }}
    .main {{ margin-left: 0; padding: 24px 20px 60px; }}
    .hamburger {{ display: block; }}
    .overlay.show {{ display: block; }}
  }}
  @media print {{
    .sidebar, .hamburger, .theme-toggle, .overlay {{ display: none !important; }}
    .main {{ margin-left: 0; max-width: 100%; padding: 20px; }}
    h2 {{ break-before: page; }}
  }}
</style>
</head>
<body>

<button class="theme-toggle" onclick="toggleTheme()" title="{t['theme_title']}">🌓</button>
<button class="hamburger" onclick="toggleSidebar()">☰</button>
<div class="overlay" onclick="toggleSidebar()"></div>

<div class="layout">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-logo">🔌 ReplayKit</div>
    <div class="sidebar-version">{t['sidebar_title']}</div>
    <nav id="toc">
      {toc_html}
    </nav>
  </aside>

  <div class="main">
    <h1 id="top">{t['heading']}<small>{t['heading_sub']}</small></h1>

    <p>{t['intro']}</p>

    <div class="callout callout-success">
      <strong>{t['tip_title']}</strong>
      {t['tip_body']}
    </div>

    <h2 id="overview">{t['overview']}</h2>
    <p>{t['total'].format(modules=len(modules), funcs=total_funcs)}</p>
    <table>
      <thead><tr><th>{t['th_module']}</th><th>{t['th_desc']}</th><th>{t['th_funcs']}</th></tr></thead>
      <tbody>
        {overview_table}
      </tbody>
    </table>

    {content_html}

  </div>
</div>

<script>
function toggleTheme() {{
  const html = document.documentElement;
  const current = html.getAttribute('data-theme');
  html.setAttribute('data-theme', current === 'dark' ? '' : 'dark');
  localStorage.setItem('theme', html.getAttribute('data-theme'));
}}
(function() {{
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
}})();

function toggleSidebar() {{
  document.getElementById('sidebar').classList.toggle('open');
  document.querySelector('.overlay').classList.toggle('show');
}}

document.querySelectorAll('.sidebar nav a').forEach(a => {{
  a.addEventListener('click', () => {{
    if (window.innerWidth <= 900) toggleSidebar();
  }});
}});

const observer = new IntersectionObserver(entries => {{
  entries.forEach(entry => {{
    if (entry.isIntersecting) {{
      document.querySelectorAll('.sidebar nav a').forEach(a => a.classList.remove('active'));
      const active = document.querySelector(`.sidebar nav a[href="#${{entry.target.id}}"]`);
      if (active) active.classList.add('active');
    }}
  }});
}}, {{ rootMargin: '-20% 0px -70% 0px' }});

document.querySelectorAll('h1[id], h2[id], h3[id]').forEach(el => observer.observe(el));
</script>

</body>
</html>"""


def main():
    lang_arg = sys.argv[1] if len(sys.argv) > 1 else None
    # --lang ko / --lang en / 인자 없으면 둘 다
    target_lang = None
    if lang_arg == "--lang" and len(sys.argv) > 2:
        target_lang = sys.argv[2]
    elif lang_arg in ("ko", "en"):
        target_lang = lang_arg

    with open(GUIDES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 영어용 데이터 로드
    data_en = data
    if GUIDES_PATH_EN.exists():
        with open(GUIDES_PATH_EN, "r", encoding="utf-8") as f:
            data_en = json.load(f)

    langs_to_gen = [target_lang] if target_lang else ["ko", "en"]
    output_map = {"ko": OUTPUT_PATH, "en": OUTPUT_PATH_EN}
    data_map = {"ko": data, "en": data_en}

    # 앱 실제 노출 함수 집합(화이트리스트/제외 정책 반영) — 가이드를 이 집합으로 필터.
    module_names = [k for k in data if k != "_meta"]
    visible_map = compute_visible_map(module_names)

    for lang in langs_to_gen:
        html = generate_html(data_map[lang], lang=lang, visible_map=visible_map)
        out = output_map[lang]
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Generated ({lang}): {out}")

    modules = {k: v for k, v in data.items() if k != "_meta"}

    def _visible_count(name: str, funcs: dict) -> int:
        allowed = visible_map.get(name)
        return len(funcs) if allowed is None else sum(1 for fn in funcs if fn in allowed)

    total = sum(_visible_count(n, v.get("functions", {})) for n, v in modules.items())
    print(f"  {len(modules)} modules, {total} functions (노출 기준)")


if __name__ == "__main__":
    main()
