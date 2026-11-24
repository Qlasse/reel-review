#!/usr/bin/env python3
"""Как устроен проект: <корень>/.reel-review.json (файла может не быть — тогда всё по умолчанию).

Корень проекта — переменная REEL_REVIEW_ROOT (её ставит server.py --root и передаёт Claude) или текущая папка.
Ролик живёт в папке бренда: <бренд>/edit/<id>/ (данные), <бренд>/out/ (мастера). Брендом считается папка с файлом
brand_marker (у плагина it-reelsmaker — it-reelsmaker.json); если такой файл лежит в самом корне, бренд один — корень.
Remotion-проект бренда — remotion_dir из того же it-reelsmaker.json, иначе ключ "remotion" здесь.

Всё, чего у проекта нет (таблица статусов, проверка, сведение звуков), страница прячет сама. Пример — в README плагина.
"""
import json, os
from pathlib import Path

HERE = Path(__file__).resolve().parent  # скрипты плагина: сюда ничего не пишем, при обновлении плагина папка меняется
ROOT = Path(os.environ.get("REEL_REVIEW_ROOT") or os.getcwd()).resolve()
FILE = ROOT / ".reel-review.json"
DEFAULTS = {
    "brand_marker": "it-reelsmaker.json",
    "remotion": "reels",
    "tracker": "",            # таблица статусов роликов (Markdown, колонки «Ролик | … | Статус | …»); пусто — без статусов
    "published": "",          # файл отметок публикации, строка «<id> <дата> <ссылка> <результат>»
    "cleanup": "",            # модуль с sync_table(root, today): переносит отметки публикации в таблицу
    "check": "",              # проверка мастера: python3 <check> <бренд> <id> --master out/<файл>, строки «ОШИБКА|ПРОВЕРИТЬ|ок …»
    "sfx": "",                # сведение звуков поверх мастера голоса: python3 <sfx> <видео> <sfx.json> -o <выход>
    "rules": ["CLAUDE.md"],   # файлы правил: окно «Правила» и правила из правок (шаблоны glob от корня)
    "rule_sections": {"rules.md": "Rules from revisions"},  # раздел, куда дописывать правило (по имени файла)
    "grid": [],               # сетка поверх кадра (G): [{"rect": [x, y, w, h], "color": "lime", "fill": false}], кадр 1080×1920
    "plugin_dirs": [],        # плагины для Claude со страницы (it-reelsmaker из приложения Claude находится сам)
    "port": 8765,
    "settings": {},           # то, что меняет окно «Настройки»: модель, права, выключение без работы…
}
_cache = {"mtime": None, "data": dict(DEFAULTS)}


def load():
    """Настройки проекта с умолчаниями; файл перечитывается, только если изменился."""
    m = FILE.stat().st_mtime if FILE.is_file() else 0
    if m != _cache["mtime"]:
        data = dict(DEFAULTS)
        if m:
            data.update(json.loads(FILE.read_text(encoding="utf-8")))
        _cache.update(mtime=m, data=data)
    return _cache["data"]


def get(key):
    return load()[key]


def path(key):
    """Путь из настроек от корня проекта; None — если не задан или файла нет."""
    v = get(key)
    p = (ROOT / v) if v else None
    return p if p and p.exists() else None


def save_settings(settings):
    """Записать раздел settings, не трогая остальное (файл правят и руками)."""
    raw = json.loads(FILE.read_text(encoding="utf-8")) if FILE.is_file() else {}
    raw["settings"] = settings
    tmp = FILE.with_name(FILE.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, FILE)


def features():
    """Что умеет страница в этом проекте."""
    return {"status": bool(get("tracker")), "check": bool(path("check")), "sfx": bool(path("sfx")), "grid": get("grid")}
