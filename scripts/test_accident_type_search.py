
from pathlib import Path
import os

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client


# 1. 환경변수
BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / "env" / ".env")

openai_client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"]
)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)


# 2. 테스트 사고 설명
query = (
    "신호등과 비보호 좌회전 표지가 있는 교차로에서 "
    "녹색신호에 직진하는 차량과 "
    "맞은편에서 녹색신호에 비보호 좌회전하는 차량이 "
    "충돌한 사고."
)

print("검색 질문:", query)


# 3. 사고 설명 임베딩
response = openai_client.embeddings.create(
    model="text-embedding-3-large",
    input=query,
    dimensions=1536
)

query_embedding = response.data[0].embedding

print("임베딩 차원:", len(query_embedding))


# 4. 사고유형 검색 RPC 호출
response = supabase.rpc(
    "match_accident_types",
    {
        "query_embedding": query_embedding,
        "p_corpus": [
            "common_2023",
            "roundabout_2025"
        ],
        "p_category": "자동차-자동차",
        "match_count": 10
    }
).execute()

results = response.data or []


# 5. 검색 결과 출력
print(f"\n검색 결과: {len(results)}개")

for rank, row in enumerate(results, start=1):
    print("=" * 50)
    print(f"순위: {rank}")
    print(f"코드: {row['codes']}")
    print(f"사고유형: {row['title']}")
    print(f"코퍼스: {row['corpus']}")
    print(f"유사도: {row['similarity']:.4f}")


# 6. 정답 후보 순위 확인
print("\n=== 차2-6 검색 결과 ===")

found = False

for rank, row in enumerate(results, start=1):
    if "차2-6" in (row.get("codes") or []):
        found = True
        print(f"차2-6 순위: {rank}위")
        print(f"유사도: {row['similarity']:.4f}")
        break

if not found:
    print("차2-6이 TOP 10에 포함되지 않았습니다.")
