#기존 rag_chunk | 변경 없음
# 기존 임베딩 | 유지
# Supabase 작업 | SELECT 조회만 수행
# OpenAI API | 새로운 짧은 텍스트 임베딩 생성
# API 비용 | 소액
# 결과 | 제목 기반 상위 10개, 차2-6 순위

from pathlib import Path
import os
import math

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client


# 설정
# 프로젝트 및 환경변수
BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / "env" / ".env")

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)

MODEL = "text-embedding-3-large"
DIMENSIONS = 1536

# 검색 실험 질문
query = (
    "신호등과 비보호 좌회전 표지가 있는 교차로에서 "
    "녹색신호에 직진하는 차량과 "
    "맞은편에서 녹색신호에 비보호 좌회전하는 차량이 "
    "충돌한 사고."
)

# 1. 사고유형별 기준만 가져오기
rows = []
start = 0
page_size = 500

while True:
    response = (
        supabase.table("rag_chunks")
        .select(
            "corpus,chunk_id,title,codes,"
            "doc_type,category,section_path,pdf_pages"
        )
        .in_("corpus", ["common_2023", "roundabout_2025"])
        .eq("doc_type", "사고유형별 기준")
        .range(start, start + page_size - 1)
        .execute()
    )

    batch = response.data or []
    rows.extend(batch)

    if len(batch) < page_size:
        break
    start += page_size

# 이번 테스트는 자동차 대 자동차 사고만 검색
rows = [
    r for r in rows
    if r.get("category") == "자동차-자동차"
]

print(f"검색 대상 사고유형 청크: {len(rows)}개")

# 2. 짧은 검색 텍스트 구성
def make_search_text(row):
    path = row.get("section_path") or []
    # 하위 경로는 사고의 방향 및 교차로 유형을 설명
    context = " > ".join(path[-2:])

    return (
        f"사고유형: {row.get('title') or ''}\n"
        f"사고 상황 분류: {context}"
    )

texts = [query] + [make_search_text(r) for r in rows]

# 3. 검색 질문과 사고유형을 새로 임베딩
# DB에는 저장하지 않음
vectors = []

for i in range(0, len(texts), 32):
    batch = texts[i:i + 32]

    response = openai_client.embeddings.create(
        model=MODEL,
        input=batch,
        dimensions=DIMENSIONS
    )

    vectors.extend([
        item.embedding
        for item in sorted(response.data, key=lambda x: x.index)
    ])

# 4. 코사인 유사도 계산
def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)

query_vector = vectors[0]

results = []

for row, vector in zip(rows, vectors[1:]):
    score = cosine_similarity(query_vector, vector)
    results.append((score, row))

results.sort(key=lambda x: x[0], reverse=True)

print("\n=== 사고유형 제목 기반 검색 TOP 10 ===")

for rank, (score, row) in enumerate(results[:10], start=1):
    print(f"\n{rank}위 | 유사도 {score:.4f}")
    print(f"코드: {row['codes']}")
    print(f"제목: {row['title']}")
    print(f"PDF 페이지: {row['pdf_pages']}")

# 5. 정답 후보의 전체 검색 순위 확인
print("\n=== 차2-6 검색 순위 확인 ===")

found = False

for rank, (score, row) in enumerate(results, start=1):
    if "차2-6" in (row.get("codes") or []):
        found = True
        print(f"차2-6 순위: {rank}위 / {len(results)}개")
        print(f"유사도: {score:.4f}")
        break

if not found:
    print("차2-6이 검색 대상에 없습니다.")
