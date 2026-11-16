from asyncio import Condition
import os
import base64
from pathlib import Path
from typing import Literal

import pymupdf
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field

# ==================================================
# 1. 프로젝트 경로 설정
# ==================================================

ROOT = Path(__file__).resolve().parents[1]

# 실제 PDF 파일 경로로 수정
PDF_PATH = ROOT / "raw" / "pdf" / "230630_자동차사고 과실비율 인정기준_최종.pdf"

# 이미지 저장 경로
IMAGE_PATH = ROOT / "rag" / "test_vision_page.png"

# PDF 페이지 번호
PDF_PAGE = 178


# ==================================================
# 2. 환경변수 설정
# ==================================================

load_dotenv(ROOT / "env" / ".env")

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    raise ValueError("OPENAI_API_KEY가 설정되지 않았습니다.")

client = OpenAI(api_key=api_key)


# ==================================================
# 3. Pydantic 데이터 구조 정의
# ==================================================

class BaseFault(BaseModel):
    A: int | None = Field(
        description="A 차량의 기본 과실비율"
    )
    B: int | None = Field(
        description="B 차량의 기본 과실비율"
    )

class Adjustment(BaseModel):
    vehicle: Literal["A","B"] | None = Field(
        description="수정요소가 적용되는 차량"
    )
    condition: str = Field(
        description="수정요소 이름. 원문 그대로 추출"
    )
    value: int | None = Field(
        description=(
            "수정요소 조정값. "
            "+10은 10, -10은 -10으로 저장"
            "확인 불가 또는 값이 없으면 null"
        )
    )
    original_text: str = Field(
        description= "표에 표시된 수정요소 원문"
    )



class AccidentRule(BaseModel):
    accident_code: str = Field(
        description="사고유형 코드"
    )

    title: str = Field(
        description="사고유형 제목"
    )

    vehicle_a: str = Field(
        description="그림에서 확인한 A 차량의 진행 방향"
    )

    vehicle_b: str = Field(
        description="그림에서 확인한 B 차량의 진행 방향"
    )

    base_fault: BaseFault

    adjustments: list[Adjustment]= Field(
        description="표에 기재된 순서대로 추출한 수정요소 목록"
    )

    discrepancies: list[str]= Field(
        description="그림과 표에서 발견된 불일치 사항"
    )

    uncertainties: list[str]=Field(
        description="확인할 수 없거나 불명확한 정보"
    )


# ==================================================
# 4. PDF 페이지 → 이미지 변환
# ==================================================

print("=== PDF 이미지 변환 ===")

if not PDF_PATH.is_file():
    raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다.{PDF_PATH}")

IMAGE_PATH.parent.mkdir(
    parents=True,
    exist_ok=True
)

with pymupdf.open(PDF_PATH) as doc:
    if PDF_PAGE < 1 or PDF_PAGE > len(doc):
        raise ValueError(f"페이지 범위 오류: 1~{len(doc)}")
    
    page = doc[PDF_PAGE -1]

    pix = page.get_pixmap(
        matrix = pymupdf.Matrix(2, 2),
        alpha = False
    )

    pix.save(str(IMAGE_PATH))

print(f"PDF 페이지: {PDF_PAGE}")
print(f"이미지 저장: {IMAGE_PATH}")


# ==================================================
# 5. 이미지 → Base64 변환
# ==================================================

image_bytes = IMAGE_PATH.read_bytes()

base64_image = base64.b64encode(
    image_bytes
).decode("utf-8")


# ==================================================
# 6. Vision LLM 프롬프트 작성
# ==================================================

SYSTEM_PROMPT = """
너는 자동차사고 과실비율 인정기준 PDF의
사고 그림과 과실비율 표를 분석하는 데이터 추출 전문가다.

[분석 목적]
PDF 이미지에서 사고유형별 정보를 정확하게 추출한다.
사고 상황을 해석하는 작업과 표의 데이터를 추출하는 작업을
명확하게 구분한다.

[1. 사고 그림 분석]
- 그림에 표시된 A 차량과 B 차량의 위치를 확인한다.
- 각 차량의 진행 방향을 화살표를 기준으로 분석한다.
- 그림에 표시된 도로 구조를 분석한다.
- 그림에 없는 사고 상황을 임의로 추측하지 않는다.

[2. 기본 과실비율 추출]
- 기본 과실비율은 반드시 표에 표시된 수치를 사용한다.
- A 차량과 B 차량의 값을 혼동하지 않는다.
- 그림을 근거로 기본 과실비율을 변경하지 않는다.
- 확인할 수 없는 수치는 null로 기록한다.

[3. 수정요소 표 추출]
- 수정요소 이름은 표에 작성된 원문 그대로 추출한다.
- 수정요소 이름을 임의로 수정하지 않는다.
- 그림에서 인식한 도로 구조로 수정요소 이름을 변경하지 않는다.
- 수정요소 이름과 적용 차량을 구분한다.
- A/B 조정값 열을 확인하여 적용 차량을 결정한다.
- '+'와 '-' 부호를 반드시 구분한다.
- 수정요소를 원본 표의 순서대로 추출한다.
- 표에 없는 수정요소를 추가하지 않는다.

[3-1. 수정요소 누락 방지 규칙]
- 과실비율 조정예시 표의 모든 행을 빠짐없이 확인한다.
- 표의 첫 번째 행부터 마지막 행까지 순서대로 추출한다.
- 수정요소 앞에 번호(①, ②, ③ 등)가 있어도 누락하지 않는다.
- 번호가 없는 수정요소도 반드시 추출한다.
- 하나의 행에 조정값이 하나만 표시되어 있어도 추출한다.
- A 차량과 B 차량의 수정요소를 각각 확인한다.
- 동일한 이름의 수정요소라도 적용 차량이 다르면 별도 항목으로 추출한다.
- 조정값이 양수 또는 음수라는 이유로 항목을 제외하지 않는다.
- 추출을 완료한 후 표의 모든 행과 추출된 항목을 다시 대조한다.
- 표에 있는데 추출하지 못한 항목이 있다면 누락된 내용을 보완한다.
- 읽을 수 없는 행이 있으면 임의로 생략하지 않고 uncertainties에 기록한다.

[4. 이미지와 표의 불일치 처리]
- 사고 그림과 표의 내용을 독립적으로 분석한다.
- 두 정보가 다르게 보이더라도 임의로 수정하거나 통합하지 않는다.
- 실제로 확인되는 불일치만 discrepancies에 기록한다.
- 수정요소 표의 조건은 기본 사고 그림과 다를 수 있으므로
  이를 자동으로 오류라고 판단하지 않는다.

[5. 불확실한 정보 처리]
- 읽기 어려운 글자나 숫자를 추측하지 않는다.
- 확인할 수 없는 내용은 uncertainties에 기록한다.
- 비어 있는 셀을 0으로 해석하지 않는다.
- '비적용'과 '조정값 없음'을 구분한다.

[6. 데이터 추출 규칙]
- 사고유형별 기본 과실비율을 추출한다.
- 수정요소별 적용 차량, 이름, 조정값을 추출한다.
- value에는 부호를 포함한 정수값을 기록한다.
- 조정값을 확인할 수 없다면 null을 사용한다.
- condition에는 A/B 차량 표시와 항목 번호를 제외한
  순수한 수정요소 이름만 기록한다.
- original_text에는 항목 번호와 A/B 표시를 포함한
  원문을 가능한 그대로 보존한다.
"""


# ==================================================
# 7. Vision LLM 분석 + Structured Outputs
# ==================================================

# Vision LLM 설정
MODEL = "gpt-6-luna"
REASONING_EFFORT = "low"

print("\n=== Vision LLM 분석 시작")

response = client.responses.parse(
    model=MODEL,
    reasoning={'effort': REASONING_EFFORT},
    input=[
        {
            "role":"system",
            "content": SYSTEM_PROMPT
        },
        {
            "role":"user",
            "content":[
                {
                    "type": "input_text",
                    "text": (
                        "이 PDF 페이지 이미지의 사고유형,"
                        "기본 과실비율, 수정요소 표를 분석해줘."
                        "이미지에 표시된 정보를 정확하게 추출해."
                    )
                },
                {
                    "type": "input_image",
                    "image_url": (
                        f"data:image/png;base64,{base64_image}"
                        ),
                    "detail": "high"
                }
            ]
        }
    ],
    text_format= AccidentRule
)



# ==================================================
# 8. 결과 확인
# ==================================================

print("\n=== Vision LLM 분석 결과 ===")

result = response.output_parsed

if result is None:
    raise RuntimeError(
        "구조화된 분석 결과를 받지 못했습니다."
        f"응답 상태: {response.status}"
    )

print(result.model_dump_json(
    indent=2
    ))

# ==================================================
# 9. 기본 데이터 검증
# ==================================================
print("\n=== 데이터 검증 ===")

fault_a = result.base_fault.A
fault_b = result.base_fault.B

if fault_a is None or fault_b is None:
    print("[확인 필요] 기본 과실비율에 누락된 값이 있습니다.")

elif fault_a + fault_b != 100:
    print("[검증 실패] 기본 과실비율의 합계가 100이 아닙니다.")

else:
    print("[검증 통과] 기본 과실비율 합계: 100")

print(f"추출된 수정요소: {len(result.adjustments)}개")

for index, adjustment in enumerate(result.adjustments, start=1):
    if adjustment.value is None:
        print(f"[확인 필요] {index}번 수정요소: {adjustment.original_text}")
    
    if adjustment.vehicle is None:
        print(f"[확인 필요] {index}번 수정요소의 적용 차량이 불명확합니다.")

if result.uncertainties:
    print("\n확인 불가 항목:")

    for item in result.uncertainties:
        print(f"- {item}")

if result.discrepancies:
    print("\n그림과 표의 불일치:")

    for item in result.discrepancies:
        print(f"- {item}")


# ==================================================
# 10. 토큰 사용량
# ==================================================

print("\n=== 토큰 사용량 ===")

if response.usage:
    print(f"입력 토큰: {response.usage.input_tokens}")
    print(f"출력 토큰: {response.usage.output_tokens}")
    print(f"총 토큰: {response.usage.total_tokens}")

print("\n=== 테스트 완료 ===")