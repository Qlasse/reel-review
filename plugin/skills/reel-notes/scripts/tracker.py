#!/usr/bin/env python3
"""Статус ролика со страницы пометок -> таблица роликов, файл отметок публикации и память Claude.

Файлы — из настроек проекта (.reel-review.json): "tracker" — Markdown-таблица по брендам (раздел «## <бренд>», колонки
«Ролик | … | Статус | …», для публикации ещё «Опубликован | Ссылка | Результат»), "published" — строки «<id> <дата>
<ссылка> <результат>», как их пишут руками. Без "tracker" статусов на странице нет.
В памяти Claude (<id>-status.md, если файл есть) обновляется строка «Статус в <таблица>».

    python3 tracker.py      # самопроверка на временных файлах
"""
import os, re, time
from pathlib import Path

import config as C
from config import ROOT

STATUSES = ["в работе", "сдан", "опубликован", "отложен", "не закончен"]
MEMORY = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(ROOT)) / "memory"
REEL = re.compile(r"[a-z]+-\d+")
PUB_COLS = ("Опубликован", "Ссылка", "Результат")


def key(rid):
    """ab-12-ce -> ab-12: у языковой версии нет своей строки в таблице."""
    m = REEL.match(rid)
    return m.group(0) if m else rid


def paths(root=None, names=None):
    """(таблица, файл отметок или None) — из настроек проекта или явно (самопроверка)."""
    t, m = names or (C.get("tracker"), C.get("published"))
    if not t:
        raise ValueError("таблица статусов не настроена: ключ tracker в .reel-review.json")
    root = root or ROOT
    return root / t, (root / m if m else None)


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def rows(text):
    """{id: {бренд, колонки таблицы}} из таблицы роликов."""
    out, brand, head = {}, None, None
    for line in text.splitlines():
        if line.startswith("## "):
            brand, head = line[3:].strip(), None
        elif line.lstrip().startswith("|"):
            c = cells(line)
            if "Статус" in c:
                head = c
            elif head and len(c) == len(head) and not set(c[0]) <= set("-: "):
                out[c[0].strip("`*")] = dict(zip(head, c), brand=brand)
    return out


def set_status(text, brand, rid, status, fill=None):
    """Новый текст таблицы: статус (и колонки fill, какие есть) в строке ролика; нет строки — новая в конце таблицы бренда."""
    fill = {k: v for k, v in (fill or {}).items() if v}
    lines, cur, head, last = text.splitlines(), None, None, {}
    for i, line in enumerate(lines):
        if line.startswith("## "):
            cur, head = line[3:].strip(), None
        elif line.lstrip().startswith("|"):
            c = cells(line)
            if "Статус" in c:
                head = c
            elif head and len(c) == len(head):
                last[cur] = (i, head)
                if c[0].strip("`*") == rid:
                    c[head.index("Статус")] = status
                    for k, v in fill.items():
                        if k in head:
                            c[head.index(k)] = v
                    lines[i] = "| " + " | ".join(c) + " |"
                    return "\n".join(lines) + "\n"
    if brand in last:
        i, head = last[brand]
        row = ["" for _ in head]
        row[0], row[head.index("Статус")] = rid, status
        for k, v in fill.items():
            if k in head:
                row[head.index(k)] = v
        lines.insert(i + 1, "| " + " | ".join(row) + " |")
    else:
        head = ["Ролик", "Тема", "Статус", "Версия", *PUB_COLS]
        row = [rid, "", status, ""] + [fill.get(k, "") for k in PUB_COLS]
        lines += ["", f"## {brand}", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head), "| " + " | ".join(row) + " |"]
    return "\n".join(lines) + "\n"


def drop(rid, root=None, names=None):
    """Убрать строку ролика из таблицы и отметку публикации (ролик удалён со страницы)."""
    table, marks = paths(root, names)
    if table.is_file() and rid in rows(t := table.read_text(encoding="utf-8")):
        write(table, "\n".join(l for l in t.splitlines()
                               if not (l.lstrip().startswith("|") and cells(l)[0].strip("`*") == rid)) + "\n")
    if marks and marks.is_file() and (m := marks.read_text(encoding="utf-8")) != marks_without(m, rid):
        write(marks, marks_without(m, rid))


def marks_without(text, rid):
    keep = [l for l in text.splitlines() if not (l.split() and l.split()[0].lower() == rid)]
    return "\n".join(keep) + "\n" if keep else ""  # новый файл — без пустой первой строки


def write(p, text):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def remember(rid, status, root_memory=MEMORY, table="таблице"):
    """Строка статуса в памяти Claude, сразу после шапки <id>-status.md. Новых файлов памяти не создаёт."""
    f = root_memory / f"{rid}-status.md"
    if not f.is_file():
        return False
    MEMO_MARK = f"**Статус в {table}:**"
    lines = [l for l in f.read_text(encoding="utf-8").splitlines() if not l.startswith(MEMO_MARK)]
    ends = [i for i, l in enumerate(lines) if l.strip() == "---"]
    at = ends[1] + 1 if len(ends) > 1 and ends[0] == 0 else 0
    line = f"{MEMO_MARK} {status} · {time.strftime('%Y-%m-%d')} (страница пометок; при расхождении верить таблице)"
    lines[at:at] = ["", line] if at else [line, ""]
    write(f, "\n".join(lines).replace("\n\n\n", "\n\n") + "\n")
    return True


def change(brand, rid, status, date="", link="", result="", root=None, memory=MEMORY, names=None):
    """Сменить статус ролика. -> {id, status} как в таблице после записи."""
    if status not in STATUSES:
        raise ValueError(f"статус: один из {', '.join(STATUSES)}")
    rid = key(rid)
    table, marks = paths(root, names)
    m = marks.read_text(encoding="utf-8") if marks and marks.is_file() else ""
    fill = None
    if status == "опубликован":
        date = date.strip() or time.strftime("%d.%m")
        fill = dict(zip(PUB_COLS, (date, link.strip(), result.strip().replace("\n", " "))))
        if marks:
            write(marks, marks_without(m, rid) + " ".join(x for x in (rid, *fill.values()) if x) + "\n")
    elif m:
        write(marks, marks_without(m, rid))  # «Ошиблись — удалите строку»: снять отметку публикации
    write(table, set_status(table.read_text(encoding="utf-8") if table.is_file() else "# Ролики\n", brand, rid, status, fill))
    remember(rid, status, memory, table.name)
    return {"id": rid, "status": rows(table.read_text(encoding="utf-8")).get(rid, {}).get("Статус", status)}


if __name__ == "__main__":
    import tempfile
    N = ("reels.md", "published.txt")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "reels.md").write_text("# Ролики\n\n## Brand A\n\n| Ролик | Тема | Статус | Версия | Опубликован | Ссылка | Результат |\n"
                                     "|---|---|---|---|---|---|---|\n| cd-04 | тема | в работе | v1 | | | |\n", encoding="utf-8")
        (d / "published.txt").write_text("# пояснение\nab-01\n", encoding="utf-8")
        (d / "mem").mkdir()
        (d / "mem" / "cd-04-status.md").write_text("---\nname: x\n---\n\nтекст\n", encoding="utf-8")
        assert change("Brand A", "cd-04", "сдан", root=d, memory=d / "mem", names=N)["status"] == "сдан"
        assert "**Статус в reels.md:** сдан" in (d / "mem" / "cd-04-status.md").read_text().split("---")[2]
        assert change("Brand A", "cd-04", "опубликован", "21.11", "https://x", "5 000 просмотров", d, d / "mem", N)["status"] == "опубликован"
        r = rows((d / "reels.md").read_text())["cd-04"]
        assert (r["Опубликован"], r["Ссылка"], r["Результат"]) == ("21.11", "https://x", "5 000 просмотров"), r
        assert "cd-04 21.11 https://x 5 000 просмотров" in (d / "published.txt").read_text()
        change("Brand A", "cd-04", "сдан", root=d, memory=d / "mem", names=N)  # отмена публикации
        assert "cd-04" not in (d / "published.txt").read_text() and "ab-01" in (d / "published.txt").read_text()
        assert (d / "mem" / "cd-04-status.md").read_text().count("**Статус в reels.md:**") == 1
        change("Brand B", "ab-12-ce", "отложен", root=d, memory=d / "mem", names=N)  # новой строки нет -> раздел бренда
        r = rows((d / "reels.md").read_text())["ab-12"]
        assert (r["Статус"], r["brand"]) == ("отложен", "Brand B"), r
        change("Brand A", "cd-09", "сдан", root=d, memory=d / "mem", names=N)  # новая строка в таблице Brand A
        assert rows((d / "reels.md").read_text())["cd-09"]["brand"] == "Brand A"
        drop("cd-09", d, N)
        assert "cd-09" not in rows((d / "reels.md").read_text()) and "cd-04" in rows((d / "reels.md").read_text())
    print("tracker: ok")
