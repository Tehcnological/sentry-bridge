import Foundation
import SwiftUI
import UIKit
import Network
import BackgroundTasks
import CoreLocation

// Cancellation is shared only with the scheduler submission queue.
private final class SessionSubmission: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
}

/// What keeps the current run going with the app in the background.
enum RunMode { case foreground, audio, session }

@MainActor final class BridgeModel: ObservableObject {
    /// The app and its intents share this one model, so there is never a second worker.
    static let shared = BridgeModel()
    @Published var settings: BridgeSettings
    @Published var snapshot = BridgeSnapshot()
    @Published var state = "Stopped"
    @Published var message: String?
    @Published var busy = false
    @Published var sessionActive = false
    @Published var localNetworkDenied = false
    @Published var discoveryVisible = false
    @Published var findingEFBs = false
    @Published var discoveredEFBs: [DiscoveredEFB] = []
    @Published var discoveryMessage: String?
    private var discoveryList = DiscoveryList()
    private func showDiscovered(now: Double = sbio_now()) {
        let entries = discoveryList.entries(now: now)
        if entries != discoveredEFBs { discoveredEFBs = entries }
    }
    private static let nothingFound = "No EFB announcements or devices found."
    private static let routeChanged = "The bridge now sends to another address. Find EFB again to list devices."
    private var discoveryWorker: BridgeWorker?
    private var discoverySearching = false
    private var discoveryTask: Task<Void, Never>?
    private var discoveryToken = UUID()
    // Keeping the bridge running: the chooser's saved answer (nil shows the chooser), this run's mode,
    // the GPS check and the notifications that report a stopped or degraded bridge.
    let flavor = BuildFlavor.current
    @Published var keepRunning: KeepRunningChoice?
    @Published var chooserVisible = false
    @Published var keepRunningNote: String?
    @Published private(set) var runMode = RunMode.foreground
    @Published private(set) var gpsRunning = false
    @Published private(set) var gpsStatus: String?
    @Published private(set) var gpsWarning: String?
    @Published private(set) var locationStatus = CLAuthorizationStatus.notDetermined
    /// GPS check is chosen but location is not set to Always; the app links to its Settings page until it is.
    var needsAlwaysLocation: Bool { choice.gpsCheck && locationStatus != .authorizedAlways }
    var choice: KeepRunningChoice {
        keepRunning ?? KeepRunningChoice(method: flavor == .sideload ? .backgroundAudio : .bridgeSession)
    }
    private let notifications = LocalNotifications()
    private lazy var watchdog = Watchdog(poster: notifications)
    private let location = PhoneLocation()
    private var gpsCheck = GPSCheck()
    private var locationStale = false
    #if !LITE
    private let audio = SilentAudio()
    #endif
    /// UI tests start foreground bridges unless a test picks a method with --keep-running.
    private var forceForeground = false
    @Published private(set) var diagnosticLines: [String] = []
    private var lastDiagnosticSample = -30.0
    var diagnosticReport: String { diagnosticLines.joined(separator: "\n") }
    private func record(_ event: String) {
        diagnosticLines.append("\(ISO8601DateFormatter().string(from: Date())) monotonic=\(String(format: "%.3f", sbio_now())) \(event)")
        if diagnosticLines.count > 512 { diagnosticLines.removeFirst(diagnosticLines.count - 512) }
    }
    private var worker: BridgeWorker?
    private var token = UUID()
    private var task: BGTask?
    private var taskIdentifier: String?
    private var submission: SessionSubmission?
    private static let submissionQueue = DispatchQueue(label: "io.sentrybridge.session-submission", qos: .userInitiated)
    private var probe: NWConnection?
    private var stopReason: String?
    private var savedValidatedSettings = false
    private var testing = false
    private let storeKey = "bridge.settings.v1"
    /// Labelled demo with bundled sample data; the saved route is restored when it ends.
    @Published private(set) var demo = false
    private var demoReplayer: DemoReplayer?
    private var settingsBeforeDemo: BridgeSettings?
    func startDemo() {
        guard !busy, !discoveryVisible else { return }
        guard let replayer = DemoReplayer() else { message = "Demo data is unavailable."; return }
        settingsBeforeDemo = settings
        var route = BridgeSettings()
        route.source = "127.0.0.1"; route.inputPort = DemoReplayer.inputPort
        // The worker opens a decode-only adapter with no output socket.
        route.destination = "127.0.0.1"; route.outputPort = DemoReplayer.outputPort
        route.experiment = false; route.restoreCompatibility = false; route.keepAwake = settings.keepAwake
        settings = route; demo = true; demoReplayer = replayer
        start()
        if busy { replayer.start(); record("Demo with sample data; nothing is forwarded to an EFB") } else { endDemo() }
    }
    private func endDemo() {
        guard demo else { return }
        demoReplayer?.stop(); demoReplayer = nil
        if let saved = settingsBeforeDemo { settings = saved }
        settingsBeforeDemo = nil; demo = false
    }
    init() {
        settings = UserDefaults.standard.data(forKey: "bridge.settings.v1")
            .flatMap { try? JSONDecoder().decode(BridgeSettings.self, from: $0) } ?? BridgeSettings()
        // Older builds allowed any length from 1 to 240; round up to the nearest offered length.
        settings.sessionMinutes = BridgeSettings.sessionLengths.first { $0 >= settings.sessionMinutes } ?? 240
        keepRunning = KeepRunningChoice.load(UserDefaults.standard.data(forKey: KeepRunningChoice.storeKey), flavor: flavor)
        chooserVisible = keepRunning == nil
        #if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("--ui-testing") {
            testing = true; settings = BridgeSettings(); syntheticSettings()
            // UI tests start foreground bridges without the chooser unless they ask for either.
            forceForeground = true; chooserVisible = false
            if let picked = arguments.first(where: { $0.hasPrefix("--keep-running=") })?.dropFirst("--keep-running=".count) {
                let parts = picked.split(separator: "+")
                if let method = KeepRunningMethod(rawValue: String(parts[0])) {
                    keepRunning = KeepRunningChoice(method: method, gpsCheck: parts.count > 1); forceForeground = false
                }
            }
            if arguments.contains("--chooser") {
                UserDefaults.standard.removeObject(forKey: KeepRunningChoice.storeKey)
                keepRunning = nil; chooserVisible = true; forceForeground = false
            }
            if arguments.contains("--reset-compatibility-notice") {
                UserDefaults.standard.removeObject(forKey: BridgeView.noticeHiddenKey)
            }
        }
        // The persistence test launches like a user but must reach the main screen.
        if arguments.contains("--ui-skip-chooser") { chooserVisible = false }
        #endif
        #if !LITE
        audio.onEvent = { [weak self] in self?.record($0) }
        audio.onLost = { [weak self] in
            guard let self, self.busy else { return }
            self.notify(.audioStopped(gpsCheck: self.gpsRunning))
            self.message = self.gpsRunning ? "Background audio stopped. GPS check keeps the bridge running."
                : "Background audio stopped. Keep the app open to keep the bridge running."
        }
        #endif
        locationStatus = location.status
        location.onAuthorization = { [weak self] in
            guard let self else { return }
            self.locationStatus = self.location.status; self.record("Location access: \(self.locationStatus.rawValue)")
        }
        location.onStopped = { [weak self] reason in
            guard let self, self.busy, self.gpsRunning else { return }
            self.notify(.locationStopped(reason)); self.gpsWarning = "GPS check stopped: \(reason)"
        }
    }
    /// The chooser's answer. Asks for notifications first, then, for GPS check, Always location access.
    /// Without Always, GPS check stays chosen and the app links to Settings (needsAlwaysLocation).
    func choose(_ option: KeepRunningOption) {
        let picked = KeepRunningChoice(method: option.method, gpsCheck: option.gpsCheck)
        save(picked); chooserVisible = false; keepRunningNote = nil
        #if DEBUG
        if testing { record("Chooser: \(option.title); permission prompts skipped in UI tests"); return }
        #endif
        record("Chooser: \(option.title)")
        notifications.requestPermission { [weak self] granted in
            guard let self else { return }
            self.record("Notifications \(granted ? "allowed" : "not allowed")")
            if !granted { self.keepRunningNote = "Notifications are off, so the app can't warn you if the bridge stops. Turn them on in Settings." }
            guard picked.gpsCheck else { return }
            self.location.requestAlways { [weak self] allowed in
                guard let self else { return }
                self.locationStatus = self.location.status; self.record("Location access \(allowed ? "allowed" : "not allowed")")
            }
        }
    }
    private func save(_ choice: KeepRunningChoice) {
        keepRunning = choice
        UserDefaults.standard.set(choice.encoded(), forKey: KeepRunningChoice.storeKey)
    }
    private func notify(_ notice: DegradedNotice) {
        record("Notification: \(notice.title). \(notice.body)")
        if !demo { notifications.post(notice, after: nil) }
    }
    /// Shown under the Start button: what keeps this run going.
    var modeLabel: String {
        if demo { return "Demo: sample data, not forwarded" }
        let base = sessionActive ? "Bridge Session" : runMode == .audio ? "Background audio"
            : gpsRunning ? "Background location" : "Foreground bridge"
        return gpsRunning ? base + (base == "Background location" ? " (GPS check)" : " + GPS check") : base
    }
    func syntheticSettings() {
        guard !busy else { return }
        settings.source = "127.0.0.1"; settings.inputPort = "14000"
        settings.destination = "127.0.0.1"; settings.outputPort = "14001"
    }
    func findEFBs() {
        exitDiscovery()
        discoveryVisible = true; findingEFBs = true
        discoveredEFBs = []; discoveryMessage = nil
        discoveryList.start(route: settings.destination)
        let id = UUID(); discoveryToken = id
        let usingBridge = busy && worker != nil
        let deadline = sbio_now() + 6
        // Listens unless the running bridge holds the port; sweeps the receiver's network either way.
        discoverySearching = true
        let search = BridgeWorker(); discoveryWorker = search
        search.findEFBs(settings, listen: !usingBridge, update: { [weak self] entries in
            DispatchQueue.main.async {
                guard let self, self.discoveryToken == id, self.discoveryVisible else { return }
                self.discoveryList.announce(entries, route: self.settings.destination); self.showDiscovered()
            }
        }, swept: { [weak self] entries in
            DispatchQueue.main.async {
                guard let self, self.discoveryToken == id, self.discoveryVisible else { return }
                self.discoveryList.sweep(entries, route: self.settings.destination); self.showDiscovered()
            }
        }, finish: { [weak self] error in
            DispatchQueue.main.async {
                guard let self, self.discoveryToken == id, self.discoveryVisible else { return }
                self.discoverySearching = false; self.discoveryWorker = nil
                if !usingBridge {
                    self.findingEFBs = false
                    self.discoveryMessage = !self.discoveryList.isCurrent(route: self.settings.destination) ? Self.routeChanged
                        : error ?? (self.discoveredEFBs.isEmpty ? Self.nothingFound : nil)
                }
            }
        })
        discoveryTask = Task { @MainActor [weak self] in
            #if DEBUG
            if self?.testing == true && ProcessInfo.processInfo.arguments.contains("--ui-delay-discovery-observer") {
                // Reproduce a delayed observer without blocking the UI or networking worker.
                do { try await Task.sleep(nanoseconds: 8_000_000_000) } catch { return }
            }
            #endif
            while !Task.isCancelled {
                guard let self, self.discoveryToken == id, self.discoveryVisible else { return }
                let now = sbio_now()
                // Sample once more before ending the search. A delayed first main-actor
                // turn must not discard announcements already received by the worker.
                if usingBridge && self.findingEFBs {
                    self.discoveryList.announce(self.snapshot.efbs, route: self.settings.destination)
                    if let error = self.snapshot.discoveryError { self.discoveryMessage = error }
                }
                self.showDiscovered(now: now)
                if usingBridge && now >= deadline && !self.discoverySearching && self.findingEFBs {
                    self.findingEFBs = false
                    self.discoveryMessage = !self.discoveryList.isCurrent(route: self.settings.destination) ? Self.routeChanged
                        : self.snapshot.discoveryError ?? (self.discoveredEFBs.isEmpty ? Self.nothingFound : nil)
                }
                do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
            }
        }
    }
    func useEFB(_ entry: DiscoveredEFB) {
        guard !busy else { return }
        guard sbio_now() - entry.seen < 30,
              sbio_efb_candidate(entry.address, entry.port, settings.source, UInt16(settings.inputPort) ?? 0) != 0 else {
            discoveredEFBs.removeAll { $0.id == entry.id }; return
        }
        settings.destination = entry.address
        exitDiscovery()
    }
    func exitDiscovery() {
        discoveryToken = UUID(); discoveryWorker?.stop(); discoveryWorker = nil
        discoveryTask?.cancel(); discoveryTask = nil
        findingEFBs = false; discoveryVisible = false; discoveredEFBs = []; discoveryList = DiscoveryList()
    }
    /// mode overrides the chooser's method for this run (Shortcuts, UI tests).
    func start(mode override: RunMode? = nil) {
        guard !busy, !discoveryVisible else { return }
        var sourceIP: UInt32 = 0, destinationIP: UInt32 = 0
        guard sb_parse_ipv4(settings.source, &sourceIP) == 1,
              sb_parse_ipv4(settings.destination, &destinationIP) == 1 else {
            message = "Enter numeric IPv4 addresses for the receiver and destination."; return
        }
        guard let input = UInt16(settings.inputPort), input > 0,
              let output = UInt16(settings.outputPort), output > 0 else {
            message = "Use ports from 1 to 65535."; return
        }
        let chosen: RunMode
        switch choice.method {
        case .backgroundAudio: chosen = .audio
        case .bridgeSession: chosen = .session
        case .appOpen: chosen = .foreground
        }
        let mode = demo ? .foreground : override ?? (forceForeground ? .foreground : chosen)
        guard mode != .session || BridgeSettings.sessionLengths.contains(settings.sessionMinutes) else { message = "Choose a session length of 60, 120, 180 or 240 minutes."; return }
        savedValidatedSettings = false
        snapshot = BridgeSnapshot(); message = nil; localNetworkDenied = false; stopReason = nil
        token = UUID(); busy = true; state = "Starting"
        diagnosticLines = []; lastDiagnosticSample = -30
        runMode = mode; gpsCheck = GPSCheck(); gpsStatus = nil; gpsWarning = nil; locationStale = false
        // GPS check runs with a background method only, and only with location access.
        gpsRunning = mode != .foreground && choice.gpsCheck && !demo
        if gpsRunning && location.status != .authorizedAlways && location.status != .authorizedWhenInUse {
            gpsRunning = false
            record("GPS check skipped: no location access")
        }
        record("User Start; flavor=\(flavor.rawValue); mode=\(mode); gpsCheck=\(gpsRunning); requested minutes=\(settings.sessionMinutes)")
        let id = token
        if mode == .session {
            requestSession(id)
        } else { launchWorker(id, duration: nil) }
    }
    /// Background running for this run: silent audio (Sideload) and the GPS check's location updates.
    private func startKeepAlive() {
        #if !LITE
        if runMode == .audio, let error = audio.start() {
            record("Background audio could not start: \(error)")
            runMode = .foreground; notify(.audioStopped(gpsCheck: gpsRunning))
            message = "Background audio could not start (\(error)). " + (gpsRunning ? "GPS check keeps the bridge running." : "The bridge runs only while the app is open.")
        } else if runMode == .audio { record("Background audio playing") }
        #endif
        if gpsRunning { location.start() }
    }
    private func stopKeepAlive() {
        #if !LITE
        audio.stop()
        #endif
        location.stop(); gpsRunning = false; gpsStatus = nil; gpsWarning = nil
        watchdog.stop()
    }
    /// Compares positions for the GPS check. The phone's position is only compared here: it never reaches
    /// the worker, the EFB or any ownship or traffic data.
    private func checkGPS(_ value: BridgeSnapshot) {
        guard gpsRunning else { return }
        let now = sbio_now()
        gpsCheck.update(sentry: value.sentryFix, phone: location.latest, now: now)
        if gpsCheck.warning != gpsWarning, !(gpsWarning?.hasPrefix("GPS check stopped") ?? false) {
            if let warning = gpsCheck.warning { record(warning) }
            gpsWarning = gpsCheck.warning
        }
        gpsStatus = gpsCheck.status
        // Location updates stopped: no fix from iOS for 60 s, counted from the start of the run.
        let silent = value.elapsed > 60 && (location.latest.map { now - $0.at > 60 } ?? true)
        if silent && !locationStale { notify(.locationStopped("No position from iOS for 60 s.")) }
        locationStale = silent
    }
    /// The session ended or its length passed while GPS check keeps the bridge running.
    private func endSessionKeepRunning(_ reason: String) {
        record("\(reason); GPS check keeps forwarding")
        task?.setTaskCompleted(success: true); task = nil; sessionActive = false
        message = "\(reason). GPS check keeps the bridge running."
        notify(.sessionEnded(gpsCheck: true))
    }
    private func requestSession(_ id: UUID) {
        let identifier = (Bundle.main.bundleIdentifier ?? "io.github.tehcnological.sentrybridge") + ".bridge." + id.uuidString
        taskIdentifier = identifier
        let registered = BGTaskScheduler.shared.register(forTaskWithIdentifier: identifier, using: .main) { [weak self] backgroundTask in
            MainActor.assumeIsolated {
                guard let self, self.token == id, self.busy, self.state != "Stopping",
                      let continued = backgroundTask as? BGContinuedProcessingTask else {
                    backgroundTask.setTaskCompleted(success: false); return
                }
                self.task = continued; self.sessionActive = true
                self.record("Continued-processing launch accepted")
                continued.progress.totalUnitCount = Int64(self.settings.sessionMinutes * 60)
                continued.expirationHandler = { [weak self] in
                    DispatchQueue.main.async {
                        guard let self, self.token == id, self.sessionActive else { return }
                        if self.gpsRunning { self.endSessionKeepRunning("Bridge Session cancelled or expired"); return }
                        self.notify(.sessionEnded(gpsCheck: false))
                        self.stop(reason: "Bridge Session cancelled or expired; forwarding stopped.")
                    }
                }
                // With GPS check on the run outlasts the session; the session itself ends at its length.
                self.launchWorker(id, duration: self.gpsRunning ? nil : Double(self.settings.sessionMinutes * 60))
            }
        }
        guard registered else {
            busy = false; state = "Stopped"; message = "System could not register the Bridge Session."
            taskIdentifier = nil; UIApplication.shared.isIdleTimerDisabled = false; return
        }
        let minutes = settings.sessionMinutes
        let submission = SessionSubmission(); self.submission = submission
        state = "Requesting session"
        let finished: (Error?) -> Void = { [weak self] error in
            if submission.isCancelled { BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier) }
            DispatchQueue.main.async {
                guard let self, self.token == id, self.busy, self.state != "Stopping" else { return }
                if let error {
                    self.record("Submission failed: \(error.localizedDescription)")
                    // Never stops the bridge: it continues in the foreground, or on GPS check's location updates.
                    let fallback = KeepRunningPolicy.sessionRefused(error.localizedDescription, gpsCheck: self.gpsRunning)
                    self.notify(fallback.notice); self.message = fallback.message
                    self.runMode = .foreground; self.submission = nil
                    if self.worker == nil { self.launchWorker(id, duration: nil) }
                } else { self.record("Request submitted using immediate-start policy") }
            }
        }
        Self.submissionQueue.async {
            guard !submission.isCancelled else { return }
            // Create and submit the request on this queue; it never crosses queue ownership.
            let request = BGContinuedProcessingTaskRequest(identifier: identifier, title: "Bridge Session", subtitle: "Preparing a \(minutes)-minute Bridge Session")
            request.strategy = .fail
            if #available(iOS 27.0, *) {
                BGTaskScheduler.shared.submitTaskRequest(request, completionHandler: finished)
            } else {
                do { try BGTaskScheduler.shared.submit(request); finished(nil) }
                catch { finished(error) }
            }
            // Stop may race the scheduler accepting the request.
            if submission.isCancelled { BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier) }
        }
    }
    private func launchWorker(_ id: UUID, duration: Double?) {
        let worker = BridgeWorker(); self.worker = worker; state = "Waiting for data"
        UIApplication.shared.isIdleTimerDisabled = settings.keepAwake
        checkLocalNetwork()
        startKeepAlive()
        if !demo { watchdog.tick(now: sbio_now()) }
        worker.run(settings, duration: duration, demo: demo, update: { [weak self] value in
            DispatchQueue.main.async {
                guard let self, self.token == id, self.state != "Stopping" else { return }
                if !self.savedValidatedSettings {
                    if !self.testing && !self.demo { UserDefaults.standard.set(try? JSONEncoder().encode(self.settings), forKey: self.storeKey) }
                    self.savedValidatedSettings = true
                }
                let previous = self.snapshot
                self.snapshot = value
                // Each shared-input rebind result after a WiFi address change is a diagnostics event.
                if let rebind = value.inputRebind, rebind != previous.inputRebind { self.record("Receiver input: \(rebind)") }
                if let follow = value.followMessage, follow != previous.followMessage { self.record("EFB: \(follow)") }
                // Following a moved EFB: show and save the address this run now sends to.
                if let followed = value.followedDestination, followed != self.settings.destination {
                    self.settings.destination = followed
                    // Find EFB results may list the old address: clear them, and drop any a running search reports later.
                    self.discoveryList.routeChanged(); self.discoveredEFBs = []
                    if self.discoveryVisible { self.discoveryMessage = Self.routeChanged }
                    if !self.testing && !self.demo { UserDefaults.standard.set(try? JSONEncoder().encode(self.settings), forKey: self.storeKey) }
                }
                if let warning = value.readings?.coWarning, warning != previous.readings?.coWarning { self.record(warning) }
                if value.elapsed - self.lastDiagnosticSample >= 30 {
                    self.record("elapsed=\(Int(value.elapsed)) input=\(value.received) sent=\(value.forwarded) age=\(String(format: "%.2f", value.age)) maxGap=\(String(format: "%.2f", value.maxGap)) weather=\(value.weather) outputErrors=\(value.sendErrors) receiver=\(value.receiverContact ?? "none")")
                    self.lastDiagnosticSample = value.elapsed
                }
                self.state = value.activity(since: previous)
                // Each update pushes the watchdog notification ahead; a suspended app stops doing so.
                if !self.demo { self.watchdog.tick(now: sbio_now()) }
                self.checkGPS(value)
                if self.gpsRunning, self.sessionActive, value.elapsed >= Double(self.settings.sessionMinutes * 60) {
                    self.endSessionKeepRunning("Bridge Session length reached")
                }
                if let continued = self.task as? BGContinuedProcessingTask {
                    continued.progress.completedUnitCount = min(Int64(value.elapsed), continued.progress.totalUnitCount)
                    continued.updateTitle("Bridge Session", subtitle: value.sessionSubtitle(minutes: self.settings.sessionMinutes))
                }
            }
        }, finish: { [weak self] error, completed in
            DispatchQueue.main.async {
                guard let self, self.token == id else { return }
                self.record("Worker stopped; completed=\(completed); \(error ?? self.stopReason ?? (completed ? "monitoring period completed" : "user stop"))")
                self.stopKeepAlive()
                if completed && self.sessionActive { self.notify(.sessionEnded(gpsCheck: false)) }
                self.task?.setTaskCompleted(success: completed && error == nil)
                self.task = nil; self.sessionActive = false; self.taskIdentifier = nil
                self.submission?.cancel(); self.submission = nil
                self.worker = nil; self.busy = false; self.state = "Stopped"
                self.endDemo()
                self.message = error ?? self.stopReason ?? (completed ? "Selected monitoring period completed." : nil)
                self.probe?.cancel(); self.probe = nil
                UIApplication.shared.isIdleTimerDisabled = false
            }
        })
    }
    func stop(reason: String? = nil) {
        guard busy else { return }
        state = "Stopping"; stopReason = reason
        record(reason ?? "User Stop")
        // A stop the app makes is reported by its own notification; the watchdog is for one it cannot make.
        watchdog.stop()
        submission?.cancel(); submission = nil
        if let taskIdentifier { BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: taskIdentifier) }
        if let worker { worker.stop() }
        else {
            token = UUID(); busy = false; sessionActive = false; state = "Stopped"; endDemo(); stopKeepAlive()
            task?.setTaskCompleted(success: false); task = nil; taskIdentifier = nil; message = reason
            UIApplication.shared.isIdleTimerDisabled = false
        }
    }
    #if DEBUG
    /// Intent outcomes, recorded only in UI-test mode so tests can check the dialog text.
    @Published private(set) var intentOutcomes: [String] = []
    #endif
    private func intentOutcome(_ outcome: String) -> String {
        record("Shortcut: \(outcome)")
        #if DEBUG
        if testing { intentOutcomes.append(outcome) }
        #endif
        return outcome
    }
    /// Starts the saved route for a Shortcut or Siri request and returns the outcome to show.
    func startFromIntent() -> String { intentOutcome(startIntentOutcome()) }
    func stopFromIntent() -> String { intentOutcome(stopIntentOutcome()) }
    private func startIntentOutcome() -> String {
        if busy {
            // No second worker: report the actual state instead of starting again.
            switch state {
            case "Stopping": return "Bridge is stopping. Start again after it stops."
            case "Requesting session": return "A Bridge Session request is already pending."
            case "Starting": return "Bridge is already starting."
            default: return sessionActive ? "Bridge Session already running." : "Bridge already running."
            }
        }
        exitDiscovery()
        // Lite: the Shortcut asks for a Bridge Session as before. Sideload: background audio.
        start(mode: flavor == .lite ? .session : .audio)
        guard busy else { return message ?? "Bridge could not start." }
        return flavor == .lite ? "Requesting a Bridge Session." : "Bridge starting."
    }
    private func stopIntentOutcome() -> String {
        guard busy else { return "Bridge is not running." }
        if state == "Stopping" { return "Bridge is already stopping." }
        stop(reason: "Stopped by shortcut.")
        return "Bridge stopping."
    }
    func backgrounded() {
        exitDiscovery()
        record("Scene backgrounded; sessionActive=\(sessionActive)")
        // Background audio and GPS check keep running hidden; a pending session request does not.
        if busy && !sessionActive && runMode != .audio && !gpsRunning {
            notify(.stoppedWhenHidden)
            stop(reason: flavor == .lite ? "Bridge stopped when you left the app. Choose Bridge Session in Settings to keep it running."
                 : "Bridge stopped when you left the app. Choose Background audio in Settings to keep it running.")
        }
        UIApplication.shared.isIdleTimerDisabled = false
    }
    func foregrounded() { record("Scene foregrounded"); locationStatus = location.status; UIApplication.shared.isIdleTimerDisabled = busy && settings.keepAwake }
    private func checkLocalNetwork() {
        guard let localHost = [settings.source, settings.destination].first(where: { !BridgeSettings.isLoopback($0) }) else { return }
        // UDP connect prompts for local network access without transmitting receiver commands.
        let currentToken = token
        let connection = NWConnection(host: NWEndpoint.Host(localHost), port: 9, using: .udp)
        probe = connection
        let deny: () -> Void = { [weak self] in
            DispatchQueue.main.async {
                guard let self, self.token == currentToken, self.busy else { return }
                self.localNetworkDenied = true
                self.stop(reason: "Local Network access denied. Enable Sentry Bridge in Settings → Privacy & Security → Local Network, then Start again.")
            }
        }
        connection.pathUpdateHandler = { path in
            if path.unsatisfiedReason == .localNetworkDenied { deny() }
        }
        connection.stateUpdateHandler = { [weak connection] state in
            if case .waiting = state, connection?.currentPath?.unsatisfiedReason == .localNetworkDenied { deny() }
        }
        connection.start(queue: DispatchQueue(label: "io.sentrybridge.permission"))
    }
}
