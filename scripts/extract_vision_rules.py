import json
import pymupdf

from pathlib import Path
from collections import defaultdict
from typing import Literal
from pydantic import BaseModel, Field


# ==================================================
# 1. 프로젝트 경로 설정
# ==================================================

ROOT = Path(__file__).resolve().parents[1]

# page_manifest.json 경로
MANIFEST_PATH = ROOT / "rag" / "vision" / "page_manifest.json"

# ============================================================
# 1-1. PDF 파일 경로 설정
# ============================================================

# corpus별 원본 PDF 파일 경로
PDF_PATHS = {
    "common_2023": ROOT / "raw" / "pdf" / "230630_자동차사고 과실비율 인정기준_최종.pdf",

    "roundabout_2025": ROOT / "raw" / "pdf" / "250624_2차로형 회전교차로사고 과실비율 비정형기준.pdf"
}

# PDF에서 변환한 이미지를 저장할 폴더
IMAGE_DIR = ROOT / "rag" / "vision" / "images"

# 이미지 렌더링 배율
IMAGE_SCALE = 2.0

# 테스트할 페이지 개수
TEST_LIMIT = 5

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
# 5. corpus에 해당하는 PDF 파일 선택
# ============================================================

def get_pdf_path(corpus: str) -> Path:
    """
    corpus를 이용해 원본 PDF 파일 경로를 반환

    예시:
    common_2023
    → 자동차사고 과실비율 인정기준 PDF
    """

    # PDF_PATHS에 corpus가 존재하는지 확인
    if corpus not in PDF_PATHS:
        raise ValueError(
            f"지원하지 않는 corpus: {corpus!r}"
        )

    # corpus에 해당하는 PDF 경로 가져오기
    pdf_path = PDF_PATHS[corpus]

    # 실제 PDF 파일이 존재하는지 확인
    if not pdf_path.is_file():
        raise FileNotFoundError(
            f"PDF 파일을 찾을 수 없습니다: {pdf_path}"
        )

    return pdf_path

# ============================================================
# 6. PDF 페이지를 PNG 이미지로 변환
# ============================================================

def render_pdf_page(pdf_path: Path, pdf_page: int, output_path: Path) -> Path:
    
    """
    PDF의 특정 페이지를 PNG 이미지로 변환
    
    매개변수:
        pdf_path : 원본 PDF 파일 경로
        pdf_page : PDF 페이지 번호(1부터 시작)\
        output_path : 변환된 이미지저장 경로
    반환값: 저장된 이미지 파일 경로
    """

    # 이미지 저장 폴더 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # PDF 파일 열기
    with pymupdf.open(pdf_path) as doc:
        total_pages = len(doc)

        if not 1 <= pdf_page <= total_pages:
            raise ValueError(
                f"잘못된 PDF 페이지 번호: {pdf_page}"
                f"(전체 페이지: {total_pages})"
            )
        
        # pymupdf 는 페이지 인덱스가 0부터 시작
        page = doc[pdf_page -1]

        # 이미지 확대 배율 설정
        matrix = pymupdf.Matrix(
            IMAGE_SCALE,
            IMAGE_SCALE
        )

        # PDF 페이지 이미지로 렌더링
        pix = page.get_pixmap(
            matrix=matrix,
            alpha=False
        )

        # 이미지 저장
        pix.save(str(output_path))

    return output_path


# ============================================================
# 7. Vision LLM JSON 출력 구조 정의
# ============================================================

class FaultRatio(BaseModel):
    """
    원본 PDF에 표시된 당사자별 기본 과실비율
    
    예시:
        A60 B40
        => A: 60, B: 40
        
        보행자 기본 과실비율 10
        => 보행자 :10

        원문에 없는 상대방 비율은 임의로 생성하지 않는다
    """

    party: str = Field(
        description= "원문에 표시된 당사자 구분(A, B, 보행자 등)"
    )

    value: int = Field(
        description= "원본 표에 표시된 기본 과실비율"
    )

class Adjustment(BaseModel):
    """
    사고유형별 과실비율 수정요소
    """

    party: str = Field(
        description= "수정요소가 적용되는 당사자"
    )

    condition: str = Field(
        description= "수정요소 이름. 차량 표시와 항목 번호 제외"
    )

    value: int|None = Field(
        description= "조정값. 가산은 양수, 감산은 음수"
    )

    original_text: str = Field(
        description= "원본 표의 수정요소 이름과 번호"
    )

    value_status: Literal[
        "적용값",
        "비적용",
        "공란",
        "확인불가"
    ] = Field(
        description= "원본 표에서 조정값이 표시된 상태"
    )


class AccidentRule(BaseModel):
    """
    사고유형 하나에 대한 추출 결과.
    """

    accident_code: str = Field(
        description= "사고유형 코드"
    )

    title: str = Field(
        description= "사고유형 제목"
    )

    participants: list[str] = Field(
        description= "원본에 표시된 사고 당사자와 진행 상황"
    )

    base_fault: list[FaultRatio] = Field(
        description= "당사자별 기본 과실비율"
    )

    adjustments: list[Adjustment] = Field(
        description= "원본 표의 순서대로 정리한 수정요소"
    )

    uncertainties: list[str] = Field(
        description= "읽기 어렵거나 확인할 수 없는 정보"
    )


class PageExtraction(BaseModel):
    """
    PDF 한 페이지에서 추출한 여러 사고유형.

    예시:
    PDF 47페이지 => 보3, 보4
    """

    accidents: list[AccidentRule]=Field(
        description= "페이지에 포함된 사고유형별 추출 결과"
    )

    page_uncertainties: list[str]=Field(
        description= "페이지 전체에서 확인이 필요한 내용"
    )


# ============================================================
# 8. Vision LLM 시스템 프롬프트
# ============================================================

SYSTEM_PROMPT = """
너는 자동차사고 과실비율 인정기준 PDF의
사고 그림과 과실비율 표를 분석하는 데이터 추출 전문가다.

[분석 목적]
PDF 페이지 이미지에 포함된 사고유형별 기준을
각각 독립적인 데이터로 추출한다.

[1. 사고유형 구분]
- 한 페이지에 여러 사고유형이 있을 수 있다.
- 사용자 메시지에서 지정한 사고유형 코드를 대상으로 분석한다.
- 각 사고유형을 독립적으로 추출한다.
- 서로 다른 사고유형의 기본 과실비율과 수정요소를 혼합하지 않는다.
- 요청한 사고유형이 이미지에서 확인되지 않으면 추측해서 생성하지 않는다.
- 확인된 사고유형은 각각 accidents 배열에 저장한다.

[2. 사고 그림 분석]
- 각 사고유형의 그림을 독립적으로 분석한다.
- 사고 당사자와 진행 방향을 확인한다.
- 그림에 없는 내용을 임의로 추측하지 않는다.

[3. 기본 과실비율 추출]
- 기본 과실비율은 원본 표에 표시된 숫자 그대로 추출한다.
- 당사자 구분을 혼동하지 않는다.
- 자동차-자동차 사고는 A/B 비율을 확인한다.
- 자동차-보행자 사고처럼 한쪽 비율만 표시된 경우
  표시된 비율만 추출한다.
- 표시되지 않은 상대방 비율을 임의로 계산하지 않는다.

[4. 수정요소 표 추출]
- 각 사고유형의 수정요소 표를 따로 분석한다.
- 첫 번째 행부터 마지막 행까지 빠짐없이 확인한다.
- 번호가 있는 항목과 번호가 없는 항목을 모두 추출한다.
- 같은 수정요소가 서로 다른 당사자에게 적용되면
  별개의 항목으로 추출한다.
- 수정요소 이름을 임의로 수정하거나 바꾸지 않는다.
- 그림에서 인식한 사고 상황으로 표의 원문을 변경하지 않는다.
- 조정값의 +와 - 부호를 정확하게 구분한다.
- condition에는 당사자 표시와 항목 번호를 제외한 이름을 기록한다.
- original_text에는 원본 표의 항목 번호와 명칭을 보존한다.
- 조정값이 표시된 경우 value_status를 '적용값'으로 기록한다.
- '비적용'과 빈 셀을 구분한다.
- 확인할 수 없는 조정값은 추측하지 않는다.

[5. 누락 방지]
- 사고유형마다 수정요소 표를 처음부터 끝까지 확인한다.
- 추출한 항목을 원본 표와 다시 대조한다.
- 다른 사고유형의 항목이 섞이지 않았는지 확인한다.
- 읽을 수 없는 항목은 uncertainties에 기록한다.
- 요청된 사고유형을 확인할 수 없으면
  page_uncertainties에 해당 코드와 이유를 기록한다.

[6. 출력 원칙]
- 원본 PDF에 존재하는 정보만 추출한다.
- 추출 결과의 정확성이 불확실하면 추측하지 않는다.
- 사고유형별 정보를 각각 독립적으로 반환한다.
"""










# ============================================================
# 4. 테스트 실행
# 2단계 테스트: PDF 페이지 이비지 변환
# 3단계 테스트: Pydantic 출력 구조 확인
# ============================================================

if __name__ == "__main__":
    # 1단계 테스트
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

    # 2단계 테스트
    
    test_pages = pages[:TEST_LIMIT]

    for index, page_info in enumerate(test_pages, start=1):
        
        # manifest에서 추출한 정보
        corpus = page_info['corpus']
        pdf_page = page_info['pdf_page']
        codes = page_info['codes']

        # corpus로 PDF 파일 선택
        pdf_path = get_pdf_path(corpus)

        # 저장할 이미지 파일명 생성
        image_name = f"{corpus}_page_{pdf_page:04d}.png"

        # 이미지 저장 경로
        output_path = IMAGE_DIR / image_name

        # PDF 페이지 PNG 이미지로 변환
        saved_path = render_pdf_page(
            pdf_path = pdf_path,
            pdf_page = pdf_page,
            output_path= output_path
        )

        print("=" * 50)
        print(f"진행: {index}/{len(test_pages)}")
        print(f"코퍼스: {corpus}")
        print(f"PDF 페이지: {pdf_page}")
        print(f"사고유형 코드: {codes}")
        print(f"저장 경로: {saved_path}")
    
    print("\n=== 2단계 테스트 완료 ===")

    # 3단계 테스트
    print("\n=== 3단계: JSON 출력 구조 확인 ===")

    print("출력 모델:", PageExtraction.__name__)

    print(
        "최상위 필드:",
        list(PageExtraction.model_fields.keys())
    )

    print(
        "사고유형 필드:",
        list(AccidentRule.model_fields.keys())
    )

    print(
        "수정요소 필드:",
        list(Adjustment.model_fields.keys())
    )

    print("\n=== 3단계 테스트 완료 ===")
