#!/usr/bin/env python3
"""Задачи страницы пометок: Claude без чата (`claude -p`), предпросмотр, настройки.

Одна тяжёлая задача за раз (рендер, предпросмотр, проверка, Claude): пока работает одна, остальные ждут. Лента работы Claude — <бренд>/edit/<id>/claude-feed.jsonl (коротко: текст, инструменты, итог);
полный разговор — сессия Claude Code, её номер в notes.json («session»): `claude --resume <номер>` в Терминале.
Один ролик — одна сессия: каждый запуск со страницы продолжает её.

    python3 jobs.py        # самопроверка разбора событий (подсказки Claude проверяет test_review.py)
    python3 jobs.py key    # ключ из буфера обмена -> Связка ключей macOS и проверка, что Claude его принимает
"""
import json, os, re, shutil, signal, subprocess, threading, time
from pathlib import Path

import config as C
import notes as N
from config import ROOT

SCRIPTS = C.HERE  # Claude зовёт notes.py и variants.py плагина по полному пути
DEFAULTS = {"model": "", "effort": "", "permission_mode": "auto", "max_budget_usd": 0, "preview_scale": 0.5,
            "extra_prompt": "", "idle_minutes": 30}
CHOICES = {"effort": ["", "low", "medium", "high", "xhigh", "max"],
           "permission_mode": ["auto", "acceptEdits", "bypassPermissions"]}
SLOT = threading.Lock()
STATE = {"job": None}


class Busy(Exception):
    pass


# ---------- настройки ----------
def settings():
    """Настройки страницы — раздел settings в .reel-review.json проекта (в папку плагина не пишем)."""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (C.get("settings") or {}).items() if k in DEFAULTS})
    return s


def save_settings(new):
    s = settings()
    for k, v in new.items():
        if k not in DEFAULTS:
            continue
        if k in CHOICES and v not in CHOICES[k]:
            raise ValueError(f"{k}: одно из {CHOICES[k]}")
        if k == "model" and not re.match(r"^[a-z0-9.\-\[\]]{0,60}$", str(v)):
            raise ValueError("модель: имя вида opus, sonnet, claude-opus-5-5")
        if k in ("max_budget_usd", "preview_scale", "idle_minutes"):
            v = float(v or 0)
            if k == "preview_scale" and not 0.2 <= v <= 1:
                raise ValueError("размер клипа предпросмотра: от 0.2 до 1")
            if k == "idle_minutes" and not 0 <= v <= 24 * 60:
                raise ValueError("выключение без работы: от 0 (никогда) до 1440 минут")
        s[k] = v
    C.save_settings(s)
    return s


KEYCHAIN = ("reel-review", "claude-token")  # служба и учётная запись ключа в Связке ключей macOS
SETUP = (f"claude setup-token, скопировать ключ целиком (обе строки), затем python3 \"{Path(__file__).resolve()}\" key")
KEY = re.compile(r"sk-ant-oat\d\d-[A-Za-z0-9_-]{60,}")


def save_key():
    """Ключ из буфера обмена — в Связку ключей. setup-token печатает ключ на двух строках, и вставка в запрос
    `security … -w` обрывается на переносе (в Связке оказывается половина ключа, Claude отвечает 401). Поэтому переносы
    и пробелы убираем здесь, проверяем вид ключа и пробуем его одним коротким запуском. Ключ не печатается."""
    raw = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
    key = re.sub(r"\s+", "", raw)
    if not KEY.fullmatch(key):
        return "В буфере обмена нет ключа вида sk-ant-oat01-…: скопируйте его из вывода claude setup-token целиком."
    # команда — через stdin `security -i`, а не аргументом: аргументы процесса видны любой программе на Mac (ps)
    r = subprocess.run(["/usr/bin/security", "-i"], input=f"add-generic-password -U -s {KEYCHAIN[0]} -a {KEYCHAIN[1]} -w {key}\n",
                       capture_output=True, text=True)
    if oauth_token_from_keychain() != key:
        return "Связка ключей не приняла запись: " + (r.stderr or r.stdout).strip()[-300:]
    env = clean_env()
    t = subprocess.run([N.tool("claude"), "-p", "Ответь одним словом: работает", "--model", "haiku", "--max-turns", "1",
                        "--output-format", "json"], capture_output=True, text=True, timeout=120, env=env, cwd=Path.home())
    try:
        ok = not json.loads(t.stdout).get("is_error")
    except ValueError:
        ok = False
    return (f"Ключ сохранён ({len(key)} знаков) и Claude его принял. Буфер обмена лучше очистить." if ok else
            f"Ключ сохранён ({len(key)} знаков), но Claude его не принял: создайте новый (claude setup-token) и повторите.")


def oauth_token_from_keychain():
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN[0], "-a", KEYCHAIN[1], "-w"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def oauth_token():
    """Долгоживущий ключ подписки (`claude setup-token`): переменная CLAUDE_CODE_OAUTH_TOKEN сервера или Связка ключей.

    Обычный вход `claude auth login` держит короткий ключ и продлевает его; продление иногда срывается, и Claude со
    страницы падает с «OAuth session expired». С долгоживущим ключом этого нет. Ключ никуда не пишется и не отдаётся
    странице — только в окружение запускаемого `claude`.
    """
    t = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if t or not Path("/usr/bin/security").exists():
        return t
    return oauth_token_from_keychain()


def clean_env():
    """Окружение без переменных сессии Claude, из которой мог быть запущен сервер: `claude -p` работает от своего входа
    (долгоживущий ключ, если он есть, иначе `claude auth login`)."""
    token = oauth_token()
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k != "ANTHROPIC_BASE_URL"}
    path = env.get("PATH", "")
    env["PATH"] = ":".join([p for p in ("/opt/homebrew/bin", "/usr/local/bin") if p not in path.split(":")] + [path])
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def auth():
    """Как войдёт Claude со страницы. С ключом `auth status` его не проверяет: годен ли он — покажет первый запуск."""
    try:
        r = subprocess.run([N.tool("claude"), "auth", "status"], capture_output=True, text=True, timeout=20, env=clean_env())
        j = json.loads(r.stdout)
        return {"ok": bool(j.get("loggedIn")), "method": j.get("authMethod", ""), "token": j.get("authMethod") == "oauth_token"}
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "method": "", "token": False, "error": str(e)}


def plugin_dir():
    """Плагин it-reelsmaker из приложения Claude (не из marketplace): `claude -p` его без --plugin-dir не видит."""
    base = Path.home() / "Library/Application Support/Claude/local-agent-mode-sessions"
    for d in sorted(base.glob("*/*/rpm/*/skills/it-reelsmaker"), key=lambda p: -p.stat().st_mtime):
        return str(d.parent.parent)
    return None


def plugin_dirs():
    """Плагины для Claude со страницы: it-reelsmaker (если найден) и plugin_dirs из настроек проекта."""
    ds = [plugin_dir()] + [str((ROOT / d).resolve()) for d in C.get("plugin_dirs")]
    return [d for d in dict.fromkeys(ds) if d and Path(d).is_dir()]


# ---------- очередь ----------
def current():
    j = STATE["job"]
    return j and {k: j[k] for k in ("kind", "brand", "id", "title", "started")}


def claim(kind, brand, rid, title):
    if not SLOT.acquire(blocking=False):
        j = STATE["job"] or {}
        raise Busy(f"Занято: {j.get('title', 'задача')} ({j.get('id', '')}). Дождитесь конца или остановите.")
    STATE["job"] = {"kind": kind, "brand": brand, "id": rid, "title": title, "started": time.time(), "proc": None}


def release():
    STATE["job"] = None
    SLOT.release()


SAME = 0.002  # доля пикселей, заметно (> 24 уровней) отличающихся от мастера: меньше — кадр в коде как в мастере


def changed_share(a, b):
    """Доля пикселей, которые заметно отличаются (сжатие мастера даёт шум ~0.5 уровня, правка — проценты)."""
    from PIL import Image, ImageChops
    A, B = (Image.open(x).convert("L").resize((270, 480)) for x in (a, b))
    h = ImageChops.difference(A, B).histogram()
    return sum(h[24:]) / sum(h)


def preview(brand, rid, frame=None, rng=None, master=None):
    """Кадр или отрезок из текущего кода -> путь от корня проекта; у кадра ещё same — совпадает ли с мастером."""
    claim("preview", brand, rid, "предпросмотр")
    try:
        got = N.render_preview(brand, rid, [tuple(rng)] if rng else [int(frame)], settings()["preview_scale"])
        out = {"path": str(got[0].relative_to(ROOT)), "clip": bool(rng), "at": time.time()}
        m = master and (ROOT / master).resolve()
        if not rng and m and m.is_relative_to(N.bdir(brand) / "out") and m.is_file():
            ref = got[0].with_name(got[0].stem + "-master.png")
            N.draw_frame(m, int(frame), 0, [], ref)
            out["same"] = changed_share(ref, got[0]) < SAME
        return out
    finally:
        release()


# ---------- Claude ----------
def feed_path(brand, rid):
    return N.bdir(brand) / "edit" / rid / "claude-feed.jsonl"


def feed_add(brand, rid, item):
    item = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **item}
    with open(feed_path(brand, rid), "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def feed(brand, rid, since=0):
    p = feed_path(brand, rid)
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    return {"items": [json.loads(l) for l in lines[since:]], "total": len(lines)}


def what(inp):
    for k in ("command", "file_path", "pattern", "skill", "description", "url", "prompt"):
        if inp.get(k):
            return str(inp[k])[:220]
    return ""


def brief(ev):
    """Событие stream-json -> пункты ленты."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        return [{"t": "init", "session": ev.get("session_id"), "model": ev.get("model"), "mode": ev.get("permissionMode")}]
    if t == "assistant":
        out = []
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "text" and c["text"].strip():
                out.append({"t": "text", "text": c["text"][:4000]})
            elif c.get("type") == "tool_use":
                out.append({"t": "tool", "name": c["name"], "what": what(c.get("input") or {})})
        return out
    if t == "result":
        auth_err = ev.get("api_error_status") == 401 or "authenticate" in (ev.get("result") or "").lower()
        hint = f"Claude не вошёл или ключ не годится. Один раз в Терминале: {SETUP}." if auth_err else ""
        return [{"t": "done", "error": bool(ev.get("is_error")), "text": (ev.get("result") or "")[:4000], "hint": hint,
                 "cost": ev.get("total_cost_usd"), "secs": round((ev.get("duration_ms") or 0) / 1000),
                 "turns": ev.get("num_turns"), "denied": len(ev.get("permission_denials") or [])}]
    return []


def rule_candidates(brand, rid):
    """Пометки, из которых может выйти правило: принятые или возвращённые, ещё без предложения."""
    return [n["id"] for n in N.read(brand, rid)["notes"]
            if (n.get("status") == "accepted" or n.get("returned")) and not n.get("rule")]


def notes_cmd(brand, rid, script="notes.py"):
    return f"python3 \"{SCRIPTS / script}\" \"{brand}\" {rid}"


def rules_howto(brand, rid):
    files = "; ".join(f"{v} — {k}" for k, v in N.rule_files().items()) or "только CLAUDE.md"
    return (f"Предложение записывай командой {notes_cmd(brand, rid)} rule <n> \"<файл>\" \"<текст>\" "
            "[--section \"<раздел>\"], а если урок частный и правилом не станет — rule <n> none \"почему\". Файлы правил: "
            f"{files}. Правило бренда — в файл правил бренда, одной строкой «- ГГГГ-ММ-ДД: правило (причина, id ролика)»; "
            "общее для всех роликов — в CLAUDE.md, пунктом в стиле его разделов; приём монтажа — в каталог приёмов, блоком "
            "в его формате, --section — нужный раздел каталога. Сначала прочитай файл и не дублируй записанное; одна "
            "пометка — не больше одного правила; правило — общее, не пересказ пометки. В файлы правил сам не пиши: "
            "пользователь подтвердит предложение на странице.")


def prompt_for(brand, rid, mode, text, ctx, s):
    head = (f"Ролик {rid}, бренд «{brand}». Ты запущен со страницы пометок (плагин reel-review) без чата: ход работы виден "
            "в ленте страницы. Пиши по-русски.")
    waiting = sum(n.get("status") in N.WAITING for n in N.read(brand, rid)["notes"])
    if mode == "notes":
        body = (f"Внеси ждущие пометки ({waiting}) по скиллу reel-notes. Полный рендер не делай: после правок покажи каждую "
                f"пометку предпросмотром из кода — {notes_cmd(brand, rid)} preview <n> — посмотри "
                "картинку и ответь на пометку (set done / question / wontfix). Наброски графики (kind draw, фигуры с color) — "
                "это эскиз: сделай по нему настоящую графику в стиле бренда по его правилам и правилам проекта (CLAUDE.md). "
                "Если правка меняет нарезку — перенарежь и обнови public/, но мастер собирай только по команде «Собери версию». "
                "Если пометку уже возвращали (returned в notes.py list) — после исправления предложи правило из этой правки. "
                + rules_howto(brand, rid))
    elif mode == "build":
        check = f", проверка python3 {C.get('check')}" if C.path("check") else ""
        table = f" Обнови «Версия» в {C.get('tracker')} и память ролика." if C.get("tracker") else ""
        body = ("Собери новую версию мастера по правилам проекта (CLAUDE.md): полный рендер (или патч, если правки мелкие и "
                f"тайминг не менялся), мастер звука, звуки{check}. Для пометок, где «после» сделано предпросмотром "
                f"(after_src = preview), сделай {notes_cmd(brand, rid)} frame <n> after с нового мастера.{table} "
                "Мастер положи на место прежнего в out/, чтобы страница его подхватила.")
    elif mode == "rules":
        ids = rule_candidates(brand, rid)
        body = (f"Правила из правок. Пометки {', '.join(ids)} приняты пользователем или возвращались на доработку — "
                f"прочитай их ({notes_cmd(brand, rid)} list --all) и для каждой реши, есть ли общий урок на будущие ролики. "
                + rules_howto(brand, rid))
    else:
        where = ""
        if ctx.get("frame") is not None:
            where = f" Пользователь смотрит мастер {ctx.get('file', '')} на кадре {ctx['frame']}"
            where += f", выделен отрезок кадров {ctx['range'][0]}–{ctx['range'][1]}." if ctx.get("range") else "."
        if mode == "variants":
            opt = f" --frame {ctx['frame']}" if ctx.get("frame") is not None else ""
            opt += f" --range {ctx['range'][0]}-{ctx['range'][1]}" if ctx.get("range") else ""
            v = notes_cmd(brand, rid, "variants.py")
            rem, b = N.remotion(brand), N.bdir(brand)
            rel = lambda x: str(x.relative_to(ROOT)) if x.is_relative_to(ROOT) else str(x)
            body = (f"Сделай три заметно разных варианта: {text}.{where} Порядок: {v} begin \"<задание>\"{opt} — получишь id; "
                    f"для каждого варианта a, b, c: внеси правку, {v} save <id> <a|b|c> \"чем вариант отличается, одной строкой\", "
                    f"затем {v} base <id> — вернуть исходное, и следующий вариант. Правь только файлы ролика: "
                    f"{rel(rem / 'src')}/{N.comp_id(rid)}*.tsx, {rel(rem / 'src' / 'plans')}/{rid}*.json, {rel(b / 'edit' / rid)}/*.json — общий кит "
                    "вариантами не откатывается. Варианты различаются подходом, а не мелочью; все — в рамках правил бренда и "
                    "проекта. Полный рендер не делай. В конце код — исходный: пользователь выберет вариант на странице.")
        else:
            body = (f"Задание со страницы: {text}{where} Сделай и покажи результат предпросмотром ({notes_cmd(brand, rid)} "
                    "preview, без полного рендера). Ответ — коротко, по делу.")
    extra = f"\n\n{s['extra_prompt']}" if s.get("extra_prompt") else ""
    return f"{head}\n\n{body}{extra}"


def start_claude(brand, rid, mode, text="", ctx=None):
    titles = {"notes": "Claude: пометки", "build": "Claude: сборка версии", "ask": "Claude: задание",
              "variants": "Claude: три варианта", "rules": "Claude: правила из правок"}
    if mode not in titles:
        raise ValueError("режим: " + ", ".join(titles))
    if mode in ("ask", "variants") and not text.strip():
        raise ValueError("напишите задание")
    if mode == "rules" and not rule_candidates(brand, rid):
        raise ValueError("нет принятых или возвращённых пометок без правила")
    title = titles[mode]
    claim("claude", brand, rid, title)
    try:
        s = settings()
        sid = N.read(brand, rid).get("session")
        prompt = prompt_for(brand, rid, mode, text, ctx or {}, s)
        cmd = [N.tool("claude"), "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", s["permission_mode"], "-n", f"{rid} · страница пометок"]
        if sid:
            cmd += ["--resume", sid]
        if s["model"]:
            cmd += ["--model", s["model"]]
        if s["effort"]:
            cmd += ["--effort", s["effort"]]
        if s["max_budget_usd"]:
            cmd += ["--max-budget-usd", str(s["max_budget_usd"])]
        for pd in plugin_dirs():
            cmd += ["--plugin-dir", pd]
        feed_add(brand, rid, {"t": "start", "mode": mode, "text": text if mode in ("ask", "variants") else title,
                              "resume": bool(sid)})
        env = clean_env()
        env["REEL_REVIEW_ROOT"] = str(ROOT)  # notes.py и variants.py, которые зовёт Claude, работают в том же проекте
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, env=env)
    except Exception:
        release()
        raise
    STATE["job"]["proc"] = proc
    threading.Thread(target=pump, args=(brand, rid, proc, sid), daemon=True).start()
    return current()


def pump(brand, rid, proc, sid):
    # ponytail: состояние задачи — в памяти сервера; перезапуск сервера во время работы Claude теряет ленту до конца запуска
    got_result = False
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            for item in brief(ev):
                if item["t"] == "init" and item["session"] and item["session"] != sid:
                    sid = item["session"]
                    N.update(brand, rid, lambda d: d.update(session=sid))
                got_result |= item["t"] == "done"
                feed_add(brand, rid, item)
        proc.wait()
        if not got_result:
            err = (proc.stderr.read() or "").strip().splitlines()[-3:]
            if any("No conversation found" in l for l in err):
                N.update(brand, rid, lambda d: d.pop("session", None))
                err.append("Сессия не найдена — следующий запуск начнёт новую.")
            stopped = proc.returncode in (-signal.SIGTERM, -signal.SIGINT)
            feed_add(brand, rid, {"t": "done", "error": not stopped, "stopped": stopped,
                                  "text": "Остановлено со страницы." if stopped else " ".join(err) or f"код {proc.returncode}"})
    finally:
        release()


def stop():
    j = STATE["job"]
    if not j or j["kind"] != "claude" or not j["proc"]:
        return False
    try:
        os.killpg(j["proc"].pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return True


def reset_session(brand, rid):
    j = STATE["job"]
    if j and j["id"] == rid:
        raise Busy("Claude сейчас работает с этим роликом")
    N.update(brand, rid, lambda d: d.pop("session", None))
    feed_add(brand, rid, {"t": "reset"})


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["key"]:
        print(save_key())
        sys.exit(0)
    assert KEY.fullmatch("sk-ant-oat01-" + "A" * 80) and not KEY.fullmatch("sk-ant-oat01-" + "A" * 40)
    init = {"type": "system", "subtype": "init", "session_id": "abc", "model": "m", "permissionMode": "auto"}
    assert brief(init) == [{"t": "init", "session": "abc", "model": "m", "mode": "auto"}]
    ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Готово"},
                                                       {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}
    assert [i["t"] for i in brief(ev)] == ["text", "tool"] and brief(ev)[1]["what"] == "ls"
    res = brief({"type": "result", "is_error": True, "api_error_status": 401, "duration_ms": 9688})[0]
    assert res["error"] and "setup-token" in res["hint"] and res["secs"] == 10
    res = brief({"type": "result", "is_error": True, "result": "Failed to authenticate: OAuth session expired"})[0]
    assert "setup-token" in res["hint"]
    assert N.comp_id("ab-12-ce") == "ReelAb12Ce" and N.comp_id("abc-01") == "ReelAbc01"
    os.environ["CLAUDECODE"] = "1"
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "проверка"
    env = clean_env()
    assert "CLAUDECODE" not in env and "/opt/homebrew/bin" in env["PATH"] and env["CLAUDE_CODE_OAUTH_TOKEN"] == "проверка"
    claim("preview", "B", "x-1", "t")
    try:
        claim("claude", "B", "x-2", "t")
        raise AssertionError("вторая задача не должна стартовать")
    except Busy:
        pass
    release()
    print("jobs: ok")
