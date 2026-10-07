# Корневой сертификат Минцифры

`russian_trusted_root_ca.pem` — корневой сертификат удостоверяющего центра
Минцифры (Russian Trusted Root CA). Им подписан сертификат
`platform-api2.max.ru`, а сервер бота ему не доверяет. Бот добавляет его к
проверке TLS только у клиента MAX (`services/max_bot.py`,
`techspec/27-max.md` §27.2); на сервер ничего не ставится, проверка TLS не
отключается.

| Поле | Значение |
| --- | --- |
| Субъект | `C=RU, O=The Ministry of Digital Development and Communications, CN=Russian Trusted Root CA` |
| Серийный номер | `1000` |
| Действует | 2022-03-01 — 2032-02-27 |
| SHA-256 | `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31` |

Официальный источник — Госуслуги, <https://www.gosuslugi.ru/crt> (файл
`https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`). Этап
027 собирался там, где эти адреса не открывались, поэтому файл взят из копии
на GitHub и принят только потому, что отпечаток SHA-256 совпал с
опубликованным для файла Госуслуг. Сверить самому:

```
openssl x509 -in bot/certs/russian_trusted_root_ca.pem -noout -fingerprint -sha256
```

Тест `test_root_certificate_is_the_one_of_the_ministry` держит этот отпечаток:
подменённый файл ворота не пропустят. Новый корневой Минцифры (срок до 2032
года) — новым файлом, новым отпечатком в тесте и строкой здесь.
