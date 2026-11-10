#!/usr/bin/env python3
"""Build RAG files for the supplied June 2025 two-lane roundabout PDF.

Usage: python build_chunks.py <source.pdf> <output-directory>
Requires Python 3.9+, pypdf, cryptography. No network or OCR service is used.
The image-only p.8 transcript below was visually verified for this source SHA256.
This script deliberately rejects other editions rather than reuse that transcript.
"""
import argparse
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

DOC = "2차로형 회전교차로 사고 과실비율 비정형 기준 (2025.6)"
DOC_TITLE = "2차로형 회전교차로 사고 과실비율 비정형 기준"
NAMESPACE = "roundabout_202506"
SOURCE_SHA256 = "3e8fcd04fe7a462c2db875db67ecf0632ff506b83728f4983eeff9df21d2d2d2"
MAX_CHARS = 7000
CODE_RE = re.compile(r"^회전-\d+$")
RATIO_RE = re.compile(r"기본\s*과실비율\s*레드\s*(\d+)\s*블루\s*(\d+)")
ROW_RE = re.compile(r"^(레드\(A\)|블루\(B\))\s+(.+?)\s+([+-]\d+)\s*$")

# Transcription of visible text, not an interpretation of arrows or geometry.
# A/B here belong to the poster inset; they are not the red/blue labels in types.
GUIDE_TRANSCRIPT = """# 회전교차로 올바른 통행방법

[이미지 문구 전사: PDF 8쪽의 안내 포스터를 육안 대조한 텍스트입니다.
그림의 차량 궤적·화살표·차선 배치는 텍스트로 재현하지 않았습니다.]

- 보행자에 반드시 양보
- 나올 때 우측 깜빡이
- 회전차량 멈추지 말고 서행
- 회전차량 우선
- 들어갈 때 좌측 깜빡이
- 좌회전 안쪽차로 / 우회전 바깥쪽차로
- 접근 시 서행

## 포스터 내 사고 예시의 문구

| 구분 | A | B |
|---|---|---|
| 차량 설명 | 교차로 내 회전 | 회전교차로 진입 |
| 통행우선권 | 우선 | 양보 |
| 과실비율 | (20) | (80) |

※세부 사고상황 등에 따라 과실비율이 달라질 수 있습니다

[전사 주: 위 A/B와 20:80은 이 포스터 안의 예시 표기입니다.
회전-1~회전-15 도표의 레드(A)·블루(B)와는 각 도표를 기준으로 구별해야 합니다.]
"""


def compact(text):
    return re.sub(r"\s+", "", text)


def clean_page(text, pno):
    """Remove only the running title and its immediately following page number."""
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and compact(lines[0]) == compact(DOC_TITLE):
        lines.pop(0)
        if lines and re.fullmatch(r"\d{1,3}", lines[0].strip()):
            if int(lines[0]) != pno:
                raise ValueError(f"PDF/printed page mismatch at PDF {pno}")
            lines.pop(0)
    return "\n".join(lines).strip()


def parse_first_page(text, expected_code):
    """Make the numeric table explicit, preserving every remaining source line."""
    lines = text.splitlines()
    if lines[0].strip() != expected_code:
        raise ValueError(f"Missing type heading: {expected_code}")
    red_at = next(i for i, s in enumerate(lines) if re.match(r"레드\(A\)\s*:", s))
    title = re.sub(r"\s+", " ", " ".join(lines[1:red_at])).strip()
    ratio_at = next(i for i, s in enumerate(lines) if RATIO_RE.fullmatch(s.strip()))
    ratio = [int(v) for v in RATIO_RE.fullmatch(lines[ratio_at].strip()).groups()]
    if sum(ratio) != 100:
        raise ValueError(f"Invalid basic ratio: {expected_code}")
    role_lines = lines[red_at:ratio_at]
    blue_at = next(i for i, s in enumerate(role_lines) if re.match(r"블루\(B\)\s*:", s))
    roles = [" ".join(role_lines[:blue_at]), " ".join(role_lines[blue_at:])]
    roles = [re.sub(r"\s+", " ", s).strip() for s in roles]
    note_at = next(i for i in range(ratio_at + 1, len(lines)) if lines[i].startswith("※"))
    table_lines = [s.strip() for s in lines[ratio_at + 1:note_at] if s.strip()]
    # pypdf extracts the vertical label as eight individual Hangul lines.
    row_start = next(i for i, s in enumerate(table_lines) if ROW_RE.fullmatch(s))
    label = "".join(table_lines[:row_start])
    if label != "과실비율조정예시":
        raise ValueError(f"Unexpected table heading: {expected_code}: {label}")
    rows = []
    for line in table_lines[row_start:]:
        match = ROW_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"Unparsed adjustment row: {expected_code}: {line}")
        rows.append(match.groups())
    # All copied terms, signs and magnitudes must remain source-exact.
    normalized = [expected_code, title, "", *roles, "", "## 기본 과실비율"]
    normalized.extend([
        "", "| 레드(A) | 블루(B) |", "|---|---|",
        f"| {ratio[0]} | {ratio[1]} |", "", "## 과실비율 조정예시", "",
        "| 해당 차량 | 수정요소 | 조정값(원문) |", "|---|---|---|",
    ])
    normalized.extend(f"| {vehicle} | {term} | {value} |" for vehicle, term, value in rows)
    normalized.extend(["", *lines[note_at:]])
    return "\n".join(normalized), title, ratio, rows


def page_range(pages):
    return str(pages[0]) if pages[0] == pages[-1] else f"{pages[0]}-{pages[-1]}"


def context(section, title, codes, part, total):
    header = f"[{DOC}] " + " > ".join(section)
    if codes:
        header += f"\n사고유형: [{codes[0]}] {title}"
    if total > 1:
        header += f"\npart {part}/{total}"
    return header


def render_text(segment, pages, part, total):
    header = context(segment["section"], segment["title"], segment["codes"], part, total)
    body = "\n\n".join(f"### PDF {p}쪽 / 인쇄 {p}쪽\n\n{text}" for p, text in pages)
    return header + "\n\n" + body


def split_pages(segment):
    # Reserve a conservative part label so final text, including context, <= 7000.
    result, current = [], []
    for page in segment["parts"]:
        candidate = current + [page]
        if len(render_text(segment, candidate, 999, 999)) > MAX_CHARS and current:
            result.append(current)
            current = [page]
        else:
            current = candidate
        if len(render_text(segment, current, 999, 999)) > MAX_CHARS:
            raise ValueError("One page exceeds 7000 characters; page-boundary split is impossible")
    if current:
        result.append(current)
    return result


def make_readme(records, source_name):
    return f"""# {DOC} — RAG용 청크

원본: `250624_2차로형 회전교차로사고 과실비율 비정형기준(1).pdf` (76 PDF쪽). 발행: 2025년 6월, 손해보험협회 과실비율분쟁심의위원회.
이 자료는 첨부된 2025년 6월판의 변환 결과입니다. 이후의 기준 변경 여부를 조사한 자료는 아닙니다.

## 구성

| 파일 | 용도 |
|---|---|
| `chunks.jsonl` | 벡터DB 적재용. 한 줄에 청크 하나. 메타데이터는 예시와 같이 최상위 필드에 있음 |
| `chunks/*.md` | 파일 기반 에이전트용. YAML front matter + 동일한 text 본문 |
| `index.md` | 전체 청크 목록. 유형코드에서 해당 Markdown 파일로 이동 |
| `build_chunks.py` | 원본 PDF로 전체 결과를 재생성하는 스크립트 |
| `validation_report.json` | 페이지 커버리지, 유형별 비율·수정요소·판례 수, 본문 보존 검사 결과 |
| `requirements.txt` | 설치할 Python 패키지 |

## 청크 기준

- 총 {len(records)}개: 사고유형별 기준 15개, 머리말 1개, 통행방법 안내 1개.
- `회전-1`부터 `회전-15`까지 유형코드 하나를 한 청크로 구성했습니다.
- 해당 유형의 사고 상황, 기본 과실비율과 해설, 수정요소와 해설, 관련 법규, 참고 판례를 함께 보존했습니다.
- 문서명·목차 경로·유형 제목을 text 앞에 추가하고, 본문 페이지마다 출처 쪽 번호를 표시했습니다.
- 7,000자는 문맥 헤더와 페이지 표시를 포함한 text 길이 기준입니다. 초과할 경우 페이지 경계에서 part 1/N으로 나눕니다. 이번 결과에는 분할이 필요하지 않았습니다.
- PDF 5쪽의 목차는 index.md로 대체했습니다. PDF 1쪽 표지, 2~4쪽 공백, 9쪽 구분표지, 73쪽 공백, 74쪽 판권, 75쪽 공백, 76쪽 뒤표지는 청크에서 제외했습니다. 발행정보는 이 설명에 보존했습니다.
- 머리말은 PDF 6~7쪽입니다. PDF 7쪽의 기존/개선 교차로 그림은 라벨만 보존했습니다.
- PDF 8쪽은 이미지뿐이어서 자동 텍스트 추출이 불가능합니다. 포스터의 주요 통행 안내와 사고 예시 문구를 육안 대조 후 전사하고, 본문에 전사임을 표시했습니다. 기관 로고 문구와 도로 위 반복 라벨 등은 전사 대상에 포함하지 않았습니다.
- 이 문서에는 독립된 총설·용어정의·수정요소 해설 장이나 변경대비표가 없습니다. 각 유형에 포함된 해설은 해당 유형 안에 유지했습니다.

## 메타데이터

| 필드 | 설명 |
|---|---|
| `id` | 기존 2023년판 청크와 합칠 때 충돌하지 않도록 roundabout_202506 접두사 사용 |
| `title` | 유형코드와 유형 제목 또는 일반 섹션명 |
| `codes` | 유형의 정확한 코드 배열. 예: ["회전-6"]. 본문에서 인용된 타 유형코드는 이 필드에 넣지 않음 |
| `category` | 이 문서의 모든 청크는 자동차-자동차 |
| `doc_type` | 사고유형별 기준 / 발간사(머리말) / 총설(통행방법 안내) |
| `section_path` | 문서·사고군·유형 제목의 경로. 사고군은 머리말에 제시된 분류를 반영 |
| `pdf_pages` | 1부터 시작하는 PDF 페이지 번호. 예시와 같은 문자열 또는 범위 문자열 |
| `printed_pages` | 본문의 인쇄 쪽 번호. 이 PDF에서는 PDF쪽과 동일함. 8쪽 포스터의 쪽은 목차의 08을 따름 |
| `source` | 문서명과 발행연월 |
| `text` | 문맥 헤더와 본문. 이 필드를 임베딩 |
| `char_count` | text의 Python 문자열 길이. 토큰 수나 바이트 수가 아님 |

## 사용과 재생성

```bash
pip install -r requirements.txt
python build_chunks.py "250624_2차로형 회전교차로사고 과실비율 비정형기준(1).pdf" ./rebuilt
```

원본 PDF는 ZIP에 중복 포함하지 않았습니다. 파일명을 바꿔도 원본 바이트가 같으면 실행됩니다.
스크립트는 검증한 2025년 6월판 전용입니다. 다른 판본에 8쪽 전사문이 잘못 적용되는 것을 막기 위해 SHA256이 다르면 중단합니다.
출력폴더 안의 같은 접두사를 가진 기존 청크만 갱신하며, 기존 2023년판 자료와의 혼동을 피하도록 별도 출력폴더를 권장합니다.

JSONL의 codes는 문자열 배열입니다. 예: `"회전-6" in record["codes"]`로 일치 여부를 확인합니다.
벡터DB가 배열 필터를 지원하지 않는 경우 적재 시 별도 코드 필드/색인을 만드세요.
문자열 배열인 section_path 등은 사용하는 벡터DB의 메타데이터 허용 형식에 맞춰 적재하면 됩니다.

## 정확성·한계

- 기본 과실비율과 수정요소 표는 원본과 대조한 뒤 명시적인 Markdown 표로 구성했습니다. 레드(A)·블루(B), 조정 부호와 숫자를 유지했습니다. 조정값의 단위 표기는 새로 덧붙이지 않았습니다.
- 기준의 기본비율과 참고 판례의 개별 사건 비율은 서로 다를 수 있습니다. 판례의 비율을 해당 유형의 기본비율로 덮어쓰지 마세요.
- 회전-6에는 블루(B) 점선구간 변경 -20이 있으며, 해설의 40:60 예외도 원문대로 남아 있습니다.
- 사고 도표·노면 화살표·안전표지 그림과 차량 경로는 텍스트에 포함되지 않습니다. `pdf_pages`로 원본을 확인하세요. 그림 때문에 `(, )`처럼 비어 보이는 본문 표기도 원문 추출 상태를 유지했습니다.
- 안전표지 설명표 등 본문의 다른 표는 텍스트 추출 순서를 유지하므로 칸 정렬이 흐트러질 수 있습니다.
- 이미지 전사 페이지를 제외한 본문은 PDF 텍스트를 추출했습니다. 원문의 띄어쓰기, 오기, 줄바꿈으로 나뉜 단어는 임의로 교정하지 않았습니다.
- 수정요소 해설에서 참조하는 「자동차사고 과실비율 인정기준」 제3편 제2장 3. 수정요소의 해설은 이 PDF에 전문이 없습니다. 함께 제공한 2023년판 RAG 자료의 해당 섹션을 추가 검색해야 합니다. 문서의 외부 참조를 이 파일의 내용으로 새로 작성하지 않았습니다.
- PDF 8쪽 포스터의 A/B는 포스터 안에서의 차량 구분입니다. 사고유형별 레드(A)·블루(B)와 동일한 차량으로 간주하면 안 됩니다.
"""


def build(source, out):
    source = source.resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != SOURCE_SHA256:
        raise ValueError("검증된 2025년 6월 원본과 SHA256이 다릅니다. 다른 판본에는 재검증이 필요합니다.")
    reader = PdfReader(source)
    if len(reader.pages) != 76:
        raise ValueError("Expected a 76-page source PDF")
    raw = {p: page.extract_text() or "" for p, page in enumerate(reader.pages, 1)}
    body = {p: clean_page(text, p) for p, text in raw.items()}
    # No reliance on document bookmarks (the supplied source has none).
    toc_pages = {int(m.group(1)) for m in re.finditer(r"\.{3,}\s*(\d+)\s*$", raw[5], re.M)}
    starts = []
    for p in range(6, 73):
        first = body[p].splitlines()[:1]
        if first and CODE_RE.fullmatch(first[0].strip()):
            starts.append((p, first[0].strip()))
    if [code for _, code in starts] != [f"회전-{i}" for i in range(1, 16)]:
        raise ValueError("Expected exactly 15 type starts, 회전-1 through 회전-15")
    if any(p not in toc_pages for p, _ in starts):
        raise ValueError("Type start pages do not agree with source TOC")
    if body[8]:
        raise ValueError("The image-only guide page unexpectedly contains extracted text")
    segments = [
        {"title": "머리말", "codes": [], "doc_type": "발간사", "section": ["머리말"],
         "parts": [(p, body[p]) for p in (6, 7)]},
        {"title": "회전교차로 올바른 통행방법", "codes": [], "doc_type": "총설",
         "section": ["회전교차로 올바른 통행방법"], "parts": [(8, GUIDE_TRANSCRIPT.strip())]},
    ]
    audit = []
    for k, (start, code) in enumerate(starts):
        stop = starts[k + 1][0] - 1 if k + 1 < len(starts) else 72
        text, title, ratio, rows = parse_first_page(body[start], code)
        group = "① 진입차량 간 사고" if k < 8 else "② 진입차량과 회전차량 간의 사고"
        segments.append({
            "title": title, "codes": [code], "doc_type": "사고유형별 기준",
            "section": [DOC_TITLE, group, f"[{code}] {title}"],
            "parts": [(start, text)] + [(p, body[p]) for p in range(start + 1, stop + 1)],
        })
        full = "\n".join(body[p] for p in range(start, stop + 1))
        audit.append({"code": code, "pdf_pages": page_range([start, stop]),
                      "basic_ratio": {"레드(A)": ratio[0], "블루(B)": ratio[1]},
                      "adjustments": [{"vehicle": a, "factor": b, "value": int(c)} for a, b, c in rows],
                      "court_decision_markers": full.count("선고"),
                      "has_situation": "사고 상황" in full,
                      "has_law": "관련 법규" in full,
                      "has_adjustment_explanation": "수정요소(인과관계를 감안한 과실비율 조정) 해설" in full,
                      "has_court_decision": "선고" in full})
    records = []
    for segment in segments:
        groups = split_pages(segment)
        for part, pages in enumerate(groups, 1):
            slug = segment["codes"][0] if segment["codes"] else segment["title"].replace(" ", "_")
            cid = f"{NAMESPACE}_{len(records) + 1:04d}_{slug}"
            if len(groups) > 1:
                cid += f"_part{part}"
            numbers = [p for p, _ in pages]
            metadata = {
                "id": cid, "title": (f"[{segment['codes'][0]}] " if segment["codes"] else "") + segment["title"]
                + (f" (part {part}/{len(groups)})" if len(groups) > 1 else ""),
                "doc_type": segment["doc_type"], "category": "자동차-자동차", "codes": segment["codes"],
                "section_path": segment["section"], "pdf_pages": page_range(numbers),
                "printed_pages": page_range(numbers), "source": DOC,
            }
            text = render_text(segment, pages, part, len(groups))
            records.append({**metadata, "text": text, "char_count": len(text)})
    covered = [p for s in segments for p, _ in s["parts"]]
    expected_pages = [6, 7, 8] + list(range(10, 73))
    if sorted(covered) != expected_pages or len(covered) != len(set(covered)):
        raise ValueError("Missing or duplicate source pages")
    # Verify all unmodified continuation pages and every first-page body after the
    # numeric table survive exactly; titles/table fields are checked during parse.
    joined = "\n".join(r["text"] for r in records)
    for p in (6, 7, *range(10, 73)):
        tail = body[p]
        if p in {start for start, _ in starts}:
            lines = tail.splitlines()
            note = next(i for i, line in enumerate(lines) if line.startswith("※"))
            tail = "\n".join(lines[note:])
        if tail not in joined:
            raise ValueError(f"Source body content was lost at page {p}")
    if not all(r["char_count"] <= MAX_CHARS for r in records):
        raise ValueError("Chunk exceeds 7000 characters")
    if not all(a[key] for a in audit for key in ("has_situation", "has_law", "has_adjustment_explanation", "has_court_decision")):
        raise ValueError("A type lost a required source section")
    out.mkdir(parents=True, exist_ok=True)
    chunk_dir = out / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    # Never recursively delete the output directory or unrelated user files.
    for old in chunk_dir.glob(f"{NAMESPACE}_*.md"):
        old.unlink()
    for record in records:
        meta = {k: v for k, v in record.items() if k not in ("text", "char_count")}
        front = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n"
        (chunk_dir / f"{record['id']}.md").write_text(front + record["text"] + "\n", encoding="utf-8")
    (out / "chunks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    index = [f"# {DOC} — RAG 청크 색인", "", f"총 {len(records)}개 청크 / 사고유형 15개. PDF 5쪽 목차를 대신합니다.", "",
             "본문은 PDF쪽 = 인쇄쪽입니다. 8쪽 포스터는 목차의 08을 따릅니다.", "",
             "사고유형은 차로변경억제형 2차로형 회전교차로를 대상으로 합니다. 적용 상황·예외·판례는 각 파일에서 확인하세요.", "",
             "| 유형코드 | 제목 / 청크 파일 | 문서구분 | PDF쪽 | 인쇄쪽 |", "|---|---|---|---|---|"]
    for r in records:
        title = r["title"].replace("|", "/")
        index.append(f"| {', '.join(r['codes']) or '—'} | [{title}](chunks/{r['id']}.md) | {r['doc_type']} | {r['pdf_pages']} | {r['printed_pages']} |")
    index.extend(["", "- 머리말의 사고군 분류를 section_path의 중간 경로로 사용했습니다.",
                  "- PDF 8쪽은 이미지 문구 전사이며 그림의 궤적은 포함하지 않습니다.",
                  "- 2023년판과의 결합, 재생성, 원문 외부 참조와 한계는 [README.md](README.md)를 확인하세요.", ""])
    (out / "index.md").write_text("\n".join(index), encoding="utf-8")
    (out / "README.md").write_text(make_readme(records, source.name), encoding="utf-8")
    (out / "requirements.txt").write_text("pypdf>=5.0\ncryptography>=41.0\n", encoding="utf-8")
    report = {"source": DOC, "source_pdf_pages": 76,
              "chunk_count": len(records), "accident_type_count": len(audit),
              "doc_type_counts": dict(Counter(r["doc_type"] for r in records)),
              "min_char_count": min(r["char_count"] for r in records),
              "max_char_count": max(r["char_count"] for r in records),
              "max_chars": MAX_CHARS, "covered_pdf_pages": sorted(covered),
              "excluded_pdf_pages": [p for p in range(1, 77) if p not in covered],
              "image_transcription_pdf_pages": [8],
              "checks": {"source_hash_matches": True, "code_sequence_matches": True,
                         "type_start_pages_match_toc": True, "page_coverage_exact": True,
                         "no_duplicate_pages": True, "source_body_preserved": True,
                         "all_type_sections_present": True, "all_text_within_7000_chars": True},
              "scope_note": "자동 검사는 추출된 텍스트 보존과 구조를 확인합니다. 그림 전체의 의미 복원을 보증하지 않습니다.",
              "types": audit}
    (out / "validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    script = Path(__file__).resolve()
    destination = (out / "build_chunks.py").resolve()
    if script != destination:
        shutil.copyfile(script, destination)
    print(json.dumps({"chunks": len(records), "types": len(audit), "max_chars": report["max_char_count"],
                      "checks": report["checks"]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    build(args.source_pdf, args.output_directory.resolve())


if __name__ == "__main__":
    main()
