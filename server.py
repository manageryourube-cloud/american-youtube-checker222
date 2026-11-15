#!/usr/bin/env python3
"""Чекер монетизации YouTube. Запуск: python server.py  ->  http://localhost:8000
Только стандартная библиотека Python 3.8+."""
import json, os, re, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "CONSENT=YES+1; SOCS=CAI",
}
VIDEOS_TO_CHECK = 6


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def extract_json(html, marker):
    i = html.find(marker)
    if i < 0:
        return None
    i = html.find("{", i)
    try:
        return json.JSONDecoder().raw_decode(html[i:])[0]
    except ValueError:
        return None


def channel_url(q):
    q = q.strip()
    if re.fullmatch(r"UC[\w-]{22}", q):
        return f"https://www.youtube.com/channel/{q}/videos"
    if q.startswith("@"):
        return f"https://www.youtube.com/{q}/videos"
    if "youtube.com" in q:
        if not q.startswith("http"):
            q = "https://" + q
        m = re.match(r"^/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)",
                     urllib.parse.urlparse(q).path)
        if m:
            return f"https://www.youtube.com/{m.group(1)}/videos"
        raise ValueError("Укажите ссылку именно на канал (youtube.com/@name), а не на видео.")
    if re.fullmatch(r"[\w.\-]{3,}", q):
        return f"https://www.youtube.com/@{q}/videos"
    raise ValueError("Не удалось распознать канал. Введите @handle, ссылку или ID (UC...).")


def to_int(s):
    m = re.match(r"([\d.,]+)\s*([KMBkmb]?)", s or "")
    if not m:
        return None
    n = float(m.group(1).replace(",", "")) if m.group(2) or "." not in m.group(1) \
        else float(m.group(1))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


def collect_video_ids(data, html):
    """Собирает ID длинных видео из ytInitialData (старая и новая разметка)."""
    ids = []

    def add(v):
        if isinstance(v, str) and re.fullmatch(r"[\w-]{11}", v) and v not in ids:
            ids.append(v)

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("videoRenderer", "gridVideoRenderer", "compactVideoRenderer"):
                    if isinstance(v, dict):
                        add(v.get("videoId"))
                elif k == "lockupViewModel":
                    if isinstance(v, dict) and "VIDEO" in str(v.get("contentType", "")):
                        add(v.get("contentId"))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    if data:
        walk(data)
    if not ids:  # запасной вариант: грубый поиск по HTML
        for v in re.findall(r'"(?:videoId|contentId)":"([\w-]{11})"', html):
            add(v)
    return ids


def check_video(vid):
    """Показывает ли YouTube рекламные слоты на видео."""
    try:
        _, html = get(f"https://www.youtube.com/watch?v={vid}")
    except Exception:
        return {"id": vid, "ok": False, "why": "страница видео не загрузилась"}
    pr = extract_json(html, "ytInitialPlayerResponse")
    if not pr:
        return {"id": vid, "ok": False, "why": "нет данных плеера"}
    ps = pr.get("playabilityStatus", {})
    if ps.get("status") != "OK":
        return {"id": vid, "ok": False,
                "why": f'{ps.get("status", "?")}: {(ps.get("reason") or "")[:70]}'}
    vd = pr.get("videoDetails", {})
    if vd.get("isLive") or vd.get("isUpcoming"):
        return {"id": vid, "ok": False, "why": "идёт эфир/премьера"}
    try:
        length = int(vd.get("lengthSeconds", 0))
    except ValueError:
        length = 0
    if 0 < length <= 60:
        return {"id": vid, "ok": False, "why": "Shorts (пропущено)"}
    ads = bool(pr.get("adPlacements")) or bool(pr.get("playerAds"))
    return {"id": vid, "ok": True, "ads": ads, "title": vd.get("title", "")[:70]}


def check_channel(q):
    url = channel_url(q)
    final, html = get(url)
    if "consent." in final or "ytInitialData" not in html:
        raise RuntimeError("YouTube не отдал страницу канала (капча/согласие/блок IP). "
                           "Попробуйте позже или запустите чекер с другого IP.")
    if "This channel does not exist" in html or "404 Not Found" in html[:3000]:
        raise ValueError("Канал не найден.")

    data = extract_json(html, "ytInitialData")
    title = (re.search(r'<meta property="og:title" content="([^"]*)"', html) or [None, ""])[1]
    cid = ((re.search(r'"externalId":"(UC[\w-]{22})"', html)
            or re.search(r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', html)
            or re.search(r'"channelId":"(UC[\w-]{22})"', html) or [None, ""])[1])
    subs_txt = (re.search(r'"(\d[\d.,]*\s?[KMBkmb]?)\s+subscribers"', html) or [None, None])[1]
    subs = to_int(subs_txt)
    vids_txt = (re.search(r'"([\d,]+)\s+videos"', html) or [None, None])[1]
    members = bool(re.search(r'"sponsorButton"|"membershipButton"|"(?:text|title|content)":"Join"', html))

    ids = collect_video_ids(data, html)
    if len(ids) < 3 and cid:  # запасной источник: RSS-лента канала
        try:
            _, xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
            for v in re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>", xml):
                if v not in ids:
                    ids.append(v)
        except Exception:
            pass
    ids = ids[:VIDEOS_TO_CHECK]
    with ThreadPoolExecutor(max_workers=5) as ex:
        all_res = list(ex.map(check_video, ids))
    res = [r for r in all_res if r["ok"]]
    checked = len(res)
    with_ads = sum(1 for r in res if r["ads"])

    signals = []
    if members:
        signals.append("Есть кнопка «Присоединиться» (платные подписки доступны только в YPP)")
    signals.append(f"Реклама найдена на {with_ads} из {checked} проверенных видео")
    if subs is not None:
        signals.append(f"Подписчиков: {subs_txt}" + (" (ниже порога YPP 500)" if subs < 500 else ""))
    if checked == 0:
        reasons = sorted({r["why"] for r in all_res if not r["ok"]})
        signals.append(f"Найдено видео: {len(ids)}. Причины, почему не проверены: "
                       + ("; ".join(reasons) if reasons else "список видео пуст"))
        if any("LOGIN_REQUIRED" in r for r in reasons):
            signals.append("YouTube требует подтвердить, что вы не бот: сервер с этого IP заблокирован. "
                           "Запустите чекер с домашнего IP или другого хостинга.")

    if members or (checked >= 3 and with_ads / checked >= 0.5):
        status, text = "yes", "Монетизация ВКЛЮЧЕНА"
        conf = "высокая" if (members or checked >= 4) else "средняя"
    elif with_ads > 0:
        status, text, conf = "maybe", "Вероятно, монетизация есть", "средняя"
        signals.append("Реклама показывается лишь на части видео: возможно, часть роликов "
                       "ограничена или это реклама по претензии Content ID")
    elif checked == 0:
        status, text, conf = "unknown", "Не удалось определить", "нет данных"
    else:
        status, text, conf = "no", "Монетизация НЕ обнаружена", "высокая" if checked >= 3 else "средняя"

    return {"title": title, "channelId": cid, "subs": subs_txt, "videos": vids_txt,
            "status": status, "verdict": text, "confidence": conf,
            "members": members, "checked": checked, "withAds": with_ads,
            "signals": signals, "samples": res}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/check":
            q = urllib.parse.parse_qs(p.query).get("q", [""])[0]
            try:
                out, code = check_channel(q), 200
            except ValueError as e:
                out, code = {"error": str(e)}, 400
            except Exception as e:
                out, code = {"error": str(e) or "Ошибка запроса к YouTube"}, 502
            self._send(code, json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        elif p.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Чекер запущен: http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
