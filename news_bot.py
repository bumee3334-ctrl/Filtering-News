#!/usr/bin/env python3
"""뉴스 스크리닝 -> 텔레그램 발송 봇 (표준 라이브러리만 사용, 구글 뉴스 RSS 사용)

동작 방식
  1) 실행될 때마다(5분 간격) 구글 뉴스 RSS로 키워드별 최근 기사를 조회 (API 키 불필요)
  2) 아직 보내지 않은 새 기사는 "대기열(pending)"에 쌓음
  3) 발송 시간대(기본 07:00~22:00)의 30분 칸(slot)마다 첫 실행에서 대기열을 텔레그램으로 발송
     - 대기열이 비어 있으면 아무것도 보내지 않음
     - 22:30~다음날 06:59에 쌓인 기사는 다음날 07:00 칸에 한꺼번에 발송

필요한 환경변수 (GitHub Secrets)
  TELEGRAM_BOT_TOKEN                BotFather가 준 봇 토큰
  TELEGRAM_CHAT_ID                  발송할 채널(또는 채팅) ID
  NEWS_KEYWORDS                     검색 키워드 (쉼표 또는 줄바꿈으로 구분)
  NEWS_EXCLUDE                      (선택) 제목에 이 단어가 들어 있으면 제외
선택 환경변수
  NEWS_WHEN=2h                      최근 몇 시간 기사만 조회할지(구글의 when: 검색 연산자, 비워 두면 제한 없음)
  SEND_START=07:00  SEND_END=22:00  SEND_INTERVAL=30   발송 시간대/간격(분)
  MODE=run|test     test면 텔레그램 테스트 메시지만 보내고 종료
  BACKFILL_HOURS=0  처음 실행할 때 몇 시간 전 기사부터 가져올지(최대 36)
  FORCE_SEND=1      시간 칸과 상관없이 대기열을 지금 발송
  DRY_RUN=1         텔레그램으로 보내지 않고 화면에만 출력
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어서 고정 오프셋 사용
GOOGLE_RSS = "https://news.google.com/rss/search"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/{method}"

LOOKBACK_HOURS = 36      # 이보다 오래된 기사는 무시
SEEN_KEEP_HOURS = 48     # 중복 확인용 기록 보관 시간
MSG_LIMIT = 3800         # 텔레그램 메시지 한 통에 보이는 글자수 여유 한도(공식 한도 4096)
MAX_ITEMS_PER_MSG = 30   # 한 통에 담는 기사 수 상한(링크가 너무 많아지는 것을 방지)
MAX_MESSAGES_PER_RUN = 10
TITLE_MAX = 200


# ----------------------------------------------------------------- 유틸
def log(msg):
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def split_terms(text):
    return [t.strip() for t in re.split(r"[,\n]", text or "") if t.strip()]


def hm_to_min(hm):
    h, m = hm.strip().split(":")
    return int(h) * 60 + int(m)


def kid(keyword):
    """키워드 이름이 저장소에 그대로 남지 않도록 짧은 해시로 저장한다."""
    return hashlib.sha1(keyword.encode("utf-8")).hexdigest()[:8]


def lid(link):
    """기사 링크가 너무 길어서 중복 확인용으로는 짧은 해시를 쓴다."""
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]


def clean_title(raw):
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:TITLE_MAX]


def parse_pub(value, fallback):
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST)
    except Exception:
        return fallback


def esc(text):
    return html.escape(text, quote=False)


def esc_attr(text):
    return html.escape(text, quote=True)


def visible_len(markup):
    """태그를 뺀, 텔레그램에서 실제로 글자수로 세는 길이."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", markup)))


# ----------------------------------------------------------------- HTTP
def http_get(req, retries=3, sleep_fn=time.sleep):
    """응답 본문(문자열)을 돌려준다. 429/5xx는 잠깐 쉬고 재시도한다."""
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {e.code}: {body}"
            if e.code == 429 or e.code >= 500:
                wait = 2 * (attempt + 1)
                try:
                    wait = min(int(json.loads(body)["parameters"]["retry_after"]), 60)
                except Exception:
                    pass
                sleep_fn(wait)
                continue
            raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"{type(e).__name__}: {e}"
            sleep_fn(2 * (attempt + 1))
    raise RuntimeError(last or "요청 실패")


def google_search_factory(when):
    def search(keyword):
        query = f"{keyword} when:{when}" if when else keyword
        qs = urllib.parse.urlencode({"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
        req = urllib.request.Request(
            f"{GOOGLE_RSS}?{qs}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; news-bot/1.0)"},
        )
        text = http_get(req)
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError:
            raise RuntimeError("RSS 형식이 아닌 응답을 받았습니다(구글이 접속을 막았을 수 있음)")
        items = []
        for it in root.iter("item"):
            items.append(
                {
                    "title": it.findtext("title") or "",
                    "link": (it.findtext("link") or "").strip(),
                    "pubDate": it.findtext("pubDate") or "",
                }
            )
        return items

    return search


def telegram_send_factory(token, chat_id):
    def send(text):
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            TELEGRAM_URL.format(token=token, method="sendMessage"),
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        data = json.loads(http_get(req))
        if not data.get("ok"):
            raise RuntimeError(f"텔레그램 오류: {data.get('description')}")

    return send


# ----------------------------------------------------------------- 상태
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("start", None)
    state.setdefault("seen", {})
    state.setdefault("pending", {})
    state.setdefault("last_slot", None)
    return state


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, path)


# ----------------------------------------------------------------- 수집
def collect(cfg, state, now, search_fn):
    """키워드별로 새 기사를 찾아 state['pending']에 넣는다. 실패한 키워드 수를 돌려준다."""
    if not state["start"]:
        state["start"] = (now - timedelta(hours=cfg["backfill_hours"])).isoformat()
    floor = max(datetime.fromisoformat(state["start"]), now - timedelta(hours=LOOKBACK_HOURS))
    seen, pending = state["seen"], state["pending"]
    failures = 0

    for kw in cfg["keywords"]:
        tag = kid(kw)
        try:
            items = search_fn(kw)
            new, pubs = 0, []
            for it in items:
                link = it.get("link")
                if not link:
                    continue
                key = lid(link)
                pub = parse_pub(it.get("pubDate"), now)
                pubs.append(pub)
                if pub < floor:
                    continue
                if key in pending:
                    if tag not in pending[key]["k"]:
                        pending[key]["k"].append(tag)
                    continue
                if key in seen:
                    continue
                seen[key] = pub.isoformat()
                title = clean_title(it.get("title")) or link
                low = title.lower()
                if any(x.lower() in low for x in cfg["exclude"]):
                    continue
                pending[key] = {"i": key, "t": title, "l": link, "k": [tag], "p": pub.isoformat()}
                new += 1
            span = f", 기사 시각 {min(pubs):%m/%d %H:%M} ~ {max(pubs):%m/%d %H:%M}" if pubs else ""
            log(f"키워드 {tag}: 조회 {len(items)}건, 신규 {new}건{span}")
        except Exception as e:  # 한 키워드가 실패해도 나머지는 계속
            failures += 1
            log(f"키워드 {tag} 조회 실패({type(e).__name__}): {e}")

    cutoff = now - timedelta(hours=SEEN_KEEP_HOURS)
    for key in [k for k, p in seen.items() if datetime.fromisoformat(p) < cutoff and k not in pending]:
        del seen[key]
    return failures


# ----------------------------------------------------------------- 발송
def slot_id(now, cfg):
    """지금이 발송 시간대의 몇 번째 30분 칸인지. 시간대 밖이면 None."""
    m = now.hour * 60 + now.minute
    if m < cfg["start_min"]:
        return None
    k = (m - cfg["start_min"]) // cfg["interval"]
    if cfg["start_min"] + k * cfg["interval"] > cfg["end_min"]:
        return None
    return f"{now:%Y-%m-%d}#{k}"


def build_messages(items, keywords, now, limit=MSG_LIMIT):
    """기사 목록을 키워드별로 묶어 텔레그램 메시지(들)로 만든다. [(text, [기사id,...]), ...]"""
    names = {kid(k): k for k in keywords}
    order = [kid(k) for k in keywords]
    groups = {}
    for it in items:
        first = next((t for t in order if t in it["k"]), None)
        groups.setdefault(first, []).append(it)
    ordered = [t for t in order if t in groups] + ([None] if None in groups else [])

    total = len(items)
    chunks = []

    def new_chunk(cont):
        head = f"📰 뉴스 {total}건 · {now:%m/%d %H:%M}" + (" (이어서)" if cont else "")
        return [head], []

    lines, ids = new_chunk(False)
    for tag in ordered:
        label = names.get(tag, "기타")
        need_header = True
        for it in groups[tag]:
            line = f'• <a href="{esc_attr(it["l"])}">{esc(it["t"])}</a>'
            extra = (["", f"<b>{esc(label)}</b>"] if need_header else []) + [line]
            if ids and (len(ids) >= MAX_ITEMS_PER_MSG or visible_len("\n".join(lines + extra)) > limit):
                chunks.append(("\n".join(lines), ids))
                lines, ids = new_chunk(True)
                extra = ["", f"<b>{esc(label)}</b>", line]
            lines += extra
            ids.append(it["i"])
            need_header = False
    if ids:
        chunks.append(("\n".join(lines), ids))
    return chunks


def flush_pending(cfg, state, now, send_fn, sleep_fn=time.sleep):
    """대기열을 보낸다. 성공한 만큼만 대기열에서 지운다. 남은 기사 수를 돌려준다."""
    items = sorted(state["pending"].values(), key=lambda x: x["p"])
    chunks = build_messages(items, cfg["keywords"], now)
    for n, (text, ids) in enumerate(chunks[:MAX_MESSAGES_PER_RUN]):
        if n:
            sleep_fn(3.5)  # 그룹/채널은 분당 약 20건 제한이 있어 간격을 둔다
        send_fn(text)
        for i in ids:
            state["pending"].pop(i, None)
    return len(state["pending"])


def run_cycle(cfg, state, now, search_fn, send_fn, sleep_fn=time.sleep):
    """한 번 실행분: 수집 -> (해당 칸이면) 발송. (실패 키워드 수, 오류 메시지 또는 None)"""
    failures = collect(cfg, state, now, search_fn)
    slot = slot_id(now, cfg)
    new_slot = slot is not None and slot != state["last_slot"]
    error = None
    if cfg["force_send"] or new_slot:
        if state["pending"]:
            try:
                remaining = flush_pending(cfg, state, now, send_fn, sleep_fn)
                if new_slot and remaining == 0:
                    state["last_slot"] = slot
            except Exception as e:
                error = f"텔레그램 발송 실패({type(e).__name__}): {e}"
        elif new_slot:
            state["last_slot"] = slot  # 보낼 게 없어도 이 칸은 처리한 것으로 표시
    return failures, error


# ----------------------------------------------------------------- 진입점
def build_cfg():
    when = os.environ.get("NEWS_WHEN")
    return {
        "keywords": split_terms(os.environ.get("NEWS_KEYWORDS")),
        "exclude": split_terms(os.environ.get("NEWS_EXCLUDE")),
        "when": "2h" if when is None or when == "" else when.strip(),
        "start_min": hm_to_min(os.environ.get("SEND_START") or "07:00"),
        "end_min": hm_to_min(os.environ.get("SEND_END") or "22:00"),
        "interval": int(os.environ.get("SEND_INTERVAL") or 30),
        "backfill_hours": min(int(os.environ.get("BACKFILL_HOURS") or 0), LOOKBACK_HOURS),
        "force_send": (os.environ.get("FORCE_SEND") or "").lower() in ("1", "true", "yes"),
    }


def main():
    cfg = build_cfg()
    dry = (os.environ.get("DRY_RUN") or "").lower() in ("1", "true", "yes")
    state_path = os.environ.get("STATE_PATH", "state.json")

    if dry:
        send_fn = lambda text: print("---- (DRY_RUN) ----\n" + text + "\n")
    else:
        send_fn = telegram_send_factory(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])

    if (os.environ.get("MODE") or "run") == "test":
        send_fn('✅ 뉴스 봇 연결 테스트입니다. 이 메시지가 보이면 텔레그램 설정은 정상입니다.\n<a href="https://news.google.com">링크 형태 확인</a>')
        log("테스트 메시지 발송 완료")
        return 0

    if not cfg["keywords"]:
        log("NEWS_KEYWORDS가 비어 있습니다. 키워드를 설정해 주세요.")
        return 1

    search_fn = google_search_factory(cfg["when"])
    state = load_state(state_path)
    now = datetime.now(KST)
    before = len(state["pending"])
    failures, error = run_cycle(cfg, state, now, search_fn, send_fn)
    save_state(state_path, state)
    log(f"대기열 {before} -> {len(state['pending'])}건, 실패 키워드 {failures}/{len(cfg['keywords'])}")

    if error:
        log(error)
        return 1
    if failures == len(cfg["keywords"]):
        log("모든 키워드 조회가 실패했습니다(네트워크/구글 접속 확인).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
