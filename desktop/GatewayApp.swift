import AppKit
import Darwin
import WebKit

struct Profile: Decodable {
    let name: String
    let key: String
    let port: Int
    let protected: Bool
    let demo: Bool
    let backends: [String]
    let revision: String
    let restarted: Bool?

    var origin: URL { URL(string: "http://127.0.0.1:\(port)/")! }
    var status: URL { origin.appendingPathComponent("api/status/\(key)") }
}

private struct Inventory: Decodable {
    let profiles: [Profile]
    let unavailable: Int
}

struct GatewayError: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

private let russian = Locale.preferredLanguages.first?.hasPrefix("ru") == true

func localized(_ ru: String, _ en: String) -> String { russian ? ru : en }

private final class DesktopInstance {
    static let reopen = Notification.Name("local.ag-mcp-gateway.desktop.reopen")
    private let descriptor: Int32

    private init(descriptor: Int32) {
        self.descriptor = descriptor
    }

    deinit { close(descriptor) }

    /// Hold one lock per OS user across releases. A secondary launch only requests window activation.
    static func acquire() throws -> DesktopInstance? {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ag-mcp-gateway")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let path = directory.appendingPathComponent(".desktop.lock").path
        let descriptor = open(path, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard descriptor >= 0 else {
            throw GatewayError(localized("Не удалось открыть блокировку окна шлюза.", "Could not open the gateway window lock."))
        }
        if flock(descriptor, LOCK_EX | LOCK_NB) == 0 {
            return DesktopInstance(descriptor: descriptor)
        }
        let busy = errno == EWOULDBLOCK
        close(descriptor)
        guard busy else {
            throw GatewayError(localized("Не удалось получить блокировку окна шлюза.", "Could not acquire the gateway window lock."))
        }
        DistributedNotificationCenter.default().postNotificationName(reopen, object: nil, userInfo: nil,
                                                                      deliverImmediately: true)
        return nil
    }
}

/// Execute a bounded local CLI operation. Configuration and tokens use pipes.
private func command(_ binary: URL, _ arguments: [String], input: Data? = nil) async throws -> Data {
    try await Task.detached(priority: .userInitiated) {
        let task = Process()
        let output = Pipe()
        let request = Pipe()
        task.executableURL = binary
        task.arguments = arguments
        task.standardOutput = output
        task.standardError = output
        task.standardInput = input == nil ? FileHandle.nullDevice : request
        try task.run()
        if let input {
            try request.fileHandleForWriting.write(contentsOf: input)
            try request.fileHandleForWriting.close()
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        guard task.terminationStatus == 0 else {
            let detail = String(decoding: data.prefix(600), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            throw GatewayError(detail.isEmpty ? localized("Команда шлюза завершилась с ошибкой.", "Gateway command failed.") : detail)
        }
        return data
    }.value
}

private extension NSToolbarItem.Identifier {
    static let gatewayProfile = Self("gateway.profile")
    static let gatewayStart = Self("gateway.start")
    static let gatewayAdd = Self("gateway.add")
    static let gatewayReload = Self("gateway.reload")
    static let gatewayLanguage = Self("gateway.language")
}

@MainActor
private final class GatewayTitlebar: NSTitlebarAccessoryViewController {
    let back: NSButton
    let forward: NSButton
    let sidebar: NSButton
    private let divider = NSBox()
    private let mark = NSImageView()
    private let titleLabel = NSTextField(labelWithString: "ag-mcp-gateway")
    private let status = NSTextField(labelWithString: "")
    private var sidebarEdge: CGFloat = 0

    /// Add the navigation group and context after the real macOS traffic lights.
    init(back: NSButton, forward: NSButton, sidebar: NSButton) {
        self.back = back
        self.forward = forward
        self.sidebar = sidebar
        super.init(nibName: nil, bundle: nil)
        view = NSView(frame: NSRect(x: 0, y: 0, width: 380, height: 38))
        layoutAttribute = .left
        for button in [back, forward, sidebar] {
            button.translatesAutoresizingMaskIntoConstraints = true
            view.addSubview(button)
        }
        divider.boxType = .custom
        divider.borderWidth = 0
        divider.fillColor = .separatorColor
        mark.image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { bounds in
            GatewayMark.draw(in: bounds, template: false)
            return true
        }
        mark.setAccessibilityElement(false)
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        status.font = .systemFont(ofSize: 11)
        status.textColor = .secondaryLabelColor
        for label in [titleLabel, status] {
            label.lineBreakMode = .byTruncatingTail
            label.maximumNumberOfLines = 1
        }
        for element in [divider, mark, titleLabel, status] { view.addSubview(element) }
    }

    required init?(coder: NSCoder) { nil }

    /// Set the localized section name sent by the current workspace page.
    func update(title text: String, sidebarEdge: CGFloat) {
        titleLabel.stringValue = text.isEmpty ? "ag-mcp-gateway" : text
        titleLabel.toolTip = titleLabel.stringValue
        self.sidebarEdge = sidebarEdge
        align()
    }

    func update(status text: String) {
        status.stringValue = text
        status.toolTip = text
    }

    /// Align with the measured sidebar edge when it leaves room for the system controls.
    func align() {
        let origin = view.convert(view.bounds, to: nil).minX
        let available = sidebarEdge - origin
        let navigationWidth = max(94, available)
        let height = view.frame.height
        view.setFrameSize(NSSize(width: navigationWidth + 276, height: height))
        let y = (height - 28) / 2
        back.frame = NSRect(x: 0, y: y, width: 28, height: 28)
        forward.frame = NSRect(x: 30, y: y, width: 28, height: 28)
        sidebar.frame = NSRect(x: navigationWidth - 36, y: y, width: 32, height: 28)
        divider.frame = NSRect(x: navigationWidth - 1, y: 0, width: 1, height: height)
        divider.isHidden = available < 94
        mark.frame = NSRect(x: navigationWidth + 10, y: (height - 18) / 2, width: 18, height: 18)
        // Give longer section names more room while keeping a slot for profile status.
        let titleWidth = min(168, max(116, ceil(titleLabel.intrinsicContentSize.width)))
        titleLabel.frame = NSRect(x: navigationWidth + 36, y: (height - 18) / 2, width: titleWidth, height: 18)
        status.frame = NSRect(x: navigationWidth + 42 + titleWidth, y: (height - 16) / 2,
                              width: 234 - titleWidth, height: 16)
    }
}

@MainActor
private final class Desktop: NSObject, NSApplicationDelegate, NSWindowDelegate, NSToolbarDelegate, WKNavigationDelegate {
    private let binary = Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent("ag-mcp-gateway")
    private var selectedName: String?
    private var titlebar: GatewayTitlebar?
    private var reloadButton: NSButton?
    private var languageButton: NSButton?
    private var pageLanguage = ""
    private var pageGeneration = 0
    private var profileButton: NSButton?
    private var startButton: NSButton?
    private var profilePromptButton: NSButton?
    private var addButton: NSButton?
    private var sidebarReady = false
    private var sidebarPending = false
    private var web = WKWebView()
    private var window: NSWindow!
    private var tray: NSStatusItem!
    private var profileForm: ProfileForm?
    private let trayStatus = NSMenuItem()
    private var profiles: [Profile] = []
    private var children: [String: Process] = [:]
    private var active: Profile?
    private var attachedPID: String?
    private var initialNavigation: WKNavigation?
    private var pending: Set<String> = []
    private var stopping: Set<String> = []
    private var starting: String? {
        didSet { updateToolbar() }
    }
    private var startingProcess: Process?
    private var cancelStart = false
    private var busy = false {
        didSet { updateToolbar() }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        buildMenu()
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(reopenWindow(_:)),
                                                              name: DesktopInstance.reopen, object: nil,
                                                              suspensionBehavior: .deliverImmediately)
        showWindow()
        Task {
            await refreshProfiles(preferred: UserDefaults.standard.string(forKey: "selectedProfile"))
            if let profile = selected, active == nil {
                await start(profile, reuseDemo: true, launchIfStopped: false)
            }
        }
    }

    /// Keep the window and the system menu as distinct, reusable controls.
    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1110, height: 750),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "ag-mcp-gateway"
        window.minSize = NSSize(width: 745, height: 480)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        let toolbar = NSToolbar(identifier: "gateway.window")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        window.toolbarStyle = .unifiedCompact
        window.titleVisibility = .hidden
        window.toolbar = toolbar
        installTitlebar()
        showProfilePrompt()
        describe(localized("Загрузка профилей…", "Loading profiles…"))
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.flexibleSpace, .gatewayProfile, .gatewayStart, .gatewayAdd, .gatewayReload, .gatewayLanguage]
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(toolbar)
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier identifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        let item = NSToolbarItem(itemIdentifier: identifier)
        switch identifier {
        case .gatewayProfile:
            item.label = localized("Профиль", "Profile")
            profileButton = toolbarButton("square.stack", label: item.label, action: #selector(showProfileMenu(_:)))
            item.view = profileButton
        case .gatewayStart:
            item.label = localized("Запустить профиль", "Start profile")
            startButton = toolbarButton("play", label: item.label, action: #selector(startOrCancel))
            item.view = startButton
        case .gatewayAdd:
            item.label = localized("Добавить", "Add")
            addButton = toolbarButton("plus", label: item.label, action: #selector(showAddMenu(_:)))
            item.view = addButton
        case .gatewayReload:
            item.label = localized("Обновить", "Reload")
            reloadButton = toolbarButton("arrow.clockwise", label: item.label, action: #selector(reloadPage))
            item.view = reloadButton
        case .gatewayLanguage:
            item.label = localized("Язык интерфейса", "Interface language")
            languageButton = toolbarButton("globe", label: item.label, action: #selector(showLanguageMenu(_:)))
            item.view = languageButton
        default:
            return nil
        }
        updateToolbar()
        return item
    }

    private func installTitlebar() {
        let back = iconButton("chevron.left", label: localized("Назад", "Back"), action: #selector(goBack))
        let forward = iconButton("chevron.right", label: localized("Вперёд", "Forward"), action: #selector(goForward))
        let sidebar = iconButton("sidebar.left", label: localized("Боковое меню", "Sidebar"), action: #selector(toggleSidebar))
        let accessory = GatewayTitlebar(back: back, forward: forward, sidebar: sidebar)
        titlebar = accessory
        window.addTitlebarAccessoryViewController(accessory)
        accessory.align()
    }

    /// Use AppKit buttons for keyboard focus and accessibility, with symbols as their visible content.
    private func toolbarButton(_ symbol: String, label: String, action: Selector) -> NSButton {
        let button = iconButton(symbol, label: label, action: action)
        button.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([button.widthAnchor.constraint(equalToConstant: 34),
                                     button.heightAnchor.constraint(equalToConstant: 28)])
        return button
    }

    private func iconButton(_ symbol: String, label: String, action: Selector) -> NSButton {
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: label) ?? NSImage()
        let button = NSButton(image: image, target: self, action: action)
        button.isBordered = false
        button.imagePosition = .imageOnly
        button.setAccessibilityLabel(label)
        button.toolTip = label
        return button
    }

    /// Reflect the selected profile and running operation in icon labels and availability.
    private func updateToolbar() {
        titlebar?.sidebar.isEnabled = sidebarReady && !sidebarPending
        titlebar?.back.isEnabled = active != nil && web.canGoBack
        titlebar?.forward.isEnabled = active != nil && web.canGoForward
        reloadButton?.isEnabled = !busy
        languageButton?.isEnabled = sidebarReady
        profileButton?.isEnabled = !busy && !profiles.isEmpty
        profileButton?.setAccessibilityValue(selected?.key)
        profileButton?.toolTip = selected.map { localized("Профиль · \($0.key)", "Profile · \($0.key)") }
            ?? localized("Выбрать профиль", "Select profile")
        startButton?.isEnabled = starting != nil || (!busy && selected != nil)
        let label = starting == nil ? localized("Запустить профиль", "Start profile")
            : localized("Отменить запуск", "Cancel start")
        startButton?.image = NSImage(systemSymbolName: starting == nil ? "play" : "xmark",
                                     accessibilityDescription: label)
        startButton?.setAccessibilityLabel(label)
        startButton?.toolTip = label
        profilePromptButton?.isEnabled = !busy
        addButton?.isEnabled = !busy
    }

    /// Keep profile names and their selection state in the menu.
    @objc private func showProfileMenu(_ sender: NSButton) {
        guard !busy, !profiles.isEmpty else { return }
        let menu = NSMenu()
        for profile in profiles {
            let entry = addAction(menu, "\(profile.key) · \(profile.port)", #selector(selectProfile(_:)))
            entry.representedObject = profile.name
            entry.state = profile.name == selectedName ? .on : .off
        }
        showToolbarMenu(menu, below: sender)
    }

    @objc private func showAddMenu(_ sender: NSButton) {
        guard !busy else { return }
        let menu = NSMenu()
        addAction(menu, localized("Создать профиль", "Create profile"), #selector(createProfile))
        if selected?.protected == true {
            addAction(menu, localized("Добавить MCP", "Add MCP"), #selector(addMCP))
        }
        showToolbarMenu(menu, below: sender)
    }

    /// Use the page's language links so locale changes retain its filters and cookie behavior.
    @objc private func showLanguageMenu(_ sender: NSButton) {
        guard sidebarReady else { return }
        let menu = NSMenu()
        for (code, title) in [("ru", "Русский"), ("en", "English")] {
            let entry = addAction(menu, title, #selector(selectLanguage(_:)))
            entry.representedObject = code
            entry.state = code == pageLanguage ? .on : .off
        }
        showToolbarMenu(menu, below: sender)
    }

    @objc private func selectLanguage(_ sender: NSMenuItem) {
        guard sidebarReady else { return }
        switch sender.representedObject as? String {
        case "ru": web.evaluateJavaScript("window.gatewaySidebar?.chooseLanguage('ru')")
        case "en": web.evaluateJavaScript("window.gatewaySidebar?.chooseLanguage('en')")
        default: break
        }
    }

    /// Place pull-down menus at the bottom edge of the toolbar button in either coordinate system.
    private func showToolbarMenu(_ menu: NSMenu, below button: NSButton) {
        let bottom = button.isFlipped ? button.bounds.maxY : button.bounds.minY
        menu.popUp(positioning: nil, at: NSPoint(x: button.bounds.minX, y: bottom), in: button)
    }

    private func buildMenu() {
        tray = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        tray.button?.image = GatewayMark.trayImage()
        let menu = NSMenu()
        menu.addItem(trayStatus)
        menu.addItem(.separator())
        addAction(menu, localized("Открыть шлюз", "Open gateway"), #selector(showWindow))
        addAction(menu, localized("Профили", "Profiles"), #selector(openProfiles))
        addAction(menu, localized("Добавить MCP", "Add MCP"), #selector(addMCP))
        menu.addItem(.separator())
        addAction(menu, localized("Скопировать токен шлюза", "Copy gateway token"), #selector(copyToken))
        addAction(menu, localized("Остановить профиль", "Stop profile"), #selector(stopSelected))
        menu.addItem(.separator())
        addAction(menu, localized("Выйти", "Quit"), #selector(quit))
        tray.menu = menu
        buildAppMenu()
    }

    /// Keep editing and window commands on the native responder chain.
    private func buildAppMenu() {
        let bar = NSMenu()
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            item.submenu = menu
            bar.addItem(item)
            return menu
        }
        let app = submenu("ag-mcp-gateway")
        addAction(app, localized("Выйти из ag-mcp-gateway", "Quit ag-mcp-gateway"), #selector(quit), key: "q")
        let file = submenu(localized("Файл", "File"))
        addAction(file, localized("Создать профиль", "Create profile"), #selector(createProfile), key: "n")
        addAction(file, localized("Добавить MCP", "Add MCP"), #selector(addMCP))
        addAction(file, localized("Профили", "Profiles"), #selector(openProfiles))
        addAction(file, localized("Запустить профиль", "Start profile"), #selector(startSelected))
        addAction(file, localized("Остановить профиль", "Stop profile"), #selector(stopSelected))
        addAction(file, localized("Изменить порт профиля", "Change profile port"), #selector(changeSelectedPort))
        file.addItem(.separator())
        file.addItem(withTitle: localized("Закрыть окно", "Close Window"),
                     action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")

        let edit = submenu(localized("Правка", "Edit"))
        for (title, action, key) in [
            (localized("Отменить", "Undo"), "undo:", "z"),
            (localized("Повторить", "Redo"), "redo:", "Z"),
            (localized("Вырезать", "Cut"), "cut:", "x"),
            (localized("Копировать", "Copy"), "copy:", "c"),
            (localized("Вставить", "Paste"), "paste:", "v"),
            (localized("Выбрать всё", "Select All"), "selectAll:", "a"),
        ] {
            edit.addItem(withTitle: title, action: NSSelectorFromString(action), keyEquivalent: key)
        }
        let view = submenu(localized("Вид", "View"))
        let sidebar = addAction(view, localized("Переключить боковое меню", "Toggle Sidebar"),
                                #selector(toggleSidebar), key: "s")
        sidebar.keyEquivalentModifierMask = [.command, .shift]
        addAction(view, localized("Обновить", "Reload"), #selector(reloadPage), key: "r")
        let windows = submenu(localized("Окно", "Window"))
        windows.addItem(withTitle: localized("Свернуть", "Minimize"),
                        action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        addAction(windows, localized("Открыть шлюз", "Open gateway"), #selector(showWindow))
        NSApp.windowsMenu = windows
        NSApp.mainMenu = bar
    }

    @discardableResult
    private func addAction(_ menu: NSMenu, _ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let item = menu.addItem(withTitle: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    /// Change the saved preference only after the current document acknowledges it.
    @objc private func toggleSidebar() {
        guard sidebarReady, !sidebarPending else { return }
        sidebarPending = true
        updateToolbar()
        evaluatePresentation("window.gatewaySidebar?.toggle()") { [weak self] value in
            guard let self else { return }
            self.sidebarPending = false
            if let collapsed = value?["collapsed"] as? Bool {
                UserDefaults.standard.set(collapsed, forKey: "sidebarCollapsed")
                self.applyPresentation(value)
            }
            self.updateToolbar()
        }
    }

    @objc private func goBack() { web.goBack() }
    @objc private func goForward() { web.goForward() }
    /// Refresh the profile inventory even when no dashboard is open.
    @objc private func reloadPage() {
        guard !busy else { return }
        busy = true
        Task {
            defer { busy = false }
            await refreshProfiles(preferred: selectedName)
            if ownsPage(web) { web.reload() }
        }
    }

    func windowDidEndLiveResize(_ notification: Notification) {
        guard sidebarReady else { return }
        evaluatePresentation("window.gatewaySidebar?.presentation()") { [weak self] value in
            self?.applyPresentation(value)
        }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        guard webView === web else { return }
        pageGeneration += 1
        sidebarReady = false
        sidebarPending = false
        updateToolbar()
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        // Reveal the new page after the header handoff, avoiding a flash of both toolbars.
        if webView === web { webView.isHidden = true }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === web, navigation === initialNavigation { initialNavigation = nil }
        attachPresentation(to: webView)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleNavigationFailure(webView, navigation: navigation)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        handleNavigationFailure(webView, navigation: navigation)
    }

    /// Give the operator a retry action when the dashboard could not open at all.
    private func handleNavigationFailure(_ page: WKWebView, navigation: WKNavigation?) {
        if page === web, let initialNavigation, navigation === initialNavigation {
            active = nil
            showProfilePrompt(detail: localized("Не удалось открыть дашборд. Повторите подключение.",
                                               "Could not open the dashboard. Try connecting again."))
        } else {
            attachPresentation(to: page)
        }
    }

    /// Restore the current document's controls after either a load or a failed navigation.
    private func attachPresentation(to page: WKWebView) {
        guard page === web else { return }
        guard ownsPage(page) else {
            page.isHidden = false
            updateToolbar()
            return
        }
        let preference = UserDefaults.standard.object(forKey: "sidebarCollapsed") as? Bool
        let value = preference.map { $0 ? "true" : "false" } ?? "null"
        evaluatePresentation("window.gatewaySidebar?.attach(\(value))") { [weak self] state in
            guard let self else { return }
            self.applyPresentation(state)
            self.updateToolbar()
            page.isHidden = false
        }
    }

    private func ownsPage(_ page: WKWebView) -> Bool {
        guard page === web, let url = page.url, let active else { return false }
        return url.scheme == "http" && url.host == "127.0.0.1" && url.port == active.port
    }

    /// Ignore results that arrive after a different document or profile has taken over.
    private func evaluatePresentation(_ script: String, completion: @escaping ([String: Any]?) -> Void) {
        let page = web
        guard ownsPage(page) else { return }
        let generation = pageGeneration
        page.evaluateJavaScript(script) { [weak self] result, _ in
            guard let self, self.ownsPage(page), generation == self.pageGeneration else { return }
            completion(result as? [String: Any])
        }
    }

    private func applyPresentation(_ value: [String: Any]?) {
        guard let collapsed = value?["collapsed"] as? Bool, let edge = value?["sidebarEdge"] as? Double,
              edge.isFinite, edge >= 0, edge <= web.bounds.width else { return }
        sidebarReady = true
        pageLanguage = value?["language"] as? String ?? ""
        languageButton?.setAccessibilityValue(pageLanguage == "ru" ? "Русский" : "English")
        let title = value?["title"] as? String ?? ""
        titlebar?.update(title: title, sidebarEdge: edge)
        titlebar?.sidebar.setAccessibilityValue(collapsed ? localized("Свёрнуто", "Collapsed")
                                                         : localized("Развёрнуто", "Expanded"))
        let hint = collapsed ? localized("Развернуть меню", "Expand sidebar") : localized("Свернуть меню", "Collapse sidebar")
        titlebar?.sidebar.toolTip = hint + " (⌘⇧S)"
    }

    private var selected: Profile? {
        profiles.first { $0.name == selectedName }
    }

    private func describe(_ message: String) {
        titlebar?.update(status: message)
        trayStatus.title = message
        tray?.button?.toolTip = "ag-mcp-gateway · " + message
    }

    private func refreshProfiles(preferred: String?) async {
        do {
            let data = try await command(binary, ["desktop", "profiles"])
            let inventory = try JSONDecoder().decode(Inventory.self, from: data)
            profiles = inventory.profiles
            selectedName = profiles.first { $0.name == preferred }?.name ?? profiles.first?.name
            if active == nil { showProfilePrompt() }
            updateToolbar()
            if inventory.unavailable > 0 {
                describe(localized("Некоторые профили не прошли проверку.", "Some profiles failed validation."))
            } else if active == nil {
                describe(localized("Выберите или создайте профиль.", "Select or create a profile."))
            }
        } catch { report(error) }
    }

    @objc private func selectProfile(_ sender: NSMenuItem) {
        guard !busy, let name = sender.representedObject as? String,
              let profile = profiles.first(where: { $0.name == name }) else { return }
        selectedName = name
        updateToolbar()
        UserDefaults.standard.set(profile.name, forKey: "selectedProfile")
        guard active?.name != name else { return }
        active = nil
        showProfilePrompt()
        Task { await start(profile, reuseDemo: true, launchIfStopped: profile.demo) }
    }

    @objc private func startOrCancel() {
        if starting != nil {
            stopSelected()
        } else {
            startSelected()
        }
    }

    @objc private func startSelected() {
        guard !busy, let profile = selected else { return }
        if pending.contains(profile.name) && children[profile.name]?.isRunning == true {
            offerRestart(profile)
        } else {
            Task { await start(profile, reuseDemo: true) }
        }
    }

    /// Probe only loopback and never attach a credential to an unmanaged listener.
    private func responseCode(_ url: URL) async -> Int? {
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 1)
        guard let (_, response) = try? await URLSession.shared.data(for: request) else { return nil }
        return (response as? HTTPURLResponse)?.statusCode
    }

    private func ownsListener(_ profile: Profile, process: Process) async -> Bool {
        guard process.isRunning else { return false }
        let listening = await portIsListening(profile.port, pid: String(process.processIdentifier))
        return listening && process.isRunning
    }

    /// Match a listener to a known process when reusing an existing local service.
    private func portIsListening(_ port: Int, pid: String? = nil) async -> Bool {
        var arguments = ["-nP", "-iTCP:\(port)", "-sTCP:LISTEN", "-Fp"]
        if let pid { arguments += ["-a", "-p", pid] }
        let result = try? await command(URL(fileURLWithPath: "/usr/sbin/lsof"), arguments)
        return result != nil
    }

    /// Identify a single listener launched by this user with the installed binary and this profile.
    private func gatewayListenerPID(_ profile: Profile) async -> String? {
        let lsof = URL(fileURLWithPath: "/usr/sbin/lsof")
        guard let data = try? await command(lsof, ["-nP", "-t", "-iTCP:\(profile.port)", "-sTCP:LISTEN"]) else {
            return nil
        }
        let owners = Set(String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline))
        guard owners.count == 1, let owner = owners.first, let number = Int32(owner), number > 0 else { return nil }
        let executable = binary.resolvingSymlinksInPath()
        var path = [CChar](repeating: 0, count: 4096)
        guard proc_pidpath(number, &path, UInt32(path.count)) > 0 else { return nil }
        let processBinary = URL(fileURLWithPath: String(cString: path)).resolvingSymlinksInPath()
        let releases = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ag-mcp-gateway/releases").resolvingSymlinksInPath()
        let installedRelease = [executable, processBinary].allSatisfy {
            $0.lastPathComponent == "ag-mcp-gateway" &&
            $0.deletingLastPathComponent().deletingLastPathComponent().path == releases.path
        }
        guard processBinary.path == executable.path || installedRelease else { return nil }
        let pid = String(number)
        let ps = URL(fileURLWithPath: "/bin/ps")
        guard let ownerData = try? await command(ps, ["-ww", "-p", pid, "-o", "uid=", "-o", "command="])
        else { return nil }
        let fields = String(decoding: ownerData, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        let prefix = "\(getuid()) "
        let suffix = " --profile \(profile.name) serve"
        guard fields.hasPrefix(prefix), fields.hasSuffix(suffix) else { return nil }
        let argv0 = fields.dropFirst(prefix.count).dropLast(suffix.count)
            .trimmingCharacters(in: .whitespaces)
        guard argv0.hasPrefix("/"),
              URL(fileURLWithPath: argv0).resolvingSymlinksInPath().path == processBinary.path else { return nil }
        return await portIsListening(profile.port, pid: pid) ? pid : nil
    }

    /// The installed demo is reusable only if the registered gateway job owns its port.
    private func isInstalledDemo(_ profile: Profile) async -> Bool {
        guard profile.demo else { return false }
        let label = "gui/\(getuid())/local.ag-mcp-gateway"
        guard let data = try? await command(URL(fileURLWithPath: "/bin/launchctl"), ["print", label]) else { return false }
        let lines = String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline)
        let program = lines.first { $0.hasPrefix("\tprogram = ") }?.dropFirst("\tprogram = ".count)
        let pid = lines.first { $0.hasPrefix("\tpid = ") }?.dropFirst("\tpid = ".count)
        guard let program, let pid, let number = Int(pid), number > 0,
              URL(fileURLWithPath: String(program)).resolvingSymlinksInPath() == binary.resolvingSymlinksInPath(),
              await portIsListening(profile.port, pid: String(number)) else { return false }
        return await responseCode(profile.status) == 200
    }

    private func start(_ profile: Profile, reuseDemo: Bool, launchIfStopped: Bool = true) async {
        guard !busy, selectedName == profile.name else { return }
        if stopping.contains(profile.name) {
            describe(localized("Дождитесь завершения службы.", "Wait for the service to stop."))
            return
        }
        busy = true
        starting = profile.name
        cancelStart = false
        var portConflict = false
        defer {
            startingProcess = nil
            starting = nil
            busy = false
            if portConflict { offerPortChange(profile, conflict: true) }
        }
        describe(localized("Запуск \(profile.key)…", "Starting \(profile.key)…"))
        if let process = children[profile.name], process.isRunning {
            let ownsPort = await ownsListener(profile, process: process)
            if cancelStart {
                describe(localized("Запуск отменён.", "Start cancelled."))
            } else if ownsPort {
                showProfile(profile)
            } else {
                describe(localized("Профиль работает с прежними настройками. Остановите его и запустите снова.",
                                   "The profile is running with earlier settings. Stop it and start it again."))
            }
            return
        }
        let installedDemo = reuseDemo ? await isInstalledDemo(profile) : false
        let portBusy = installedDemo ? false : await portIsListening(profile.port)
        var verifiedPID: String?
        if portBusy, let pid = await gatewayListenerPID(profile),
           await responseCode(profile.status) == (profile.protected ? 401 : 200),
           await gatewayListenerPID(profile) == pid {
            verifiedPID = pid
        }
        guard !cancelStart else {
            describe(localized("Запуск отменён.", "Start cancelled."))
            return
        }
        if installedDemo {
            showProfile(profile)
            return
        }
        if let verifiedPID {
            showProfile(profile, attachedPID: verifiedPID)
            return
        }
        if portBusy {
            let detail = localized("Порт \(profile.port) занят другим процессом.",
                                   "Port \(profile.port) is in use by another process.")
            if active == nil { showProfilePrompt(detail: detail) }
            describe(detail)
            portConflict = launchIfStopped
            return
        }
        guard launchIfStopped else {
            describe(localized("Выбран \(profile.key). Нажмите «Запустить».", "Selected \(profile.key). Choose Start."))
            return
        }

        let process = Process()
        process.executableURL = binary
        process.arguments = ["--profile", profile.name, "serve"]
        if profile.demo { process.arguments?.append("--allow-unauthenticated") }
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { report(error); return }
        startingProcess = process
        children[profile.name] = process
        process.terminationHandler = { [weak self] process in
            Task { @MainActor in
                guard let self, self.children[profile.name] === process else { return }
                self.stopping.remove(profile.name)
                guard self.active?.name == profile.name else { return }
                self.active = nil
                self.showProfilePrompt()
                self.describe(localized("Служба завершилась · \(profile.key)", "Service exited · \(profile.key)"))
            }
        }
        while process.isRunning && !cancelStart {
            let expected = profile.protected ? 401 : 200
            if await responseCode(profile.status) == expected,
               await ownsListener(profile, process: process), !cancelStart {
                pending.remove(profile.name)
                showProfile(profile)
                return
            }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        if cancelStart {
            describe(localized("Запуск отменён.", "Start cancelled."))
            return
        }
        describe(localized("Профиль не запустился. Проверьте порт и доступ к Связке ключей.", "Profile did not start. Check the port and Keychain access."))
    }

    @objc private func changeSelectedPort() {
        if let profile = selected { offerPortChange(profile) }
    }

    /// Ask for the new port and explain what happens to the running profile.
    private func offerPortChange(_ profile: Profile, conflict: Bool = false, draft: String = "", error: String? = nil) {
        guard !busy, window.attachedSheet == nil else { return }
        busy = true
        let alert = NSAlert()
        alert.messageText = conflict ? localized("Порт \(profile.port) уже занят", "Port \(profile.port) is already in use")
            : localized("Изменить порт · \(profile.key)", "Change port · \(profile.key)")
        alert.informativeText = error ?? localized(
            "Укажите свободный порт от 1024 до 65535. Запущенный профиль перезапустится.",
            "Choose an available port from 1024 to 65535. A running profile will restart."
        )
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 26))
        field.setAccessibilityLabel(localized("Новый порт", "New port"))
        field.placeholderString = localized("Новый порт", "New port")
        field.stringValue = draft
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        alert.addButton(withTitle: localized("Сохранить порт", "Save port"))
        alert.addButton(withTitle: localized("Отмена", "Cancel"))
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            self.busy = false
            guard response == .alertFirstButtonReturn else { return }
            let draft = field.stringValue
            Task {
                guard let port = Int(draft), (1024...65535).contains(port), port != profile.port else {
                    let message = localized("Укажите другой порт от 1024 до 65535.",
                                            "Choose a different port from 1024 to 65535.")
                    self.offerPortChange(profile, conflict: conflict, draft: draft, error: message)
                    return
                }
                await self.savePort(port, for: profile)
            }
        }
    }

    /// Open the dashboard after the CLI has applied the new port and restarted the profile.
    private func savePort(_ port: Int, for profile: Profile) async {
        guard !busy else { return }
        busy = true
        do {
            let request = try JSONSerialization.data(withJSONObject: [
                "name": profile.name, "revision": profile.revision, "port": port,
            ])
            let response = try await command(binary, ["desktop", "port"], input: request)
            let saved = try JSONDecoder().decode(Profile.self, from: response)
            if saved.restarted == true {
                active = nil
                pending.remove(saved.name)
                await refreshProfiles(preferred: saved.name)
                busy = false
                await start(saved, reuseDemo: false, launchIfStopped: false)
            } else {
                await showSavedProfile(saved)
            }
        } catch {
            busy = false
            report(error)
        }
    }

    /// Drop the outgoing document's controls before changing the window content.
    private func clearPagePresentation() {
        pageGeneration += 1
        sidebarReady = false
        sidebarPending = false
        titlebar?.update(title: "", sidebarEdge: 0)
    }

    /// Keep a stopped profile actionable even when there is no dashboard to load.
    private func showProfilePrompt(detail: String? = nil) {
        clearPagePresentation()
        initialNavigation = nil
        attachedPID = nil
        web.stopLoading()
        let content = NSView()
        let title: String
        let message: String
        let action: Selector
        let buttonTitle: String
        if let profile = selected {
            title = localized("Профиль не запущен", "Profile is not running")
            message = detail ?? localized("Запустите \(profile.key), чтобы открыть каталог и настройки MCP.",
                                         "Start \(profile.key) to open the MCP catalog and settings.")
            action = #selector(startSelected)
            buttonTitle = localized("Запустить профиль", "Start profile")
        } else {
            title = localized("Нет профилей", "No profiles")
            message = localized("Создайте профиль, чтобы добавить MCP.", "Create a profile to add MCP servers.")
            action = #selector(createProfile)
            buttonTitle = localized("Создать профиль", "Create profile")
        }
        let heading = NSTextField(wrappingLabelWithString: title)
        heading.font = .preferredFont(forTextStyle: .title2)
        let explanation = NSTextField(wrappingLabelWithString: message)
        explanation.font = .preferredFont(forTextStyle: .body)
        explanation.textColor = .secondaryLabelColor
        let button = NSButton(title: buttonTitle, target: self, action: action)
        button.bezelStyle = .rounded
        button.controlSize = .large

        let stack = NSStackView(views: [heading, explanation, button])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.setCustomSpacing(16, after: explanation)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 32),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 32),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -32),
        ])
        profilePromptButton = button
        window.contentView = content
        updateToolbar()
    }

    /// Open an isolated web view only once a verified gateway is listening.
    private func resetWeb() {
        clearPagePresentation()
        profilePromptButton = nil
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        web = WKWebView(frame: .zero, configuration: configuration)
        web.navigationDelegate = self
        window.contentView = web
        updateToolbar()
    }

    /// Open the dashboard and retain the verified PID when the service was started elsewhere.
    private func showProfile(_ profile: Profile, attachedPID: String? = nil) {
        active = profile
        self.attachedPID = attachedPID
        resetWeb()
        describe(localized("Запущен · \(profile.key) · \(profile.port)", "Running · \(profile.key) · \(profile.port)"))
        initialNavigation = web.load(URLRequest(url: profile.origin))
    }

    @objc private func createProfile() { editProfile(nil) }

    @objc private func addMCP() {
        guard let profile = selected, profile.protected else {
            createProfile()
            return
        }
        editProfile(profile)
    }

    private func editProfile(_ profile: Profile?) {
        guard !busy else { return }
        showWindow()
        busy = true
        let form = ProfileForm(profile: profile)
        profileForm = form
        form.present(on: window, save: { [binary] request in
            let response = try await command(binary, ["desktop", "save"], input: request)
            return try JSONDecoder().decode(Profile.self, from: response)
        }, finish: { [weak self] result in
            guard let self else { return }
            self.profileForm = nil
            guard let saved = result else {
                self.busy = false
                Task { @MainActor [weak self] in
                    guard let self, self.window.attachedSheet == nil, let button = self.addButton else { return }
                    self.window.makeFirstResponder(button)
                }
                return
            }
            Task { await self.showSavedProfile(saved) }
        })
    }

    private func showSavedProfile(_ saved: Profile) async {
        await refreshProfiles(preferred: saved.name)
        UserDefaults.standard.set(saved.name, forKey: "selectedProfile")
        if active?.name != saved.name {
            active = nil
            showProfilePrompt()
        }
        pending.insert(saved.name)
        describe(localized("Сохранён \(saved.key). Запустите профиль для применения.",
                           "Saved \(saved.key). Start the profile to apply."))
        busy = false
        if children[saved.name]?.isRunning == true { offerRestart(saved) }
    }

    private func offerRestart(_ profile: Profile) {
        let alert = NSAlert()
        alert.messageText = localized("Перезапустить профиль?", "Restart the profile?")
        alert.informativeText = localized("Изменения сохранены. Новый MCP появится после перезапуска.",
                                          "Changes are saved. The new MCP will appear after restart.")
        alert.addButton(withTitle: localized("Перезапустить", "Restart"))
        alert.addButton(withTitle: localized("Позже", "Later"))
        if alert.runModal() == .alertFirstButtonReturn {
            Task {
                guard !busy else { return }
                busy = true
                let stopped = await stop(profile)
                busy = false
                if stopped { await start(profile, reuseDemo: false) }
            }
        }
    }

    @objc private func stopSelected() {
        guard let profile = selected else { return }
        if starting == profile.name {
            cancelStart = true
            if let process = startingProcess, process.isRunning {
                stopping.insert(profile.name)
                process.terminate()
            }
            return
        }
        guard !busy, !stopping.contains(profile.name) else { return }
        Task {
            busy = true
            defer { busy = false }
            _ = await stop(profile)
        }
    }

    /// Stop either a child launched by this window or a verified profile from an earlier window.
    private func stop(_ profile: Profile) async -> Bool {
        stopping.insert(profile.name)
        defer { stopping.remove(profile.name) }
        if let process = children[profile.name], process.isRunning {
            process.terminate()
            for _ in 0..<80 where process.isRunning {
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
            if process.isRunning {
                describe(localized("Служба завершает вызовы.", "The service is draining calls."))
                return false
            }
        } else {
            do {
                _ = try await command(binary, ["--profile", profile.name, "desktop", "stop"])
            } catch {
                report(error)
                return false
            }
        }
        children.removeValue(forKey: profile.name)
        if active?.name == profile.name {
            active = nil
            showProfilePrompt()
        }
        describe(localized("Остановлен · \(profile.key)", "Stopped · \(profile.key)"))
        return true
    }

    @objc private func copyToken() {
        guard let profile = selected, profile.protected, !busy else { return }
        Task {
            do {
                let data = try await command(binary, ["--profile", profile.name, "desktop", "token"])
                guard let token = (try JSONSerialization.jsonObject(with: data) as? [String: String])?["token"] else { return }
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(token, forType: .string)
                let clipboardVersion = NSPasteboard.general.changeCount
                Task {
                    try? await Task.sleep(nanoseconds: 60_000_000_000)
                    if NSPasteboard.general.changeCount == clipboardVersion { NSPasteboard.general.clearContents() }
                }
                describe(localized("Токен скопирован. Имя пользователя — gateway.", "Token copied. The username is gateway."))
            } catch { report(error) }
        }
    }

    @objc private func openProfiles() {
        showWindow()
        if let profileButton { showProfileMenu(profileButton) }
    }

    @objc private func showWindow() {
        NSApp.activate(ignoringOtherApps: true)
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func reopenWindow(_ notification: Notification) { showWindow() }

    @objc private func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        profileForm?.isSaving == true ? .terminateCancel : .terminateNow
    }

    func applicationWillTerminate(_ notification: Notification) {
        DistributedNotificationCenter.default().removeObserver(self)
        NSStatusBar.system.removeStatusItem(tray)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls { handleLocalAction(url) }
    }

    private func handleLocalAction(_ url: URL) {
        guard url.scheme == "agm-gateway", url.query == nil, url.fragment == nil,
              url.user == nil, url.password == nil, url.port == nil else { return }
        if url.host == "profiles" && url.path.isEmpty {
            openProfiles()
        } else if url.host == "add-mcp", !busy {
            let key = String(url.path.dropFirst())
            Task {
                await refreshProfiles(preferred: selected?.name)
                guard let profile = profiles.first(where: { $0.key == key }) else {
                    describe(localized("Профиль из ссылки не найден.", "The profile from the link was not found."))
                    return
                }
                editProfile(profile.protected ? profile : nil)
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "agm-gateway" {
            decisionHandler(.cancel)
            if navigationAction.navigationType == .linkActivated { handleLocalAction(url) }
            return
        }
        let sameOrigin = url.scheme == "http" && url.host == "127.0.0.1" && url.port == active?.port
        decisionHandler(sameOrigin || url.absoluteString == "about:blank" ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let space = challenge.protectionSpace
        guard let profile = active, profile.protected,
              space.host == "127.0.0.1", space.port == profile.port, space.protocol == "http",
              space.realm == "ag-mcp-gateway", space.authenticationMethod == NSURLAuthenticationMethodHTTPBasic,
              challenge.previousFailureCount == 0 else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        Task {
            do {
                guard webView === web, await canAuthenticate(profile) else {
                    completionHandler(.cancelAuthenticationChallenge, nil)
                    return
                }
                let data = try await command(binary, ["--profile", profile.name, "desktop", "token"])
                guard await canAuthenticate(profile), webView === web, active?.name == profile.name,
                      let token = (try JSONSerialization.jsonObject(with: data) as? [String: String])?["token"] else {
                    completionHandler(.cancelAuthenticationChallenge, nil)
                    return
                }
                completionHandler(.useCredential, URLCredential(user: "gateway", password: token, persistence: .forSession))
            } catch {
                completionHandler(.cancelAuthenticationChallenge, nil)
                report(error)
            }
        }
    }

    /// Check listener ownership again after the asynchronous Keychain read.
    private func canAuthenticate(_ profile: Profile) async -> Bool {
        if let process = children[profile.name], await ownsListener(profile, process: process) {
            return true
        }
        guard let attachedPID else { return false }
        return await gatewayListenerPID(profile) == attachedPID
    }

    private func report(_ error: Error) {
        let alert = NSAlert(error: error)
        alert.runModal()
        describe(error.localizedDescription)
    }
}

@main
private enum Application {
    @MainActor static func main() throws {
        if CommandLine.arguments.count == 4, CommandLine.arguments[1] == "--render-icon",
           let size = Int(CommandLine.arguments[3]), (16...1024).contains(size) {
            try GatewayMark.writeIcon(to: CommandLine.arguments[2], size: size)
            return
        }
        guard let instance = try DesktopInstance.acquire() else { return }
        let app = NSApplication.shared
        let delegate = Desktop()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        withExtendedLifetime(instance) { app.run() }
    }
}
