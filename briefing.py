"""오늘의 아침 브리핑 — Claude API 번역·요약

개미 투자자가 하루 등락에 휘둘리지 않고 큰 흐름을 볼 수 있도록,
해외 공신력 있는 매체(CNBC)와 한국은행 RSS에서 기사를 아침마다 7개 모아 한국어로 보여준다.

- RSS에 있는 제목과 소개문만 쓰고, 기사 본문은 가져오지 않는다.
- 한 번 실행할 때 RSS마다 한 번씩만 요청한다. RSS 하나가 안 열려도 나머지는 계속 진행한다.
- 최근 48시간 안의 기사만 고른다.
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
ENV_FILE = BASE_DIR / ".env"

KST = timezone(timedelta(hours=9))
TOTAL_ITEMS = 7
MAX_AGE = timedelta(hours=48)

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


def is_recent(article, now):
    return article["published"] is not None and now - article["published"] <= MAX_AGE


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


def print_stats(candidates):
    """최근 48시간 기사 중 카테고리·세부 지표별로 몇 개 걸렸는지 보여준다."""
    counts = {}
    for a in candidates:
        counts[a["cls"]["label"]] = counts.get(a["cls"]["label"], 0) + 1

    order = [f"{ECON} · {r} {n}" for r, subs in ECON_INDICATORS.items() for n in subs]
    order += list(OTHER_CATEGORIES)
    print(f"최근 48시간 안에 키워드에 걸린 기사: {len(candidates)}개")
    for label in order:
        if counts.get(label):
            print(f"  {label}: {counts[label]}개")


# ---------------------------------------------------------------- 요약 (Claude API)

def make_client():
    """.env에서 API 키를 읽어 클라이언트를 만든다. 키 값은 절대 출력하지 않는다."""
    import anthropic
    from dotenv import load_dotenv

    # .env가 있으면 읽고, 없으면 조용히 넘어간다. 이미 있는 환경변수는 덮어쓰지 않는다.
    load_dotenv(ENV_FILE)
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
    bullets = [l[2:].strip() for l in lines if l.startswith("- ")]
    if not bullets:
        return None
    if korean:
        return None, bullets
    title_ko = lines[0].lstrip("#").strip()
    if not title_ko or title_ko.startswith("- "):
        return None
    return title_ko, bullets


RULES = """- 기사 정보에 없는 내용, 숫자, 이름은 절대 쓰지 않는다
- "사라/팔아라", "오를 것이다" 같은 예측이나 투자 권유는 쓰지 않는다
- 정보가 부족하면 마지막 줄에 "- 자세한 내용은 원문 확인 필요"라고 쓴다"""


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


def save_news(items, updated_at):
    data = {
        "mode": "practice" if PRACTICE_MODE else "ai",
        "model": None if PRACTICE_MODE else MODEL,
        "updated_at": updated_at.isoformat(),
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


def render_html(data):
    esc = html.escape
    cards = [render_card(it) for it in data["items"]]
    if not cards:
        cards.append('<p class="empty">최근 48시간 안에 키워드에 맞는 기사가 없습니다.</p>')

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
  @media (max-width: 480px) {{
    main {{ padding-top: 24px; }}
    h1 {{ font-size: 1.35rem; }}
    .card h2 {{ font-size: 1.08rem; }}
  }}
</style>
</head>
<body>
<main>
  <header>
    <h1>오늘의 아침 브리핑</h1>
    <p class="sub">기준 시각 {esc(format_kst(data["updated_at"]))} (한국 시간) · 출처 CNBC, 한국은행</p>
    {badge}
  </header>
  {"".join(cards)}
  <footer>RSS의 제목과 소개문만 사용합니다. 기사 본문은 원문 링크에서 확인하세요. 투자 권유가 아닙니다.</footer>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- 실행

def main():
    print("모드:", "연습 모드 (API 호출 없음)" if PRACTICE_MODE else f"AI 요약 ({MODEL})")
    now = datetime.now(timezone.utc)
    articles = collect_articles()
    candidates = [a for a in articles if a["cls"] and is_recent(a, now)]
    print_stats(candidates)

    selected = select_articles(candidates)
    items = build_items(selected)  # 요약은 고른 기사에만
    data = save_news(items, now.astimezone(KST))
    INDEX_HTML.write_text(render_html(data), encoding="utf-8")

    ok = sum(1 for it in items if it["ai_summary"])
    print(f"완료: {len(items)}개 기사 (AI 요약 {ok}개) -> {NEWS_JSON.name}, {INDEX_HTML.name}")
    for it in items:
        print(f"  [{it['label']}] {it['title_ko'] or it['title']}")


if __name__ == "__main__":
    main()
