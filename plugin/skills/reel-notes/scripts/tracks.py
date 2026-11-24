#!/usr/bin/env python3
"""Дорожки под видео на странице пометок и правки без Claude.

Дорожки: склейки (captions.json → segments), вставки (visual_plan.json → inserts), слова субтитров, звуки (sfx.json),
находки проверки проекта (ключ check в .reel-review.json; результат — edit/<id>/verify/check.json).

Правки мышью:
- слово субтитров — во всех копиях слов: captions.json, reelkit-props.json (из них рисует кит it-reelsmaker),
  <remotion>/src/plans/<id>.json. Окна правок копятся в edit/<id>/caption_edits.json до «В мастер»:
  patch_render.py плагина it-reelsmaker перерисовывает ±4 с вокруг слова прямо в мастере (≈ 20 с), звук не трогается;
- звук — время и громкость в sfx.json. «В мастер»: видео мастера + мастер голоса (out/renders/<id>-voice.mp4)
  → скрипт сведения проекта (ключ sfx). Прежний мастер — out/renders/<имя>-prev.mp4 (один, перезаписывается).
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

import config as C
import jobs
import notes as N
from config import ROOT
WINDOW = 4.0  # с вокруг исправленного слова: фраза субтитров на экране целиком, с запасом
LEVEL = re.compile(r"^(ОШИБКА|ПРОВЕРИТЬ|ок)\s+(.*)$")
# место на шкале — только диапазон «a–b», число в начале строки или «X и Y с»; прочие числа — длительности
SPAN = re.compile(r"(\d+[.,]\d+)\s*[–-]\s*(\d+[.,]\d+)")
LEAD = re.compile(r"^(\d+[.,]\d+)\s*с")
TWICE = re.compile(r"(\d+[.,]\d+)\s+и\s+(\d+[.,]\d+)\s*с")
_dur = {}


def load(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def save(p, doc):
    """Записать JSON в том же отступе, что был (файлы плагина — indent 1)."""
    old = p.read_text(encoding="utf-8") if p.is_file() else ""
    m = re.match(r"[\[{]\n( +)", old)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=len(m[1]) if m else 1) + ("\n" if old.endswith("\n") else ""),
                   encoding="utf-8")
    os.replace(tmp, p)


def edir(brand, rid):
    return N.bdir(brand) / "edit" / rid


def caption_copies(brand, rid):
    """[(файл, документ, слова)] — все копии слов субтитров, которые есть у ролика."""
    out = []
    for p in (edir(brand, rid) / "captions.json", edir(brand, rid) / "reelkit-props.json",
              N.remotion(brand) / "src" / "plans" / f"{rid}.json"):
        d = load(p)
        words = d and (d.get("words") or (d.get("captions") or {}).get("words"))
        if isinstance(words, list) and words and {"text", "start"} <= set(words[0]):
            out.append((p, d, words))
    return out


def first_num(out):
    """Первое число из вывода ffprobe csv («39.666667,» у видео с side data)."""
    m = re.search(r"\d+(?:\.\d+)?", out or "")
    return float(m[0]) if m else 0.0


def sound_dur(p):
    key = (str(p), p.stat().st_mtime)
    if key not in _dur:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                           capture_output=True, text=True)
        _dur[key] = first_num(r.stdout) or 0.3
    return _dur[key]


def master_of(brand, rid, rel):
    """Мастер, который смотрит страница: <бренд>/out/<файл> этого ролика."""
    p = (ROOT / rel).resolve()
    out = (N.bdir(brand) / "out").resolve()
    if p.parent != out or not p.is_file() or p.suffix != ".mp4":
        raise ValueError("мастер должен лежать в out/ бренда")
    return p


def voice_of(brand, rid):
    """Самый свежий мастер голоса ролика: out/renders/<id>[-vN]-voice.mp4."""
    b = N.bdir(brand)
    d = b / "out" / "renders"
    vs = [f for f in d.glob("*-voice.mp4") if N.owner(b, f.name) == rid] if d.is_dir() else []
    return max(vs, key=lambda f: f.stat().st_mtime) if vs else None


def duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                        "-of", "csv=p=0", str(p)], capture_output=True, text=True)
    return first_num(r.stdout)


def tracks(brand, rid, rel):
    e = edir(brand, rid)
    master = master_of(brand, rid, rel)
    mt = master.stat().st_mtime
    caps = load(e / "captions.json") or {}
    copies = caption_copies(brand, rid)
    words = copies[0][2] if copies else []
    plan = load(e / "visual_plan.json") or {}
    sfx = load(e / "sfx.json")
    sounds = []
    for i, s in enumerate((sfx or {}).get("sounds", [])):
        f = next((b / s["file"] for b in (e, N.bdir(brand)) if (b / s["file"]).is_file()), None)
        sounds.append({"i": i, "file": s["file"], "t0": float(s["at"]), "t1": float(s["at"]) + (sound_dur(f) if f else 0.3),
                       "what": s.get("what", ""), "below_db": s.get("below_db"), "gain_db": s.get("gain_db"),
                       "missing": f is None})
    check = load(e / "verify" / "check.json")
    edits = load(e / "caption_edits.json") or []
    return {
        "cuts": [{"t0": s["out_start"], "t1": s["out_start"] + s["out_dur"], "label": s.get("source", ""),
                  "what": s.get("beat", "")} for s in caps.get("segments", [])],
        "inserts": [{"t0": i["start"], "t1": i["start"] + i["dur"], "label": i.get("id", ""), "kind": i.get("kind", ""),
                     "what": i.get("what", "")} for i in plan.get("inserts", []) if "start" in i and "dur" in i],
        "words": [{"i": i, "t0": w["start"], "t1": w.get("end", w["start"] + 0.2), "text": w["text"]} for i, w in enumerate(words)],
        # правка слова дойдёт до кадра, только если композиция читает одну из копий (у op-02 свои «subs» в плане)
        "words_editable": any(p.name != "captions.json" for p, _, _ in copies),
        "caption_files": [str(p.relative_to(ROOT)) for p, _, _ in copies],
        "sounds": sounds, "below_voice_db": (sfx or {}).get("below_voice_db", 15), "has_sfx": sfx is not None,
        "sfx_pending": bool(sfx) and (e / "sfx.json").stat().st_mtime > mt,
        "voice": str(v.relative_to(ROOT)) if (v := voice_of(brand, rid)) else None,
        "caption_edits": edits,
        # что умеет проект: проверка и сведение — свои скрипты (настройки), перерисовка слов — patch_render.py it-reelsmaker
        "can": {"check": bool(C.path("check")), "sfx": bool(C.path("sfx")), "patch": bool(patch_render())},
        "check": check if check and check.get("master") == rel else None,
        "check_stale": bool(check) and (check.get("master") != rel or check.get("master_mtime") != mt),
    }


def patch_render():
    d = jobs.plugin_dir()
    p = d and Path(d) / "skills" / "it-reelsmaker" / "scripts" / "patch_render.py"
    return p if p and p.is_file() else None


# ---------- правки ----------
def set_word(brand, rid, i, start, old, text):
    text = " ".join(str(text).split())
    if not text or len(text) > 60:
        raise ValueError("слово: от 1 до 60 знаков, без переносов")
    changed, hit = [], None
    for p, d, words in caption_copies(brand, rid):
        w = words[i] if 0 <= i < len(words) and abs(words[i]["start"] - start) < 0.005 and words[i]["text"] == old else \
            next((w for w in words if abs(w["start"] - start) < 0.005 and w["text"] == old), None)
        if w:
            w["text"] = text
            hit = hit or w
            save(p, d)
            changed.append(str(p.relative_to(ROOT)))
    if not changed:
        raise ValueError("слово не найдено: субтитры поменялись — обновите страницу")
    ed = edir(brand, rid) / "caption_edits.json"
    edits = load(ed) or []
    edits.append({"start": hit["start"], "end": hit.get("end", hit["start"] + 0.3), "old": old, "new": text,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    same = [e for e in edits if e["start"] == hit["start"]]
    if same[0]["old"] == text:  # слово вернули как было — в мастер нести нечего
        edits = [e for e in edits if e["start"] != hit["start"]]
    if edits:
        save(ed, edits)
    elif ed.is_file():
        ed.unlink()
    return {"changed": changed}


def num(x, nd):
    v = round(float(x), nd)
    return int(v) if v.is_integer() else v  # 8, а не 8.0: файл не меняется без нужды


def set_sound(brand, rid, i, file, at=None, below_db=None, gain_db=None):
    p = edir(brand, rid) / "sfx.json"
    doc = load(p)
    if not doc or not 0 <= i < len(doc.get("sounds", [])) or doc["sounds"][i]["file"] != file:
        raise ValueError("звук не найден: sfx.json поменялся — обновите страницу")
    s = doc["sounds"][i]
    if at is not None:
        at = num(at, 3)
        dur = (load(edir(brand, rid) / "captions.json") or {}).get("duration") or 1e9
        if not 0 <= at < dur:
            raise ValueError(f"время звука: от 0 до {dur:.2f} с")
        s["at"] = at
    if below_db is not None:
        s["below_db"] = num(below_db, 1)
    if gain_db is not None:
        s["gain_db"] = num(gain_db, 1)
    save(p, doc)
    return {"sound": s}


# ---------- в мастер ----------
def keep_prev(master):
    prev = master.parent / "renders" / f"{master.stem}-prev.mp4"
    prev.parent.mkdir(exist_ok=True)
    os.replace(master, prev)
    return prev


def apply_sounds(brand, rid, rel):
    master = master_of(brand, rid, rel)
    sfx = C.path("sfx")
    if not sfx:
        raise ValueError("сведение звуков не настроено: ключ sfx в .reel-review.json")
    voice = voice_of(brand, rid)
    if not voice:
        raise ValueError("нет мастера голоса (out/renders/<id>-voice.mp4): свести звуки может только «Собрать версию»")
    if abs(duration(voice) - duration(master)) > 0.05:
        raise ValueError(f"{voice.name} другой длины, чем мастер: ролик перенарезан после него — «Собрать версию»")
    jobs.claim("sfx", brand, rid, "звуки → в мастер")
    try:
        with tempfile.TemporaryDirectory() as t:
            src, out = Path(t) / "in.mp4", Path(t) / "out.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(master), "-i", str(voice), "-map", "0:v", "-map", "1:a",
                            "-c", "copy", str(src)], check=True)
            r = subprocess.run([sys.executable, str(sfx), str(src), str(edir(brand, rid) / "sfx.json"), "-o", str(out)],
                               cwd=N.bdir(brand), capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"{sfx.name}: " + (r.stderr or r.stdout).strip()[-600:])
            prev = keep_prev(master)
            shutil.move(out, master)
        return {"master": rel, "prev": str(prev.relative_to(ROOT)), "log": r.stdout.strip().splitlines()[-12:]}
    finally:
        jobs.release()


def windows(edits, dur, fps):
    """Окна кадров вокруг исправленных слов, слитые при пересечении."""
    ws = sorted((max(0, round((e["start"] - WINDOW) * fps)), min(round(dur * fps) - 1, round((e["end"] + WINDOW) * fps)))
                for e in edits)
    out = []
    for a, b in ws:
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def apply_captions(brand, rid, rel):
    master = master_of(brand, rid, rel)
    ed = edir(brand, rid) / "caption_edits.json"
    edits = load(ed) or []
    if not edits:
        raise ValueError("правок субтитров нет")
    pr = patch_render()
    if not pr:
        raise ValueError("плагин it-reelsmaker не найден: patch_render.py недоступен")
    props = edir(brand, rid) / "reelkit-props.json"
    fps = 30
    ws = windows(edits, duration(master), fps)
    jobs.claim("captions", brand, rid, "субтитры → в мастер")
    try:
        with tempfile.TemporaryDirectory() as t:
            cur = Path(t) / "m0.mp4"
            subprocess.run(["cp", "-c", str(master), str(cur)], check=True)
            for k, (a, b) in enumerate(ws, 1):
                nxt = Path(t) / f"m{k}.mp4"
                cmd = [sys.executable, str(pr), str(cur), "--comp", N.comp_id(rid), "--frames", f"{a}-{b}",
                       "--remotion", str(N.remotion(brand)), "-o", str(nxt)]
                if props.is_file():
                    cmd += ["--props", str(props)]
                r = subprocess.run(cmd, capture_output=True, text=True, env=jobs.clean_env())
                if r.returncode or not nxt.is_file():
                    raise RuntimeError("patch_render: " + (r.stderr or r.stdout).strip()[-700:])
                cur = nxt
            prev = keep_prev(master)
            shutil.move(cur, master)
        ed.unlink()
        return {"master": rel, "prev": str(prev.relative_to(ROOT)), "windows": ws}
    finally:
        jobs.release()


# ---------- проверка ----------
def parse_check(text):
    items = []
    for line in text.splitlines():
        m = LEVEL.match(line.strip())
        if not m:
            continue
        level, msg = m[1], m[2]
        f = lambda x: float(x.replace(",", "."))
        t0 = t1 = None
        if level != "ок":
            if (g := SPAN.search(msg)):
                t0, t1 = f(g[1]), f(g[2])
            elif (g := LEAD.match(msg) or TWICE.search(msg)):
                t0 = t1 = f(g[1])
        items.append({"level": level, "text": msg, "t0": t0, "t1": t1})
    return items


def run_check(brand, rid, rel):
    master = master_of(brand, rid, rel)
    check = C.path("check")
    if not check:
        raise ValueError("проверка не настроена: ключ check в .reel-review.json")
    jobs.claim("check", brand, rid, "проверка мастера")
    try:
        r = subprocess.run([sys.executable, str(check), brand, rid, "--master", str(master.relative_to(N.bdir(brand)))],
                           cwd=ROOT, capture_output=True, text=True, timeout=900)
    finally:
        jobs.release()
    items = parse_check(r.stdout)
    if not items:
        raise RuntimeError(f"{check.name}: " + (r.stderr or r.stdout).strip()[-600:])
    doc = {"master": rel, "master_mtime": master.stat().st_mtime, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "items": items,
           "errors": sum(i["level"] == "ОШИБКА" for i in items), "checks": sum(i["level"] == "ПРОВЕРИТЬ" for i in items)}
    (edir(brand, rid) / "verify").mkdir(exist_ok=True)
    save(edir(brand, rid) / "verify" / "check.json", doc)
    return doc


if __name__ == "__main__":
    got = parse_check("ОШИБКА    хвост 0.91 с после последнего слова «час»\n"
                      "ПРОВЕРИТЬ 12.40–13.10 с: между склейками 2 кадр(а)\nок        конец: 0.58 с после слова\n"
                      "ПРОВЕРИТЬ 39.21 с: звук после последнего слова (0.43 с)\n"
                      "ОШИБКА    b03 7.20–8.10 длится 0.90 с (минимум 2.0 с)\n"
                      "ПРОВЕРИТЬ «две версии» звучит дважды: 1.20 и 9.80 с — два захода?\nмусор")
    assert [g["level"] for g in got] == ["ОШИБКА", "ПРОВЕРИТЬ", "ок", "ПРОВЕРИТЬ", "ОШИБКА", "ПРОВЕРИТЬ"]
    assert [(g["t0"], g["t1"]) for g in got] == [(None, None), (12.4, 13.1), (None, None), (39.21, 39.21), (7.2, 8.1),
                                                (1.2, 1.2)], got
    assert windows([{"start": 10, "end": 10.3}, {"start": 12, "end": 12.2}, {"start": 30, "end": 30.4}], 40, 30) == \
        [[180, 486], [780, 1032]]
    assert windows([{"start": 1, "end": 1.2}], 3, 30) == [[0, 89]]
    assert first_num("39.666667,\n") == 39.666667 and first_num("") == 0.0
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.json"
        p.write_text('{\n "a": 1\n}\n')
        save(p, {"a": 2, "b": "ё"})
        assert p.read_text() == '{\n "a": 2,\n "b": "ё"\n}\n'
    print("tracks: ok")
