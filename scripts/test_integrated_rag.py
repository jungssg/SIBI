
from pathlib import Path
import os

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client


# ==========================================
# 1. 환경 설정
# ==========================================

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / "env" / ".env")

openai_client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"]
)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)

MODEL = "text-embedding-3-large"
DIMENSIONS = 1536


# ==========================================
# 2. 사용자 사고 설명
# ==========================================

query = (
    "신호등과 비보호 좌회전 표지가 있는 교차로에서 "
    "녹색신호에 직진하는 차량과 "
    "맞은편에서 녹색신호에 비보호 좌회전하는 차량이 "
    "충돌한 사고."
)
# 사용자 입력 예시
accident_category = "자동차-자동차"

print("\n=== 사용자 사고 설명 ===")
print(query)


# ==========================================
# 3. 자연어 임베딩
# ==========================================

embedding_response = openai_client.embeddings.create(
    model=MODEL,
    input=query,
    dimensions=DIMENSIONS
)

query_embedding = embedding_response.data[0].embedding

print(f"\n임베딩 차원: {len(query_embedding)}")


# ==========================================
# 4. 사고유형 후보 검색
# ==========================================

search_response = supabase.rpc(
    "match_accident_types",
    {
        "query_embedding": query_embedding,
        "p_corpus": [
            "common_2023",
            "roundabout_2025"
        ],
        "p_category": accident_category,
        "match_count": 5
    }
).execute()

candidates = search_response.data or []

print("\n=== 사고유형 후보 TOP 5 ===")

for rank, item in enumerate(candidates, start=1):
    print(
        f"{rank}위 | "
        f"{item['codes']} | "
        f"{item['title']} | "
        f"유사도 {item['similarity']:.4f}"
    )


# ==========================================
# 5. 사고유형 후보의 PDF 원문 조회
# ==========================================

print("\n=== PDF 원문 연결 결과 ===")

for rank, candidate in enumerate(candidates, start=1):

    corpus = candidate["corpus"]
    chunk_id = candidate["chunk_id"]

    # 벡터 재검색 없이 원본 청크를 정확히 조회
    original_response = (
        supabase.table("rag_chunks")
        .select(
            "corpus,chunk_id,title,codes,"
            "pdf_pages,printed_pages,text"
        )
        .eq("corpus", corpus)
        .eq("chunk_id", chunk_id)
        .limit(1)
        .execute()
    )

    originals = original_response.data or []

    if not originals:
        print(f"\n{rank}위: PDF 원문을 찾지 못했습니다.")
        continue

    original = originals[0]

    print("\n" + "=" * 60)
    print(f"후보 순위: {rank}")
    print(f"사고유형: {candidate['title']}")
    print(f"사고유형 코드: {candidate['codes']}")
    print(f"유사도: {candidate['similarity']:.4f}")
    print(f"PDF 페이지: {original['pdf_pages']}")
    print(f"인쇄 페이지: {original['printed_pages']}")
    print(f"원문 글자 수: {len(original['text'])}")

    # 전체 원문은 1위 후보만 출력
    if rank == 1:
        print("\n--- 1위 후보 PDF 원문 전체 ---")
        print(original["text"])
    else:
        print("\n원문 미리보기:")
        print(original["text"][:200])


print("\n=== 통합 검색 테스트 완료 ===")
print("주의: 검색 순위는 사고유형의 최종 확정 결과가 아닙니다.")
