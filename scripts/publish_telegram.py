#!/usr/bin/env python3
"""Публикует следующий готовый пост из очереди в Telegram-канал ЦКИ.
Запускается в GitHub Actions — репозиторий уже checked out в рабочую директорию.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

QUEUE_NAME = "Очередь_публикаций_ТГ.md"
CONTENT_NAME = "Контент_ТГ_ВК_ЦКИ.md"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = "@cifrovye_dokazatelstva"

BLOCKED_MARKERS = ("⛔ нужны данные", "⛔ нужно написать", "⛔ пишется по факту события")


def resolve_path(expected_name, directory="."):
    """Находит файл в директории, устойчиво к NFC/NFD различиям в юникод-именах
    (macOS обычно хранит составные кириллические имена в NFD, Linux-раннер — нет)."""
    if os.path.exists(expected_name):
        return expected_name
    target_nfc = unicodedata.normalize("NFC", expected_name)
    for entry in os.listdir(directory):
        if unicodedata.normalize("NFC", entry) == target_nfc:
            return os.path.join(directory, entry) if directory != "." else entry
    raise FileNotFoundError(expected_name)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def parse_queue_rows(text):
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or "---" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 5 or cells[0] == "№":
            continue
        rows.append(cells)  # [num, day, post, rubric, status]
    return rows


def find_target(rows):
    """(row, None) - первая строка со статусом ровно 'готов'.
    (None, row) - следующая незавершённая строка заблокирована / требует внимания.
    (None, None) - очередь пуста или полностью опубликована."""
    for row in rows:
        status = row[4]
        if status == "готов":
            return row, None
        if not status.startswith("опубликовано"):
            return None, row
    return None, None


def get_post_section(content, post_num):
    section_re = re.compile(
        r"^###\s*ПОСТ\s+" + str(post_num) + r"\b.*?\n(.*?)(?=\n###\s*ПОСТ|\n##\s|\Z)",
        re.S | re.M,
    )
    m = section_re.search(content)
    if not m:
        raise RuntimeError(f"Не найден раздел ПОСТ {post_num} в {CONTENT_NAME}")
    return m.group(1)


def extract_telegram_html(section):
    """Ищет вручную оформленную HTML-версию поста (готовую к отправке как есть,
    parse_mode=HTML). Возвращает None, если для этого поста её нет."""
    html_re = re.compile(
        r"\*\*Telegram HTML\*\*.*?\n\n(.*?)\n\n\*\*ВКонтакте\*\*", re.S
    )
    m = html_re.search(section)
    if not m:
        return None
    return m.group(1).strip()


def extract_telegram_text(section, post_num):
    """Механическая конвертация markdown-версии поста (fallback, если HTML-версии нет)."""
    tg_re = re.compile(r"\*\*Telegram\*\*\s*\n\n(.*?)\n\n\*\*(?:Telegram HTML|ВКонтакте)\*\*", re.S)
    m2 = tg_re.search(section)
    if not m2:
        raise RuntimeError(f"Не найден подраздел Telegram для ПОСТ {post_num}")
    raw = m2.group(1)

    lines = []
    for line in raw.split("\n"):
        line = line.rstrip()
        if line.startswith("> "):
            line = line[2:]
        elif line == ">":
            line = ""
        lines.append(line)
    text = "\n".join(lines).strip("\n")
    text = text.replace("**", "*")
    return text


def send_telegram(text, parse_mode="Markdown"):
    data = {"chat_id": CHAT_ID, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=urllib.parse.urlencode(data).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return False, body


def main():
    if not BOT_TOKEN:
        print("::error::TELEGRAM_BOT_TOKEN не задан (добавьте секрет в настройках репозитория)")
        sys.exit(1)

    queue_path = resolve_path(QUEUE_NAME)
    content_path = resolve_path(CONTENT_NAME)

    queue_text = read(queue_path)
    rows = parse_queue_rows(queue_text)
    target, blocker = find_target(rows)

    if target is None:
        if blocker:
            print("Публикация пропущена: следующий пункт очереди требует внимания.")
            print(f"  Пост: {blocker[2]}")
            print(f"  Статус: {blocker[4]}")
        else:
            print("Все пункты очереди уже опубликованы или очередь пуста.")
        sys.exit(0)

    post_num_match = re.search(r"Пост\s+(\d+)", target[2])
    if not post_num_match:
        print(f"::error::Не удалось определить номер поста в ячейке: {target[2]}")
        sys.exit(1)
    post_num = post_num_match.group(1)

    content_text = read(content_path)
    section = get_post_section(content_text, post_num)

    html_text = extract_telegram_html(section)
    if html_text is not None:
        print(f"Найдена готовая HTML-версия для ПОСТ {post_num}, отправляю как есть.")
        tg_text, parse_mode = html_text, "HTML"
    else:
        print(f"HTML-версии для ПОСТ {post_num} нет, использую автоконвертацию из Markdown.")
        tg_text, parse_mode = extract_telegram_text(section, post_num), "Markdown"

    ok, resp = send_telegram(tg_text, parse_mode=parse_mode)
    if not ok:
        err_desc = resp.get("description", "") if isinstance(resp, dict) else str(resp)
        if "parse" in err_desc.lower() or "entit" in err_desc.lower():
            print(f"{parse_mode} не распарсился ({err_desc}), повторяю без parse_mode…")
            ok, resp = send_telegram(tg_text, parse_mode=None)
        if not ok:
            print(f"::error::Публикация в Telegram не удалась: {resp}")
            sys.exit(1)

    print(f"Опубликовано: Пост {post_num} ({target[2]})")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_status = f"опубликовано {today}"
    new_lines = []
    replaced = False
    for line in queue_text.splitlines():
        s = line.strip()
        if not replaced and s.startswith("|") and "---" not in s:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 5 and cells[0] == target[0] and cells[2] == target[2] and cells[4] == "готов":
                cells[4] = new_status
                line = "| " + " | ".join(cells) + " |"
                replaced = True
        new_lines.append(line)

    if not replaced:
        print("::warning::Не удалось найти строку для обновления статуса в файле очереди")
    else:
        write(queue_path, "\n".join(new_lines) + "\n")
        print(f"Статус обновлён: {new_status}")


if __name__ == "__main__":
    main()
