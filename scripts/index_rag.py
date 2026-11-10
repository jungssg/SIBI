#!/usr/bin/env python3
"""rag/*/*chunks.jsonl 을 OpenAI 로 임베딩해서 Supabase rag_chunks 테이블에 적재한다.

모델: text-embedding-3-large, dimensions=1536  (DB 컬럼 embedding vector(1536) 과 일치해야 함)

사용 예
    python scripts/index_rag.py --dry-run                 # API 호출 없이 파일/매핑만 검증
    python scripts/index_rag.py --corpus law_roadtraffic  # 특정 코퍼스만
    python scripts/index_rag.py                           # 전체 (이미 적재된 동일 청크는 건너뜀)
    python scripts/index_rag.py --force                   # 건너뛰지 않고 전부 다시 임베딩

필요 환경변수 (.env 또는 셸)
    (env/.env 를 먼저 찾고, 없으면 프로젝트 루트의 .env. --env-file 로 직접 지정 가능)
    OPENAI_API_KEY
    SUPABASE_URL                    예) https://velvkmwqjvfgdoqlyfne.supabase.co
    SUPABASE_SECRET_KEY             sb_secret_... 서버 전용 키(구 service_role 대체). 화면 코드나 git 에 넣지 말 것
                                    (구 SUPABASE_SERVICE_ROLE_KEY 도 임시로 인식하지만 곧 폐기 예정)
    ※ sb_publishable_... 키는 RLS 가 적용되어 적재할 수 없으므로 여기서 쓰지 않는다.

설치
    pip install -U openai supabase python-dotenv   # supabase 는 최신 버전 사용(새 키 형식 sb_...)

동작
    - 같은 (corpus, chunk_id) 는 upsert 로 덮어쓰므로 여러 번 실행해도 중복되지 않는다.
    
    - DB에 이미 같은 text 와 같은 embedding_model 로 임베딩된 청크는 건너뛴다(--force 로 무시).
    - 임베딩 대상은 청크의 text 필드(문맥 헤더 + 본문)이다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 1536
EMBED_TAG = f"{EMBED_MODEL}@{EMBED_DIM}"  # rag_chunks.embedding_model 에 기록

# 폴더명 -> DB corpus 값
CORPORA = {
    "common_rag": "common_2023",
    "roundabout_rag": "roundabout_2025",
    "law_rag": "law_roadtraffic",
}

# chunks.jsonl 필드 중 rag_chunks 컬럼으로 직접 들어가는 것
COLUMN_FIELDS = {
    "id", "title", "doc_type", "category", "codes", "priority", "section_path",
    "pdf_pages", "printed_pages", "source", "url", "text", "char_count",
}
# 컬럼으로 넣지 않고 버리는 필드 (corpus 는 폴더에서 결정)
DROP_FIELDS = {"corpus"}


def clean_str(v):
    """None/빈 문자열 -> None, NUL 문자 제거."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace("\x00", "").strip()
        return v or None
    return v


def to_row(rec: dict, corpus: str) -> dict:
    """chunks.jsonl 한 줄 -> rag_chunks 행 (embedding 제외)."""
    text = (rec.get("text") or "").replace("\x00", "")
    if not text.strip():
        raise ValueError(f"text 가 비어 있음: id={rec.get('id')}")
    section = rec.get("section_path") or []
    if isinstance(section, str):
        section = [section]
    extra = {k: v for k, v in rec.items() if k not in COLUMN_FIELDS and k not in DROP_FIELDS}
    return {
        "corpus": corpus,
        "chunk_id": str(rec["id"]),
        "title": clean_str(rec.get("title")),
        "doc_type": clean_str(rec.get("doc_type")),
        "category": clean_str(rec.get("category")),
        "codes": list(rec.get("codes") or []),
        "priority": clean_str(rec.get("priority")),
        "section_path": [str(s) for s in section],
        "pdf_pages": clean_str(rec.get("pdf_pages")),
        "printed_pages": clean_str(rec.get("printed_pages")),
        "source": clean_str(rec.get("source")),
        "url": clean_str(rec.get("url")),
        "extra": extra,
        "text": text,
        "char_count": rec.get("char_count") or len(text),
    }


def load_env(arg):
    """env/.env -> .env 순으로 찾는다. --env-file 로 직접 지정 가능."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("[주의] python-dotenv 미설치: 환경변수를 셸에서 직접 설정해야 합니다.")
        return
    root = Path(__file__).resolve().parent.parent
    candidates = [Path(arg)] if arg else [root / "env" / ".env", root / ".env"]
    for p in candidates:
        if p.is_file():
            load_dotenv(p, override=False)
            print(f".env 로드: {p}")
            return
    print("[주의] .env 파일을 찾지 못했습니다:", ", ".join(str(c) for c in candidates))


def get_supabase_key():
    """secret 키(sb_secret_...)를 우선 사용, 없으면 구 service_role 키로 임시 대체."""
    key = os.getenv("SUPABASE_SECRET_KEY")
    if not key:
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if key:
            print("[주의] SUPABASE_SERVICE_ROLE_KEY 는 곧 폐기됩니다. SUPABASE_SECRET_KEY(sb_secret_...)로 교체하세요.")
    if key and key.startswith("sb_publishable_"):
        sys.exit("SUPABASE_SECRET_KEY 에 publishable 키가 들어 있습니다. 적재에는 sb_secret_... 키가 필요합니다.")
    return key


def find_rag_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    root = Path(__file__).resolve().parent.parent
    for name in ("rag", "data"):
        if (root / name).is_dir():
            return root / name
    return root / "rag"


def find_chunk_file(folder_path: Path):
    """폴더 안의 청크 파일을 찾는다: chunks.jsonl 또는 *chunks.jsonl (예: common_chunks.jsonl)."""
    exact = folder_path / "chunks.jsonl"
    if exact.is_file():
        return exact, None
    found = sorted(folder_path.glob("*chunks.jsonl"))
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, f"청크 파일(*chunks.jsonl)이 없음: {folder_path}"
    return None, f"청크 파일이 여러 개라 고를 수 없음: {[f.name for f in found]}"


def load_corpus(rag_dir: Path, folder: str, corpus: str):
    path, err = find_chunk_file(rag_dir / folder)
    if err:
        return None, err
    rows, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            row = to_row(rec, corpus)
            if row["chunk_id"] in seen:
                raise ValueError(f"{path}:{n} chunk_id 중복: {row['chunk_id']}")
            seen.add(row["chunk_id"])
            rows.append(row)
    return (rows, path), None


def fetch_existing(sb, corpus: str) -> dict:
    """DB에 이미 있는 청크: chunk_id -> (text, embedding_model, 임베딩 존재 여부)."""
    existing, start, page = {}, 0, 1000
    while True:
        res = (
            sb.table("rag_chunks")
            .select("chunk_id,text,embedding_model")
            .eq("corpus", corpus)
            .not_.is_("embedding", "null")
            .range(start, start + page - 1)
            .execute()
        )
        for r in res.data:
            existing[r["chunk_id"]] = (r["text"], r["embedding_model"])
        if len(res.data) < page:
            return existing
        start += page


def embed_batch(client, texts: list[str]) -> list[list[float]]:
    res = client.embeddings.create(model=EMBED_MODEL, input=texts, dimensions=EMBED_DIM)
    vecs = [d.embedding for d in sorted(res.data, key=lambda d: d.index)]
    for v in vecs:
        if len(v) != EMBED_DIM:
            raise RuntimeError(f"임베딩 차원 불일치: {len(v)} != {EMBED_DIM}")
    return vecs


def main():
    ap = argparse.ArgumentParser(description="chunks.jsonl -> 임베딩 -> Supabase rag_chunks")
    ap.add_argument("--rag-dir", help="rag 폴더 경로 (기본: 프로젝트 루트의 rag/ 또는 data/)")
    ap.add_argument("--env-file", help=".env 경로 (기본: env/.env, 없으면 프로젝트 루트의 .env)")
    ap.add_argument("--corpus", choices=sorted(CORPORA.values()), action="append",
                    help="특정 코퍼스만 처리 (여러 번 지정 가능)")
    ap.add_argument("--batch-size", type=int, default=16, help="임베딩 요청당 청크 수 (기본 16)")
    ap.add_argument("--force", action="store_true", help="이미 임베딩된 청크도 다시 처리")
    ap.add_argument("--dry-run", action="store_true", help="API/DB 호출 없이 검증만")
    ap.add_argument("--limit", type=int, help="코퍼스별 처음 N개만 (테스트용)")
    args = ap.parse_args()

    rag_dir = find_rag_dir(args.rag_dir)
    print(f"rag 폴더: {rag_dir}")

    targets = [(f, c) for f, c in CORPORA.items() if not args.corpus or c in args.corpus]

    # 1) 파일 읽기/검증
    loaded = {}
    for folder, corpus in targets:
        res, err = load_corpus(rag_dir, folder, corpus)
        if err:
            print(f"  [건너뜀] {corpus}: {err}")
            continue
        rows, path = res
        if args.limit:
            rows = rows[: args.limit]
        loaded[corpus] = rows
        chars = sum(len(r["text"]) for r in rows)
        print(f"  [{corpus}] {path.name}: 청크 {len(rows)}개, 총 {chars:,}자")
    if not loaded:
        sys.exit("처리할 청크가 없습니다. --rag-dir 경로를 확인하세요.")

    if args.dry_run:
        corpus, rows = next(iter(loaded.items()))
        sample = {k: (v if k != "text" else v[:80] + "…") for k, v in rows[0].items()}
        print("\n[dry-run] 첫 행 매핑 예시:")
        print(json.dumps(sample, ensure_ascii=False, indent=2))
        print("\n[dry-run] API/DB 호출 없이 종료합니다.")
        return

    # 2) 클라이언트 준비
    load_env(args.env_file)
    sb_key = get_supabase_key()
    missing = [k for k in ("OPENAI_API_KEY", "SUPABASE_URL") if not os.getenv(k)]
    if not sb_key:
        missing.append("SUPABASE_SECRET_KEY")
    if missing:
        sys.exit(f"환경변수가 없습니다: {', '.join(missing)}  (.env 확인)")
    from openai import OpenAI
    from supabase import create_client

    oa = OpenAI(max_retries=5)  # 429/5xx 는 SDK가 지수 백오프로 재시도
    sb = create_client(os.environ["SUPABASE_URL"], sb_key)

    # 3) 코퍼스별 처리
    grand_embedded = grand_skipped = 0
    for corpus, rows in loaded.items():
        existing = {} if args.force else fetch_existing(sb, corpus)
        todo = [
            r for r in rows
            if args.force or existing.get(r["chunk_id"]) != (r["text"], EMBED_TAG)
        ]
        skipped = len(rows) - len(todo)
        print(f"\n[{corpus}] 임베딩 대상 {len(todo)}개 (건너뜀 {skipped}개)")

        for i in range(0, len(todo), args.batch_size):
            batch = todo[i : i + args.batch_size]
            t0 = time.time()
            vecs = embed_batch(oa, [r["text"] for r in batch])
            payload = [{**r, "embedding": v, "embedding_model": EMBED_TAG} for r, v in zip(batch, vecs)]
            sb.table("rag_chunks").upsert(payload, on_conflict="corpus,chunk_id").execute()
            print(f"  {min(i + args.batch_size, len(todo))}/{len(todo)} 완료 ({time.time() - t0:.1f}s)")

        grand_embedded += len(todo)
        grand_skipped += skipped

    # 4) 결과 확인
    print("\n=== 적재 결과 ===")
    for corpus in loaded:
        res = (
            sb.table("rag_chunks").select("pk", count="exact")
            .eq("corpus", corpus).not_.is_("embedding", "null").limit(1).execute()
        )
        print(f"  {corpus}: 임베딩된 청크 {res.count}개 (파일 {len(loaded[corpus])}개)")
    print(f"이번 실행: 임베딩 {grand_embedded}개, 건너뜀 {grand_skipped}개")


if __name__ == "__main__":
    main()
