# ag-mcp-gateway

[Русский](#русский) · [English](#english) · [Wiki RU](docs/public/wiki/Home-ru.md) · [Wiki EN](docs/public/wiki/Home.md)

## Русский

Локальный standalone-шлюз объединяет MCP-серверы для нескольких агентов на одной машине. Он показывает каталог и состояние подключений, применяет политику инструментов, объединяет одинаковые одновременные вызовы при явном разрешении и экспортирует технические логи и метрики.

### Установка и первый запуск

1. Возьмите архив `ag-mcp-gateway-VERSION-macos-ARCH.zip` из согласованного канала поставки. Проверьте архитектуру командой `uname -m` и SHA-256 по контрольной сумме поставки. Для поставки x86‑64 на Apple Silicon требуется Rosetta.
2. Распакуйте архив в отдельный каталог. Оставьте исполняемый файл рядом с `_internal`. Python для запуска архива не требуется.
3. Из каталога `ag-mcp-gateway` выполните команды ниже. `demo-profile` создаёт файл один раз. Для повторного запуска выполните только `serve`.

```sh
./ag-mcp-gateway --version
./ag-mcp-gateway demo-profile
./ag-mcp-gateway serve --allow-unauthenticated
```

Откройте [обзор](http://127.0.0.1:18792/) и выберите **Русский** или **English** в шапке. Во втором терминале выполните `./ag-mcp-gateway status`. Для остановки нажмите Ctrl+C в терминале сервера.

Для рабочего профиля скопируйте [подходящий пример](profiles/company.example.json) из каталога `profiles` в `~/.ag-mcp-gateway/profiles/<имя>.json`, согласуйте настройки и запускайте `./ag-mcp-gateway --profile <имя> serve`. Связка ключей macOS имеет приоритет над явно настроенным резервным источником из окружения. При Basic-аутентификации в браузере используйте пользователя `gateway` и токен профиля как пароль. Флаг без авторизации предназначен для локального демо.

### Для агентов

1. Определите имя профиля, среду и порт из задачи. Выполните `./ag-mcp-gateway --profile <имя> status` и проверьте ожидаемые MCP.
2. Сгенерируйте фрагмент командой `./ag-mcp-gateway --profile <имя> config codex`. Для других клиентов доступны `cursor`, `claude`, `opencode`, `deepseek` и `acp`. Проверьте абсолютный путь к бинарнику и подключите фрагмент в своём клиенте.
3. Начните с `tools/list`, продолжите по курсору при необходимости. При включённом MCP Bridge отдельно подключите `config --management codex` для диагностики и разрешённых действий управления.
4. Для разбора ошибки используйте `gateway_read_errors` по trace ID или `./ag-mcp-gateway --profile <имя> logs --limit 20`. Секреты задаются через настройки профиля и хранилище. В чат и журналы передавайте только безопасные диагностические идентификаторы.

[Установка и обновление](docs/public/wiki/Installation-ru.md) · [Работа агентов](docs/public/wiki/Agents-ru.md) · [Эксплуатация](docs/public/wiki/Operations-ru.md) · [Полный справочник](docs/public/wiki/Reference-ru.md)

## English

A local standalone gateway lets several agents use MCP servers through one service on the same machine. It provides a catalog and connection status, tool policies, opt-in coalescing of identical concurrent calls, and a pull interface for logs and metrics.

### Install and run

1. Get `ag-mcp-gateway-VERSION-macos-ARCH.zip` from your approved distribution channel. Match the architecture reported by `uname -m` and verify the published SHA-256. An x86-64 build on Apple Silicon requires Rosetta.
2. Extract to a dedicated directory. Keep the executable next to `_internal`. The archive runs without a separate Python installation.
3. Run the commands below from the extracted `ag-mcp-gateway` directory. Create the demo profile once. On subsequent starts, run only `serve`.

```sh
./ag-mcp-gateway --version
./ag-mcp-gateway demo-profile
./ag-mcp-gateway serve --allow-unauthenticated
```

Open the [overview](http://127.0.0.1:18792/) and choose **English** or **Русский** in the header. Run `./ag-mcp-gateway status` in a second terminal. Press Ctrl+C in the server terminal to stop it.

For a working profile, copy an [example](profiles/company.example.json) from `profiles` to `~/.ag-mcp-gateway/profiles/<name>.json`, configure it with the operator, and run `./ag-mcp-gateway --profile <name> serve`. The macOS Keychain takes precedence over an explicitly configured environment fallback. Browser Basic authentication uses username `gateway` and the profile token as the password. The unauthenticated flag is intended for the local demo.

### For agents

1. Determine the expected profile, environment and port. Run `./ag-mcp-gateway --profile <name> status` and verify the intended backends.
2. Generate a client entry with `./ag-mcp-gateway --profile <name> config codex`. Other targets include `cursor`, `claude`, `opencode`, `deepseek` and `acp`. Confirm the absolute executable path before adding the fragment to your client.
3. Start with `tools/list` and follow the continuation cursor when present. If the operator enabled MCP Bridge, register it separately with `config --management codex` for diagnostics and authorized management.
4. Diagnose failures with `gateway_read_errors` and a trace ID, or run `./ag-mcp-gateway --profile <name> logs --limit 20`. Keep credentials in the configured secret source and use safe diagnostic identifiers in chats and incident notes.

[Installation and upgrades](docs/public/wiki/Installation.md) · [Agent workflows](docs/public/wiki/Agents.md) · [Operations](docs/public/wiki/Operations.md) · [Architecture](docs/public/wiki/Architecture.md)
