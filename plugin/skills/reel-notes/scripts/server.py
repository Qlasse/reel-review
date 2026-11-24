#!/usr/bin/env python3
"""Страница пометок на кадре: http://127.0.0.1:8765

    python3 <scripts>/server.py --root <папка проекта> [--port 8765]

Только локально (127.0.0.1). Отдаёт мастера из <бренд>/out/, картинки пометок и читает/пишет
<бренд>/edit/<id>/notes.json через notes.py. Видео — с Range, чтобы перемотка работала покадрово.
Claude, предпросмотр и настройки — jobs.py; окно правил и настроек — /rules (rules.html); как устроен проект — config.py.
Без работы (страница закрыта, Claude и рендер не идут) сервер выключается сам: settings.idle_minutes, по умолчанию 30.
"""
import argparse, json, os, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ARGS = None
if __name__ == "__main__":  # корень проекта — до импорта модулей: config читает его при импорте
    _ap = argparse.ArgumentParser(description="Страница пометок на кадре")
    _ap.add_argument("--root", help="папка проекта (по умолчанию текущая)")
    _ap.add_argument("--port", type=int, help="порт (по умолчанию из .reel-review.json или 8765)")
    ARGS = _ap.parse_args()
    if ARGS.root:
        os.environ["REEL_REVIEW_ROOT"] = str(Path(ARGS.root).resolve())

sys.path.insert(0, str(Path(__file__).parent))
import config as C
import jobs
import notes as N
import tracker
import tracks
import variants

from config import ROOT
PAGE = Path(__file__).parent / "index.html"
RULES_PAGE = Path(__file__).parent / "rules.html"
MEDIA = {".mp4": "video/mp4", ".mov": "video/quicktime", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg"}
RID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


brands = N.brands
rule_files = N.rule_files
owner = N.owner
LAST = {"t": time.time()}  # последний запрос страницы — для выключения без работы


def relp(p):
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def comps(brand):
    root = N.remotion(brand) / "src" / "Root.tsx"
    return set(re.findall(r'id="([A-Za-z0-9]+)"', root.read_text(encoding="utf-8"))) if root.is_file() else set()


def reels():
    out = []
    t = C.get("tracker") and ROOT / C.get("tracker")
    table = tracker.rows(t.read_text(encoding="utf-8")) if t and t.is_file() else {}
    for name, b in brands().items():
        have = comps(name)
        for p in sorted((b / "out").glob("*.mp4")):
            rid = owner(b, p.name)
            ns = N.read(name, rid)["notes"] if RID.match(rid) else []
            row = table.get(tracker.key(rid), {})
            out.append({"brand": name, "id": rid, "file": p.name, "path": str(p.relative_to(ROOT)),
                        "key": tracker.key(rid), "status": row.get("Статус", ""), "version": row.get("Версия", ""),
                        "published": {k: row.get(k, "") for k in ("Опубликован", "Ссылка", "Результат")},
                        "mtime": p.stat().st_mtime, "notes": len(ns), "preview": N.comp_id(rid) in have,
                        "waiting": sum(n.get("status") in N.WAITING for n in ns),
                        "answered": sum(n.get("status") in ("done", "question", "wontfix") for n in ns)})
    return sorted(out, key=lambda r: -r["mtime"])


def probe(path):
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                                  capture_output=True, text=True).stdout or "{}")
    s = (j.get("streams") or [{}])[0]
    num, den = (s.get("r_frame_rate") or "30/1").split("/")
    return {"w": s.get("width", 1080), "h": s.get("height", 1920), "fps": float(num) / float(den or 1),
            "dur": float(j.get("format", {}).get("duration", 0))}


def brand_colors():
    out = {}
    for name, b in brands().items():
        for f in b.glob("brands/*/brand.json"):
            c = json.loads(f.read_text(encoding="utf-8")).get("colors", {})
            vals = list(c.values()) + list((c.get("extra") or {}).values())
            out[name] = list(dict.fromkeys(v.upper() for v in vals if isinstance(v, str) and v.startswith("#")))
    return out


def rule_read(rel):
    if rel not in rule_files():
        raise ValueError("этот файл правилами не считается")
    p = ROOT / rel
    return {"path": rel, "text": p.read_text(encoding="utf-8") if p.is_file() else "", "mtime": p.stat().st_mtime if p.is_file() else 0}


def rule_write(rel, text, mtime):
    """Записать файл правил, если его не меняли с момента чтения (иначе 409 и свежий текст)."""
    cur = rule_read(rel)
    if abs(cur["mtime"] - float(mtime or 0)) > 1e-3:
        return 409, {"error": "Файл изменился, пока вы его правили (Claude или другое окно). Загружен свежий текст: "
                              "перенесите свои правки в него.", **cur}
    p = ROOT / rel
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    tmp.replace(p)
    return 200, rule_read(rel)


def size(p):
    return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def reel_delete(brand, rid, scope, file="", dry=True):
    """В Корзину: один мастер (scope=file) или весь ролик (scope=reel). dry — только список."""
    j = jobs.current()
    if j and j["id"] == rid:
        raise jobs.Busy("С роликом сейчас работает " + j["title"])
    b = brands()[brand]
    if scope == "file":
        p = (b / "out" / file).resolve()
        if p.parent != (b / "out").resolve() or not p.is_file():
            raise ValueError("нет такого файла в out/")
        items, keep = [p], []
    elif scope == "reel":
        items = [f for d in (b / "out", b / "out" / "renders") if d.is_dir() for f in sorted(d.iterdir())
                 if f.is_file() and owner(b, f.name) == rid]
        rem = N.remotion(brand)
        items += [d for d in (b / "edit" / rid, rem / "public" / rid) if d.is_dir()]
        keep = [relp(f) for f in (rem / "src").glob(N.comp_id(rid) + "*.tsx")]
    else:
        raise ValueError("scope: file или reel")
    res = {"items": [{"path": relp(p), "size": size(p)} for p in items], "keep": keep,
           "row": scope == "reel" and bool(C.get("tracker")) and tracker.key(rid) == rid}
    if dry:
        return res
    for p in items:
        subprocess.run(["/usr/bin/trash", str(p)], check=True, capture_output=True)
    if res["row"]:
        tracker.drop(rid)
    return res


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def reel_args(self, brand, rid):
        if brand not in brands() or not RID.match(rid or ""):
            raise ValueError("нет такого бренда или id")
        return brand, rid

    def media(self, rel):
        p = (ROOT / unquote(rel)).resolve()
        if not p.is_relative_to(ROOT) or p.suffix.lower() not in MEDIA or not p.is_file():
            return self.send(404, {"error": "нет файла"})
        size = p.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            start = int(m[1]) if m[1] else max(0, size - int(m[2]))
            end = min(int(m[2]), size - 1) if m[1] and m[2] else size - 1
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", MEDIA[p.suffix.lower()])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, post):
        """Запрос только со своей страницы. 127.0.0.1 не спасает от чужих сайтов в браузере: форма с text/plain уходит
        без спроса (запустить Claude, удалить ролик), а подмена DNS даёт чужому сайту читать ответы. Поэтому:
        Host — наш адрес, Origin (если есть) — наш, у POST — application/json (с ним браузер сначала спрашивает
        разрешение, а сервер его чужим не даёт)."""
        port = self.server.server_port
        ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return (self.headers.get("Host", "") in ours and (not origin or origin.removeprefix("http://") in ours)
                and (not post or self.headers.get("Content-Type", "").split(";")[0].strip() == "application/json"))

    def do_GET(self):
        if not self.allowed(False):
            return self.send(403, {"error": "запрос не со страницы пометок"})
        LAST["t"] = time.time()
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/features":
                return self.send(200, {**C.features(), "root": str(ROOT), "idle_minutes": jobs.settings()["idle_minutes"]})
            if u.path == "/rules":
                return self.send(200, RULES_PAGE.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/claude":
                b, rid = self.reel_args(q.get("brand"), q.get("id"))
                return self.send(200, {"job": jobs.current(), "session": N.read(b, rid).get("session"),
                                       **jobs.feed(b, rid, int(q.get("since") or 0))})
            if u.path == "/api/tracks":
                return self.send(200, tracks.tracks(*self.reel_args(q.get("brand"), q.get("id")), q.get("path", "")))
            if u.path == "/api/variants":
                return self.send(200, variants.listing(*self.reel_args(q.get("brand"), q.get("id"))))
            if u.path == "/api/job":
                return self.send(200, {"job": jobs.current()})
            if u.path == "/api/settings":
                return self.send(200, {"settings": jobs.settings(), "choices": jobs.CHOICES, "auth": jobs.auth(),
                                       "plugin": bool(jobs.plugin_dir())})
            if u.path == "/api/brands":
                return self.send(200, brand_colors())
            if u.path == "/api/rules":
                return self.send(200, [{"path": k, "title": v} for k, v in rule_files().items()])
            if u.path == "/api/rule":
                return self.send(200, rule_read(q.get("path", "")))
            if u.path == "/api/reels":
                return self.send(200, reels())
            if u.path == "/api/info":
                p = (ROOT / q["path"]).resolve()
                if not p.is_relative_to(ROOT) or not p.is_file():
                    return self.send(404, {"error": "нет файла"})
                return self.send(200, probe(p))
            if u.path == "/api/notes":
                return self.send(200, N.read(*self.reel_args(q.get("brand"), q.get("id"))))
            if u.path.startswith("/media/"):
                return self.media(u.path[len("/media/"):])
            self.send(404, {"error": "нет страницы"})
        except (KeyError, ValueError) as e:
            self.send(400, {"error": str(e)})

    def do_POST(self):
        if not self.allowed(True):
            return self.send(403, {"error": "запрос не со страницы пометок"})
        LAST["t"] = time.time()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/settings":
                return self.send(200, {"settings": jobs.save_settings(body)})
            if self.path == "/api/rule":
                return self.send(*rule_write(body["path"], body["text"], body.get("mtime")))
            if self.path == "/api/claude/stop":
                return self.send(200, {"stopped": jobs.stop()})
            brand, rid = self.reel_args(body.get("brand"), body.get("id"))
            if self.path == "/api/claude":
                return self.send(200, {"job": jobs.start_claude(brand, rid, body.get("mode", "notes"), body.get("text", ""),
                                                                body.get("ctx"))})
            if self.path == "/api/claude/reset":
                jobs.reset_session(brand, rid)
                return self.send(200, {"ok": True})
            if self.path == "/api/preview":
                return self.send(200, jobs.preview(brand, rid, body.get("frame"), body.get("range"), body.get("path")))
            if self.path == "/api/word":
                return self.send(200, tracks.set_word(brand, rid, int(body["i"]), float(body["start"]), body["old"], body["text"]))
            if self.path == "/api/sound":
                return self.send(200, tracks.set_sound(brand, rid, int(body["i"]), body["file"], body.get("at"),
                                                       body.get("below_db"), body.get("gain_db")))
            if self.path == "/api/apply/sounds":
                return self.send(200, tracks.apply_sounds(brand, rid, body["path"]))
            if self.path == "/api/apply/captions":
                return self.send(200, tracks.apply_captions(brand, rid, body["path"]))
            if self.path == "/api/check":
                return self.send(200, tracks.run_check(brand, rid, body["path"]))
            if self.path == "/api/variants/apply":
                j = jobs.current()
                if j and j["id"] == rid:
                    raise jobs.Busy("С роликом сейчас работает " + j["title"])
                return self.send(200, variants.apply(brand, rid, body["vid"], body["key"]))
            if self.path == "/api/rule/accept":
                note = next(n for n in N.read(brand, rid)["notes"] if n["id"] == body["note_id"])
                rule = note.get("rule") or {}
                if rule.get("status") != "proposed":
                    raise ValueError("предложения правила у пометки нет")
                text = (body.get("text") or rule["text"]).strip()
                got = N.insert_rule(rule["file"], text, rule.get("section"))
                return self.send(200, N.update(brand, rid, lambda d: N.upsert(d, {"id": note["id"], "rule": {
                    **rule, "text": text, "status": "written", "written": N.now(), "line": got["line"]}})))
            if self.path == "/api/rule/dismiss":
                note = next(n for n in N.read(brand, rid)["notes"] if n["id"] == body["note_id"])
                return self.send(200, N.update(brand, rid, lambda d: N.upsert(d, {"id": note["id"], "rule": {
                    **(note.get("rule") or {}), "status": "dismissed"}})))
            if self.path == "/api/reel/delete":
                return self.send(200, reel_delete(brand, rid, body.get("scope"), body.get("file", ""), body.get("dry", True)))
            if self.path == "/api/note":
                note = body["note"]
                if not isinstance(note, dict) or not re.match(r"^[a-z0-9]{1,20}$", str(note.get("id", ""))):
                    raise ValueError("пометка без id")
                return self.send(200, N.update(brand, rid, lambda d: N.upsert(d, note)))
            if self.path == "/api/status":
                return self.send(200, tracker.change(brand, rid, body.get("status", ""), body.get("date", ""),
                                                     body.get("link", ""), body.get("result", "")))
            if self.path == "/api/note/delete":
                nid = body["note_id"]
                return self.send(200, N.update(brand, rid, lambda d: d.update(
                    notes=[n for n in d["notes"] if n["id"] != nid])))
            self.send(404, {"error": "нет такого действия"})
        except jobs.Busy as e:
            self.send(409, {"error": str(e)})
        except (KeyError, ValueError, StopIteration, json.JSONDecodeError) as e:
            self.send(400, {"error": str(e) or "нет такой пометки"})
        except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
            self.send(500, {"error": str(e)})


def watchdog(httpd):
    """Выключить сервер, когда нет работы: страница не опрашивает (закрыта), Claude, рендер и проверка не идут."""
    while True:
        time.sleep(20)
        idle = float(jobs.settings()["idle_minutes"] or 0)
        if idle and not jobs.current() and time.time() - LAST["t"] > idle * 60:
            print(f"Без работы {idle:g} мин — выключаюсь. Открыть снова: та же команда запуска.", flush=True)
            httpd.shutdown()
            return


def main():
    port = (ARGS and ARGS.port) or int(C.get("port"))
    if not N.brands():
        sys.exit(f"В {ROOT} нет брендов: нужны папки с {C.get('brand_marker')} (или этот файл в корне). "
                 "Укажите папку проекта: --root, или brand_marker в .reel-review.json.")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    idle = float(jobs.settings()["idle_minutes"] or 0)
    print(f"Страница пометок: http://127.0.0.1:{port} · проект {ROOT}"
          + (f" · без работы выключусь через {idle:g} мин" if idle else " · сам не выключаюсь"), flush=True)
    threading.Thread(target=watchdog, args=(httpd,), daemon=True).start()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
