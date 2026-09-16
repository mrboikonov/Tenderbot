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

try:
    # Опционально: если рядом с bot.py лежит файл .env (только для
    # локального запуска — см. run_local.ps1/run_local.sh), подгружаем
    # из него переменные окружения. В GitHub Actions файла .env нет,
    # и эта строка просто ничего не делает.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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


def _get_json(url: str, referer: str | None = None) -> dict | list | None:
    """Как _get_soup, но для JSON API-эндпоинтов (без HTML-парсинга)."""
    domain = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    verify_ssl = domain not in INSECURE_DOMAINS

    headers = {"Accept": "application/json"}
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
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        log.error("Ошибка запроса JSON %s: %s", url, e)
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
        "наименование организации": "customer",
        "наименование закупки": "title",
        "срок подачи предложений": "deadline",
        "дата публикации": "published",
    }

    def _strip_leading_label(text: str) -> str:
        """
        Запасной вариант: убирает ведущий "прилипший" текст лейбла
        (латиница/пунктуация) до первого кириллического символа.
        Используется, если точное совпадение по LABEL_MAP не сработало
        (например, из-за иного регистра/формата лейбла на странице).
        """
        m = re.search(r"[А-Яа-яЁё]", text)
        return text[m.start():].strip() if m else text.strip()

    def _extract_field(cell_text: str) -> tuple[str, str] | None:
        low = cell_text.lower()
        for label, key in LABEL_MAP.items():
            if low.startswith(label):
                return key, cell_text[len(label):].lstrip(" :\t").strip()
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
                cells = row.select("td")
                for cell in cells:
                    cell_text = cell.get_text(strip=True)
                    field = _extract_field(cell_text)
                    if field:
                        data[field[0]] = field[1]

                if not data["title"]:
                    # Запасной вариант: точный лейбл не совпал — берём
                    # ячейку с наибольшим количеством кириллических
                    # символов (после отсечения латинского "лейбла" в
                    # начале) как наиболее вероятное название тендера.
                    best_text, best_len = "", 0
                    for cell in cells:
                        stripped = _strip_leading_label(cell.get_text(strip=True))
                        cyr_len = len(re.findall(r"[А-Яа-яЁё]", stripped))
                        if cyr_len > best_len:
                            best_text, best_len = stripped, cyr_len
                    if best_text:
                        data["title"] = best_text

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


_tenders_kg_logged_in = False


def _tenders_kg_ensure_login() -> bool:
    """
    tenders.kg требует авторизации даже для просмотра списка объявлений.
    Форма логина (проверено вручную 2026-09-15, вкладка Network ->
    Payload при отправке формы) отправляет обычный POST без токена
    reCAPTCHA:
        POST https://www.tenders.kg/login.php
        Form Data: btnSubmit=Login, username=<...>, password=<...>
    Логинимся один раз за запуск скрипта через общую SESSION — cookies
    сессии сохранятся и будут использоваться во всех дальнейших запросах
    к этому домену автоматически.
    """
    global _tenders_kg_logged_in
    if _tenders_kg_logged_in:
        return True

    username = os.environ.get("TENDERS_KG_USERNAME", "")
    password = os.environ.get("TENDERS_KG_PASSWORD", "")
    if not username or not password:
        log.warning(
            "TENDERS_KG_USERNAME/TENDERS_KG_PASSWORD не заданы — "
            "tenders.kg не будет обработан."
        )
        return False

    login_url = "https://www.tenders.kg/login.php"
    try:
        resp = SESSION.post(
            login_url,
            data={"btnSubmit": "Login", "username": username, "password": password},
            headers={"Referer": "https://www.tenders.kg/"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Ошибка логина на tenders.kg: %s", e)
        return False

    # Проверяем, что вход реально сработал: на странице после логина не
    # должно быть формы "Войти". Это не 100% надёжная проверка (текст
    # может встречаться и в другом контексте), но простая и достаточная
    # для базовой диагностики.
    if "Войти" in resp.text and "Пароль" in resp.text:
        log.error(
            "Логин на tenders.kg не удался (страница похода на форму "
            "входа) — проверьте TENDERS_KG_USERNAME/TENDERS_KG_PASSWORD."
        )
        return False

    _tenders_kg_logged_in = True
    log.info("Успешный вход на tenders.kg.")
    return True


def parse_tenders_kg() -> list[dict]:
    """
    tenders.kg — требует авторизации (см. _tenders_kg_ensure_login).
    После успешного логина список объявлений лежит на
    Announcements_list.php. Разметка не проверена вручную на реальных
    данных (только форма логина) — селекторы ниже основаны на общей
    структуре форума/списка (похоже на движок форума), могут требовать
    донастройки после первого успешного логина. Если после входа
    найдено 0 записей — почти наверняка нужно поправить селекторы, а
    не логин (логин к этому моменту уже подтверждён отдельной проверкой
    выше).
    """
    if not _tenders_kg_ensure_login():
        return []

    base = "https://www.tenders.kg"
    list_url = f"{base}/Announcements_list.php?a=return?f=all"
    soup = _get_soup(list_url, referer=base)
    results = []
    if not soup:
        return results

    # TODO: уточнить селекторы после первого успешного логина — общий
    # шаблон: строки таблицы/списка со ссылкой на Announcements_view.php.
    for link_tag in soup.select('a[href*="Announcements_view.php"]'):
        href = link_tag.get("href", "")
        url = href if href.startswith("http") else base + "/" + href.lstrip("/")
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


def _normalize_goszakupki_status(raw: str) -> str:
    raw_u = (raw or "").upper()
    if "CANCEL" in raw_u or "REJECT" in raw_u:
        return STATUS_CANCELLED
    if any(w in raw_u for w in ["COMPLETE", "FINISH", "CLOSED", "ARCHIVE", "SIGNED"]):
        return STATUS_COMPLETED
    return STATUS_ACTIVE


def parse_goszakupki_okmot_kg() -> list[dict]:
    """
    goszakupki.okmot.kg — React SPA, но данные приходят через открытый
    JSON API (найдено вручную через DevTools -> Network, 2026-09-15):
        GET /api/public_tender/published?first=0&rows=N
    Возвращает {"content": [ {id, number, companyName, name, method,
    amount, datePublished, dateContest, status}, ... ]}.

    Статусы наблюдались как "VERIFIED_PUBLISHED" (активно) в самом списке
    /published. На детальной странице конкретного тендера видели бейдж
    "Не состоялся" (тендер, скорее всего, был отменён/не собрал заявок) —
    это подтверждает, что другие статусы существуют, но раз "published"
    эндпоинт, судя по всему, отдаёт только активные объявления, для
    надёжного отслеживания смены статуса, вероятно, нужен ещё один
    API-эндпоинт (что-то вроде /api/public_tender/completed или
    /failed) — стоит поискать его так же через DevTools -> Network,
    открыв фильтр статуса на сайте (если он есть в разделе "Объявления").

    Ссылка на детальную страницу подтверждена вручную 2026-09-15:
        https://goszakupki.okmot.kg/public/order/view/<id>?tab=tender
    """
    base = "https://goszakupki.okmot.kg"
    api_url = f"{base}/api/public_tender/published?first=0&rows=50"
    # Формат подтверждён вручную 2026-09-15 (реальная страница тендера):
    # https://goszakupki.okmot.kg/public/order/view/<id>?tab=tender
    DETAIL_URL_TEMPLATE = base + "/public/order/view/{id}?tab=tender"

    data = _get_json(api_url, referer=f"{base}/public/home")
    results = []
    if not data:
        return results

    items = data.get("content", []) if isinstance(data, dict) else data
    for item in items:
        tender_id = item.get("id") or item.get("number")
        if not tender_id:
            continue
        results.append({
            "id": f"goszakupki:{tender_id}",
            "url": DETAIL_URL_TEMPLATE.format(id=tender_id),
            "title": item.get("name", "").strip() or f"Тендер №{item.get('number', tender_id)}",
            "source": "goszakupki.okmot.kg",
            "status": _normalize_goszakupki_status(item.get("status", "")),
            "customer": item.get("companyName", ""),
            "deadline": item.get("dateContest", ""),
            "published": item.get("datePublished", ""),
        })
    return results


def parse_procurement_kg() -> list[dict]:
    """
    procurement.kg — проверено вручную 2026-09-15: полная аналитика
    требует регистрации, НО раздел "Свежие закупки" на главной странице
    открыт без входа и содержит ~24 последних объявления со всех
    площадок страны, каждое со ссылкой вида /tenders/<id>.
    """
    base = "https://procurement.kg"
    soup = _get_soup(base)
    results = []
    if not soup:
        return results

    seen_ids = set()
    for link_tag in soup.select('a[href*="/tenders/"]'):
        href = link_tag.get("href", "")
        m = re.search(r"/tenders/(\d+)", href)
        if not m:
            continue  # пропускаем не относящиеся ссылки (напр. /tenders/catalog)
        tender_id = m.group(1)
        if tender_id in seen_ids:
            continue
        seen_ids.add(tender_id)

        title = link_tag.get_text(strip=True)
        if not title:
            continue
        url = href if href.startswith("http") else base + href

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

# Позволяет запускать бота только для части площадок — используется для
# локального запуска (см. run_local.ps1 / run_local.sh), где обрабатываются
# только площадки, заблокированные для IP-адресов GitHub Actions
# (tenders_kg, procurement_kg), в то время как остальные три продолжают
# работать в облаке по прежнему расписанию.
_ONLY_SOURCES = _split_env_list("ONLY_SOURCES")
if _ONLY_SOURCES:
    PARSERS = [p for p in PARSERS if any(name in p.__name__ for name in _ONLY_SOURCES)]
    log.info("ONLY_SOURCES активен, обрабатываются только: %s", [p.__name__ for p in PARSERS])


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
            # Временная диагностика: показываем первые 3 названия, чтобы
            # проверить, правильно ли извлекается title (а не заглушка
            # вида "Тендер №..."). Можно убрать после проверки.
            for sample in found[:3]:
                log.info(
                    "  пример: [%s] %r",
                    sample.get("source"),
                    sample.get("title"),
                )
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
    """
    if tender.get("source") == "goszakupki.okmot.kg":
        return _parse_goszakupki_results(tender)

    # Универсальный HTML-путь для остальных площадок (см. комментарий
    # в начале функции файла — TODO под каждую площадку свои селекторы).
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


def _parse_goszakupki_results(tender: dict) -> dict:
    """
    goszakupki.okmot.kg — победитель/участники через найденный вручную
    API (проверено 2026-09-15):
        GET /api/public_submission/getSubmissionsbyTenderId?tenderId=<id>
    На момент проверки этот эндпоинт для тестового тендера вернул
    пустой список `[]` (у тендера ещё не было заявок), поэтому точная
    структура объекта одной заявки (какое поле содержит название
    компании-участника и как помечен победитель) НЕ подтверждена
    напрямую. Ниже — код, который пробует несколько наиболее вероятных
    названий полей (`companyName`, `supplierName`, `name`, флаги
    `isWinner`/`winner`/`status`). Если после реального запуска бота
    победитель/участники будут пустыми для тендера, который явно завершён
    с результатом — откройте такой тендер на сайте, найдите в Network тот
    же запрос `getSubmissionsbyTenderId` и пришлите содержимое Response —
    поля будут скорректированы.
    """
    base = "https://goszakupki.okmot.kg"
    raw_id = tender["id"].split(":", 1)[1] if ":" in tender["id"] else tender["id"]
    url = f"{base}/api/public_submission/getSubmissionsbyTenderId?tenderId={raw_id}"

    data = _get_json(url, referer=tender["url"])
    winner = None
    participants: list[str] = []

    if isinstance(data, list):
        for sub in data:
            if not isinstance(sub, dict):
                continue
            name = (
                sub.get("companyName")
                or sub.get("supplierName")
                or sub.get("name")
                or sub.get("participantName")
            )
            if not name:
                continue
            participants.append(name)
            is_winner = (
                sub.get("isWinner") is True
                or sub.get("winner") is True
                or str(sub.get("status", "")).upper() in ("WINNER", "SELECTED", "WON")
            )
            if is_winner:
                winner = name

    return {"winner": winner, "participants": participants}


def is_my_company(name: str | None) -> bool:
    if not name or not MY_COMPANY_NAMES:
        return False
    name_l = name.lower()
    return any(my in name_l or name_l in my for my in MY_COMPANY_NAMES)


# ---------------------------------------------------------------------------
# Отправка почты
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str) -> bool:
    if not TO_EMAILS:
        log.warning("TO_EMAILS пуст — письмо не отправлено: %s", subject)
        return False
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.error("GMAIL_USER / GMAIL_APP_PASSWORD не заданы — письмо не отправлено")
        return False

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
        return True
    except smtplib.SMTPException as e:
        log.error("Ошибка отправки письма: %s", e)
        return False


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
        email_sent = send_email(f"🆕 Новые тендеры: {len(new_tenders)} шт.", body)
        if email_sent:
            for t in new_tenders:
                db[t["id"]]["notified"] = True
        else:
            log.warning(
                "Письмо о новых тендерах не отправлено — они останутся "
                "непомеченными и попробуют отправиться на следующем запуске."
            )

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
