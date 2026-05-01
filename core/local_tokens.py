import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import DATA_DIR, TOKENS_DIR

UPLOAD_PLATFORMS = ("sub2api",)
LOCAL_TOKENS_DB_NAME = "local_tokens.db"

_TOKEN_FILENAME_RE = re.compile(r"_(\d{10,})\.json$")


def get_local_tokens_db_path() -> Path:
    data_dir = Path(DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / LOCAL_TOKENS_DB_NAME


def _db_path() -> Path:
    return get_local_tokens_db_path()


def _connect() -> sqlite3.Connection:
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL DEFAULT '',
            refresh_token TEXT NOT NULL DEFAULT '',
            expired TEXT NOT NULL DEFAULT '',
            uploaded_platforms_json TEXT NOT NULL DEFAULT '[]',
            uploaded_at_json TEXT NOT NULL DEFAULT '{}',
            content_json TEXT NOT NULL DEFAULT '{}',
            sub2api_uploaded INTEGER NOT NULL DEFAULT 0,
            sort_ns INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_tokens_sort ON local_tokens(sort_ns DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_tokens_identity ON local_tokens(email, refresh_token)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_tokens_sub2api ON local_tokens(sub2api_uploaded, sort_ns DESC, id DESC)"
    )
    return conn


@contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _json_dumps(value: Any, *, fallback: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return fallback


def _json_loads(text: str, *, default: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default


def _local_token_sort_key(file_name: str) -> int:
    match = _TOKEN_FILENAME_RE.search(str(file_name or ""))
    return int(match.group(1)) if match else 0


def _build_local_token_filename(email: str, *, sort_ns: Optional[int] = None) -> str:
    email_part = str(email or "unknown").strip() or "unknown"
    email_part = email_part.replace("@", "_")
    return f"token_{email_part}_{int(sort_ns or time.time_ns())}.json"


def extract_uploaded_platforms(token_data: Dict[str, Any]) -> List[str]:
    platforms = set()
    raw_platforms = token_data.get("uploaded_platforms")
    if isinstance(raw_platforms, list):
        for item in raw_platforms:
            name = str(item).strip().lower()
            if name in UPLOAD_PLATFORMS:
                platforms.add(name)
    return [platform for platform in UPLOAD_PLATFORMS if platform in platforms]


def is_sub2api_uploaded(token_data: Dict[str, Any]) -> bool:
    return "sub2api" in extract_uploaded_platforms(token_data)


def _normalize_uploaded_at(token_data: Dict[str, Any]) -> Dict[str, str]:
    raw_uploaded_at = token_data.get("uploaded_at")
    if not isinstance(raw_uploaded_at, dict):
        return {}
    normalized: Dict[str, str] = {}
    for key, value in raw_uploaded_at.items():
        name = str(key or "").strip().lower()
        if name in UPLOAD_PLATFORMS:
            normalized[name] = str(value or "").strip()
    return normalized


def _prepare_token_payload(
    token_data: Dict[str, Any],
    *,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    payload = dict(token_data if isinstance(token_data, dict) else {})
    uploaded_platforms = extract_uploaded_platforms(payload)
    uploaded_at = _normalize_uploaded_at(payload)
    if uploaded_platforms:
        payload["uploaded_platforms"] = uploaded_platforms
    else:
        payload.pop("uploaded_platforms", None)
    if uploaded_at:
        payload["uploaded_at"] = uploaded_at
    else:
        payload.pop("uploaded_at", None)

    email = str(payload.get("email") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    expired = str(payload.get("expired") or "").strip()
    resolved_filename = str(filename or "").strip()
    sort_ns = _local_token_sort_key(resolved_filename)
    if sort_ns <= 0:
        sort_ns = time.time_ns()
    if not resolved_filename:
        resolved_filename = _build_local_token_filename(email, sort_ns=sort_ns)

    now_iso = datetime.now().isoformat(timespec="seconds")
    return {
        "filename": resolved_filename,
        "email": email,
        "refresh_token": refresh_token,
        "expired": expired,
        "uploaded_platforms": uploaded_platforms,
        "uploaded_at": uploaded_at,
        "content": payload,
        "sub2api_uploaded": 1 if "sub2api" in uploaded_platforms else 0,
        "sort_ns": int(sort_ns),
        "timestamp": now_iso,
    }


def save_local_token(
    token_data: Dict[str, Any],
    *,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    prepared = _prepare_token_payload(token_data, filename=filename)
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO local_tokens (
                filename,
                email,
                refresh_token,
                expired,
                uploaded_platforms_json,
                uploaded_at_json,
                content_json,
                sub2api_uploaded,
                sort_ns,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filename) DO UPDATE SET
                email = excluded.email,
                refresh_token = excluded.refresh_token,
                expired = excluded.expired,
                uploaded_platforms_json = excluded.uploaded_platforms_json,
                uploaded_at_json = excluded.uploaded_at_json,
                content_json = excluded.content_json,
                sub2api_uploaded = excluded.sub2api_uploaded,
                sort_ns = excluded.sort_ns,
                updated_at = excluded.updated_at
            """,
            (
                prepared["filename"],
                prepared["email"],
                prepared["refresh_token"],
                prepared["expired"],
                _json_dumps(prepared["uploaded_platforms"], fallback="[]"),
                _json_dumps(prepared["uploaded_at"], fallback="{}"),
                _json_dumps(prepared["content"], fallback="{}"),
                prepared["sub2api_uploaded"],
                prepared["sort_ns"],
                prepared["timestamp"],
                prepared["timestamp"],
            ),
        )
    return {
        "filename": prepared["filename"],
        "email": prepared["email"],
        "refresh_token": prepared["refresh_token"],
        "expired": prepared["expired"],
    }


def save_local_token_text(
    token_text: str,
    *,
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    parsed = json.loads(str(token_text or "{}"))
    if not isinstance(parsed, dict):
        raise ValueError("token payload must be a JSON object")
    return save_local_token(parsed, filename=filename)


def _extract_import_items(payload: Any) -> tuple[List[Any], str]:
    if isinstance(payload, dict):
        raw_tokens = payload.get("tokens")
        if isinstance(raw_tokens, list):
            return raw_tokens, "bundle"
        return [payload], "single"
    if isinstance(payload, list):
        return payload, "list"
    raise ValueError("仅支持单个 Token 对象、Token 数组或包含 tokens 数组的导出 JSON")


def _normalize_import_token_item(raw_item: Any, index: int) -> tuple[Optional[str], Dict[str, Any]]:
    if not isinstance(raw_item, dict):
        raise ValueError(f"第 {index} 项不是 JSON 对象")

    filename = str(raw_item.get("filename") or "").strip() or None
    raw_content = raw_item.get("content")

    if raw_content is None:
        token_data = dict(raw_item)
        token_data.pop("filename", None)
    else:
        if not isinstance(raw_content, dict):
            raise ValueError(f"第 {index} 项的 content 字段必须是对象")
        token_data = dict(raw_content)
        for key in ("email", "refresh_token", "expired", "uploaded_platforms", "uploaded_at"):
            if key not in token_data and key in raw_item:
                token_data[key] = raw_item.get(key)

    if not token_data:
        raise ValueError(f"第 {index} 项缺少 Token 内容")

    if not any(
        str(token_data.get(field) or "").strip()
        for field in ("email", "refresh_token", "access_token", "id_token")
    ):
        raise ValueError(f"第 {index} 项缺少有效的 Token 字段")

    return filename, token_data


def import_local_token_payload(payload: Any) -> Dict[str, Any]:
    items, source_format = _extract_import_items(payload)
    imported = 0
    failed = 0
    results: List[Dict[str, Any]] = []

    for index, item in enumerate(items, start=1):
        try:
            filename, token_data = _normalize_import_token_item(item, index)
            saved = save_local_token(token_data, filename=filename)
            imported += 1
            results.append({
                "index": index,
                "ok": True,
                "filename": str(saved.get("filename") or ""),
                "email": str(saved.get("email") or ""),
            })
        except Exception as exc:
            failed += 1
            results.append({
                "index": index,
                "ok": False,
                "error": str(exc),
            })

    return {
        "source_format": source_format,
        "total": len(items),
        "imported": imported,
        "failed": failed,
        "results": results,
    }


def import_legacy_json_tokens(
    source_dir: Optional[Path | str] = None,
    *,
    delete_imported: bool = False,
) -> Dict[str, Any]:
    token_dir = Path(source_dir) if source_dir is not None else Path(TOKENS_DIR)
    files = sorted(
        [path for path in token_dir.glob("*.json") if path.is_file()],
        key=lambda path: (_local_token_sort_key(path.name), path.name),
        reverse=True,
    )

    imported = 0
    failed = 0
    deleted = 0
    delete_failed = 0
    results: List[Dict[str, Any]] = []

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("token payload must be a JSON object")
            saved = save_local_token(payload, filename=path.name)
            imported += 1
            item = {
                "filename": path.name,
                "ok": True,
                "email": str(saved.get("email") or ""),
            }
            if delete_imported:
                try:
                    path.unlink()
                    deleted += 1
                    item["deleted"] = True
                except Exception as exc:
                    delete_failed += 1
                    item["deleted"] = False
                    item["delete_error"] = str(exc)
            results.append(item)
        except Exception as exc:
            failed += 1
            results.append({
                "filename": path.name,
                "ok": False,
                "error": str(exc),
            })

    return {
        "source_dir": str(token_dir),
        "db_path": str(get_local_tokens_db_path()),
        "total": len(files),
        "imported": imported,
        "failed": failed,
        "delete_requested": bool(delete_imported),
        "deleted": deleted,
        "delete_failed": delete_failed,
        "results": results,
    }


def _row_to_record(
    row: sqlite3.Row,
    *,
    include_content: bool = False,
) -> Dict[str, Any]:
    uploaded_platforms = _json_loads(
        str(row["uploaded_platforms_json"] or "[]"),
        default=[],
    )
    if not isinstance(uploaded_platforms, list):
        uploaded_platforms = []
    record: Dict[str, Any] = {
        "filename": str(row["filename"] or ""),
        "email": str(row["email"] or ""),
        "expired": str(row["expired"] or ""),
        "uploaded_platforms": extract_uploaded_platforms({"uploaded_platforms": uploaded_platforms}),
        "refresh_token": str(row["refresh_token"] or ""),
    }
    if include_content:
        content = _json_loads(str(row["content_json"] or "{}"), default={})
        record["content"] = content if isinstance(content, dict) else {}
    return record


def get_local_token_record(
    filename: str,
    *,
    include_content: bool = False,
) -> Optional[Dict[str, Any]]:
    normalized = str(filename or "").strip()
    if not normalized:
        return None
    with _connection() as conn:
        row = conn.execute(
            """
            SELECT filename, email, refresh_token, expired, uploaded_platforms_json, content_json
            FROM local_tokens
            WHERE filename = ?
            """,
            (normalized,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_record(row, include_content=include_content)


def get_local_token_records_by_filenames(
    filenames: List[str],
    *,
    include_content: bool = False,
) -> Dict[str, Dict[str, Any]]:
    normalized_filenames = [
        str(filename or "").strip()
        for filename in list(filenames or [])
        if str(filename or "").strip()
    ]
    if not normalized_filenames:
        return {}

    records: Dict[str, Dict[str, Any]] = {}
    chunk_size = 500
    with _connection() as conn:
        for start in range(0, len(normalized_filenames), chunk_size):
            chunk = normalized_filenames[start:start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT filename, email, refresh_token, expired, uploaded_platforms_json, content_json
                FROM local_tokens
                WHERE filename IN ({placeholders})
                """,
                tuple(chunk),
            ).fetchall()
            for row in rows:
                record = _row_to_record(row, include_content=include_content)
                records[str(record.get("filename") or "")] = record
    return records


def delete_local_token(filename: str) -> bool:
    normalized = str(filename or "").strip()
    if not normalized:
        return False
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM local_tokens WHERE filename = ?",
            (normalized,),
        )
    return int(cursor.rowcount or 0) > 0


def set_token_uploaded_platform(filename: str, platform: str, synced: bool) -> bool:
    platform_name = str(platform).strip().lower()
    if platform_name not in UPLOAD_PLATFORMS:
        return False

    current = get_local_token_record(str(filename or "").strip(), include_content=True)
    if not current:
        return False

    token_data = dict(current.get("content") or {})
    platforms = set(extract_uploaded_platforms(token_data))
    if synced:
        platforms.add(platform_name)
    else:
        platforms.discard(platform_name)
    token_data["uploaded_platforms"] = [name for name in UPLOAD_PLATFORMS if name in platforms]

    uploaded_at = token_data.get("uploaded_at")
    if not isinstance(uploaded_at, dict):
        uploaded_at = {}
    if synced:
        uploaded_at[platform_name] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        uploaded_at.pop(platform_name, None)
    if uploaded_at:
        token_data["uploaded_at"] = uploaded_at
    else:
        token_data.pop("uploaded_at", None)

    try:
        save_local_token(token_data, filename=str(current.get("filename") or ""))
        return True
    except Exception:
        return False


def mark_token_uploaded_platform(filename: str, platform: str) -> bool:
    return set_token_uploaded_platform(filename, platform, True)


def sub2api_identity_keys(email: str, refresh_token: str) -> List[str]:
    keys: List[str] = []
    email_norm = str(email or "").strip().lower()
    refresh_token_norm = str(refresh_token or "").strip()
    if email_norm:
        keys.append(f"email:{email_norm}")
    if refresh_token_norm:
        keys.append(f"rt:{refresh_token_norm}")
    return keys


def load_local_token_identity_keys(max_files: int = 20000) -> set[str]:
    keys: set[str] = set()
    safe_limit = max(1, int(max_files or 20000))
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT email, refresh_token
            FROM local_tokens
            ORDER BY sort_ns DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    for row in rows:
        keys.update(
            sub2api_identity_keys(
                email=str(row["email"] or ""),
                refresh_token=str(row["refresh_token"] or ""),
            )
        )
    return keys


def _is_token_not_expired(expired_value: Any) -> bool:
    text = str(expired_value or "").strip()
    if not text:
        return True
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() > time.time()
    except Exception:
        return True


def _filter_local_token_records(
    records: List[Dict[str, Any]],
    *,
    status: str = "all",
    keyword: str = "",
) -> List[Dict[str, Any]]:
    normalized_status = str(status or "all").strip().lower() or "all"
    keyword_norm = str(keyword or "").strip().lower()
    filtered: List[Dict[str, Any]] = []
    for item in records:
        uploaded = "sub2api" in set(item.get("uploaded_platforms") or [])
        if normalized_status == "synced" and not uploaded:
            continue
        if normalized_status == "unsynced" and uploaded:
            continue
        if keyword_norm:
            email = str(item.get("email") or "").strip().lower()
            file_name = str(item.get("filename") or "").strip().lower()
            if keyword_norm not in email and keyword_norm not in file_name:
                continue
        filtered.append(item)
    return filtered


def _paginate_local_token_records(
    records: List[Dict[str, Any]],
    *,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    safe_page_size = max(10, min(int(page_size or 20), 200))
    total = len(records)
    total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
    safe_page = max(1, min(int(page or 1), total_pages))
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "items": records[start:end],
        "page": safe_page,
        "page_size": safe_page_size,
        "filtered_total": total,
        "total_pages": total_pages,
    }


def load_local_token_records(*, include_content: bool = False) -> List[Dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT filename, email, refresh_token, expired, uploaded_platforms_json, content_json
            FROM local_tokens
            ORDER BY sort_ns DESC, id DESC
            """
        ).fetchall()
    return [_row_to_record(row, include_content=include_content) for row in rows]


def list_local_token_filenames() -> List[str]:
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT filename
            FROM local_tokens
            ORDER BY sort_ns DESC, id DESC
            """
        ).fetchall()
    return [str(row["filename"] or "") for row in rows if str(row["filename"] or "").strip()]


def read_local_token_inventory(
    *,
    status: str = "all",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    include_content: bool = True,
) -> Dict[str, Any]:
    base_records = load_local_token_records(include_content=include_content)
    filtered_records = _filter_local_token_records(
        base_records,
        status=status,
        keyword=keyword,
    )
    paged = _paginate_local_token_records(
        filtered_records,
        page=page,
        page_size=page_size,
    )
    page_items = paged["items"]

    total = len(base_records)
    valid = sum(1 for item in base_records if _is_token_not_expired(item.get("expired")))
    synced = sum(
        1
        for item in base_records
        if "sub2api" in set(item.get("uploaded_platforms") or [])
    )

    for item in page_items:
        item.pop("refresh_token", None)

    return {
        "items": page_items,
        "page": int(paged["page"]),
        "page_size": int(paged["page_size"]),
        "filtered_total": int(paged["filtered_total"]),
        "total_pages": int(paged["total_pages"]),
        "total": total,
        "summary": {
            "total": total,
            "valid": valid,
            "synced": synced,
            "unsynced": max(0, total - synced),
        },
        "status": str(status or "all"),
        "keyword": str(keyword or ""),
    }
