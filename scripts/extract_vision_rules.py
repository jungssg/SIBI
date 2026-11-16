import json
from pathlib import Path
from collections import defaultdict

import pymupdf

# ==================================================
# 1. 프로젝트 경로 설정
# ==================================================

ROOT = Path(__file__).resolve().parents[1]

# page_manifest.json 경로
MANIFEST_PATH = ROOT / "rag" / "vision" / "page_manifest.json"

# ==================================================
# 2. page_manifest.json 불러오기
# ==================================================

def load_manifest(path:Path) -> dict:
    """
    page_manifest.json 파일을 읽고
    Python 딕셔너리로 반환
    """
    
    if not path.is_file():
        raise FileNotFoundError( f'파일을 찾을 수 없습니다.: {path}')
    
    # json 파일 읽기
    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    
    return data

# ==================================================
# 3. PDF 페이지별 사고유형 정리
# ==================================================

def group_by_page(manifest: dict)-> list[dict]:
    """
    accident_type_chunks에서 다음 정보를 가져온다.

    - corpus
    - start_page
    - codes
    - chunk_id

    동일한 PDF 페이지에 존재하는 사고유형을 하나의 그룹으로 정리
    """

    # 같은 페이지의 사고유형을 묶기 위한 딕셔너리
    grouped = defaultdict(
        lambda: {
            "codes": [],
            "chunk_ids": []
        }
    )

    # 사고유형 청크 목록
    chunks = manifest["accident_type_chunks"]

    for chunk in chunks:
        # PDF 문서 구분
        corpus = chunk["corpus"]

        # 사고유형 시작 페이지
        page = chunk["start_page"]

        # 사고유형 코드 목록
        codes = chunk["codes"]

        # 원본 chunk_id
        chunk_id = chunk["chunk_id"]

        # corpus와 페이지 번호를 그룹 키로 사용
        key = (corpus, page)

        # 사고유형 코드 중복 제거
        for code in codes:
            if code not in grouped[key]["codes"]:
                grouped[key]["codes"].append(code)
        
        # 청크 ID 중복 제거
        if chunk_id not in grouped[key]["chunk_ids"]:
            grouped[key]["chunk_ids"].append(chunk_id)

    # 딕셔너리를 리스트로
    result = []

    
    for (corpus, page), values in grouped.items():
        result.append({
            "corpus": corpus,
            "pdf_page": page,
            "codes": values["codes"],
            "chunk_ids": values["chunk_ids"]
        })

    # PDF 문서명과 페이지 번호 순서로 정렬
    result.sort(
        key= lambda x:(x["corpus"], x["pdf_page"])
    )

    return result

# ============================================================
# 4. 테스트 실행
# ============================================================

if __name__ == "__main__":
    print("=== page_manifest.json 읽기 ===")

    # JSON 파일 읽기
    manifest = load_manifest(MANIFEST_PATH)

    # 페이지별 사고유형 저리
    pages = group_by_page(manifest)

    print(f"전체 처리 대상 페이지: {len(pages)}개")

    print("\n=== 페이지별 사고유형 확인(5개)===")

    for page in pages[:5]:
        print('='*50)

        print(f"코퍼스: {page['corpus']}")
        print(f"PDF 페이지: {page['pdf_page']}")
        print(f"사고유형 코드: {page['codes']}")
        print(f"원본 청크 ID: {page['chunk_ids']}")
    
    print("1단계 TEST 완료")
