# Tender Monitoring Bot (Кыргызстан)

Мониторинг тендеров с 5 площадок, фильтрация по ключевым словам,
уведомления на Gmail, отслеживание отмен и победителей. Работает
полностью на GitHub Actions без сервера.

## Структура проекта

```
tender-bot/
├── bot.py                          # основной скрипт
├── requirements.txt
├── tenders_db.json                 # база тендеров (создаётся автоматически)
├── sent_tenders.json               # плоский список уже отправленных ID (для совместимости)
└── .github/
    └── workflows/
        └── tender_bot.yml          # расписание + автокоммит базы
```

## Настройка

### 1. Пароль приложения Gmail

Обычный пароль от Gmail для SMTP не подойдёт. Нужно:
1. Включить двухфакторную аутентификацию в аккаунте Google.
2. Создать "Пароль приложения" (App Password): https://myaccount.google.com/apppasswords
3. Использовать этот 16-значный пароль как `GMAIL_APP_PASSWORD`.

### 2. Secrets репозитория (Settings → Secrets and variables → Actions → Secrets)

| Имя | Пример значения | Описание |
|---|---|---|
| `GMAIL_USER` | `mybot@gmail.com` | Аккаунт, с которого шлются письма |
| `GMAIL_APP_PASSWORD` | `abcd efgh ijkl mnop` | Пароль приложения Gmail |
| `TO_EMAILS` | `mail1@gmail.com, mail2@mail.ru` | Получатели через запятую |

### 3. Variables репозитория (там же, вкладка Variables)

| Имя | Пример значения | Описание |
|---|---|---|
| `KEYWORDS` | `ремонт дорог, строительство, IT оборудование` | Ключевые слова через запятую |
| `MY_COMPANY_NAMES` | `ООО Ромашка, ИП Иванов` | Ваши названия компаний для проверки победы |

Variables используются вместо Secrets для KEYWORDS/MY_COMPANY_NAMES,
потому что это не чувствительные данные и их удобнее редактировать —
но при желании можно перенести их и в Secrets, поменяв `vars.` на
`secrets.` в `tender_bot.yml`.

### 4. Права на запись для workflow

Settings → Actions → General → Workflow permissions →
**Read and write permissions** (иначе `git push` из workflow не сработает).

### 5. Первый запуск

Запустите workflow вручную (Actions → Tender Monitoring Bot → Run workflow),
чтобы убедиться, что письма приходят и `tenders_db.json` коммитится.

## Важно про парсеры (`parse_*` функции в bot.py)

Каждая площадка (`zakupki.gov.kg`, `tenders.kg`, `aris.kg`,
`goszakupki.okmot.kg`, `procurement.kg`) имеет свою вёрстку, и часть из
них (например, `tenders.kg`) требует авторизации либо рендерит список
через JavaScript. В коде каждая функция `parse_<site>` содержит
рабочий каркас запроса + разбора HTML, но CSS-селекторы отмечены как
`TODO` — перед продакшн-использованием откройте страницу в браузере,
посмотрите DevTools → Elements/Network и подставьте реальные селекторы
(или, если данные приходят через JSON-API, замените парсинг HTML на
прямой запрос к этому API — это надёжнее).

## Логика статусов

- Новый тендер → сохраняется со статусом `Активен`, шлётся письмо "Новые тендеры".
- Если статус меняется `Активен → Отменен` → отдельное письмо-предупреждение.
- Если статус становится `Завершен` → бот пытается спарсить победителя/участников
  со страницы тендера и включает это в сводное письмо "Итоги тендеров".
- Если победитель совпадает с одним из `MY_COMPANY_NAMES` → отдельное
  письмо "🎉 Поздравляем!".

## Локальный тест

```bash
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
export TO_EMAILS="you@gmail.com"
export KEYWORDS="строительство, ремонт"
export MY_COMPANY_NAMES="ООО Ромашка"

pip install -r requirements.txt
python bot.py
```
