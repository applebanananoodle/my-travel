#!/usr/bin/env python3
"""
나의 여행 선반 — 어드민 서버 (내 컴퓨터에서만 돌아간다, 파이썬 표준 라이브러리만 쓴다)

이 폴더는 두 쪽으로 나뉜다.
  어드민  admin/     내 컴퓨터에서만 여는 관리 화면. 여행을 기록하고 사진을 올리고 고친다
  사이트  docs/      GitHub Pages 가 웹에 올리는 쪽. shelf.json 하나만 읽어서 선반을 그린다

데이터가 흐르는 길
  어드민에서 기록·수정  →  data/trips.json (내 기록 원본)
  어드민에서 사진 올리기 →  docs/photos/ (사이트가 그대로 보여 준다. 공개된다)
  어드민의 [게시] 버튼  →  docs/shelf.json (사이트가 읽는 파일)
  git push            →  웹에 반영

Google 지도 키는 .env 에 둔다 (GOOGLE_MAPS_KEY). .env 자체는 .gitignore 로 GitHub 에 올라가지 않지만,
지도를 그리려면 [게시]할 때 키가 docs/shelf.json 에 들어가 웹에 공개된다. 지도 키는 원래 그렇게 쓰는 것이라
Google Cloud 콘솔에서 '내 사이트 주소에서만', 'Maps Embed API 만' 쓰이도록 꼭 제한해 둔다. README 참고.

[게시]를 따로 둔 이유. 어드민에서 고치는 중인 것이 바로 웹에 나가지 않게 하려고.
고치고, 로컬 미리보기로 확인하고, 게시하고, push 하는 네 걸음이다.

실행
    python3 admin/server.py          # http://127.0.0.1:8798 어드민, /site/ 는 사이트 미리보기
    python3 admin/server.py 9000     # 포트 지정
"""
import base64
import json
import os
import re
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 폴더 맨 바깥
ADMIN = ROOT / "admin"
DOCS = ROOT / "docs"                               # GitHub Pages 가 여는 폴더
PHOTOS = DOCS / "photos"                           # 올린 사진. 사이트에서 공개된다
TRIPS_PATH = ROOT / "data" / "trips.json"          # 내 기록 원본
SHELF_PATH = DOCS / "shelf.json"                   # 사이트가 읽는 파일
ENV_PATH = ROOT / ".env"                           # Google 지도 키. GitHub 에 올라가지 않는다
DEFAULT_PORT = 8798                                 # 책장 어드민(8799)과 동시에 켤 수 있게
MAX_PHOTO = 8 * 1024 * 1024                        # 사진 한 장 최대 8MB (어드민이 미리 줄여서 보낸다)


# ───────────────────────────── ① 설정과 저장 ─────────────────────────────
def read_env():
    """ .env 의 KEY=값 줄을 읽는다. 환경변수에 같은 이름이 있으면 그쪽이 먼저다. """
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    if os.environ.get("GOOGLE_MAPS_KEY"):
        env["GOOGLE_MAPS_KEY"] = os.environ["GOOGLE_MAPS_KEY"]
    return env


def maps_key():
    key = read_env().get("GOOGLE_MAPS_KEY", "").strip()
    return "" if not key or key.startswith("여기에") else key


def load_db():
    if not TRIPS_PATH.exists():
        return {"shelf": {"title": "나의 여행 선반", "owner": ""}, "trips": []}
    return json.loads(TRIPS_PATH.read_text(encoding="utf-8"))


def save_db(db):
    # 임시 파일에 먼저 쓰고 바꿔치기한다. 쓰다가 꺼져도 원본이 반쯤 지워지지 않게.
    TRIPS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRIPS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TRIPS_PATH)


# ───────────────────────────── ② 사진 ─────────────────────────────
# 어드민이 브라우저에서 사진을 1200px JPG 로 줄여 보낸다. 줄이는 동안 위치 정보(GPS)도 빠진다.
# 여기서는 진짜 그림 파일인지만 확인하고 docs/photos/ 에 새 이름으로 저장한다.
PHOTO_RE = re.compile(r"photos/[A-Za-z0-9._-]+")
MAGIC = {".jpg": [b"\xff\xd8\xff"], ".png": [b"\x89PNG"], ".webp": [b"RIFF"]}


def save_photo(data_url):
    m = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=\s]+)", data_url or "")
    if not m:
        raise ValueError("사진은 JPG, PNG, WEBP 만 올릴 수 있어요")
    raw = base64.b64decode(m.group(2))
    if len(raw) > MAX_PHOTO:
        raise ValueError("사진이 너무 커요 (8MB 까지)")
    ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}[m.group(1)]
    if not any(raw.startswith(sig) for sig in MAGIC[ext]):
        raise ValueError("그림 파일이 아닌 것 같아요")
    PHOTOS.mkdir(parents=True, exist_ok=True)
    name = f"trip-{date.today():%Y%m%d}-{secrets.token_hex(3)}{ext}"
    (PHOTOS / name).write_bytes(raw)
    return "photos/" + name


def delete_photo(path, db):
    """ 다른 기록이 쓰지 않는 사진이면 파일을 지운다. photos/ 밖의 파일은 절대 건드리지 않는다. """
    if not path or not PHOTO_RE.fullmatch(path):
        return
    if any(t.get("photo") == path for t in db["trips"]):
        return
    target = DOCS / path
    if target.is_file():
        target.unlink()


# ───────────────────────────── ③ 기록 다루기 ─────────────────────────────
# 여행 한 건에 들어가는 칸. 어드민 입력 화면과 사이트가 이 이름을 같이 쓴다.
#   photo 사진 경로, place 장소, country 나라, city 도시, address 지도에 표시할 주소,
#   date 다녀온 날, memo 메모, fav 다시 가고 싶은 곳
FIELDS = ["photo", "place", "country", "city", "address", "date", "memo", "fav"]

# 이 가운데 사이트(docs/shelf.json)로 내보내는 칸. 게시와 '게시하지 않은 변경' 확인이 같이 쓴다
PUBLIC_FIELDS = ["id", "photo", "place", "country", "city", "address", "date", "memo", "fav"]

# 다녀온 날은 2025-03-14, 2025-03, 2025 가운데 아는 만큼. 모르면 비워 둔다
DATE_RE = re.compile(r"\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?")


def clean_trip(data, old=None):
    trip = dict(old or {})
    for k in FIELDS:
        if k in data:
            trip[k] = data[k]
    for k in ("place", "country", "city", "address", "date", "memo", "photo"):
        trip[k] = (trip.get(k) or "").strip()
    if not trip["place"]:
        raise ValueError("장소가 비어 있어요")
    trip["date"] = trip["date"].replace(".", "-").replace("/", "-")
    if trip["date"] and not DATE_RE.fullmatch(trip["date"]):
        raise ValueError("다녀온 날은 2025-03-14, 2025-03, 2025 처럼 적어 주세요")
    if trip["photo"] and not PHOTO_RE.fullmatch(trip["photo"]):
        raise ValueError("사진 경로가 이상해요")
    trip["fav"] = bool(trip.get("fav"))
    return trip


def new_id(db):
    return max([t.get("id", 0) for t in db["trips"]] + [0]) + 1


def sort_key(t):
    # 최근에 다녀온 곳이 먼저. 날짜를 모르는 곳은 맨 뒤에, 기록한 순서대로
    return (1, t["date"], -t["id"]) if t.get("date") else (0, "", -t["id"])


def publish(db):
    """ 사이트가 읽는 docs/shelf.json 을 새로 쓴다. 사이트에 필요한 칸만 내보낸다. """
    trips = sorted(db["trips"], key=sort_key, reverse=True)
    out = {
        "shelf": db.get("shelf", {}),
        "updated": date.today().isoformat(),
        "trips": [{k: t.get(k) for k in PUBLIC_FIELDS} for t in trips],
    }
    if maps_key():
        out["maps"] = {"key": maps_key()}   # 사이트가 지도를 그릴 때 쓴다. 웹에 공개되므로 Google 에서 사용처를 제한해 둘 것
    DOCS.mkdir(exist_ok=True)
    SHELF_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return len(trips)


def published_differs(db):
    """ 어드민에서 고친 뒤 아직 게시하지 않은 게 있는지. 어드민 위쪽 알림에 쓴다. """
    if not SHELF_PATH.exists():
        return True
    try:
        pub = json.loads(SHELF_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return True
    ids = lambda items: sorted((t.get("id"), json.dumps(t, sort_keys=True, ensure_ascii=False)) for t in items)
    mine = [{k: t.get(k) for k in PUBLIC_FIELDS} for t in db["trips"]]
    maps = {"key": maps_key()} if maps_key() else None
    return ids(mine) != ids(pub.get("trips", [])) or pub.get("shelf") != db.get("shelf") or pub.get("maps") != maps


# ───────────────────────────── ④ 서버 ─────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 터미널을 조용하게
        pass

    def send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_PHOTO * 2:
            raise ValueError("보낸 내용이 너무 커요")
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def send_file(self, path):
        if not path.is_file():
            self.send_error(404)
            return
        types = {".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp"}
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/":
                return self.send_file(ADMIN / "admin.html")
            if u.path == "/site":
                self.send_response(301)
                self.send_header("Location", "/site/")
                self.end_headers()
                return
            if u.path.startswith("/site/"):
                # 사이트 미리보기. docs/ 를 그대로 보여 준다. 웹에 올라가면 이 화면이 그대로 열린다
                rel = urllib.parse.unquote(u.path[len("/site/"):]) or "index.html"
                target = (DOCS / rel).resolve()
                if DOCS.resolve() not in target.parents and target != DOCS.resolve():
                    return self.send_error(403)
                return self.send_file(target)
            if u.path == "/api/trips":
                db = load_db()
                return self.send_json({**db, "unpublished": published_differs(db), "hasMapsKey": bool(maps_key())})
            self.send_error(404)
        except Exception as e:  # 화면에 원인을 그대로 보여 준다. 막히면 그 문장을 Claude 에게 보여 주면 된다
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/photo":
                return self.send_json({"photo": save_photo(self.read_body().get("data"))})
            db = load_db()
            if u.path == "/api/trips":
                trip = clean_trip(self.read_body())
                trip["id"] = new_id(db)
                trip["addedAt"] = date.today().isoformat()
                db["trips"].append(trip)
                save_db(db)
                return self.send_json(trip)
            if u.path == "/api/shelf":
                data = self.read_body()
                db["shelf"] = {"title": (data.get("title") or "나의 여행 선반").strip(), "owner": (data.get("owner") or "").strip()}
                save_db(db)
                return self.send_json(db["shelf"])
            if u.path == "/api/publish":
                return self.send_json({"count": publish(db)})
            if u.path == "/api/clear-samples":
                gone = [t for t in db["trips"] if t.get("sample")]
                db["trips"] = [t for t in db["trips"] if not t.get("sample")]
                save_db(db)
                for t in gone:
                    delete_photo(t.get("photo"), db)
                return self.send_json({"count": len(db["trips"])})
            self.send_error(404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_PUT(self):
        m = re.fullmatch(r"/api/trips/(\d+)", urllib.parse.urlparse(self.path).path)
        if not m:
            return self.send_error(404)
        try:
            db = load_db()
            i = next((i for i, t in enumerate(db["trips"]) if t["id"] == int(m.group(1))), None)
            if i is None:
                return self.send_json({"error": "없는 기록이에요"}, 404)
            old_photo = db["trips"][i].get("photo")
            db["trips"][i] = clean_trip(self.read_body(), db["trips"][i])
            db["trips"][i].pop("sample", None)  # 손댄 샘플은 내 기록으로 본다
            save_db(db)
            if old_photo != db["trips"][i]["photo"]:
                delete_photo(old_photo, db)     # 사진을 바꾸거나 뺐으면 예전 사진 파일은 지운다
            self.send_json(db["trips"][i])
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/trips/(\d+)", urllib.parse.urlparse(self.path).path)
        if not m:
            return self.send_error(404)
        db = load_db()
        gone = [t for t in db["trips"] if t["id"] == int(m.group(1))]
        db["trips"] = [t for t in db["trips"] if t["id"] != int(m.group(1))]
        save_db(db)
        for t in gone:
            delete_photo(t.get("photo"), db)
        self.send_json({"ok": True})


def main():
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(nums[0]) if nums else DEFAULT_PORT
    # 127.0.0.1 에만 연다. 같은 와이파이의 다른 사람이 내 어드민에 들어올 수 없게
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  나의 여행 선반 어드민   {url}\n  사이트 미리보기         {url}site/\n\n  끄려면 이 창에서 Ctrl+C\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  어드민을 껐어요")


if __name__ == "__main__":
    main()
