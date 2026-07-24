"""모듈 함수 파라미터 DB (CSV) API.

인자를 입력하는 모든 모듈 함수에 대해, 자주 쓰는 인자 조합을 CSV 파일로
저장/공유하는 기능. 파일은 backend/param_db/<module>/<function>.csv 에 저장되며
서버에 두므로 같은 서버를 쓰는 사용자끼리 자동 공유된다.

CSV 포맷 (utf-8-sig, Excel 호환):
    Sheet,Description,<param1>,<param2>,...
- Sheet: 카테고리(시트) 이름. UI 모달에서 탭으로 구분된다. 비우면 기본 시트.
  (CSV 에는 엑셀식 시트 개념이 없으므로 열 값으로 그룹핑한다)
- Description: 항목 설명 — DB 모달 목록의 기준 열(맨 왼쪽).
- 나머지 열: 함수 인자명 그대로. 값은 전부 문자열(프론트 args 와 동일).

가져오기(import)는 업로드 CSV 로 파일 전체 교체, 내보내기(export)는 사본 다운로드.
"""
import asyncio
import csv
import io
import logging
import re
import threading
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from ..services import module_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paramdb", tags=["paramdb"])

# backend/param_db/<module>/<function>.csv
_PARAM_DB_DIR = Path(__file__).resolve().parent.parent.parent / "param_db"

# 파일 쓰기 직렬화 (여러 사용자가 동시에 행 추가/삭제해도 유실 방지)
_write_lock = threading.Lock()

# 예약 열 이름 (인자 열이 아님)
_SHEET_COL = "Sheet"
_DESC_COL = "Description"

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _safe_path(module: str, function: str) -> Path:
    """경로 조작(traversal) 차단 후 CSV 파일 경로 반환."""
    if not _NAME_RE.match(module) or not _NAME_RE.match(function):
        raise HTTPException(status_code=400, detail="Invalid module/function name")
    return _PARAM_DB_DIR / module / f"{function}.csv"


def _function_param_names(module: str, function: str) -> list[str]:
    """모듈 가이드/introspection 기반 함수 인자명 목록. 함수를 못 찾으면 []."""
    try:
        for fn in module_service.get_module_functions(module):
            if fn.get("name") == function:
                return [p["name"] for p in fn.get("params", [])]
    except Exception as e:
        logger.warning("paramdb: get_module_functions(%s) failed: %s", module, e)
    return []


def _decode_csv_bytes(data: bytes) -> str:
    """utf-8(-sig) 우선, 한국어 Excel 저장본(cp949) 폴백."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # 마지막 수단: 손실 감수 디코드
    return data.decode("utf-8", errors="replace")


def _parse_csv_text(text: str) -> tuple[list[str], list[dict]]:
    """CSV 텍스트 → (인자 헤더 목록, 행 목록).

    행: {"sheet": str, "description": str, "args": {인자명: 값}}
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        return [], []
    header = [h.strip() for h in rows[0]]
    if _DESC_COL not in header:
        raise HTTPException(
            status_code=400,
            detail=f"CSV header must contain a '{_DESC_COL}' column",
        )
    arg_cols = [h for h in header if h not in (_SHEET_COL, _DESC_COL)]
    out: list[dict] = []
    for r in rows[1:]:
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        out.append({
            "sheet": (rec.get(_SHEET_COL) or "").strip(),
            "description": rec.get(_DESC_COL, ""),
            "args": {c: rec.get(c, "") for c in arg_cols},
        })
    return arg_cols, out


def _read_db(path: Path) -> tuple[list[str], list[dict]]:
    """저장된 CSV 읽기. 파일이 없으면 ([], [])."""
    if not path.is_file():
        return [], []
    return _parse_csv_text(_decode_csv_bytes(path.read_bytes()))


def _write_db(path: Path, arg_cols: list[str], entries: list[dict]) -> None:
    """CSV 저장 (utf-8-sig — Excel 더블클릭 호환)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([_SHEET_COL, _DESC_COL, *arg_cols])
    for e in entries:
        args = e.get("args", {})
        w.writerow([e.get("sheet", ""), e.get("description", ""),
                    *[str(args.get(c, "")) for c in arg_cols]])
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


def _group_sheets(entries: list[dict]) -> list[dict]:
    """행 목록 → 시트별 그룹 (등장 순서 유지). 빈 시트명은 'Default'."""
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for i, e in enumerate(entries):
        name = e["sheet"] or "Default"
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append({
            "index": i,  # 파일 내 절대 행 인덱스 (삭제용)
            "description": e["description"],
            "args": e["args"],
        })
    return [{"name": n, "rows": grouped[n]} for n in order]


class AddRowRequest(BaseModel):
    sheet: str = ""
    description: str
    args: dict[str, str] = {}


class DeleteRowRequest(BaseModel):
    index: int  # GET 응답의 rows[].index (파일 내 절대 인덱스)


class UpdateRowRequest(BaseModel):
    index: int  # GET 응답의 rows[].index (파일 내 절대 인덱스)
    sheet: str = ""
    description: str
    args: dict[str, str] = {}


@router.get("/{module}/{function}")
async def get_param_db(module: str, function: str):
    """함수의 파라미터 DB 조회. 파일이 없으면 함수 인자 기반 빈 템플릿 반환."""
    path = _safe_path(module, function)
    arg_cols, entries = await asyncio.to_thread(_read_db, path)
    param_names = await asyncio.to_thread(_function_param_names, module, function)
    if not arg_cols:
        arg_cols = param_names
    return {
        "exists": path.is_file(),
        "headers": arg_cols,
        "param_names": param_names,
        "sheets": _group_sheets(entries),
        "total": len(entries),
    }


@router.post("/{module}/{function}/rows")
async def add_param_db_row(module: str, function: str, req: AddRowRequest):
    """현재 입력값을 DB 에 행으로 추가. 파일이 없으면 함수 인자 헤더로 생성."""
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="Description is required")
    path = _safe_path(module, function)

    def _do():
        with _write_lock:
            arg_cols, entries = _read_db(path)
            if not arg_cols:
                arg_cols = _function_param_names(module, function) or sorted(req.args.keys())
            # 기존 헤더에 없는 새 인자는 열로 추가 (함수 시그니처 변경 대응)
            for k in req.args.keys():
                if k not in arg_cols:
                    arg_cols.append(k)
            entries.append({
                "sheet": req.sheet.strip(),
                "description": req.description.strip(),
                "args": {c: str(req.args.get(c, "")) for c in arg_cols},
            })
            _write_db(path, arg_cols, entries)
            return len(entries)

    total = await asyncio.to_thread(_do)
    return {"success": True, "total": total}


@router.post("/{module}/{function}/update-row")
async def update_param_db_row(module: str, function: str, req: UpdateRowRequest):
    """행 수정 (index = GET 응답 rows[].index). 설명/시트/인자 전부 교체."""
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="Description is required")
    path = _safe_path(module, function)

    def _do():
        with _write_lock:
            arg_cols, entries = _read_db(path)
            if not (0 <= req.index < len(entries)):
                raise HTTPException(status_code=404, detail="Row not found")
            # 기존 헤더에 없는 새 인자는 열로 추가 (함수 시그니처 변경 대응)
            for k in req.args.keys():
                if k not in arg_cols:
                    arg_cols.append(k)
            entries[req.index] = {
                "sheet": req.sheet.strip(),
                "description": req.description.strip(),
                "args": {c: str(req.args.get(c, "")) for c in arg_cols},
            }
            _write_db(path, arg_cols, entries)

    await asyncio.to_thread(_do)
    return {"success": True}


@router.post("/{module}/{function}/delete-row")
async def delete_param_db_row(module: str, function: str, req: DeleteRowRequest):
    """행 삭제 (index = GET 응답 rows[].index)."""
    path = _safe_path(module, function)

    def _do():
        with _write_lock:
            arg_cols, entries = _read_db(path)
            if not (0 <= req.index < len(entries)):
                raise HTTPException(status_code=404, detail="Row not found")
            entries.pop(req.index)
            _write_db(path, arg_cols, entries)
            return len(entries)

    total = await asyncio.to_thread(_do)
    return {"success": True, "total": total}


@router.get("/{module}/{function}/export")
async def export_param_db(module: str, function: str):
    """CSV 사본 다운로드. 파일이 없으면 함수 인자 헤더만 있는 템플릿을 내려준다."""
    from fastapi.responses import Response

    path = _safe_path(module, function)

    def _do() -> bytes:
        if path.is_file():
            return path.read_bytes()
        cols = _function_param_names(module, function)
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow([_SHEET_COL, _DESC_COL, *cols])
        return buf.getvalue().encode("utf-8-sig")

    data = await asyncio.to_thread(_do)
    filename = f"{module}.{function}.csv"
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{module}/{function}/import")
async def import_param_db(module: str, function: str, file: UploadFile = File(...)):
    """CSV 업로드로 DB 전체 교체. 헤더가 함수 인자와 다르면 warnings 로 알려준다."""
    path = _safe_path(module, function)
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="CSV file too large (>10MB)")
    arg_cols, entries = _parse_csv_text(_decode_csv_bytes(data))
    if not arg_cols:
        raise HTTPException(status_code=400, detail="CSV has no parameter columns")

    warnings: list[str] = []
    param_names = await asyncio.to_thread(_function_param_names, module, function)
    if param_names:
        unknown = [c for c in arg_cols if c not in param_names]
        missing = [p for p in param_names if p not in arg_cols]
        if unknown:
            warnings.append(f"함수 인자가 아닌 열: {', '.join(unknown)}")
        if missing:
            warnings.append(f"CSV 에 없는 함수 인자: {', '.join(missing)}")

    def _do():
        with _write_lock:
            _write_db(path, arg_cols, entries)

    await asyncio.to_thread(_do)
    logger.info("paramdb: imported %s rows into %s/%s (%s)",
                len(entries), module, function, file.filename)
    return {"success": True, "total": len(entries), "warnings": warnings}
