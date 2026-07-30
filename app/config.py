from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки backend. Значения берутся из .env или переменных окружения."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Режим разработки: код подтверждения возвращается в ответе и печатается в лог.
    debug: bool = True

    # JWT
    secret_key: str = "CHANGE-ME-IN-PRODUCTION"
    access_token_ttl_minutes: int = 60 * 24 * 30  # 30 дней

    # Код подтверждения
    code_ttl_seconds: int = 300  # 5 минут
    code_length: int = 4

    # Отправка писем (SMTP). Пусто → код пишется в лог (dev-режим).
    # Подходит любой российский провайдер: Яндекс 360, Mailopost, SMTP.bz и т.п.
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""          # напр. "Фаворит <noreply@favorit-consult.ru>"
    smtp_ssl: bool = True         # True → SSL (465), False → STARTTLS (587)

    # Интеграция с Битрикс24 (пусто → работаем на мок-адаптере)
    bitrix_webhook_url: str = ""
    # ID открытой линии Битрикс для чата с клиентом. Заполнить после того,
    # как в UI Битрикса создадите открытую линию и подключите наш коннектор.
    openlines_line_id: str = ""
    # Секретный токен исходящего вебхука Битрикс — для валидации, что запрос
    # реально от Битрикса. Задаём при настройке события OnImConnectorMessageAdd.
    bitrix_event_token: str = ""

    # OAuth «Локального приложения» Битрикс — нужен для методов, которые
    # запрещены во входящем вебхуке (imconnector.register и т.п.).
    # Заполняется после создания Local App в UI Битрикса.
    bitrix_client_id: str = ""
    bitrix_client_secret: str = ""
    # Домен портала Битрикс (например favorit-consult.bitrix24.ru). Определяется
    # автоматически при установке приложения; задан по умолчанию для нашего портала.
    bitrix_domain: str = "favorit-consult.bitrix24.ru"

    # Белый список для чтения из Битрикс (через запятую). Пусто → без ограничения.
    # На время теста на живом портале держим здесь только тестовые e-mail/телефоны.
    bitrix_allowed_emails: str = ""
    bitrix_allowed_phones: str = ""

    @property
    def bitrix_allowed_set(self) -> set:
        return {e.strip().lower() for e in self.bitrix_allowed_emails.split(",") if e.strip()}

    @property
    def bitrix_allowed_phone_set(self) -> set:
        import re
        out = set()
        for p in self.bitrix_allowed_phones.split(","):
            d = re.sub(r"\D", "", p)
            if d:
                out.add(d[-10:])  # последние 10 цифр — без учёта +7/8
        return out

    # CORS — список origin через запятую. На проде — только наш прод-домен.
    # В dev/пилоте можно добавить http://89.169.178.16 для обратной совместимости.
    cors_origins: str = "https://app.favorit-consult.ru"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Хранилище файлов (РФ S3-совместимое) — заполним на этапе интеграции
    s3_endpoint: str = ""
    s3_bucket: str = ""

    # База данных пользователей приложения (dev — SQLite; прод — PostgreSQL)
    db_path: str = "favorit.db"

    # Админ-панель
    admin_password: str = "admin123"  # сменить в проде!
    # Email для уведомлений (сейчас — NPS-оценки клиентов).
    admin_email: str = "ivan.izhko@gmail.com"

    # ---- Двухфакторная аутентификация админки (TOTP) ----
    # Base32-секрет для приложения-аутентификатора (Google Authenticator,
    # Яндекс.Ключ, 1Password и т.п.). Сгенерировать: python scripts/gen_totp.py
    # Пусто → 2FA выключена, вход только по паролю (как раньше).
    admin_totp_secret: str = ""
    # Имя, под которым запись видна в приложении-аутентификаторе.
    admin_totp_issuer: str = "Фаворит · Админ"

    @property
    def totp_enabled(self) -> bool:
        return bool(self.admin_totp_secret.strip())

    # ---- Защита логина от подбора пароля ----
    # После N неудачных попыток с одного IP вход блокируется на M секунд.
    admin_login_max_attempts: int = 5
    admin_login_lockout_seconds: int = 300  # 5 минут

    # ---- Контроль качества по оценкам клиентов ----
    # Градация: 0-6 — в контроль качества, 9-10 — лучшие оценки, идут
    # в рейтинг менеджеров, 7-8 — нейтральные, не влияют ни на что.
    # Совпадает с классическими границами NPS.
    qc_detractor_max: int = 6      # 0..6 → задача в отдел контроля качества
    qc_promoter_min: int = 9       # 9..10 → лучшие, идут в рейтинг

    # ID сотрудников в Битриксе, на которых уходят задачи. Узнать можно
    # в профиле сотрудника: /company/personal/user/<ID>/
    # Пусто → задача не создаётся, событие только пишется в лог.
    bitrix_qc_head_id: int = 0        # руководитель отдела контроля качества
    bitrix_support_head_id: int = 0   # руководитель отдела сопровождения
    bitrix_ceo_id: int = 0            # генеральный директор

    @property
    def monthly_report_recipients(self) -> list[int]:
        """Кому уходит месячный отчёт. Порядок фиксирован: контроль качества,
        сопровождение, гендиректор. Нули отбрасываем — незаполненный ID
        означает «этому пока не отправляем»."""
        return [i for i in (self.bitrix_qc_head_id,
                            self.bitrix_support_head_id,
                            self.bitrix_ceo_id) if i]

    # Дедлайн задачи по низкой оценке, в часах от момента её появления.
    qc_task_deadline_hours: int = 24
    # Сколько раз пробуем создать задачу, прежде чем сдаться (Битрикс мог
    # лежать). Строка остаётся в базе, её видно в админке.
    qc_task_max_attempts: int = 5
    # По оценкам старше этого возраста задачи не ставим. Защита от залпа:
    # при первом запуске воркер разбирает всю накопленную базу разом, и без
    # ограничения отдел контроля качества получил бы сотню задач по делам
    # годичной давности.
    qc_task_max_age_days: int = 7

    # Число оценок, разбираемых за один прогон воркера. Ограничение нужно,
    # чтобы первый запуск на накопленной базе не выгреб Битрикс лимитами.
    qc_batch_size: int = 50

    # День месяца, начиная с которого отправляем отчёт за прошлый месяц.
    monthly_report_day: int = 1

    # Распознавание номеров со скриншотов — Yandex Vision OCR (РФ).
    yandex_vision_api_key: str = ""
    yandex_folder_id: str = ""

    # Анти-спам / 230-ФЗ
    collector_confirm_threshold: int = 3  # сколько жалоб → номер «подтверждён»
    # Наши защищённые номера (белый список, их нельзя заблокировать), через запятую.
    company_phones: str = ""

    @property
    def company_phone_list(self) -> list:
        return [p.strip() for p in self.company_phones.split(",") if p.strip()]

    # Т-Касса (Тинькофф Эквайринг). Оставили модуль `tbank_pay.py` в репо
    # на будущее (если решим подключать карточную оплату). Сейчас основной
    # флоу — оплата по реквизитам без комиссии банку.
    tbank_terminal_key: str = ""
    tbank_password: str = ""
    tbank_notification_url: str = "https://app.favorit-consult.ru/api/payments/webhook"
    tbank_success_url: str = "https://app.favorit-consult.ru/pay-ok"
    tbank_fail_url: str = "https://app.favorit-consult.ru/pay-fail"

    # ---- Реквизиты юрлица для оплаты по переводу ----
    # Показываются клиенту в экране «Оплата по реквизитам». Значения по
    # умолчанию — актуальные реквизиты ЮЦ «Фаворит». .env перекроет при
    # необходимости.
    company_legal_name: str = "ЮЦ «Фаворит»"
    company_full_name: str = "ООО «Юридический центр Фаворит»"
    company_inn: str = "7720413438"
    company_kpp: str = "772001001"
    company_ogrn: str = "1187746123806"
    company_bank_account: str = "40702810510000501406"
    company_bank_name: str = "АО «Тинькофф Банк»"
    company_bank_bik: str = "044525974"
    company_bank_correspondent: str = "30101810145250000984"


settings = Settings()
