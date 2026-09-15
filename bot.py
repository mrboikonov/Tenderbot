#!/usr/bin/env python3
"""
Tender Monitoring Bot — Кыргызстан
=====================================

Мониторит 5 площадок госзакупок КР, фильтрует по ключевым словам,
отслеживает смену статусов (Активен -> Отменен/Завершен), парсит
победителей/участников по завершенным тендерам, шлёт письма на Gmail
и хранит состояние в tenders_db.json (коммитится обратно в репозиторий
GitHub Actions отдельным workflow-шагом, см. .github/workflows/tender_bot.yml).

ВАЖНО (прочитать перед запуском в проде):
------------------------------------------
Реальные сайты (zakupki.gov.kg, tenders.kg, aris.kg, goszakupki.okmot.kg,
procurement.kg) периодически меняют разметку, некоторые требуют
авторизации или рендерят список через JavaScript. Функции-парсеры ниже
(`parse_<site>`) содержат рабочий каркас (запрос страницы, обработка
ошибок, извлечение полей через BeautifulSoup) с CSS-селекторами,
помеченными как TODO — их нужно один раз проверить и подправить под
актуальный HTML конкретной площадки (открыть страницу в браузере,
посмотреть DevTools -> Elements). Это единственная часть, которую
невозможно гарантированно "угадать" без доступа к живой авторизованной
сессии сайта.
"""

import os
import re
import json
import time
import smtplib
import logging
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tender-bot")

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------

def _split_env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]

KEYWORDS = [k.lower() for k in _split_env_list("KEYWORDS")]
TO_EMAILS = _split_env_list("TO_EMAILS")
MY_COMPANY_NAMES = [c.lower() for c in _split_env_list("MY_COMPANY_NAMES")]

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

DB_FILE = "tenders_db.json"
# sent_tenders.json оставлен для обратной совместимости с требованием (п.3
# из первого блока задачи) — фактически дедуп теперь встроен в tenders_db.json
# через поле "notified", но мы дополнительно пишем плоский список ID, если
# кто-то из внешних скриптов на него рассчитывает.
SENT_FILE = "sent_tenders.json"

REQUEST_TIMEOUT = 30

# Более "браузерный" набор заголовков — часть площадок (403 Forbidden)
# блокирует запросы, которые выглядят как автоматизированные (пустой
# Accept/Accept-Language, отсутствие Referer). Это не гарантирует обход
# блокировки (некоторые WAF блокируют сами IP облачных дата-центров,
# на которых работает GitHub Actions — см. README про этот случай),
# но для части сайтов помогает.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Домены, у которых сервер отдаёт битую цепочку SSL-сертификатов
# (сертификат есть, но проверка "unable to get local issuer certificate"
# проваливается — это ошибка настройки сервера площадки, а не наша).
# Для них отключаем проверку сертификата. Это ослабляет защиту от
# подмены сервера (MITM), поэтому используется только точечно и только
# для этих конкретных доменов.
INSECURE_DOMAINS = {"goszakupki.okmot.kg"}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = _build_session()

SOURCES = [
    "zakupki.gov.kg",
    "tenders.kg",
    "aris.kg",
    "goszakupki.okmot.kg",
    "procurement.kg",
]

STATUS_ACTIVE = "Активен"
STATUS_CANCELLED = "Отменен"
STATUS_COMPLETED = "Завершен"


# ---------------------------------------------------------------------------
# Работа с БД (tenders_db.json) и файлом sent_tenders.json
# ---------------------------------------------------------------------------

def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Не удалось прочитать %s (%s), начинаю с пустой базы", DB_FILE, e)
    return {}


def save_db(db: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2, sort_keys=True)


def save_sent_list(db: dict) -> None:
    """Плоский список уже уведомленных ссылок — для совместимости."""
    sent_ids = sorted([tid for tid, t in db.items() if t.get("notified")])
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(sent_ids, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Парсеры площадок
# ---------------------------------------------------------------------------
# Каждый parse_* возвращает список словарей вида:
# {
#   "id": "уникальный ID (например, URL тендера)",
#   "url": "...",
#   "title": "...",
#   "source": "имя площадки",
#   "status": STATUS_ACTIVE / STATUS_COMPLETED / STATUS_CANCELLED,
#   "customer": "заказчик" (опционально),
#   "deadline": "срок подачи" (опционально),
# }
#
# Если при подведении итогов доступны данные — дополнительно:
#   "winner": "название компании-победителя" или None
#   "participants": ["...", "..."]  или []

def _get_soup(url: str, referer: str | None = None) -> BeautifulSoup | None:
    domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    verify_ssl = domain not in INSECURE_DOMAINS

    headers = {}
    if referer:
        headers["Referer"] = referer

    try:
        resp = SESSION.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            verify=verify_ssl,
        )
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.error("Ошибка запроса %s: %s", url, e)
        return None


def parse_zakupki_gov_kg() -> list[dict]:
    """
    zakupki.gov.kg — портал на JSF/PrimeFaces (НЕ SPA, серверный рендер,
    проверено вручную 2026-09-15). Список объявлений:
      https://zakupki.gov.kg/popp/view/order/list.xhtml
    Список отменённых объявлений — готовая отдельная страница:
      https://zakupki.gov.kg/popp/view/order/rejectList.xhtml
    Каждая ячейка таблицы содержит "прилипший" текст лейбла перед
    значением (типичный PrimeFaces responsive-паттерн, лейбл рисуется
    через CSS, но остаётся в тексте DOM), например:
      "purchase NameПокупка аристона бойлер для водонагрева"
    Поэтому парсим по префиксам лейблов, а не по CSS-классам — это
    устойчивее к вёрстке.

    ВАЖНО: список без пагинации через JS отдаёт только первую страницу
    (обычно 10 последних объявлений). Для ежедневного мониторинга новых
    тендеров этого как правило достаточно, но если нужно больше —
    возможно, у списка есть параметр количества строк на странице
    (см. выпадающий список "Rows Per Page" на сайте) — стоит проверить
    вручную, поддерживает ли он передачу через URL.
    """
    base = "https://zakupki.gov.kg"
    list_url = f"{base}/popp/view/order/list.xhtml"
    reject_url = f"{base}/popp/view/order/rejectList.xhtml"

    LABEL_MAP = {
        "Name of company": "customer",
        "purchase Name": "title",
        "Bids Submission Deadline": "deadline",
        "Date published": "published",
    }

    def _extract_field(cell_text: str) -> tuple[str, str] | None:
        for label, key in LABEL_MAP.items():
            if cell_text.startswith(label):
                return key, cell_text[len(label):].strip()
        return None

    def _parse_list(url: str) -> dict[str, dict]:
        """Возвращает {tender_id: {...}} для страницы списка объявлений."""
        soup = _get_soup(url, referer=list_url)
        found: dict[str, dict] = {}
        if not soup:
            return found

        for link_tag in soup.select('a[href*="view.xhtml?id="]'):
            href = link_tag.get("href", "")
            m = re.search(r"id=(\d+)", href)
            if not m:
                continue
            tender_id = m.group(1)
            tender_url = href if href.startswith("http") else base + "/popp/view/order/" + href

            row = link_tag.find_parent("tr")
            data = {"customer": "", "title": "", "deadline": "", "published": ""}
            if row:
                for cell in row.select("td"):
                    field = _extract_field(cell.get_text(strip=True))
                    if field:
                        data[field[0]] = field[1]

            found[tender_id] = {
                "id": tender_url,
                "url": tender_url,
                "title": data["title"] or f"Тендер №{tender_id}",
                "source": "zakupki.gov.kg",
                "status": STATUS_ACTIVE,
                "customer": data["customer"],
                "deadline": data["deadline"],
            }
        return found

    active = _parse_list(list_url)
    cancelled = _parse_list(reject_url)

    for tender_id in cancelled:
        if tender_id in active:
            active[tender_id]["status"] = STATUS_CANCELLED
        else:
            cancelled[tender_id]["status"] = STATUS_CANCELLED
            active[tender_id] = cancelled[tender_id]

    return list(active.values())


def parse_tenders_kg() -> list[dict]:
    """
    tenders.kg — обратите внимание: публичный список часто требует
    авторизации (мы проверили: страница без сессии показывает форму
    "Войти"). Если у вас есть аккаунт, добавьте авторизацию через
    requests.Session() с логином/паролем из окружения (TENDERS_KG_LOGIN /
    TENDERS_KG_PASSWORD) перед парсингом. Ниже — шаблон парсинга
    предполагает, что вы уже авторизованы (session с cookies) либо что
    гостевой доступ ("Войти как гость") даёт доступ к списку.
    """
    base = "https://www.tenders.kg"
    list_url = f"{base}/Announcements_list.php?a=return?f=all"
    soup = _get_soup(list_url)
    results = []
    if not soup:
        return results

    # TODO: проверить реальную структуру таблицы объявлений после логина/гостя
    for row in soup.select("table.announcements tr, div.announcement-row"):
        link_tag = row.select_one("a")
        if not link_tag or not link_tag.get("href"):
            continue
        url = link_tag["href"]
        if url.startswith("/"):
            url = base + url
        title = link_tag.get_text(strip=True)
        if not title:
            continue
        results.append({
            "id": url,
            "url": url,
            "title": title,
            "source": "tenders.kg",
            "status": STATUS_ACTIVE,
        })
    return results


def parse_aris_kg() -> list[dict]:
    """
    aris.kg — раздел тендеров лежит НЕ на главной странице, а на /tenders.
    Разметка — обычная HTML-таблица (без JS-рендера): три колонки —
    "Заголовок" (ссылка на /tenders/view/<id>), "Дата публикации",
    "Срок подачи заявок". Пагинация: /tenders/2, /tenders/3, ...
    Проверено вручную на реальной странице 2026-09-15.
    """
    base = "https://www.aris.kg"
    results = []

    # Возьмём первые 3 страницы списка — обычно этого достаточно, чтобы
    # не пропустить новые тендеры между запусками бота (расписание —
    # раз в день). При необходимости увеличьте диапазон.
    for page in range(1, 4):
        list_url = f"{base}/tenders" if page == 1 else f"{base}/tenders/{page}"
        soup = _get_soup(list_url, referer=f"{base}/tenders")
        if not soup:
            continue

        table = soup.select_one("table")
        if not table:
            continue

        rows = table.select("tr")
        for row in rows:
            cells = row.select("td")
            if len(cells) < 3:
                continue  # строка заголовка таблицы или пустая
            link_tag = cells[0].select_one("a")
            if not link_tag or not link_tag.get("href"):
                continue
            url = link_tag["href"]
            if url.startswith("/"):
                url = base + url
            title = link_tag.get_text(strip=True)
            deadline = cells[2].get_text(strip=True)
            results.append({
                "id": url,
                "url": url,
                "title": title,
                "source": "aris.kg",
                "status": STATUS_ACTIVE,
                "deadline": deadline,
            })
        time.sleep(1)

    return results


def parse_goszakupki_okmot_kg() -> list[dict]:
    """
    goszakupki.okmot.kg — портал электронных госзакупок кабинета министров.
    TODO: часто такие ЕИС рендерят список через JS/API (XHR к /api/...).
    Если requests.get() возвращает пустой HTML, посмотрите вкладку Network
    в браузере — вероятно, есть отдельный JSON-эндпоинт, который проще
    и надёжнее парсить напрямую (requests.get(api_url).json()).
    """
    base = "https://goszakupki.okmot.kg"
    soup = _get_soup(f"{base}/public/home")
    results = []
    if not soup:
        return results

    for card in soup.select("div.tender-card, tr.procurement-row"):
        link_tag = card.select_one("a")
        if not link_tag or not link_tag.get("href"):
            continue
        url = link_tag["href"]
        if url.startswith("/"):
            url = base + url
        title = link_tag.get_text(strip=True)
        results.append({
            "id": url,
            "url": url,
            "title": title,
            "source": "goszakupki.okmot.kg",
            "status": STATUS_ACTIVE,
        })
    return results


def parse_procurement_kg() -> list[dict]:
    """
    procurement.kg
    TODO: уточнить реальные селекторы после осмотра страницы в браузере.
    """
    base = "https://procurement.kg"
    soup = _get_soup(base)
    results = []
    if not soup:
        return results

    for card in soup.select("div.tender-item, li.tender"):
        link_tag = card.select_one("a")
        if not link_tag or not link_tag.get("href"):
            continue
        url = link_tag["href"]
        if url.startswith("/"):
            url = base + url
        title = link_tag.get_text(strip=True)
        results.append({
            "id": url,
            "url": url,
            "title": title,
            "source": "procurement.kg",
            "status": STATUS_ACTIVE,
        })
    return results


PARSERS = [
    parse_zakupki_gov_kg,
    parse_tenders_kg,
    parse_aris_kg,
    parse_goszakupki_okmot_kg,
    parse_procurement_kg,
]


def _normalize_status(raw: str) -> str | None:
    raw = (raw or "").lower()
    if any(w in raw for w in ["отмен", "аннулир"]):
        return STATUS_CANCELLED
    if any(w in raw for w in ["заверш", "итог", "подведен"]):
        return STATUS_COMPLETED
    if any(w in raw for w in ["актив", "прием", "открыт"]):
        return STATUS_ACTIVE
    return None


def fetch_all_tenders() -> list[dict]:
    all_tenders = []
    for parser in PARSERS:
        try:
            found = parser()
            log.info("%s: найдено %d записей", parser.__name__, len(found))
            all_tenders.extend(found)
        except Exception as e:
            log.exception("Парсер %s упал с ошибкой: %s", parser.__name__, e)
        time.sleep(1)  # вежливая пауза между площадками
    return all_tenders


def matches_keywords(title: str) -> bool:
    if not KEYWORDS:
        return True  # если темы не заданы — пропускаем все
    title_l = title.lower()
    return any(kw in title_l for kw in KEYWORDS)


# ---------------------------------------------------------------------------
# Парсинг результатов тендера (победитель / участники)
# ---------------------------------------------------------------------------

def parse_tender_results(tender: dict) -> dict:
    """
    Пытается открыть страницу конкретного тендера и вытащить победителя
    и список участников. Возвращает dict с ключами winner/participants
    (пустые значения, если не найдено или структура не распознана).

    TODO: под каждую площадку селекторы для блока "Результаты"/"Протокол"
    свои — этот шаблон ищет наиболее общие текстовые маркеры
    ("Победитель:", "Участники:") и должен быть уточнён вручную.
    """
    soup = _get_soup(tender["url"])
    winner = None
    participants: list[str] = []
    if not soup:
        return {"winner": winner, "participants": participants}

    text = soup.get_text("\n", strip=True)

    m = re.search(r"Победитель[:\s]+([^\n]+)", text, re.IGNORECASE)
    if m:
        winner = m.group(1).strip()

    m = re.search(r"Участники[:\s]+([^\n]+)", text, re.IGNORECASE)
    if m:
        raw = m.group(1)
        participants = [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]

    return {"winner": winner, "participants": participants}


def is_my_company(name: str | None) -> bool:
    if not name or not MY_COMPANY_NAMES:
        return False
    name_l = name.lower()
    return any(my in name_l or name_l in my for my in MY_COMPANY_NAMES)


# ---------------------------------------------------------------------------
# Отправка почты
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str) -> None:
    if not TO_EMAILS:
        log.warning("TO_EMAILS пуст — письмо не отправлено: %s", subject)
        return
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.error("GMAIL_USER / GMAIL_APP_PASSWORD не заданы — письмо не отправлено")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    # Получателей кладём в Bcc, "To" оставляем самому себе, чтобы не
    # раскрывать список адресов друг другу.
    msg["To"] = GMAIL_USER
    msg["Bcc"] = ", ".join(TO_EMAILS)

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    all_recipients = [GMAIL_USER] + TO_EMAILS

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, all_recipients, msg.as_string())
        log.info("Письмо отправлено: %s (получателей: %d)", subject, len(TO_EMAILS))
    except smtplib.SMTPException as e:
        log.error("Ошибка отправки письма: %s", e)


def render_tender_html(t: dict) -> str:
    parts = [
        f"<b>{t['title']}</b><br>",
        f"Площадка: {t['source']}<br>",
        f"Статус: {t['status']}<br>",
        f"<a href='{t['url']}'>{t['url']}</a><br>",
    ]
    if t.get("customer"):
        parts.append(f"Заказчик: {t['customer']}<br>")
    if t.get("deadline"):
        parts.append(f"Срок подачи: {t['deadline']}<br>")
    if t.get("winner"):
        parts.append(f"Победитель: <b>{t['winner']}</b><br>")
    if t.get("participants"):
        parts.append(f"Участники: {', '.join(t['participants'])}<br>")
    return "<div style='margin-bottom:16px;padding:12px;border:1px solid #ddd'>" + "".join(parts) + "</div>"


# ---------------------------------------------------------------------------
# Основная логика одного прогона
# ---------------------------------------------------------------------------

def run() -> None:
    db = load_db()
    now = datetime.now(timezone.utc).isoformat()

    found_tenders = fetch_all_tenders()

    new_tenders = []
    cancelled_alerts = []
    win_alerts = []
    summary_updates = []

    seen_ids = set()

    for t in found_tenders:
        tid = t["id"]
        seen_ids.add(tid)

        if not matches_keywords(t["title"]):
            continue

        existing = db.get(tid)

        if existing is None:
            # Новый тендер
            t["first_seen"] = now
            t["notified"] = False
            db[tid] = t
            new_tenders.append(t)
            continue

        # Тендер уже известен — проверяем изменение статуса
        old_status = existing.get("status")
        new_status = t["status"]

        if new_status != old_status:
            existing["status"] = new_status
            existing["status_updated_at"] = now

            if old_status == STATUS_ACTIVE and new_status == STATUS_CANCELLED:
                cancelled_alerts.append(existing)

            if new_status == STATUS_COMPLETED:
                results = parse_tender_results(existing)
                existing["winner"] = results.get("winner")
                existing["participants"] = results.get("participants")
                summary_updates.append(existing)
                if is_my_company(results.get("winner")):
                    win_alerts.append(existing)

        db[tid] = existing

    # Дополнительно: для тендеров, уже помеченных "Завершен" в базе, но без
    # winner (например, итоги подвели не сразу) — пробуем повторно спарсить
    for tid, t in db.items():
        if t.get("status") == STATUS_COMPLETED and not t.get("winner") and tid in seen_ids:
            results = parse_tender_results(t)
            if results.get("winner"):
                t["winner"] = results["winner"]
                t["participants"] = results.get("participants", [])
                summary_updates.append(t)
                if is_my_company(results.get("winner")):
                    win_alerts.append(t)

    # --- Отправка писем ---

    if new_tenders:
        body = "<h2>Новые тендеры по вашим темам</h2>" + "".join(
            render_tender_html(t) for t in new_tenders
        )
        send_email(f"🆕 Новые тендеры: {len(new_tenders)} шт.", body)
        for t in new_tenders:
            db[t["id"]]["notified"] = True

    for t in cancelled_alerts:
        body = (
            f"<h2>⚠️ Внимание! Тендер «{t['title']}» был отменен заказчиком</h2>"
            + render_tender_html(t)
        )
        send_email(f"⚠️ Тендер отменен: {t['title']}", body)

    if summary_updates:
        body = "<h2>Итоги по завершенным тендерам</h2>" + "".join(
            render_tender_html(t) for t in summary_updates
        )
        send_email(f"📋 Итоги тендеров: {len(summary_updates)} шт.", body)

    for t in win_alerts:
        body = (
            f"<h2>🎉 Поздравляем! Вы выиграли тендер «{t['title']}»!</h2>"
            + render_tender_html(t)
        )
        send_email(f"🎉 Победа в тендере: {t['title']}", body)

    save_db(db)
    save_sent_list(db)

    log.info(
        "Готово. Новых: %d, отмен: %d, итогов: %d, побед: %d",
        len(new_tenders), len(cancelled_alerts), len(summary_updates), len(win_alerts),
    )


if __name__ == "__main__":
    run()
