import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

SETTINGS_FILE = APP_DIR / "settings.json"
SKIP_FILE = APP_DIR / "skip.txt"
LOG_FILE = APP_DIR / "debug.log"
WEB_DIR = APP_DIR / "web"

BASE_URL = "https://openapi.chzzk.naver.com"
PAGE_SIZE = 20
HARD_PAGE_LIMIT = 5000
SESSION = requests.Session()

FOLLOWER_FETCH_CAP = 5000
LIVE_REFRESH_CAP = 300
SITE_LIVE_DETAIL = "https://api.chzzk.naver.com/service/v3/channels/{}/live-detail"
SITE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ChzzkLook"}

ParamsType = Any

logger = logging.getLogger("chzzk_look")
logger.setLevel(logging.INFO)
logger.handlers.clear()
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
if sys.stderr is not None:
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    logger.addHandler(_ch)

_file_handler: Optional[RotatingFileHandler] = None


def set_file_logging(enabled: bool) -> None:
    global _file_handler
    if enabled and _file_handler is None:
        _file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        _file_handler.setFormatter(_fmt)
        logger.addHandler(_file_handler)
        logger.info("debug.log 기록 켜짐")
    elif not enabled and _file_handler is not None:
        logger.info("debug.log 기록 꺼짐")
        logger.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None


def file_logging_enabled() -> bool:
    return _file_handler is not None


def mask_secret(s: str) -> str:
    if not s:
        return ""
    return "****" if len(s) <= 4 else s[:2] + "****" + s[-2:]


def to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@dataclass
class LiveItem:
    live_id: int
    channel_id: str
    channel_name: str
    live_title: str
    open_date_raw: str
    viewers: int
    url: str
    category_value: str
    category_type: str
    live_category: str
    thumbnail_url: str = ""
    channel_image_url: str = ""
    tags: List[str] = field(default_factory=list)
    adult: bool = False


@dataclass
class Hit:
    item: LiveItem
    followers: Optional[int]
    uptime: Optional[float]


@dataclass
class SearchParams:
    client_id: str
    client_secret: str
    category: str
    max_scan: int
    follower_enabled: bool
    min_followers: int
    max_followers: int
    viewer_enabled: bool
    min_viewers: int
    max_viewers: int
    uptime_enabled: bool
    max_hours: float
    skip_text: str = ""
    tags: List[str] = field(default_factory=list)
    tag_mode: str = "all"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Client-Secret": self.client_secret,
            "Content-Type": "application/json",
        }

    def condition_text(self) -> str:
        parts = [f"카테고리={self.category or '전체'}"]
        parts.append(f"팔로워 {self.min_followers}~{self.max_followers}" if self.follower_enabled else "팔로워 조건 없음")
        parts.append(f"시청자 {self.min_viewers}~{self.max_viewers}" if self.viewer_enabled else "시청자 조건 없음")
        parts.append(f"업타임<={self.max_hours:g}h" if self.uptime_enabled else "업타임 조건 없음")
        if self.tags:
            joiner = " & " if self.tag_mode == "all" else " | "
            parts.append("태그=" + joiner.join(self.tags))
        return ", ".join(parts)


@dataclass
class SearchResult:
    hits: List[Hit] = field(default_factory=list)
    condition: str = ""
    scanned: int = 0
    cancelled: bool = False


_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def extract_channel_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"chzzk\.naver\.com/(?:live/)?([0-9a-fA-F]{32})", url.strip())
    return m.group(1).lower() if m else None


def parse_skip_text(text: str) -> Tuple[Set[str], Set[str]]:
    url_set: Set[str] = set()
    channel_set: Set[str] = set()

    def flush(u: str):
        u = u.replace(" ", "")
        if not u:
            return
        url_set.add(u)
        cid = extract_channel_id_from_url(u)
        if cid:
            channel_set.add(cid)

    cur = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _HEX32.match(line):
            flush(cur)
            cur = ""
            channel_set.add(line.lower())
        elif line.startswith(("http://", "https://", "chzzk.naver.com")):
            flush(cur)
            cur = line
        elif cur:
            cur += line
    flush(cur)
    return url_set, channel_set


def uptime_hours(open_date_raw: str) -> Optional[float]:
    s = (open_date_raw or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        logger.warning(f"openDate 파싱 실패: {open_date_raw!r}")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600.0)


def api_get(path: str, headers: Dict[str, str], params: ParamsType, timeout: int = 15, retries: int = 4) -> dict:
    url = f"{BASE_URL}{path}"
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, headers=headers, params=params, timeout=timeout)
        except requests.RequestException as e:
            logger.error(f"네트워크 오류: {e}")
            if attempt == retries:
                raise RuntimeError(f"네트워크 오류: {e}") from e
            time.sleep(0.7 * (attempt + 1))
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                logger.error(f"JSON 파싱 실패: {resp.text[:400]}")
                raise RuntimeError("API 응답이 JSON이 아닙니다.")
            if "content" not in data:
                logger.error(f"예상치 못한 응답: {str(data)[:500]}")
                raise RuntimeError(f"예상치 못한 응답: {str(data)[:300]}")
            return data["content"] or {}

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            logger.warning(f"API 임시 오류 {resp.status_code}, 재시도 {attempt + 1}/{retries}")
            time.sleep(0.8 * (attempt + 1))
            continue

        logger.error(f"API 오류 {resp.status_code}: {resp.text[:300]}")
        if resp.status_code in (401, 403):
            raise RuntimeError(f"인증 실패({resp.status_code}). Client ID / Secret을 확인해 주세요.")
        raise RuntimeError(f"API 오류 {resp.status_code}: {resp.text[:300]}")

    raise RuntimeError("도달 불가")


def is_all_category(text: str) -> bool:
    return (text or "").strip().lower() in ("", "전체", "all", "*")


_TYPE_ALIASES = {"게임": "GAME", "game": "GAME", "스포츠": "SPORTS", "sports": "SPORTS", "기타": "ETC", "etc": "ETC"}


def matches_category(user_input: str, category_type: str, live_category: str, category_value: str) -> bool:
    terms = [t.strip() for t in (user_input or "").split(",") if t.strip()]
    if not terms or any(is_all_category(t) for t in terms):
        return True
    return any(_match_one_category(t, category_type, live_category, category_value) for t in terms)


def split_terms(text: str) -> List[str]:
    return list(dict.fromkeys(t for t in re.split(r"[,#\s]+", (text or "").strip().lower()) if t))


def matches_tags(tags: List[str], terms: List[str], mode: str = "all") -> bool:
    if not terms:
        return True
    low = [t.lower().lstrip("#") for t in tags]
    hit = [any(term in tag for tag in low) for term in terms]
    return all(hit) if mode == "all" else any(hit)


def _match_one_category(u: str, category_type: str, live_category: str, category_value: str) -> bool:
    u = u.strip().lower()
    ct = (category_type or "").strip().upper()
    lc = (live_category or "").strip().lower()
    cv = (category_value or "").strip().lower()

    if u in ("토크", "talk", "etc/talk", "etc\\talk"):
        return (ct == "ETC" and lc == "talk") or ("토크" in cv) or ("talk" in cv)
    if u in _TYPE_ALIASES:
        return ct == _TYPE_ALIASES[u]
    if "/" in u:
        left, right = (p.strip() for p in u.split("/", 1))
        if left and right:
            return ct == left.upper() and lc == right
    return (u in cv) or (u in lc)


def _parse_live(x: dict) -> Optional[LiveItem]:
    channel_id = str(x.get("channelId") or "").strip().lower().replace(" ", "")
    live_id = to_int(x.get("liveId"))
    open_date = str(x.get("openDate") or "")
    if not channel_id or not open_date or live_id == 0:
        return None
    thumb = str(
        x.get("liveImageUrl") or x.get("liveThumbnailImageUrl") or x.get("liveImageUrlSmall")
        or x.get("thumbnailImageUrl") or x.get("defaultThumbnailImageUrl") or ""
    ).strip().replace("{type}", "480")
    return LiveItem(
        live_id=live_id,
        channel_id=channel_id,
        channel_name=str(x.get("channelName") or ""),
        live_title=str(x.get("liveTitle") or ""),
        open_date_raw=open_date,
        viewers=to_int(x.get("concurrentUserCount")),
        url=f"https://chzzk.naver.com/live/{channel_id}",
        category_value=str(x.get("liveCategoryValue") or "").strip(),
        category_type=str(x.get("categoryType") or "").strip().upper(),
        live_category=str(x.get("liveCategory") or "").strip().lower(),
        thumbnail_url=thumb,
        channel_image_url=str(x.get("channelImageUrl") or "").strip(),
        tags=[str(t) for t in (x.get("tags") or []) if t],
        adult=bool(x.get("adult")),
    )


ProgressFn = Callable[[int, int], None]


def fetch_lives(
    headers: Dict[str, str],
    max_scan: int,
    category_input: str,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> Tuple[List[LiveItem], int, bool]:
    lives: List[LiveItem] = []
    seen: Set[int] = set()
    cat_counter: Dict[str, int] = {}
    next_token: Optional[str] = None
    scanned = pages = empty_pages = 0
    cancelled = False

    while scanned < max_scan and pages < HARD_PAGE_LIMIT:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        params: Dict[str, Any] = {"size": min(PAGE_SIZE, max_scan - scanned)}
        if next_token:
            params["next"] = next_token

        content = api_get("/open/v1/lives", headers=headers, params=params)
        data_list = content.get("data") or []
        next_token = (content.get("page") or {}).get("next")
        pages += 1

        if not isinstance(data_list, list):
            logger.error(f"data가 list가 아님: {type(data_list)}")
            break

        empty_pages = empty_pages + 1 if not data_list else 0
        if empty_pages >= 2:
            logger.info("연속 빈 페이지라 중단")
            break

        for x in data_list:
            scanned += 1
            it = _parse_live(x)
            if it is None or it.live_id in seen:
                continue
            seen.add(it.live_id)
            key = f"{it.category_type}/{it.live_category or 'unknown'}"
            cat_counter[key] = cat_counter.get(key, 0) + 1
            if matches_category(category_input, it.category_type, it.live_category, it.category_value):
                lives.append(it)

        if progress:
            progress(scanned, len(lives))
        if not next_token:
            break

    top = sorted(cat_counter.items(), key=lambda kv: kv[1], reverse=True)[:8]
    logger.info(f"페이지 {pages}, 스캔 {scanned}, 일치 {len(lives)}, 카테고리 TOP: {top}")
    return lives, scanned, cancelled


def fetch_category_values(headers: Dict[str, str], max_scan: int = 400) -> List[str]:
    lives, _, _ = fetch_lives(headers, max_scan, "전체")
    return sorted({it.category_value for it in lives if it.category_value})


def fetch_channel_info(headers: Dict[str, str], channel_ids: List[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for i in range(0, len(channel_ids), 20):
        params: List[Tuple[str, str]] = [("channelIds", cid) for cid in channel_ids[i:i + 20]]
        content = api_get("/open/v1/channels", headers=headers, params=params)
        for x in content.get("data") or []:
            cid = str(x.get("channelId") or "").strip().lower()
            if cid:
                out[cid] = x
    return out


def _fetch_one_live_detail(channel_id: str) -> Optional[dict]:
    try:
        resp = requests.get(SITE_LIVE_DETAIL.format(channel_id), headers=SITE_HEADERS, timeout=6)
        if resp.status_code != 200:
            return None
        content = resp.json().get("content")
        if not content:
            return {"viewers": None, "status": "CLOSE"}
        v = content.get("concurrentUserCount")
        return {"viewers": None if v is None else to_int(v), "status": str(content.get("status") or "")}
    except (requests.RequestException, ValueError):
        return None


def fetch_live_details(channel_ids: List[str], cancel: Optional[threading.Event] = None) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch_one_live_detail, cid): cid for cid in dict.fromkeys(channel_ids)}
        for fut in as_completed(futures):
            if cancel is not None and cancel.is_set():
                break
            r = fut.result()
            if r is not None:
                out[futures[fut]] = r
    return out


def run_search(
    p: SearchParams,
    status: Callable[[str], None],
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> SearchResult:
    logger.info("=" * 40)
    logger.info(f"검색 시작 ClientId={p.client_id} Secret={mask_secret(p.client_secret)}")
    logger.info(f"조건: {p.condition_text()}, max_scan={p.max_scan}")

    skip_urls, skip_channels = parse_skip_text(p.skip_text)
    status(f"라이브 목록 스캔 중 (카테고리: {p.category or '전체'})...")
    lives, scanned, cancelled = fetch_lives(p.headers, p.max_scan, p.category, progress, cancel)

    lives = [x for x in lives if x.url not in skip_urls and x.channel_id not in skip_channels]
    logger.info(f"스킵 적용 후: {len(lives)}개 (스킵 채널 {len(skip_channels)}개)")

    hits: List[Hit] = []
    for it in lives:
        if p.tags and not matches_tags(it.tags, p.tags, p.tag_mode):
            continue
        if p.viewer_enabled and not (p.min_viewers <= it.viewers <= p.max_viewers):
            continue
        up = uptime_hours(it.open_date_raw)
        if p.uptime_enabled and (up is None or up > p.max_hours):
            continue
        hits.append(Hit(it, None, up))

    if hits and (p.follower_enabled or len(hits) <= FOLLOWER_FETCH_CAP):
        status(f"팔로워 수 불러오는 중... ({len(hits)}개)")
        channel_map = fetch_channel_info(p.headers, sorted({h.item.channel_id for h in hits}))
        kept: List[Hit] = []
        for h in hits:
            info = channel_map.get(h.item.channel_id)
            if info:
                h.followers = to_int(info.get("followerCount"))
                if not h.item.channel_image_url:
                    h.item.channel_image_url = str(info.get("channelImageUrl") or "").strip()
            if p.follower_enabled and (h.followers is None or not (p.min_followers <= h.followers <= p.max_followers)):
                continue
            kept.append(h)
        hits = kept

    if hits and len(hits) <= LIVE_REFRESH_CAP and not (cancel is not None and cancel.is_set()):
        status(f"시청자 수 최신화 중... ({len(hits)}개)")
        details = fetch_live_details([h.item.channel_id for h in hits], cancel)
        kept = []
        changed = closed = 0
        for h in hits:
            d = details.get(h.item.channel_id)
            if d is not None:
                if d["status"] == "CLOSE":
                    closed += 1
                    continue
                if d["viewers"] is not None and d["viewers"] != h.item.viewers:
                    changed += 1
                    h.item.viewers = d["viewers"]
            if p.viewer_enabled and not (p.min_viewers <= h.item.viewers <= p.max_viewers):
                continue
            kept.append(h)
        logger.info(f"시청자 최신화: 확인 {len(details)}/{len(hits)}, 값 변경 {changed}, 종료 {closed}, 남음 {len(kept)}")
        hits = kept

    hits.sort(key=lambda h: (h.item.viewers, h.followers if h.followers is not None else 10**12, h.uptime or 0))
    logger.info(f"최종 통과: {len(hits)}개 (중지={cancelled})")
    return SearchResult(hits, p.condition_text(), scanned, cancelled)


def read_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(data: dict) -> None:
    tmp = str(SETTINGS_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def read_skip_file() -> str:
    try:
        return SKIP_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as e:
        logger.error(f"skip.txt 읽기 실패: {e}")
        return ""


def write_skip_file(text: str) -> None:
    body = "\n".join(line.rstrip() for line in text.strip().splitlines())
    tmp = str(SKIP_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(body + ("\n" if body else ""))
    os.replace(tmp, SKIP_FILE)


def merge_skip_texts(base: str, extra: str) -> str:
    _, have = parse_skip_text(base)
    add: List[str] = []
    for line in extra.splitlines():
        s = line.strip()
        if not s:
            continue
        cid = extract_channel_id_from_url(s) or (s.lower() if _HEX32.match(s) else None)
        if cid and cid not in have:
            have.add(cid)
            add.append(s)
    return "\n".join([base.strip()] + add).strip() if add else base.strip()


def hit_to_dict(h: Hit) -> dict:
    it = h.item
    return {
        "liveId": it.live_id,
        "channelId": it.channel_id,
        "channelName": it.channel_name,
        "channelImage": it.channel_image_url,
        "title": it.live_title,
        "thumbnail": it.thumbnail_url,
        "url": it.url,
        "viewers": it.viewers,
        "followers": h.followers,
        "uptime": None if h.uptime is None else round(h.uptime, 2),
        "category": it.category_value or f"{it.category_type}/{it.live_category}",
        "tags": it.tags,
        "adult": it.adult,
    }
