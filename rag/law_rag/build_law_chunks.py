"""도로교통법 PDF(국가법령정보센터 출력본)를 RAG용 청크로 만든다.

사용법:
    python build_chunks.py <도로교통법.pdf> [출력폴더] [--max-chars 2000] [--include-addenda]

필요: Python 3.9+, pypdf  (pip install pypdf)

출력(출력폴더 기준):
    chunks.jsonl      벡터DB 적재용 (한 줄에 청크 하나)
    chunks/*.md       사람이 확인하기 위한 파일 (YAML front matter + 본문)
    index.md          장 > 조문 > 청크 파일 색인

청크 규칙
    - 기본 단위는 "조(條)" 하나. 목차(1~6쪽)와 부칙은 제외한다.
    - 조문이 --max-chars 를 넘으면 항(①②③...) 경계에서 나누고,
      한 항이 너무 길면 호(1. 2. 3....) 경계에서 나눈다.
    - 쪼개진 청크에도 장·조 제목을 머리글로 반복해 단독으로 읽어도 맥락이 유지된다.
    - 개정 이력 표시(<개정 ...>, [본조신설 ...] 등)는 본문에서 빼고 amendments 필드에 보관한다.
      단, [시행일: ...] 안내는 본문에 남긴다.
    - 쪽 번호는 PDF 쪽 = 인쇄(법제처 하단) 쪽이다.
"""
import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

from pypdf import PdfReader

LAW_NAME = "도로교통법"
CORPUS = "law_roadtraffic"
ID_PREFIX = "law"

# 장 번호 기준 우선순위: 사고 과실 판단과 직접 관련된 장(총칙·보행자·차마 통행·운전자 의무·고속도로 특례)
HIGH_PRIORITY_CHAPTERS = {"1", "2", "3", "4", "4의2", "5"}

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟"
CIRCLED_RE = re.compile(f"[{CIRCLED}]")

ARTICLE_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\(([^)]+)\)\s*(.*)$")
DELETED_ARTICLE_RE = re.compile(r"^제(\d+)조(?:의(\d+))?\s*삭제")
CHAPTER_RE = re.compile(r"^제(\d+)장(?:의(\d+))?\s+(.+?)\s*(?:<[^>]*>)?\s*$")
SECTION_RE = re.compile(r"^제(\d+)절(?:의\d+)?\s+(.+?)\s*(?:<[^>]*>)?\s*$")
ITEM_RE = re.compile(r"^\d+(?:의\d+)?\.\s")
HEADER_FOOTER_RE = re.compile(r"^법제처\s+\d+\s+국가법령정보센터\s*$")
ADDENDA_RE = re.compile(r"^부칙\s*(?:<|$)")

# 본문에서 빼서 amendments 로 옮길 개정 이력 표시
# <개정 ...>, <신설 ...>, 삭제<2018. 3. 27.> 처럼 날짜가 들어 있는 <...> 표시 전부
INLINE_AMEND_RE = re.compile(r"<[^<>]*?\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.[^<>]*>")
# 줄 끝에서 열렸다가 다음 줄에서 닫히는 표시(예: "<신\n설 2019. 12. 24.>")의 시작부
OPEN_AMEND_RE = re.compile(r"<(?:개|신|삭|전|\d{4})")
BRACKET_AMEND_RE = re.compile(
    r"^\[(?:본조신설|전문개정|제목개정|본조삭제|종전|제\d+조에서 이동|본조이동|시행일: 삭제)[^\]]*\]\s*$"
)
OTHER_LAW_REF_RE = re.compile(r"「[^」]+」(?:\s*제\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?)*")
ARTICLE_REF_RE = re.compile(r"제(\d+)조(?:의(\d+))?")


def art_label(n, sub):
    return f"제{n}조" + (f"의{sub}" if sub else "")


def art_key(n, sub):
    return (int(n), int(sub) if sub else 0)


# ---------------------------------------------------------------------------
# 1) PDF -> (페이지, 줄) 목록
# ---------------------------------------------------------------------------
def read_lines(pdf_path):
    reader = PdfReader(str(pdf_path))
    lines = []  # (page, text)
    for pno, page in enumerate(reader.pages, 1):
        raw = page.extract_text() or ""
        raw_lines = [l.rstrip() for l in raw.split("\n")]
        for i, line in enumerate(raw_lines):
            s = line.strip()
            if not s:
                continue
            if HEADER_FOOTER_RE.match(re.sub(r"\s+", " ", s)):
                continue
            if s == LAW_NAME and i <= 2:  # 쪽마다 반복되는 문서명
                continue
            lines.append((pno, s))
    return lines


def find_body_start(lines):
    """목차를 건너뛰고 본문 첫 줄(제1조(목적) 이 법은 ...)의 위치를 찾는다."""
    for idx, (_, text) in enumerate(lines):
        if re.match(r"^제1조\(목적\)\s*이 법은", text):
            return idx
    raise RuntimeError("본문 시작(제1조(목적))을 찾지 못했습니다. 다른 판본의 PDF일 수 있습니다.")


# ---------------------------------------------------------------------------
# 2) 줄 -> 조문(장·절 포함) 단위로 묶기
# ---------------------------------------------------------------------------
def parse_articles(lines, include_addenda=False):
    start = find_body_start(lines)
    articles = []
    cur = None
    chapter = None  # dict(num, title)
    section = None
    last_key = (0, 0)
    in_addenda = False
    skipped_deleted = []

    # 본문 첫 장은 "제1장 총칙"이 목차 뒤에서 다시 나오므로, 시작 직전 줄에서 장을 복원
    for _, text in reversed(lines[:start]):
        m = CHAPTER_RE.match(text)
        if m:
            chapter = {"num": m.group(1) + (f"의{m.group(2)}" if m.group(2) else ""), "title": m.group(3)}
            break
    if chapter is None:
        chapter = {"num": "1", "title": "총칙"}

    for pno, text in lines[start:]:
        if ADDENDA_RE.match(text):
            in_addenda = True
            if not include_addenda:
                break
        if in_addenda:
            continue

        mch = CHAPTER_RE.match(text)
        if mch and "조" not in text.split()[0]:
            chapter = {"num": mch.group(1) + (f"의{mch.group(2)}" if mch.group(2) else ""), "title": mch.group(3)}
            section = None
            continue
        msec = SECTION_RE.match(text)
        if msec:
            section = msec.group(2)
            continue

        mdel = DELETED_ARTICLE_RE.match(text)
        if mdel and art_key(mdel.group(1), mdel.group(2)) >= last_key:
            skipped_deleted.append(art_label(mdel.group(1), mdel.group(2)))
            last_key = art_key(mdel.group(1), mdel.group(2))
            cur = None
            continue

        mart = ARTICLE_RE.match(text)
        # 다른 조문을 인용하는 줄이 조문 제목처럼 보이는 경우를 막기 위해 번호가 증가하는 경우만 새 조문으로 인정
        if mart and art_key(mart.group(1), mart.group(2)) > last_key:
            last_key = art_key(mart.group(1), mart.group(2))
            cur = {
                "n": mart.group(1),
                "sub": mart.group(2),
                "title": mart.group(3),
                "chapter": dict(chapter),
                "section": section,
                "lines": [],  # (page, text)
            }
            articles.append(cur)
            rest = mart.group(4).strip()
            if rest:
                cur["lines"].append((pno, rest))
            continue

        if cur is not None:
            cur["lines"].append((pno, text))

    return articles, skipped_deleted


# ---------------------------------------------------------------------------
# 3) 조문 내용 정리 / 분할
# ---------------------------------------------------------------------------
def clean_article_lines(lines):
    """개정 이력 표시를 분리한다. 반환: (본문 줄 [(page,text)], amendments [str])

    <개정 2012. 3. 21., ... > 표시는 여러 줄에 걸쳐 이어지는 경우가 많아,
    '<'로 열린 표시는 '>'가 나올 때까지 이어 붙여서 한 번에 제거한다.
    """
    body, amendments = [], []
    pending, pending_page, prefix = None, None, ""
    for pno, text in lines:
        if pending is not None:  # 이전 줄에서 열린 <개정 ...> 표시를 이어 붙이는 중
            pending += " " + text
            if ">" not in text:
                continue
            text, pending = pending, None
            pno = pending_page
        elif BRACKET_AMEND_RE.match(text):
            amendments.append(text)
            continue
        elif OPEN_AMEND_RE.search(text) and ">" not in text[OPEN_AMEND_RE.search(text).start():]:
            pending, pending_page = text, pno
            continue
        found = INLINE_AMEND_RE.findall(text)
        if found:
            amendments.extend(re.sub(r"\s+", " ", f) for f in found)
            text = INLINE_AMEND_RE.sub("", text).strip()
            text = re.sub(r"\s{2,}", " ", text)
        if text:
            body.append((pno, text))
    if pending:  # 닫히지 않은 표시가 남은 경우 그대로 본문에 남긴다(데이터 손실 방지)
        body.append((pending_page, pending))
    amendments = list(dict.fromkeys(amendments))
    return body, amendments


def split_paragraphs(body_lines):
    """항(①②...) 단위로 줄을 묶는다. 항 표시가 없는 도입부는 첫 항목으로 둔다."""
    paras = []  # dict(marker, lines[(page,text)])
    cur = {"marker": "", "lines": []}
    for pno, text in body_lines:
        if CIRCLED_RE.match(text[0]):
            if cur["lines"]:
                paras.append(cur)
            cur = {"marker": text[0], "lines": [(pno, text)]}
        else:
            cur["lines"].append((pno, text))
    if cur["lines"]:
        paras.append(cur)
    return paras


def para_len(p):
    return sum(len(t) + 1 for _, t in p["lines"])


def split_long_paragraph(p, max_chars):
    """한 항이 max_chars 를 넘으면 호(1. 2. ...) 경계에서 나눈다."""
    if para_len(p) <= max_chars:
        return [p]
    groups, cur = [], []
    for pno, text in p["lines"]:
        if ITEM_RE.match(text) and cur and sum(len(t) + 1 for _, t in cur) >= max_chars * 0.6:
            groups.append(cur)
            cur = []
        cur.append((pno, text))
        if sum(len(t) + 1 for _, t in cur) >= max_chars and not ITEM_RE.match(text):
            pass
    if cur:
        groups.append(cur)
    # 호 경계가 없어서 여전히 긴 덩어리는 줄 단위로 강제 분할
    out = []
    for g in groups:
        if sum(len(t) + 1 for _, t in g) <= max_chars * 1.5:
            out.append(g)
            continue
        buf = []
        for ln in g:
            buf.append(ln)
            if sum(len(t) + 1 for _, t in buf) >= max_chars:
                out.append(buf)
                buf = []
        if buf:
            out.append(buf)
    parts = []
    for i, g in enumerate(out):
        parts.append({"marker": p["marker"] if i == 0 else "", "lines": g, "continued": i > 0})
    return parts


def pack_paragraphs(paras, max_chars):
    """연속된 항을 max_chars 이내로 묶는다."""
    units = []
    for p in paras:
        units.extend(split_long_paragraph(p, max_chars))
    chunks, cur, cur_len = [], [], 0
    for u in units:
        ln = para_len(u)
        if cur and cur_len + ln > max_chars:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(u)
        cur_len += ln
    if cur:
        chunks.append(cur)
    return chunks


def paragraph_range(units):
    markers = [u["marker"] for u in units if u["marker"] and not u.get("continued")]
    if not markers:
        return ""
    return markers[0] if len(markers) == 1 else f"{markers[0]}~{markers[-1]}"


def page_range(units):
    pages = [p for u in units for p, _ in u["lines"]]
    lo, hi = min(pages), max(pages)
    return str(lo) if lo == hi else f"{lo}-{hi}"


def extract_refs(text, self_key):
    cleaned = OTHER_LAW_REF_RE.sub(" ", text)  # 「다른 법률」 인용은 제외
    refs = OrderedDict()
    for m in ARTICLE_REF_RE.finditer(cleaned):
        if art_key(m.group(1), m.group(2)) == self_key:
            continue
        refs[art_label(m.group(1), m.group(2))] = True
    return list(refs)


# ---------------------------------------------------------------------------
# 4) 청크 생성
# ---------------------------------------------------------------------------
def build_chunks(articles, source_label, max_chars):
    chunks = []
    seq = 0
    for art in articles:
        label = art_label(art["n"], art["sub"])
        heading = f"{label}({art['title']})"
        body_lines, amendments = clean_article_lines(art["lines"])
        if not body_lines:
            continue
        paras = split_paragraphs(body_lines)
        packed = pack_paragraphs(paras, max_chars)
        seq += 1
        ch = art["chapter"]
        chapter_label = f"제{ch['num'].replace('의', '장의')}" if "의" in ch["num"] else f"제{ch['num']}장"
        chapter_full = f"{chapter_label} {ch['title']}"
        section_path = [LAW_NAME, chapter_full]
        if art["section"]:
            section_path.append(art["section"])
        section_path.append(heading)
        priority = "high" if ch["num"] in HIGH_PRIORITY_CHAPTERS else "low"
        n_parts = len(packed)
        for pi, units in enumerate(packed, 1):
            body_text = "\n".join(t for u in units for _, t in u["lines"])
            title_line = heading if n_parts == 1 else f"{heading} ({pi}/{n_parts})"
            header = f"[{source_label}] " + " > ".join(section_path[1:-1] + [heading])
            if n_parts > 1:
                header += f" ({pi}/{n_parts})"
            text = f"{header}\n\n{heading}\n{body_text}"
            cid = f"{ID_PREFIX}_{seq:04d}_{label}" + (f"_p{pi}" if n_parts > 1 else "")
            chunks.append(
                {
                    "id": cid,
                    "corpus": CORPUS,
                    "title": title_line,
                    "codes": [],
                    "category": "",
                    "doc_type": "법조문",
                    "law_name": LAW_NAME,
                    "law_article": label,
                    "paragraphs": paragraph_range(units),
                    "chapter": chapter_full,
                    "priority": priority,
                    "section_path": section_path,
                    "pdf_pages": page_range(units),
                    "printed_pages": page_range(units),
                    "source": source_label,
                    "refs": extract_refs(body_text, art_key(art["n"], art["sub"])),
                    "amendments": amendments if pi == 1 else [],
                    "part": pi,
                    "part_total": n_parts,
                    "text": text,
                    "char_count": len(text),
                }
            )
    return chunks


# ---------------------------------------------------------------------------
# 5) 파일 출력
# ---------------------------------------------------------------------------
def yaml_scalar(v):
    return json.dumps(v, ensure_ascii=False)


FRONT_KEYS = [
    "id", "corpus", "title", "doc_type", "law_article", "paragraphs", "chapter", "priority",
    "codes", "section_path", "pdf_pages", "printed_pages", "source", "refs", "part", "part_total",
]


def write_outputs(chunks, out_dir, source_label):
    out_dir = Path(out_dir)
    chunk_dir = out_dir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    for old in chunk_dir.glob("*.md"):
        old.unlink()

    with open(out_dir / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    for c in chunks:
        fm = "\n".join(f"{k}: {yaml_scalar(c[k])}" for k in FRONT_KEYS)
        (chunk_dir / f"{c['id']}.md").write_text(f"---\n{fm}\n---\n{c['text']}\n", encoding="utf-8")

    n_articles = len({c["law_article"] for c in chunks})
    lines = [
        f"# {LAW_NAME} — RAG 청크 색인",
        "",
        f"출처: {source_label}",
        f"총 {len(chunks)}개 청크 / {n_articles}개 조문. 목차와 부칙은 제외했습니다.",
        "",
        "`priority`가 high인 장(1~5장)이 교통사고 과실 판단과 직접 관련됩니다.",
        "",
    ]
    cur_ch = None
    for c in chunks:
        if c["chapter"] != cur_ch:
            cur_ch = c["chapter"]
            lines += ["", f"## {cur_ch} ({c['priority']})", "", "| 조문 | 청크 파일 | PDF쪽 | 항 |", "|---|---|---|---|"]
        lines.append(
            f"| {c['law_article']} | [{c['title']}](chunks/{c['id']}.md) | {c['pdf_pages']} | {c['paragraphs'] or '—'} |"
        )
    (out_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def detect_source_label(lines):
    for _, text in lines[:12]:
        m = re.search(r"\[시행\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]\s*\[법률\s*제(\d+)호", text)
        if m:
            y, mo, d, no = m.groups()
            return f"{LAW_NAME} (시행 {y}.{int(mo)}.{int(d)}, 법률 제{no}호)"
    return LAW_NAME


def main():
    ap = argparse.ArgumentParser(description="도로교통법 PDF -> RAG 청크")
    ap.add_argument("pdf")
    ap.add_argument("out", nargs="?", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--max-chars", type=int, default=2000, help="청크 최대 글자 수(기본 2000)")
    ap.add_argument("--include-addenda", action="store_true", help="부칙도 포함(기본 제외)")
    args = ap.parse_args()

    lines = read_lines(args.pdf)
    source_label = detect_source_label(lines)
    articles, deleted = parse_articles(lines, include_addenda=args.include_addenda)
    chunks = build_chunks(articles, source_label, args.max_chars)
    if not chunks:
        sys.exit("청크가 만들어지지 않았습니다.")
    write_outputs(chunks, args.out, source_label)

    sizes = [c["char_count"] for c in chunks]
    print(f"출처: {source_label}")
    print(f"조문 {len(articles)}개 (삭제 조문 {len(deleted)}개 제외) → 청크 {len(chunks)}개")
    print(f"청크 길이: 최소 {min(sizes)} / 중앙값 {sorted(sizes)[len(sizes)//2]} / 최대 {max(sizes)}")
    print("우선순위:", dict(Counter(c["priority"] for c in chunks)))
    print("분할된 조문:", sorted({c["law_article"] for c in chunks if c["part_total"] > 1})[:20], "...")
    print(f"저장: {Path(args.out) / 'chunks.jsonl'}")


if __name__ == "__main__":
    main()