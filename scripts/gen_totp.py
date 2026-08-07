#!/usr/bin/env python3
"""Генератор секрета для 2FA админ-панели.

Запуск из корня репо:
    python scripts/gen_totp.py

Выводит base32-секрет для .env и otpauth-ссылку. Секрет вводится в
приложение-аутентификатор вручную («ввести ключ настройки»).

Проверить, что секрет работает, до правки .env:
    python scripts/gen_totp.py --check <СЕКРЕТ>
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.security import generate_totp_secret, totp_uri, verify_totp  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="2FA для админки Фаворит")
    p.add_argument("--account", default="admin", help="имя записи в аутентификаторе")
    p.add_argument("--issuer", default=settings.admin_totp_issuer, help="издатель")
    p.add_argument("--check", metavar="SECRET",
                   help="проверить существующий секрет вместо генерации нового")
    args = p.parse_args()

    if args.check:
        code = input("Введите текущий код из аутентификатора: ").strip()
        ok = verify_totp(code, args.check)
        print("\n  ✓ Код верный — секрет рабочий." if ok
              else "\n  ✗ Код не подошёл. Проверьте секрет и время на телефоне.")
        return 0 if ok else 1

    secret = generate_totp_secret()
    uri = totp_uri(secret, args.account, args.issuer)

    print(f"""
  Секрет сгенерирован.

  1. Добавьте в .env админ-сервиса:

       ADMIN_TOTP_SECRET={secret}

  2. В аутентификаторе (Google Authenticator, Яндекс.Ключ, 1Password)
     выберите «Ввести ключ настройки» и введите:

       Аккаунт : {args.account}
       Ключ    : {secret}
       Тип     : По времени (TOTP)

  3. Проверьте до перезапуска сервиса:

       python scripts/gen_totp.py --check {secret}

  4. Перезапустите сервис: sudo systemctl restart favorit-admin

  otpauth-ссылка (если аутентификатор умеет импорт по ссылке):
    {uri}

  ВНИМАНИЕ: не отправляйте этот секрет в мессенджер и не вставляйте его
  в онлайн-генераторы QR — тот, у кого есть секрет, проходит второй фактор.
  Если секрет утёк — сгенерируйте новый этим же скриптом.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
