import ctypes
import json
import sys
import threading
import time
import traceback
import webbrowser
from typing import Any, Dict, List, Optional

import webview

from chzzk_core import (
    WEB_DIR, SearchParams, fetch_category_values, split_terms, hit_to_dict, logger, merge_skip_texts,
    set_file_logging,
    read_settings, read_skip_file, run_search, write_settings, write_skip_file,
)

APP_TITLE = "Chzzk Look"
DEFAULTS: Dict[str, Any] = {
    "client_id": "", "client_secret": "", "save_secret": False,
    "category": "토크", "max_total": 10000,
    "follower_enabled": True, "min_followers": 0, "max_followers": 10,
    "viewer_enabled": True, "min_viewers": 0, "max_viewers": 1,
    "uptime_enabled": False, "max_uptime_hours": 5.0,
    "tags": "", "tag_mode": "all", "debug_log": True,
    "skip_urls_text": "", "sort": "viewers", "view": "grid",
}
ALLOWED_URL_PREFIX = "https://chzzk.naver.com/"


def _num(data: dict, key: str, label: str, cast: Any = int, minimum: float = 0):
    raw = data.get(key)
    try:
        v = cast(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"'{label}' 값이 올바르지 않습니다: {raw if raw not in (None, '') else '(비어 있음)'}")
    if v < minimum:
        raise ValueError(f"'{label}' 값은 {minimum:g} 이상이어야 합니다.")
    return v


def build_params(d: dict) -> SearchParams:
    cid, secret = str(d.get("client_id", "")).strip(), str(d.get("client_secret", "")).strip()
    if not cid or not secret:
        raise ValueError("Client ID / Client Secret을 입력해 주세요.")
    fol, vie, upt = bool(d.get("follower_enabled")), bool(d.get("viewer_enabled")), bool(d.get("uptime_enabled"))
    p = SearchParams(
        client_id=cid, client_secret=secret,
        category=str(d.get("category", "")).strip(),
        max_scan=_num(d, "max_total", "최대 스캔", minimum=1),
        follower_enabled=fol,
        min_followers=_num(d, "min_followers", "팔로워 최소") if fol else 0,
        max_followers=_num(d, "max_followers", "팔로워 최대") if fol else 0,
        viewer_enabled=vie,
        min_viewers=_num(d, "min_viewers", "시청자 최소") if vie else 0,
        max_viewers=_num(d, "max_viewers", "시청자 최대") if vie else 0,
        uptime_enabled=upt,
        max_hours=_num(d, "max_uptime_hours", "업타임", float) if upt else 0.0,
        skip_text=str(d.get("skip_urls_text", "")),
        tags=split_terms(str(d.get("tags", ""))),
        tag_mode="any" if d.get("tag_mode") == "any" else "all",
    )
    if len(p.tags) > 10 or any(len(t) > 30 for t in p.tags):
        raise ValueError("태그는 10개까지, 각 30자 이하로 입력해 주세요.")
    if fol and p.min_followers > p.max_followers:
        raise ValueError("팔로워 최소값이 최대값보다 큽니다.")
    if vie and p.min_viewers > p.max_viewers:
        raise ValueError("시청자 최소값이 최대값보다 큽니다.")
    return p


class Api:

    def __init__(self):
        self._window: Optional[webview.Window] = None
        self._cancel = threading.Event()
        self._search_lock = threading.Lock()
        self._skip_last: Optional[str] = None

    def get_settings(self) -> dict:
        saved = read_settings()
        s = {**DEFAULTS, **saved}
        if not s.get("save_secret"):
            s["client_secret"] = ""

        skip_file = read_skip_file()
        legacy = str(saved.get("skip_urls_text") or "")
        merged = merge_skip_texts(skip_file, legacy)
        try:
            if merged != skip_file.strip():
                write_skip_file(merged)
                logger.info("settings.json의 패스 목록을 skip.txt로 합침")
            if "skip_urls_text" in saved:
                saved.pop("skip_urls_text")
                write_settings(saved)
        except OSError as e:
            logger.error(f"패스 목록 이전 실패: {e}")
        self._skip_last = merged
        s["skip_urls_text"] = merged
        return s

    def save_settings(self, data: dict) -> bool:
        if not isinstance(data, dict):
            return False
        clean = {k: data.get(k, v) for k, v in DEFAULTS.items() if k != "skip_urls_text"}
        if not clean.get("save_secret"):
            clean["client_secret"] = ""
        skip = str(data.get("skip_urls_text", "")).strip()
        try:
            write_settings(clean)
            set_file_logging(bool(clean.get("debug_log")))
            if skip != self._skip_last:
                write_skip_file(skip)
                self._skip_last = skip
            return True
        except OSError as e:
            logger.error(f"세팅 저장 실패: {e}")
            return False

    def search(self, data: dict) -> dict:
        try:
            params = build_params(data or {})
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not self._search_lock.acquire(blocking=False):
            return {"ok": False, "error": "이미 검색 중입니다."}
        self._cancel.clear()
        last = [0.0]

        def push(kind: str, payload: Any, force: bool = False):
            now = time.monotonic()
            if not force and now - last[0] < 0.08:
                return
            last[0] = now
            if self._window:
                self._window.evaluate_js(f"window.ChzzkLook && ChzzkLook.on{kind}({json.dumps(payload, ensure_ascii=False)})")

        try:
            res = run_search(
                params,
                status=lambda m: push("Status", m, force=True),
                progress=lambda s, m: push("Progress", {"scanned": s, "matched": m}),
                cancel=self._cancel,
            )
            return {
                "ok": True, "hits": [hit_to_dict(h) for h in res.hits], "condition": res.condition,
                "scanned": res.scanned, "cancelled": res.cancelled,
                "followerEnabled": params.follower_enabled,
            }
        except Exception as e:
            logger.error(f"검색 실패: {e}\n{traceback.format_exc()}")
            return {"ok": False, "error": str(e)}
        finally:
            self._search_lock.release()

    def stop(self) -> bool:
        self._cancel.set()
        return True

    def refresh_categories(self, client_id: str, client_secret: str) -> dict:
        cid, secret = str(client_id or "").strip(), str(client_secret or "").strip()
        if not cid or not secret:
            return {"ok": False, "error": "Client ID / Client Secret을 입력해 주세요."}
        try:
            headers = {"Client-Id": cid, "Client-Secret": secret, "Content-Type": "application/json"}
            return {"ok": True, "categories": fetch_category_values(headers)}
        except Exception as e:
            logger.error(f"카테고리 수집 실패: {e}")
            return {"ok": False, "error": str(e)}

    def open_urls(self, urls: List[str]) -> int:
        opened = 0
        for u in (urls or [])[:20]:
            if isinstance(u, str) and u.startswith(ALLOWED_URL_PREFIX):
                webbrowser.open(u)
                opened += 1
        return opened


def _dark_title_bar():
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            val = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
    except Exception:
        pass


def main():
    set_file_logging(bool({**DEFAULTS, **read_settings()}.get("debug_log")))
    logger.info("Chzzk Look 시작")
    api = Api()
    window = webview.create_window(
        APP_TITLE, str(WEB_DIR / "index.html"), js_api=api,
        width=1360, height=880, min_size=(980, 640), background_color="#0a0b0e", text_select=True,
    )
    api._window = window
    window.events.shown += _dark_title_bar
    window.events.closing += api.stop
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
