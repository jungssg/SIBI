import os
import csv
import json
import re
import time
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

def find_project_root():
    """이 파일이 어디에 있든(프로젝트 루트, scripts/collect/ 등) raw/ 폴더가 있는 프로젝트 루트를 찾는다."""
    here = Path(__file__).resolve().parent
    for p in (here, *here.parents):
        if (p / "raw").is_dir():
            return p
    return here


BASE_DIR = find_project_root()
DATA_DIR = BASE_DIR / "raw" / "hanmoonchul"

load_dotenv(BASE_DIR / "env" / ".env")


API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise RuntimeError("env/.env에 YOUTUBE_API_KEY를 설정하세요.")

youtube = build("youtube", "v3", developerKey=API_KEY)

# 모든 입출력은 raw/hanmoonchul/ 아래
LIST_PATH = DATA_DIR / "uploads_list.json"                  # video_id 목록 (channel_videos.py 병합 결과)
LEGACY_LIST_PATH = DATA_DIR / "uploads_list_15303-28817.json"  # 병합 전 최신 20,000개 목록 (병합 파일이 없을 때만 대체 사용)
DETAIL_JSONL = DATA_DIR / "video_details.jsonl"             # 진행 상황 저장 (한 줄에 영상 1개)
MISSING_PATH = DATA_DIR / "video_details_missing.txt"       # 비공개/삭제 등으로 받지 못한 video_id (다시 요청하지 않음)
DETAIL_CSV = DATA_DIR / "hanmoonchul_videos_detail.csv"     # 최종 CSV


def parse_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def to_row(v):
    sn, st, cd = v["snippet"], v.get("statistics", {}), v["contentDetails"]
    return {
        "video_id": v["id"],
        "title": sn["title"],
        "published_at": sn["publishedAt"],
        # "duration_sec": parse_duration(cd["duration"]),
        # "views": st.get("viewCount", ""),
        # "likes": st.get("likeCount", ""),          # 좋아요 비공개면 빈 값
        # "comments": st.get("commentCount", ""),    # 댓글 중지면 빈 값
        "tags": "|".join(sn.get("tags", [])),
        # "category_id": sn.get("categoryId", ""),
        # "live_broadcast": sn.get("liveBroadcastContent", ""),
        # "definition": cd.get("definition", ""),
        "caption": cd.get("caption", ""),
        "description": sn.get("description", ""),
        "url": f"https://www.youtube.com/watch?v={v['id']}",
    }


def load_done_ids():
    done = set()
    if os.path.exists(DETAIL_JSONL):
        with open(DETAIL_JSONL, encoding="utf-8") as f:
            for line in f:
                done.add(json.loads(line)["video_id"])
    return done


def load_missing_ids():
    if not os.path.exists(MISSING_PATH):
        return set()
    with open(MISSING_PATH, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def resolve_list_path():
    if os.path.exists(LIST_PATH):
        return LIST_PATH
    if os.path.exists(LEGACY_LIST_PATH):
        print(f"[주의] {LIST_PATH.name} 이 없어 최신분 목록({LEGACY_LIST_PATH.name})만 사용합니다.")
        print("       과거 영상까지 받으려면 먼저 `python channel_videos.py older` 로 목록을 병합하세요.")
        return LEGACY_LIST_PATH
    raise SystemExit(f"목록이 없습니다: {LIST_PATH}\n먼저 `python channel_videos.py list` 또는 `older` 를 실행하세요.")


def fetch_details():
    list_path = resolve_list_path()
    with open(list_path, encoding="utf-8") as f:
        all_ids = [v["video_id"] for v in json.load(f)]

    done = load_done_ids()
    missing = load_missing_ids()
    todo = [i for i in all_ids if i not in done and i not in missing]
    print(f"목록 {list_path.name}: 전체 {len(all_ids)}개 / 이미 완료 {len(done)}개 / "
          f"받을 수 없음(기록) {len(missing & set(all_ids))}개 / 남은 {len(todo)}개")

    with open(DETAIL_JSONL, "a", encoding="utf-8") as out:
        for i in range(0, len(todo), 50):
            chunk = todo[i:i + 50]
            try:
                res = youtube.videos().list(
                    part="snippet,statistics,contentDetails",
                    id=",".join(chunk),
                ).execute()
            except HttpError as e:
                print(f"중단: {e.resp.status} {str(e)[:150]}")
                print("같은 명령을 다시 실행하면 이어서 진행됩니다.")
                break
            got = set()
            for v in res["items"]:
                out.write(json.dumps(to_row(v), ensure_ascii=False) + "\n")
                got.add(v["id"])
            out.flush()
            lost = [vid for vid in chunk if vid not in got]  # 비공개/삭제 등: API가 돌려주지 않음
            if lost:
                with open(MISSING_PATH, "a", encoding="utf-8") as mf:
                    mf.write("\n".join(lost) + "\n")
            print(f"상세 수집: {min(i + 50, len(todo))}/{len(todo)}")
            time.sleep(0.3)


def export_csv():
    rows = []
    with open(DETAIL_JSONL, encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    if not rows:
        print("저장할 데이터가 없습니다.")
        return
    with open(DETAIL_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"{DETAIL_CSV} 저장 완료 ({len(rows)}개)")


if __name__ == "__main__":
    fetch_details()
    export_csv()