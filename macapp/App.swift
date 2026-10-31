// diskmap — native macOS app shell.
//
// Starts the bundled Python dashboard server on a private port, shows it in a
// native window (WKWebView), and keeps a live menu bar item. Closing the window
// keeps diskmap running in the menu bar so history keeps recording; Quit stops it.
//
// Build with build.sh (needs Xcode or the Command Line Tools).

import Cocoa
import UserNotifications
import WebKit

// MARK: - Python server process

final class ServerController {
    private(set) var process: Process?
    private(set) var port: Int = 0
    let token: String = (UUID().uuidString + UUID().uuidString).replacingOccurrences(of: "-", with: "")
    var onReady: ((Int) -> Void)?
    var onExit: ((Int32, String) -> Void)?
    var stopping = false
    private var stdoutBuffer = ""
    private(set) var logTail = ""

    var resourcesDir: URL {
        Bundle.main.resourceURL!.appendingPathComponent("diskmap", isDirectory: true)
    }

    var dataDir: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("diskmap", isDirectory: true)
    }

    func start() {
        stopping = false
        port = 0
        stdoutBuffer = ""
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", resourcesDir.appendingPathComponent("server.py").path,
                       "--port", "0", "--no-open",
                       "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        var env = ProcessInfo.processInfo.environment
        let extraPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = extraPath + ":" + (env["PATH"] ?? "")
        env["DISKMAP_DATA"] = dataDir.path
        env["DISKMAP_TOKEN"] = token
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = env
        p.currentDirectoryURL = resourcesDir

        let out = Pipe()
        let err = Pipe()
        p.standardOutput = out
        p.standardError = err
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.handleStdout(text) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.appendLog(text) }
        }
        p.terminationHandler = { [weak self] proc in
            let status = proc.terminationStatus
            DispatchQueue.main.async {
                guard let self = self else { return }
                out.fileHandleForReading.readabilityHandler = nil
                err.fileHandleForReading.readabilityHandler = nil
                if !self.stopping { self.onExit?(status, self.logTail) }
            }
        }
        do {
            try p.run()
            process = p
        } catch {
            onExit?(-1, "Couldn't start python3: \(error.localizedDescription)")
        }
    }

    private func appendLog(_ text: String) {
        logTail = String((logTail + text).suffix(6000))
    }

    private func handleStdout(_ text: String) {
        appendLog(text)
        stdoutBuffer += text
        guard port == 0 else { return }
        if let range = stdoutBuffer.range(of: "DISKMAP_PORT=[0-9]+", options: .regularExpression) {
            let value = stdoutBuffer[range].replacingOccurrences(of: "DISKMAP_PORT=", with: "")
            if let n = Int(value) {
                port = n
                onReady?(n)
            }
        }
    }

    func stop() {
        stopping = true
        if let p = process, p.isRunning { p.terminate() }
        process = nil
    }

    func url(_ path: String) -> URL? {
        guard port > 0 else { return nil }
        let sep = path.contains("?") ? "&" : "?"
        return URL(string: "http://127.0.0.1:\(port)\(path)\(sep)t=\(token)")
    }

    func action(_ body: [String: Any], done: ((String) -> Void)? = nil) {
        guard port > 0, let url = URL(string: "http://127.0.0.1:\(port)/api/action"),
              let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.httpBody = data
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(token, forHTTPHeaderField: "X-Token")
        URLSession.shared.dataTask(with: req) { data, _, _ in
            var msg = "Done."
            if let data = data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                msg = (obj["msg"] as? String) ?? (obj["error"] as? String) ?? msg
            }
            DispatchQueue.main.async { done?(msg) }
        }.resume()
    }
}

// MARK: - Open at login (a per-user LaunchAgent that runs `open -a diskmap`)

enum LoginItem {
    static let label = "local.diskmap.app"
    static var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }
    static var isEnabled: Bool { FileManager.default.fileExists(atPath: plistURL.path) }

    static func set(_ enabled: Bool) {
        if enabled {
            let plist: [String: Any] = [
                "Label": label,
                "ProgramArguments": ["/usr/bin/open", "-g", "-a", Bundle.main.bundlePath, "--args", "--hidden"],
                "RunAtLoad": true,
            ]
            do {
                try FileManager.default.createDirectory(at: plistURL.deletingLastPathComponent(),
                                                        withIntermediateDirectories: true)
                let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
                try data.write(to: plistURL)
            } catch {
                print("diskmap: couldn't write login item: \(error)")
            }
        } else {
            try? FileManager.default.removeItem(at: plistURL)
        }
    }
}

// MARK: - Formatting helpers

func fmtBytes(_ b: Double) -> String {
    if b >= 1e12 { return String(format: "%.2f TB", b / 1e12) }
    if b >= 1e9 { return String(format: "%.1f GB", b / 1e9) }
    if b >= 1e6 { return String(format: "%.0f MB", b / 1e6) }
    return String(format: "%.0f KB", b / 1e3)
}

func num(_ any: Any?) -> Double? {
    if let d = any as? Double { return d }
    if let i = any as? Int { return Double(i) }
    if let n = any as? NSNumber { return n.doubleValue }
    return nil
}

// MARK: - Desktop widget

/// A small borderless window that lives on the desktop (above the icons, below your windows),
/// showing live stats. Drag it anywhere; double-click opens diskmap.
final class DragView: NSView {
    var onDoubleClick: (() -> Void)?
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { onDoubleClick?(); return }
        window?.performDrag(with: event)
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

@MainActor
final class WidgetController {
    var panel: NSPanel?
    var web: WKWebView?
    var onOpen: (() -> Void)?

    static let sizes: [String: NSSize] = ["small": NSSize(width: 170, height: 170), "medium": NSSize(width: 344, height: 170),
                                          "large": NSSize(width: 344, height: 344)]

    var sizeName: String {
        get { UserDefaults.standard.string(forKey: "widgetSize") ?? "medium" }
        set { UserDefaults.standard.set(newValue, forKey: "widgetSize") }
    }

    var onTop: Bool {
        get { UserDefaults.standard.bool(forKey: "widgetOnTop") }
        set { UserDefaults.standard.set(newValue, forKey: "widgetOnTop") }
    }

    var isShown: Bool { panel?.isVisible ?? false }

    func show(port: Int) {
        guard port > 0, let url = URL(string: "http://127.0.0.1:\(port)/widget") else { return }
        let size = Self.sizes[sizeName] ?? NSSize(width: 344, height: 170)
        if panel == nil {
            let p = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            p.isOpaque = false
            p.backgroundColor = .clear
            p.hasShadow = true
            p.isReleasedWhenClosed = false
            p.hidesOnDeactivate = false
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
            let config = WKWebViewConfiguration()
            let w = WKWebView(frame: NSRect(origin: .zero, size: size), configuration: config)
            w.setValue(false, forKey: "drawsBackground")
            w.autoresizingMask = [.width, .height]
            let container = NSView(frame: NSRect(origin: .zero, size: size))
            container.wantsLayer = true
            container.layer?.cornerRadius = 22
            container.layer?.masksToBounds = true
            container.addSubview(w)
            let drag = DragView(frame: container.bounds)
            drag.autoresizingMask = [.width, .height]
            drag.onDoubleClick = { [weak self] in self?.onOpen?() }
            container.addSubview(drag)
            p.contentView = container
            p.setFrameAutosaveName("diskmapWidget")
            if p.frame.origin == .zero, let screen = NSScreen.main {
                let vf = screen.visibleFrame
                p.setFrameOrigin(NSPoint(x: vf.maxX - size.width - 24, y: vf.maxY - size.height - 24))
            }
            panel = p
            web = w
        }
        applyLevel()
        resize()
        web?.load(URLRequest(url: url))
        panel?.orderFront(nil)
        UserDefaults.standard.set(true, forKey: "widgetOn")
    }

    func applyLevel() {
        guard let p = panel else { return }
        if onTop {
            p.level = .floating
        } else {
            p.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
        }
    }

    func resize() {
        guard let p = panel else { return }
        let size = Self.sizes[sizeName] ?? NSSize(width: 344, height: 170)
        var f = p.frame
        f.origin.y += f.size.height - size.height
        f.size = size
        p.setFrame(f, display: true)
        web?.reload()
    }

    func hide() {
        panel?.orderOut(nil)
        UserDefaults.standard.set(false, forKey: "widgetOn")
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate,
                         UNUserNotificationCenterDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    let server = ServerController()
    var statusItem: NSStatusItem!
    var statusMenu = NSMenu()
    var timer: Timer?
    var summary: [String: Any] = [:]
    var loginMenuItem: NSMenuItem?
    var lastNoteSeq = -1
    var notificationsAllowed = false
    var noteCategories: [String: UNNotificationCategory] = [:]
    let widget = WidgetController()
    var homeInfo: [String: Any] = [:]
    var pollCount = 0
    let startHidden = CommandLine.arguments.contains("--hidden")
    let views: [(String, String)] = [("city", "City"), ("home", "Today"), ("overview", "Live"),
                                     ("processes", "Processes"), ("hardware", "Hardware"), ("network", "Network"),
                                     ("storage", "Storage"), ("software", "Software"), ("history", "History")]

    var showStatsInMenuBar: Bool {
        get { UserDefaults.standard.object(forKey: "statusText") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "statusText") }
    }

    var showGraphInMenuBar: Bool {
        get { UserDefaults.standard.object(forKey: "statusGraph") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "statusGraph") }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMainMenu()
        buildWindow()
        buildStatusItem()
        setUpNotifications()

        server.onReady = { [weak self] port in
            guard let self = self, let url = URL(string: "http://127.0.0.1:\(port)/") else { return }
            self.webView.load(URLRequest(url: url))
            self.startPolling()
            self.fetchHome()
            if UserDefaults.standard.bool(forKey: "widgetOn") { self.widget.show(port: port) }
        }
        server.onExit = { [weak self] status, log in
            self?.showError(status: status, log: log)
        }
        widget.onOpen = { [weak self] in self?.showView("city") }
        server.start()

        if startHidden {
            NSApp.setActivationPolicy(.accessory)
        } else {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMain()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        server.stop()
    }

    // Closing the window just hides it; diskmap keeps running in the menu bar.
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        NSApp.setActivationPolicy(.accessory)
        // stop the page's live stream so the dashboard drops to its slower background pace
        webView.evaluateJavaScript("window.__dmPause && window.__dmPause()", completionHandler: nil)
        return false
    }

    @objc func showMain() {
        NSApp.setActivationPolicy(.regular)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        webView.evaluateJavaScript("window.__dmResume && window.__dmResume()", completionHandler: nil)
    }

    func showView(_ view: String) {
        showMain()
        let safe = view.filter { $0.isLetter }
        webView.evaluateJavaScript("location.hash = '#/\(safe)'", completionHandler: nil)
    }

    // MARK: Window

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        config.userContentController.add(self, name: "diskmap")
        applyAppearance(UserDefaults.standard.string(forKey: "theme") ?? "auto")
        config.websiteDataStore = .default()

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false
        webView.setValue(false, forKey: "drawsBackground")
        if #available(macOS 13.3, *) { webView.isInspectable = true }
        webView.loadHTMLString(Self.statusPage(title: "Starting diskmap…",
                                               body: "Collecting live stats. The disk map fills in after the first scan."),
                               baseURL: nil)

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "diskmap"
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.backgroundColor = NSColor.windowBackgroundColor
        window.center()
        window.setFrameAutosaveName("diskmapMain")
    }

    static func statusPage(title: String, body: String, pre: String = "") -> String {
        let esc = { (s: String) -> String in
            s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        }
        return """
        <!doctype html><html><head><meta charset="utf-8"><style>
        :root{color-scheme:light dark}
        body{margin:0;height:100vh;display:grid;place-items:center;font:13px -apple-system,system-ui;background:#f6f6f4;color:#1b1b19}
        @media (prefers-color-scheme: dark){body{background:#121211;color:#ececea}}
        .b{max-width:640px;padding:24px;text-align:center}
        h1{font-size:17px;margin:0 0 6px} p{opacity:.7;margin:0 0 12px}
        pre{text-align:left;font:11px ui-monospace,Menlo;background:rgba(127,127,127,.12);padding:12px;border-radius:8px;max-height:300px;overflow:auto;white-space:pre-wrap}
        .s{width:18px;height:18px;border:2px solid rgba(127,127,127,.3);border-top-color:currentColor;border-radius:50%;margin:0 auto 14px;animation:r .8s linear infinite}
        @keyframes r{to{transform:rotate(360deg)}}
        </style></head><body><div class="b">\(pre.isEmpty ? "<div class=\"s\"></div>" : "")<h1>\(esc(title))</h1><p>\(esc(body))</p>\(pre.isEmpty ? "" : "<pre>\(esc(pre))</pre>")</div></body></html>
        """
    }

    func showError(status: Int32, log: String) {
        timer?.invalidate()
        var hint = "The dashboard stopped unexpectedly. Choose View › Restart Dashboard to try again."
        if log.contains("No such file") || log.contains("xcrun") || log.contains("python3: command not found") {
            hint = "diskmap needs Python 3. Install it with: xcode-select --install"
        }
        webView.loadHTMLString(Self.statusPage(title: "diskmap stopped (code \(status))", body: hint, pre: log), baseURL: nil)
        statusItem.button?.title = " !"
        showMain()
    }

    @objc func restartServer() {
        server.stop()
        webView.loadHTMLString(Self.statusPage(title: "Restarting…", body: "Starting the dashboard again."), baseURL: nil)
        server.start()
    }

    // MARK: Appearance

    func applyAppearance(_ theme: String) {
        switch theme {
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        default: NSApp.appearance = nil
        }
        UserDefaults.standard.set(theme, forKey: "theme")
        for item in appearanceItems { item.state = (item.representedObject as? String) == theme ? .on : .off }
    }

    var appearanceItems: [NSMenuItem] = []

    @objc func chooseAppearance(_ sender: NSMenuItem) {
        guard let t = sender.representedObject as? String else { return }
        applyAppearance(t)
        webView.evaluateJavaScript("window.__dmTheme && window.__dmTheme('\(t)')", completionHandler: nil)
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any] else { return }
        if let theme = body["theme"] as? String, ["auto", "light", "dark"].contains(theme) {
            applyAppearance(theme)
        }
    }

    // MARK: Widget & focus

    @objc func toggleWidget(_ sender: NSMenuItem) {
        if widget.isShown { widget.hide() } else { widget.show(port: server.port) }
        rebuildStatusMenu()
    }

    @objc func chooseWidgetSize(_ sender: NSMenuItem) {
        guard let size = sender.representedObject as? String else { return }
        widget.sizeName = size
        if widget.isShown { widget.resize() } else { widget.show(port: server.port) }
        rebuildStatusMenu()
    }

    @objc func toggleWidgetOnTop(_ sender: NSMenuItem) {
        widget.onTop.toggle()
        widget.applyLevel()
        rebuildStatusMenu()
    }

    @objc func startFocus(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String else { return }
        let active = (homeInfo["focus"] as? [String: Any])?["name"] as? String
        let body: [String: Any] = active == name ? ["kind": "focus_end"] : ["kind": "focus_start", "name": name]
        server.action(body) { msg in
            self.post(["title": msg, "body": ""])
            self.fetchHome()
        }
    }

    func fetchHome() {
        guard let url = server.url("/api/home") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data = data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.homeInfo = obj
                self.rebuildStatusMenu()
            }
        }.resume()
    }

    // MARK: Menus

    func buildMainMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About diskmap", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        let login = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin(_:)), keyEquivalent: "")
        login.target = self
        login.state = LoginItem.isEnabled ? .on : .off
        appMenu.addItem(login)
        loginMenuItem = login
        let stats = NSMenuItem(title: "Show Stats in Menu Bar", action: #selector(toggleStatusText(_:)), keyEquivalent: "")
        stats.target = self
        stats.state = showStatsInMenuBar ? .on : .off
        appMenu.addItem(stats)
        let graph = NSMenuItem(title: "Show CPU Graph in Menu Bar", action: #selector(toggleStatusGraph(_:)), keyEquivalent: "")
        graph.target = self
        graph.state = showGraphInMenuBar ? .on : .off
        appMenu.addItem(graph)
        let data = NSMenuItem(title: "Show Data Folder", action: #selector(showDataFolder), keyEquivalent: "")
        data.target = self
        appMenu.addItem(data)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide diskmap", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit diskmap", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let view = NSMenu(title: "View")
        for (i, v) in views.enumerated() {
            let item = NSMenuItem(title: v.1, action: #selector(goToView(_:)), keyEquivalent: String(i + 1))
            item.target = self
            item.representedObject = v.0
            view.addItem(item)
        }
        let diag = NSMenuItem(title: "Diagnostics", action: #selector(goToView(_:)), keyEquivalent: "")
        diag.target = self
        diag.representedObject = "diagnostics"
        view.addItem(diag)
        let settings = NSMenuItem(title: "Settings…", action: #selector(goToView(_:)), keyEquivalent: ",")
        settings.target = self
        settings.representedObject = "settings"
        view.addItem(settings)
        view.addItem(.separator())
        let cmd = NSMenuItem(title: "Command Bar", action: #selector(openCommandBar), keyEquivalent: "k")
        cmd.target = self
        view.addItem(cmd)
        let cityItem = NSMenuItem(title: "City", action: nil, keyEquivalent: "")
        let cityMenu = NSMenu(title: "City")
        for (id, name) in [("nyc", "New York"), ("dubai", "Dubai"), ("karachi", "Karachi"), ("paris", "Paris"), ("london", "London"), ("tokyo", "Tokyo")] {
            let it = NSMenuItem(title: name, action: #selector(pickCity(_:)), keyEquivalent: "")
            it.target = self
            it.representedObject = id
            cityMenu.addItem(it)
        }
        cityMenu.addItem(.separator())
        let nextC = NSMenuItem(title: "Next City", action: #selector(nextCity), keyEquivalent: "]")
        nextC.target = self
        cityMenu.addItem(nextC)
        let prevC = NSMenuItem(title: "Previous City", action: #selector(prevCity), keyEquivalent: "[")
        prevC.target = self
        cityMenu.addItem(prevC)
        cityItem.submenu = cityMenu
        view.addItem(cityItem)
        let appearance = NSMenuItem(title: "Appearance", action: nil, keyEquivalent: "")
        let am = NSMenu(title: "Appearance")
        for (title, key) in [("System", "auto"), ("Light", "light"), ("Dark", "dark")] {
            let it = NSMenuItem(title: title, action: #selector(chooseAppearance(_:)), keyEquivalent: "")
            it.target = self
            it.representedObject = key
            it.state = (UserDefaults.standard.string(forKey: "theme") ?? "auto") == key ? .on : .off
            am.addItem(it)
            appearanceItems.append(it)
        }
        appearance.submenu = am
        view.addItem(appearance)
        view.addItem(.separator())
        let reload = NSMenuItem(title: "Reload", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
        view.addItem(reload)
        let restart = NSMenuItem(title: "Restart Dashboard", action: #selector(restartServer), keyEquivalent: "R")
        restart.target = self
        view.addItem(restart)
        let rescan = NSMenuItem(title: "Rescan Disk", action: #selector(rescanDisk), keyEquivalent: "")
        rescan.target = self
        view.addItem(rescan)
        view.addItem(.separator())
        let zin = NSMenuItem(title: "Zoom In", action: #selector(zoomIn), keyEquivalent: "=")
        zin.target = self
        view.addItem(zin)
        let zout = NSMenuItem(title: "Zoom Out", action: #selector(zoomOut), keyEquivalent: "-")
        zout.target = self
        view.addItem(zout)
        let zreset = NSMenuItem(title: "Actual Size", action: #selector(zoomReset), keyEquivalent: "0")
        zreset.target = self
        view.addItem(zreset)
        viewItem.submenu = view

        let winItem = NSMenuItem()
        main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let show = NSMenuItem(title: "Show diskmap", action: #selector(showMain), keyEquivalent: "d")
        show.target = self
        win.addItem(show)
        winItem.submenu = win

        NSApp.mainMenu = main
        NSApp.windowsMenu = win
    }

    @objc func goToView(_ sender: NSMenuItem) {
        guard let v = sender.representedObject as? String else { return }
        showMain()
        webView.evaluateJavaScript("location.hash = '#/\(v)'", completionHandler: nil)
    }

    @objc func reloadPage() { webView.reload() }
    @objc func zoomIn() { webView.pageZoom = min(2.0, webView.pageZoom + 0.1) }
    @objc func zoomOut() { webView.pageZoom = max(0.6, webView.pageZoom - 0.1) }
    @objc func zoomReset() { webView.pageZoom = 1.0 }

    @objc func rescanDisk() {
        server.action(["kind": "rescan"]) { msg in self.notifyInline(msg) }
    }

    @objc func toggleLogin(_ sender: NSMenuItem) {
        LoginItem.set(!LoginItem.isEnabled)
        sender.state = LoginItem.isEnabled ? .on : .off
        rebuildStatusMenu()
    }

    @objc func toggleStatusText(_ sender: NSMenuItem) {
        showStatsInMenuBar.toggle()
        sender.state = showStatsInMenuBar ? .on : .off
        updateStatusTitle()
    }

    @objc func toggleStatusGraph(_ sender: NSMenuItem) {
        showGraphInMenuBar.toggle()
        sender.state = showGraphInMenuBar ? .on : .off
        updateStatusTitle()
    }

    @objc func openCommandBar() {
        showMain()
        webView.evaluateJavaScript("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', metaKey: true}))", completionHandler: nil)
    }

    @objc func showDataFolder() {
        NSWorkspace.shared.activateFileViewerSelecting([server.dataDir])
    }

    func notifyInline(_ msg: String) {
        let js = "(function(){var t=document.getElementById('toast');if(!t)return;t.textContent=\(Self.jsString(msg));t.classList.add('on');setTimeout(function(){t.classList.remove('on')},4000)})()"
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    static func jsString(_ s: String) -> String {
        if let data = try? JSONSerialization.data(withJSONObject: [s]), let text = String(data: data, encoding: .utf8) {
            return String(text.dropFirst().dropLast())
        }
        return "\"\""
    }

    // MARK: Menu bar item

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "gauge.with.dots.needle.33percent", accessibilityDescription: "diskmap")
                ?? NSImage(systemSymbolName: "speedometer", accessibilityDescription: "diskmap")
            image?.isTemplate = true
            button.image = image
            button.imagePosition = .imageLeading
            button.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        }
        statusItem.menu = statusMenu
        rebuildStatusMenu()
    }

    func startPolling() {
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { self?.poll() }
        }
        poll()
    }

    func poll() {
        guard let url = server.url("/api/summary?since=\(max(0, lastNoteSeq))") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data = data, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.summary = obj
                self.updateStatusTitle()
                self.rebuildStatusMenu()
                self.deliverNotes(obj)
                self.pollCount += 1
                if self.pollCount % 10 == 0 { self.fetchHome() }
            }
        }.resume()
    }

    func updateStatusTitle() {
        guard let button = statusItem.button else { return }
        if showGraphInMenuBar, let hist = summary["cpu_hist"] as? [Any], hist.count > 2 {
            button.image = sparkline(hist.compactMap { num($0) })
        } else {
            let image = NSImage(systemSymbolName: "gauge.with.dots.needle.33percent", accessibilityDescription: "diskmap")
                ?? NSImage(systemSymbolName: "speedometer", accessibilityDescription: "diskmap")
            image?.isTemplate = true
            button.image = image
        }
        guard showStatsInMenuBar, let cpu = num(summary["cpu"]) else {
            button.title = ""
            return
        }
        var parts = [String(format: "%.0f%%", cpu)]
        if let t = num(summary["temp"]) { parts.append(String(format: "%.0f°", t)) }
        else if let used = num(summary["mem_used"]), let total = num(summary["mem_total"]), total > 0 {
            parts.append(String(format: "%.0f%% mem", used / total * 100))
        }
        let alerts = Int(num(summary["alerts"]) ?? 0)
        button.title = " " + parts.joined(separator: "  ") + (alerts > 0 ? "  ⚠︎" : "")
    }

    func sparkline(_ values: [Double]) -> NSImage {
        let w: CGFloat = 36, h: CGFloat = 16
        let pts = Array(values.suffix(36))
        let image = NSImage(size: NSSize(width: w, height: h), flipped: false) { rect in
            guard pts.count > 1 else { return true }
            let step = rect.width / CGFloat(max(1, pts.count - 1))
            let path = NSBezierPath()
            for (i, v) in pts.enumerated() {
                let y = 1.5 + CGFloat(min(100, max(0, v)) / 100) * (rect.height - 3)
                let p = NSPoint(x: CGFloat(i) * step, y: y)
                if i == 0 { path.move(to: p) } else { path.line(to: p) }
            }
            let area = path.copy() as! NSBezierPath
            area.line(to: NSPoint(x: rect.width, y: 0))
            area.line(to: NSPoint(x: 0, y: 0))
            area.close()
            NSColor.black.withAlphaComponent(0.25).setFill()
            area.fill()
            NSColor.black.setStroke()
            path.lineWidth = 1.3
            path.lineJoinStyle = .round
            path.stroke()
            return true
        }
        image.isTemplate = true
        return image
    }

    // MARK: Notifications

    func setUpNotifications() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            DispatchQueue.main.async {
                self.notificationsAllowed = granted
                self.injectAppInfo()
            }
        }
    }

    func injectAppInfo() {
        let state = notificationsAllowed ? "allowed" : "off"
        webView.evaluateJavaScript("window.__dmApp = {notifications: '\(state)'}", completionHandler: nil)
    }

    func deliverNotes(_ obj: [String: Any]) {
        let seq = Int(num(obj["seq"]) ?? 0)
        if lastNoteSeq < 0 {  // first poll after launch: don't replay old alerts
            lastNoteSeq = seq
            return
        }
        guard let notes = obj["notes"] as? [[String: Any]] else { return }
        for note in notes {
            let n = Int(num(note["seq"]) ?? 0)
            guard n > lastNoteSeq else { continue }
            lastNoteSeq = n
            post(note)
        }
    }

    func post(_ note: [String: Any]) {
        let title = (note["title"] as? String) ?? "diskmap"
        let body = (note["body"] as? String) ?? ""
        let action = note["action"] as? [String: Any]
        guard notificationsAllowed else {
            // fall back to a plain notification via AppleScript
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            let esc = { (s: String) in s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") }
            p.arguments = ["-e", "display notification \"\(esc(body))\" with title \"diskmap\" subtitle \"\(esc(title))\""]
            try? p.run()
            return
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        if let action = action, let data = try? JSONSerialization.data(withJSONObject: action),
           let json = String(data: data, encoding: .utf8) {
            content.userInfo = ["action": json]
            let label = (action["label"] as? String) ?? "Open"
            let id = "dm.\(label)"
            if noteCategories[id] == nil {
                let button = UNNotificationAction(identifier: "DO", title: label, options: [.foreground])
                noteCategories[id] = UNNotificationCategory(identifier: id, actions: [button], intentIdentifiers: [], options: [])
                UNUserNotificationCenter.current().setNotificationCategories(Set(noteCategories.values))
            }
            content.categoryIdentifier = id
        }
        let request = UNNotificationRequest(identifier: "dm.\(note["seq"] ?? UUID().uuidString)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let json = response.notification.request.content.userInfo["action"] as? String ?? ""
        let pressedButton = response.actionIdentifier == "DO"
        DispatchQueue.main.async {
            self.handleNotificationTap(json: json, pressedButton: pressedButton)
            completionHandler()
        }
    }

    func handleNotificationTap(json: String, pressedButton: Bool) {
        guard let data = json.data(using: .utf8),
              let action = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            showView("overview")
            return
        }
        if pressedButton, let act = action["act"] as? [String: Any] {
            server.action(act) { msg in
                self.post(["title": msg, "body": ""])
            }
            return
        }
        showView((action["open"] as? String) ?? "overview")
    }

    func rebuildStatusMenu() {
        let m = statusMenu
        m.removeAllItems()
        func info(_ title: String) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.isEnabled = false
            m.addItem(item)
        }
        if summary.isEmpty {
            info("Starting…")
        } else {
            if let cpu = num(summary["cpu"]) {
                var line = String(format: "CPU  %.0f%%", cpu)
                if let g = num(summary["gpu"]) { line += String(format: "   ·   GPU  %.0f%%", g) }
                info(line)
            }
            if let used = num(summary["mem_used"]), let total = num(summary["mem_total"]) {
                let pressure = (summary["pressure"] as? String) ?? "normal"
                info("Memory  \(fmtBytes(used)) of \(fmtBytes(total))" + (pressure == "normal" ? "" : "  ·  \(pressure) pressure"))
            }
            if let t = num(summary["temp"]) { info(String(format: "CPU temperature  %.0f °C", t)) }
            if let w = num(summary["power"]) { info(String(format: "Power  %.1f W", w)) }
            if let b = num(summary["battery"]) {
                let charging = (summary["charging"] as? Bool) == true
                info(String(format: "Battery  %.0f%%", b) + (charging ? "  ·  charging" : ""))
            }
            if let d = num(summary["disk_free"]) { info("Free disk  \(fmtBytes(d))") }
            if let down = num(summary["net_down"]), let up = num(summary["net_up"]) {
                info("Network  ↓ \(fmtBytes(down))/s   ↑ \(fmtBytes(up))/s")
            }
            if let top = summary["top"] as? [String], !top.isEmpty {
                m.addItem(.separator())
                for t in top {
                    let item = NSMenuItem(title: "⚠︎  " + t, action: #selector(openOverview), keyEquivalent: "")
                    item.target = self
                    m.addItem(item)
                }
            }
        }
        m.addItem(.separator())
        let open = NSMenuItem(title: "Open diskmap", action: #selector(showMain), keyEquivalent: "")
        open.target = self
        m.addItem(open)
        let cmd = NSMenuItem(title: "Command Bar…", action: #selector(openCommandBar), keyEquivalent: "")
        cmd.target = self
        m.addItem(cmd)
        if let profiles = homeInfo["profiles"] as? [[String: Any]], !profiles.isEmpty {
            let active = (homeInfo["focus"] as? [String: Any])?["name"] as? String
            let fItem = NSMenuItem(title: active.map { "Focus: \($0)" } ?? "Focus Mode", action: nil, keyEquivalent: "")
            let fm = NSMenu(title: "Focus")
            for p in profiles {
                guard let name = p["name"] as? String else { continue }
                let icon = (p["icon"] as? String) ?? ""
                let it = NSMenuItem(title: (icon.isEmpty ? "" : icon + "  ") + (active == name ? "End \(name)" : name),
                                    action: #selector(startFocus(_:)), keyEquivalent: "")
                it.target = self
                it.representedObject = name
                it.state = active == name ? .on : .off
                fm.addItem(it)
            }
            fItem.submenu = fm
            m.addItem(fItem)
        }
        let wItem = NSMenuItem(title: "Desktop Widget", action: nil, keyEquivalent: "")
        let wm = NSMenu(title: "Desktop Widget")
        let wShow = NSMenuItem(title: widget.isShown ? "Hide Widget" : "Show Widget", action: #selector(toggleWidget(_:)), keyEquivalent: "")
        wShow.target = self
        wm.addItem(wShow)
        wm.addItem(.separator())
        for (title, key) in [("Small", "small"), ("Medium", "medium"), ("Large", "large")] {
            let it = NSMenuItem(title: title, action: #selector(chooseWidgetSize(_:)), keyEquivalent: "")
            it.target = self
            it.representedObject = key
            it.state = widget.sizeName == key ? .on : .off
            wm.addItem(it)
        }
        wm.addItem(.separator())
        let top = NSMenuItem(title: "Keep Above Windows", action: #selector(toggleWidgetOnTop(_:)), keyEquivalent: "")
        top.target = self
        top.state = widget.onTop ? .on : .off
        wm.addItem(top)
        wItem.submenu = wm
        m.addItem(wItem)
        let rescan = NSMenuItem(title: (summary["scanning"] as? Bool) == true ? "Scanning disk…" : "Rescan Disk",
                                action: #selector(rescanDisk), keyEquivalent: "")
        rescan.target = self
        rescan.isEnabled = (summary["scanning"] as? Bool) != true
        m.addItem(rescan)
        let login = NSMenuItem(title: "Open at Login", action: #selector(toggleLoginFromStatus(_:)), keyEquivalent: "")
        login.target = self
        login.state = LoginItem.isEnabled ? .on : .off
        m.addItem(login)
        m.addItem(.separator())
        m.addItem(withTitle: "Quit diskmap", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    }

    @objc func toggleLoginFromStatus(_ sender: NSMenuItem) {
        LoginItem.set(!LoginItem.isEnabled)
        loginMenuItem?.state = LoginItem.isEnabled ? .on : .off
        rebuildStatusMenu()
    }

    @objc func pickCity(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, id.allSatisfy({ $0.isLetter }) else { return }
        showMain()
        webView.evaluateJavaScript("location.hash = '#/city'; setTimeout(function(){ if (window.DM_city) DM_city.switchCity('\(id)'); }, 80)", completionHandler: nil)
    }

    @objc func nextCity() {
        showMain()
        webView.evaluateJavaScript("location.hash = '#/city'; if (window.DM_city) DM_city.cycleCity(1)", completionHandler: nil)
    }

    @objc func prevCity() {
        showMain()
        webView.evaluateJavaScript("location.hash = '#/city'; if (window.DM_city) DM_city.cycleCity(-1)", completionHandler: nil)
    }

    @objc func openOverview() {
        showMain()
        webView.evaluateJavaScript("location.hash = '#/overview'", completionHandler: nil)
    }

    // MARK: Web view delegates

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        if let url = navigationAction.request.url, let scheme = url.scheme, scheme.hasPrefix("http"),
           let host = url.host, host != "127.0.0.1", host != "localhost" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var dest = dir.appendingPathComponent(suggestedFilename)
        var i = 1
        while FileManager.default.fileExists(atPath: dest.path) {
            dest = dir.appendingPathComponent("\(i)-\(suggestedFilename)")
            i += 1
        }
        completionHandler(dest)
    }

    func downloadDidFinish(_ download: WKDownload) {
        notifyInline("Saved to your Downloads folder.")
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        injectAppInfo()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }
}

// MARK: - Entry point

@main
struct DiskmapApp {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
        _ = delegate  // keep the delegate alive for the life of the app
    }
}
