#!/usr/bin/env python3
"""Пометки на кадре: <бренд>/edit/<id>/notes.json — общий файл страницы пометок и Claude.

Запуск из корня проекта (или с REEL_REVIEW_ROOT); <scripts> — папка этого файла:
    python3 <scripts>/notes.py "<бренд>" <id> list              # ждут Claude (new, reopened)
    python3 <scripts>/notes.py "<бренд>" <id> list --all
    python3 <scripts>/notes.py "<бренд>" <id> frame n1 before   # кадр пометки с отметками -> edit/<id>/notes/
    python3 <scripts>/notes.py "<бренд>" <id> frame n1 after --master out/<id>.mp4 [--frame 380]
    python3 <scripts>/notes.py "<бренд>" <id> set n1 done "Плашку подняла на 40 px"
    python3 <scripts>/notes.py "<бренд>" <id> set n1 question "Убрать совсем или перенести на 12 с?"
    python3 <scripts>/notes.py "<бренд>" <id> preview n1 [--frame 380]  # «после» из текущего кода, без рендера
    python3 <scripts>/notes.py "<бренд>" <id> rule n1 "<файл правил>" "- 2026-11-24: …"
    python3 <scripts>/notes.py "<бренд>" <id> rule n1 none "частный случай, правилом не станет"

rule — предложение правила из правки (принятой или возвращённой пометки). В файл его пишет не Claude, а пользователь
кнопкой «Записать» на странице: строка встаёт в конец раздела (--section, по умолчанию свой у каждого файла).

preview рисует кадр (у пометки с отрезком — ещё клип отрезка) из текущего кода композиции через stills.mjs плагина:
~2 с кадр, ~7 с на 3 с клипа. Это «после» до полного рендера; после рендера — frame <n> after с мастера.

Статусы: new, reopened — ждут Claude; done, question, wontfix — ответил Claude; accepted — принято пользователем.
Координаты фигур — в кадре 1080×1920, кадр — номер кадра мастера (с 0), range — [первый, последний] кадр.
Фигуры: point, rect (fill — залитая плашка), arrow, pen (pts — линия от руки), text (x, y — левый верх, size); color — свой цвет
наброска графики (kind draw), без него — цвет пометки.
"""
import argparse, fcntl, json, math, os, re, shutil, subprocess, sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import config as C
from config import ROOT

WAITING = ("new", "reopened")
COLOR = (255, 90, 54)
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"  # кириллица и Ӏ


OWN_SUFFIX = re.compile(r"(?:\.prev|-(?:cover|master|voice|seams|grid|sheet|render|patched|prev))+$")


def brands():
    """{имя: папка} брендов: подпапки корня с файлом-маркером; нет таких, но маркер в самом корне — один бренд, корень."""
    mark = C.get("brand_marker")
    subs = {d.name: d for d in sorted(ROOT.iterdir()) if d.is_dir() and (d / mark).is_file()}
    return subs or ({ROOT.name: ROOT} if (ROOT / mark).is_file() else {})


def bdir(brand):
    b = brands().get(brand)
    if b is None:
        raise ValueError(f"нет бренда {brand}")
    return b


def remotion(brand):
    """Remotion-проект бренда: remotion_dir из it-reelsmaker.json, иначе "remotion" из настроек проекта."""
    m = bdir(brand) / C.get("brand_marker")
    try:
        d = json.loads(m.read_text(encoding="utf-8")).get("remotion_dir") if m.suffix == ".json" else None
    except (OSError, ValueError):
        d = None
    return (bdir(brand) / d).resolve() if d else ROOT / C.get("remotion")  # абсолютный d остаётся как есть


def owner(bdir_, name):
    """Ролик файла: ab-14-v1-render.mp4 -> ab-14, ab-12-v3-ce-voice.mp4 -> ab-12-ce, cd-04-cover.jpg -> cd-04."""
    stem = OWN_SUFFIX.sub("", Path(name).stem)
    m = re.match(r"^([a-z]+-\d+)-v\d+(.*)$", stem)
    if not m:
        return stem
    alt = m[1] + OWN_SUFFIX.sub("", m[2])
    return alt if (bdir_ / "edit" / alt).is_dir() else m[1]


def rule_files():
    """Файлы правил (окно «Правила» и правила из правок): путь от корня -> название."""
    fs = {}
    for pat in C.get("rules"):
        for p in sorted(ROOT.glob(pat)):
            if p.is_file():
                rel = p.relative_to(ROOT)
                fs[str(rel)] = f"{rel.parts[0]} — {p.name}" if len(rel.parts) > 2 else str(rel)
    return fs


def insert_rule(rel, text, section=None):
    """Вставить текст правила в конец раздела «## <section>…» файла правил (нет раздела — в конец файла)."""
    if rel not in rule_files():
        raise ValueError(f"{rel}: не файл правил")
    p = ROOT / rel
    section = section or C.get("rule_sections").get(Path(rel).name, "")
    lines = p.read_text(encoding="utf-8").rstrip("\n").split("\n")
    head = next((i for i, l in enumerate(lines) if l.startswith("## ") and l[3:].strip().startswith(section)), None) \
        if section else None
    end = len(lines) if head is None else next((i for i in range(head + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > (head or 0) + 1 and not lines[end - 1].strip():
        end -= 1
    block = text.strip("\n").split("\n")
    if block[0].startswith("### "):  # приём каталога — отдельный блок с пустой строкой перед ним
        block = [""] + block
    lines[end:end] = block
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return {"file": rel, "section": section if head is not None else "", "line": end + 1}


def now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def notes_path(brand, rid):
    return bdir(brand) / "edit" / rid / "notes.json"


@contextmanager
def locked(brand, rid):
    p = notes_path(brand, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{p}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def read(brand, rid):
    p = notes_path(brand, rid)
    doc = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    doc.setdefault("brand", brand), doc.setdefault("id", rid), doc.setdefault("notes", [])
    return doc


def update(brand, rid, fn):
    """Прочитать, изменить fn(doc), записать — под блокировкой (страница и Claude пишут в один файл)."""
    with locked(brand, rid):
        doc = read(brand, rid)
        fn(doc)
        p = notes_path(brand, rid)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        return doc


def upsert(doc, note):
    """Слить поля пометки в существующую (по id); append — реплика в ветку обсуждения."""
    note = dict(note)
    append = note.pop("append", None)
    cur = next((n for n in doc["notes"] if n["id"] == note["id"]), None)
    if cur is None:
        cur = {"status": "new", "thread": [], "created": now()}
        doc["notes"].append(cur)
    if note.get("status") == "reopened" and cur.get("status") != "reopened":
        cur["returned"] = cur.get("returned", 0) + 1  # возвращённая правка — повод для правила
    cur.update(note)
    cur["updated"] = now()
    if append:
        cur.setdefault("thread", []).append({"who": append.get("who", "user"), "text": append["text"], "at": now()})
    doc["notes"].sort(key=lambda n: n.get("frame", 0))


def comp_id(rid):
    """ab-14 -> ReelAb14, ab-12-ce -> ReelAb12Ce (композиция ролика в src/Root.tsx Remotion-проекта)."""
    return "Reel" + "".join(p.capitalize() for p in rid.split("-"))


def tool(name):
    return shutil.which(name) or next((p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}") if Path(p).exists()), name)


def render_preview(brand, rid, items, scale=0.5):
    """Кадры (int) и отрезки ((a, b)) из текущего кода композиции -> [Path] в edit/<id>/preview/."""
    props = bdir(brand) / "edit" / rid / "reelkit-props.json"
    out = bdir(brand) / "edit" / rid / "preview"
    args = [f"{i[0]}-{i[1]}" if isinstance(i, tuple) else str(int(i)) for i in items]
    r = subprocess.run([tool("node"), str(C.HERE / "stills.mjs"), comp_id(rid), str(props) if props.is_file() else "-", str(out),
                        *args, f"--scale={scale}"], cwd=remotion(brand), capture_output=True, text=True, timeout=900)
    if r.returncode:
        err = [l for l in (r.stderr or r.stdout).splitlines() if l.strip()]
        raise RuntimeError("предпросмотр: " + " | ".join(err[-4:])[-900:])
    return [Path(l) for l in r.stdout.splitlines() if l.startswith(str(out))]


def draw_frame(master, frame, fps, shapes, out, image=None):
    """Кадр мастера (или готовая картинка image) с фигурами пометки -> out."""
    from PIL import Image, ImageDraw, ImageFont
    out.parent.mkdir(parents=True, exist_ok=True)
    if image:
        Image.open(image).convert("RGB").save(out)
    else:
        # по номеру кадра, а не -ss: на реальном мастере -ss (n+0.5)/fps давал кадр n+1…n+2 (страница и код показывают n)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(master), "-vf", f"select=eq(n\\,{int(frame)})",
                        "-frames:v", "1", "-fps_mode", "passthrough", str(out)], check=True)
    im = Image.open(out).convert("RGB")
    k = im.width / 1080
    d = ImageDraw.Draw(im, "RGBA")
    lw = max(3, round(6 * k))
    for s in shapes or []:
        col = s.get("color") or COLOR
        if s["type"] == "point":
            r = 22 * k
            x, y = s["x"] * k, s["y"] * k
            d.ellipse([x - r, y - r, x + r, y + r], outline=col, width=lw)
        elif s["type"] == "rect":
            box = [s["x"] * k, s["y"] * k, (s["x"] + s["w"]) * k, (s["y"] + s["h"]) * k]
            if s.get("fill"):
                d.rounded_rectangle(box, radius=24 * k, fill=col)
            else:
                d.rectangle(box, outline=col, width=lw)
        elif s["type"] == "arrow":
            x1, y1, x2, y2 = (s[c] * k for c in ("x1", "y1", "x2", "y2"))
            d.line([x1, y1, x2, y2], fill=col, width=lw)
            a = math.atan2(y2 - y1, x2 - x1)
            h = 34 * k
            d.polygon([(x2, y2), (x2 - h * math.cos(a - 0.45), y2 - h * math.sin(a - 0.45)),
                       (x2 - h * math.cos(a + 0.45), y2 - h * math.sin(a + 0.45))], fill=col)
        elif s["type"] == "pen" and len(s.get("pts", [])) > 1:
            d.line([(x * k, y * k) for x, y in s["pts"]], fill=col, width=max(2, round(s.get("w", 6) * k)), joint="curve")
        elif s["type"] == "text" and s.get("text"):
            f = ImageFont.truetype(FONT, max(10, round(s.get("size", 64) * k)))
            d.text((s["x"] * k, s["y"] * k), s["text"], font=f, fill=col)
    im.save(out)


def shape_text(s):
    c = f" {s['color']}" if s.get("color") else ""
    if s["type"] == "point":
        return f"точка {s['x']:.0f},{s['y']:.0f}{c}"
    if s["type"] == "rect":
        return f"{'плашка' if s.get('fill') else 'рамка'} x {s['x']:.0f}–{s['x'] + s['w']:.0f}, y {s['y']:.0f}–{s['y'] + s['h']:.0f}{c}"
    if s["type"] == "arrow":
        return f"стрелка {s['x1']:.0f},{s['y1']:.0f} → {s['x2']:.0f},{s['y2']:.0f}{c}"
    if s["type"] == "pen":
        xs, ys = [p[0] for p in s["pts"]], [p[1] for p in s["pts"]]
        return f"линия от руки x {min(xs):.0f}–{max(xs):.0f}, y {min(ys):.0f}–{max(ys):.0f}, {len(xs)} точек{c}"
    if s["type"] == "text":
        return f"текст «{s.get('text', '')}» с {s['x']:.0f},{s['y']:.0f}, {s.get('size', 64)} px{c}"
    return s["type"]


def describe(n):
    sh = "; ".join(shape_text(s) for s in n.get("shapes", []))
    rng = f", отрезок кадров {n['range'][0]}–{n['range'][1]}" if n.get("range") else ""
    rng += f", возвращалась {n['returned']} раз" if n.get("returned") else ""
    rule = n.get("rule") or {}
    rng += f", правило: {rule.get('status')}" if rule else ""
    last = n.get("thread", [])[-1:] or [None]
    tail = f"\n      последняя реплика ({last[0]['who']}): {last[0]['text']}" if last[0] else ""
    return (f"{n['id']:>8} [{n.get('status')}] {n.get('t', 0):6.2f} с, кадр {n.get('frame')}{rng}, {n.get('kind')}, "
            f"мастер {n.get('master', '?')}\n      «{n.get('text', '')}»" + (f"\n      {sh}" if sh else "") + tail)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("brand"); ap.add_argument("id")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("frame"); p.add_argument("note"); p.add_argument("tag", choices=["before", "after"])
    p.add_argument("--master"); p.add_argument("--frame", type=int)
    p = sub.add_parser("preview"); p.add_argument("note"); p.add_argument("--frame", type=int)
    p.add_argument("--scale", type=float, default=0.5)
    p = sub.add_parser("set"); p.add_argument("note"); p.add_argument("status", choices=["done", "question", "wontfix"])
    p.add_argument("reply")
    p = sub.add_parser("rule"); p.add_argument("note"); p.add_argument("file", help="файл правил от корня или none")
    p.add_argument("text"); p.add_argument("--section")
    a = ap.parse_args()
    doc = read(a.brand, a.id)
    if a.cmd == "list":
        shown = [n for n in doc["notes"] if a.all or n.get("status") in WAITING]
        print(f"{a.id}: пометок {len(doc['notes'])}, ждут {sum(n.get('status') in WAITING for n in doc['notes'])}")
        for n in shown:
            print(describe(n))
        return
    n = next((x for x in doc["notes"] if x["id"] == a.note), None)
    if n is None:
        sys.exit(f"нет пометки {a.note}")
    if a.cmd == "frame":
        b = bdir(a.brand)
        master = b / a.master if a.master else ROOT / n["master_path"]
        frame = a.frame if a.frame is not None else n["frame"]
        out = b / "edit" / a.id / "notes" / f"{n['id']}-{a.tag}.png"
        draw_frame(master, frame, n.get("fps") or 30, n.get("shapes"), out)
        update(a.brand, a.id, lambda d: upsert(d, {"id": n["id"], a.tag: str(out.relative_to(ROOT)),
                                                   f"{a.tag}_frame": frame, f"{a.tag}_src": "master",
                                                   **({"after_clip": None} if a.tag == "after" else {})}))
        print(out)
    elif a.cmd == "preview":
        frame = a.frame if a.frame is not None else n["frame"]
        rng = n.get("range") if a.frame is None else None
        got = render_preview(a.brand, a.id, [frame] + ([tuple(rng)] if rng else []), a.scale)
        out = bdir(a.brand) / "edit" / a.id / "notes" / f"{n['id']}-after.png"
        draw_frame(None, frame, 0, n.get("shapes"), out, image=got[0])
        clip = got[1] if len(got) > 1 else None
        if clip:
            dst = out.with_name(f"{n['id']}-after.mp4")
            os.replace(clip, dst)
            clip = str(dst.relative_to(ROOT))
        update(a.brand, a.id, lambda d: upsert(d, {"id": n["id"], "after": str(out.relative_to(ROOT)), "after_frame": frame,
                                                   "after_src": "preview", "after_clip": clip}))
        print(out, clip or "")
    elif a.cmd == "rule":
        if a.file == "none":
            rule = {"status": "none", "why": a.text}
        elif a.file not in rule_files():
            sys.exit(f"{a.file}: не файл правил. Можно: {', '.join(rule_files())}")
        else:
            rule = {"status": "proposed", "file": a.file, "section": a.section or C.get("rule_sections").get(Path(a.file).name, ""),
                    "text": a.text}
        update(a.brand, a.id, lambda d: upsert(d, {"id": n["id"], "rule": rule}))
        print(f"{n['id']}: правило {rule['status']}")
    elif a.cmd == "set":
        update(a.brand, a.id, lambda d: upsert(d, {"id": n["id"], "status": a.status,
                                                   "append": {"who": "claude", "text": a.reply}}))
        print(f"{n['id']}: {a.status}")


if __name__ == "__main__":
    main()
