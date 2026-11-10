import os
import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------- 설정 ----------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "env" / ".env")

API_KEY = os.getenv("YOUTUBE_API_KEY")
CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")

if not API_KEY or not CHANNEL_ID:
    raise RuntimeError("env/.env에 YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID를 설정하세요.")

youtube = build("youtube", "v3", developerKey=API_KEY)

RECENT_PATH = "list_UU.json"             # 1단계: 재생목록으로 받은 최신 20,000개
OLD_PATH = "older_videos.json"           # 2단계: 검색으로 받은 과거 영상 (진행 상황 포함)
LIST_PATH = "uploads_list.json"          # 병합 결과
DETAIL_PATH = "hanmuncheol_videos.csv"   # 3단계: 상세 정보

CHANNEL_START = datetime(2018, 9, 24, tzinfo=timezone.utc)  # 첫 영상(2018-09-25) 하루 앞
STEP_DAYS = 7


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_quota_error(e):
    return "quotaExceeded" in str(e)


# ---------- 1단계: 최신 영상 목록 (재생목록) ----------
def fetch_playlist(playlist_id):
    videos, token = [], None
    while True:
        res = youtube.playlistItems().list(
            part="snippet,contentDetails", playlistId=playlist_id,
            maxResults=50, pageToken=token,
        ).execute()
        for it in res["items"]:
            videos.append({
                "video_id": it["contentDetails"]["videoId"],
                "title": it["snippet"]["title"],
                "published_at": it["contentDetails"].get("videoPublishedAt"),
            })
        token = res.get("nextPageToken")
        if not token:
            return videos


def fetch_recent_list():
    playlist_id = "UU" + CHANNEL_ID[2:]
    videos = fetch_playlist(playlist_id)
    with open(RECENT_PATH, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=2)
    oldest = min(v["published_at"] for v in videos if v["published_at"])
    print(f"{RECENT_PATH} 저장: {len(videos)}개 (가장 오래된: {oldest})")
    return videos

def execute_with_retry(request, max_retries=6):
    """분당 제한(429)이면 대기 후 재시도, 일일 쿼터 소진이면 그대로 예외 발생"""
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            msg = str(e)
            if "quotaExceeded" in msg:      # 일일 쿼터 소진 -> 상위에서 저장 후 종료
                raise
            if e.resp.status in (429, 403) and "rateLimitExceeded" in msg:
                wait = 30 * (attempt + 1)
                print(f"분당 제한 도달. {wait}초 대기 후 재시도 ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("재시도 횟수를 초과했습니다. 잠시 후 다시 실행하세요.")

# ---------- 2단계: 과거 영상 검색 (오래된 쪽부터, 이어하기 지원) ----------
def search_window(start, end):
    items, token = {}, None
    for _ in range(10):
        request = youtube.search().list(
            part="snippet", channelId=CHANNEL_ID, type="video",
            order="date", publishedAfter=iso(start), publishedBefore=iso(end),
            maxResults=50, pageToken=token,
        )
        res = execute_with_retry(request)
        time.sleep(1.5)   # 호출 간격으로 분당 제한 완화
        for it in res["items"]:
            items[it["id"]["videoId"]] = {
                "video_id": it["id"]["videoId"],
                "title": it["snippet"]["title"],
                "published_at": it["snippet"]["publishedAt"],
            }
        token = res.get("nextPageToken")
        if not token:
            return items, False
    return items, True


def collect_window(start, end, store):
    items, truncated = search_window(start, end)
    if truncated and (end - start) > timedelta(hours=1):
        mid = start + (end - start) / 2
        collect_window(start, mid, store)
        collect_window(mid, end, store)
    else:
        store.update(items)


def fetch_older(channel_start, oldest_collected, step_days=7):
    store, cursor = {}, channel_start
    if os.path.exists(OLD_PATH):
        with open(OLD_PATH, encoding="utf-8") as f:
            state = json.load(f)
        store = state["videos"]
        cursor = parse_iso(state["cursor"])
        print(f"이어서 시작: {cursor:%Y-%m-%d}부터, 기존 {len(store)}개")

    def save():
        with open(OLD_PATH, "w", encoding="utf-8") as f:
            json.dump({"cursor": cursor.isoformat(), "videos": store}, f, ensure_ascii=False)

    try:
        while cursor < oldest_collected:
            nxt = min(cursor + timedelta(days=step_days), oldest_collected)
            collect_window(cursor, nxt, store)
            cursor = nxt
            save()
            print(f"{cursor:%Y-%m-%d}까지 수집: {len(store)}개")
    except HttpError as e:
        save()
        if is_quota_error(e):
            print("\n일일 쿼터 소진. 내일 같은 명령을 다시 실행하면 이어서 진행됩니다.")
            return False
        raise
    return True  # 끝까지 완료



# ---------- 병합 ----------
def merge_lists():
    merged = {}
    if os.path.exists(OLD_PATH):
        with open(OLD_PATH, encoding="utf-8") as f:
            merged.update(json.load(f)["videos"])
    with open(RECENT_PATH, encoding="utf-8") as f:
        for v in json.load(f):
            merged[v["video_id"]] = v
    videos = sorted(merged.values(), key=lambda v: v.get("published_at") or "")
    with open(LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(videos, f, ensure_ascii=False, indent=2)
    print(f"{LIST_PATH} 저장: 합계 {len(videos)}개 (채널 전체 36596개)")
    return videos


# ---------- 3단계: 상세 정보 ----------
def parse_duration(s):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


def fetch_video_details(video_ids):
    rows = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        res = youtube.videos().list(
            part="snippet,statistics,contentDetails", id=",".join(chunk)
        ).execute()
        for v in res["items"]:
            sn, st = v["snippet"], v.get("statistics", {})
            rows.append({
                "video_id": v["id"],
                "title": sn["title"],
                "published_at": sn["publishedAt"],
                # "duration_sec": parse_duration(v["contentDetails"]["duration"]),
                # "views": st.get("viewCount", 0),
                # "likes": st.get("likeCount", 0),
                # "comments": st.get("commentCount", 0),
                "tags": "|".join(sn.get("tags", [])),
                "description": sn.get("description", ""),
                "url": f"https://www.youtube.com/watch?v={v['id']}",
            })
        print(f"상세 수집: {min(i + 50, len(video_ids))}/{len(video_ids)}")
    missing = len(video_ids) - len(rows)
    if missing:
        print(f"참고: {missing}개는 비공개/삭제 등으로 가져오지 못했습니다.")
    return rows


def save_csv(rows, path):
    if not rows:
        print("저장할 데이터가 없습니다.")
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"{path} 저장 완료 ({len(rows)}개)")


# ---------- 실행 ----------
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "list":                      # 최신 20,000개 목록 수집
        fetch_recent_list()
        merge_lists()

    elif mode == "older":                   # 과거 영상 검색 (매일 반복 실행)
        if not os.path.exists(RECENT_PATH):
            fetch_recent_list()
        with open(RECENT_PATH, encoding="utf-8") as f:
            recent = json.load(f)
        oldest = min(parse_iso(v["published_at"]) for v in recent if v.get("published_at"))
        oldest_collected = (oldest + timedelta(days=1)).replace(hour=0, minute=0, second=0)

        done = fetch_older(CHANNEL_START, oldest_collected, step_days=STEP_DAYS)
        merge_lists()                       # 중간 상태라도 합쳐서 저장
        if done:
            print("과거 영상 수집 완료! 이제 `python channel_videos.py`로 상세 조회를 하세요.")

    else:                                   # 상세 조회
        if not os.path.exists(LIST_PATH):
            raise SystemExit("목록이 없습니다. 먼저 `python channel_videos.py older`를 실행하세요.")
        with open(LIST_PATH, encoding="utf-8") as f:
            videos = json.load(f)
        print(f"{LIST_PATH}에서 {len(videos)}개 불러옴")
        rows = fetch_video_details([v["video_id"] for v in videos])
        save_csv(rows, DETAIL_PATH)