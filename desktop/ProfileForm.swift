import AppKit

@MainActor
final class ProfileForm: NSObject, NSPopoverDelegate {
    private enum Step { case profile, connection }
    private struct ValidationIssue: LocalizedError {
        let field: NSView
        let message: String
        var errorDescription: String? { message }
    }

    /// A column whose rows either fill the available width or keep their natural size.
    private final class FormColumn: NSStackView {
        init(spacing: CGFloat) {
            super.init(frame: .zero)
            orientation = .vertical
            alignment = .leading
            distribution = .fill
            self.spacing = spacing
        }

        required init?(coder: NSCoder) { nil }

        func add(_ row: NSView, fill: Bool = true) {
            addArrangedSubview(row)
            if fill {
                row.widthAnchor.constraint(equalTo: widthAnchor).isActive = true
            }
        }
    }

    private final class FormDocument: NSView {
        override var isFlipped: Bool { true }
    }

    private final class HelpContent: NSView {
        var dismiss: (() -> Void)?
        override var acceptsFirstResponder: Bool { true }
        override func cancelOperation(_ sender: Any?) { dismiss?() }
    }

    private static let sheetWidth: CGFloat = 550
    private static let inset: CGFloat = 24
    private static let columnWidth = sheetWidth - 2 * inset
    private weak var parentWindow: NSWindow?
    private let profile: Profile?
    private var step: Step
    private let panel = NSPanel(contentRect: .zero, styleMask: [.titled], backing: .buffered, defer: false)
    private let name = NSTextField(string: "")
    private let environment = NSPopUpButton()
    private let port = NSTextField(string: "")
    private let server = NSTextField(string: "")
    private let transport = NSSegmentedControl(labels: ["HTTP", "stdio"], trackingMode: .selectOne,
                                               target: nil, action: nil)
    private let url = NSTextField(string: "")
    private let executable = NSTextField(string: "")
    private let arguments = NSTextView()
    private let argumentFormat = NSPopUpButton()
    private let auth = NSPopUpButton()
    private let tokenSource = NSPopUpButton()
    private let keychain = NSTextField(string: "")
    private let token = NSSecureTextField(string: "")
    private let portDisclosure = NSButton()
    private let helpPopover = NSPopover()
    private weak var helpButton: NSButton?
    private var problem: Error?
    private weak var invalidGroup: NSView?
    private var advanced = false
    private var saving = false
    var isSaving: Bool { saving }
    private var saved: Profile?
    private var save: ((Data) async throws -> Profile)?

    init(profile: Profile?) {
        self.profile = profile
        step = profile == nil ? .profile : .connection
        super.init()
        configureFields()
        helpPopover.behavior = .transient
        helpPopover.animates = false
        helpPopover.delegate = self
    }

    /// Present one attached sheet, with state retained through back navigation and save errors.
    func present(on window: NSWindow, save: @escaping (Data) async throws -> Profile,
                 finish: @escaping (Profile?) -> Void) {
        self.save = save
        parentWindow = window
        panel.title = profile == nil ? localized("Создать профиль", "Create profile") : localized("Добавить MCP", "Add MCP")
        panel.isReleasedWhenClosed = false
        render(focus: step == .profile ? name : server)
        window.beginSheet(panel) { [self] _ in
            helpPopover.close()
            finish(saved)
        }
    }

    private func configureFields() {
        environment.addItems(withTitles: [localized("Тестовая", "Test"), localized("Рабочая", "Production"),
                                          localized("Локальная", "Local")])
        environment.target = self
        environment.action = #selector(environmentChanged)
        argumentFormat.addItems(withTitles: [localized("По одному на строку", "One per line"), "JSON"])
        auth.addItems(withTitles: [localized("Без авторизации", "None"), "Basic", "Bearer"])
        tokenSource.addItems(withTitles: [localized("Новый токен", "New token"),
                                          localized("Существующая запись", "Existing Keychain item")])
        for control in [transport, auth, tokenSource] as [NSControl] {
            control.target = self
            control.action = #selector(changeFields(_:))
        }
        transport.selectedSegment = 0
        transport.setAccessibilityLabel(localized("Транспорт", "Transport"))
        name.placeholderString = "company-test"
        port.placeholderString = "18793"
        server.placeholderString = "docs"
        url.placeholderString = "https://mcp.example.invalid/mcp"
        executable.placeholderString = "/usr/local/bin/mcp-server"
        keychain.placeholderString = "company.docs"
        arguments.frame = NSRect(x: 0, y: 0, width: Self.columnWidth, height: 72)
        arguments.minSize = NSSize(width: 0, height: 72)
        arguments.isVerticallyResizable = true
        arguments.isHorizontallyResizable = false
        arguments.autoresizingMask = [.width]
        arguments.textContainer?.widthTracksTextView = true
        arguments.isRichText = false
        arguments.isAutomaticQuoteSubstitutionEnabled = false
        arguments.font = .monospacedSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        arguments.textContainerInset = NSSize(width: 5, height: 4)
        arguments.setAccessibilityLabel(localized("Аргументы запуска", "Launch arguments"))
        portDisclosure.target = self
        portDisclosure.action = #selector(toggleAdvanced)
        portDisclosure.bezelStyle = .regularSquare
        portDisclosure.isBordered = false
        portDisclosure.setButtonType(.momentaryChange)
        portDisclosure.imagePosition = .imageLeading
        portDisclosure.heightAnchor.constraint(greaterThanOrEqualToConstant: 28).isActive = true
    }

    /// Rebuild the visible groups, keeping the controls that hold the draft and its focus.
    private func render(focus: NSView? = nil) {
        helpPopover.close()
        for control in [name, environment, port, server, transport, url, executable, argumentFormat,
                        auth, tokenSource, keychain, token, portDisclosure] as [NSControl] {
            control.isEnabled = !saving
            control.setAccessibilityHelp(nil)
        }
        arguments.isEditable = !saving
        arguments.setAccessibilityHelp(nil)
        invalidGroup = nil
        let body = FormColumn(spacing: 14)
        let header = heading()
        body.add(header)
        body.setCustomSpacing(20, after: header)
        if step == .profile { profileFields(into: body) } else { connectionFields(into: body) }
        let footer = FormColumn(spacing: 8)
        // A failed save must remain visible even when the fields need to scroll.
        if let problem, !(problem is ValidationIssue) {
            footer.add(errorLabel(problem.localizedDescription))
        }
        footer.add(actions())
        installLayout(body: body, footer: footer)

        panel.initialFirstResponder = focus ?? (step == .profile ? name : server)
        if let focus {
            panel.makeFirstResponder(focus)
            let visibleGroup = invalidGroup ?? focus
            visibleGroup.scrollToVisible(visibleGroup.bounds.insetBy(dx: -4, dy: -4))
        }
    }

    /// Fit the sheet to its contents. In short windows only the form body scrolls.
    private func installLayout(body: FormColumn, footer: FormColumn) {
        body.widthAnchor.constraint(equalToConstant: Self.columnWidth).isActive = true
        footer.widthAnchor.constraint(equalToConstant: Self.columnWidth).isActive = true
        let bodyHeight = ceil(body.fittingSize.height)
        let footerHeight = ceil(footer.fittingSize.height)
        let documentHeight = bodyHeight + 36
        let footerArea = footerHeight + 28
        let parentHeight = parentWindow?.contentLayoutRect.height ?? 650
        let height = min(documentHeight + footerArea, max(240, parentHeight - 16))

        let document = FormDocument(frame: NSRect(x: 0, y: 0, width: Self.sheetWidth, height: documentHeight))
        body.frame = NSRect(x: Self.inset, y: 20, width: Self.columnWidth, height: bodyHeight)
        document.addSubview(body)
        let scroll = NSScrollView(frame: .zero)
        scroll.drawsBackground = false
        scroll.scrollerStyle = .overlay
        scroll.hasVerticalScroller = documentHeight > height - footerArea
        scroll.autohidesScrollers = true
        scroll.documentView = document

        let content = NSView(frame: NSRect(x: 0, y: 0, width: Self.sheetWidth, height: height))
        for view in [scroll, footer] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: content.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -12),
            footer.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: Self.inset),
            footer.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -Self.inset),
            footer.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
        ])
        panel.setContentSize(NSSize(width: Self.sheetWidth, height: height))
        panel.contentView = content
        content.layoutSubtreeIfNeeded()
    }

    private func heading() -> NSView {
        let group = FormColumn(spacing: 4)
        if profile == nil {
            group.add(note(step == .profile ? localized("Шаг 1 из 2", "Step 1 of 2")
                                                           : localized("Шаг 2 из 2", "Step 2 of 2")))
        }
        let title = wrappingLabel(step == .profile ? localized("Профиль и среда", "Profile and environment")
            : (profile == nil ? localized("Первый MCP", "First MCP server") : localized("Подключение MCP", "MCP connection")))
        title.font = .systemFont(ofSize: 19, weight: .semibold)
        group.add(title)
        if step == .profile {
            group.add(note(localized("На следующем шаге добавите первый MCP.",
                                                    "On the next step, add your first MCP server.")))
        } else if let profile {
            group.add(note(localized("Профиль: \(profile.key)", "Profile: \(profile.key)")))
        }
        return group
    }

    /// Keep the optional port directly after its disclosure.
    private func profileFields(into group: FormColumn) {
        group.add(field(localized("Имя профиля", "Profile name"), name,
                            help: localized("Имя отличает профиль в каталоге. До 32 латинских букв, цифр, дефисов или подчёркиваний.",
                                            "Use a name that identifies the profile in the catalog. Up to 32 Latin letters, digits, hyphens or underscores.")))
        group.add(field(localized("Среда", "Environment"), environment,
                            help: localized("Тестовая — для проверки перед запуском. Рабочая — для реальных подключений. Локальная — для работы без метки среды.",
                                            "Test is for trying a setup. Production is for live connections. Local leaves the profile untagged.")))
        portDisclosure.removeFromSuperview()
        portDisclosure.title = advanced ? localized("Скрыть порт", "Hide port") : localized("Настроить порт", "Customize port")
        portDisclosure.image = NSImage(systemSymbolName: advanced ? "chevron.down" : "chevron.right",
                                       accessibilityDescription: nil)
        portDisclosure.setAccessibilityLabel(portDisclosure.title)
        portDisclosure.setAccessibilityExpanded(advanced)
        group.add(portDisclosure, fill: false)
        if advanced { group.add(field(localized("Порт (необязательно)", "Port (optional)"), port,
                            help: localized("Порт, по которому клиенты подключаются к шлюзу на этом компьютере. Оставьте пустым для порта по умолчанию или укажите свободный порт от 1024 до 65535.",
                                            "Clients connect to the gateway on this port. Leave blank for the default or enter an unused port from 1024 to 65535."))) }
    }

    /// Show the fields for the chosen transport and authentication method in reading order.
    private func connectionFields(into group: FormColumn) {
        group.add(field(localized("Имя MCP", "MCP name"), server,
                            help: localized("Имя подключения в каталоге этого профиля. Оно должно отличаться от имён других MCP в профиле.",
                                            "This name identifies the connection in the profile’s catalog. Use a different name for each MCP server in the profile.")))
        group.add(field(localized("Транспорт", "Transport"), transport,
                            help: localized("HTTP — если сервер доступен по URL. stdio — если шлюз должен запустить сервер как локальную программу.",
                                            "Choose HTTP for a server with a URL. Choose stdio when the gateway should launch a local program."), fill: false))
        if transport.selectedSegment == 0 {
            group.add(field(localized("Адрес Streamable HTTP", "Streamable HTTP URL"), url,
                            help: localized("Укажите полный адрес MCP-сервера, включая путь, например /mcp. Для удалённого сервера используйте HTTPS. HTTP разрешён для адресов этого компьютера.",
                                            "Enter the full MCP server URL, including its path, such as /mcp. Use HTTPS for a remote server. HTTP is allowed for loopback addresses.")))
            group.add(field(localized("Авторизация", "Authentication"), auth,
                            help: localized("Без авторизации — для сервера без учётных данных. Basic — для имени пользователя и пароля. Bearer — для токена доступа.",
                                            "Choose None when the server needs no credentials. Basic uses a username and password. Bearer uses an access token.")))
            switch auth.indexOfSelectedItem {
            case 1:
                group.add(note(localized("После запуска введите логин и пароль на странице «Учётные данные MCP».",
                                                        "After launch, enter the username and password on the MCP credentials page.")))
            case 2:
                group.add(field(localized("Источник токена", "Token source"), tokenSource,
                            help: localized("Выберите «Новый токен», чтобы сохранить его в Связке ключей, или «Существующая запись», чтобы использовать сохранённый токен.",
                                            "Choose New token to store it in Keychain, or Existing Keychain item to use a stored token.")))
                group.add(tokenSource.indexOfSelectedItem == 0
                    ? field(localized("Токен · сохранится в Связке ключей", "Token · stored in Keychain"), token,
                            help: localized("Введите токен доступа MCP-сервера без префикса Bearer.",
                                            "Enter the MCP server’s access token without the Bearer prefix."))
                    : field(localized("Имя записи в Связке ключей", "Keychain item name"), keychain,
                            help: localized("Укажите имя существующей записи шлюза, в которой уже хранится токен этого MCP-сервера.",
                                            "Enter the name of the existing gateway Keychain item that holds this MCP server’s token.")))
            default:
                break
            }
        } else {
            executable.removeFromSuperview()
            let choose = NSButton(title: localized("Выбрать…", "Choose…"), target: self, action: #selector(chooseExecutable))
            choose.isEnabled = !saving
            let pathRow = NSStackView(views: [executable, choose])
            pathRow.alignment = .centerY
            pathRow.spacing = 8
            executable.setContentHuggingPriority(.defaultLow, for: .horizontal)
            executable.setAccessibilityLabel(localized("Исполняемый файл", "Executable"))
            group.add(field(localized("Доверенный исполняемый файл", "Trusted executable"), pathRow,
                            help: localized("Выберите программу, которую шлюз запустит как MCP-сервер. Укажите абсолютный путь к исполняемому файлу.",
                                            "Choose the program the gateway should launch as the MCP server. Enter the full path to the executable."), input: executable))
            let scroll = NSScrollView()
            scroll.borderType = .bezelBorder
            scroll.hasVerticalScroller = true
            scroll.heightAnchor.constraint(equalToConstant: 80).isActive = true
            arguments.removeFromSuperview()
            scroll.documentView = arguments
            group.add(field(localized("Формат аргументов", "Argument format"), argumentFormat,
                            help: localized("«По одному на строку» — каждый аргумент на отдельной строке. JSON — массив строк в квадратных скобках.",
                                            "One per line treats each line as a separate argument. JSON expects an array of strings in square brackets.")))
            group.add(field(localized("Аргументы запуска", "Launch arguments"), scroll,
                            help: localized("Параметры, которые шлюз передаст программе при запуске. Если они не нужны, оставьте поле пустым.",
                                            "These values are passed to the program at launch. Leave blank if the server needs no arguments."), input: arguments))
        }
        group.add(note(localized("После сохранения запустите профиль. Для работающего профиля потребуется перезапуск.",
                                                "Start the profile after saving. A running profile needs a restart.")))
    }

    private func actions() -> NSStackView {
        let cancel = NSButton(title: localized("Отмена", "Cancel"), target: self, action: #selector(close))
        cancel.keyEquivalent = "\u{1b}"
        let primaryLabel = step == .profile ? localized("Продолжить", "Continue")
            : (saving ? localized("Сохраняется…", "Saving…") : localized("Сохранить", "Save"))
        let primary = NSButton(title: primaryLabel, target: self, action: #selector(next))
        primary.keyEquivalent = "\r"
        primary.bezelStyle = .rounded
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        var buttons: [NSView] = [spacer, cancel, primary]
        if profile == nil && step == .connection {
            let back = NSButton(title: localized("Назад", "Back"), target: self, action: #selector(goBack))
            back.isEnabled = !saving
            buttons.insert(back, at: 0)
        }
        cancel.isEnabled = !saving
        primary.isEnabled = !saving
        let row = NSStackView(views: buttons)
        row.alignment = .centerY
        row.spacing = 8
        return row
    }

    /// Give wrapping labels the actual column width before measuring the sheet.
    private func wrappingLabel(_ message: String) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: message)
        label.alignment = .left
        label.preferredMaxLayoutWidth = Self.columnWidth
        return label
    }

    private func note(_ message: String) -> NSTextField {
        let label = wrappingLabel(message)
        label.textColor = .secondaryLabelColor
        return label
    }

    private func errorLabel(_ message: String) -> NSTextField {
        let label = wrappingLabel(message)
        label.textColor = .systemRed
        label.setAccessibilityLabel(localized("Ошибка формы", "Form error"))
        return label
    }

    /// Align a caption, its control and its validation message in the same column.
    private func field(_ title: String, _ control: NSView, help: String,
                       input: NSView? = nil, fill: Bool = true) -> NSView {
        control.removeFromSuperview()
        let input = input ?? control
        if input === control { input.setAccessibilityLabel(title) }
        let label = wrappingLabel(title)
        label.font = .systemFont(ofSize: NSFont.systemFontSize, weight: .medium)
        let caption = NSStackView(views: [label, makeHelpButton(title: title, help: help)])
        caption.alignment = .centerY
        caption.spacing = 4
        let group = FormColumn(spacing: 4)
        group.add(caption, fill: false)
        group.add(control, fill: fill)
        input.setAccessibilityHelp(help)
        if let issue = problem as? ValidationIssue, issue.field === input {
            group.add(errorLabel(issue.message))
            input.setAccessibilityHelp(issue.message)
            invalidGroup = group
        }
        return group
    }

    /// Pair a named info action with each visible field label.
    private func makeHelpButton(title: String, help: String) -> NSButton {
        let button = NSButton(title: title, target: self, action: #selector(showFieldHelp(_:)))
        button.image = NSImage(systemSymbolName: "info.circle", accessibilityDescription: nil)
        button.imagePosition = .imageOnly
        button.isBordered = false
        button.isEnabled = !saving
        button.toolTip = help
        button.setAccessibilityLabel(localized("Справка о поле «\(title)»", "Help for \(title)"))
        button.setAccessibilityExpanded(false)
        NSLayoutConstraint.activate([
            button.widthAnchor.constraint(equalToConstant: 28),
            button.heightAnchor.constraint(equalToConstant: 28),
        ])
        return button
    }

    /// Show field guidance without changing any of the form's values.
    @objc private func showFieldHelp(_ sender: NSButton) {
        if helpPopover.isShown, helpButton === sender {
            dismissFieldHelp()
            return
        }
        helpPopover.close()
        guard let help = sender.toolTip else { return }

        let label = wrappingLabel(help)
        label.preferredMaxLayoutWidth = 272
        label.frame.size.width = 272
        let height = ceil(label.cell?.cellSize(forBounds: NSRect(x: 0, y: 0, width: 272, height: 400)).height
                          ?? label.fittingSize.height)
        let content = HelpContent(frame: NSRect(x: 0, y: 0, width: 304, height: height + 32))
        label.frame = NSRect(x: 16, y: 16, width: 272, height: height)
        content.addSubview(label)
        content.dismiss = { [weak self] in self?.dismissFieldHelp() }
        let controller = NSViewController()
        controller.view = content
        helpPopover.contentViewController = controller
        helpPopover.contentSize = content.frame.size
        helpButton = sender
        sender.setAccessibilityExpanded(true)
        helpPopover.show(relativeTo: sender.bounds, of: sender, preferredEdge: .maxX)
    }

    private func dismissFieldHelp() {
        let button = helpButton
        helpPopover.close()
        if let button, button.window === panel { panel.makeFirstResponder(button) }
    }

    func popoverDidShow(_ notification: Notification) {
        if let content = helpPopover.contentViewController?.view {
            content.window?.makeFirstResponder(content)
        }
    }

    func popoverDidClose(_ notification: Notification) {
        helpButton?.setAccessibilityExpanded(false)
        helpButton = nil
    }

    @objc private func environmentChanged() {
        port.placeholderString = ["18793", "18794", "18792"][environment.indexOfSelectedItem]
    }

    @objc private func toggleAdvanced() {
        advanced.toggle()
        if !advanced, (problem as? ValidationIssue)?.field === port { problem = nil }
        render(focus: advanced ? port : portDisclosure)
    }

    @objc private func changeFields(_ sender: NSControl) {
        problem = nil
        render(focus: sender)
    }

    @objc private func chooseExecutable() {
        let chooser = NSOpenPanel()
        chooser.canChooseDirectories = false
        chooser.allowsMultipleSelection = false
        chooser.beginSheetModal(for: panel) { [weak self] result in
            guard result == .OK, let path = chooser.url?.path else { return }
            self?.executable.stringValue = path
        }
    }

    @objc private func goBack() {
        step = .profile
        problem = nil
        render(focus: name)
    }

    @objc private func close() {
        guard !saving else { return }
        panel.sheetParent?.endSheet(panel, returnCode: .cancel)
    }

    @objc private func next() {
        guard !saving else { return }
        do {
            if step == .profile {
                _ = try profileValues()
                step = .connection
                problem = nil
                render(focus: server)
                return
            }
            let payload = try request()
            guard let save else { return }
            saving = true
            problem = nil
            render()
            Task {
                do {
                    saved = try await save(payload)
                    panel.sheetParent?.endSheet(panel, returnCode: .OK)
                } catch {
                    saving = false
                    showError(error)
                }
            }
        } catch { showError(error) }
    }

    /// Keep the sheet open, restore editing focus, and announce the error.
    private func showError(_ problem: Error) {
        self.problem = problem
        let invalidField = (problem as? ValidationIssue)?.field
        render(focus: invalidField ?? (step == .profile ? name : server))
        NSAccessibility.post(element: NSApplication.shared, notification: .announcementRequested, userInfo: [
            .announcement: problem.localizedDescription,
            .priority: NSAccessibilityPriorityLevel.high.rawValue,
        ])
    }

    /// Validate the identifier and associate any error with its input.
    private func identifier(_ input: NSTextField, message: String) throws -> String {
        let value = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.range(of: "^[A-Za-z0-9_-]{1,32}$", options: .regularExpression) != nil else {
            throw ValidationIssue(field: input, message: message)
        }
        return value
    }

    /// Preserve the existing profile revision or validate the new profile's identity and port.
    private func profileValues() throws -> [String: Any] {
        if let profile { return ["name": profile.name, "revision": profile.revision] }
        var values: [String: Any] = ["name": try identifier(name, message: localized(
            "Имя профиля — до 32 латинских букв, цифр, дефисов или подчёркиваний.",
            "Use up to 32 letters, numbers, hyphens or underscores for the profile name."))]
        if environment.indexOfSelectedItem < 2 { values["environment"] = ["test", "production"][environment.indexOfSelectedItem] }
        if advanced && !port.stringValue.isEmpty {
            guard let value = Int(port.stringValue), (1024...65535).contains(value) else {
                throw ValidationIssue(field: port,
                                      message: localized("Укажите порт от 1024 до 65535.", "Enter a port from 1024 to 65535."))
            }
            values["port"] = value
        }
        return values
    }

    /// Validate the active transport and serialize its profile mutation request.
    private func request() throws -> Data {
        var payload = try profileValues()
        payload["backend_name"] = try identifier(server, message: localized(
            "Имя MCP — до 32 латинских букв, цифр, дефисов или подчёркиваний.",
            "Use up to 32 letters, numbers, hyphens or underscores for the MCP name."))
        if transport.selectedSegment == 0 {
            let endpoint = url.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            let components = URLComponents(string: endpoint)
            let host = components?.host?.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]")) ?? ""
            let scheme = components?.scheme?.lowercased()
            let local = ["127.0.0.1", "::1", "localhost"].contains(host)
            guard !host.isEmpty, components?.user == nil, components?.password == nil,
                  components?.query == nil, components?.fragment == nil,
                  scheme == "https" || (scheme == "http" && local) else {
                throw ValidationIssue(field: url, message: localized(
                    "Укажите HTTPS-адрес или локальный HTTP-адрес без реквизитов и параметров.",
                    "Enter an HTTPS URL or a loopback HTTP URL without credentials or parameters."))
            }
            var backend: [String: Any] = ["transport": "http", "url": endpoint]
            if auth.indexOfSelectedItem == 1 { backend["basic_keychain"] = true }
            if auth.indexOfSelectedItem == 2 {
                if tokenSource.indexOfSelectedItem == 0 {
                    guard !token.stringValue.isEmpty else {
                        throw ValidationIssue(field: token, message: localized("Введите токен.", "Enter a token."))
                    }
                    payload["bearer_secret"] = token.stringValue
                } else {
                    guard !keychain.stringValue.isEmpty else {
                        throw ValidationIssue(field: keychain,
                                              message: localized("Укажите имя записи в Связке ключей.", "Enter the Keychain item name."))
                    }
                    backend["bearer_token_keychain"] = keychain.stringValue
                }
            }
            payload["backend"] = backend
        } else {
            let path = executable.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard path.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: path) else {
                throw ValidationIssue(field: executable, message: localized(
                    "Выберите исполняемый файл по абсолютному пути.", "Choose an executable by absolute path."))
            }
            let args = try launchArguments()
            payload["backend"] = ["transport": "stdio", "command": path, "args": args]
        }
        return try JSONSerialization.data(withJSONObject: payload)
    }

    /// Parse arguments in the selected format without altering their order.
    private func launchArguments() throws -> [String] {
        let text = arguments.string
        if text.isEmpty { return [] }
        if argumentFormat.indexOfSelectedItem == 0 {
            return text.components(separatedBy: .newlines).filter { !$0.isEmpty }
        }
        guard let values = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String] else {
            throw ValidationIssue(field: arguments,
                                  message: localized("Введите JSON-массив строк.", "Enter a JSON array of strings."))
        }
        return values
    }
}
