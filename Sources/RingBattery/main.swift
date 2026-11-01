import Cocoa
import UserNotifications
import Darwin
import CoreBluetooth
import Security
import ServiceManagement

let support = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/RingBattery")

struct MonitorState: Codable {
    var battery: Int?
    var lastRead: Double?
    var startedAt = Date().timeIntervalSince1970
    var lowSent = false
    var unavailableSent = false

    mutating func observe(_ value: Int?, now: Double, canNotify: Bool) -> String? {
        if let value = value {
            battery = value
            lastRead = now
            unavailableSent = false
            if value > 30 { lowSent = false }
            if value <= 20 && !lowSent && canNotify { lowSent = true; return "low" }
        } else if now - (lastRead ?? startedAt) >= 7200 && !unavailableSent && canNotify {
            unavailableSent = true
            return "unavailable"
        }
        return nil
    }
}

struct DeskReminder {
    var startedAt: Double
    var sent = false
    mutating func tick(now: Double, idle: Double, canNotify: Bool) -> Bool {
        if idle >= 300 { startedAt = now; sent = false }
        if now - startedAt >= 3000 && !sent && canNotify { sent = true; return true }
        return false
    }
}

struct Reading: Decodable {
    var ok: Bool
    var batteryPercent: Int?
    var measuredAt: Double?
    var error: String?
    var cancelled: Bool?
    var heartRate: Int?
    var hrvRmssd: Double?
    var validBeats: Int?
    var coveragePercent: Int?
    var rings: [NearbyRing]?
    var key: String?
}

struct NearbyRing: Decodable {
    var address: String
    var name: String
    var rssi: Int?
}

struct Connection {
    var key: String
    var address: String
}

func normalizedKey(_ text: String) -> String? {
    let value = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return value.utf8.count == 32 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) } ? value : nil
}

let keyQuery: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                             kSecAttrService as String: "io.github.bodharma.ringbattery",
                             kSecAttrAccount as String: "ring-authentication"]

func readKeychain() -> String? {
    var query = keyQuery
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
          let data = result as? Data, let key = String(data: data, encoding: .utf8) else { return nil }
    return normalizedKey(key)
}

func saveKeychain(_ key: String) throws {
    let attributes: [String: Any] = [kSecValueData as String: Data(key.utf8)]
    var result = SecItemUpdate(keyQuery as CFDictionary, attributes as CFDictionary)
    if result == errSecItemNotFound {
        var query = keyQuery.merging(attributes) { _, value in value }
        query[kSecAttrLabel as String] = "Ring Battery authentication key"
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        result = SecItemAdd(query as CFDictionary, nil)
    }
    guard result == errSecSuccess else {
        throw NSError(domain: NSOSStatusErrorDomain, code: Int(result), userInfo: [NSLocalizedDescriptionKey: "Could not save the ring key in your login Keychain. Unlock Keychain and try again."])
    }
}

func selfTest() {
    var state = MonitorState(startedAt: 0)
    assert(state.observe(20, now: 0, canNotify: false) == nil)
    assert(state.observe(20, now: 1, canNotify: true) == "low")
    assert(state.observe(10, now: 2, canNotify: true) == nil)
    assert(state.observe(31, now: 3, canNotify: true) == nil)
    assert(state.observe(19, now: 4, canNotify: true) == "low")
    assert(state.observe(nil, now: 7204, canNotify: true) == "unavailable")
    assert(state.observe(nil, now: 7300, canNotify: true) == nil)
    assert(state.observe(80, now: 7400, canNotify: true) == nil)
    assert(state.observe(nil, now: 14600, canNotify: true) == "unavailable")
    var desk = DeskReminder(startedAt: 0)
    assert(!desk.tick(now: 2999, idle: 0, canNotify: true))
    assert(desk.tick(now: 3000, idle: 0, canNotify: true))
    assert(!desk.tick(now: 3060, idle: 0, canNotify: true))
    assert(!desk.tick(now: 3300, idle: 300, canNotify: true))
    assert(!desk.tick(now: 6299, idle: 0, canNotify: true))
    assert(desk.tick(now: 6300, idle: 0, canNotify: true))
    assert(normalizedKey(" 00000000000000000000000000000000\n") != nil)
    assert(normalizedKey("key") == nil)
    assert(normalizedKey(String(repeating: "g", count: 32)) == nil)
    print("Alert, desk reminder and key validation self-checks passed")
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, CBCentralManagerDelegate, NSWindowDelegate {
    let defaults = UserDefaults.standard
    let center = UNUserNotificationCenter.current()
    var status: NSStatusItem!
    var state = MonitorState()
    var desk = DeskReminder(startedAt: Date().timeIntervalSince1970)
    var process: Process?
    var canNotify = false
    var available = false
    var checkingIn = false
    var quitting = false
    var refreshAfterCleanup = false
    var nextPoll = Date()
    var checkInEnd: Date?
    var lastError: String?
    var timer: Timer?
    var wakeObserver: NSObjectProtocol?
    var bluetooth: CBCentralManager?
    var connection: Connection?
    var setupWindow: NSWindow?
    var ringPicker: NSPopUpButton?
    var keyField: NSSecureTextField?
    var setupStatus: NSTextField?
    var nearbyRings: [NearbyRing] = []
    var needsKeyMigration = false
    var advancedControls: NSStackView?

    var breaksEnabled: Bool {
        get { defaults.object(forKey: "breaksEnabled") as? Bool ?? false }
        set { defaults.set(newValue, forKey: "breaksEnabled") }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        do {
            try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: support.path)
        } catch { showMessage("Cannot open local storage", "Ring Battery needs access to its Application Support folder."); NSApp.terminate(nil); return }
        loadConnection()
        if let data = defaults.data(forKey: "monitorState"), let saved = try? JSONDecoder().decode(MonitorState.self, from: data) { state = saved }
        status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        bluetooth = CBCentralManager(delegate: self, queue: .main, options: [CBCentralManagerOptionShowPowerAlertKey: false])
        center.delegate = self
        requestNotifications()
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self = self else { return }
            self.desk = DeskReminder(startedAt: Date().timeIntervalSince1970)
            if self.process != nil { self.refreshAfterCleanup = true; self.stopReader() }
            else { self.runReader() }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.tick() }
        if connection != nil { runReader() }
        else { render(); DispatchQueue.main.async { self.connectRing() } }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn && !quitting && connection != nil {
            if process == nil { runReader() } else { refreshAfterCleanup = true }
        }
        render()
    }

    func requestNotifications() {
        center.requestAuthorization(options: [.alert, .sound]) { [weak self] _, _ in self?.updatePermission() }
    }

    func updatePermission() {
        center.getNotificationSettings { [weak self] settings in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.canNotify = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
                self.render()
            }
        }
    }

    func send(_ id: String, title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil)) { [weak self] error in
            if error != nil { DispatchQueue.main.async {
                guard let self = self else { return }
                if id == "low" { self.state.lowSent = false }
                if id == "unavailable" { self.state.unavailableSent = false }
                if id == "desk" { self.desk.sent = false }
                self.save()
            } }
        }
    }

    func notifyBattery(_ value: Int?) {
        if let kind = state.observe(value, now: Date().timeIntervalSince1970, canNotify: canNotify) {
            if kind == "low" {
                send(kind, title: "Charge your Oura ring", body: "Battery is \(value ?? state.battery ?? 0)%. Put the ring on its charger.")
            } else {
                send(kind, title: "Oura battery status unavailable", body: "No fresh reading for two hours. The ring may be out of range, busy, or out of battery.")
            }
        }
        save()
    }

    func save() {
        if let data = try? JSONEncoder().encode(state) { defaults.set(data, forKey: "monitorState") }
    }

    func tick() {
        updatePermission()
        let now = Date()
        if connection != nil && setupWindow?.isVisible != true && process == nil && now >= nextPoll { runReader() }
        if connection != nil && process == nil && !available { notifyBattery(nil) }
        if breaksEnabled {
            let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: UInt32.max)!)
            if desk.tick(now: now.timeIntervalSince1970, idle: idle, canNotify: canNotify) {
                send("desk", title: "Time for a desk break", body: "About 50 minutes at your Mac. Stand up or walk for a few minutes when convenient.")
            }
        }
        render()
    }

    @objc func runReader() { startReader(checkIn: false) }

    func startReader(checkIn: Bool) {
        guard process == nil, let connection = connection, setupWindow?.isVisible != true, !quitting else { return }
        checkingIn = checkIn
        checkInEnd = checkIn ? Date().addingTimeInterval(315) : nil
        runBackend(checkIn ? ["--check-in"] : [], request: ["key": connection.key, "address": connection.address], timeout: checkIn ? 355 : 50) { [weak self] reading in
            self?.finishedReading(reading, checkIn: checkIn)
        }
    }

    func runBackend(_ arguments: [String], request: [String: String]? = nil, timeout: Double = 50, completion: @escaping (Reading?) -> Void) {
        guard process == nil else { return }
        let child = Process()
        let output = Pipe()
        let input = Pipe()
        child.executableURL = Bundle.main.resourceURL?.appendingPathComponent("Reader/ring-reader")
        child.arguments = arguments
        child.standardInput = input
        child.standardOutput = output
        child.standardError = FileHandle.nullDevice
        child.environment = ProcessInfo.processInfo.environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
        process = child
        child.terminationHandler = { [weak self] _ in
            let data = output.fileHandleForReading.readDataToEndOfFile()
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let reading = try? decoder.decode(Reading.self, from: data)
            DispatchQueue.main.async {
                guard let self = self, self.process === child else { return }
                self.process = nil
                self.checkingIn = false
                self.checkInEnd = nil
                completion(reading)
                self.render()
                if self.quitting { NSApp.reply(toApplicationShouldTerminate: true) }
                else if self.refreshAfterCleanup {
                    self.refreshAfterCleanup = false
                    self.runReader()
                }
            }
        }
        do {
            try child.run()
            if let request = request { try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: request)) }
            try input.fileHandleForWriting.close()
        } catch {
            if child.isRunning { stopReader() }
            else { process = nil; checkingIn = false; checkInEnd = nil; completion(nil); render() }
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + timeout) { [weak self, weak child] in
            if let child = child, self?.process === child, child.isRunning { self?.stopReader() }
        }
        render()
    }

    func finishedReading(_ reading: Reading?, checkIn: Bool) {
        nextPoll = Date().addingTimeInterval(1800)
        let fresh = reading?.measuredAt.map { $0.isFinite && abs(Date().timeIntervalSince1970 - $0) < 120 } ?? false
        if checkIn {
            if reading?.cancelled == true { lastError = nil }
            else if reading?.ok == true, fresh, let rate = reading?.heartRate, (30...200).contains(rate) {
                lastError = nil
                send("check-in", title: "Resting check-in complete", body: "Average heart rate: \(rate) bpm. Open the Oura menu for signal coverage and experimental HRV.")
            } else {
                lastError = reading?.error ?? "Check-in did not return a valid result. Refresh battery to retry ring cleanup."
                if quitting { send("check-in-error", title: "Check-in needs attention", body: lastError!) }
                else { showMessage("Check-in unavailable", lastError!) }
            }
        } else if reading?.ok == true, fresh, let battery = reading?.batteryPercent, (0...100).contains(battery) {
            available = true
            lastError = nil
            if needsKeyMigration, let connection = connection {
                do { try saveKeychain(connection.key); defaults.set(connection.address, forKey: "ringAddress"); needsKeyMigration = false }
                catch { lastError = error.localizedDescription }
            }
            notifyBattery(battery)
        } else {
            available = false
            lastError = reading?.error ?? "Battery read failed. Keep the ring nearby and try Refresh."
            notifyBattery(nil)
        }
        render()
    }

    func item(_ title: String, _ action: Selector? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    func loadConnection() {
        var key = readKeychain()
        if key == nil {
            let path = support.appendingPathComponent("auth.hex")
            if let attributes = try? FileManager.default.attributesOfItem(atPath: path.path),
               let permissions = attributes[.posixPermissions] as? NSNumber,
               let owner = attributes[.ownerAccountID] as? NSNumber,
               permissions.intValue & 0o077 == 0, owner.uint32Value == getuid(),
               let text = try? String(contentsOf: path, encoding: .utf8) { key = normalizedKey(text); needsKeyMigration = key != nil }
        }
        var address = defaults.string(forKey: "ringAddress")
        if address == nil, let data = try? Data(contentsOf: support.appendingPathComponent("peer.json")),
           let peer = try? JSONSerialization.jsonObject(with: data) as? [String: String] { address = peer["address"] }
        if let key = key, let address = address, UUID(uuidString: address) != nil { connection = Connection(key: key, address: address) }
    }

    @objc func connectRing() {
        guard process == nil, !quitting else { return }
        if let window = setupWindow { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 390), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Connect your Oura ring"
        window.isReleasedWhenClosed = false
        window.delegate = self
        let help = NSTextField(wrappingLabelWithString: "Choose your ring and connect using an existing local iPhone backup. Nothing is transferred from your phone. A previously saved ring key also works.")
        help.preferredMaxLayoutWidth = 470
        let picker = NSPopUpButton(frame: .zero)
        picker.addItem(withTitle: "Scan to find nearby rings")
        ringPicker = picker
        let scan = NSButton(title: "Scan nearby rings", target: self, action: #selector(scanNearby))
        let secret = NSSecureTextField(frame: .zero)
        secret.placeholderString = "32-character ring key"
        secret.setAccessibilityLabel("Ring authentication key")
        keyField = secret
        let imports = NSStackView(views: [NSButton(title: "Import key file…", target: self, action: #selector(importKeyFile))])
        imports.spacing = 12
        let statusLabel = NSTextField(wrappingLabelWithString: "Keep the ring nearby. Setup needs an existing unencrypted backup or saved key; it never starts a new backup.")
        statusLabel.preferredMaxLayoutWidth = 470
        setupStatus = statusLabel
        let connect = NSButton(title: "Connect this ring", target: self, action: #selector(confirmConnection))
        connect.bezelStyle = .rounded
        let advanced = NSStackView(views: [NSTextField(labelWithString: "Existing ring key"), secret, imports, connect])
        advanced.orientation = .vertical
        advanced.alignment = .leading
        advanced.spacing = 8
        advanced.isHidden = true
        advancedControls = advanced
        let advancedButton = NSButton(checkboxWithTitle: "Advanced key import", target: self, action: #selector(toggleAdvanced))
        let primary = NSButton(title: "Connect using existing backup", target: self, action: #selector(connectExistingBackup))
        primary.bezelStyle = .rounded
        let choose = NSButton(title: "Choose an existing backup…", target: self, action: #selector(importBackup))
        let stack = NSStackView(views: [help, picker, scan, primary, choose, advancedButton, advanced, statusLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.detachesHiddenViews = true
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView?.addSubview(stack)
        if let content = window.contentView {
            NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24), stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24), stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24), picker.widthAnchor.constraint(equalTo: stack.widthAnchor), secret.widthAnchor.constraint(equalToConstant: 470)])
        }
        setupWindow = window
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        scanNearby()
    }

    @objc func toggleAdvanced(_ sender: NSButton) {
        let visible = sender.state == .on
        advancedControls?.isHidden = !visible
        setupWindow?.setContentSize(NSSize(width: 520, height: visible ? 540 : 390))
    }

    func windowWillClose(_ notification: Notification) {
        if notification.object as? NSWindow === setupWindow {
            keyField?.stringValue = ""
            if process != nil { stopReader() }
        }
    }

    @objc func connectExistingBackup() {
        guard process == nil else { return }
        guard nearbyRings.indices.contains(ringPicker?.indexOfSelectedItem ?? -1) else {
            setupStatus?.stringValue = "Scan for your ring and select it first."
            return
        }
        setupStatus?.stringValue = "Looking for an existing local backup. No phone transfer is started…"
        runBackend(["--existing-backup"]) { [weak self] reading in
            guard let self = self, self.setupWindow?.isVisible == true, !self.quitting else { return }
            if reading?.ok == true, let key = reading?.key.flatMap(normalizedKey) {
                self.keyField?.stringValue = key
                self.confirmConnection()
            } else { self.setupStatus?.stringValue = reading?.error ?? "Could not find a usable local backup. Choose an existing backup folder or use Advanced key import." }
        }
    }

    @objc func scanNearby() {
        guard process == nil else { return }
        setupStatus?.stringValue = "Scanning for nearby Oura rings (10 seconds)…"
        runBackend(["--scan"], timeout: 25) { [weak self] reading in
            guard let self = self, self.setupWindow?.isVisible == true, !self.quitting else { return }
            self.nearbyRings = reading?.ok == true ? (reading?.rings ?? []).filter { UUID(uuidString: $0.address) != nil } : []
            self.ringPicker?.removeAllItems()
            for ring in self.nearbyRings { self.ringPicker?.addItem(withTitle: "Oura ring · \(ring.address.suffix(8))") }
            if self.nearbyRings.isEmpty { self.ringPicker?.addItem(withTitle: "No rings found") }
            self.setupStatus?.stringValue = reading?.ok == true ? "Found \(self.nearbyRings.count) ring(s). Select yours and import the existing key." : reading?.error ?? "Scan failed. Allow Bluetooth access in System Settings and scan again."
        }
    }

    @objc func importKeyFile() { chooseKeySource(backup: false) }
    @objc func importBackup() { chooseKeySource(backup: true) }
    func chooseKeySource(backup: Bool) {
        guard let window = setupWindow, process == nil else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = backup
        panel.allowsMultipleSelection = false
        panel.message = backup ? "Choose an unencrypted Finder iPhone backup folder or exported Oura app database. Nothing is uploaded." : "Choose the file containing your existing ring authentication key."
        panel.beginSheetModal(for: window) { [weak self] result in
            guard result == .OK, let path = panel.url?.path, let self = self else { return }
            self.setupStatus?.stringValue = "Reading the selected key source locally…"
            self.runBackend(["--extract-key"], request: ["path": path], timeout: 20) { [weak self] reading in
                guard let self = self, self.setupWindow?.isVisible == true, !self.quitting else { return }
                if reading?.ok == true, let key = reading?.key.flatMap(normalizedKey) {
                    self.keyField?.stringValue = key
                    if backup { self.confirmConnection() }
                    else { self.setupStatus?.stringValue = "Key imported locally. Select your ring and click Connect this ring." }
                } else { self.setupStatus?.stringValue = reading?.error ?? "Could not import a ring key from this file." }
            }
        }
    }

    @objc func confirmConnection() {
        guard process == nil else { return }
        guard let key = normalizedKey(keyField?.stringValue ?? "") else { setupStatus?.stringValue = "Import or enter the ring’s 32-character authentication key."; return }
        let index = ringPicker?.indexOfSelectedItem ?? -1
        guard nearbyRings.indices.contains(index) else { setupStatus?.stringValue = "Scan for your ring and select it first."; return }
        let address = nearbyRings[index].address
        setupStatus?.stringValue = "Authenticating and checking battery…"
        runBackend(["--connect"], request: ["key": key, "address": address]) { [weak self] reading in
            guard let self = self, self.setupWindow?.isVisible == true, !self.quitting else { return }
            guard reading?.ok == true, let battery = reading?.batteryPercent, (0...100).contains(battery),
                  let measured = reading?.measuredAt, measured.isFinite, abs(Date().timeIntervalSince1970 - measured) < 120 else {
                self.setupStatus?.stringValue = reading?.error ?? "Connection failed. Check that this key belongs to the selected ring."
                return
            }
            do { try saveKeychain(key) }
            catch { self.setupStatus?.stringValue = error.localizedDescription; return }
            if self.connection?.address != address { self.state = MonitorState() }
            self.defaults.set(address, forKey: "ringAddress")
            self.connection = Connection(key: key, address: address)
            self.needsKeyMigration = false
            self.keyField?.stringValue = ""
            self.setupWindow?.close()
            self.finishedReading(reading, checkIn: false)
            self.send("connected", title: "Ring connected", body: "Battery is \(battery)%. Checks run every 30 minutes while your Mac is awake.")
        }
    }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister() }
            else {
                try SMAppService.mainApp.register()
                if SMAppService.mainApp.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
            }
        } catch { showMessage("Login item needs attention", error.localizedDescription) }
        render()
    }

    func render() {
        guard status != nil else { return }
        let battery = connection == nil ? "—" : available ? state.battery.map { "\($0)%" } ?? "?" : "?"
        status.button?.title = "Oura \(battery)" + (checkingIn ? " · check-in" : process != nil ? " ↻" : "")
        status.button?.setAccessibilityLabel("Oura battery \(battery)")
        let menu = NSMenu()
        menu.autoenablesItems = false
        if let value = state.battery, let stamp = state.lastRead {
            let formatter = DateFormatter(); formatter.dateStyle = .short; formatter.timeStyle = .short
            menu.addItem(item("Last reading: \(value)% · \(formatter.string(from: Date(timeIntervalSince1970: stamp)))"))
        } else { menu.addItem(item(connection == nil ? "Connect your ring to start" : "Waiting for first ring reading")) }
        if let error = lastError { menu.addItem(item(error)) }
        menu.addItem(item("Connect ring…", #selector(connectRing)))
        menu.items.last?.isEnabled = process == nil
        menu.addItem(item("Refresh now · every 30 min", #selector(runReader)))
        menu.items.last?.isEnabled = process == nil && connection != nil && setupWindow?.isVisible != true
        menu.addItem(.separator())
        let breaks = item("Desk-break reminders · 50 min", #selector(toggleBreaks))
        breaks.state = breaksEnabled ? .on : .off
        menu.addItem(breaks)
        if checkingIn {
            let remaining = max(0, Int(checkInEnd?.timeIntervalSinceNow ?? 0))
            menu.addItem(item("Cancel check-in · about \((remaining + 59) / 60) min left", #selector(cancelCheckIn)))
        } else {
            menu.addItem(item("Start 5-minute resting check-in…", #selector(beginCheckIn)))
            menu.items.last?.isEnabled = process == nil && connection != nil && setupWindow?.isVisible != true
        }
        if FileManager.default.fileExists(atPath: support.appendingPathComponent("last-check-in.json").path) {
            menu.addItem(item("View last check-in…", #selector(viewCheckIn)))
        }
        menu.addItem(.separator())
        menu.addItem(item(canNotify ? "Send test notification" : "Enable notifications…", #selector(testNotification)))
        let login = item("Start at login", #selector(toggleLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(item("Quit Ring Battery", #selector(quit)))
        status.menu = menu
        for entry in menu.items where entry.action == nil { entry.isEnabled = false }
        let diagnostic: [String: Any] = ["battery": state.battery as Any? ?? NSNull(), "available": available,
                                       "notifications_allowed": canNotify, "checking_in": checkingIn,
                                       "last_read": state.lastRead as Any? ?? NSNull(), "menu_title": status.button?.title ?? "",
                                       "last_error": lastError as Any? ?? NSNull(),
                                       "bluetooth_authorization": CBManager.authorization.rawValue]
        if let data = try? JSONSerialization.data(withJSONObject: diagnostic) { try? data.write(to: support.appendingPathComponent("app-status.json"), options: .atomic) }
    }

    @objc func toggleBreaks() { breaksEnabled.toggle(); desk = DeskReminder(startedAt: Date().timeIntervalSince1970); render() }
    func stopReader() {
        guard let child = process, child.isRunning else { return }
        child.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { [weak self, weak child] in
            if let child = child, self?.process === child, child.isRunning { kill(child.processIdentifier, SIGKILL) }
        }
    }
    @objc func cancelCheckIn() { stopReader() }
    @objc func beginCheckIn() {
        let alert = NSAlert()
        alert.messageText = "Five-minute resting check-in"
        alert.informativeText = "Wear your ring, sit comfortably, and keep your hand still for five minutes. This uses extra ring battery. HRV is experimental; no stress score is inferred."
        alert.addButton(withTitle: "Start check-in")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn { startReader(checkIn: true) }
    }
    @objc func viewCheckIn() {
        guard let data = try? Data(contentsOf: support.appendingPathComponent("last-check-in.json")),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rate = value["heart_rate"] as? Int else { return }
        let hrv = (value["hrv_rmssd"] as? Double).map { "\($0) ms (experimental)" } ?? "withheld: insufficient signal coverage"
        let stamp = (value["measured_at"] as? Double).map { Date(timeIntervalSince1970: $0).description } ?? ""
        showMessage("Last resting check-in", "\(stamp)\nAverage heart rate: \(rate) bpm\nHRV / RMSSD: \(hrv)\nSignal coverage: \(value["coverage_percent"] ?? "?")%\nValid beats: \(value["valid_beats"] ?? "?")\n\nUse trends for personal reflection. These readings are not validated for medical or stress decisions.")
    }
    func showMessage(_ title: String, _ body: String) {
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = body
        NSApp.activate(ignoringOtherApps: true); alert.runModal()
    }
    @objc func testNotification() {
        if canNotify { send("test", title: "Ring Battery notifications work", body: "Low-battery alerts trigger at 20%. Desk-break reminders trigger after about 50 minutes at your Mac.") }
        else {
            requestNotifications()
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension")!)
        }
    }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if connection == nil { connectRing() } else { status.button?.performClick(nil) }
        return true
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if process != nil { quitting = true; stopReader(); return .terminateLater }
        return .terminateNow
    }
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) { completionHandler([.banner, .sound]) }
}

if CommandLine.arguments.contains("--self-test") { selfTest() }
else {
    let delegate = AppDelegate()
    let app = NSApplication.shared
    app.delegate = delegate
    app.run()
}
