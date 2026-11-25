#!/usr/bin/env python3
"""Три варианта одной правки: Claude делает их по очереди, пользователь выбирает на странице пометок.

Из корня проекта (или с REEL_REVIEW_ROOT); <scripts> — папка этого файла:
    python3 <scripts>/variants.py "<бренд>" <id> begin "хук динамичнее" [--frame 40] [--range 0-90]  # -> id
    python3 <scripts>/variants.py "<бренд>" <id> save <vid> a "плашка въезжает слева, 10 кадров"
    python3 <scripts>/variants.py "<бренд>" <id> base <vid>          # вернуть файлы как до вариантов
    python3 <scripts>/variants.py "<бренд>" <id> apply <vid> b       # то, что делает кнопка «Выбрать»
    python3 <scripts>/variants.py "<бренд>" <id> list

Снимок — копии файлов ролика: <remotion>/src/<Комп>*.tsx, <remotion>/src/plans/<id>*.json, edit/<id>/*.json (без
notes.json и caption_edits.json). Лежат в edit/<id>/variants/<vid>/<ключ>/ с путями от корня проекта (путь вне корня —
без ведущего «/»). Правки вне этих файлов
(общий кит) вариантами не откатываются. save рисует предпросмотр варианта из кода: кадр (и клип, если задан отрезок).
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

import config as C
import notes as N
from config import ROOT
SKIP = {"notes.json", "caption_edits.json"}
KEYS = ("a", "b", "c", "d")
TRASH = ["/usr/bin/trash"]  # лишние файлы после отката — в Корзину (тест подменяет на rm)


VID = re.compile(r"v[0-9-]{1,30}")  # id набора вариантов приходит со страницы: без «..» и «/»


def vdir(brand, rid, vid=""):
    if vid and not VID.fullmatch(vid):
        raise ValueError("неверный id вариантов")
    return N.bdir(brand) / "edit" / rid / "variants" / vid


def tracked(brand, rid):
    src = N.remotion(brand) / "src"
    fs = list(src.glob(N.comp_id(rid) + "*.tsx")) + list((src / "plans").glob(rid + "*.json"))
    fs += [p for p in (N.bdir(brand) / "edit" / rid).glob("*.json") if p.name not in SKIP]
    return sorted(C.relp(p) for p in fs if p.is_file())


def in_snap(snap, rel):
    """Место файла в снимке: путь вне корня проекта — без ведущего «/», иначе snap / "/abs" дал бы сам оригинал."""
    return snap / rel.lstrip("/")


def manifest(brand, rid, vid):
    return json.loads((vdir(brand, rid, vid) / "variant.json").read_text(encoding="utf-8"))


def write_manifest(brand, rid, vid, m):
    p = vdir(brand, rid, vid) / "variant.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def snapshot(brand, rid, dst):
    files = tracked(brand, rid)
    for rel in files:
        in_snap(dst, rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, in_snap(dst, rel))
    return files


def restore(brand, rid, snap, files):
    """Файлы ролика = снимок: изменённые переписать, лишние (появились после снимка) — в Корзину."""
    changed = []
    for rel in sorted(set(files) | set(tracked(brand, rid))):
        src, dst = in_snap(snap, rel), ROOT / rel
        if rel in files and src.is_file():
            if not dst.is_file() or dst.read_bytes() != src.read_bytes():
                shutil.copy2(src, dst)
                changed.append(rel)
        elif dst.is_file():
            subprocess.run(TRASH + [str(dst)], check=True, capture_output=True)
            changed.append(rel + " (в Корзину)")
    return changed


def begin(brand, rid, task, frame=None, rng=None):
    vid = "v" + time.strftime("%m%d-%H%M%S")
    d = vdir(brand, rid, vid)
    d.mkdir(parents=True)
    files = snapshot(brand, rid, d / "base")
    write_manifest(brand, rid, vid, {"id": vid, "task": task, "frame": frame, "range": rng, "created": N.now(),
                                     "files": {"base": files}, "options": {}, "chosen": None})
    return vid


def save(brand, rid, vid, key, text, scale=0.5):
    m = manifest(brand, rid, vid)
    d = vdir(brand, rid, vid)
    if key in m["options"]:
        shutil.rmtree(d / key, ignore_errors=True)
    m["files"][key] = snapshot(brand, rid, d / key)
    frame = m["frame"] if m["frame"] is not None else (m["range"] or [0])[0]
    got = N.render_preview(brand, rid, [int(frame)] + ([tuple(m["range"])] if m["range"] else []), scale)
    opt = {"text": text, "frame": frame}
    for g in got:
        dst = d / f"{key}{g.suffix}"
        os.replace(g, dst)
        opt["clip" if g.suffix == ".mp4" else "image"] = str(dst.relative_to(ROOT))  # edit/ — всегда внутри корня
    m["options"][key] = opt
    write_manifest(brand, rid, vid, m)
    return opt


def apply(brand, rid, vid, key):
    m = manifest(brand, rid, vid)
    if key != "base" and (key not in KEYS or key not in m["options"]):
        raise ValueError(f"нет варианта {key}")
    changed = restore(brand, rid, vdir(brand, rid, vid) / key, m["files"][key])
    m["chosen"] = key
    m["chosen_at"] = N.now()
    write_manifest(brand, rid, vid, m)
    return {"chosen": key, "changed": changed}


def listing(brand, rid):
    d = vdir(brand, rid)
    ms = [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*/variant.json")] if d.is_dir() else []
    return sorted(ms, key=lambda m: m["created"], reverse=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("brand"); ap.add_argument("id")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("begin"); p.add_argument("task"); p.add_argument("--frame", type=int); p.add_argument("--range")
    p = sub.add_parser("save"); p.add_argument("vid"); p.add_argument("key", choices=KEYS); p.add_argument("text")
    p = sub.add_parser("base"); p.add_argument("vid")
    p = sub.add_parser("apply"); p.add_argument("vid"); p.add_argument("key")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "begin":
        rng = [int(x) for x in a.range.split("-")] if a.range else None
        print(begin(a.brand, a.id, a.task, a.frame, rng))
    elif a.cmd == "save":
        print(json.dumps(save(a.brand, a.id, a.vid, a.key, a.text), ensure_ascii=False))
    elif a.cmd == "base":
        m = manifest(a.brand, a.id, a.vid)
        print("вернул:", ", ".join(restore(a.brand, a.id, vdir(a.brand, a.id, a.vid) / "base", m["files"]["base"])) or "без изменений")
    elif a.cmd == "apply":
        print(json.dumps(apply(a.brand, a.id, a.vid, a.key), ensure_ascii=False))
    else:
        for m in listing(a.brand, a.id):
            print(m["id"], m["task"], "варианты:", ",".join(m["options"]), "выбран:", m["chosen"])


if __name__ == "__main__":
    main()
