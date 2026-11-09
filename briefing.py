"""오늘의 아침 브리핑 — 2단계: Claude API 번역·요약

개미 투자자가 하루 등락에 휘둘리지 않고 큰 흐름을 볼 수 있도록,
해외 공신력 있는 매체(CNBC) 기사를 아침마다 5개 모아 한국어로 보여준다.

- RSS에 있는 제목과 소개문만 쓰고, 기사 본문은 가져오지 않는다.
- 한 번 실행할 때 RSS마다 한 번씩만 요청한다.
- 기사 하나당 Claude API를 한 번만 호출한다 (번역 + 요약을 한 번에).
- API 호출이 실패한 기사는 영어 제목·소개문·링크만 보여주고, 나머지는 계속 진행한다.
- API 키는 환경변수 ANTHROPIC_API_KEY에서 읽는다. 내 PC에서는 .env 파일에 넣어 두면 되고,
  .env가 없으면(GitHub Actions 등) 이미 설정된 환경변수를 그대로 쓴다. 키 값은 출력하지 않는다.

실행:
    python briefing.py             # AI 번역·요약
    python briefing.py --practice  # 연습 모드 (API 호출 없음, 1단계처럼 동작)
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
TOTAL_ITEMS = 5

RSS_FEEDS = {
    "경제·금리": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "AI·빅테크": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "지정학": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    "시장·암호화폐·유동성": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
}

# 순서가 곧 우선순위 (묶음마다 1개씩 뽑을 때 이 순서대로 뽑는다)
KEYWORD_GROUPS = {
    "돈의 흐름": ["Fed", "rate", "policy", "midterm", "hedge fund"],
    "유동성": ["TGA", "Treasury General Account", "liquidity", "Treasury"],
    "AI 인프라": ["AI", "capex", "Nvidia", "Microsoft", "data center", "memory", "HBM"],
    "지정학": ["oil", "Iran", "war", "China", "tariff"],
    "암호화폐": ["crypto", "bitcoin", "BTC"],
}


# ---------------------------------------------------------------- 키워드 매칭

def _compile(keyword):
    # 단어 단위로 찾아서 "AI"가 "said"에, "rate"가 "corporate"에 걸리지 않게 한다.
    # 복수형(rates, wars 등)은 허용하고, 대소문자는 무시한다.
    return re.compile(r"\b" + re.escape(keyword) + r"(?:s|es)?\b", re.IGNORECASE)


PATTERNS = {g: [_compile(k) for k in words] for g, words in KEYWORD_GROUPS.items()}


def matching_groups(text):
    return [g for g, pats in PATTERNS.items() if any(p.search(text) for p in pats)]


# ---------------------------------------------------------------- RSS 가져오기

def clean_text(raw):
    """소개문에 섞인 HTML 태그를 지우고 공백을 정리한다."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_link(link):
    # 같은 기사가 피드마다 ?쿼리만 다르게 올 수 있어서 떼고 비교한다.
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
    seen_links, seen_titles = set(), set()
    articles = []
    for name, url in RSS_FEEDS.items():
        try:
            feed_items = fetch_feed(url)
            print(f"[가져옴] {name}: {len(feed_items)}개")
        except Exception as e:  # 피드 하나가 실패해도 나머지는 계속
            print(f"[실패] {name}: {e}")
            continue
        for it in feed_items:
            key_link, key_title = normalize_link(it["link"]), it["title"].lower()
            if key_link in seen_links or key_title in seen_titles:
                continue  # 여러 RSS에 같은 기사가 있으면 하나만
            seen_links.add(key_link)
            seen_titles.add(key_title)
            it["groups"] = matching_groups(f"{it['title']} {it['description']}")
            if it["groups"]:
                articles.append(it)
    return articles


# ---------------------------------------------------------------- 고르기

def select_articles(articles, total=TOTAL_ITEMS):
    """최신순 총 5개, 가능하면 묶음마다 최소 1개. 모자라면 있는 만큼만."""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    pool = sorted(articles, key=lambda a: a["published"] or oldest, reverse=True)

    chosen, used = [], set()
    # 1단계: 묶음마다 아직 안 뽑힌 가장 최신 기사 1개씩
    for group in KEYWORD_GROUPS:
        if len(chosen) >= total:
            break
        for a in pool:
            if a["link"] not in used and group in a["groups"]:
                chosen.append({**a, "group": group})
                used.add(a["link"])
                break
    # 2단계: 남은 자리는 최신순으로 채우기
    for a in pool:
        if len(chosen) >= total:
            break
        if a["link"] not in used:
            chosen.append({**a, "group": a["groups"][0]})
            used.add(a["link"])

    chosen.sort(key=lambda a: a["published"] or oldest, reverse=True)
    return chosen


# ---------------------------------------------------------------- 요약 (Claude API)

def make_client():
    """.env에서 API 키를 읽어 클라이언트를 만든다. 키 값은 절대 출력하지 않는다."""
    import anthropic
    from dotenv import load_dotenv

    # .env가 있으면 읽고, 없으면 조용히 넘어간다. 이미 있는 환경변수는 덮어쓰지 않는다.
    load_dotenv(ENV_FILE)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[경고] ANTHROPIC_API_KEY가 없습니다 (.env 또는 환경변수 확인). "
              "모든 기사를 영어 원문으로 보여줍니다.")
        return None
    return anthropic.Anthropic(timeout=60.0)  # 키는 환경변수에서 SDK가 직접 읽는다


def parse_answer(text):
    """첫 줄 = 한국어 제목, "- "로 시작하는 줄 = 요약. 형식이 안 맞으면 None."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    title_ko = lines[0].lstrip("#").strip()
    bullets = [l[2:].strip() for l in lines[1:] if l.startswith("- ")]
    if not title_ko or not bullets:
        return None
    return title_ko, bullets


def summarize(client, title, description):
    """기사 하나를 번역·요약한다. API는 기사당 딱 한 번만 호출한다.

    성공하면 (한국어 제목, [요약 줄들])을, 연습 모드이거나 실패하면 None을 돌려준다.
    """
    prompt = f"""아래 기사 정보만 보고 한국어로 답해 줘.
- 첫 줄: 제목을 자연스러운 한국어로 옮긴다
- 다음 세 줄: 한국어 요약. 각 줄은 한 문장, 줄 앞에 "- "를 붙인다
- 기사 정보에 없는 내용, 숫자, 이름은 절대 쓰지 않는다
- "사라/팔아라", "오를 것이다" 같은 예측이나 투자 권유는 쓰지 않는다
- 정보가 부족하면 마지막 줄에 "- 자세한 내용은 원문 확인 필요"라고 쓴다

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
    parsed = parse_answer(text)
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
            print(f"  [요약 완료] {title_ko}")
        else:
            # 연습 모드 또는 실패: 영어 제목·소개문 그대로
            title_ko, summary = None, [a["description"]] if a["description"] else []
        items.append({
            "title": a["title"],
            "title_ko": title_ko,
            "summary": summary,
            "ai_summary": result is not None,
            "link": a["link"],
            "published": a["published"].isoformat() if a["published"] else None,
            "group": a["group"],
        })
    return items


def save_news(items, updated_at):
    data = {
        "mode": "practice" if PRACTICE_MODE else "ai",
        "model": None if PRACTICE_MODE else MODEL,
        "updated_at": updated_at.isoformat(),
        "keywords": KEYWORD_GROUPS,
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
        <span class="tag">{esc(it["group"])}</span>
        <time>{esc(format_kst(it["published"]))}</time>
      </div>"""
    link = (f'<a class="link" href="{esc(it["link"], quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">원문 보기</a>')

    if it["ai_summary"]:
        lis = "".join(f"<li>{esc(s)}</li>" for s in it["summary"])
        body = f"""
      <h2>{esc(it["title_ko"])}</h2>
      <p class="orig">{esc(it["title"])}</p>
      <ul class="summary">{lis}</ul>"""
    else:
        desc = it["summary"][0] if it["summary"] else "(소개문 없음)"
        body = f"""
      <h2 lang="en">{esc(it["title"])}</h2>
      <p class="desc" lang="en">{esc(desc)}</p>"""

    return f'\n    <article class="card">{head}{body}\n      {link}\n    </article>'


def render_html(data):
    esc = html.escape
    cards = [render_card(it) for it in data["items"]]
    if not cards:
        cards.append('<p class="empty">오늘은 키워드에 맞는 기사가 없습니다.</p>')

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
    <p class="sub">기준 시각 {esc(format_kst(data["updated_at"]))} (한국 시간) · 출처 CNBC</p>
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
    articles = collect_articles()
    print(f"키워드에 맞는 기사: {len(articles)}개")
    selected = select_articles(articles)
    items = build_items(selected)
    data = save_news(items, datetime.now(KST))
    INDEX_HTML.write_text(render_html(data), encoding="utf-8")

    ok = sum(1 for it in items if it["ai_summary"])
    print(f"완료: {len(items)}개 기사 (AI 요약 {ok}개) -> {NEWS_JSON.name}, {INDEX_HTML.name}")
    for it in items:
        print(f"  [{it['group']}] {it['title_ko'] or it['title']}")


if __name__ == "__main__":
    main()
