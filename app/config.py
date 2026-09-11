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

    # База данных пользователей приложения.
    # dev — SQLite (db_path), прод — PostgreSQL (pg_dsn, use_postgres=True).
    # Основной backend (favorit-app) должен использовать те же настройки,
    # иначе admin и backend будут смотреть в разные БД.
    db_path: str = "favorit.db"
    pg_dsn: str = ""
    use_postgres: bool = False

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
    # Градация: 0-7 — в контроль качества, 9-10 — лучшие оценки, идут
    # в рейтинг менеджеров, 8 — нейтральная, не влияет ни на что.
    # Семёрка отнесена к низким по решению бизнеса: клиент, поставивший 7,
    # чем-то недоволен, и разбирать это лучше до того, как он уйдёт.
    # Подписи диапазонов в интерфейсе строятся из этих чисел — менять
    # их достаточно здесь.
    qc_detractor_max: int = 7      # 0..7 → задача в отдел контроля качества
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

    # ---- Стадии банкротства (воронка Битрикса) ----
    # Направление сделок «БФЛ. Сопровождение».
    bankruptcy_category_id: int = 15

    # Стадия, означающая завершение дела. По решению заказчика это «Долг
    # списан», а НЕ «Успешно реализовано»: сделка может висеть открытой
    # ещё долго после списания, и считать по ней значило бы завышать срок.
    stage_done_id: str = "C15:UC_UJI8T9"

    # Стадия «Договор заключён» — точка начала «цикла ведения дела».
    # Sales-менеджер создаёт сделку раньше (первый контакт → квалификация),
    # клиент подписывает через несколько дней → срок «от создания сделки»
    # завышен на pre-sales-хвост. «Цикл от договора» — честнее для оценки
    # юристов сопровождения.
    stage_contract_signed_id: str = "C15:FINAL_INVOICE"

    # Стадии после списания: дело уже закончено, просто сделка доводится до
    # формального закрытия. В «в работе» они попадать не должны — иначе
    # завершённые дела вечно считались бы текущими.
    stage_success_ids: str = "C15:WON"

    # Стадии, которые не показывать в воронке. По умолчанию пусто: показываем
    # всё, что есть в Битриксе. Скрытая стадия ломает сверку — сумма по
    # колонке «Сейчас» перестаёт сходиться с общим числом сделок, и человек
    # решает, что интеграция считает неправильно.
    # Если какая-то стадия окажется шумом, впишите её сюда через запятую.
    stage_service_ids: str = ""

    # Ожидание и проблемы: дело стоит не потому, что идёт работа. Считаем
    # отдельно и показываем как «застряло», в средний срок прохождения
    # не подмешиваем — иначе непонятно, где работа, а где простой.
    stage_stuck_ids: str = "C15:UC_7ICUJ1,C15:PREPARATION,C15:UC_ZTR9AW"

    # Провальные исходы. Из среднего срока дела исключаются: дело не дошло
    # до списания, и его длительность ничего не говорит о типичном сроке.
    stage_failed_ids: str = "C15:PREPAYMENT_INVOIC,C15:EXECUTING,C15:LOSE"

    # Взаимоисключающие процедуры: клиент проходит одну из них, не обе.
    # Показываем рядом, а не усредняем вместе.
    stage_alternative_ids: str = "C15:UC_LWCW5Y,C15:UC_3UFLUY"

    def _stage_set(self, raw: str) -> set:
        return {s.strip() for s in raw.split(",") if s.strip()}

    @property
    def service_stages(self) -> set:
        return self._stage_set(self.stage_service_ids)

    @property
    def stuck_stages(self) -> set:
        return self._stage_set(self.stage_stuck_ids)

    @property
    def failed_stages(self) -> set:
        return self._stage_set(self.stage_failed_ids)

    @property
    def success_stages(self) -> set:
        return self._stage_set(self.stage_success_ids)

    @property
    def alternative_stages(self) -> set:
        return self._stage_set(self.stage_alternative_ids)

    # Как часто обновлять снимок сделок. Стадии меняются медленнее оценок,
    # каждые 10 минут выгребать Битрикс незачем.
    stage_sync_min_interval_minutes: int = 60
    # Сколько сделок тянуть за один прогон. Битрикс отдаёт по 50 на страницу;
    # ограничение защищает от многочасового первого прогона.
    stage_sync_max_deals: int = 2000

    # ---- Отдел сопровождения: отказы и зависшие дела ----
    # Поле сделки в Битриксе с причиной отказа, если оно уже ведётся
    # (например UF_CRM_1712345678). Пусто → причина заполняется в админке.
    bitrix_refusal_reason_field: str = ""

    # Дело считается зависшим, если стоит на стадии дольше обычного.
    # Порог — медиана прохождения этой же стадии, умноженная на коэффициент.
    # Так «Реализация» с её месяцами не считается зависшей наравне со
    # «Сбором документов»: у каждой стадии своя норма, взятая из ваших данных.
    stuck_factor: float = 2.0
    # Нижняя граница: на быстрых стадиях удвоенная медиана может дать 3 дня,
    # а дёргать руководителя из-за трёх дней незачем.
    stuck_min_days: int = 14
    # Когда завершённых прохождений мало, медиане верить нельзя — берём это.
    stuck_default_days: int = 45
    # Сколько дел в риске показывать и по скольким ставить задачу за прогон.
    stuck_alert_batch: int = 20

    # По отказам старше этого срока задачи «выяснить причину» не ставим.
    # В воронке накапливаются отказы за все годы работы: на реальной базе их
    # почти тысяча. Без отсечки первый же запуск завалил бы отдел контроля
    # качества задачами по делам, закрытым годы назад, и разбирать их всё
    # равно никто не станет — клиент давно ушёл. В таблице они видны все.
    refusal_reason_max_age_days: int = 30

    # Первый запуск на накопленной базе: если зависших дел разом больше
    # этого числа, задачи не ставятся, а дела помечаются как уже известные.
    # Иначе руководитель сопровождения получил бы сотни задач за то, что
    # копилось годами. Дальше задачи идут только по новым зависаниям,
    # а накопленное разбирается по таблице в панели.
    stuck_baseline_threshold: int = 30

    # ---- Жалобы клиентов ----
    # Общий секрет для служебного эндпоинта, через который основной backend
    # передаёт жалобу. Пусто → эндпоинт закрыт (503), а не открыт всем:
    # незаполненная настройка не должна оборачиваться дырой.
    internal_api_token: str = ""

    # Срок ответа на претензию — 10 дней по Закону о защите прав потребителей.
    # Это внешнее требование, а не наше пожелание: просрочка даёт клиенту
    # право на неустойку. Показываем этот срок клиенту в приложении.
    complaint_answer_days: int = 10
    # Внутренний дедлайн задачи короче срока ответа — чтобы осталось время
    # на сам ответ, а не на тушение пожара в последний день.
    complaint_task_deadline_workdays: int = 3

    # Ограничения на подачу. Основное — одна открытая жалоба на категорию
    # (см. docs/research-complaints.md); эти числа лишь предохранитель
    # от скрипта, из норматива они не выведены.
    complaint_max_per_day: int = 3
    complaint_max_per_month: int = 10
    complaint_cooldown_seconds: int = 300
    complaint_text_min: int = 10
    complaint_text_max: int = 4000

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
