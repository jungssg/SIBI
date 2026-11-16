"""
[목적]
기존 RAG JSONL 파일에서 사고유형별 기준 청크만 추출하고,
Vision LLM으로 분석할 PDF 페이지 후보 목록을 생성한다.

[입력 파일]
1. rag/common_rag/common_chunks.jsonl
2. rag/roundabout_rag/round_chunks.jsonl

[출력 파일]
rag/vision/page_manifest.json

[주의]
- PDF를 이미지로 변환하지 않는다.
- OpenAI API를 호출하지 않는다.
- Supabase 데이터를 수정하지 않는다.
- 실제 사고 그림과 표가 있는 페이지인지 아직 검증하지 않는다.

[실행 방법]
python scripts/prepare_vision_pages.py
"""

import argparse
import json
import re
from pathlib import Path
# 같은 PDF 페이지에 속하는 청크를 묶기 위한 자료구조
from collections import defaultdict


# ==================================================
# 1. 프로젝트 최상위 경로 설정
# ==================================================

ROOT = Path(__file__).resolve().parents[1]


# ==================================================
# 2. 분석 대상 사고 카테고리 설정
# ==================================================

# 사고유형별 과실비율 기준 중
# 아래 3개 카테고리만 Vision LLM 분석 대상으로 선택한다.
#
# 발간사, 총설, 수정요소 해설 등은
# 이번 사고유형별 표 추출 대상에서 제외한다.

TARGET_CATEGORIES = {
    "자동차-자동차",
    "자동차-자전거",
    "자동차-보행자",
}


# ==================================================
# 3. 입력 JSONL 파일 경로 설정
# ==================================================

# corpus:
# PDF 문서 또는 데이터 집합을 구분하기 위한 식별자

# common_2023:
# 2023년 자동차사고 과실비율 인정기준

# roundabout_2025:
# 2025년 2차로형 회전교차로 사고 과실비율 기준

INPUT_LOCATIONS = {
    "common_2023": "rag/common_rag/common_chunks.jsonl",
    "roundabout_2025":"rag/roundabout_rag/round_chunks.jsonl"
}


# ==================================================
# 4. 입력 JSONL 파일 위치 확인 함수
# ==================================================

def locate_input(corpus: str) -> Path:
    """
    corpus에 해당하는 JSONL 파일의 경로를 반환한다.
    파일이 존재하지 않으면 오류를 발생시킨다.
    """

    # 프로젝트 최상위 경로 + JSONL 상대경로
    path = ROOT / INPUT_LOCATIONS[corpus]

    # 파일 존재 여부 확인
    if not path.is_file():
        raise FileNotFoundError(
            f"JSONL 파일을 찾을 수 없습니다: {path}"
        )
    return path

# ==================================================
# 5. PDF 페이지 범위 변환 함수
# ==================================================

def parse_pages(value: str) -> list[int]:

    """
    JSONL의 pdf_pages 값을 정수 리스트로 변환한다.

    예시 1:
    "97"
    → [97]

    예시 2:
    "178-182"
    → [178, 179, 180, 181, 182]
    """

    # 입력값을 문자열로 변환하고 앞뒤 공백 제거
    page_text = str(value or "").strip()

    # 지원하는 형식:
    # 1. 단일 페이지: 97
    # 2. 페이지 범위: 178-182
    # 3. 페이지 범위: 10~16
    #
    # \d+ → 숫자 1개 이상
    # \s* → 공백 0개 이상
    # [-~–] → 페이지 범위 구분 기호

    match = re.fullmatch(
        r"(\d+)\s*(?:[-~–]\s*(\d+))?",
        page_text
    )

    if not match:
        raise ValueError(
            f"지원하지 않는 pdf_pages 형식: {page_text!r}"
        )

    # 시작 페이지 번호
    first = int(match.group(1))

    # 끝 페이지 번호
    last = int(match.group(2) or first)

    # 잘못된 페이지 범위 검사
    if first < 1 or last < first:
        raise ValueError(
            f"잘못된 PDF 페이지 범위: {page_text!r}"
        )

    # 시작 페이지부터 끝 페이지까지 리스트 생성
    return list(range(first, last + 1))


# ==================================================
# 6. JSONL 파일 읽기 함수
# ==================================================

def read_chunks(path: Path):

    """
    JSONL 파일을 한 줄씩 읽고
    각 줄을 Python 딕셔너리로 변환한다.

    JSONL 예시:

    {"id": "001", "title": "사고유형1"}
    {"id": "002", "title": "사고유형2"}

    yield를 사용하므로 파일 전체를 한꺼번에
    메모리에 적재하지 않고 한 행씩 처리한다.
    """

    # UTF-8 형식으로 JSONL 파일 열기
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    # JSON 문자열을 Python 딕셔너리로 변환
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    # JSON 형식에 문제가 있으면
                    # 오류가 발생한 파일과 줄 번호 표시
                    raise ValueError(
                        f"{path}:{line_number} "
                        f"JSON 오류: {exc}"
                    ) from exc


# ==================================================
# 7. Vision LLM 분석 대상 목록 생성 함수
# ==================================================

def build_manifest() -> dict:

    """
    두 JSONL 파일에서 사고유형별 기준만 추출한다.

    처리 순서:

    1. JSONL 읽기
    2. doc_type 필터링
    3. category 필터링
    4. PDF 페이지 범위 변환
    5. 사고유형별 정보 저장
    6. 시작 페이지 중복 제거
    7. 전체 결과와 통계 반환
    """

    # 사고유형별 청크 목록
    entries = []

    # 실제 사용한 입력 파일 경로 저장
    sources = {}

    # --------------------------------------------------
    # 7-1. 각 JSONL 파일 순회
    # --------------------------------------------------

    for corpus in INPUT_LOCATIONS:
        # 해당 corpus의 실제 JSONL 파일 경로 확인
        path = locate_input(corpus)

        # 사용한 파일 경로 기록
        sources[corpus] = str(path)

        # JSONL 파일의 각 청크를 순서대로 확인
        for chunk in read_chunks(path):

            # ------------------------------------------
            # 7-2. doc_type 필터링
            # ------------------------------------------

            # "사고유형별 기준"이 아니면 제외
            if chunk.get("doc_type") != "사고유형별 기준":
                continue

            # ------------------------------------------
            # 7-3. category 필터링
            # ------------------------------------------

            # 자동차-자동차
            # 자동차-자전거
            # 자동차-보행자
            # 위 세 가지 카테고리에 해당하지 않으면 제외

            if chunk.get("category") not in TARGET_CATEGORIES:
                continue

            # ------------------------------------------
            # 7-4. PDF 페이지 범위 추출
            # ------------------------------------------

            try:
                pages = parse_pages(
                    chunk.get("pdf_pages")
                )

            except ValueError as exc:
                raise ValueError(
                    f"{corpus} / {chunk.get('id')}: {exc}"
                ) from exc

            # ------------------------------------------
            # 7-5. 사고유형 청크 정보 저장
            # ------------------------------------------

            entries.append({
                # 데이터 집합 이름
                "corpus": corpus,

                # JSONL 청크 고유 ID
                "chunk_id": chunk.get("id"),

                # 사고유형 카테고리
                "category": chunk.get("category"),

                # 사고유형 제목
                "title": chunk.get("title"),

                # 사고유형 코드 목록
                # 예: ["차2-6"]
                # 예: ["보24", "보25"]
                "codes": chunk.get("codes") or [],

                # 원본 PDF 페이지 문자열
                # 예: "178-182"
                "pdf_pages": chunk.get("pdf_pages"),

                # 사고유형이 시작되는 PDF 페이지
                "start_page": pages[0],

                # 사고유형 전체 PDF 페이지 범위
                "all_pages": pages,

                # 하나의 청크에 사고유형 코드가
                # 2개 이상 포함되어 있는지 확인
                "has_multiple_codes": (
                    len(chunk.get("codes") or []) > 1
                ),
            })

    # ==================================================
    # 8. 시작 페이지 중복 제거
    # ==================================================

    # 같은 PDF 페이지에 여러 사고유형 청크가 존재할 수 있다.
    # 예: PDF 97페이지에 보24와 보25가 함께 있는 경우
    # 동일한 페이지를 여러 번 분석하지 않도록
    # corpus와 시작 페이지 번호를 기준으로 그룹화한다.

    page_groups = defaultdict(list)

    for entry in entries:

        # corpus와 페이지 번호를 묶어서 고유 키 생성
        key = (
            entry["corpus"],
            entry["start_page"]
        )

        # 해당 페이지에 연결된 청크 ID 추가
        page_groups[key].append(
            entry["chunk_id"]
        )

    # ==================================================
    # 9. Vision LLM 시작 페이지 후보 생성
    # ==================================================

    start_pages = [

        {
            # PDF 데이터 집합
            "corpus": corpus,

            # 분석 후보 PDF 페이지 번호
            "pdf_page": page_number,

            # 해당 페이지와 연결된 사고유형 청크
            "chunk_ids": chunk_ids,

            # 아직 실제 그림과 표 존재 여부는
            # 확인되지 않았다는 안내
            "note": (
                "후보 페이지: 실제 사고 그림·표 "
                "존재 여부 확인 필요"
            ),
        }

        # corpus 및 페이지 번호 순으로 정렬
        for (corpus, page_number), chunk_ids
        in sorted(page_groups.items())
    ]

    # ==================================================
    # 10. 사고유형 전체 페이지 범위 통계
    # ==================================================

    # 각 corpus에서 사고유형 관련 페이지들을 중복 없이 저장한다.   
    # set 자료형은 중복된 값을 자동으로 제거한다.

    all_pages_by_corpus = defaultdict(set)

    for entry in entries:

        # 해당 사고유형의 전체 페이지 범위를
        # corpus별 집합에 추가한다.
        all_pages_by_corpus[
            entry["corpus"]
        ].update(
            entry["all_pages"]
        )

    # ==================================================
    # 11. 결과 통계 생성
    # ==================================================

    summary = {

        # 필터링된 전체 사고유형 청크 수
        "filtered_chunks": len(entries),

        # corpus별 사고유형 청크 수
        "chunks_by_corpus": {

            corpus: sum(
                e["corpus"] == corpus
                for e in entries
            )

            for corpus in INPUT_LOCATIONS
        },

        # 복수 사고유형 코드가 포함된 청크 수
        "multi_code_chunks": sum(
            e["has_multiple_codes"]
            for e in entries
        ),

        # 중복 제거한 시작 페이지 수
        "unique_start_pages": len(start_pages),

        # corpus별 시작 페이지 수
        "unique_start_pages_by_corpus": {

            corpus: sum(
                p["corpus"] == corpus
                for p in start_pages
            )

            for corpus in INPUT_LOCATIONS
        },

        # corpus별 전체 페이지 범위에서
        # 중복을 제거한 페이지 수
        "unique_pages_in_full_ranges_by_corpus": {

            corpus: len(
                all_pages_by_corpus[corpus]
            )

            for corpus in INPUT_LOCATIONS
        },
    }

    # ==================================================
    # 12. 최종 결과 반환
    # ==================================================

    return {

        # 결과 파일 설명
        "description": (
            "Vision LLM analysis candidates; "
            "NOT confirmed table/image pages"
        ),

        # 실제 사용한 JSONL 입력 파일 경로
        "sources": sources,

        # 전체 통계
        "summary": summary,

        # Vision LLM 분석용 시작 페이지 후보
        "start_page_candidates": start_pages,

        # 각 사고유형의 상세 메타데이터
        "accident_type_chunks": entries,
    }


# ==================================================
# 13. 메인 실행 함수
# ==================================================

def main():

    """ 
    작업 순서
    1. 출력 파일 경로 설정
    2. 사고유형별 페이지 후보 목록 생성
    3. JSON 파일 저장
    4. 결과 통계 출력
    """

    # ----------------------------------------------
    # 13-1. 실행 옵션 설정
    # ----------------------------------------------

    parser = argparse.ArgumentParser(
        description=__doc__
    )

    # --output 옵션으로 결과 파일 경로 변경 가능
    #
    # 별도 지정하지 않으면 기본 경로 사용:
    # rag/vision/page_manifest.json

    parser.add_argument(
        "--output",
        type=Path,
        default=(ROOT / "rag" / "vision" / "page_manifest.json"),
        help="JSON manifest 저장 경로",
    )

    # 명령행 옵션 읽기
    args = parser.parse_args()

    # ----------------------------------------------
    # 13-2. 페이지 후보 목록 생성
    # ----------------------------------------------

    result = build_manifest()

    # ----------------------------------------------
    # 13-3. 결과 저장 폴더 생성
    # ----------------------------------------------

    # rag/vision 폴더가 없으면 자동 생성
    # 이미 존재하면 오류 없이 진행
    args.output.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # ----------------------------------------------
    # 13-4. JSON 파일 저장
    # ----------------------------------------------

    args.output.write_text(
        json.dumps(
            result,
            ensure_ascii=False,  # 한글이 Unicode 이스케이프로 변환되지 않도록 설정
            indent=2   # 들여쓰기 2칸 설정
        ),
        encoding="utf-8"
    )

    # ----------------------------------------------
    # 13-5. 통계 정보 출력
    # ----------------------------------------------

    summary = result["summary"]

    print("=== Vision PDF 페이지 후보 준비 ===")

    print(
        f"사고유형 청크: "
        f"{summary['filtered_chunks']}개"
    )

    # corpus별 청크 수 출력
    for corpus, count in summary[
        "chunks_by_corpus"
    ].items():

        print(f"  {corpus} : {count}개")

    # 복수 코드가 포함된 청크 수
    print(
        "복수 사고유형 코드 포함 청크: "
        f"{summary['multi_code_chunks']}개"
    )

    # 중복 제거한 시작 페이지 수
    print(
        "중복 제거한 시작 페이지: "
        f"{summary['unique_start_pages']}개"
    )

    # corpus별 시작 페이지 및 전체 범위 통계
    for corpus, count in summary[
        "unique_start_pages_by_corpus"
    ].items():

        print(
            f"  {corpus}: "
            f"시작 페이지 {count}개"
        )

        print(
            "  전체 페이지 범위 내 고유 페이지: "
            f"{summary['unique_pages_in_full_ranges_by_corpus'][corpus]}개"
        )

    # ----------------------------------------------
    # 13-6. 시작 페이지 후보 미리보기
    # ----------------------------------------------

    print("\n[시작 페이지 후보 예시]")

    # 전체 목록 중 처음 10개만 출력
    for row in result[
        "start_page_candidates"
    ][:10]:

        print(
            f"  {row['corpus']} "
            f"/ PDF {row['pdf_page']}쪽 "
            f"/ {row['chunk_ids']}"
        )

    # ----------------------------------------------
    # 13-7. 저장 결과 출력
    # ----------------------------------------------

    print(f"\n결과 저장: {args.output}")

    print(
        "주의: 시작 페이지에 표가 있다는 보장은 없습니다. "
        "PDF 대조 후 API 호출하세요."
    )


# ==================================================
# 14. Python 파일 직접 실행 시 main() 호출
# ==================================================

if __name__ == "__main__":
    main()
