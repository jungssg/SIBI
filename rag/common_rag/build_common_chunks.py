"""Split 자동차사고 과실비율 인정기준 PDF into RAG chunks.

Boundaries: TOC headings (pages 2-6, printed page + 1 = pdf page) and
accident-type code pages (보N / 차N-N / 거N-N standalone line near top).
Output: chunks.jsonl, chunks/*.md (YAML front matter), index.md
"""
import sys, os, re, json, shutil, pypdf

sys.stdout.reconfigure(encoding="utf-8")
SRC, OUT = sys.argv[1], sys.argv[2]
MAX_CHARS = 7000
DOC = "자동차사고 과실비율 인정기준 (2023.6.30)"

r = pypdf.PdfReader(SRC)
N = len(r.pages)
raw = [""] + [(p.extract_text() or "") for p in r.pages]  # 1-indexed

# ---------- page cleanup ----------
HDR_RE = [
    re.compile(r"^\s*제\d장\.\s"),
    re.compile(r"^\s*자동차사고 과실비율 인정기준\s*(│.*)?$"),
    re.compile(r"^\s*\d{3}\s*$"),
]

def body_lines(pno):
    lines = raw[pno].splitlines()
    # strip running header (first few lines)
    i = 0
    while i < min(4, len(lines)) and any(h.match(lines[i]) for h in HDR_RE):
        i += 1
    lines = lines[i:]
    # strip trailing "목 차" marker / page number on TOC-like pages
    while lines and re.match(r"^\s*\d{3}\s*$", lines[-1]):
        lines.pop()
    # join vertical text (runs of single-character lines)
    out, buf = [], []
    for l in lines:
        s = l.strip()
        if len(s) == 1 and re.match(r"[가-힣]", s):
            buf.append(s)
            continue
        if buf:
            out.append("".join(buf) if len(buf) > 1 else buf[0])
            buf = []
        out.append(l.rstrip())
    if buf:
        out.append("".join(buf))
    # join split codes: "거43" + "-1" -> "거43-1"
    fixed = []
    for l in out:
        if fixed and re.match(r"^\s*-\d+\s*$", l) and re.match(r"^\s*(보|차|거)\d+\s*$", fixed[-1]):
            fixed[-1] = fixed[-1].strip() + l.strip()
        else:
            fixed.append(l)
    return fixed

BODY = [None] + [body_lines(p) for p in range(1, N + 1)]

def printed(pno):
    return pno - 1

# ---------- TOC ----------
TOC_LINE = re.compile(r"^(.*?)\s*\.{3,}\s*(\d{3})\s*$")

def level_of(t):
    if re.match(r"^제\d편", t): return 1
    if re.match(r"^제\d장", t): return 2
    if re.match(r"^\d+\.\s", t): return 3
    if re.match(r"^[가-하]\.\s", t): return 4
    if re.match(r"^\(\d+\)", t): return 5
    if re.match(r"^\d+\)", t): return 6
    return 1  # 발간사, ※ 별첨

toc = []
for p in range(2, 7):
    for l in raw[p].splitlines():
        m = TOC_LINE.match(l.strip())
        if m:
            t = re.sub(r"\s+", " ", m.group(1)).strip()
            toc.append({"title": t, "level": level_of(t), "page": int(m.group(2)) + 1})

def norm(s):
    s = re.sub(r"\[.*?\]", "", s)
    s = re.sub(r"[\s()（）內後前對]", "", s)
    return s

def find_line(pno, title):
    key = norm(title)[:12]
    for i, l in enumerate(BODY[pno]):
        if key and norm(l).startswith(key):
            return i
    return 0

# ---------- accident-type codes ----------
CODE_RE = re.compile(r"^\s*((?:보|차|거)\d+(?:-\d+)?)\s*$")

def codes_on(pno, first_n=None):
    lines = BODY[pno] if first_n is None else BODY[pno][:first_n]
    return [m.group(1) for l in lines if (m := CODE_RE.match(l))]

# ---------- boundaries ----------
bounds = []  # (page, line, kind, payload)
for e in toc:
    if e["page"] <= 7 and e["title"] != "발간사":
        continue
    bounds.append((e["page"], find_line(e["page"], e["title"]), "toc", e))
for p in range(29, N + 1):
    if codes_on(p, 15):
        bounds.append((p, 0, "type", None))
bounds.append((7, 0, "toc", {"title": "발간사", "level": 1, "page": 7}))
bounds = sorted({(b[0], b[1], b[2], json.dumps(b[3], ensure_ascii=False)) for b in bounds})
bounds = [(a, b, c, json.loads(d)) for a, b, c, d in bounds]

# breadcrumb at each boundary (TOC stack as of that point)
segments = []
stack = {}
for idx, (p, ln, kind, e) in enumerate(bounds):
    if kind == "toc":
        lv = e["level"]
        stack = {k: v for k, v in stack.items() if k < lv}
        stack[lv] = e["title"]
    end = bounds[idx + 1][:2] if idx + 1 < len(bounds) else (N + 1, 0)
    # collect text from (p, ln) to end
    parts = []  # list of (page, text)
    q, start = p, ln
    while (q, start) < end and q <= N:
        stop = end[1] if q == end[0] else len(BODY[q])
        txt = "\n".join(BODY[q][start:stop]).strip()
        if txt:
            parts.append((q, txt))
        q, start = q + 1, 0
        if q > end[0]:
            break
    segments.append({"crumb": [stack[k] for k in sorted(stack)], "kind": kind, "parts": parts})

# merge tiny segments (heading-only) forward
merged = []
carry = None
for s in segments:
    if carry:
        s["parts"] = carry["parts"] + s["parts"]
        carry = None
    size = sum(len(t) for _, t in s["parts"])
    if size < 300 and s is not segments[-1]:
        carry = s
        continue
    merged.append(s)

# ---------- type titles ----------
def type_info(text):
    lines = [l.strip() for l in text.splitlines()]
    codes, titles = [], {}
    for i, l in enumerate(lines):
        m = CODE_RE.match(l)
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
            nxt = next((x for x in lines[i + 1:i + 4] if x and not CODE_RE.match(x)), "")
            titles[m.group(1)] = nxt
    # prefer inline "코드 제목" lines (e.g. "거43-1 자전거 전용도로 통행 ...")
    for l in lines:
        m = re.match(r"^((?:보|차|거)\d+-\d+)\s+(\S.{4,})$", l)
        if m and m.group(1) in codes:
            titles[m.group(1)] = m.group(2).strip()
    return codes, titles

CAT = {"보": "자동차-보행자", "차": "자동차-자동차", "거": "자동차-자전거"}

def doc_type(crumb, codes):
    c = " ".join(crumb)
    if codes: return "사고유형별 기준"
    if "변경대비표" in c: return "변경대비표"
    if "총 설" in c or "총설" in c: return "총설"
    if "수정요소" in c: return "수정요소 해설"
    if "용어 정의" in c or "적용 범위" in c: return "적용범위·용어정의"
    if "개정경과" in c: return "개정경과"
    if "발간사" in c: return "발간사"
    return "기타"

# ---------- build chunks (split long ones at page boundaries) ----------
chunks = []
for s in merged:
    full = "\n".join(t for _, t in s["parts"])
    codes, titles = type_info(full)
    groups, cur, size = [], [], 0
    for pg, t in s["parts"]:
        if cur and size + len(t) > MAX_CHARS:
            groups.append(cur); cur, size = [], 0
        cur.append((pg, t)); size += len(t)
    if cur: groups.append(cur)
    for gi, g in enumerate(groups):
        pages = sorted({pg for pg, _ in g})
        chunks.append({
            "crumb": s["crumb"], "codes": codes, "titles": titles,
            "part": (gi + 1, len(groups)), "pages": pages,
            "text": "\n".join(t for _, t in g),
            "doc_type": doc_type(s["crumb"], codes),
        })

# ---------- write ----------
if os.path.isdir(os.path.join(OUT, "chunks")):
    shutil.rmtree(os.path.join(OUT, "chunks"))
os.makedirs(os.path.join(OUT, "chunks"))

def safe(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")[:60]

records, index_rows = [], []
for i, c in enumerate(chunks, 1):
    codes = c["codes"]
    if codes:
        name = codes[0] if len(codes) == 1 else f"{codes[0]}~{codes[-1]}"
        title = " / ".join(f"[{k}] {c['titles'][k]}" for k in codes)
    else:
        name = c["crumb"][-1] if c["crumb"] else "front"
        title = c["crumb"][-1] if c["crumb"] else ""
    pn, pt = c["part"]
    cid = f"{i:04d}_{safe(name)}" + (f"_part{pn}" if pt > 1 else "")
    p0, p1 = c["pages"][0], c["pages"][-1]
    meta = {
        "id": cid,
        "title": title + (f" (part {pn}/{pt})" if pt > 1 else ""),
        "doc_type": c["doc_type"],
        "category": CAT.get(codes[0][0], "") if codes else "",
        "codes": codes,
        "section_path": c["crumb"],
        "pdf_pages": f"{p0}-{p1}" if p1 != p0 else str(p0),
        "printed_pages": f"{printed(p0)}-{printed(p1)}" if p1 != p0 else str(printed(p0)),
        "source": DOC,
    }
    # context header prepended to text so each chunk is self-describing for embedding
    ctx = " > ".join(c["crumb"])
    head = f"[{DOC}] {ctx}" + (f"\n사고유형: {title}" if codes else "") + (f" (part {pn}/{pt})" if pt > 1 else "")
    text = head + "\n\n" + c["text"]
    records.append({**meta, "text": text, "char_count": len(text)})
    fm = ["---"]
    for k, v in meta.items():
        fm.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    fm.append("---\n")
    with open(os.path.join(OUT, "chunks", cid + ".md"), "w", encoding="utf-8") as f:
        f.write("\n".join(fm) + text + "\n")
    index_rows.append(meta)

with open(os.path.join(OUT, "chunks.jsonl"), "w", encoding="utf-8") as f:
    for rec in records:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

with open(os.path.join(OUT, "index.md"), "w", encoding="utf-8") as f:
    f.write(f"# {DOC} — RAG 청크 색인\n\n")
    f.write(f"총 {len(records)}개 청크. `pdf_pages`는 PDF 쪽, `printed_pages`는 책에 인쇄된 쪽 번호.\n\n")
    f.write("| id | 유형코드 | 제목 | 문서구분 | PDF쪽 |\n|---|---|---|---|---|\n")
    for m in index_rows:
        f.write(f"| [{m['id']}](chunks/{m['id']}.md) | {', '.join(m['codes'])} | {m['title'].replace('|', '/')} | {m['doc_type']} | {m['pdf_pages']} |\n")

sizes = [r_["char_count"] for r_ in records]
print("chunks", len(records), "min", min(sizes), "max", max(sizes), "avg", sum(sizes) // len(sizes))
allcodes = [k for r_ in records for k in r_["codes"]]
print("codes", len(set(allcodes)), {p: sum(1 for k in set(allcodes) if k[0] == p) for p in "보차거"})
covered = sorted({p for c in chunks for p in c["pages"]})
print("pages covered", len(covered), "missing", [p for p in range(7, N + 1) if p not in covered])
