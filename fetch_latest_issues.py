#!/usr/bin/env python3
"""
抓取學術期刊「最新一期」的目錄 (Table of Contents)；可產生公開網頁與 RSS
Fetch the latest-issue table of contents for film & media studies journals.

資料來源：Crossref REST API（免費、免金鑰）。只用 Python 標準函式庫，不需安裝套件。

用法 Usage
  python3 fetch_latest_issues.py                       # 用 journals.csv 的期刊清單
  python3 fetch_latest_issues.py --mailto you@mail.com # 建議加上，Crossref 會給較快的「禮貌通道」
  python3 fetch_latest_issues.py --sjr scimagojr.csv   # 用 SCImago 下載的 CSV 自動建立 Q1 清單
  python3 fetch_latest_issues.py --only "Screen,Film Quarterly"

輸出 Output (預設在 ./output/)
  latest_issues_YYYY-MM-DD.html   可瀏覽、可搜尋的報告，自上次執行後有新一期會標 NEW
  latest_issues_YYYY-MM-DD.csv    每篇文章一列，可用 Excel 開
  latest_issues_YYYY-MM-DD.json   原始整理結果
  state.json                      記錄每本期刊上次看到的期別，用來判斷 NEW
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://api.crossref.org"
TITLE = "電影與媒體研究期刊"  # 由 --title 覆寫
ROWS = 300  # 每本期刊抓最近 300 筆，足以涵蓋最新一期與線上優先文章
SELECT = ",".join([
    "DOI", "title", "subtitle", "author", "volume", "issue", "page", "type",
    "published-print", "published-online", "issued", "container-title", "URL", "abstract",
])

# 用於 --sjr 篩選：期刊名稱含這些字才算「電影與媒體研究」
FILM_MEDIA_KEYWORDS = [
    "film", "cinema", "screen", "television", "tv", "media", "video",
    "animation", "documentary", "moving image", "broadcast", "game",
    "visual culture", "celebrity", "porn", "radio", "sound", "digital culture",
    "communication", "popular culture",
]
SJR_CATEGORIES = ["Visual Arts and Performing Arts", "Communication", "Cultural Studies"]


# ---------------------------------------------------------------- 期刊清單

def norm_issn(s: str) -> str:
    s = re.sub(r"[^0-9Xx]", "", s or "").upper()
    return f"{s[:4]}-{s[4:]}" if len(s) == 8 else ""


def load_journals_csv(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            issns = [norm_issn(row.get("issn_print", "")), norm_issn(row.get("issn_online", ""))]
            issns = [i for i in issns if i]
            if row.get("name") and (issns or row.get("source_id")):
                out.append({
                    "name": row["name"].strip(),
                    "issns": issns,
                    "publisher": (row.get("publisher") or "").strip(),
                    "field": (row.get("field") or "").strip(),
                    "sjr_note": (row.get("sjr_note") or "").strip(),
                    "source": (row.get("source") or "crossref").strip().lower(),
                    "review": (row.get("review") or "").strip(),
                    "lang": (row.get("lang") or "").strip(),
                    "source_id": (row.get("source_id") or "").strip(),
                })
    return out


def load_journals_sjr(path: Path, categories: list[str], use_keywords: bool) -> list[dict]:
    """讀取 SCImago (scimagojr.com) 匯出的 CSV（分號分隔），挑出指定學門為 Q1 的期刊。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines(), delimiter=";")
    out = []
    for row in reader:
        if (row.get("Type") or "journal").strip().lower() != "journal":
            continue
        cats = row.get("Categories") or ""
        q1_cats = [c for c in categories if re.search(re.escape(c) + r"\s*\(Q1\)", cats)]
        if not q1_cats:
            continue
        title = (row.get("Title") or "").strip()
        if use_keywords and not any(k in title.lower() for k in FILM_MEDIA_KEYWORDS):
            continue
        issns = [norm_issn(x) for x in (row.get("Issn") or "").split(",")]
        issns = [i for i in issns if i]
        if not issns:
            continue
        out.append({
            "name": title,
            "issns": issns,
            "publisher": (row.get("Publisher") or "").strip(),
            "field": "; ".join(q1_cats),
            "sjr_note": f"SJR {row.get('SJR', '')}".strip(),
        })
    return out


def write_journals_csv(journals: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "issn_print", "issn_online", "publisher", "field", "sjr_note"])
        for j in journals:
            issns = j["issns"] + [""]
            w.writerow([j["name"], issns[0], issns[1] if len(issns) > 2 else "", j["publisher"],
                        j["field"], j.get("sjr_note", "")])


# ---------------------------------------------------------------- Crossref

class Crossref:
    def __init__(self, mailto: str | None, timeout: int = 40, retries: int = 4):
        ua = "film-media-latest-issues/1.0 (https://github.com/)"
        if mailto:
            ua += f" (mailto:{mailto})"
        self.headers = {"User-Agent": ua}
        self.mailto = mailto
        self.timeout = timeout
        self.retries = retries

    def get(self, path: str, params: dict) -> dict:
        if self.mailto:
            params = {**params, "mailto": self.mailto}
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        delay = 2.0
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise LookupError("not found in Crossref") from e
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(delay); delay *= 2; continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < self.retries:
                    time.sleep(delay); delay *= 2; continue
                raise
        raise RuntimeError("unreachable")

    def recent_works(self, issn: str) -> list[dict]:
        data = self.get(f"/journals/{issn}/works", {
            "sort": "published", "order": "desc", "rows": ROWS, "select": SELECT,
        })
        return data.get("message", {}).get("items", [])


# ---------------------------------------------------------------- 整理資料

def date_parts(item: dict, *keys: str) -> tuple[int, int, int] | None:
    for k in keys:
        dp = (item.get(k) or {}).get("date-parts") or [[]]
        p = [x for x in (dp[0] or []) if x is not None]
        if p:
            p = (p + [0, 0])[:3]  # 只有年或年月時補 0
            return (int(p[0]), int(p[1]), int(p[2]))
    return None


def fmt_date(t: tuple[int, int, int] | None) -> str:
    if not t:
        return ""
    y, m, d = t
    return f"{y}" + (f"-{m:02d}" if m else "") + (f"-{d:02d}" if d else "")


def num(s) -> float:
    """'12' -> 12, '3-4' -> 3, 'S1' -> 0；用於期別排序。"""
    m = re.search(r"\d+", str(s or ""))
    return float(m.group()) if m else 0.0


def clean_abstract(s: str) -> str:
    """Crossref 摘要是 JATS XML：去標籤、去掉開頭的 Abstract 字樣。"""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = " ".join(s.split())
    return re.sub(r"^(abstract|summary)[:.\s]+", "", s, flags=re.I)


def simplify(item: dict) -> dict:
    authors = []
    for a in item.get("author") or []:
        name = " ".join(x for x in [a.get("given"), a.get("family")] if x) or a.get("name", "")
        if name:
            authors.append(name)
    title = " ".join((item.get("title") or [""])[0].split())
    sub = (item.get("subtitle") or [""])
    if sub and sub[0] and sub[0].lower() not in title.lower():
        title += ": " + " ".join(sub[0].split())
    return {
        "title": html.unescape(re.sub(r"<[^>]+>", "", title)) or "(untitled)",
        "authors": authors,
        "doi": item.get("DOI", ""),
        "url": item.get("URL") or (f"https://doi.org/{item['DOI']}" if item.get("DOI") else ""),
        "volume": str(item.get("volume") or ""),
        "issue": str(item.get("issue") or ""),
        "page": str(item.get("page") or ""),
        "date_print": fmt_date(date_parts(item, "published-print")),
        "date_online": fmt_date(date_parts(item, "published-online")),
        "type": item.get("type", ""),
        "abstract": clean_abstract(item.get("abstract", "") or ""),
    }


SKIP_TITLES = re.compile(r"^(issue information|front matter|back matter|cover|editorial board|"
                         r"table of contents|contents|masthead|ifc|ibc|ofc|obc)\b", re.I)


def pick_latest_issue(items: list[dict]) -> tuple[dict | None, list[dict]]:
    """從近期文章中找出「最新一期」。回傳 (期別資訊, 線上優先文章)。"""
    items = [i for i in items if i.get("type") in ("journal-article", None, "")]
    groups: dict[tuple[str, str], list[dict]] = {}
    online_first = []
    for it in items:
        vol, iss = str(it.get("volume") or "").strip(), str(it.get("issue") or "").strip()
        if iss or vol:
            groups.setdefault((vol, iss), []).append(it)
        else:
            online_first.append(it)
    if not groups:
        return None, online_first

    def key(kv):
        (vol, iss), members = kv
        d = max((date_parts(m, "published-print", "issued") or (0, 0, 0)) for m in members)
        return (d, num(vol), num(iss))

    def build(vol, iss, members):
        issue_date = max((date_parts(m, "published-print", "issued") or (0, 0, 0)) for m in members)
        arts = [simplify(m) for m in members]
        arts = [a for a in arts if not SKIP_TITLES.match(a["title"])]
        arts.sort(key=lambda a: (num(a["page"].split("-")[0]) if a["page"] else 1e9, a["title"]))
        return {"volume": vol, "issue": iss, "date": fmt_date(issue_date), "articles": arts,
                "container_title": (members[0].get("container-title") or [""])[0]}

    ordered = sorted(groups.items(), key=key, reverse=True)
    latest = build(*ordered[0][0], ordered[0][1])
    # 之後幾期（較舊）也保留，用來補上「兩次執行之間出了不只一期」時被跳過的期別
    latest["_older"] = [build(v, i, m) for (v, i), m in ordered[1:9]]
    return latest, online_first


def fill_abstracts_openalex(results: list[dict], mailto: str | None) -> int:
    """Crossref 沒有摘要的文章（如 Taylor & Francis、Oxford），改向 OpenAlex 補抓。"""
    missing = {}
    for r in results:
        arts = ((r.get("latest") or {}).get("articles") or []) + (r.get("online_first") or [])
        for e in r.get("earlier_new") or []:
            arts += e["articles"]
        for a in arts:
            if a.get("doi") and not a.get("abstract"):
                missing.setdefault(a["doi"].lower(), []).append(a)
    dois, filled = list(missing), 0
    for i in range(0, len(dois), 40):
        chunk = dois[i:i + 40]
        params = {"filter": "doi:" + "|".join(chunk), "per-page": 50,
                  "select": "doi,abstract_inverted_index"}
        if mailto:
            params["mailto"] = mailto
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "journal-tracker"}),
                                        timeout=60) as resp:
                data = json.load(resp)
        except Exception as e:  # noqa: BLE001
            print(f"OpenAlex 補摘要失敗（略過）：{type(e).__name__}: {e}", file=sys.stderr)
            continue
        for w in data.get("results", []):
            inv = w.get("abstract_inverted_index") or {}
            if not inv:
                continue
            pos = sorted((p_, word) for word, ps in inv.items() for p_ in ps)
            text = clean_abstract(" ".join(word for _, word in pos))
            doi = (w.get("doi") or "").lower().replace("https://doi.org/", "")
            for a in missing.get(doi, []):
                a["abstract"] = text
                filled += 1
        time.sleep(0.2)
    return filled


def cinii_works(issn: str, mailto: str | None = None) -> list[dict]:
    """日文期刊：CiNii Research OpenSearch（不需金鑰）。轉成與 Crossref 相同的欄位格式。"""
    params = {"issn": issn, "format": "json", "count": 200, "sortorder": 0}
    url = "https://cir.nii.ac.jp/opensearch/articles?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "journal-tracker" + (f" (mailto:{mailto})" if mailto else "")})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    out = []
    for it in data.get("items") or []:
        def first(v):
            if isinstance(v, list):
                v = v[0] if v else ""
            if isinstance(v, dict):
                v = v.get("@value") or v.get("@id") or ""
            return str(v or "")
        d = first(it.get("prism:publicationDate"))
        parts = [int(x) for x in re.findall(r"\d+", d)[:3]]
        creators = it.get("dc:creator") or []
        if not isinstance(creators, list):
            creators = [creators]
        doi = first(it.get("prism:doi"))
        out.append({
            "title": [first(it.get("title"))],
            "volume": first(it.get("prism:volume")) or None,
            "issue": first(it.get("prism:number")) or None,
            "page": "-".join(x for x in [first(it.get("prism:startingPage")), first(it.get("prism:endingPage"))] if x),
            "published-print": {"date-parts": [parts]} if parts else None,
            "author": [{"name": first(c)} for c in creators if first(c)],
            "DOI": doi if doi.startswith("10.") else "",
            "URL": first(it.get("link")) or first(it.get("@id")),
            "abstract": first(it.get("description")),
            "type": "journal-article",
        })
    return out


def openalex_works(source_id: str, mailto: str | None = None) -> list[dict]:
    """沒有 Crossref/CiNii 資料的期刊（例如部分韓國期刊）：用 OpenAlex 的期刊 ID（S 開頭）抓最新文章。"""
    params = {"filter": f"primary_location.source.id:{source_id}", "sort": "publication_date:desc", "per-page": 200,
              "select": "id,doi,title,publication_date,biblio,authorships,abstract_inverted_index,type"}
    if mailto:
        params["mailto"] = mailto
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "journal-tracker"}), timeout=60) as r:
        data = json.load(r)
    out = []
    for w in data.get("results", []):
        b = w.get("biblio") or {}
        parts = [int(x) for x in re.findall(r"\d+", w.get("publication_date") or "")[:3]]
        inv = w.get("abstract_inverted_index") or {}
        abstract = " ".join(word for _, word in sorted((p_, word) for word, ps in inv.items() for p_ in ps))
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        out.append({
            "title": [w.get("title") or ""], "volume": b.get("volume"), "issue": b.get("issue"),
            "page": "-".join(x for x in [b.get("first_page") or "", b.get("last_page") or ""] if x),
            "published-print": {"date-parts": [parts]} if parts else None,
            "author": [{"name": (a.get("author") or {}).get("display_name", "")} for a in w.get("authorships") or []],
            "DOI": doi, "URL": w.get("doi") or w.get("id"), "abstract": abstract, "type": "journal-article",
        })
    return out


def fetch_journal(cr: Crossref, j: dict) -> dict:
    res = {**j, "status": "ok", "error": "", "latest": None, "online_first": []}
    errors = []
    merged: dict[str, dict] = {}
    # 兩個 ISSN 都查再合併：有些出版社把舊期別登記在印刷版、新期別登記在電子版
    # （例如 Film History、Framework、The Velvet Light Trap）
    ids = [j["source_id"]] if j.get("source") == "openalex" else j["issns"]
    for issn in ids:
        try:
            items = openalex_works(issn, cr.mailto) if j.get("source") == "openalex" else cinii_works(issn, cr.mailto) if j.get("source") == "cinii" else cr.recent_works(issn)
        except LookupError as e:
            errors.append(f"{issn}: {e}"); continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{issn}: {type(e).__name__}: {e}"); continue
        for it in items:
            merged.setdefault(it.get("DOI") or id(it), it)
    if not merged:
        res["status"], res["error"] = "error", "; ".join(errors) or "no works"
        return res
    latest, of = pick_latest_issue(list(merged.values()))
    if not latest:
        res["status"], res["error"] = "error", "no volume/issue info in Crossref"
        return res
    res["latest"] = latest
    cutoff = latest["date"]
    of = [s for s in map(simplify, of) if (s["date_online"] or s["date_print"]) >= cutoff]
    of.sort(key=lambda s: s["date_online"] or s["date_print"], reverse=True)
    res["online_first"] = of[:30]
    return res


# ---------------------------------------------------------------- 輸出

def issue_label(lt: dict) -> str:
    parts = []
    if lt["volume"]:
        parts.append(f"Vol. {lt['volume']}")
    if lt["issue"]:
        parts.append(f"No. {lt['issue']}")
    return ", ".join(parts) or "(no volume/issue)"


def write_csv(results: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig 讓 Excel 正確顯示
        w = csv.writer(f)
        w.writerow(["journal", "volume", "issue", "issue_date", "is_new", "section",
                    "title", "authors", "pages", "doi", "url", "abstract"])
        for r in results:
            lt = r.get("latest")
            if not lt:
                continue
            for a in lt["articles"]:
                w.writerow([r["name"], lt["volume"], lt["issue"], lt["date"], "Y" if r.get("is_new") else "",
                            "issue", a["title"], "; ".join(a["authors"]), a["page"], a["doi"], a["url"], a.get("abstract", "")])
            for e in r.get("earlier_new") or []:
                for a in e["articles"]:
                    w.writerow([r["name"], e["volume"], e["issue"], e["date"], "Y", "issue (earlier new)",
                                a["title"], "; ".join(a["authors"]), a["page"], a["doi"], a["url"], a.get("abstract", "")])
            for a in r.get("online_first", []):
                w.writerow([r["name"], "", "", a["date_online"], "", "online first",
                            a["title"], "; ".join(a["authors"]), "", a["doi"], a["url"], a.get("abstract", "")])


def esc(s) -> str:
    return html.escape(str(s or ""))


def article_li(a: dict) -> str:
    au = ", ".join(a["authors"][:6]) + (" et al." if len(a["authors"]) > 6 else "")
    meta = " · ".join(x for x in [au, f"pp. {a['page']}" if a["page"] else "",
                                  a["date_online"] and f"online {a['date_online']}"] if x)
    return (f'<li><a href="{esc(a["url"])}" target="_blank" rel="noopener">{esc(a["title"])}</a>'
            f'<div class="meta">{esc(meta)}</div>'
            + (f'<details class="abs"><summary>摘要</summary><p>{esc(a["abstract"])}</p></details>'
               if a.get("abstract") else "") + '</li>')


def name_key(name: str) -> tuple:
    """刊名排序：忽略開頭的 The／A，不分大小寫；拉丁字母刊名在前，日、韓文刊名在後。"""
    n = re.sub(r"^(the|a|an)\s+", "", name.strip(), flags=re.I).casefold()
    return (not n[:1].isascii(), n)


def write_html(results: list[dict], path: Path, generated: str, top_extra: str = "") -> None:
    ok = [r for r in results if r.get("latest")]
    bad = [r for r in results if not r.get("latest")]
    ok.sort(key=lambda r: name_key(r["name"]))  # 刊名 A–Z（日、韓文刊名排在最後）
    n_new = sum(1 for r in ok if r.get("is_new"))
    cards = []
    for r in ok:
        lt = r["latest"]
        badge = '<span class="new">NEW</span>' if r.get("is_new") else ""
        of = r.get("online_first") or []
        of_html = ""
        if of:
            of_html = (f'<details class="of"><summary>Online first（尚未編入期別）· {len(of)}</summary>'
                       f'<ol>{"".join(article_li(a) for a in of)}</ol></details>')
        cards.append(f"""
<section class="card" data-search="{esc((r['name'] + ' ' + ' '.join(a['title'] + ' ' + ' '.join(a['authors']) for a in lt['articles'] + of)).lower())}">
  <details {'open' if r.get('is_new') else ''}>
    <summary>
      <div class="head"><h2>{esc(r['name'])}</h2>{badge}</div>
      <div class="sub">{esc(issue_label(lt))} · {esc(lt['date'] or '日期不詳')} · {len(lt['articles'])} 篇
        <span class="dim">· {esc(r.get('publisher',''))} · {esc(r.get('field',''))}{(' · ' + esc(r['review'])) if r.get('review') else ''}</span></div>
    </summary>
    <ol>{''.join(article_li(a) for a in lt['articles'])}</ol>
    {''.join(f'<details class="of" open><summary>同樣是上次之後的新期別：{esc(issue_label(e))} · {esc(e["date"])} · {len(e["articles"])} 篇</summary><ol>{"".join(article_li(a) for a in e["articles"])}</ol></details>' for e in r.get('earlier_new') or [])}
    {of_html}
  </details>
</section>""")
    errs = "".join(f"<li><b>{esc(r['name'])}</b> — {esc(r['error'])}</li>" for r in bad)
    page = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(TITLE)} 最新一期</title>
<style>
:root{{--bg:#f7f5f0;--card:#fff;--ink:#1d1c1a;--dim:#6b665e;--line:#e4e0d6;--accent:#b4441c;--link:#1d4f91}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151412;--card:#1f1d1a;--ink:#ece8df;--dim:#9a948a;--line:#34312b;--accent:#f08a5d;--link:#8db4ea}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Noto Sans TC",sans-serif}}
main{{max-width:920px;margin:0 auto;padding:32px 16px 80px}}
h1{{font:600 26px/1.2 Georgia,"Noto Serif TC",serif;margin:0 0 6px}}
.top{{color:var(--dim);margin-bottom:18px}}
input{{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit;margin-bottom:18px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:10px}}
summary{{cursor:pointer;padding:14px 16px;list-style:none}}summary::-webkit-details-marker{{display:none}}
.head{{display:flex;gap:10px;align-items:center}}h2{{font:600 17px/1.3 Georgia,"Noto Serif TC",serif;margin:0}}
.sub{{color:var(--ink);font-size:13.5px;margin-top:3px}}.dim{{color:var(--dim)}}
.new{{background:var(--accent);color:#fff;font-size:11px;font-weight:700;padding:2px 7px;border-radius:99px;letter-spacing:.05em}}
ol{{margin:0;padding:0 18px 14px 42px}}li{{margin:0 0 9px}}a{{color:var(--link);text-decoration:none}}a:hover{{text-decoration:underline}}
.meta{{color:var(--dim);font-size:13px}}
.abs summary{{padding:2px 0;font-size:12.5px;color:var(--dim)}}.abs p{{margin:4px 0 6px;font-size:13.5px;color:var(--ink)}}
.of summary{{padding:6px 16px 10px;color:var(--dim);font-size:13.5px}}
.errs{{color:var(--dim);font-size:13.5px;margin-top:28px}}
</style></head><body><main>
<h1>{esc(TITLE)} · 最新一期</h1>
{top_extra}
<div class="top">產生時間 {esc(generated)} · {len(ok)} 本期刊 · 自上次執行後有新一期：{n_new} 本 · 資料來源 Crossref、CiNii、OpenAlex</div>
<input id="q" placeholder="搜尋期刊、標題或作者…" autocomplete="off">
<div id="list">{''.join(cards)}</div>
{f'<div class="errs"><b>無法取得（{len(bad)}）</b><ul>{errs}</ul></div>' if bad else ''}
</main><script>
const q=document.getElementById('q');q.addEventListener('input',()=>{{const v=q.value.trim().toLowerCase();
document.querySelectorAll('.card').forEach(c=>{{const hit=!v||c.dataset.search.includes(v);c.style.display=hit?'':'none';
if(v&&hit)c.querySelector('details').open=true;}});}});
</script></body></html>"""
    path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- 公開網頁與 RSS

def subscribe_box(site_url: str) -> str:
    feed = site_url.rstrip("/") + "/feed.xml"
    return (f'<div style="background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;'
            f'margin:0 0 18px;font-size:14px"><b>訂閱 / Subscribe</b>：把這個 RSS 網址加到 Feedly、Inoreader 等閱讀器，'
            f'有新一期就會收到通知 · <a href="{esc(feed)}">{esc(feed)}</a><br>'
            f'<span class="dim">想收 email？用 Blogtrottr 或 Feedrabbit 之類的免費服務，貼上同一個 RSS 網址即可。'
            f' · <a href="archive/">過去的報告</a></span></div>')


def rfc822(d: dt.datetime) -> str:
    return d.strftime("%a, %d %b %Y %H:%M:%S +0000")


def write_site(results: list[dict], site: Path, site_url: str, generated: str, today: str,
               first_run: bool, report_html: Path) -> int:
    """docs/index.html（最新報告）、docs/archive/、docs/feed.xml（RSS 2.0）。回傳新增的 RSS 項目數。"""
    import shutil
    (site / "archive").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_html, site / "index.html")
    shutil.copyfile(report_html, site / "archive" / f"{today}.html")
    reports = sorted((site / "archive").glob("20*.html"), reverse=True)
    links = "".join(f'<li><a href="{f.name}">{f.stem}</a></li>' for f in reports)
    (site / "archive" / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(TITLE)} · 過去的報告</title><body style="font:15px/1.6 system-ui;max-width:720px;margin:32px auto;padding:0 16px">'
        f'<h1 style="font:600 22px Georgia,serif">{esc(TITLE)} · 過去的報告</h1><ul>{links}</ul>'
        f'<p><a href="../">← 最新一期</a></p></body>', encoding="utf-8")

    items_path = site / "feed_items.json"
    try:
        items = json.loads(items_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        items = []
    seen = {i["guid"] for i in items}
    now = rfc822(dt.datetime.now(dt.timezone.utc))
    new_items = []
    for r in results:
        lt = r.get("latest")
        if not lt or not (r.get("is_new") or first_run):
            continue
        for iss in [lt] + list(r.get("earlier_new") or []):
            guid = f"{r['name']}|{iss['volume']}|{iss['issue']}"
            if guid in seen:
                continue
            toc = "".join(
                f'<li><a href="{esc(a["url"])}">{esc(a["title"])}</a>'
                f'{(" — " + esc(", ".join(a["authors"][:4]))) if a["authors"] else ""}</li>' for a in iss["articles"])
            new_items.append({"guid": guid, "title": f"{r['name']} — {issue_label(iss)} ({iss['date']})",
                              "link": site_url.rstrip("/") + "/archive/" + today + ".html",
                              "description": f"<ol>{toc}</ol>", "pubDate": now})
    items = (new_items + items)[:300]
    items_path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    xml_items = "".join(
        f"<item><title>{esc(i['title'])}</title><link>{esc(i['link'])}</link>"
        f"<guid isPermaLink=\"false\">{esc(i['guid'])}</guid><pubDate>{i['pubDate']}</pubDate>"
        f"<description>{esc(i['description'])}</description></item>" for i in items)
    (site / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{esc(TITLE)} · 最新一期</title><link>{esc(site_url)}</link>"
        f"<description>{esc(TITLE)}：每本期刊出新一期時發佈一則（目錄附連結）</description>"
        f"<language>zh-Hant</language><lastBuildDate>{now}</lastBuildDate>{xml_items}</channel></rss>",
        encoding="utf-8")
    (site / ".nojekyll").write_text("")
    return len(new_items)


# ---------------------------------------------------------------- Email

def build_email_html(results: list[dict], generated: str, first_run: bool, site_url: str = "") -> str:
    """寄信用的 HTML：全部 inline style（Gmail 會移除 <style> 與 <details>）。"""
    ok = [r for r in results if r.get("latest")]
    new = [r for r in ok if r.get("is_new")]
    same = [r for r in ok if not r.get("is_new")]
    bad = [r for r in results if not r.get("latest")]
    full = ok if first_run else new  # 第一次執行：全部列目錄；之後：只列有新一期的

    def toc(r):
        return "".join(toc_one(r, lt, i == 0) for i, lt in enumerate([r["latest"]] + (r.get("earlier_new") or [])))

    def toc_one(r, lt, head):
        items = "".join(
            f'<li style="margin:0 0 6px"><a href="{esc(a["url"])}" style="color:#1d4f91;text-decoration:none">'
            f'{esc(a["title"])}</a><br><span style="color:#6b665e;font-size:13px">'
            f'{esc(", ".join(a["authors"][:6]) + (" et al." if len(a["authors"]) > 6 else ""))}</span></li>'
            for a in lt["articles"])
        tag = (' <span style="background:#b4441c;color:#fff;font-size:11px;padding:1px 6px;'
               'border-radius:9px">NEW</span>') if r.get("is_new") else ""
        h3 = f'<h3 style="font:600 16px Georgia,serif;margin:22px 0 2px">{esc(r["name"])}{tag}</h3>' if head else ""
        return (h3 + f'<div style="color:#6b665e;font-size:13px;margin-bottom:6px">{esc(issue_label(lt))} · '
                f'{esc(lt["date"])} · {len(lt["articles"])} 篇</div><ol style="padding-left:20px;margin:0">{items}</ol>')

    if first_run:
        intro = f"這是第一次執行，以下列出 {len(ok)} 本期刊目前的最新一期。之後的信只會詳列有新一期的期刊。"
    else:
        intro = f"自上次執行以來，{len(new)} 本期刊出了新一期（共檢查 {len(ok)} 本）。"
    rest = ""
    if not first_run and same:
        rows = "".join(f'<li>{esc(r["name"])} — {esc(issue_label(r["latest"]))} ({esc(r["latest"]["date"])})</li>'
                       for r in same)
        rest = (f'<h3 style="font:600 15px Georgia,serif;margin:28px 0 4px;color:#6b665e">沒有新一期</h3>'
                f'<ul style="color:#6b665e;font-size:13px;padding-left:20px">{rows}</ul>')
    errs = ""
    if bad:
        errs = ('<p style="color:#6b665e;font-size:13px;margin-top:24px">無法取得：'
                + esc("、".join(r["name"] for r in bad)) + "</p>")
    return (f'<div style="font:15px/1.5 -apple-system,Helvetica,Arial,sans-serif;color:#1d1c1a;max-width:720px">'
            f'<h2 style="font:600 22px Georgia,serif;margin:0 0 6px">{esc(TITLE)} · 最新一期</h2>'
            f'<p style="color:#6b665e;margin:0 0 8px">{esc(generated)} · {esc(intro)}</p>'
            f'{"".join(toc(r) for r in full)}{rest}{errs}'
            + (f'<p style="margin-top:24px">網頁版（所有期刊最新一期與文章連結）：<a href="{esc(site_url)}" style="color:#1d4f91">{esc(site_url)}</a></p>' if site_url else "")
            + f'<p style="color:#6b665e;font-size:12px;margin-top:28px">完整報告與 CSV 見附件。資料來源 Crossref。</p></div>')


def send_email(cfg: dict, subject: str, html_body: str, attachments: list[Path]) -> None:
    import mimetypes
    import smtplib
    import ssl
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from") or cfg["username"]
    to = cfg.get("to") or cfg["username"]
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg.set_content("請用支援 HTML 的郵件程式開啟本信；完整報告見附件。")
    msg.add_alternative(html_body, subtype="html")
    for p in attachments:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)

    host, port = cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 465))
    password = cfg["app_password"].replace(" ", "")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(cfg["username"], password); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx); s.login(cfg["username"], password); s.send_message(msg)


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    global TITLE
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Fetch the latest issues of academic journals (Crossref, CiNii, OpenAlex).")
    p.add_argument("--journals", type=Path, default=here / "journals.csv", help="期刊清單 CSV")
    p.add_argument("--sjr", type=Path, help="SCImago 匯出的 CSV；提供時改用其中 Q1 期刊")
    p.add_argument("--sjr-categories", default=",".join(SJR_CATEGORIES),
                   help="要篩 Q1 的 SJR 學門，逗號分隔")
    p.add_argument("--no-keyword-filter", action="store_true", help="--sjr 時不以名稱關鍵字篩選")
    p.add_argument("--only", help="只抓名稱包含這些字的期刊，逗號分隔")
    p.add_argument("--mailto", help="你的 email（Crossref 禮貌通道，較快較穩）")
    p.add_argument("--out", type=Path, default=here / "output", help="輸出資料夾")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--email", action="store_true", help="跑完後依 email_config.json 寄出結果")
    p.add_argument("--config", type=Path, default=here / "email_config.json", help="寄信設定檔")
    p.add_argument("--title", default=TITLE, help="報告標題（例如：考古學期刊）")
    p.add_argument("--site", type=Path, help="輸出公開網頁的資料夾（例如 docs，給 GitHub Pages 用）")
    p.add_argument("--site-url", default="", help="公開網頁網址（RSS 連結用）")
    a = p.parse_args(argv)
    TITLE = a.title

    cfg = {}
    if a.email:
        try:
            cfg = json.loads(a.config.read_text(encoding="utf-8"))
            assert cfg.get("username") and cfg.get("app_password")
        except Exception:  # noqa: BLE001
            print(f"找不到或無法讀取寄信設定 {a.config}（需要 username 與 app_password）。"
                  "請先執行 setup_schedule.sh。", file=sys.stderr)
            return 2
        a.mailto = a.mailto or cfg.get("username")

    if a.sjr:
        cats = [c.strip() for c in a.sjr_categories.split(",") if c.strip()]
        journals = load_journals_sjr(a.sjr, cats, not a.no_keyword_filter)
        a.out.mkdir(parents=True, exist_ok=True)
        write_journals_csv(journals, a.out / "journals_from_sjr.csv")
        print(f"從 SJR 檔篩出 {len(journals)} 本 Q1 期刊（清單已存到 {a.out / 'journals_from_sjr.csv'}，可手動增刪後用 --journals 指定）")
    else:
        journals = load_journals_csv(a.journals)
    if a.only:
        keys = [k.strip().lower() for k in a.only.split(",") if k.strip()]
        journals = [j for j in journals if any(k in j["name"].lower() for k in keys)]
    if not journals:
        print("沒有期刊可抓。", file=sys.stderr)
        return 1

    a.out.mkdir(parents=True, exist_ok=True)
    state_path = a.out / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    prev_state = dict(state)

    cr = Crossref(a.mailto)
    results = []
    print(f"抓取 {len(journals)} 本期刊…")
    had_state = bool(state)  # 已執行過：之後新加入的期刊，第一次出現就算 NEW（會進 RSS）
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(fetch_journal, cr, j): j for j in journals}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            lt = r.get("latest")
            if lt:
                sig = f"{lt['volume']}|{lt['issue']}"
                prev = state.get(r["name"])
                r["is_new"] = (prev != sig) if prev is not None else had_state
                older = lt.pop("_older", [])
                r["earlier_new"] = []
                if r["is_new"]:
                    # 上次看到的期別之後、最新一期之前的期別也算新的（避免兩次執行間出兩期時漏掉）
                    for o in older:
                        if f"{o['volume']}|{o['issue']}" == prev:
                            break
                        r["earlier_new"].append(o)
                    else:
                        r["earlier_new"] = []  # 找不到上次的期別：只報最新一期，避免誤報
                    r["earlier_new"] = r["earlier_new"][:6]
                state[r["name"]] = sig
                flag = ("  ★NEW" + (f"（另有 {len(r['earlier_new'])} 期較早的新期別）" if r["earlier_new"] else "")) if r["is_new"] else ""
                print(f"[{i:>3}/{len(journals)}] {r['name']}: {issue_label(lt)} ({lt['date']}) "
                      f"{len(lt['articles'])} 篇{flag}")
            else:
                print(f"[{i:>3}/{len(journals)}] {r['name']}: 失敗 — {r['error']}")
            results.append(r)

    results.sort(key=lambda r: name_key(r["name"]))  # 刊名 A–Z
    n_filled = fill_abstracts_openalex(results, a.mailto)
    if n_filled:
        print(f"從 OpenAlex 補上 {n_filled} 篇摘要")
    today = dt.date.today().isoformat()
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    base = a.out / f"latest_issues_{today}"
    write_html(results, base.with_suffix(".html"), generated,
               subscribe_box(a.site_url) if a.site and a.site_url else "")
    write_csv(results, base.with_suffix(".csv"))
    base.with_suffix(".json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n完成：{base.with_suffix('.html')}")
    if a.site:
        n_items = write_site(results, a.site, a.site_url, generated, today, not prev_state, base.with_suffix(".html"))
        print(f"公開網頁已更新：{a.site}（RSS 新增 {n_items} 則）")

    if a.email:
        first_run = not prev_state
        n_new = sum(1 for r in results if r.get("is_new"))
        n_ok = sum(1 for r in results if r.get("latest"))
        subject = (f"[{TITLE}] {today} 第一次執行：{n_ok} 本期刊最新一期" if first_run
                   else f"[{TITLE}] {today}：{n_new} 本期刊有新一期")
        try:
            send_email(cfg, subject, build_email_html(results, generated, first_run, a.site_url or ""),
                       [base.with_suffix(".html"), base.with_suffix(".csv")])
            print(f"已寄出到 {cfg.get('to') or cfg['username']}")
        except Exception as e:  # noqa: BLE001
            hint = ("（Gmail 不接受帳號／應用程式密碼：GMAIL_USER 必須是產生該應用程式密碼的那個 Gmail 帳號）"
                    if "535" in str(e) or "Disconnected" in type(e).__name__ else "")
            # 寄信失敗不影響資料與網頁更新；在 GitHub Actions 頁面顯示錯誤註記
            print(f"::error::寄信失敗：{type(e).__name__}: {e}{hint}")
            print(f"寄信失敗：{type(e).__name__}: {e}{hint}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
