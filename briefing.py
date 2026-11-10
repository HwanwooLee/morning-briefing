"""오늘의 아침 브리핑 — Claude API 번역·요약

개미 투자자가 하루 등락에 휘둘리지 않고 큰 흐름을 볼 수 있도록,
해외 공신력 있는 매체(CNBC)와 한국은행 RSS에서 기사를 아침마다 7개 모아 한국어로 보여준다.

- RSS에 있는 제목과 소개문만 쓰고, 기사 본문은 가져오지 않는다.
- 한 번 실행할 때 RSS마다 한 번씩만 요청한다. RSS 하나가 안 열려도 나머지는 계속 진행한다.
- 지난번 실행 이후의 기사만 고른다 (last_run.json. 기록이 없으면 48시간, 최대 96시간).
- 맨 위에 나스닥·S&P 500(FRED), 코스피(한국은행 ECOS) 지수 카드를 보여준다.
- 분류된 기사는 news_history.json에 쌓고(API 안 씀), dashboard.html로 건수를 보여준다.
- 고른 기사에만 Claude API를 기사당 한 번 호출한다 (영어 기사: 번역+요약, 한국어 기사: 요약만).
- API 호출이 실패한 기사는 원래 제목·소개문·링크만 보여주고, 나머지는 계속 진행한다.
- API 키는 환경변수 ANTHROPIC_API_KEY에서 읽는다. 내 PC에서는 .env 파일에 넣어 두면 되고,
  .env가 없으면(GitHub Actions 등) 이미 설정된 환경변수를 그대로 쓴다. 키 값은 출력하지 않는다.

실행:
    python briefing.py             # AI 번역·요약
    python briefing.py --practice  # 연습 모드 (API 호출 없음)
"""

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ---------------------------------------------------------------- 설정

PRACTICE_MODE = "--practice" in sys.argv

MODEL = "claude-sonnet-5-5"

BASE_DIR = Path(__file__).resolve().parent
NEWS_JSON = BASE_DIR / "news.json"
INDEX_HTML = BASE_DIR / "index.html"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
HISTORY_JSON = BASE_DIR / "news_history.json"
LAST_RUN_JSON = BASE_DIR / "last_run.json"
ENV_FILE = BASE_DIR / ".env"

KST = timezone(timedelta(hours=9))
TOTAL_ITEMS = 7
DEFAULT_WINDOW = timedelta(hours=48)  # 마지막 실행 기록이 없을 때
MAX_WINDOW = timedelta(hours=96)      # 아무리 오래 쉬었어도 이 이상은 거슬러 가지 않음

# 지수 카드: (화면 이름, 데이터 출처, 시리즈/통계 코드, 시장)
# '업데이트 지연'은 같은 시장의 다른 지수와 기준일을 비교한다 (한국·미국은 휴장일이 달라서).
INDICES = [
    ("나스닥 종합", "FRED", "NASDAQCOM", "US"),
    ("S&P 500", "FMP", "^GSPC", "US"),
    ("코스피", "ECOS", "802Y001/0001000", "KR"),  # 1.5.1.1. 주식시장(일) / KOSPI지수
]
API_KEY_NAMES = {"FRED": "FRED_API_KEY", "FMP": "FMP_API_KEY", "ECOS": "ECOS_API_KEY"}

RSS_FEEDS = {
    "경제·금리": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "AI·빅테크": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "지정학": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    "시장·암호화폐·유동성": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "한국은행": "https://www.bok.or.kr/portal/bbs/P0000093/news.rss?menuNo=200789",
}

# ---------------------------------------------------------------- 카테고리와 키워드
# 아래에 적힌 순서가 곧 우선순위다.

ECON = "경제지표"

# 경제지표: 구분(한국→일본→미국→원자재) 안에서 세부 지표 순서대로 확인
ECON_INDICATORS = {
    "한국": {
        "금리 결정": ["Bank of Korea", "기준금리", "통화정책방향"],
        "환율": ["Korean won", "KRW", "원·달러", "원/달러", "환율"],
    },
    "일본": {
        "금리 결정": ["Bank of Japan", "BOJ"],
        "환율": ["yen", "JPY", "USD/JPY"],
    },
    "미국": {
        "CPI": ["CPI", "consumer price", "inflation report"],
        "PCE": ["PCE", "personal consumption"],
        "PPI": ["PPI", "producer price", "wholesale inflation"],
        "고용": ["payrolls", "jobs report", "unemployment rate"],
        "실업수당 청구": ["jobless claims"],
        "FOMC": ["FOMC", "Fed meeting", "rate decision"],
        "달러": ["dollar index", "DXY", "greenback"],
    },
    "원자재": {
        "유가": ["crude", "WTI", "Brent", "oil price"],
        "금": ["gold price", "gold futures", "spot gold", "bullion"],
    },
}

# 경제지표가 아닐 때 이 순서대로 확인
OTHER_CATEGORIES = {
    "유동성": ["TGA", "Treasury General Account", "liquidity", "carry trade", "yen carry"],
    "AI 인프라": ["AI", "capex", "Nvidia", "Microsoft", "data center", "memory", "HBM"],
    "지정학": ["Iran", "Iranian", "war", "China", "tariff"],
    "정치·정책": ["midterm", "policy", "election"],
    "암호화폐": ["crypto", "bitcoin", "BTC"],
}

# 최우선: 이 키워드가 있으면 무조건 유동성
CARRY_KEYWORDS = ["carry trade", "yen carry"]

# 미국 필터: 다른 나라 이름이 있고 U.S.가 없으면 '미국' 경제지표에서 제외
US_MARKERS = ["U.S.", "United States"]
OTHER_COUNTRIES = [
    "China", "Chinese", "Japan", "Japanese", "Korea", "Korean",
    "euro zone", "eurozone", "Europe", "European", "ECB",
    "UK", "Britain", "British", "Germany", "German", "France",
    "India", "Canada", "Australia", "Mexico", "Brazil", "Russia", "Switzerland",
]

# 큰 카테고리 (최소 1개씩 뽑을 때 이 순서)
CATEGORY_ORDER = [ECON, *OTHER_CATEGORIES]


# ---------------------------------------------------------------- 키워드 일치

HANGUL = re.compile(r"[가-힣]")


def compile_keyword(keyword):
    """키워드 하나를 정규식으로 바꾼다.

    - 한국어 키워드: 글자가 들어 있으면 일치
    - 영어 키워드: 대소문자 무시, 단어 단위로만 일치 ("AI"는 said·rain 안에서 안 걸림,
      "gold"는 Goldman에서 안 걸림). 여러 단어는 그 순서대로 붙어 있을 때만 일치.
      끝에 붙는 복수형 s/es는 허용 ("consumer prices", "oil prices").
    """
    if HANGUL.search(keyword):
        return re.compile(re.escape(keyword))
    body = r"\s+".join(re.escape(w) for w in keyword.split())
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?:s|es)?(?![A-Za-z0-9])", re.IGNORECASE)


def build_patterns(compile_fn=compile_keyword):
    """모든 키워드를 미리 정규식으로 만들어 둔다. (비교용으로 다른 방식도 넣을 수 있게 분리)"""
    def many(words):
        return [compile_fn(w) for w in words]

    return {
        "carry": many(CARRY_KEYWORDS),
        "econ": {region: {name: many(words) for name, words in subs.items()}
                 for region, subs in ECON_INDICATORS.items()},
        "other": {cat: many(words) for cat, words in OTHER_CATEGORIES.items()},
        # "US"는 대문자일 때만 (소문자 us는 '우리'라는 뜻이라서)
        "us": many(US_MARKERS) + [re.compile(r"(?<![A-Za-z0-9])US(?![A-Za-z0-9])")],
        "countries": many(OTHER_COUNTRIES),
    }


PATTERNS = build_patterns()


def _any(patterns, text):
    return any(p.search(text) for p in patterns)


def _hits(patterns, text):
    """서로 다른 키워드가 몇 개 나왔는지 (같은 키워드가 여러 번 나와도 1개로 센다)."""
    return sum(1 for p in patterns if p.search(text))


DESCRIPTION_MIN_HITS = 2  # 소개문으로 분류할 때 필요한 '같은 카테고리의 서로 다른 키워드' 수


def classify_article(title, description, patterns=PATTERNS):
    """제목 우선 + 소개문은 2개 이상.

    1. 제목에 키워드가 있으면 그 카테고리로 정한다.
    2. 제목에 키워드가 없으면 소개문을 보되, 같은 카테고리의 서로 다른 키워드가
       2개 이상 나올 때만 인정한다. (경제지표는 세부 지표 하나 안에서 센다.)
    3. 소개문에 키워드가 1개만 있으면 분류하지 않는다.
    (미국 필터의 나라 이름 확인은 제목+소개문 전체로 한다.)
    """
    context = f"{title} {description}"
    return (classify(title, patterns, context, min_hits=1)
            or classify(description, patterns, context, min_hits=DESCRIPTION_MIN_HITS))


def classify(text, patterns=PATTERNS, context=None, min_hits=1):
    """글 하나를 카테고리 하나로 정한다. 아무 데도 안 걸리면 None.

    min_hits: 한 카테고리(경제지표는 세부 지표)에서 서로 다른 키워드가 몇 개 이상 나와야 인정할지
    context: 미국 필터에서 나라 이름을 찾을 범위 (없으면 text)

    우선순위
      1. carry trade / yen carry → 유동성
      2. 경제지표: 한국 → 일본 → 미국 → 원자재
      3. 유동성 → AI 인프라 → 지정학 → 정치·정책 → 암호화폐
    """
    liquidity_ok = _hits(patterns["other"]["유동성"], text) >= min_hits
    if liquidity_ok and _any(patterns["carry"], text):
        return {"category": "유동성", "region": None, "indicator": None, "label": "유동성"}

    context = context or text
    is_foreign = _any(patterns["countries"], context) and not _any(patterns["us"], context)
    for region, subs in patterns["econ"].items():
        if region == "미국" and is_foreign:
            continue  # 미국 필터
        for name, pats in subs.items():
            if _hits(pats, text) >= min_hits:
                return {"category": ECON, "region": region, "indicator": name,
                        "label": f"{ECON} · {region} {name}"}

    for cat, pats in patterns["other"].items():
        if _hits(pats, text) >= min_hits:
            return {"category": cat, "region": None, "indicator": None, "label": cat}
    return None


# ---------------------------------------------------------------- RSS 가져오기

def clean_text(raw):
    """소개문에 섞인 HTML 태그를 지우고 공백을 정리한다.
    (한국은행 RSS는 태그가 한 번 더 이스케이프되어 있어서 먼저 풀고 지운다.)"""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_link(link):
    # 같은 기사가 피드마다 ?쿼리만 다르게 올 수 있어서 떼고 비교한다.
    # (한국은행 링크는 ?nttId=가 기사 번호라서 쿼리를 남긴다.)
    if "bok.or.kr" in link:
        return link.strip().lower()
    return link.split("?")[0].split("#")[0].rstrip("/").lower()


def fetch_feed(url):
    """RSS 한 개를 딱 한 번 요청해서 제목·링크·소개문·시각만 꺼낸다."""
    req = urllib.request.Request(url, headers={"User-Agent": "morning-briefing/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())

    items = []
    for node in root.iter("item"):
        title = clean_text(node.findtext("title"))
        link = (node.findtext("link") or "").strip()
        if not title or not link:
            continue
        try:
            published = parsedate_to_datetime(node.findtext("pubDate"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = None
        items.append({
            "title": title,
            "link": link,
            "description": clean_text(node.findtext("description")),
            "published": published,
        })
    return items


def collect_articles():
    """모든 RSS를 한 번씩 가져와 중복을 지운다. 분류 결과는 a["cls"]에 (안 걸리면 None)."""
    seen_links, seen_titles = set(), set()
    articles = []
    for name, url in RSS_FEEDS.items():
        try:
            feed_items = fetch_feed(url)
            print(f"[가져옴] {name}: {len(feed_items)}개")
        except Exception as e:  # 피드 하나가 실패해도 나머지는 계속
            print(f"[건너뜀] {name}: {e}")
            continue
        for it in feed_items:
            key_link, key_title = normalize_link(it["link"]), it["title"].lower()
            if key_link in seen_links or key_title in seen_titles:
                continue  # 여러 RSS에 같은 기사가 있으면 하나만
            seen_links.add(key_link)
            seen_titles.add(key_title)
            it["source"] = name
            it["cls"] = classify_article(it["title"], it["description"])
            articles.append(it)
    return articles


def window_start(now):
    """기사를 고를 시작 시각 = 지난번 실행 시각. 기록이 없으면 48시간 전, 최대 96시간 전까지만."""
    try:
        last = datetime.fromisoformat(
            json.loads(LAST_RUN_JSON.read_text(encoding="utf-8"))["last_run"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return now - DEFAULT_WINDOW, "실행 기록 없음 → 최근 48시간"
    if last >= now:
        return now - DEFAULT_WINDOW, "실행 기록이 이상함 → 최근 48시간"
    if now - last > MAX_WINDOW:
        return now - MAX_WINDOW, "지난 실행이 96시간보다 오래됨 → 최근 96시간"
    return last, f"지난번 실행({format_kst(last.isoformat())} KST) 이후"


def save_last_run(now):
    LAST_RUN_JSON.write_text(json.dumps({"last_run": now.isoformat()}, indent=2) + "\n",
                             encoding="utf-8")


def is_recent(article, start, now):
    p = article["published"]
    return p is not None and start < p <= now


# ---------------------------------------------------------------- 고르기

def select_articles(candidates, total=TOTAL_ITEMS):
    """최신순 총 7개, 가능하면 큰 카테고리마다 최소 1개. 모자라면 있는 만큼만."""
    pool = sorted(candidates, key=lambda a: a["published"], reverse=True)

    chosen, used = [], set()
    # 1단계: 큰 카테고리마다 가장 최신 기사 1개씩
    for cat in CATEGORY_ORDER:
        if len(chosen) >= total:
            break
        for a in pool:
            if a["link"] not in used and a["cls"]["category"] == cat:
                chosen.append(a)
                used.add(a["link"])
                break
    # 2단계: 남은 자리는 최신순으로 채우기
    for a in pool:
        if len(chosen) >= total:
            break
        if a["link"] not in used:
            chosen.append(a)
            used.add(a["link"])

    chosen.sort(key=lambda a: a["published"], reverse=True)
    return chosen


def print_stats(candidates, window_text):
    """기간 안 기사 중 카테고리·세부 지표별로 몇 개 걸렸는지 보여준다."""
    counts = {}
    for a in candidates:
        counts[a["cls"]["label"]] = counts.get(a["cls"]["label"], 0) + 1

    order = [f"{ECON} · {r} {n}" for r, subs in ECON_INDICATORS.items() for n in subs]
    order += list(OTHER_CATEGORIES)
    print(f"기사 기간: {window_text}")
    print(f"기간 안에 키워드에 걸린 기사: {len(candidates)}개")
    for label in order:
        if counts.get(label):
            print(f"  {label}: {counts[label]}개")


# ---------------------------------------------------------------- 환경변수 (.env)

def load_env():
    """.env가 있으면 읽고, 없으면(GitHub Actions 등) 이미 있는 환경변수를 그대로 쓴다.
    이미 있는 환경변수는 덮어쓰지 않는다. 키 값은 절대 출력하지 않는다."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ENV_FILE)


# ---------------------------------------------------------------- 지수 카드 (FRED / ECOS)

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "morning-briefing/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fred_closes(series_id, key):
    """FRED 일별 종가를 최신순으로 [(날짜, 값), ...]. 휴장일 값 "."은 건너뛴다."""
    query = urllib.parse.urlencode({
        "series_id": series_id, "api_key": key, "file_type": "json",
        "sort_order": "desc", "limit": 15,
    })
    data = fetch_json(f"https://api.stlouisfed.org/fred/series/observations?{query}")
    closes = []
    for o in data.get("observations", []):
        try:
            closes.append((o["date"], float(o["value"])))
        except (KeyError, ValueError):
            continue  # "." = 휴장일
    return closes


def fmp_closes(symbol, key):
    """FMP 일별 종가(EOD)를 최신순으로 [(날짜, 값), ...]. 무료 요금제에서 ^GSPC 확인됨."""
    query = urllib.parse.urlencode({"symbol": symbol, "apikey": key})
    data = fetch_json(f"https://financialmodelingprep.com/stable/historical-price-eod/light?{query}")
    if isinstance(data, dict):  # 오류 응답 (예: {"Error Message": "..."}). 키는 가리고 보여준다
        msg = str(data.get("Error Message") or data)[:120].replace(key, "***")
        raise LookupError(f"FMP 오류: {msg}")
    closes = []
    for r in data:
        try:
            closes.append((r["date"], float(r["price"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(closes, reverse=True)


def ecos_closes(stat_item, key, today):
    """ECOS 일별 통계를 최신순으로 [(날짜, 값), ...]. stat_item = '통계코드/항목코드'."""
    stat, item = stat_item.split("/")
    start = (today - timedelta(days=30)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}"
           f"/json/kr/1/100/{stat}/D/{start}/{end}/{item}")
    data = fetch_json(url)
    if "RESULT" in data:  # 오류 응답 (예: INFO-100 인증키 오류, INFO-200 데이터 없음)
        r = data["RESULT"]
        raise LookupError(f"ECOS {r.get('CODE')}: {str(r.get('MESSAGE', '')).splitlines()[0]}")
    rows = data.get("StatisticSearch", {}).get("row", [])
    closes = []
    for r in rows:
        try:
            t = r["TIME"]
            closes.append((f"{t[:4]}-{t[4:6]}-{t[6:8]}", float(r["DATA_VALUE"])))
        except (KeyError, ValueError):
            continue
    return sorted(closes, reverse=True)


def fetch_indices(now):
    """지수 카드 3개. 하나가 실패해도 그 카드만 '데이터 없음'으로 두고 계속한다.
    등락률 = (전날 종가 / 그 직전 거래일 종가 - 1) × 100"""
    cards = []
    for name, source, code, market in INDICES:
        card = {"name": name, "source": source, "code": code, "market": market,
                "date": None, "close": None, "prev_close": None, "change_pct": None,
                "delayed": False}
        key = os.environ.get(API_KEY_NAMES[source])
        try:
            if not key:
                raise LookupError(f"{API_KEY_NAMES[source]} 없음")
            if source == "FRED":
                closes = fred_closes(code, key)
            elif source == "FMP":
                closes = fmp_closes(code, key)
            else:
                closes = ecos_closes(code, key, now.astimezone(KST).date())
            if len(closes) < 2:
                raise LookupError("거래일 데이터가 2개보다 적음")
            (date, close), (_, prev) = closes[0], closes[1]
            card.update(date=date, close=close, prev_close=prev,
                        change_pct=round((close / prev - 1) * 100, 2))
            print(f"[지수] {name}: {close:,.2f} ({card['change_pct']:+.2f}%, {date})")
        except LookupError as e:
            print(f"[지수] {name}: 데이터 없음 ({e})")
        except Exception as e:  # 네트워크 등. 키가 URL에 있으므로 오류 종류(와 HTTP 코드)만 출력
            reason = type(e).__name__
            if getattr(e, "code", None):  # HTTPError면 상태 코드도
                reason += f" {e.code}"
            print(f"[지수] {name}: 데이터 없음 ({reason})")
        cards.append(card)

    # 업데이트 지연: 같은 시장의 다른 지수보다 기준일이 오래됐으면 표시
    for c in cards:
        others = [o["date"] for o in cards
                  if o is not c and o["date"] and o["market"] == c["market"]]
        if c["date"] and others and c["date"] < max(others):
            c["delayed"] = True
            print(f"[지수] {c['name']}: 업데이트 지연 (기준일 {c['date']} < {max(others)})")
    return cards


# ---------------------------------------------------------------- 요약 (Claude API)

def make_client():
    """.env에서 API 키를 읽어 클라이언트를 만든다. 키 값은 절대 출력하지 않는다."""
    import anthropic

    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[경고] ANTHROPIC_API_KEY가 없습니다 (.env 또는 환경변수 확인). "
              "모든 기사를 원문 그대로 보여줍니다.")
        return None
    return anthropic.Anthropic(timeout=60.0)  # 키는 환경변수에서 SDK가 직접 읽는다


def is_korean(title):
    return bool(HANGUL.search(title))


def parse_answer(text, korean):
    """영어 기사: 첫 줄 = 한국어 제목, "- " 줄 = 요약.
    한국어 기사: "- " 줄만 요약. 형식이 안 맞으면 None."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    bullets = tidy_bullets([l[2:].strip() for l in lines if l.startswith("- ")])
    if not bullets:
        return None
    if korean:
        return None, bullets
    title_ko = lines[0].lstrip("#").strip()
    if not title_ko or title_ko.startswith("- "):
        return None
    return title_ko, bullets


CHECK_ORIGINAL = "자세한 내용은 원문 확인 필요"

RULES = f"""- 기사 정보에 없는 내용, 숫자, 이름은 절대 쓰지 않는다
- "사라/팔아라", "오를 것이다" 같은 예측이나 투자 권유는 쓰지 않는다
- 요약 줄에는 기사에 있는 사실만 쓴다. "정보가 부족하다", "구체적인 내용은 알 수 없다",
  "소개문에 나와 있지 않다"처럼 정보가 모자란다는 뜻의 문장은 요약 줄에 쓰지 않는다
- 정보가 부족하면 그 뜻은 맨 마지막 줄 "- {CHECK_ORIGINAL}" 한 번으로만 나타낸다
  (이 줄은 세 줄 요약 뒤에 따로 붙이고, 두 번 이상 쓰지 않는다)"""


def tidy_bullets(bullets):
    """'원문 확인 필요' 줄이 여러 번 나오거나 중간에 끼면, 맨 끝에 한 번만 남긴다."""
    body = [b for b in bullets if CHECK_ORIGINAL not in b]
    if len(body) < len(bullets):
        body.append(CHECK_ORIGINAL)
    return body


def summarize(client, title, description):
    """고른 기사 하나를 요약한다. API는 기사당 딱 한 번만 호출한다.

    영어 기사는 번역+요약, 한국어 기사는 번역 없이 세 줄 요약만.
    성공하면 (한국어 제목 또는 None, [요약 줄들]), 연습 모드이거나 실패하면 None.
    """
    korean = is_korean(title)
    if korean:
        prompt = f"""아래 기사 정보만 보고 한국어로 답해 줘.
- 세 줄 요약. 각 줄은 한 문장, 줄 앞에 "- "를 붙인다 (제목은 쓰지 않는다)
{RULES}

제목: {title}
소개문: {description}"""
    else:
        prompt = f"""아래 기사 정보만 보고 한국어로 답해 줘.
- 첫 줄: 제목을 자연스러운 한국어로 옮긴다
- 다음 세 줄: 한국어 요약. 각 줄은 한 문장, 줄 앞에 "- "를 붙인다
{RULES}

제목: {title}
소개문: {description}"""

    if PRACTICE_MODE or client is None:
        return None

    import anthropic

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            output_config={"effort": "low"},  # 짧은 번역·요약이라 낮은 노력으로 충분
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        print("  [API 실패] API 키가 올바르지 않습니다 (.env 또는 GitHub Secrets 확인)")
        return None
    except anthropic.RateLimitError:
        print("  [API 실패] 요청 한도 초과")
        return None
    except anthropic.APIStatusError as e:
        print(f"  [API 실패] 서버 응답 오류 ({e.status_code})")
        return None
    except anthropic.APIConnectionError:
        print("  [API 실패] 네트워크 연결 오류")
        return None

    if response.stop_reason == "refusal":
        print("  [API 실패] 모델이 답변을 거절했습니다")
        return None

    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = parse_answer(text, korean)
    if parsed is None:
        print("  [API 실패] 답변 형식이 예상과 다릅니다")
    return parsed


# ---------------------------------------------------------------- 저장 / 출력

def build_items(articles):
    client = None if PRACTICE_MODE else make_client()
    items = []
    for a in articles:
        result = summarize(client, a["title"], a["description"])
        if result:
            title_ko, summary = result
            print(f"  [요약 완료] {title_ko or a['title']}")
        else:
            # 연습 모드 또는 실패: 원래 제목·소개문 그대로
            title_ko, summary = None, [a["description"]] if a["description"] else []
        items.append({
            "title": a["title"],
            "title_ko": title_ko,
            "summary": summary,
            "ai_summary": result is not None,
            "link": a["link"],
            "published": a["published"].isoformat() if a["published"] else None,
            "source": a["source"],
            "category": a["cls"]["category"],
            "region": a["cls"]["region"],
            "indicator": a["cls"]["indicator"],
            "label": a["cls"]["label"],
        })
    return items


def save_news(items, updated_at, indices, window_from, window_text):
    data = {
        "mode": "practice" if PRACTICE_MODE else "ai",
        "model": None if PRACTICE_MODE else MODEL,
        "updated_at": updated_at.isoformat(),
        "window": {"from": window_from.isoformat(), "description": window_text},
        "indices": indices,
        "keywords": {ECON: ECON_INDICATORS, **OTHER_CATEGORIES},
        "items": items,
    }
    NEWS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def format_kst(iso):
    if not iso:
        return "시각 정보 없음"
    return datetime.fromisoformat(iso).astimezone(KST).strftime("%Y-%m-%d %H:%M")


def render_card(it):
    esc = html.escape
    head = f"""
      <div class="meta">
        <span class="tag">{esc(it["label"])}</span>
        <time>{esc(format_kst(it["published"]))}</time>
      </div>"""
    link = (f'<a class="link" href="{esc(it["link"], quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">원문 보기</a>')
    lang = "ko" if is_korean(it["title"]) else "en"

    if it["ai_summary"]:
        lis = "".join(f"<li>{esc(s)}</li>" for s in it["summary"])
        if it["title_ko"]:  # 영어 기사: 한국어 제목(크게) + 영어 원제(작게)
            title = f'<h2>{esc(it["title_ko"])}</h2>\n      <p class="orig">{esc(it["title"])}</p>'
        else:               # 한국어 기사: 원래 제목 그대로
            title = f'<h2>{esc(it["title"])}</h2>'
        body = f"""
      {title}
      <ul class="summary">{lis}</ul>"""
    else:
        desc = it["summary"][0] if it["summary"] else "(소개문 없음)"
        body = f"""
      <h2 lang="{lang}">{esc(it["title"])}</h2>
      <p class="desc" lang="{lang}">{esc(desc)}</p>"""

    return f'\n    <article class="card">{head}{body}\n      {link}\n    </article>'


def render_index_card(c):
    """지수 카드 하나. 상승/하락은 색 + 부호(+/-) + 글자(상승/하락)로 함께 보여준다."""
    esc = html.escape
    if c["close"] is None:
        return f"""
      <div class="idx">
        <div class="idx-name">{esc(c["name"])}</div>
        <div class="idx-none">데이터 없음</div>
      </div>"""
    pct = c["change_pct"]
    if pct > 0:
        cls, word = "up", "상승"
    elif pct < 0:
        cls, word = "down", "하락"
    else:
        cls, word = "flat", "보합"
    delayed = ('\n        <div class="idx-delay">업데이트 지연</div>'
               if c.get("delayed") else "")
    return f"""
      <div class="idx">
        <div class="idx-name">{esc(c["name"])}</div>
        <div class="idx-close">{c["close"]:,.2f}</div>
        <div class="idx-chg {cls}">{pct:+.2f}% <span>{word}</span></div>
        <div class="idx-date">기준일 {esc(c["date"])} · 전날 종가</div>{delayed}
      </div>"""


def render_html(data):
    esc = html.escape
    cards = [render_card(it) for it in data["items"]]
    if not cards:
        cards.append('<p class="empty">지난번 실행 이후 키워드에 맞는 기사가 없습니다.</p>')
    index_cards = "".join(render_index_card(c) for c in data.get("indices", []))

    if data["mode"] == "practice":
        badge = '<span class="badge">연습 모드 (AI 요약 아님)</span>'
    else:
        badge = '<span class="badge">AI 요약은 틀릴 수 있음 · 투자 조언 아님</span>'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>오늘의 아침 브리핑</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #fff; color: #222;
         font-family: "Malgun Gothic", "맑은 고딕", sans-serif; line-height: 1.65;
         -webkit-text-size-adjust: 100%; }}
  main {{ max-width: 680px; margin: 0 auto; padding: 40px 16px 64px; }}
  header {{ border-bottom: 2px solid #222; padding-bottom: 16px; margin-bottom: 8px; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 6px; letter-spacing: -0.02em; }}
  .sub {{ color: #777; font-size: .88rem; margin: 0 0 10px; }}
  .badge {{ display: inline-block; font-size: .8rem; font-weight: 700; color: #b45309;
           border: 1px solid #f3c98b; background: #fff8ed; padding: 3px 10px; border-radius: 4px; }}
  .card {{ padding: 22px 0; border-bottom: 1px solid #eee; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
          font-size: .8rem; color: #888; }}
  .tag {{ color: #1d4ed8; font-weight: 700; }}
  .tag::after {{ content: "·"; color: #bbb; margin-left: 8px; font-weight: 400; }}
  .card h2 {{ font-size: 1.2rem; line-height: 1.45; margin: 6px 0 4px; word-break: keep-all; }}
  .orig {{ margin: 0 0 10px; color: #999; font-size: .82rem; line-height: 1.4; }}
  .summary {{ margin: 0 0 10px; padding-left: 1.1em; color: #333; font-size: .95rem; }}
  .summary li {{ margin-bottom: 4px; }}
  .desc {{ margin: 0 0 10px; color: #444; font-size: .95rem; }}
  .link {{ color: #1d4ed8; font-size: .88rem; font-weight: 700; text-decoration: none;
          display: inline-block; padding: 4px 0; }}
  .link::after {{ content: " →"; }}
  .link:hover {{ text-decoration: underline; }}
  .empty {{ color: #888; padding: 24px 0; }}
  footer {{ color: #aaa; font-size: .78rem; margin-top: 28px; }}
  .indices {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 24px; }}
  .idx {{ border: 1px solid #e5e5e5; border-radius: 8px; padding: 12px; min-width: 0; }}
  .idx-name {{ font-size: .82rem; color: #666; font-weight: 700; }}
  .idx-close {{ font-size: 1.2rem; font-weight: 700; margin-top: 2px;
               font-variant-numeric: tabular-nums; }}
  .idx-chg {{ font-size: .9rem; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .idx-chg span {{ font-size: .78rem; font-weight: 400; }}
  .idx-chg.up {{ color: #c62828; }}
  .idx-chg.down {{ color: #1d4ed8; }}
  .idx-chg.flat {{ color: #666; }}
  .idx-date {{ font-size: .72rem; color: #999; margin-top: 4px; }}
  .idx-none {{ color: #999; font-size: .95rem; margin-top: 6px; }}
  .idx-delay {{ display: inline-block; font-size: .72rem; font-weight: 700; color: #92400e;
               background: #fff8ed; border: 1px solid #f3c98b; border-radius: 4px;
               padding: 0 6px; margin-top: 4px; }}
  .nav {{ font-size: .88rem; }}
  .nav a {{ color: #1d4ed8; font-weight: 700; text-decoration: none; }}
  .nav a:hover {{ text-decoration: underline; }}
  @media (max-width: 480px) {{
    main {{ padding-top: 24px; }}
    h1 {{ font-size: 1.35rem; }}
    .card h2 {{ font-size: 1.08rem; }}
    .indices {{ grid-template-columns: 1fr; }}
    .idx {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }}
    .idx-name {{ flex-basis: 100%; }}
    .idx-close, .idx-chg, .idx-date {{ margin-top: 0; }}
  }}
</style>
</head>
<body>
<main>
  <section class="indices" aria-label="주요 지수 (전날 종가)">{index_cards}
  </section>
  <header>
    <h1>오늘의 아침 브리핑</h1>
    <p class="sub">기준 시각 {esc(format_kst(data["updated_at"]))} (한국 시간) · 출처 CNBC, 한국은행
      · 기사 기간: {esc(data["window"]["description"])}</p>
    {badge}
    <p class="nav"><a href="dashboard.html">기사 건수 대시보드 보기 →</a></p>
  </header>
  {"".join(cards)}
  <footer>지수: FRED(나스닥 종합), Financial Modeling Prep(S&amp;P 500), 한국은행 ECOS(코스피) 일별 종가, 전일 대비 등락률.<br>
    RSS의 제목과 소개문만 사용합니다. 기사 본문은 원문 링크에서 확인하세요. 투자 권유가 아닙니다.</footer>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- 기록 (news_history.json)
# API를 쓰지 않는다. 제목·링크·분류만 저장하고 본문·요약은 저장하지 않는다.

def load_history():
    try:
        return json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def update_history(candidates):
    """이번 실행에서 분류된 기사(기간 안) 전체를 기록에 추가. 같은 link는 한 번만."""
    history = load_history()
    known = {normalize_link(h["link"]) for h in history}
    added = 0
    for a in sorted(candidates, key=lambda a: a["published"]):
        key = normalize_link(a["link"])
        if key in known:
            continue
        known.add(key)
        c = a["cls"]
        history.append({
            "date": a["published"].astimezone(KST).date().isoformat(),  # 기사 날짜 (한국 시간)
            "section": a["source"],
            "category": c["category"],
            "region": c["region"],
            "indicator": c["indicator"],
            "title": a["title"],
            "link": a["link"],
        })
        added += 1
    HISTORY_JSON.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
    return history, added


# ---------------------------------------------------------------- 대시보드 (dashboard.html)

DASH_PERIODS = (7, 14)
MIN_HISTORY_DAYS = 3  # 기록된 날짜가 이보다 적으면 예시 데이터로 그린다


def period_dates(end, days):
    return [(end - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


def compute_stats(history, end, days):
    """기간 안 기록을 카테고리·출처·날짜별로 센다."""
    dates = period_dates(end, days)
    by_category = {c: 0 for c in CATEGORY_ORDER}
    by_section = {s: 0 for s in RSS_FEEDS}
    by_date = {d: 0 for d in dates}
    total = 0
    for h in history:
        if dates[0] <= h["date"] <= dates[-1]:
            total += 1
            by_category[h["category"]] = by_category.get(h["category"], 0) + 1
            by_section[h["section"]] = by_section.get(h["section"], 0) + 1
            by_date[h["date"]] += 1
    return {"days": days, "start": dates[0], "end": dates[-1], "total": total,
            "by_category": by_category, "by_section": by_section, "by_date": by_date}


def example_stats(end, days):
    """기록이 부족할 때 쓰는 예시 숫자 (실제 기사 수 아님). 합계가 서로 맞게 만든다."""
    pattern = [9, 12, 7, 14, 10, 4, 3, 11, 13, 8, 15, 9, 5, 6]
    dates = period_dates(end, days)
    by_date = {d: pattern[i % len(pattern)] for i, d in enumerate(dates)}
    total = sum(by_date.values())
    cat_seq = [1, 1, 1, 0, 2, 2, 3, 1, 0, 4, 1, 5]   # 카테고리 순번을 이 비율로 돌려 가며 배정
    sec_seq = [1, 1, 0, 3, 2, 1, 3, 0, 4, 1]
    cats, secs = list(CATEGORY_ORDER), list(RSS_FEEDS)
    by_category = {c: 0 for c in cats}
    by_section = {s: 0 for s in secs}
    for i in range(total):
        by_category[cats[cat_seq[i % len(cat_seq)]]] += 1
        by_section[secs[sec_seq[i % len(sec_seq)]]] += 1
    return {"days": days, "start": dates[0], "end": dates[-1], "total": total,
            "by_category": by_category, "by_section": by_section, "by_date": by_date}


def verify_dashboard(shown, end):
    """그래프에 그린 숫자가 news_history.json을 직접 다시 읽어 센 합계와 같은지 확인한다.
    (compute_stats와 다른 방식으로 따로 센다.) 맞으면 True."""
    raw = json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
    all_ok = True
    for days, s in shown.items():
        start = (end - timedelta(days=days - 1)).isoformat()
        rows = [h for h in raw if start <= h["date"] <= end.isoformat()]

        def count(field):
            out = {}
            for h in rows:
                out[h[field]] = out.get(h[field], 0) + 1
            return out

        nonzero = lambda d: {k: v for k, v in d.items() if v}
        checks = [
            ("모은 기사 수", s["total"], len(rows)),
            ("카테고리 막대 합계", sum(s["by_category"].values()), len(rows)),
            ("출처 막대 합계", sum(s["by_section"].values()), len(rows)),
            ("날짜 선 합계", sum(s["by_date"].values()), len(rows)),
            ("카테고리별 값", nonzero(s["by_category"]), count("category")),
            ("출처별 값", nonzero(s["by_section"]), count("section")),
            ("날짜별 값", nonzero(s["by_date"]), count("date")),
        ]
        bad = [name for name, a, b in checks if a != b]
        if bad:
            all_ok = False
            print(f"  [검증 실패] {days}일: {', '.join(bad)}")
        else:
            print(f"  [검증 통과] {days}일: 그래프 숫자 = 기록 파일 합계 ({len(rows)}건)")
    return all_ok


def top_category(by_category):
    best = max(by_category.values(), default=0)
    if best == 0:
        return "없음", ""
    names = [c for c, v in by_category.items() if v == best]
    return " · ".join(names), f"{best}건"


def bar_chart(title, counts):
    """가로 막대. 0에서 시작하고 막대 끝에 값을 쓴다. 아래에 '표로 보기'."""
    esc = html.escape
    peak = max(counts.values(), default=0) or 1
    rows, trs = [], []
    for name, v in counts.items():
        ratio = v / peak
        rows.append(f"""
        <div class="bar-row" title="{esc(name)}: {v}건">
          <span class="bar-label">{esc(name)}</span>
          <span class="bar-track"><span class="bar" style="width: calc((100% - 3.2em) * {ratio:.4f})"></span><span class="bar-val">{v}</span></span>
        </div>""")
        trs.append(f"<tr><td>{esc(name)}</td><td>{v}</td></tr>")
    return f"""
      <section class="chart">
        <h3>{esc(title)}</h3>
        <div class="bars">{"".join(rows)}
        </div>
        <details><summary>표로 보기</summary>
          <table><thead><tr><th>항목</th><th>기사 수</th></tr></thead><tbody>{"".join(trs)}</tbody></table>
        </details>
      </section>"""


def line_chart(title, by_date):
    """날짜별 기사 수 선 그래프 (SVG). 0에서 시작. 점에 마우스를 올리면 값이 보인다."""
    esc = html.escape
    W, H, L, R, T, B = 480, 210, 30, 24, 22, 30
    dates, values = list(by_date), list(by_date.values())
    peak = max(values, default=0)
    step = max(1, -(-peak // 4))           # 눈금 간격 (위로 4칸)
    top = step * 4
    x = lambda i: L + (W - L - R) * (i / max(1, len(dates) - 1))
    y = lambda v: T + (H - T - B) * (1 - v / top)

    grid = "".join(
        f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="{"axis" if t == 0 else "grid"}"/>'
        f'<text x="{L - 6}" y="{y(t) + 4:.1f}" text-anchor="end" class="tick">{t}</text>'
        for t in range(0, top + 1, step))
    every = 1 if len(dates) <= 7 else 2
    xlabels = "".join(
        f'<text x="{x(i):.1f}" y="{H - 10}" text-anchor="middle" class="tick">{d[5:7]}/{d[8:]}</text>'
        for i, d in enumerate(dates) if (len(dates) - 1 - i) % every == 0)
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    labeled = {len(values) - 1, values.index(peak)} if values else set()  # 최댓값·마지막 값만 숫자 표시
    points = "".join(
        f'<g><circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4"/>'
        f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="12" class="hit"><title>{d}: {v}건</title></circle>'
        + (f'<text x="{x(i):.1f}" y="{y(v) - 9:.1f}" text-anchor="middle" class="val">{v}</text>'
           if i in labeled else "") + "</g>"
        for i, (d, v) in enumerate(zip(dates, values)))
    trs = "".join(f"<tr><td>{d}</td><td>{v}</td></tr>" for d, v in by_date.items())
    return f"""
      <section class="chart">
        <h3>{esc(title)}</h3>
        <svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}">
          {grid}{xlabels}
          <path d="{path}" class="line"/>
          {points}
        </svg>
        <details><summary>표로 보기</summary>
          <table><thead><tr><th>날짜</th><th>기사 수</th></tr></thead><tbody>{trs}</tbody></table>
        </details>
      </section>"""


def render_panel(s, hidden):
    name, sub = top_category(s["by_category"])
    avg = s["total"] / s["days"]
    return f"""
    <div class="panel" data-days="{s['days']}"{' hidden' if hidden else ''}>
      <p class="period">기간: {s['start']} ~ {s['end']} ({s['days']}일, 기사 날짜 기준)</p>
      <div class="stats">
        <div class="stat"><div class="stat-label">모은 기사 수</div><div class="stat-value">{s['total']}건</div></div>
        <div class="stat"><div class="stat-label">가장 많이 나온 카테고리</div><div class="stat-value">{html.escape(name)}</div><div class="stat-sub">{sub}</div></div>
        <div class="stat"><div class="stat-label">하루 평균 기사 수</div><div class="stat-value">{avg:.1f}건</div></div>
      </div>
      {bar_chart("카테고리별 기사 수", s["by_category"])}
      {bar_chart("출처(RSS)별 기사 수", s["by_section"])}
      {line_chart("날짜별 기사 수", s["by_date"])}
    </div>"""


def render_dashboard(history, now):
    """기록이 MIN_HISTORY_DAYS일 미만이면 예시 데이터로 그리고 크게 경고한다."""
    end = now.astimezone(KST).date()
    recorded_days = len({h["date"] for h in history})
    is_example = recorded_days < MIN_HISTORY_DAYS
    if is_example:
        stats = {d: example_stats(end, d) for d in DASH_PERIODS}
    else:
        stats = {d: compute_stats(history, end, d) for d in DASH_PERIODS}

    if is_example:
        warning = ('<div class="warning" role="alert">기록이 아직 부족합니다. '
                   '예시 데이터이며 실제 기사 수가 아닙니다</div>')
    else:
        warning = ""
    buttons = "".join(
        f'<button type="button" data-days="{d}" aria-pressed="{"true" if i == 0 else "false"}">{d}일</button>'
        for i, d in enumerate(DASH_PERIODS))
    panels = "".join(render_panel(stats[d], hidden=i > 0) for i, d in enumerate(DASH_PERIODS))

    page = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>기사 건수 대시보드</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #fff; color: #222;
         font-family: "Malgun Gothic", "맑은 고딕", sans-serif; line-height: 1.6;
         -webkit-text-size-adjust: 100%; }}
  main {{ max-width: 680px; margin: 0 auto; padding: 32px 16px 64px; }}
  .nav {{ font-size: .88rem; margin: 0 0 16px; }}
  .nav a {{ color: #1d4ed8; font-weight: 700; text-decoration: none; }}
  .nav a:hover {{ text-decoration: underline; }}
  .warning {{ font-size: 1.25rem; font-weight: 700; color: #92400e; background: #fff8ed;
             border: 2px solid #f3c98b; border-radius: 8px; padding: 16px; margin-bottom: 20px;
             word-break: keep-all; }}
  header {{ border-bottom: 2px solid #222; padding-bottom: 14px; margin-bottom: 16px; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 4px; letter-spacing: -0.02em; }}
  .sub {{ color: #777; font-size: .88rem; margin: 0; }}
  .toolbar {{ display: flex; gap: 6px; margin-bottom: 8px; }}
  .toolbar button {{ font: inherit; font-size: .9rem; padding: 6px 16px; border-radius: 6px;
                    border: 1px solid #ccc; background: #fff; color: #333; cursor: pointer; }}
  .toolbar button[aria-pressed="true"] {{ background: #222; border-color: #222; color: #fff; font-weight: 700; }}
  .period {{ color: #555; font-size: .9rem; margin: 0 0 14px; }}
  .stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 8px; }}
  .stat {{ border: 1px solid #e5e5e5; border-radius: 8px; padding: 12px; min-width: 0; }}
  .stat-label {{ font-size: .78rem; color: #666; }}
  .stat-value {{ font-size: 1.3rem; font-weight: 700; word-break: keep-all;
                font-variant-numeric: tabular-nums; }}
  .stat-sub {{ font-size: .78rem; color: #888; }}
  .chart {{ padding: 18px 0; border-bottom: 1px solid #eee; }}
  .chart h3 {{ font-size: 1rem; margin: 0 0 10px; }}
  .bar-row {{ display: grid; grid-template-columns: 8.5em 1fr; align-items: center;
             gap: 8px; margin: 6px 0; font-size: .85rem; }}
  .bar-label {{ color: #444; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .bar-track {{ display: flex; align-items: center; gap: 6px; border-left: 1px solid #999; min-height: 20px; }}
  .bar {{ display: block; height: 14px; background: #1d4ed8; border-radius: 0 4px 4px 0; }}
  .bar-val {{ color: #222; font-weight: 700; font-variant-numeric: tabular-nums; }}
  svg {{ width: 100%; height: auto; display: block; }}
  svg .grid {{ stroke: #eee; }}
  svg .axis {{ stroke: #999; }}
  svg .tick {{ fill: #888; font-size: 13px; }}
  svg .val {{ fill: #222; font-size: 13px; font-weight: 700; }}
  svg .line {{ fill: none; stroke: #1d4ed8; stroke-width: 2; }}
  svg circle {{ fill: #1d4ed8; stroke: #fff; stroke-width: 2; }}
  svg circle.hit {{ fill: transparent; stroke: none; }}
  details {{ margin-top: 8px; font-size: .85rem; }}
  summary {{ color: #1d4ed8; cursor: pointer; }}
  table {{ border-collapse: collapse; margin-top: 6px; width: 100%; max-width: 360px; }}
  th, td {{ border-bottom: 1px solid #eee; padding: 4px 8px; text-align: left; }}
  td:last-child, th:last-child {{ text-align: right; font-variant-numeric: tabular-nums; }}
  footer {{ color: #aaa; font-size: .78rem; margin-top: 24px; }}
  @media (max-width: 480px) {{
    h1 {{ font-size: 1.3rem; }}
    .stats {{ grid-template-columns: 1fr; }}
    .bar-row {{ grid-template-columns: 7em 1fr; }}
  }}
</style>
</head>
<body>
<main>
  <p class="nav"><a href="index.html">← 오늘의 아침 브리핑</a></p>
  {warning}
  <header>
    <h1>기사 건수 대시보드</h1>
    <p class="sub">CNBC·한국은행 RSS 기준 기사 건수</p>
  </header>
  <div class="toolbar" role="group" aria-label="기간 선택">{buttons}</div>
  {panels}
  <footer>업데이트 {esc_kst(now)} (한국 시간) · 기록된 날짜 {recorded_days}일 ·
    키워드에 걸린 기사만 셉니다 (같은 링크는 한 번만).</footer>
</main>
<script>
  document.querySelectorAll(".toolbar button").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      document.querySelectorAll(".toolbar button").forEach(function (b) {{
        b.setAttribute("aria-pressed", b === btn ? "true" : "false");
      }});
      document.querySelectorAll(".panel").forEach(function (p) {{
        p.hidden = p.dataset.days !== btn.dataset.days;
      }});
    }});
  }});
</script>
</body>
</html>
"""
    return page, stats, is_example, end


def esc_kst(dt):
    return html.escape(dt.astimezone(KST).strftime("%Y-%m-%d %H:%M"))


# ---------------------------------------------------------------- 실행

def main():
    print("모드:", "연습 모드 (API 호출 없음)" if PRACTICE_MODE else f"AI 요약 ({MODEL})")
    load_env()
    now = datetime.now(timezone.utc)
    start, window_text = window_start(now)

    indices = fetch_indices(now)  # 실패한 카드만 '데이터 없음'
    articles = collect_articles()
    candidates = [a for a in articles if a["cls"] and is_recent(a, start, now)]
    print_stats(candidates, window_text)

    # 기록 + 대시보드 (API 안 씀)
    history, added = update_history(candidates)
    page, stats, is_example, end = render_dashboard(history, now)
    DASHBOARD_HTML.write_text(page, encoding="utf-8")
    print(f"기록: {added}건 추가 (누적 {len(history)}건) -> {HISTORY_JSON.name}, {DASHBOARD_HTML.name}")
    if is_example:
        print(f"  기록된 날짜가 {MIN_HISTORY_DAYS}일 미만이라 대시보드는 예시 데이터로 그렸습니다.")
        print("  실제 기록 합계 검증:")
        verify_dashboard({d: compute_stats(history, end, d) for d in DASH_PERIODS}, end)
    else:
        verify_dashboard(stats, end)

    selected = select_articles(candidates)
    items = build_items(selected)  # 요약은 고른 기사에만
    data = save_news(items, now.astimezone(KST), indices, start, window_text)
    INDEX_HTML.write_text(render_html(data), encoding="utf-8")

    # 연습 모드는 실행 기록을 남기지 않는다 (연습을 여러 번 해도 실제 실행 기간이 줄지 않게)
    if not PRACTICE_MODE:
        save_last_run(now)

    ok = sum(1 for it in items if it["ai_summary"])
    print(f"완료: {len(items)}개 기사 (AI 요약 {ok}개) -> {NEWS_JSON.name}, {INDEX_HTML.name}")
    for it in items:
        print(f"  [{it['label']}] {it['title_ko'] or it['title']}")


if __name__ == "__main__":
    main()
