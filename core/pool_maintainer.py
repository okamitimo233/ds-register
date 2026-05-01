"""
账号池维护模块
Sub2Api 平台的探测、清理、计数和补号逻辑。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import httpx

from .token_health import probe_token_health

_FALLBACK_ACCOUNT_PAGE_SIZE = 100
_ACCOUNT_PAGE_SIZE = 500
_ACCOUNT_PAGE_FETCH_WORKERS = 8
_SESSION_POOL_SIZE = 32
_PROBE_WORKERS = 16
_DELETE_WORKERS = 24
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_REQUEST_RETRY_TOTAL = 3
_REQUEST_RETRY_BACKOFF = 1.0
_DEFAULT_TIMEOUT = httpx.Timeout(30.0)
_RETRYABLE_REQUEST_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError)


def _normalize_proxy_url(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if ":" in value:
        return f"http://{value}"
    return ""


def _build_http_client(proxy: str = "") -> httpx.Client:
    normalized_proxy = _normalize_proxy_url(proxy)
    limits = httpx.Limits(
        max_connections=_SESSION_POOL_SIZE,
        max_keepalive_connections=_SESSION_POOL_SIZE,
    )
    transport = httpx.HTTPTransport(
        proxy=normalized_proxy or None,
        retries=3,
        limits=limits,
    )
    return httpx.Client(
        transport=transport,
        timeout=_DEFAULT_TIMEOUT,
        follow_redirects=True,
    )


def _request_with_status_retries(
    request: Callable[..., httpx.Response],
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    response: Optional[httpx.Response] = None
    last_exc: Optional[Exception] = None
    for attempt in range(_REQUEST_RETRY_TOTAL + 1):
        try:
            response = request(method, url, **kwargs)
        except _RETRYABLE_REQUEST_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= _REQUEST_RETRY_TOTAL:
                raise
            time.sleep(_REQUEST_RETRY_BACKOFF * (2 ** attempt))
            continue
        if response.status_code not in _RETRYABLE_STATUS_CODES or attempt >= _REQUEST_RETRY_TOTAL:
            return response
        time.sleep(_REQUEST_RETRY_BACKOFF * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    if response is None:
        raise RuntimeError("HTTP request did not produce a response")
    return response


def _parse_time_to_epoch(raw: Any) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        return {}
    payload = raw.split(".")[1]
    try:
        pad = 4 - (len(payload) % 4)
        if pad != 4:
            payload += "=" * pad
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _normalize_group_ids(group_ids: Optional[List[int]]) -> List[int]:
    result: List[int] = []
    seen: set[int] = set()
    for item in list(group_ids or []):
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _build_account_payload(
    email: str,
    token_data: Dict[str, Any],
    group_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()

    access_payload = _decode_jwt_payload(access_token) if access_token else {}
    access_auth = access_payload.get("https://api.openai.com/auth") or {}
    chatgpt_account_id = str(
        access_auth.get("chatgpt_account_id") or token_data.get("account_id") or ""
    ).strip()
    chatgpt_user_id = str(access_auth.get("chatgpt_user_id") or "").strip()
    exp_timestamp = access_payload.get("exp", 0)
    expires_at = (
        int(exp_timestamp)
        if isinstance(exp_timestamp, int) and exp_timestamp > 0
        else int(time.time()) + 863999
    )

    id_payload = _decode_jwt_payload(id_token) if id_token else {}
    id_auth = id_payload.get("https://api.openai.com/auth") or {}
    organization_id = str(id_auth.get("organization_id") or "").strip()
    if not organization_id:
        organizations = id_auth.get("organizations") or []
        if organizations:
            organization_id = str((organizations[0] or {}).get("id") or "").strip()
    normalized_group_ids = _normalize_group_ids(group_ids)

    return {
        "name": str(email or "").strip(),
        "credentials": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 863999,
            "expires_at": expires_at,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "organization_id": organization_id,
        },
        "extra": {"email": str(email or "").strip().lower()},
        "group_ids": normalized_group_ids,
        "concurrency": 10,
        "priority": 1,
        "status": "active",
        "auto_pause_on_expired": True,
    }


class Sub2ApiMaintainer:
    """Sub2Api 平台池维护 — 通过 Admin API 管理账号池"""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        min_candidates: int = 200,
        email: str = "",
        password: str = "",
        openai_proxy: str = "",
        group_ids: Optional[List[int]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.min_candidates = min_candidates
        self.email = email
        self.password = password
        self.openai_proxy = str(openai_proxy or "").strip()
        self.group_ids = _normalize_group_ids(group_ids)
        self._auth_lock = threading.Lock()
        self._page_size_lock = threading.Lock()
        self._session_local = threading.local()
        self._account_page_size = max(1, int(_ACCOUNT_PAGE_SIZE))

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        if result <= 0:
            return None
        return result

    def _get_account_page_size(self, requested: Optional[int] = None) -> int:
        preferred = self._positive_int(requested) or _ACCOUNT_PAGE_SIZE
        with self._page_size_lock:
            negotiated = self._positive_int(self._account_page_size) or _ACCOUNT_PAGE_SIZE
        return max(1, min(preferred, negotiated))

    def _set_account_page_size(self, page_size: int) -> None:
        normalized = self._positive_int(page_size)
        if normalized is None:
            return
        with self._page_size_lock:
            self._account_page_size = normalized

    def _effective_page_size(
        self,
        data: Dict[str, Any],
        requested_page_size: int,
        item_count: int,
        page: int,
    ) -> int:
        total = self._positive_int(data.get("total")) or 0
        if page == 1 and total > item_count and 0 < item_count < requested_page_size:
            return item_count

        payload_page_size = self._positive_int(
            data.get("page_size") or data.get("pageSize") or data.get("per_page")
        )
        if payload_page_size is not None:
            return max(1, min(requested_page_size, payload_page_size))

        return requested_page_size

    @staticmethod
    def _normalize_account_status_filter(status: Any) -> str:
        normalized = str(status or "").strip().lower()
        if normalized == "normal":
            return "active"
        if normalized in ("active", "error", "disabled"):
            return normalized
        return ""

    @staticmethod
    def _coerce_account_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            for key in ("list", "records", "rows"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    raw_items = candidate
                    break
        if not isinstance(raw_items, list):
            return []
        return [item for item in raw_items if isinstance(item, dict)]

    def _normalize_accounts_page(
        self,
        payload: Any,
        *,
        page: int,
        requested_page_size: int,
    ) -> Dict[str, Any]:
        normalized_payload = dict(payload) if isinstance(payload, dict) else {}
        items = self._coerce_account_items(normalized_payload)
        effective_page_size = self._effective_page_size(
            normalized_payload,
            requested_page_size=requested_page_size,
            item_count=len(items),
            page=page,
        )
        normalized_payload["items"] = items
        normalized_payload["page"] = self._positive_int(
            normalized_payload.get("page")
            or normalized_payload.get("current")
            or normalized_payload.get("current_page")
            or normalized_payload.get("currentPage")
        ) or page
        normalized_payload["page_size"] = effective_page_size
        normalized_payload["total"] = self._positive_int(normalized_payload.get("total")) or 0
        return normalized_payload

    def _iter_account_pages(
        self,
        *,
        timeout: int = 15,
        page_size: int = _ACCOUNT_PAGE_SIZE,
    ):
        current_page = 1
        current_page_size = self._get_account_page_size(page_size)
        while True:
            data = self.list_accounts(
                page=current_page,
                page_size=current_page_size,
                timeout=timeout,
            )
            normalized_data = data if isinstance(data, dict) else {}
            items = self._coerce_account_items(normalized_data)
            total = self._positive_int(normalized_data.get("total")) or 0
            effective_page_size = self._effective_page_size(
                normalized_data,
                requested_page_size=current_page_size,
                item_count=len(items),
                page=current_page,
            )
            yield current_page, effective_page_size, total, items
            if not items:
                break
            if total > 0 and current_page * effective_page_size >= total:
                break
            if len(items) < effective_page_size:
                break
            current_page += 1
            current_page_size = effective_page_size

    def _get_client(self) -> httpx.Client:
        client = getattr(self._session_local, "client", None)
        if client is None:
            client = _build_http_client()
            self._session_local.client = client
        return client

    def _login(self) -> str:
        client = self._get_client()
        resp = _request_with_status_retries(
            client.request,
            "POST",
            f"{self.base_url}/api/v1/auth/login",
            json={"email": self.email, "password": self.password},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        token = (
            data.get("token")
            or data.get("access_token")
            or (data.get("data") or {}).get("token")
            or (data.get("data") or {}).get("access_token")
            or ""
        )
        if token:
            self.bearer_token = token
        return token

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
        url = f"{self.base_url}{path}"
        client = self._get_client()
        resp = _request_with_status_retries(client.request, method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401 and self.email and self.password:
            current_token = self.bearer_token
            with self._auth_lock:
                if self.bearer_token == current_token:
                    self._login()
            refreshed_token = self.bearer_token
            if refreshed_token or self.bearer_token != current_token:
                resp = _request_with_status_retries(client.request, method, url, headers=self._headers(), **kwargs)
                return resp
            resp = _request_with_status_retries(client.request, method, url, headers=self._headers(), **kwargs)
        return resp

    def get_dashboard_stats(self, timeout: int = 15) -> Dict[str, Any]:
        resp = self._request(
            "GET", "/api/v1/admin/dashboard/stats",
            params={"timezone": "Asia/Shanghai"}, timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data") if isinstance(data.get("data"), dict) else data

    def list_accounts(
        self,
        page: int = 1,
        page_size: int = _ACCOUNT_PAGE_SIZE,
        timeout: int = 15,
        search: str = "",
        status: str = "",
    ) -> Dict[str, Any]:
        safe_page = max(1, int(page or 1))
        requested_page_size = self._get_account_page_size(page_size)
        params = {
            "page": safe_page, "page_size": requested_page_size,
            "platform": "openai", "type": "oauth",
        }
        search_text = str(search or "").strip()
        status_filter = self._normalize_account_status_filter(status)
        if search_text:
            params["search"] = search_text
        if status_filter:
            params["status"] = status_filter
        resp = self._request(
            "GET", "/api/v1/admin/accounts",
            params=params, timeout=timeout,
        )
        if (
            int(resp.status_code or 0) in (400, 413, 414, 422)
            and requested_page_size > _FALLBACK_ACCOUNT_PAGE_SIZE
        ):
            self._set_account_page_size(_FALLBACK_ACCOUNT_PAGE_SIZE)
            return self.list_accounts(
                page=safe_page,
                page_size=_FALLBACK_ACCOUNT_PAGE_SIZE,
                timeout=timeout,
                search=search_text,
                status=status_filter,
            )
        resp.raise_for_status()
        data = resp.json()
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        return self._normalize_accounts_page(
            payload,
            page=safe_page,
            requested_page_size=requested_page_size,
        )

    def _list_all_accounts(self, timeout: int = 15, page_size: int = _ACCOUNT_PAGE_SIZE) -> List[Dict[str, Any]]:
        all_accounts: List[Dict[str, Any]] = []
        seen_ids: set[int] = set()
        for _, _, _, items in self._iter_account_pages(timeout=timeout, page_size=page_size):
            for item in items:
                account_id = self._normalize_account_id(item.get("id"))
                if account_id is None:
                    all_accounts.append(item)
                    continue
                if account_id in seen_ids:
                    continue
                seen_ids.add(account_id)
                all_accounts.append(item)
        return all_accounts

    def _list_account_items_by_ids(
        self,
        ids: List[int],
        timeout: int = 15,
    ) -> Dict[int, Dict[str, Any]]:
        id_set = {account_id for account_id in ids if isinstance(account_id, int) and account_id > 0}
        if not id_set:
            return {}
        result: Dict[int, Dict[str, Any]] = {}
        for _, _, _, items in self._iter_account_pages(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE):
            for item in items:
                account_id = self._normalize_account_id((item or {}).get("id"))
                if account_id is not None and account_id in id_set and isinstance(item, dict):
                    result[account_id] = item
                    id_set.discard(account_id)
            if not id_set:
                break
        return result

    def _account_identity(self, item: Dict[str, Any]) -> Dict[str, str]:
        email = ""
        rt = ""
        extra = item.get("extra")
        if isinstance(extra, dict):
            email = str(extra.get("email") or "").strip().lower()
        if not email:
            name = str(item.get("name") or "").strip().lower()
            if "@" in name:
                email = name
        creds = item.get("credentials")
        if isinstance(creds, dict):
            rt = str(creds.get("refresh_token") or "").strip()
        return {"email": email, "refresh_token": rt}

    @staticmethod
    def _account_sort_key(item: Dict[str, Any]) -> tuple[float, int]:
        updated = _parse_time_to_epoch(item.get("updated_at") or item.get("updatedAt"))
        try:
            item_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            item_id = 0
        return (updated, item_id)

    @staticmethod
    def _normalize_account_id(raw: Any) -> Optional[int]:
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            return None
        if account_id <= 0:
            return None
        return account_id

    @staticmethod
    def _is_abnormal_status(status: Any) -> bool:
        return str(status or "").strip().lower() in ("error", "disabled")

    def _build_dedupe_plan(self, all_accounts: List[Dict[str, Any]], details_limit: int = 120) -> Dict[str, Any]:
        id_to_account: Dict[int, Dict[str, Any]] = {}
        parent: Dict[int, int] = {}
        key_to_ids: Dict[str, List[int]] = {}

        for item in all_accounts:
            acc_id = self._normalize_account_id(item.get("id"))
            if acc_id is None:
                continue
            id_to_account[acc_id] = item
            parent[acc_id] = acc_id

            identity = self._account_identity(item)
            email = identity["email"]
            refresh_token = identity["refresh_token"]
            if email:
                key_to_ids.setdefault(f"email:{email}", []).append(acc_id)
            if refresh_token:
                key_to_ids.setdefault(f"rt:{refresh_token}", []).append(acc_id)

        def find(x: int) -> int:
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != x:
                nxt = parent[x]
                parent[x] = root
                x = nxt
            return root

        def union(a: int, b: int) -> None:
            ra = find(a)
            rb = find(b)
            if ra != rb:
                parent[rb] = ra

        for ids in key_to_ids.values():
            if len(ids) > 1:
                head = ids[0]
                for acc_id in ids[1:]:
                    union(head, acc_id)

        components: Dict[int, List[int]] = {}
        for acc_id in id_to_account.keys():
            root = find(acc_id)
            components.setdefault(root, []).append(acc_id)

        duplicate_groups = [ids for ids in components.values() if len(ids) > 1]
        delete_ids: List[int] = []
        group_details: List[Dict[str, Any]] = []

        for group_ids in duplicate_groups:
            group_items = [id_to_account[i] for i in group_ids]
            keep_item = max(group_items, key=self._account_sort_key)
            keep_id = self._normalize_account_id(keep_item.get("id")) or 0
            group_delete_ids = sorted([i for i in group_ids if i != keep_id], reverse=True)
            delete_ids.extend(group_delete_ids)

            if len(group_details) < details_limit:
                emails_set = set()
                for it in group_items:
                    identity = self._account_identity(it)
                    if identity["email"]:
                        emails_set.add(identity["email"])
                emails = sorted(emails_set)
                group_details.append({
                    "keep_id": keep_id,
                    "delete_ids": group_delete_ids,
                    "size": len(group_ids),
                    "emails": emails,
                })

        return {
            "duplicate_groups": len(duplicate_groups),
            "duplicate_accounts": sum(len(g) for g in duplicate_groups),
            "delete_ids": delete_ids,
            "groups_preview": group_details,
            "truncated_groups": max(0, len(duplicate_groups) - len(group_details)),
        }

    def _count_candidate_accounts(
        self,
        all_accounts: List[Dict[str, Any]],
        dedupe_plan: Optional[Dict[str, Any]] = None,
    ) -> int:
        duplicate_delete_ids = {
            int(account_id)
            for account_id in ((dedupe_plan or {}).get("delete_ids") or [])
            if isinstance(account_id, int)
        }
        count = 0
        for item in all_accounts:
            account_id = self._normalize_account_id(item.get("id"))
            if account_id is None or account_id in duplicate_delete_ids:
                continue
            if self._is_abnormal_status(item.get("status")):
                continue
            count += 1
        return count

    def _build_pool_status_snapshot(
        self,
        all_accounts: List[Dict[str, Any]],
        dedupe_plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        effective_dedupe_plan = dedupe_plan or self._build_dedupe_plan(all_accounts, details_limit=1)
        error_count = sum(
            1 for account in all_accounts
            if self._is_abnormal_status(account.get("status"))
        )
        candidate_count = self._count_candidate_accounts(all_accounts, effective_dedupe_plan)
        return {
            "total": len(all_accounts),
            "candidates": candidate_count,
            "error_count": error_count,
            "duplicate_groups": int(effective_dedupe_plan.get("duplicate_groups", 0)),
            "duplicate_to_delete": len(effective_dedupe_plan.get("delete_ids") or []),
            "threshold": self.min_candidates,
            "healthy": candidate_count >= self.min_candidates,
            "percent": round(candidate_count / self.min_candidates * 100, 1) if self.min_candidates > 0 else 100,
            "last_checked": time.strftime("%Y-%m-%d %H:%M:%S"),
            "error": None,
        }

    def _update_account_tokens(
        self,
        account_id: int,
        email: str,
        token_data: Dict[str, Any],
        timeout: int = 20,
    ) -> Dict[str, Any]:
        payload = _build_account_payload(email, token_data, group_ids=self.group_ids)
        resp = self._request(
            "PUT",
            f"/api/v1/admin/accounts/{int(account_id)}",
            json=payload,
            timeout=timeout,
        )
        body = ""
        try:
            body = str(resp.text or "")
        except Exception:
            body = ""
        return {
            "ok": resp.status_code in (200, 201),
            "status": int(resp.status_code or 0),
            "body": body[:300],
        }

    def _probe_single_account(self, account: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
        account_id = self._normalize_account_id(account.get("id"))
        if account_id is None:
            return {
                "id": None,
                "email": "",
                "probe_status": "error",
                "verification_status": "",
                "error": "账号 ID 无效",
                "token_updated": False,
                "refresh_synced": False,
                "update_status": 0,
            }

        identity = self._account_identity(account)
        email = identity.get("email") or str(account.get("name") or "").strip()
        credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
        access_token = str(credentials.get("access_token") or "").strip()
        refresh_token = str(credentials.get("refresh_token") or "").strip()
        fallback_account_id = str(
            credentials.get("chatgpt_account_id") or account_id
        ).strip()

        probe_result = probe_token_health(
            refresh_token,
            access_token=access_token,
            proxy=self.openai_proxy,
            max_tries=2,
            fallback_email=email,
            fallback_account_id=fallback_account_id,
        )
        probe_status = str(probe_result.get("status") or "error")
        verification_status = str(probe_result.get("verification_status") or "")
        token_updated = False
        refresh_synced = False
        update_status = 0

        if probe_status == "alive":
            token_data = probe_result.get("token_data")
            if isinstance(token_data, dict):
                update_result = self._update_account_tokens(
                    account_id,
                    email,
                    token_data,
                    timeout=max(15, timeout),
                )
                token_updated = bool(update_result.get("ok"))
                update_status = int(update_result.get("status") or 0)
                if not token_updated:
                    refresh_synced = self.refresh_account(account_id, timeout=timeout)
            else:
                refresh_synced = self.refresh_account(account_id, timeout=timeout)

        return {
            "id": account_id,
            "email": email,
            "probe_status": probe_status,
            "verification_status": verification_status,
            "error": str(probe_result.get("error") or ""),
            "token_updated": token_updated,
            "refresh_synced": refresh_synced,
            "update_status": update_status,
        }

    def _probe_accounts_parallel(
        self,
        accounts: List[Dict[str, Any]],
        timeout: int = 30,
        workers: int = 8,
    ) -> Dict[int, Dict[str, Any]]:
        valid_accounts = [
            account
            for account in accounts
            if self._normalize_account_id((account or {}).get("id")) is not None
        ]
        if not valid_accounts:
            return {}

        results: Dict[int, Dict[str, Any]] = {}
        pool_workers = max(1, min(workers, _PROBE_WORKERS, len(valid_accounts)))
        with ThreadPoolExecutor(max_workers=pool_workers) as executor:
            future_to_id = {
                executor.submit(self._probe_single_account, account, timeout=timeout): self._normalize_account_id(account.get("id"))
                for account in valid_accounts
            }
            for future in as_completed(future_to_id):
                account_id = future_to_id[future]
                if account_id is None:
                    continue
                try:
                    results[account_id] = future.result()
                except Exception as exc:
                    results[account_id] = {
                        "id": account_id,
                        "email": "",
                        "probe_status": "error",
                        "verification_status": "",
                        "error": str(exc),
                        "token_updated": False,
                        "refresh_synced": False,
                        "update_status": 0,
                    }
        return results

    def list_account_inventory(self, timeout: int = 15) -> Dict[str, Any]:
        all_accounts = self._list_all_accounts(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE)
        dedupe_plan = self._build_dedupe_plan(
            all_accounts,
            details_limit=max(1, len(all_accounts)),
        )
        candidate_count = self._count_candidate_accounts(all_accounts, dedupe_plan)
        duplicate_delete_ids = {
            int(account_id)
            for account_id in (dedupe_plan.get("delete_ids") or [])
            if isinstance(account_id, int)
        }
        duplicate_map: Dict[int, Dict[str, Any]] = {}
        for group in dedupe_plan.get("groups_preview") or []:
            keep_id = self._normalize_account_id(group.get("keep_id"))
            delete_ids = [
                account_id
                for account_id in (
                    self._normalize_account_id(item)
                    for item in (group.get("delete_ids") or [])
                )
                if account_id is not None
            ]
            group_ids = ([keep_id] if keep_id is not None else []) + delete_ids
            group_size = max(1, int(group.get("size") or len(group_ids) or 1))
            emails = [str(email).strip().lower() for email in (group.get("emails") or []) if str(email).strip()]
            for account_id in group_ids:
                duplicate_map[account_id] = {
                    "group_size": group_size,
                    "keep_id": keep_id,
                    "delete_candidate": account_id in duplicate_delete_ids,
                    "emails": emails,
                }

        items: List[Dict[str, Any]] = []
        abnormal_count = 0
        for raw_item in sorted(all_accounts, key=self._account_sort_key, reverse=True):
            account_id = self._normalize_account_id(raw_item.get("id"))
            if account_id is None:
                continue
            identity = self._account_identity(raw_item)
            status = str(raw_item.get("status") or "").strip().lower() or "unknown"
            if self._is_abnormal_status(status):
                abnormal_count += 1
            duplicate_info = duplicate_map.get(account_id) or {}
            items.append({
                "id": account_id,
                "name": str(raw_item.get("name") or "").strip(),
                "email": identity.get("email") or str(raw_item.get("name") or "").strip(),
                "status": status,
                "updated_at": raw_item.get("updated_at") or raw_item.get("updatedAt") or "",
                "created_at": raw_item.get("created_at") or raw_item.get("createdAt") or "",
                "is_duplicate": bool(duplicate_info),
                "duplicate_group_size": int(duplicate_info.get("group_size") or 0),
                "duplicate_keep": duplicate_info.get("keep_id") == account_id,
                "duplicate_delete_candidate": bool(duplicate_info.get("delete_candidate")),
                "duplicate_emails": duplicate_info.get("emails") or [],
            })

        return {
            "total": len(items),
            "candidate_count": candidate_count,
            "error_count": abnormal_count,
            "duplicate_groups": int(dedupe_plan.get("duplicate_groups", 0)),
            "duplicate_accounts": int(dedupe_plan.get("duplicate_accounts", 0)),
            "items": items,
        }

    def _refresh_accounts_parallel(self, account_ids: List[int], timeout: int = 30, workers: int = 8) -> Dict[str, List[int]]:
        success_ids: List[int] = []
        failed_ids: List[int] = []
        ids = [i for i in account_ids if isinstance(i, int) and i > 0]
        if not ids:
            return {"success_ids": success_ids, "failed_ids": failed_ids}

        pool_workers = max(1, min(workers, _PROBE_WORKERS, len(ids)))
        with ThreadPoolExecutor(max_workers=pool_workers) as executor:
            future_to_id = {
                executor.submit(self.refresh_account, account_id, timeout=timeout): account_id
                for account_id in ids
            }
            for future in as_completed(future_to_id):
                account_id = future_to_id[future]
                try:
                    ok = bool(future.result())
                except Exception:
                    ok = False
                if ok:
                    success_ids.append(account_id)
                else:
                    failed_ids.append(account_id)
        return {"success_ids": success_ids, "failed_ids": failed_ids}

    def _delete_accounts_parallel(self, account_ids: List[int], timeout: int = 15, workers: int = 12) -> Dict[str, Any]:
        deleted_ok_ids: List[int] = []
        failed_ids: List[int] = []
        unique_ids = sorted({i for i in account_ids if isinstance(i, int) and i > 0}, reverse=True)
        if not unique_ids:
            return {"deleted_ok": 0, "deleted_fail": 0, "deleted_ok_ids": deleted_ok_ids, "failed_ids": failed_ids}

        pool_workers = max(1, min(workers, _DELETE_WORKERS, len(unique_ids)))
        with ThreadPoolExecutor(max_workers=pool_workers) as executor:
            future_to_id = {
                executor.submit(self.delete_account, account_id, timeout=timeout): account_id
                for account_id in unique_ids
            }
            for future in as_completed(future_to_id):
                account_id = future_to_id[future]
                try:
                    ok = bool(future.result())
                except Exception:
                    ok = False
                if ok:
                    deleted_ok_ids.append(account_id)
                else:
                    failed_ids.append(account_id)

        return {
            "deleted_ok": len(deleted_ok_ids),
            "deleted_fail": len(failed_ids),
            "deleted_ok_ids": deleted_ok_ids,
            "failed_ids": failed_ids,
        }

    def dedupe_duplicate_accounts(self, timeout: int = 15, dry_run: bool = True, details_limit: int = 120) -> Dict[str, Any]:
        """
        清理 Sub2Api 中 OpenAI OAuth 重复账号（按 email 或 refresh_token 判重）。
        - 同一连通重复组保留“最新”账号（updated_at 优先，其次 id 最大）。
        - dry_run=True 时仅预览，不执行删除。
        """
        all_accounts = self._list_all_accounts(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE)
        dedupe_plan = self._build_dedupe_plan(all_accounts, details_limit=details_limit)
        delete_ids = dedupe_plan["delete_ids"]
        deleted_ok = 0
        deleted_fail = 0
        failed_ids: List[int] = []
        if not dry_run and delete_ids:
            delete_result = self._delete_accounts_parallel(
                delete_ids,
                timeout=timeout,
                workers=_DELETE_WORKERS,
            )
            deleted_ok = int(delete_result.get("deleted_ok", 0))
            deleted_fail = int(delete_result.get("deleted_fail", 0))
            failed_ids = list(delete_result.get("failed_ids") or [])

        return {
            "dry_run": dry_run,
            "total": len(all_accounts),
            "duplicate_groups": int(dedupe_plan["duplicate_groups"]),
            "duplicate_accounts": int(dedupe_plan["duplicate_accounts"]),
            "to_delete": len(delete_ids),
            "deleted_ok": deleted_ok,
            "deleted_fail": deleted_fail,
            "failed_delete_ids": failed_ids[:200],
            "groups_preview": dedupe_plan["groups_preview"],
            "truncated_groups": int(dedupe_plan["truncated_groups"]),
        }

    def probe_accounts(self, account_ids: List[int], timeout: int = 30) -> Dict[str, Any]:
        ids = sorted({
            account_id
            for account_id in (
                self._normalize_account_id(item)
                for item in (account_ids or [])
            )
            if account_id is not None
        })
        if not ids:
            return {
                "requested": 0,
                "refreshed_ok": 0,
                "refreshed_fail": 0,
                "recovered": 0,
                "still_abnormal": 0,
                "details": [],
            }

        before_items = self._list_account_items_by_ids(ids, timeout=timeout)
        before_status = {
            account_id: str((before_items.get(account_id) or {}).get("status") or "unknown").strip().lower()
            for account_id in ids
        }
        probe_details = self._probe_accounts_parallel(
            [before_items[account_id] for account_id in ids if account_id in before_items],
            timeout=max(30, timeout),
            workers=_PROBE_WORKERS,
        )

        if any(
            detail.get("probe_status") == "alive"
            for detail in probe_details.values()
        ):
            time.sleep(2)
        after_status = self._list_accounts_by_ids(ids, timeout=timeout)

        recovered_ids: List[int] = []
        abnormal_after_ids: List[int] = []
        details: List[Dict[str, Any]] = []
        alive_count = 0
        deleted_count = 0
        token_invalid_count = 0
        indeterminate_count = 0
        for account_id in ids:
            before = str(before_status.get(account_id) or "unknown").strip().lower()
            after = str(after_status.get(account_id) or before or "unknown").strip().lower()
            probe_detail = probe_details.get(account_id) or {}
            probe_status = str(probe_detail.get("probe_status") or "error")
            if probe_status == "alive":
                alive_count += 1
            elif probe_status == "deleted":
                deleted_count += 1
            elif probe_status == "token_invalid":
                token_invalid_count += 1
            else:
                indeterminate_count += 1
            if self._is_abnormal_status(before) and not self._is_abnormal_status(after):
                recovered_ids.append(account_id)
            if self._is_abnormal_status(after):
                abnormal_after_ids.append(account_id)
            if len(details) < 200:
                details.append({
                    "id": account_id,
                    "before_status": before,
                    "after_status": after,
                    "probe_status": probe_status,
                    "verification_status": str(probe_detail.get("verification_status") or ""),
                    "token_updated": bool(probe_detail.get("token_updated")),
                    "refresh_synced": bool(probe_detail.get("refresh_synced")),
                    "error": str(probe_detail.get("error") or ""),
                })

        return {
            "requested": len(ids),
            "refreshed_ok": alive_count,
            "refreshed_fail": deleted_count + token_invalid_count + indeterminate_count,
            "recovered": len(recovered_ids),
            "still_abnormal": len(abnormal_after_ids),
            "probe_alive": alive_count,
            "probe_deleted": deleted_count,
            "probe_token_invalid": token_invalid_count,
            "probe_indeterminate": indeterminate_count,
            "details": details,
        }

    def delete_accounts_batch(self, account_ids: List[int], timeout: int = 15) -> Dict[str, Any]:
        ids = [
            account_id
            for account_id in (
                self._normalize_account_id(item)
                for item in (account_ids or [])
            )
            if account_id is not None
        ]
        delete_result = self._delete_accounts_parallel(
            ids,
            timeout=timeout,
            workers=_DELETE_WORKERS,
        )
        return {
            "requested": len({*ids}),
            "deleted_ok": int(delete_result.get("deleted_ok", 0)),
            "deleted_fail": int(delete_result.get("deleted_fail", 0)),
            "deleted_ok_ids": list(delete_result.get("deleted_ok_ids") or []),
            "failed_ids": list(delete_result.get("failed_ids") or []),
        }

    def handle_exception_accounts(
        self,
        account_ids: Optional[List[int]] = None,
        timeout: int = 30,
        delete_unresolved: bool = True,
    ) -> Dict[str, Any]:
        requested_ids = [
            account_id
            for account_id in (
                self._normalize_account_id(item)
                for item in (account_ids or [])
            )
            if account_id is not None
        ]

        if requested_ids:
            current_status = self._list_accounts_by_ids(requested_ids, timeout=timeout)
            target_ids = [
                account_id
                for account_id in requested_ids
                if self._is_abnormal_status(current_status.get(account_id))
            ]
            skipped_non_abnormal = max(0, len(set(requested_ids)) - len(target_ids))
        else:
            all_accounts = self._list_all_accounts(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE)
            target_ids = [
                account_id
                for account_id in (
                    self._normalize_account_id(item.get("id"))
                    for item in all_accounts
                    if self._is_abnormal_status(item.get("status"))
                )
                if account_id is not None
            ]
            skipped_non_abnormal = 0

        unique_target_ids = sorted(set(target_ids))
        if not unique_target_ids:
            return {
                "requested": len(set(requested_ids)) if requested_ids else 0,
                "targeted": 0,
                "refreshed_ok": 0,
                "refreshed_fail": 0,
                "recovered": 0,
                "remaining_abnormal": 0,
                "deleted_ok": 0,
                "deleted_fail": 0,
                "skipped_non_abnormal": skipped_non_abnormal,
            }

        target_items = self._list_account_items_by_ids(unique_target_ids, timeout=timeout)
        probe_details = self._probe_accounts_parallel(
            [target_items[account_id] for account_id in unique_target_ids if account_id in target_items],
            timeout=max(30, timeout),
            workers=_PROBE_WORKERS,
        )
        if any(
            detail.get("probe_status") == "alive"
            for detail in probe_details.values()
        ):
            time.sleep(2)
        after_status = self._list_accounts_by_ids(unique_target_ids, timeout=timeout)
        delete_candidates = [
            account_id
            for account_id in unique_target_ids
            if str((probe_details.get(account_id) or {}).get("probe_status") or "") in ("deleted", "token_invalid")
        ]
        remaining_abnormal_ids = [
            account_id
            for account_id in unique_target_ids
            if self._is_abnormal_status(after_status.get(account_id))
            and account_id not in set(delete_candidates)
        ]
        delete_candidate_set = set(delete_candidates)
        remaining_abnormal_set = set(remaining_abnormal_ids)
        recovered_ids = [
            account_id
            for account_id in unique_target_ids
            if account_id not in remaining_abnormal_set
            and account_id not in delete_candidate_set
        ]
        alive_count = sum(
            1
            for detail in probe_details.values()
            if str(detail.get("probe_status") or "") == "alive"
        )
        deleted_count = sum(
            1
            for detail in probe_details.values()
            if str(detail.get("probe_status") or "") == "deleted"
        )
        token_invalid_count = sum(
            1
            for detail in probe_details.values()
            if str(detail.get("probe_status") or "") == "token_invalid"
        )
        indeterminate_count = max(
            0,
            len(probe_details) - alive_count - deleted_count - token_invalid_count,
        )

        delete_result = {
            "deleted_ok": 0,
            "deleted_fail": 0,
            "deleted_ok_ids": [],
            "failed_ids": [],
        }
        if delete_unresolved and delete_candidates:
            delete_result = self._delete_accounts_parallel(
                delete_candidates,
                timeout=timeout,
                workers=_DELETE_WORKERS,
            )

        return {
            "requested": len(set(requested_ids)) if requested_ids else len(unique_target_ids),
            "targeted": len(unique_target_ids),
            "refreshed_ok": alive_count,
            "refreshed_fail": deleted_count + token_invalid_count + indeterminate_count,
            "recovered": len(recovered_ids),
            "remaining_abnormal": len(remaining_abnormal_ids),
            "deleted_ok": int(delete_result.get("deleted_ok", 0)),
            "deleted_fail": int(delete_result.get("deleted_fail", 0)),
            "deleted_ok_ids": list(delete_result.get("deleted_ok_ids") or []),
            "failed_ids": list(delete_result.get("failed_ids") or []),
            "skipped_non_abnormal": skipped_non_abnormal,
            "probe_alive": alive_count,
            "probe_deleted": deleted_count,
            "probe_token_invalid": token_invalid_count,
            "probe_indeterminate": indeterminate_count,
        }

    def refresh_account(self, account_id: int, timeout: int = 30) -> bool:
        try:
            resp = self._request(
                "POST", f"/api/v1/admin/accounts/{account_id}/refresh",
                timeout=timeout,
            )
            return resp.status_code in (200, 201)
        except Exception:
            return False

    def delete_account(self, account_id: int, timeout: int = 15) -> bool:
        try:
            resp = self._request(
                "DELETE", f"/api/v1/admin/accounts/{account_id}",
                timeout=timeout,
            )
            return resp.status_code in (200, 204)
        except Exception:
            return False

    def get_pool_status(self, timeout: int = 15) -> Dict[str, Any]:
        try:
            all_accounts = self._list_all_accounts(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE)
            dedupe_plan = self._build_dedupe_plan(all_accounts, details_limit=1)
            return self._build_pool_status_snapshot(all_accounts, dedupe_plan)
        except Exception as e:
            return {
                "total": 0, "candidates": 0, "error_count": 0,
                "threshold": self.min_candidates, "healthy": False,
                "percent": 0, "last_checked": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": str(e),
            }

    def test_connection(self, timeout: int = 10) -> Dict[str, Any]:
        try:
            status = self.get_pool_status(timeout)
            total = int(status.get("total", 0))
            normal = int(status.get("candidates", 0))
            error = int(status.get("error_count", 0))
            return {
                "ok": True,
                "total": total,
                "normal": normal,
                "error": error,
                "message": f"连接成功，共 {total} 个账号，{normal} 正常，{error} 异常",
            }
        except Exception as e:
            return {"ok": False, "total": 0, "normal": 0, "error": 0,
                    "message": f"连接失败: {e}"}

    def _list_accounts_by_ids(
        self, ids: List[int], timeout: int = 15,
    ) -> Dict[int, str]:
        """查询指定 ID 的账号当前状态，返回 {id: status}"""
        result: Dict[int, str] = {}
        id_set = set(ids)
        for _, _, _, items in self._iter_account_pages(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE):
            for item in items:
                aid = item.get("id")
                if aid in id_set:
                    result[aid] = str(item.get("status", ""))
                    id_set.discard(aid)
            if not id_set:
                break
        return result

    def probe_and_clean_sync(
        self,
        timeout: int = 15,
        actions: Optional[Dict[str, bool]] = None,
        before_status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        action_flags = {
            "refresh_abnormal_accounts": bool((actions or {}).get("refresh_abnormal_accounts", True)),
            "delete_abnormal_accounts": bool((actions or {}).get("delete_abnormal_accounts", True)),
            "dedupe_duplicate_accounts": bool((actions or {}).get("dedupe_duplicate_accounts", True)),
        }
        started = time.time()
        all_accounts = self._list_all_accounts(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE)
        before_dedupe_plan = self._build_dedupe_plan(all_accounts, details_limit=1)
        before_status = self._build_pool_status_snapshot(all_accounts, before_dedupe_plan)
        if before_status_callback is not None:
            before_status_callback(before_status)

        error_accounts = [
            account for account in all_accounts
            if self._is_abnormal_status(account.get("status"))
        ]

        error_ids = [
            self._normalize_account_id(acc.get("id"))
            for acc in error_accounts
        ]
        error_ids = [i for i in error_ids if i is not None]
        initial_error_ids = set(error_ids)
        probe_details: Dict[int, Dict[str, Any]] = {}
        if action_flags["refresh_abnormal_accounts"] and error_accounts:
            probe_details = self._probe_accounts_parallel(
                error_accounts,
                timeout=max(30, timeout),
                workers=_PROBE_WORKERS,
            )

        current_accounts = all_accounts
        current_error_ids = set(initial_error_ids)
        alive_probe_count = sum(
            1
            for detail in probe_details.values()
            if str(detail.get("probe_status") or "") == "alive"
        )
        requires_account_reload = action_flags["refresh_abnormal_accounts"] and alive_probe_count > 0
        if requires_account_reload:
            time.sleep(2)
        if requires_account_reload:
            current_accounts = self._list_all_accounts(timeout=timeout, page_size=_ACCOUNT_PAGE_SIZE)
            current_error_ids = {
                int(acc_id) for acc_id in (
                    self._normalize_account_id(account.get("id"))
                    for account in current_accounts
                    if self._is_abnormal_status(account.get("status"))
                ) if isinstance(acc_id, int)
            }
        recovered = len(initial_error_ids - current_error_ids)
        deleted_probe_ids = {
            int(account_id)
            for account_id, detail in probe_details.items()
            if str(detail.get("probe_status") or "") == "deleted"
        }
        token_invalid_probe_ids = {
            int(account_id)
            for account_id, detail in probe_details.items()
            if str(detail.get("probe_status") or "") == "token_invalid"
        }
        deterministic_dead_ids = deleted_probe_ids | token_invalid_probe_ids
        indeterminate_probe_count = max(
            0,
            len(probe_details) - alive_probe_count - len(deterministic_dead_ids),
        )

        current_dedupe_plan = self._build_dedupe_plan(
            current_accounts,
            details_limit=120 if action_flags["dedupe_duplicate_accounts"] else 1,
        )
        duplicate_delete_ids: List[int] = []
        if action_flags["dedupe_duplicate_accounts"]:
            duplicate_delete_ids = [int(i) for i in current_dedupe_plan["delete_ids"] if isinstance(i, int)]
        normal_count = self._count_candidate_accounts(current_accounts, current_dedupe_plan)

        delete_targets: set[int] = set()
        if action_flags["delete_abnormal_accounts"]:
            delete_targets.update(deterministic_dead_ids)
        if action_flags["dedupe_duplicate_accounts"]:
            delete_targets.update(duplicate_delete_ids)
        delete_result = self._delete_accounts_parallel(
            sorted(delete_targets, reverse=True),
            timeout=timeout,
            workers=_DELETE_WORKERS,
        )
        deleted_ok = int(delete_result.get("deleted_ok", 0))
        deleted_fail = int(delete_result.get("deleted_fail", 0))
        deleted_ok_ids = set(int(i) for i in (delete_result.get("deleted_ok_ids") or []) if isinstance(i, int))

        remaining_accounts = [
            account
            for account in current_accounts
            if self._normalize_account_id(account.get("id")) not in deleted_ok_ids
        ]
        after_dedupe_plan = self._build_dedupe_plan(remaining_accounts, details_limit=1)
        after_status = self._build_pool_status_snapshot(remaining_accounts, after_dedupe_plan)

        deleted_from_error = len(deleted_ok_ids & deterministic_dead_ids)
        deleted_from_duplicate = len(deleted_ok_ids & set(duplicate_delete_ids))

        elapsed_ms = int((time.time() - started) * 1000)

        return {
            "actions": action_flags,
            "total": len(current_accounts), "normal": normal_count,
            "initial_error_count": len(initial_error_ids),
            "error_count": len(current_error_ids), "refreshed": recovered,
            "refresh_attempted": len(error_ids) if action_flags["refresh_abnormal_accounts"] else 0,
            "refresh_failed": len(deterministic_dead_ids) + indeterminate_probe_count,
            "deleted_ok": deleted_ok, "deleted_fail": deleted_fail,
            "duplicate_groups": int(current_dedupe_plan["duplicate_groups"]),
            "duplicate_accounts": int(current_dedupe_plan["duplicate_accounts"]),
            "duplicate_to_delete": len(duplicate_delete_ids),
            "deleted_from_error": deleted_from_error,
            "deleted_from_duplicate": deleted_from_duplicate,
            "probe_alive": alive_probe_count,
            "probe_deleted": len(deleted_probe_ids),
            "probe_token_invalid": len(token_invalid_probe_ids),
            "probe_indeterminate": indeterminate_probe_count,
            "duration_ms": elapsed_ms,
            "before_status": before_status,
            "after_status": after_status,
        }

    def calculate_gap(self, current_candidates: Optional[int] = None) -> int:
        if current_candidates is None:
            status = self.get_pool_status()
            if status.get("error"):
                raise RuntimeError(f"Sub2Api 池状态查询失败: {status['error']}")
            current_candidates = status["candidates"]
        return max(0, self.min_candidates - current_candidates)
