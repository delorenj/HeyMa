import AppKit
import Carbon
import Foundation

// HeyMa is only a control surface. Vinyl's installed client owns capture, RPC and injection.
private let clientLabel = "sh.delo.vinyl.client"
private let loginLabel = "sh.delo.heyma"
private let settingsKey = "hasOpenedSettings"
private let hotkeyKey = "globalDictationHotkey"
private let phonographURL: String = {
    let raw = ProcessInfo.processInfo.environment["HEYMA_PHONOGRAPH_URL"] ?? ""
    return (raw.isEmpty ? "https://phonograph.intellifornia.com" : raw).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
}()
private let phonographEnabled = (ProcessInfo.processInfo.environment["HEYMA_PHONOGRAPH"] ?? "").lowercased() != "off"
private let home = FileManager.default.homeDirectoryForCurrentUser

private final class CommandResult {
    let code: Int32
    let output: String

    init(code: Int32, output: String) {
        self.code = code
        self.output = output
    }
    var detail: String { output.trimmingCharacters(in: .whitespacesAndNewlines) }
}

private enum ClientError: LocalizedError {
    case missingCLI
    case timedOut(String)
    case failed(String)
    case malformedStatus
    case busy

    var errorDescription: String? {
        switch self {
        case .missingCLI: return "Vinyl CLI not found. Install vinyl-client, then reopen HeyMa."
        case .timedOut(let name): return "\(name) timed out. Check the Vinyl client log and try again."
        case .failed(let reason): return reason
        case .malformedStatus: return "Vinyl returned invalid live status. Try vinyl doctor in Terminal."
        case .busy: return "Finish or cancel dictation before changing the server."
        }
    }
}

private final class FlippedDocumentView: NSView {
    override var isFlipped: Bool { true }
}

private final class VinylCLI {
    private let queue = DispatchQueue(label: "sh.delo.heyma.commands", qos: .userInitiated)

    private func executable() -> URL? {
        let fm = FileManager.default
        let env = ProcessInfo.processInfo.environment
        if let override = env["VINYL_BIN"], override.hasPrefix("/"), fm.isExecutableFile(atPath: override) {
            return URL(fileURLWithPath: override)
        }
        // Follow the configured client LaunchAgent, not a leftover direct-install
        // wrapper that might still exist while Homebrew owns the live client.
        let agent = home.appendingPathComponent("Library/LaunchAgents/\(clientLabel).plist")
        if let data = try? Data(contentsOf: agent),
           let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let args = plist["ProgramArguments"] as? [String], let program = args.first {
            let script = args.dropFirst().first ?? ""
            let candidate: String?
            if [program, script].contains(where: { $0.hasPrefix("/opt/homebrew/") }) {
                candidate = "/opt/homebrew/bin/vinyl"
            } else if [program, script].contains(where: { $0.hasPrefix("/usr/local/") }) {
                candidate = "/usr/local/bin/vinyl"
            } else if [program, script].contains(where: { $0.hasPrefix(home.path + "/.local/share/vinyl/") }) {
                candidate = home.appendingPathComponent(".local/bin/vinyl").path
            } else if URL(fileURLWithPath: program).lastPathComponent == "vinyl" {
                candidate = program
            } else {
                candidate = nil
            }
            if let candidate = candidate, fm.isExecutableFile(atPath: candidate) {
                return URL(fileURLWithPath: candidate)
            }
        }
        let candidates = [home.appendingPathComponent(".local/bin/vinyl").path,
                          "/opt/homebrew/bin/vinyl", "/usr/local/bin/vinyl"]
        return candidates.first { fm.isExecutableFile(atPath: $0) }
            .map { URL(fileURLWithPath: $0) }
    }

    func submit(_ args: [String], timeout: TimeInterval = 8,
                completion: @escaping (Result<CommandResult, Error>) -> Void) {
        queue.async {
            let result: Result<CommandResult, Error>
            do { result = .success(try self.run(args, timeout: timeout)) }
            catch { result = .failure(error) }
            DispatchQueue.main.async { completion(result) }
        }
    }

    func work(_ body: @escaping () throws -> Void,
              completion: @escaping (Result<Void, Error>) -> Void) {
        queue.async {
            let result: Result<Void, Error>
            do { try body(); result = .success(()) }
            catch { result = .failure(error) }
            DispatchQueue.main.async { completion(result) }
        }
    }

    func run(_ args: [String], timeout: TimeInterval = 8) throws -> CommandResult {
        guard let url = executable() else { throw ClientError.missingCLI }
        return try runURL(url, args, timeout: timeout)
    }

    func runURL(_ url: URL, _ args: [String], timeout: TimeInterval = 8) throws -> CommandResult {
        let lock = NSLock()
        var bytes = Data()
        var expired = false
        let reader = DispatchGroup()
        reader.enter()
        let process = Process()
        process.executableURL = url
        process.arguments = args
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        DispatchQueue.global(qos: .utility).async {
            defer { reader.leave() }
            while true {
                let chunk = pipe.fileHandleForReading.readData(ofLength: 4096)
                if chunk.isEmpty { break }
                lock.lock()
                if bytes.count < 16384 { bytes.append(chunk.prefix(16384 - bytes.count)) }
                lock.unlock()
            }
        }
        do { try process.run() }
        catch {
            pipe.fileHandleForWriting.closeFile()
            _ = reader.wait(timeout: .now() + 1)
            throw ClientError.failed("Could not run \(url.lastPathComponent): \(error.localizedDescription)")
        }
        let deadline = DispatchWorkItem {
            lock.lock()
            expired = process.isRunning
            lock.unlock()
            if process.isRunning {
                process.terminate()
                DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
                    if process.isRunning { kill(process.processIdentifier, SIGKILL) }
                }
            }
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: deadline)
        process.waitUntilExit()
        deadline.cancel()
        pipe.fileHandleForWriting.closeFile()
        if reader.wait(timeout: .now() + 2) == .timedOut {
            pipe.fileHandleForReading.closeFile()
            throw ClientError.failed("Could not finish reading \(url.lastPathComponent) output.")
        }
        lock.lock()
        let text = String(decoding: bytes, as: UTF8.self)
        let didExpire = expired
        lock.unlock()
        if didExpire { throw ClientError.timedOut(args.first ?? url.lastPathComponent) }
        return CommandResult(code: process.terminationStatus, output: text)
    }

    func status() throws -> [String: Any] {
        let result = try run(["status"], timeout: 5)
        // CLI returns 1 and prints the stale state.json mirror when RPC is down.
        // Do not turn that mirror into a healthy status.
        guard result.code == 0 else {
            throw ClientError.failed("Vinyl client is not responding (exit \(result.code)). Check the client LaunchAgent and log.")
        }
        guard let data = result.output.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let state = object["state"] as? String, ["idle", "listening"].contains(state) else {
            throw ClientError.malformedStatus
        }
        return object
    }
}

private func endpoint() -> String {
    let file = home.appendingPathComponent(".config/vinyl/client.json")
    guard let data = try? Data(contentsOf: file),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return "" }
    return object["server"] as? String ?? ""
}

private func pairedMarker() -> Bool {
    let path = home.appendingPathComponent(".config/vinyl/token").path
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
          attrs[.type] as? FileAttributeType == .typeRegular else { return false }
    return (attrs[.size] as? NSNumber)?.intValue ?? 0 > 0
}

private func heymaVersion() -> String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
}

private func installedVersion() -> String {
    var roots = [home.appendingPathComponent(".local/share/vinyl/vinyl/__init__.py")]
    for prefix in ["/opt/homebrew/opt/vinyl-client", "/usr/local/opt/vinyl-client"] {
        let base = URL(fileURLWithPath: prefix).appendingPathComponent("libexec")
        roots.append(base.appendingPathComponent("vinyl/__init__.py"))
        roots.append(base.appendingPathComponent("lib/vinyl/__init__.py"))
        let lib = base.appendingPathComponent("lib")
        if let dirs = try? FileManager.default.contentsOfDirectory(at: lib, includingPropertiesForKeys: nil) {
            roots += dirs.filter { $0.lastPathComponent.hasPrefix("python") }
                .map { $0.appendingPathComponent("site-packages/vinyl/__init__.py") }
        }
        let venv = base.appendingPathComponent("venv/lib")
        if let dirs = try? FileManager.default.contentsOfDirectory(at: venv, includingPropertiesForKeys: nil) {
            roots += dirs.filter { $0.lastPathComponent.hasPrefix("python") }
                .map { $0.appendingPathComponent("site-packages/vinyl/__init__.py") }
        }
    }
    let pattern = try! NSRegularExpression(pattern: #"(?m)^__version__\s*=\s*["']([^"']+)["']"#)
    for file in roots {
        guard let text = try? String(contentsOf: file, encoding: .utf8) else { continue }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        if let match = pattern.firstMatch(in: text, range: range),
           let swiftRange = Range(match.range(at: 1), in: text) { return String(text[swiftRange]) }
    }
    return "Unavailable"
}

private func validatedEndpoint(_ raw: String) -> String? {
    let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    let parts = value.split(separator: ":", omittingEmptySubsequences: false)
    guard parts.count == 2, let host = parts.first, !host.isEmpty,
          host.count <= 253, host.split(separator: ".", omittingEmptySubsequences: false).allSatisfy({ label in
              !label.isEmpty && label.count <= 63 && label.first != "-" && label.last != "-" &&
              label.utf8.allSatisfy { (48...57).contains($0) || (65...90).contains($0) ||
                  (97...122).contains($0) || $0 == 45 }
          }),
          let port = Int(parts[1]), (1...65535).contains(port) else { return nil }
    return "\(host):\(port)"
}

// Thin, synchronous launchd agent-file owner shared by the UI helpers. The
// entire plist is rewritten on enable/disable, so the property names below
// must only change together with the app version that writes them.
private final class LaunchAgentClient {
    private let path: URL

    init(label: String) {
        path = home.appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    private func properties() -> [String: Any]? {
        guard let data = try? Data(contentsOf: path),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil),
              let properties = plist as? [String: Any] else { return nil }
        return properties
    }

    private func programArguments() -> [String]? {
        properties()?["ProgramArguments"] as? [String]
    }

    private func runAtLoad() -> Bool {
        (properties()?["RunAtLoad"] as? Bool) ?? false
    }

    /// An agent counts as enabled for THIS app only if it runs the current
    /// bundle at login. A stale file (for example from a copy that has since
    /// been replaced, so it names a path that no longer exists) is repaired
    /// silently instead of being reported as on or off.
    private func repairIfStale() {
        guard runAtLoad(),
              let arguments = programArguments(), arguments.count == 2,
              let executable = arguments.first, arguments.last == "--background",
              let current = Bundle.main.executableURL else { return }
        if !FileManager.default.fileExists(atPath: executable) || executable != current.path {
            try? enable(executable: current.path)
        }
    }

    func prepareCheckbox(_ checkbox: NSButton) {
        repairIfStale()
        checkbox.state = FileManager.default.fileExists(atPath: path.path) ? .on : .off
    }

    func toggle(enable: Bool) throws {
        if enable {
            guard Bundle.main.bundleURL.pathExtension == "app",
                  let executable = Bundle.main.executableURL else {
                throw ClientError.failed("Login startup needs an installed HeyMa.app. Move the app to Applications and try again.")
            }
            try self.enable(executable: executable.path)
        } else {
            // Never bootout our own job from this process; it could kill the UI.
            // Removing the plist disables startup at the next login.
            try FileManager.default.removeItem(at: path)
        }
    }

    private func enable(executable: String) throws {
        let properties: [String: Any] = ["Label": loginLabel,
            "ProgramArguments": [executable, "--background"], "RunAtLoad": true]
        let data = try PropertyListSerialization.data(fromPropertyList: properties,
                                                       format: .xml, options: 0)
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try data.write(to: path, options: .atomic)
        // RunAtLoad takes effect at the next login. Bootstrapping now would
        // launch a second copy of the already-running app.
    }
}

private final class AppController: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let cli = VinylCLI()
    private var item: NSStatusItem!
    private var window: NSWindow!
    private var stateLabel = NSTextField(labelWithString: "Checking client…")
    private var detailLabel = NSTextField(wrappingLabelWithString: "")
    private var versionLabel = NSTextField(labelWithString: "")
    private var serverField = NSTextField()
    private var pairingLabel = NSTextField(labelWithString: "")
    private var hotkeyCheckbox = NSButton(checkboxWithTitle: "Use Control–Option–D to toggle dictation", target: nil, action: nil)
    private var loginCheckbox = NSButton(checkboxWithTitle: "Open HeyMa at login", target: nil, action: nil)
    private let toggleAgent = LaunchAgentClient(label: loginLabel)
    private var saveButton = NSButton(title: "Save server", target: nil, action: nil)
    // Section model (same as the Linux tray, components/heyma-shell model.MENU):
    // top Dictation toggle, then Wax, Vinyl, Phonograph sections, then Settings and Quit.
    private var dictationItem = NSMenuItem(title: "Start dictation", action: nil, keyEquivalent: "")
    private var cancelItem = NSMenuItem(title: "Cancel dictation", action: nil, keyEquivalent: "")
    private var menuState = NSMenuItem(title: "Checking client…", action: nil, keyEquivalent: "")
    private var vinylConnectedItem = NSMenuItem(title: "Connected: …", action: nil, keyEquivalent: "")
    private var vinylServerDownItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var vinylRestartHintItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private var vinylRestartItem = NSMenuItem(title: "Restart client", action: nil, keyEquivalent: "")
    private var phonographStatusItem = NSMenuItem(title: "Checking…", action: nil, keyEquivalent: "")
    private var phonographHealth = "checking…"
    private var phonographTimer: Timer?
    private var flashToken = 0
    /// Endpoint the running client dictates against (status field `server`; client 0.5.0
    /// has no such field, so this falls back to client.json and `endpointFromStatus` is false).
    private var runningServer = ""
    private var serverReachableFlag: Bool?
    private var endpointFromStatus = false
    private var timer: Timer?
    private var busy = false
    private var listening = false
    private var reachable = false
    private var lastError = ""
    private var trace = ""
    /// Status says the server cannot be reached: an explicit `server_reachable: false`,
    /// or the client's own connect failure (`client start HOST:PORT: …`).
    private var serverDown: Bool {
        if let flag = serverReachableFlag { return !flag }
        return lastError.hasPrefix("client start ")
    }
    private var hotkeyRef: EventHotKeyRef?
    private var hotkeyHandler: EventHandlerRef?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installMenu()
        buildWindow()
        installHotkeyHandler()
        if UserDefaults.standard.bool(forKey: hotkeyKey) { configureHotkey(true) }
        if !CommandLine.arguments.contains("--background") &&
            (!UserDefaults.standard.bool(forKey: settingsKey) || CommandLine.arguments.contains("--settings")) {
            showSettings()
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in self?.refresh() }
        if phonographEnabled {
            checkPhonograph()
            phonographTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.checkPhonograph() }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showSettings()
        return true
    }
    func windowWillClose(_ notification: Notification) { NSApp.setActivationPolicy(.accessory) }

    private func installMenu() {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "waveform", accessibilityDescription: "HeyMa idle")
        let menu = NSMenu()
        menuState.isEnabled = false
        menu.addItem(menuState)
        menu.addItem(.separator())

        // Top toggle. One item (as on Linux): Start dictation / Stop dictation.
        dictationItem.target = self; dictationItem.action = #selector(toggleDictationAction)
        menu.addItem(dictationItem)
        menu.addItem(.separator())

        // Wax section: filled by the Voice Memos bridge (S6, HEYMA-35).
        let wax = waxSectionItems()
        if !wax.isEmpty {
            let parent = NSMenuItem(title: "Wax", action: nil, keyEquivalent: "")
            let submenu = NSMenu(title: "Wax")
            wax.forEach { submenu.addItem($0) }
            parent.submenu = submenu
            menu.addItem(parent)
        }

        // Vinyl section.
        let vinyl = NSMenu(title: "Vinyl")
        for row in [vinylConnectedItem, vinylServerDownItem, vinylRestartHintItem] { row.isEnabled = false; vinyl.addItem(row) }
        vinylRestartItem.target = self; vinylRestartItem.action = #selector(restartClient)
        vinyl.addItem(vinylRestartItem)
        vinyl.addItem(.separator())
        cancelItem.target = self; cancelItem.action = #selector(cancel)
        vinyl.addItem(cancelItem)
        vinyl.addItem(withTitle: "Copy trace reference", action: #selector(copyTrace), keyEquivalent: "").target = self
        vinyl.addItem(withTitle: "Open client log", action: #selector(openLog), keyEquivalent: "").target = self
        let vinylParent = NSMenuItem(title: "Vinyl", action: nil, keyEquivalent: "")
        vinylParent.submenu = vinyl
        menu.addItem(vinylParent)

        // Phonograph section: nothing installs on a client; it is a hosted URL.
        if phonographEnabled {
            let phonograph = NSMenu(title: "Phonograph")
            phonographStatusItem.isEnabled = false
            phonograph.addItem(phonographStatusItem)
            phonograph.addItem(withTitle: "Open Phonograph", action: #selector(openPhonograph), keyEquivalent: "").target = self
            let parent = NSMenuItem(title: "Phonograph", action: nil, keyEquivalent: "")
            parent.submenu = phonograph
            menu.addItem(parent)
        }

        menu.addItem(.separator())
        menu.addItem(withTitle: "Settings…", action: #selector(showSettingsAction), keyEquivalent: ",").target = self
        menu.addItem(withTitle: "Quit HeyMa", action: #selector(quit), keyEquivalent: "q").target = self
        item.menu = menu
        updateUI()
    }

    /// The Wax section's rows. Empty until the Voice Memos bridge (S6, HEYMA-35) lands;
    /// an empty list adds no menu item, so there is never a dead entry.
    private func waxSectionItems() -> [NSMenuItem] { [] }

    private func checkPhonograph() {
        guard let url = URL(string: phonographURL + "/healthz") else { phonographHealth = "bad URL"; return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 4
        request.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            let health: String
            if let http = response as? HTTPURLResponse { health = (200..<300).contains(http.statusCode) ? "healthy" : "HTTP \(http.statusCode)" }
            else { health = "unreachable" }
            DispatchQueue.main.async { self?.phonographHealth = health; self?.updateUI() }
        }.resume()
    }

    private func label(_ text: String, size: CGFloat = 13, color: NSColor = .secondaryLabelColor) -> NSTextField {
        let view = NSTextField(wrappingLabelWithString: text)
        view.font = .systemFont(ofSize: size)
        view.textColor = color
        return view
    }

    private func heading(_ text: String) -> NSTextField {
        let view = label(text, size: 13, color: .labelColor)
        view.font = .systemFont(ofSize: 13, weight: .semibold)
        return view
    }

    private func row(_ views: [NSView], spacing: CGFloat = 12) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = spacing
        return stack
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 510, height: 610),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        // Programmatic NSWindows otherwise release themselves on close; retain
        // this reusable settings window for menu/Dock reopen throughout the app.
        window.isReleasedWhenClosed = false
        window.title = "HeyMa"
        window.minSize = NSSize(width: 440, height: 550)
        window.delegate = self
        window.center()
        let scroll = NSScrollView(frame: window.contentView!.bounds)
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        window.contentView = scroll
        let content = FlippedDocumentView(frame: NSRect(x: 0, y: 0, width: 510, height: 610))
        content.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = content
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 26),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -26),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 22),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -22),
            content.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            content.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor)
        ])
        let logoCard = NSView()
        logoCard.wantsLayer = true
        logoCard.layer?.backgroundColor = NSColor(calibratedRed: 0.17, green: 0.12, blue: 0.11, alpha: 1).cgColor
        logoCard.layer?.cornerRadius = 14
        logoCard.translatesAutoresizingMaskIntoConstraints = false
        let logo = NSImageView()
        logo.image = Bundle.main.url(forResource: "heyma-logo", withExtension: "png")
            .flatMap { NSImage(contentsOf: $0) }
        logo.imageScaling = .scaleProportionallyUpOrDown
        logo.translatesAutoresizingMaskIntoConstraints = false
        logoCard.addSubview(logo)
        stack.addArrangedSubview(logoCard)
        NSLayoutConstraint.activate([
            logoCard.widthAnchor.constraint(equalTo: stack.widthAnchor),
            logoCard.heightAnchor.constraint(equalToConstant: 148),
            logo.centerXAnchor.constraint(equalTo: logoCard.centerXAnchor),
            logo.centerYAnchor.constraint(equalTo: logoCard.centerYAnchor),
            logo.widthAnchor.constraint(equalToConstant: 180),
            logo.heightAnchor.constraint(equalToConstant: 120)
        ])
        stack.addArrangedSubview(label("Vinyl Dictation", size: 13))
        stack.setCustomSpacing(24, after: stack.arrangedSubviews.last!)
        stateLabel.font = .systemFont(ofSize: 17, weight: .medium)
        stack.addArrangedSubview(stateLabel)
        detailLabel.textColor = .secondaryLabelColor
        stack.addArrangedSubview(detailLabel)
        stack.addArrangedSubview(heading("CLIENT"))
        versionLabel.textColor = .secondaryLabelColor
        stack.addArrangedSubview(versionLabel)
        serverField.placeholderString = "server.example.com:7733"
        serverField.stringValue = endpoint()
        serverField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        serverField.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true
        saveButton.target = self; saveButton.action = #selector(saveServer)
        stack.addArrangedSubview(row([serverField, saveButton]))
        stack.addArrangedSubview(label("Server hostname and port. Saving applies on the next client restart; it never interrupts dictation. Restart the existing Vinyl LaunchAgent after finishing your session."))
        stack.addArrangedSubview(pairingLabel)
        stack.setCustomSpacing(24, after: stack.arrangedSubviews.last!)
        stack.addArrangedSubview(heading("SHORTCUTS & STARTUP"))
        hotkeyCheckbox.target = self; hotkeyCheckbox.action = #selector(toggleHotkey)
        hotkeyCheckbox.state = UserDefaults.standard.bool(forKey: hotkeyKey) ? .on : .off
        stack.addArrangedSubview(hotkeyCheckbox)
        stack.addArrangedSubview(label("Or bind ~/.local/bin/vinyl-toggle in Raycast, skhd, BetterTouchTool or Shortcuts. The native shortcut does not require Accessibility permission."))
        loginCheckbox.target = self; loginCheckbox.action = #selector(toggleLogin)
        toggleAgent.prepareCheckbox(loginCheckbox)
        stack.addArrangedSubview(loginCheckbox)
        stack.setCustomSpacing(24, after: stack.arrangedSubviews.last!)
        stack.addArrangedSubview(heading("PERMISSIONS"))
        stack.addArrangedSubview(label("Vinyl's Python client needs Microphone and its System Events injection needs Accessibility. HeyMa does not request either permission; check the relevant entries in System Settings after first dictation."))
        let microphone = NSButton(title: "Microphone settings", target: self, action: #selector(openMicrophone))
        let accessibility = NSButton(title: "Accessibility settings", target: self, action: #selector(openAccessibility))
        stack.addArrangedSubview(row([microphone, accessibility]))
        stack.setCustomSpacing(24, after: stack.arrangedSubviews.last!)
        stack.addArrangedSubview(heading("WORDS & REWRITES"))
        stack.addArrangedSubview(label("Dictionary corrections run on the server, not this Mac. Manage shared terms with wax dict on the server. Vinyl-only rewrite rules also run where decoding happens; this client does not edit either set."))
    }

    private func showSettings() {
        UserDefaults.standard.set(true, forKey: settingsKey)
        NSApp.setActivationPolicy(.regular)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func refresh() {
        guard !busy else { return }
        busy = true
        cli.submit(["status"], timeout: 5) { [weak self] result in
            guard let self = self else { return }
            self.busy = false
            switch result {
            case .failure(let error): self.reachable = false; self.listening = false; self.lastError = error.localizedDescription
            case .success(let response):
                // Never use the CLI's stale mirror fallback (exit 1) as live proof.
                if response.code != 0 { self.reachable = false; self.listening = false
                    self.lastError = "Client unavailable (status exit \(response.code)). Open the log or run vinyl doctor." }
                else if let data = response.output.data(using: .utf8),
                        let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                        let state = object["state"] as? String, ["idle", "listening"].contains(state) {
                    self.reachable = true; self.listening = state == "listening"
                    self.lastError = object["last_error"] as? String ?? ""
                    self.trace = object["trace_ref"] as? String ?? ""
                    if let server = object["server"] as? String, !server.isEmpty {
                        self.runningServer = server; self.endpointFromStatus = true
                    } else { self.runningServer = endpoint(); self.endpointFromStatus = false }
                    // Forward-compatible: a client that says its server is unreachable.
                    self.serverReachableFlag = object["server_reachable"] as? Bool
                } else { self.reachable = false; self.listening = false; self.lastError = ClientError.malformedStatus.localizedDescription }
            }
            self.updateUI()
        }
    }

    private func updateUI() {
        let symbol = !reachable || !lastError.isEmpty ? "exclamationmark.circle" : listening ? "mic.fill" : "waveform"
        item?.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "HeyMa \(listening ? "listening" : reachable ? "idle" : "unavailable")")
        let status = !reachable ? "Client unavailable" : listening ? "Listening" : lastError.isEmpty ? "Ready to dictate" : "Client reported an error"
        menuState.title = "HeyMa · \(status)"
        stateLabel.stringValue = status
        detailLabel.stringValue = lastError.isEmpty ? (trace.isEmpty ? "No recent trace" : "Trace: \(trace)") : lastError
        versionLabel.stringValue = "HeyMa \(heymaVersion()) · installed Vinyl \(installedVersion())"
        pairingLabel.stringValue = pairedMarker() ? "Pairing file present (connection not verified)" : "Pairing file missing. Copy the server token to ~/.config/vinyl/token."
        dictationItem.title = listening ? "Stop dictation" : "Start dictation"
        dictationItem.isEnabled = reachable && !busy
        cancelItem.isEnabled = reachable && listening && !busy

        // Vinyl section: the endpoint the running client uses, a restart hint when
        // client.json names another one, and a server-down line.
        let configured = endpoint()
        let running = runningServer.isEmpty ? configured : runningServer
        let mismatch = reachable && !configured.isEmpty && !running.isEmpty && configured != running
        vinylConnectedItem.title = !reachable ? "Client not running"
            : running.isEmpty ? "Connected: no server configured" : "Connected: \(running)"
        vinylRestartHintItem.title = "Restart the client to use \(configured)"
        vinylRestartHintItem.isHidden = !mismatch
        vinylRestartItem.isHidden = !mismatch
        vinylRestartItem.isEnabled = mismatch && !listening && !busy
        let down = reachable && serverDown
        vinylServerDownItem.title = "Server unreachable\(running.isEmpty ? "" : ": \(running)")"
        vinylServerDownItem.isHidden = !down
        phonographStatusItem.title = "\(phonographURL.replacingOccurrences(of: "https://", with: "")): \(phonographHealth)"
        saveButton.isEnabled = !listening && !busy
    }

    private func action(_ name: String, timeout: TimeInterval = 12) {
        guard reachable, !busy else { return }
        busy = true; updateUI()
        cli.submit([name], timeout: timeout) { [weak self] result in
            guard let self = self else { return }
            self.busy = false
            switch result {
            case .failure(let error): self.lastError = error.localizedDescription
            case .success(let output):
                if output.code != 0 { self.lastError = "\(name) failed: \(output.detail)" }
                else if let data = output.output.data(using: .utf8),
                        let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.lastError = body["last_error"] as? String ?? ""
                    self.trace = body["trace_ref"] as? String ?? self.trace
                    self.listening = (body["state"] as? String) == "listening"
                } else { self.lastError = "\(name) returned invalid status. Check the log." }
            }
            self.updateUI()
            self.refresh()
        }
    }

    private func alert(_ message: String) {
        let panel = NSAlert()
        panel.messageText = "HeyMa"
        panel.informativeText = message
        panel.alertStyle = .warning
        panel.runModal()
    }

    @objc private func toggleDictationAction() { toggleDictation() }
    @objc private func cancel() { action("cancel", timeout: 18) }
    @objc private func showSettingsAction() { showSettings() }
    @objc private func quit() { NSApp.terminate(nil) }
    @objc private func openLog() {
        let path = home.appendingPathComponent(".local/state/vinyl/client.log")
        if FileManager.default.fileExists(atPath: path.path) { NSWorkspace.shared.open(path) }
        else { alert("No client log yet at \(path.path). Start the Vinyl client or run vinyl doctor.") }
    }
    @objc private func copyTrace() {
        guard !trace.isEmpty else { alert("No trace reference is available from the live client yet. Start a dictation first."); return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(trace, forType: .string)
    }
    @objc private func openPhonograph() {
        if let url = URL(string: phonographURL) { NSWorkspace.shared.open(url) }
    }
    @objc private func restartClient() {
        // Same command a person would run; never while dictating (kickstart -k kills the session).
        guard reachable, !listening, !busy else { return }
        busy = true; updateUI()
        cli.work({ [cli] in
            let result = try cli.runURL(URL(fileURLWithPath: "/bin/launchctl"),
                                        ["kickstart", "-k", "gui/\(getuid())/\(clientLabel)"], timeout: 15)
            guard result.code == 0 else { throw ClientError.failed("Could not restart the client: \(result.detail)") }
        }) { [weak self] result in
            guard let self = self else { return }
            self.busy = false
            if case .failure(let error) = result { self.lastError = error.localizedDescription }
            else { self.lastError = ""; self.runningServer = ""; self.endpointFromStatus = false }
            self.updateUI()
            // The restarted client needs a moment to answer on its socket.
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.refresh() }
        }
    }
    @objc private func openMicrophone() { openSettings("Privacy_Microphone") }
    @objc private func openAccessibility() { openSettings("Privacy_Accessibility") }
    private func openSettings(_ pane: String) {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") else { return }
        NSWorkspace.shared.open(url)
    }

    @objc private func saveServer() {
        guard let server = validatedEndpoint(serverField.stringValue) else {
            alert("Enter a DNS hostname and port, for example big-chungus.burro-salmon.ts.net:7733 (port 1–65535).")
            return
        }
        guard !busy else { return }
        busy = true; updateUI()
        cli.work({ [cli] in
            // Persist only: an external hotkey can start a session at any time,
            // so a status check cannot make launchctl kickstart -k safe here.
            // Saving also remains available when the client needs repair.
            let saved = try cli.run(["setup", "--server", server, "--no-enable"], timeout: 15)
            guard saved.code == 0 else { throw ClientError.failed("Could not save server: \(saved.detail)") }
        }) { [weak self] result in
            guard let self = self else { return }
            self.busy = false
            switch result {
            case .success:
                self.serverField.stringValue = server; self.lastError = ""
                self.alert("Server saved for the next client restart. Finish any dictation, then restart the existing Vinyl LaunchAgent to apply it. The running session was not interrupted.")
            case .failure(let error): self.lastError = error.localizedDescription; self.alert(self.lastError)
            }
            self.updateUI(); self.refresh()
        }
    }

    private func installHotkeyHandler() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let handler: EventHandlerUPP = { _, event, context in
            guard let event = event, let context = context else { return noErr }
            var id = EventHotKeyID()
            let result = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                           EventParamType(typeEventHotKeyID), nil,
                                           MemoryLayout<EventHotKeyID>.size, nil, &id)
            if result == noErr && id.id == 1 {
                let app = Unmanaged<AppController>.fromOpaque(context).takeUnretainedValue()
                DispatchQueue.main.async { app.toggleDictation() }
            }
            return noErr
        }
        let context = UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())
        InstallEventHandler(GetApplicationEventTarget(), handler, 1, &spec, context, &hotkeyHandler)
    }

    private func configureHotkey(_ enable: Bool) {
        if let ref = hotkeyRef { UnregisterEventHotKey(ref); hotkeyRef = nil }
        guard enable else { hotkeyCheckbox.state = .off; UserDefaults.standard.set(false, forKey: hotkeyKey); return }
        let id = EventHotKeyID(signature: 0x484D4150, id: 1)
        let result = RegisterEventHotKey(UInt32(kVK_ANSI_D), UInt32(controlKey | optionKey), id,
                                        GetApplicationEventTarget(), 0, &hotkeyRef)
        if result == noErr { hotkeyCheckbox.state = .on; UserDefaults.standard.set(true, forKey: hotkeyKey) }
        else { hotkeyCheckbox.state = .off; UserDefaults.standard.set(false, forKey: hotkeyKey)
            alert("Control–Option–D could not be registered (macOS code \(result)). Another app may own that shortcut.") }
    }
    private func toggleDictation() {
        guard reachable, !busy else {
            // The press is dropped. Never silently: beep and flash the status item.
            pressFeedback(reachable ? "busy" : "offline")
            return
        }
        action(listening ? "stop" : "start", timeout: listening ? 55 : 18)
    }

    /// Hotkey press that cannot be honoured (Vinyl story 2.7B): audible and visible.
    private func pressFeedback(_ reason: String) {
        NSSound.beep()
        flashToken += 1
        let token = flashToken
        item?.button?.title = " \(reason)"
        item?.button?.image = NSImage(systemSymbolName: "exclamationmark.circle", accessibilityDescription: "HeyMa \(reason)")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
            guard let self = self, self.flashToken == token else { return }
            self.item?.button?.title = ""
            self.updateUI()
        }
    }
    @objc private func toggleHotkey() { configureHotkey(hotkeyCheckbox.state == .on) }

    @objc private func toggleLogin() {
        let enable = loginCheckbox.state == .on
        cli.work({ try self.toggleAgent.toggle(enable: enable) }) { [weak self] result in
            guard let self = self else { return }
            if case .failure(let error) = result {
                self.loginCheckbox.state = enable ? .off : .on
                self.alert(error.localizedDescription)
            }
        }
    }
}

let application = NSApplication.shared
private let controller = AppController() // retain the delegate for the lifetime of the process
application.delegate = controller
application.run()
