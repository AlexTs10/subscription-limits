import AppKit

struct Limit: Decodable, Identifiable {
    let id: String
    let name: String
    let remaining: Double?
    let resetsAt: String?
}

struct Account: Decodable, Identifiable {
    let id: String
    let label: String
    let provider: String
    let detail: String
    let limits: [Limit]
    let error: String?
    let fetchedAt: String?
    let retryAt: Double?
    let apiRetryAt: Double?
    let errorCode: String?
    let fallbackError: String?
}

struct Snapshot: Decodable {
    let accounts: [Account]
    let checkedAt: String
    let error: String?
}

func dateFromISO(_ text: String?) -> Date? {
    guard let text else { return nil }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.date(from: text) ?? ISO8601DateFormatter().date(from: text)
}

func intervalText(_ seconds: TimeInterval) -> String {
    let minutes = max(1, Int(ceil(seconds / 60)))
    if minutes >= 1440 { return "\(minutes / 1440)d \((minutes % 1440) / 60)h" }
    if minutes >= 60 { return "\(minutes / 60)h \(minutes % 60)m" }
    return "\(minutes)m"
}

func percentText(_ value: Double) -> String {
    String(format: "%.0f%%", value)
}

@MainActor
final class LimitsModel {
    var accounts: [Account] = []
    var refreshing = false
    var message: String?
    var checkedAt: Date?
    var onChange: (() -> Void)?
    private var lastAttempt = Date.distantPast
    private var timer: Timer?
    private var worker: Process?

    init() {
        let cache = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Subscription Limits/snapshot.json")
        if let data = try? Data(contentsOf: cache) { accept(data) }
        let timer = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func accept(_ data: Data) {
        defer { onChange?() }
        guard let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else {
            message = "Could not read limits. Try Refresh."
            return
        }
        if snapshot.error == nil {
            accounts = snapshot.accounts
            checkedAt = dateFromISO(snapshot.checkedAt)
        }
        message = snapshot.error
    }

    func refresh() {
        // Reopening the menu must not flood the provider with requests.
        guard !refreshing, Date().timeIntervalSince(lastAttempt) >= 300 else { return }
        defer { onChange?() }
        guard let script = Bundle.main.path(forResource: "limits_reader", ofType: "py") else {
            message = "The reader is missing. Install the app again."
            return
        }
        lastAttempt = Date()
        refreshing = true
        message = nil
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [script]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        worker = process
        do {
            try process.run()
        } catch {
            worker = nil
            refreshing = false
            message = "Could not start the reader."
            return
        }
        DispatchQueue.global(qos: .utility).async {
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            Task { @MainActor [weak self] in
                guard let self, self.worker === process else { return }
                self.worker = nil
                self.refreshing = false
                self.accept(data)
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 90) { [weak self] in
            guard let self, self.worker === process, process.isRunning else { return }
            process.terminate()
            self.worker = nil
            self.refreshing = false
            self.message = "The update took too long. Try Refresh."
            self.onChange?()
        }
    }

    func stop() {
        timer?.invalidate()
        if worker?.isRunning == true { worker?.terminate() }
    }
}

// NSMenu supplies the material, corner radius, shadow, separators and keyboard
// behavior. These read-only views contain only standard AppKit controls.
@MainActor
final class AccountUsageView: NSView {
    static let menuWidth: CGFloat = 342
    private let rowHeight: CGFloat = 35
    override var isFlipped: Bool { true }

    init(account: Account) {
        super.init(frame: .zero)
        setAccessibilityRole(.group)
        setAccessibilityLabel(account.label)
        update(account)
    }

    required init?(coder: NSCoder) { nil }

    private func label(_ text: String, frame: NSRect, secondary: Bool = false) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = secondary ? .systemFont(ofSize: NSFont.smallSystemFontSize) : .menuFont(ofSize: 0)
        field.textColor = secondary ? .secondaryLabelColor : .labelColor
        field.lineBreakMode = .byTruncatingTail
        field.frame = frame
        field.toolTip = text
        addSubview(field)
        return field
    }

    func update(_ account: Account) {
        subviews.forEach { $0.removeFromSuperview() }
        let stale = account.isStale
        let rows = max(1, account.limits.count)
        let hasError = account.error != nil
        frame.size = NSSize(width: Self.menuWidth,
                            height: CGFloat(rows) * rowHeight + (hasError ? 20 : 0) + 2)

        if account.limits.isEmpty {
            _ = label("Limits unavailable", frame: NSRect(x: 20, y: 2, width: 300, height: 18), secondary: true)
        }

        for (index, limit) in account.limits.enumerated() {
            let y = CGFloat(index) * rowHeight
            let value = limit.remaining.map { percentText($0) + " left" } ?? "Not reported"
            let title = limit.name + " " + value
            let field = label(title, frame: NSRect(x: 20, y: y, width: 209, height: 18))
            let attributed = NSMutableAttributedString(string: title, attributes: [
                .font: NSFont.menuFont(ofSize: 0),
                .foregroundColor: stale ? NSColor.secondaryLabelColor : NSColor.labelColor
            ])
            let range = NSRange(location: (limit.name + " ").utf16.count, length: value.utf16.count)
            attributed.addAttribute(.font, value: NSFont.systemFont(ofSize: NSFont.systemFontSize, weight: .semibold), range: range)
            if !stale, let remaining = limit.remaining, remaining <= 10 {
                attributed.addAttribute(.foregroundColor, value: NSColor.systemRed, range: range)
            }
            field.attributedStringValue = attributed

            if let reset = dateFromISO(limit.resetsAt) {
                let duration = reset.timeIntervalSinceNow
                let text = duration > 0 ? "Resets in " + intervalText(duration) : "Reset due"
                let caption = label(text, frame: NSRect(x: 226, y: y + 2, width: 96, height: 16), secondary: true)
                caption.alignment = .right
                caption.toolTip = reset.formatted(date: .complete, time: .shortened)
            }

            if let remaining = limit.remaining {
                let progress = NSProgressIndicator(frame: NSRect(x: 20, y: y + 21, width: 302, height: 6))
                progress.style = .bar
                progress.controlSize = .small
                progress.isIndeterminate = false
                progress.minValue = 0
                progress.maxValue = 100
                progress.doubleValue = min(100, max(0, remaining))
                progress.isDisplayedWhenStopped = true
                progress.alphaValue = stale ? 0.45 : 1
                progress.setAccessibilityLabel(limit.name + " remaining")
                addSubview(progress)
            }
        }

        if let error = account.error {
            let field = label(account.errorText ?? error,
                              frame: NSRect(x: 20, y: CGFloat(rows) * rowHeight, width: 302, height: 18), secondary: true)
            field.toolTip = [error, account.fallbackError].compactMap { $0 }.joined(separator: "\n")
        }
    }
}

extension Account {
    var errorText: String? {
        guard let error else { return nil }
        if errorCode == "rate_limited" || error == "Provider busy · will retry" {
            let deadline = max(retryAt ?? 0, apiRetryAt ?? 0)
            if deadline > Date().timeIntervalSince1970 {
                return "Usage checks paused · retry in " + intervalText(deadline - Date().timeIntervalSince1970)
            }
            return "Usage checks paused · retry due"
        }
        return error
    }

    var isStale: Bool {
        error != nil || dateFromISO(fetchedAt).map { Date().timeIntervalSince($0) > 600 } ?? true
    }

    var menuTitle: String {
        label + (isStale && !limits.isEmpty ? " · Old data" : "")
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var model: LimitsModel!
    private let menu = NSMenu(title: "Subscription Limits")
    private var menuIsOpen = false
    private var accountViews: [String: AccountUsageView] = [:]
    private var accountHeaders: [String: NSMenuItem] = [:]
    private var refreshItem: NSMenuItem?
    private var checkedItem: NSMenuItem?
    private var menuTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
        if let existing = running.first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
            existing.activate()
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.accessory)
        model = LimitsModel()
        menu.autoenablesItems = false
        menu.minimumWidth = AccountUsageView.menuWidth
        menu.delegate = self
        let autosaveName = "SubscriptionLimits.MenuBar"
        let positionKey = "NSStatusItem Preferred Position " + autosaveName
        // New items can land behind the camera cutout on a crowded menu bar.
        // Seed a position at the right edge once; preserve later user moves.
        if UserDefaults.standard.object(forKey: positionKey) == nil {
            UserDefaults.standard.set(0, forKey: positionKey)
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.autosaveName = autosaveName
        statusItem.isVisible = true
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "gauge.with.dots.needle.50percent", accessibilityDescription: "Subscription Limits")
                ?? NSImage(systemSymbolName: "chart.bar", accessibilityDescription: "Subscription Limits")
            button.image?.isTemplate = true
            button.imageScaling = .scaleProportionallyDown
            if button.image == nil { button.title = "%" }
            button.setAccessibilityLabel("Subscription Limits")
        }
        statusItem.menu = menu
        model.onChange = { [weak self] in self?.modelDidChange() }
        rebuildMenu()
        updateTooltip()
        model.refresh()
        // Record this app's geometry after AppKit has placed the icon. No
        // screenshots or information from other applications are recorded.
        let placementCheck = Timer(timeInterval: 1, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.recordMenuBarPosition() }
        }
        RunLoop.main.add(placementCheck, forMode: .common)
    }

    private func recordMenuBarPosition() {
        guard let button = statusItem.button, let window = button.window, let screen = window.screen else { return }
        let frame = window.convertToScreen(button.convert(button.bounds, to: nil))
        let topAreas = [screen.auxiliaryTopLeftArea, screen.auxiliaryTopRightArea].compactMap { $0 }
        let fits = topAreas.isEmpty ? screen.frame.contains(frame) : topAreas.contains {
            frame.minX >= $0.minX && frame.maxX <= $0.maxX
        }
        let report: [String: Any] = [
            "checkedAt": ISO8601DateFormatter().string(from: Date()),
            "visible": statusItem.isVisible,
            "hasIcon": button.image != nil,
            "iconFrame": NSStringFromRect(frame),
            "outsideCameraCutout": fits,
            "screenTopAreas": topAreas.map { NSStringFromRect($0) }
        ]
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Subscription Limits")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            let file = directory.appendingPathComponent("menu-bar-status.json")
            try JSONSerialization.data(withJSONObject: report, options: .prettyPrinted).write(to: file, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        } catch {
            // Diagnostics must not interrupt the menu or its usage updates.
        }
    }

    private func informationItem(_ text: String) -> NSMenuItem {
        let item = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private var checkedText: String {
        if let message = model.message { return message }
        guard let checkedAt = model.checkedAt else { return "Waiting for limits" }
        let age = Date().timeIntervalSince(checkedAt)
        return age < 60 ? "Checked just now" : "Checked " + intervalText(age) + " ago"
    }

    private func rebuildMenu() {
        menu.removeAllItems()
        accountViews.removeAll()
        accountHeaders.removeAll()

        for (index, account) in model.accounts.enumerated() {
            if index > 0 { menu.addItem(.separator()) }
            let header = NSMenuItem.sectionHeader(title: account.menuTitle)
            let updated = dateFromISO(account.fetchedAt).map { "Updated " + $0.formatted(date: .abbreviated, time: .shortened) } ?? "No update yet"
            header.toolTip = [account.detail, updated].filter { !$0.isEmpty }.joined(separator: "\n")
            menu.addItem(header)
            accountHeaders[account.id] = header

            let usage = AccountUsageView(account: account)
            let item = NSMenuItem()
            item.view = usage
            item.isEnabled = false
            menu.addItem(item)
            accountViews[account.id] = usage
        }

        if model.accounts.isEmpty {
            menu.addItem(informationItem(model.refreshing ? "Reading local accounts…" : "No accounts found"))
        }
        menu.addItem(.separator())
        let checked = informationItem(checkedText)
        menu.addItem(checked)
        checkedItem = checked

        let refresh = NSMenuItem(title: model.refreshing ? "Updating…" : "Refresh",
                                 action: #selector(refreshLimits), keyEquivalent: "r")
        refresh.target = self
        refresh.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: nil)
        refresh.isEnabled = !model.refreshing
        menu.addItem(refresh)
        refreshItem = refresh
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Subscription Limits", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApp
        menu.addItem(quit)
    }

    private func modelDidChange() {
        updateTooltip()
        if !menuIsOpen {
            rebuildMenu()
            return
        }
        // Keep menu items stable while the user moves the pointer or uses the
        // keyboard. New account sections are added the next time it opens.
        for account in model.accounts {
            accountViews[account.id]?.update(account)
            accountHeaders[account.id]?.title = account.menuTitle
        }
        refreshItem?.title = model.refreshing ? "Updating…" : "Refresh"
        refreshItem?.isEnabled = !model.refreshing
        checkedItem?.title = checkedText
    }

    private func updateTooltip() {
        statusItem.button?.toolTip = model.accounts.map { account in
            let limits = account.limits.map { "\($0.name): \($0.remaining.map { percentText($0) + " left" } ?? "not reported")" }.joined(separator: ", ")
            return account.label + "\n" + (limits.isEmpty ? "Limits unavailable" : limits) + (account.isStale ? " (old or unavailable)" : "")
        }.joined(separator: "\n\n")
    }

    @objc private func refreshLimits() { model.refresh() }

    private func showMenu() {
        guard !menuIsOpen else { return }
        NSApp.activate(ignoringOtherApps: true)
        statusItem.button?.performClick(nil)
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        if !menuIsOpen { rebuildMenu() }
    }

    func menuWillOpen(_ menu: NSMenu) {
        menuIsOpen = true
        menuTimer?.invalidate()
        let timer = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.modelDidChange() }
        }
        RunLoop.main.add(timer, forMode: .common)
        menuTimer = timer
        model.refresh()
    }

    func menuDidClose(_ menu: NSMenu) {
        menuIsOpen = false
        menuTimer?.invalidate()
        menuTimer = nil
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        // A menu tracks events until it closes. Return from the launch event
        // before opening it so Finder and other callers do not wait on it.
        DispatchQueue.main.async { [weak self] in self?.showMenu() }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        menuTimer?.invalidate()
        model?.stop()
    }
}

@main
struct SubscriptionLimitsApp {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
