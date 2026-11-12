
from pathlib import Path
import os
import time

from dotenv import load_dotenv
from openai import OpenAI
from supabase import create_client


# ==========================================
# 1. 환경 설정
# ==========================================

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / "env" / ".env")

MODEL = "text-embedding-3-large"
DIMENSIONS = 1536
MODEL_TAG = f"{MODEL}@{DIMENSIONS}"
BATCH_SIZE = 16

openai_client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"]
)

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SECRET_KEY"]
)


# ==========================================
# 2. 임베딩되지 않은 사고유형 조회
# ==========================================

response = (
    supabase.table("accident_type_index")
    .select(
        "corpus,chunk_id,codes,title,"
        "category,search_text"
    )
    .is_("embedding", "null")
    .order("chunk_id")
    .execute()
)

rows = response.data or []

print(f"임베딩 대상: {len(rows)}개")

if not rows:
    print("새롭게 임베딩할 사고유형이 없습니다.")
    raise SystemExit(0)


# ==========================================
# 3. 배치별 임베딩 및 Supabase 저장
# ==========================================

completed = 0

for start in range(0, len(rows), BATCH_SIZE):

    batch = rows[start:start + BATCH_SIZE]

    texts = [
        row["search_text"]
        for row in batch
    ]

    started_at = time.time()

    # OpenAI 임베딩 생성
    result = openai_client.embeddings.create(
        model=MODEL,
        input=texts,
        dimensions=DIMENSIONS
    )

    vectors = [
        item.embedding
        for item in sorted(
            result.data,
            key=lambda item: item.index
        )
    ]

    # 임베딩 생성 결과 검증
    if len(vectors) != len(batch):
        raise ValueError(
            "임베딩 개수와 입력 데이터 개수가 다릅니다."
        )

    for vector in vectors:
        if len(vector) != DIMENSIONS:
            raise ValueError(
                f"임베딩 차원이 {DIMENSIONS}이 아닙니다."
            )

    # Supabase 저장 데이터 구성
    payload = []

    for row, vector in zip(batch, vectors):

        payload.append({
            "corpus": row["corpus"],
            "chunk_id": row["chunk_id"],
            "codes": row["codes"],
            "title": row["title"],
            "category": row["category"],
            "search_text": row["search_text"],
            "embedding": vector,
            "embedding_model": MODEL_TAG
        })

    # 기존 UNIQUE(corpus, chunk_id)를 이용해 갱신
    supabase.table("accident_type_index").upsert(
        payload,
        on_conflict="corpus,chunk_id"
    ).execute()

    completed += len(batch)

    elapsed = time.time() - started_at

    print(
        f"{completed}/{len(rows)} 완료 "
        f"({elapsed:.1f}s)"
    )


# ==========================================
# 4. 종료
# ==========================================

print("\n=== 사고유형 임베딩 적재 완료 ===")
print(f"이번 실행 임베딩: {completed}개")
print(f"임베딩 모델: {MODEL_TAG}")
print("저장 테이블: accident_type_index")
