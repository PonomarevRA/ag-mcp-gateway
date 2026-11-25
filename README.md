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

На карточке MCP доступны его инструменты, адрес подключения и диагностика. При включённом MCP Bridge в браузерных **Настройках** можно применить политику отдельного инструмента.

### Мок-тесты MCP

[Пример тестового профиля](profiles/mock-tests.example.json) включает Мок-тесты и MCP Bridge. Скопируйте его как `mock-tests.json` в отдельный каталог профилей, задайте локальный токен `AMG_MOCK_TEST_TOKEN` и укажите отдельный каталог состояния.

```sh
./ag-mcp-gateway --profiles ./mock-profiles --state ./mock-state --profile mock-tests serve
```

Откройте [Мок-тесты](http://127.0.0.1:18892/tests). Для Basic-аутентификации используйте пользователя `gateway` и токен профиля. В редакторе задайте синтетические аргументы, фикстуру и ожидаемый результат. Запуск сохраняет ревизию сценария, проверки, ответ и трассу под отдельным идентификатором. Отчёт доступен в UI и в JSON.

Агент может подключить management MCP через `config --management codex` с теми же параметрами профиля. `gateway_test_template` возвращает готовый сценарий и порядок вызовов. `gateway_test_validate` проверяет черновик, `gateway_test_save` сохраняет его по ревизии, `gateway_test_list` показывает сценарии, `gateway_test_run` запускает выбранную ревизию, а `gateway_test_run_get` открывает отчёт по `run_id`. Ошибки формы возвращают код `invalid_test_arguments` и массив `issues`, а ошибки правил сценария при проверке возвращают `valid: false` и такой же массив. В каждой записи есть `code`, `path`, `message` и, где применимо, `hint`. Каждый прогон обращается только к одному локальному синтетическому MCP-процессу.

Для рабочего профиля скопируйте [подходящий пример](profiles/company.example.json) из каталога `profiles` в `~/.ag-mcp-gateway/profiles/<имя>.json`, согласуйте настройки и запускайте `./ag-mcp-gateway --profile <имя> serve`. Связка ключей macOS имеет приоритет над явно настроенным резервным источником из окружения. При Basic-аутентификации в браузере используйте пользователя `gateway` и токен профиля как пароль. Флаг без авторизации предназначен для локального демо.

Рабочие профили и их копии храните в `~/.ag-mcp-gateway/profiles/` или в другом локальном каталоге вне репозитория. Это правило действует для публичного и приватного репозиториев GitHub. В Git входят только проверенные синтетические примеры. Для новых клонов включите [проверку перед отправкой](#profile-publication-check).

### Адрес одного MCP

Клиент с поддержкой Streamable HTTP может подключиться к отдельному серверу по адресу `/mcp/<ключ-профиля>/<backend>`. Встроенный MCP профиля `demo` доступен по `http://127.0.0.1:18792/mcp/demo/demo` и публикует инструмент `echo`. Общий адрес `http://127.0.0.1:18792/mcp/demo` по-прежнему публикует `demo.echo`. В защищённом профиле оба адреса принимают токен профиля в заголовке `Authorization: Bearer …`.

Для клиента со stdio сгенерируйте фрагмент командой `./ag-mcp-gateway --profile demo config --backend demo codex`. Кнопка **Скопировать настройки** в карточке копирует JSON `mcpServers` для текущего профиля и этого MCP. В защищённом профиле замените `<gateway-profile-token>` токеном профиля. Адрес также доступен в `connections[].mcp_url` вывода `status`.

### Для агентов

1. Определите имя профиля, среду и порт из задачи. Выполните `./ag-mcp-gateway --profile <имя> status` и проверьте ожидаемые MCP.
2. Сгенерируйте фрагмент командой `./ag-mcp-gateway --profile <имя> config codex`. Для других клиентов доступны `cursor`, `claude`, `opencode`, `deepseek` и `acp`. Проверьте абсолютный путь к бинарнику и подключите фрагмент в своём клиенте.
3. Начните с `tools/list`, продолжите по курсору при необходимости. При включённом MCP Bridge отдельно подключите `config --management codex` для диагностики и разрешённых действий управления.
4. Для разбора ошибки используйте `gateway_read_errors` по trace ID или `./ag-mcp-gateway --profile <имя> logs --limit 20`. Секреты задаются через настройки профиля и хранилище. В чат и журналы передавайте только безопасные диагностические идентификаторы.

Если цепочка чтения или обработки JSON будет повторяться, переиспользуйте подходящий модуль профиля или сохраните новый для запуска одним вызовом через MCP Bridge. [Порядок работы с модулями](docs/public/wiki/Agents-ru.md#переиспользование-цепочек-mcp)

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

Open the [MCP catalog](http://127.0.0.1:18792/) and use the globe in the header to choose **English** or **Русский**. Run `./ag-mcp-gateway status` in a second terminal. Press Ctrl+C in the server terminal to stop it.

The MCP card shows the connection address and links to the server’s tools and diagnostics. When MCP Bridge is enabled, browser **Settings** lets an operator apply a policy to an individual tool.

### MCP mock tests

Copy the [mock-test profile](profiles/mock-tests.example.json) to a dedicated profiles folder as `mock-tests.json`. Set the local `AMG_MOCK_TEST_TOKEN` and run `./ag-mcp-gateway --profiles ./mock-profiles --state ./mock-state --profile mock-tests serve`. Open [Mock tests](http://127.0.0.1:18892/tests) and sign in as `gateway` with that token. Each run exercises a local synthetic MCP process and stores the scenario revision, checks, response, and trace.

The browser **View** mode opens saved requests and runs their exact revision. **Edit** exposes request arguments, mock responses, checks, schemas, and local hooks. Create or rename collections in the sidebar. Drag a request into a collection or use its **Move request** control. A collection can contain requests for multiple MCP backends.

Start with `gateway_test_template` for a runnable scenario and the validate, save, run, and report sequence. Invalid tool arguments return `invalid_test_arguments` with an `issues` array. Scenario rule failures in `gateway_test_validate` return `valid: false` and an `issues` array. Each issue contains `code`, `path`, `message`, and an optional `hint`. Create a collection with `gateway_test_group_save` and `expected_revision: 0`, pass its `group_id` when creating a request with `gateway_test_save`, then use the `placement_revision` from `gateway_test_list` to move it with `gateway_test_move`. Listing supports `group_id` and `backend` filters. A move keeps the executable request's revision and run links.

#### Version mock collections with Git

Store `mock-catalog.json`, one JSON file per collection under `collections/`, and one per saved request under `requests/`. A request contains its collection ID, arguments, mock responses, checks, schemas, and local hooks. Exported files use stable names and omit local revisions, report history, profile configuration, and gateway credentials. Use synthetic payloads in the definitions.

Stop the selected gateway profile before transferring its catalog. Use `check` in a Git checkout or CI before importing a reviewed branch.

```sh
./ag-mcp-gateway --profiles ./mock-profiles --state ./mock-state --profile mock-tests mock-catalog export ./mcp-mocks
git add mcp-mocks
./ag-mcp-gateway mock-catalog check ./mcp-mocks
./ag-mcp-gateway --profiles ./mock-profiles --state ./mock-state --profile mock-tests mock-catalog import ./mcp-mocks
```

Import updates changed IDs atomically and keeps local requests and collections missing from the Git directory. Export writes the selected profile's definitions into the catalog directory. Import reviewed Git edits before exporting more local edits into that same directory. Both operations skip unchanged entries. Review the resulting diff with `git diff -- mcp-mocks` after a later export.

For a working profile, copy an [example](profiles/company.example.json) from `profiles` to `~/.ag-mcp-gateway/profiles/<name>.json`, configure it with the operator, and run `./ag-mcp-gateway --profile <name> serve`. The macOS Keychain takes precedence over an explicitly configured environment fallback. Browser Basic authentication uses username `gateway` and the profile token as the password. The unauthenticated flag is intended for the local demo.

Keep working profiles and their copies in `~/.ag-mcp-gateway/profiles/` or another local directory outside the checkout. This applies to both the public and private GitHub repositories. Git tracks only reviewed synthetic examples.

<a id="profile-publication-check"></a>

`publication/profile_guard.py` checks outgoing commits when installed as the local Git `pre-push` hook. For a fresh checkout without an existing hook, run:

```sh
hook="$(git rev-parse --git-path hooks/pre-push)"
mkdir -p "$(dirname "$hook")"
test ! -e "$hook" && test ! -L "$hook" && install -m 755 publication/profile_guard.py "$hook"
```

Use `python3 publication/profile_guard.py --audit` to inspect the history reachable from local refs.

### Address for a single MCP server

A Streamable HTTP client can connect to one server at `/mcp/<profile-key>/<backend>`. The built-in MCP in the `demo` profile is available at `http://127.0.0.1:18792/mcp/demo/demo` and exposes `echo`. The aggregate address `http://127.0.0.1:18792/mcp/demo` still exposes `demo.echo`. For a protected profile, both addresses accept the profile token in the `Authorization: Bearer …` header.

For a stdio client, generate an entry with `./ag-mcp-gateway --profile demo config --backend demo codex`. Use **Copy settings** on the MCP card to copy an `mcpServers` JSON entry for the active profile and that MCP. For protected profiles, replace `<gateway-profile-token>` with the profile token. The address is also available in `connections[].mcp_url` in the `status` output.

### For agents

1. Determine the expected profile, environment and port. Run `./ag-mcp-gateway --profile <name> status` and verify the intended backends.
2. Generate a client entry with `./ag-mcp-gateway --profile <name> config codex`. Other targets include `cursor`, `claude`, `opencode`, `deepseek` and `acp`. Confirm the absolute executable path before adding the fragment to your client.
3. Start with `tools/list` and follow the continuation cursor when present. If the operator enabled MCP Bridge, register it separately with `config --management codex` for diagnostics and authorized management.
4. Diagnose failures with `gateway_read_errors` and a trace ID, or run `./ag-mcp-gateway --profile <name> logs --limit 20`. Keep credentials in the configured secret source and use safe diagnostic identifiers in chats and incident notes.

For a recurring read-only workflow or JSON transformation, reuse a matching profile module or save a new one that runs in one MCP Bridge call. [Module workflow](docs/public/wiki/Agents.md#reuse-mcp-workflows)

[Installation and upgrades](docs/public/wiki/Installation.md) · [Agent workflows](docs/public/wiki/Agents.md) · [Operations](docs/public/wiki/Operations.md) · [Architecture](docs/public/wiki/Architecture.md)
