#!/usr/bin/env python3
"""Самопроверка плагина на временном проекте (настоящие файлы не трогает).

    python3 test_review.py
"""
import json, os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent

CLAUDE_MD = """# Проект

## Правила из правок

- **Пример.** старое правило.

## Нарезка

- текст
"""
RULES_MD = "# Brand rules\n\n## Basics\n\n- x\n\n## Rules from revisions\n\n- 2026-11-20: старое правило (b-01)\n"
CATALOG_MD = "# Каталог\n\n## Приняты\n\n### Приём А\n- Что: а\n\n## На проверке\n\n### Приём Б\n- Что: б\n\n## Отклонены\n\n- **В** — нет\n"


def fake_project(d, outside):
    """Корень d: бренд «Brand» (it-reelsmaker.json, Remotion-проект вне корня — outside), правила, настройки."""
    (d / "CLAUDE.md").write_text(CLAUDE_MD)
    b = d / "Brand"
    (b / "brands" / "brand").mkdir(parents=True)
    (b / "brands" / "brand" / "rules.md").write_text(RULES_MD)
    (b / "it-reelsmaker.json").write_text(json.dumps({"remotion_dir": str(outside)}))
    (d / "docs").mkdir(parents=True)
    (d / "docs" / "catalog.md").write_text(CATALOG_MD)
    (d / ".reel-review.json").write_text(json.dumps({
        "rules": ["CLAUDE.md", "*/brands/*/rules.md", "docs/catalog.md"],
        "rule_sections": {"CLAUDE.md": "Правила из правок", "rules.md": "Rules from revisions", "catalog.md": "Приняты"},
        "tracker": "reels.md", "published": "published.txt"}))
    e = b / "edit" / "zz-01"
    e.mkdir(parents=True)
    words = [{"text": "Apple", "start": 0.02, "end": 0.3}, {"text": "выпустила", "start": 0.3, "end": 0.8}]
    (e / "captions.json").write_text(json.dumps({"duration": 10, "segments": [], "words": words}, indent=1))
    ins = {"id": "b01", "kind": "broll", "start": 3.0, "dur": 2.0}
    (e / "visual_plan.json").write_text(json.dumps({"inserts": [ins]}, indent=1))
    (e / "reelkit-props.json").write_text(json.dumps({"captions": {"words": words}, "inserts": [{**ins, "src_in": 0.5}]}, indent=1))
    (e / "sfx.json").write_text(json.dumps({"below_voice_db": 12, "sounds": [{"file": "a.wav", "at": 1.0, "below_db": 8}]}, indent=1))
    (e / "notes.json").write_text('{"notes": []}')
    (b / "brands" / "brand" / "brand.json").write_text(json.dumps({"colors": {"primary": "#ABCDEF", "accent": '#fff" onmouseover="x'}}))
    (outside / "src" / "plans").mkdir(parents=True)
    (outside / "src" / "ReelZz01.tsx").write_text("const X = 1;\n")
    # копия вставок в плане композиции — вне корня: правка не должна падать на пути
    (outside / "src" / "plans" / "zz-01.json").write_text(json.dumps({"inserts": [{**ins, "src_in": 0.5}]}, indent=1))


def test_config(N, C):
    assert list(N.brands()) == ["Brand"] and N.remotion("Brand") == OUT
    assert set(N.rule_files()) == {"CLAUDE.md", "Brand/brands/brand/rules.md", "docs/catalog.md"}
    assert C.features() == {"status": True, "check": False, "sfx": False, "grid": []}
    assert N.owner(N.bdir("Brand"), "zz-01-v3-voice.mp4") == "zz-01"
    C.save_settings({"model": "sonnet"})
    raw = json.loads(C.FILE.read_text())
    assert raw["settings"] == {"model": "sonnet"} and raw["tracker"] == "reels.md"  # остальное не тронуто


def test_rules(N, d):
    N.insert_rule("Brand/brands/brand/rules.md", "- 2026-11-24: ТЕСТ (zz-01)")
    assert (d / "Brand/brands/brand/rules.md").read_text().splitlines()[-1] == "- 2026-11-24: ТЕСТ (zz-01)"
    N.insert_rule("CLAUDE.md", "- **ТЕСТ.** правило")
    c = (d / "CLAUDE.md").read_text().splitlines()
    i = c.index("- **ТЕСТ.** правило")
    assert c[i + 1] == "" and c[i + 2] == "## Нарезка", c[i:i + 3]
    N.insert_rule("docs/catalog.md", "### Т2\n- Что: y")
    k = (d / "docs/catalog.md").read_text().splitlines()
    j = k.index("### Т2")
    assert k[j - 1] == "" and k[j + 2] == "" and k[j + 3].startswith("## На проверке"), k[j - 1:j + 4]
    try:
        N.insert_rule("tools/x.md", "x")
        raise AssertionError("чужой файл")
    except ValueError:
        pass


def test_edits(tracks, d):
    e = d / "Brand/edit/zz-01"
    r = tracks.set_word("Brand", "zz-01", 1, 0.3, "выпустила", " выпустилa ")
    assert len(r["changed"]) == 2
    assert json.loads((e / "reelkit-props.json").read_text())["captions"]["words"][1]["text"] == "выпустилa"
    assert json.loads((e / "caption_edits.json").read_text())[0]["old"] == "выпустила"
    tracks.set_word("Brand", "zz-01", 1, 0.3, "выпустилa", "выпустила")  # вернули как было
    assert not (e / "caption_edits.json").exists()
    try:
        tracks.set_word("Brand", "zz-01", 1, 0.3, "чужое", "x")
        raise AssertionError("должно не найти")
    except ValueError:
        pass
    tracks.set_word_time("Brand", "zz-01", 1, 0.3, "выпустила", 0.34, 0.9)  # сдвиг и обрезка слова — во всех копиях
    assert json.loads((e / "reelkit-props.json").read_text())["captions"]["words"][1] == \
        {"text": "выпустила", "start": 0.34, "end": 0.9}
    win = json.loads((e / "caption_edits.json").read_text())[-1]
    assert (win["start"], win["end"], win["kind"]) == (0.3, 0.9, "word_time")  # окно — старое и новое место
    tracks.set_insert("Brand", "zz-01", "b01", 3.0, "move", 4.0, 6.0)
    tracks.set_insert("Brand", "zz-01", "b01", 4.0, "in", 3.6, 6.0)  # начало раньше — src_in тоже раньше
    props = json.loads((e / "reelkit-props.json").read_text())["inserts"][0]
    plan = json.loads((e / "visual_plan.json").read_text())["inserts"][0]
    assert (props["start"], props["dur"], props["src_in"]) == (3.6, 2.4, 0.1) and (plan["start"], plan["dur"]) == (3.6, 2.4)
    assert "src_in" not in plan  # в копии без src_in его не появляется
    try:
        tracks.set_insert("Brand", "zz-01", "b01", 3.6, "in", 3.0, 6.0)  # раньше начала исходника
        raise AssertionError("src_in < 0")
    except ValueError:
        pass
    tracks.set_insert("Brand", "zz-01", "b01", 3.6, "out", 3.6, 5.0)
    assert json.loads((e / "reelkit-props.json").read_text())["inserts"][0]["dur"] == 1.4
    (e / "caption_edits.json").unlink()
    tracks.set_sound("Brand", "zz-01", 0, "a.wav", start=0.2, dur=0.5)
    s = json.loads((e / "sfx.json").read_text())["sounds"][0]
    assert (s["start"], s["dur"]) == (0.2, 0.5)
    tracks.set_sound("Brand", "zz-01", 0, "a.wav", start=0, dur=0)  # 0 — обрезки нет, ключей тоже
    assert {"start", "dur"}.isdisjoint(json.loads((e / "sfx.json").read_text())["sounds"][0])
    tracks.set_sound("Brand", "zz-01", 0, "a.wav", at=2.5, below_db=6)
    s = json.loads((e / "sfx.json").read_text())["sounds"][0]
    assert (s["at"], s["below_db"]) == (2.5, 6)
    assert '"below_db": 6\n' in (e / "sfx.json").read_text()  # 6, а не 6.0
    try:
        tracks.set_sound("Brand", "zz-01", 0, "b.wav", at=1)
        raise AssertionError("чужой файл звука")
    except ValueError:
        pass


def test_variants(N, variants, d):
    def fake_preview(brand, rid, items, scale=0.5):
        out = N.bdir(brand) / "edit" / rid / "preview"
        out.mkdir(exist_ok=True)
        fs = [out / (f"f{i[0]}-{i[1]}.mp4" if isinstance(i, tuple) else f"f{i}.png") for i in items]
        for f in fs:
            f.write_bytes(b"x")
        return fs
    N.render_preview = fake_preview
    calls = []
    def png_preview(*a, **k):
        from PIL import Image
        calls.append(a[2])
        fs = fake_preview(*a, **k)
        for f in fs:
            if f.suffix == ".png":
                Image.new("RGB", (54, 96)).save(f)
        return fs
    N.render_preview = png_preview
    (N.bdir("Brand") / "edit/zz-01/notes.json").write_text(json.dumps({"notes": [
        {"id": "p1", "frame": 10, "status": "new"}, {"id": "p2", "frame": 20, "range": [20, 50], "status": "new"}]}))
    sys.argv = ["notes.py", "Brand", "zz-01", "preview", "p1", "p2"]
    N.main()  # две пометки — один запуск предпросмотра на все кадры и отрезки
    assert calls == [[10, 20, (20, 50)]], calls
    got = {n["id"]: n for n in N.read("Brand", "zz-01")["notes"]}
    assert got["p1"]["after"].endswith("p1-after.png") and got["p2"]["after_clip"].endswith("p2-after.mp4")
    N.render_preview = fake_preview
    variants.TRASH = ["rm"]
    tsx = OUT / "src" / "ReelZz01.tsx"
    vid = variants.begin("Brand", "zz-01", "хук динамичнее", 40, [0, 90])
    base = variants.vdir("Brand", "zz-01", vid) / "base"
    assert (base / str(tsx).lstrip("/")).read_text() == "const X = 1;\n"  # Remotion вне корня — копия внутри снимка
    tsx.write_text("const X = 2;\n")
    extra = OUT / "src" / "plans" / "zz-01-extra.json"
    extra.write_text("{}")
    opt = variants.save("Brand", "zz-01", vid, "a", "вариант A")
    assert opt["image"].endswith("a.png") and opt["clip"].endswith("a.mp4")
    variants.restore("Brand", "zz-01", base, variants.manifest("Brand", "zz-01", vid)["files"]["base"])
    assert tsx.read_text() == "const X = 1;\n" and not extra.exists()  # лишний файл убран
    tsx.write_text("const X = 3;\n")
    variants.save("Brand", "zz-01", vid, "b", "вариант B")
    r = variants.apply("Brand", "zz-01", vid, "a")
    assert tsx.read_text() == "const X = 2;\n" and extra.exists() and r["chosen"] == "a"
    m = variants.listing("Brand", "zz-01")[0]
    assert m["chosen"] == "a" and set(m["options"]) == {"a", "b"}
    assert "Brand/edit/zz-01/notes.json" not in m["files"]["base"]


def test_security(server, variants):
    assert server.brand_colors() == {"Brand": ["#ABCDEF"]}  # цвет со вставкой разметки не попадает на страницу
    for vid in ("../..", "v1/../x", ""):
        try:
            variants.apply("Brand", "zz-01", vid, "a")
            raise AssertionError(f"id вариантов {vid!r} пропущен")
        except (ValueError, OSError):
            pass


def test_prompts(jobs):
    p = jobs.prompt_for("Brand", "zz-01", "ask", "плашку выше", {"frame": 300, "range": [280, 380], "file": "zz-01.mp4"},
                        {"extra_prompt": "Пиши кратко."})
    assert "кадре 300" in p and "280–380" in p and p.endswith("Пиши кратко.") and str(HERE / "notes.py") in p
    p = jobs.prompt_for("Brand", "zz-01", "variants", "хук динамичнее", {"frame": 40, "range": [0, 90]}, {})
    assert f'{HERE / "variants.py"}" "Brand" zz-01 begin' in p and "--frame 40 --range 0-90" in p
    assert f"{OUT / 'src'}/ReelZz01*.tsx" in p and "Brand/edit/zz-01/*.json" in p
    p = jobs.prompt_for("Brand", "zz-01", "build", "", {}, {})
    assert "reels.md" in p and "проверка python3" not in p  # проверки в проекте нет — Claude о ней не говорим


def test_tracker(tracker, d):
    (d / "reels.md").write_text("# Ролики\n\n## Brand\n\n| Ролик | Тема | Статус | Версия | Опубликован | Ссылка | Результат |\n"
                                 "|---|---|---|---|---|---|---|\n| zz-01 | x | в работе | v1 | | | |\n")
    tracker.change("Brand", "zz-01", "опубликован", "", "https://x", "1 000")
    r = tracker.rows((d / "reels.md").read_text())["zz-01"]
    assert r["Статус"] == "опубликован" and r["Ссылка"] == "https://x" and r["Опубликован"]  # дата — сегодня
    assert (d / "published.txt").read_text().startswith("zz-01 ")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as o:
        d, OUT = Path(t).resolve(), Path(o).resolve()
        fake_project(d, OUT)
        os.environ["REEL_REVIEW_ROOT"] = str(d)  # до импорта: config читает корень при импорте
        sys.path.insert(0, str(HERE))
        import config as C
        import jobs, notes as N, tracker, tracks, variants
        test_config(N, C)
        test_rules(N, d)
        test_edits(tracks, d)
        test_prompts(jobs)
        test_tracker(tracker, d)
        test_variants(N, variants, d)
        import server
        test_security(server, variants)
    print("review: ok")
