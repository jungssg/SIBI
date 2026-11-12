# RAG 검색 테스트
# 1. 자연어 → 임베딩
# 2. Supabase 벡터 검색
# 3. 검색 결과 출력
# 테스트 사고 상황: 신호등이 있는 교차로에서 A 차량은 직진하고 있었고, B 차량은 반대편에서 비보호 좌회전하던 중 충돌했다.
# 검색 대상: 자동차사고 과실비율 인정기준 PDF 2종
# 검색 방식: 1536차원 코사인 유사도
# 반환 결과: 상위 5개 chunk


from pathlib import Path
import os

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client


# 설정
# 프로젝트 루트 경로
BASE_DIR = Path(__file__).resolve().parents[1]

# 환경변수 로드
load_dotenv(BASE_DIR / "env" / ".env")

# API 클라이언트
openai_client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"]
)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)

# 테스트 사고 설명
query = (
    "신호등과 비보호 좌회전 표지가 있는 교차로에서 "
    "녹색신호에 직진하는 차량과 "
    "맞은편에서 녹색신호에 비보호 좌회전하는 차량이 "
    "충돌한 사고."
)

print("검색 질문:", query)

# 1. 자연어 → 임베딩
embedding_response = openai_client.embeddings.create(
    model="text-embedding-3-large",
    input=query,
    dimensions=1536
)

query_embedding = embedding_response.data[0].embedding

print("임베딩 차원:", len(query_embedding))

# 2. Supabase 벡터 검색
response = supabase.rpc(
    "match_rag_chunks",
    {
        "query_embedding": query_embedding,
        "p_corpus": [
            "common_2023",
            "roundabout_2025"
        ],
        "p_codes": None,
        "match_count": 10
    }
).execute()

# 3. 검색 결과 출력
results = response.data or []

print(f"\n검색 결과: {len(results)}개")

for idx, row in enumerate(results, start=1):
    print(f"\n{'=' * 50}")
    print(f"순위: {idx}")
    print(f"코퍼스: {row['corpus']}")
    print(f"사고유형: {row['title']}")
    print(f"사고유형 코드: {row['codes']}")
    print(f"PDF 페이지: {row['pdf_pages']}")
    print(f"유사도: {row['similarity']:.4f}")
    print(f"본문 미리보기: {row['text'][:200]}")