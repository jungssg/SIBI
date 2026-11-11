import os
import csv
import json
import re
import time
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "raw" / "hanmoonchul"

load_dotenv(BASE_DIR / "env" / ".env")


API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    raise RuntimeError("env/.env에 YOUTUBE_API_KEY를 설정하세요.")

youtube = build("youtube", "v3", developerKey=API_KEY)

LIST_PATH =  DATA_DIR /"uploads_list_15303-28817.json"             # video_id 목록 (channel_videos.py 결과)
DETAIL_JSONL =  DATA_DIR /"video_details.jsonl"        # 진행 상황 저장 (한 줄에 영상 1개)
DETAIL_CSV =  DATA_DIR /"hanmoonchul_videos.csv"       # 최종 CSV


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


def fetch_details():
    with open(LIST_PATH, encoding="utf-8") as f:
        all_ids = [v["video_id"] for v in json.load(f)]

    done = load_done_ids()
    todo = [i for i in all_ids if i not in done]
    print(f"전체 {len(all_ids)}개 / 이미 완료 {len(done)}개 / 남은 {len(todo)}개")

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
            for v in res["items"]:
                out.write(json.dumps(to_row(v), ensure_ascii=False) + "\n")
            out.flush()
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