import Foundation

struct BridgeSettings: Codable {
    var source = "192.168.4.1"
    var inputPort = "4000"
    var destination = "127.0.0.1"
    var outputPort = "4000"
    static let sessionLengths = [60, 120, 180, 240]
    var sessionMinutes = 240
    var keepAwake = false
    /// The Bridge Session switch of builds before the keep-running chooser, stored as "experiment". Kept so
    /// saved settings still decode; a Lite build preselects "Only while the app is open" when it was off.
    var experiment = true
    // Optional keeps older saved settings decodable; missing means the default port.
    var discoveryPort: String? = "63093"
    // Missing in older saved settings means on, matching the other platforms' default.
    var restoreCompatibility: Bool? = true
    /// EFB on this device: loopback on the receiver port, which Stratux-type EFBs listen on.
    mutating func useThisDevice() { destination = "127.0.0.1"; outputPort = inputPort }
    /// EFB on another device: its address (entered or found) on the standard GDL90 port.
    mutating func useOtherDevice() { destination = ""; outputPort = "4000" }
    /// The input shares its port only for loopback output on that same port; otherwise it stays exclusive.
    var sharesInput: Bool {
        guard let input = UInt16(inputPort), let output = UInt16(outputPort) else { return false }
        return Self.isLoopback(destination) && input == output
    }
    /// Numeric IPv4 in 127.0.0.0/8, parsed like the C adapter; anything else is not loopback.
    static func isLoopback(_ address: String) -> Bool {
        var ip: UInt32 = 0
        return sb_parse_ipv4(address, &ip) == 1 && ip >> 24 == 127
    }
    /// Receiver web requests (keepalive, Compatibility Mode) go only to a receiver on the network,
    /// never to a loopback source such as the synthetic replay route.
    var contactsReceiver: Bool { !Self.isLoopback(source) }
}
struct BridgeSnapshot {
    var efbs: [DiscoveredEFB] = []
    var discoveryListening = false
    var discoveryError: String?
    /// Result of the latest automatic Compatibility Mode request; nil when none was sent.
    var compatibilityRequest: String?
    /// Result of the latest receiver status request (keepalive or silence recovery).
    var receiverContact: String?
    /// Latest shared-input rebind result after a WiFi address change; nil when none happened.
    var inputRebind: String?
    /// Fresh battery, CO and temperature from the receiver's statistics stream; nil when none or stale.
    var readings: ReceiverReadings?
    /// The address this run now sends to after following a moved EFB; nil while unchanged.
    var followedDestination: String?
    /// Latest message from following the EFB (switched, or none or several devices answer).
    var followMessage: String?
    var received: UInt64 = 0, forwarded: UInt64 = 0, sendErrors: UInt64 = 0
    var traffic: UInt32 = 0, weather: UInt32 = 0, heartbeat: UInt32 = 0
    var crcBad: UInt32 = 0
    var gps = false, mode: Int32 = 0
    var age = -1.0, elapsed = 0.0, maxGap = 0.0
    /// The Sentry's own fix, read for the GPS check only; the worker never changes it.
    var sentryFix = SentryFix()
    var detail = "Ready to bridge"
    func activity(since previous: BridgeSnapshot) -> String {
        if age < 0 { return "Waiting for data" }
        if age >= 3 { return "Receiver silent" }
        let sent = forwarded > previous.forwarded
        if sendErrors > previous.sendErrors { return sent ? "Forwarding • output errors" : "Output errors" }
        if sent { return "Receiving • forwarding" }
        if forwarded == 0 && sendErrors > 0 { return "Output errors" }
        return "Receiving"
    }
    /// Subtitle for the system's Bridge Session Live Activity. It reports the receiver and GPS
    /// state the app itself shows; "sent" is UDP transport, not confirmed EFB receipt.
    func sessionSubtitle(minutes: Int, locale: Locale = .current) -> String {
        let progress = "\(Int(elapsed / 60))/\(minutes) min"
        if age < 0 { return "Waiting for receiver • \(progress)" }
        if age >= 3 { return "Receiver silent \(Int(age)) s • \(progress)" }
        let count = { (value: UInt64) in value.formatted(.number.locale(locale)) }
        return "\(gps ? "GPS fix" : "No GPS fix") • \(count(UInt64(traffic))) traffic • \(count(forwarded)) sent • \(progress)"
    }
    init() {}
    init(_ s: sbio_status) {
        received = s.received; forwarded = s.forwarded; sendErrors = s.send_errors
        traffic = s.traffic; weather = s.weather; heartbeat = s.heartbeat; crcBad = s.crc_bad
        gps = s.gps_valid != 0; mode = s.mode; age = s.age; elapsed = s.elapsed; maxGap = s.max_gap
        sentryFix = SentryFix(gps: gps, valid: s.own_valid != 0, lat: s.own_lat, lon: s.own_lon, age: s.own_age)
        var detail = s.detail
        self.detail = withUnsafePointer(to: &detail) {
            $0.withMemoryRebound(to: CChar.self, capacity: 256) { String(cString: $0) }
        }
    }
}
// The worker exclusively owns the sockets and C decoder. Networking polls wait at most 50 ms between Stop checks.
final class BridgeWorker: @unchecked Sendable {
    private static let discoveryQueue = DispatchQueue(label: "io.sentrybridge.discovery", qos: .userInitiated)
    // Receiver web requests run on this worker's own queue, never on the UDP worker or a queue
    // shared with a replacement worker; Stop or the end of a run cancels queued and active ones.
    private let receiverQueue = DispatchQueue(label: "io.sentrybridge.receiver", qos: .userInitiated)
    private let cancel = sbio_cancel_create()
    private let receiverHTTPPort: UInt16, loopbackReceiver: Bool
    // Following a moved EFB; host tests replace the probe and the local address, never the rule.
    // A replaced probe still receives this worker's cancel token.
    private let followCheckS: Double, sweepProbe: (([String], OpaquePointer?) -> [Bool]?)?, localAddress: (String) -> String?
    private let readsLoopbackStats: Bool
    // Host tests can refuse the switch to check that a refused one changes nothing but its message.
    private let refusesSwitch: Bool
    init() {
        receiverHTTPPort = 80; loopbackReceiver = false; readsLoopbackStats = false; refusesSwitch = false
        followCheckS = EFBFollower.checkS; sweepProbe = nil; localAddress = EFBSweep.localAddress(receiver:)
    }
    #if SBIO_TESTING
    /// Host tests only: an unprivileged receiver web port and, optionally, a loopback fake receiver.
    init(receiverHTTPPort: UInt16, loopbackReceiver: Bool = false, readsLoopbackStats: Bool = false) {
        self.receiverHTTPPort = receiverHTTPPort; self.loopbackReceiver = loopbackReceiver
        self.readsLoopbackStats = readsLoopbackStats; refusesSwitch = false
        followCheckS = EFBFollower.checkS; sweepProbe = nil; localAddress = EFBSweep.localAddress(receiver:)
    }
    /// Host tests only: a stubbed probe (no real network) and local address, and a short check interval.
    convenience init(followCheckS: Double, local: String?, probe: @escaping ([String]) -> [Bool]?) {
        self.init(followCheckS: followCheckS, local: local, cancellableProbe: { addresses, _ in probe(addresses) })
    }
    /// Host tests only: a probe given this worker's cancel token, as the real one is.
    init(followCheckS: Double, local: String?, refusesSwitch: Bool = false, cancellableProbe: @escaping ([String], OpaquePointer?) -> [Bool]?) {
        receiverHTTPPort = 80; loopbackReceiver = false; readsLoopbackStats = false
        self.followCheckS = followCheckS; sweepProbe = cancellableProbe; localAddress = { _ in local }
        self.refusesSwitch = refusesSwitch
    }
    #endif
    private var probe: ([String]) -> [Bool]? {
        guard let sweepProbe else { return EFBSweep.probe(cancel) }
        return { [cancel] in sweepProbe($0, cancel) }
    }
    // Queued and active requests retain this worker, so the token outlives every request using it.
    deinit { sbio_cancel_destroy(cancel) }
    private let lock = NSLock()
    private var stopped = false
    func stop() { lock.lock(); stopped = true; lock.unlock(); sbio_cancel_set(cancel) }
    private var shouldStop: Bool { lock.lock(); defer { lock.unlock() }; return stopped }
    private var ended = false
    /// Helpers (statistics reader, EFB follower) run while this worker's run lasts and Stop is not pressed.
    private var helpersRun: Bool { lock.lock(); defer { lock.unlock() }; return !stopped && !ended }
    private var readings: ReceiverReadings?, readingsAt = -Double.infinity
    private var lastReceive: Double?
    // The destination this run sends to; the follower changes it only under the lock, for this run.
    // The destination this run sends to. The follower only requests a switch; the UDP loop changes it after
    // the adapter accepted the new address, so a refused switch leaves the route, checks and settings as they were.
    private var destination = "", pendingDestination: (from: String, to: String)?
    private var followedDestination: String?, followMessage: String?
    private var compatibilityResult: String?
    private var contactResult: String?
    private var contactInFlight = false
    // A Sentry stops streaming to a WiFi client that sends it nothing for a while; a read-only status
    // request keeps this client active and restores the stream. One request at a time.
    private func contactReceiver(_ receiver: String, reason: String) {
        lock.lock(); defer { lock.unlock() }
        guard !contactInFlight else { return }
        contactInFlight = true
        receiverQueue.async { [self] in
            var error = [CChar](repeating: 0, count: 256)
            let status = sbio_request_status(receiver, receiverHTTPPort, 3000, cancel, &error, error.count)
            lock.lock(); defer { lock.unlock() }
            contactInFlight = false
            if status != SBIO_HTTP_CANCELLED { contactResult = "\(reason): " + (status > 0 ? "HTTP \(status)" : String(cString: error)) }
        }
    }
    private func requestCompatibility(_ receiver: String) {
        lock.lock(); compatibilityResult = "Compatibility Mode request queued"; lock.unlock()
        receiverQueue.async { [self] in
            var error = [CChar](repeating: 0, count: 256)
            let status = sbio_request_compatibility(receiver, receiverHTTPPort, 5000, cancel, &error, error.count)
            guard status != SBIO_HTTP_CANCELLED else { return }
            let result = status == 200 ? "Compatibility Mode requested (HTTP 200)"
                : status > 0 ? "Compatibility Mode request failed (HTTP \(status))"
                : "Compatibility Mode request failed: \(String(cString: error))"
            lock.lock(); compatibilityResult = result; lock.unlock()
        }
    }
    /// Find EFB: announcements (listen false: the running bridge holds the port and lists them) and a
    /// sweep of the receiver's network for devices that do not announce, reported once as "Device" on
    /// the EFB UDP port. finish comes only after the sweep has ended, on every path; Stop cancels it.
    func findEFBs(_ settings: BridgeSettings, listen: Bool = true, update: @escaping ([DiscoveredEFB]) -> Void,
                 swept: @escaping ([DiscoveredEFB]) -> Void = { _ in }, finish: @escaping (String?) -> Void) {
        Self.discoveryQueue.async { [self] in
            let sweeping = DispatchGroup()
            // Not on the receiver's WiFi: announcements only.
            if !shouldStop, let local = localAddress(settings.source) {
                sweeping.enter()
                DispatchQueue.global(qos: .userInitiated).async { [self] in
                    defer { sweeping.leave() }
                    let port = UInt16(settings.outputPort) ?? 0, input = UInt16(settings.inputPort) ?? 0
                    let found = EFBSweep.sweep(local: local, exclude: [settings.source], probe: probe, cancelled: { shouldStop })
                        .filter { sbio_efb_candidate($0, port, settings.source, input) != 0 }
                    let now = sbio_now()
                    if !shouldStop { swept(found.map { DiscoveredEFB(address: $0, port: port, app: "Device", seen: now) }) }
                }
            }
            var error: String?
            defer { sweeping.wait(); finish(error) }
            guard listen else { return }
            do {
                guard !shouldStop else { return }
                let listener = try EFBListener(port: UInt16(settings.discoveryPort ?? "63093") ?? 0)
                defer { listener.close() }
                let cache = EFBAnnouncements(settings)
                let deadline = sbio_now() + 6
                var lastUpdate = 0.0
                while !shouldStop && sbio_now() < deadline {
                    try listener.step(cache, timeout: 50)
                    let now = sbio_now()
                    if !shouldStop && now - lastUpdate >= 1 { update(cache.snapshot()); lastUpdate = now }
                }
                if !shouldStop { update(cache.snapshot()) }
                listener.close()
            } catch let caught { error = caught.localizedDescription }
        }
    }
    func run(_ settings: BridgeSettings, duration: Double?, demo: Bool = false,
             update: @escaping (BridgeSnapshot) -> Void,
             finish: @escaping (String?, Bool) -> Void) {
        DispatchQueue(label: "io.sentrybridge.udp", qos: .userInitiated).async { [self] in
            let helpers = DispatchGroup()
            let result = process(settings, duration: duration, demo: demo, helpers: helpers, update: update)
            // A finished run sends nothing more to the receiver, however it ended.
            lock.lock(); ended = true; lock.unlock()
            sbio_cancel_set(cancel)
            // However the run ended (Stop, its length completed, a UDP failure), the cancel above also stops
            // the reader's waits and the follower's probes, which watch the same token, so both end within
            // about 50 ms, a sweep in progress included. A new Start comes only after this run.
            helpers.wait()
            // Socket close precedes completion; a restart cannot race the previous bind.
            finish(result.0, result.1)
        }
    }
    private func process(_ settings: BridgeSettings, duration: Double?, demo: Bool, helpers: DispatchGroup,
                         update: (BridgeSnapshot) -> Void) -> (String?, Bool) {
        guard !shouldStop else { return (nil, false) }
        // An exited search must release its socket before a bridge can bind that port.
        let released = DispatchSemaphore(value: 0)
        Self.discoveryQueue.async { released.signal() }
        while released.wait(timeout: .now() + .milliseconds(10)) == .timedOut {
            // Stop must not wait for a different search's six-second listening period.
            if shouldStop { return (nil, false) }
        }
        guard !shouldStop else { return (nil, false) }
        var error = [CChar](repeating: 0, count: 512)
        let opened = demo ? sbio_open_demo(UInt16(settings.inputPort) ?? 0, &error, error.count) : sbio_open_shared(settings.source, UInt16(settings.inputPort) ?? 0,
                                        settings.destination, UInt16(settings.outputPort) ?? 0,
                                        settings.sharesInput ? 1 : 0, &error, error.count)
        guard let io = opened else {
            return (String(cString: error), false)
        }
        defer { sbio_close(io) }
        let cache = EFBAnnouncements(settings)
        var listener: EFBListener?
        var discoveryError: String?
        let discoveryPort = demo ? 0 : UInt16(settings.discoveryPort ?? "63093") ?? 0
        let sharesInput = discoveryPort != 0 && discoveryPort == UInt16(settings.inputPort)
        if sharesInput {
            sbio_observe(io, { context, bytes, count, source in
                guard let context else { return }
                Unmanaged<EFBAnnouncements>.fromOpaque(context).takeUnretainedValue().receive(bytes, count: count, hostAddress: source)
            }, Unmanaged.passUnretained(cache).toOpaque())
        } else if discoveryPort != 0 {
            do { listener = try EFBListener(port: discoveryPort) }
            catch { discoveryError = error.localizedDescription }
        }
        defer {
            sbio_observe(io, nil, nil); listener?.close()
            withExtendedLifetime(cache) {}
        }
        lock.lock(); destination = settings.destination; lock.unlock()
        // Readings while forwarding, Compatibility Mode included; never in the demo or from a loopback source.
        if !demo && (settings.contactsReceiver || readsLoopbackStats) {
            let reader = ReceiverReadingsReader(receiver: settings.source, port: receiverHTTPPort, cancel: cancel,
                                                running: { [self] in helpersRun }, publish: { [self] values in
                lock.lock(); readings = values; readingsAt = sbio_now(); lock.unlock()
            })
            helpers.enter()
            DispatchQueue(label: "io.sentrybridge.readings", qos: .utility).async { reader.run(); helpers.leave() }
        }
        // Follow a moved EFB only for one Other device address.
        if !demo && (settings.contactsReceiver || sweepProbe != nil) && !BridgeSettings.isLoopback(settings.destination) {
            helpers.enter()
            DispatchQueue(label: "io.sentrybridge.follow", qos: .utility).async { [self] in follow(settings); helpers.leave() }
        }
        var lastUpdate = 0.0, lastContact = -Double.infinity, lastRefresh = 0.0
        var inputRebind: String?
        while !shouldStop {
            guard sbio_step(io, 50) == 0 else { return ("UDP reception failed. Check WiFi and Local Network access.", false) }
            guard !shouldStop else { return (nil, false) }
            lock.lock(); let next = pendingDestination; pendingDestination = nil; lock.unlock()
            if let next {
                let accepted = !refusesSwitch && sbio_set_destination(io, next.to) == 0
                lock.lock()
                if accepted {
                    destination = next.to; followedDestination = next.to
                    followMessage = EFBFollower.followMessage(from: next.from, to: next.to)
                } else {
                    followMessage = "The EFB at \(next.from) stopped answering, but \(next.to) cannot be used; still sending to \(next.from)."
                }
                lock.unlock()
            }
            do { try listener?.step(cache) }
            catch { discoveryError = error.localizedDescription; listener?.close(); listener = nil }
            // The core schedules at most 3 requests per encrypted episode, 20 s apart.
            if settings.contactsReceiver || loopbackReceiver, settings.restoreCompatibility ?? true, sbio_wants_enable(io) != 0 { requestCompatibility(settings.source) }
            let now = sbio_now()
            if now - lastUpdate >= 1 {
                var s = sbio_status(); sbio_snapshot(io, &s)
                lock.lock(); if s.age >= 0 { lastReceive = now - s.age }; lock.unlock()
                // Contact at start, every 30 s while receiving, every 5 s while silent.
                let silent = s.age < 0 || s.age >= 3
                // A new DHCP lease leaves a shared input on the old address; rebind in place.
                if settings.sharesInput, silent, now - lastRefresh >= 5 {
                    let result = sbio_refresh_shared_input(io)
                    if result != 0 { var detail = sbio_status(); sbio_snapshot(io, &detail); inputRebind = BridgeSnapshot(detail).detail }
                    lastRefresh = now
                }
                if settings.contactsReceiver || loopbackReceiver, now - lastContact >= (silent ? 5 : 30) {
                    contactReceiver(settings.source, reason: silent ? "Receiver silent; status request" : "Keepalive")
                    lastContact = now
                }
                var value = BridgeSnapshot(s)
                value.efbs = cache.snapshot(); value.discoveryListening = sharesInput || listener != nil
                value.discoveryError = discoveryError
                lock.lock(); value.compatibilityRequest = compatibilityResult; value.receiverContact = contactResult
                value.readings = now - readingsAt <= ReceiverReadingsReader.staleS ? readings : nil
                value.followedDestination = followedDestination; value.followMessage = followMessage; lock.unlock()
                value.inputRebind = inputRebind
                update(value); lastUpdate = now
                if let duration, s.elapsed >= duration { return (nil, true) }
            }
        }
        return (nil, false)
    }
    /// While forwarding to one Other device address, check it every 30 s; after an EFB that answered
    /// missed two checks, while receiver data still arrives and this device is on the receiver's network,
    /// sweep and switch to the one other device that answers (EFBFollower). The switch is committed under
    /// the lock, only for this run and only if the destination has not changed since the probe.
    private func follow(_ settings: BridgeSettings) {
        var rule = EFBFollower()
        let port = UInt16(settings.outputPort) ?? 0, input = UInt16(settings.inputPort) ?? 0
        while helpersRun {
            let wake = sbio_now() + followCheckS
            while helpersRun && sbio_now() < wake { Thread.sleep(forTimeInterval: 0.05) }
            lock.lock(); let current = destination, heard = lastReceive; lock.unlock()
            guard helpersRun, let heard, sbio_now() - heard < 10 else { continue }
            guard let answered = probe([current])?.first, helpersRun else { continue }
            guard rule.checked(answered: answered), let local = localAddress(settings.source) else { continue }
            let found = EFBSweep.sweep(local: local, exclude: [settings.source], probe: probe, cancelled: { !helpersRun })
                .filter { sbio_efb_candidate($0, port, settings.source, input) != 0 }
            guard helpersRun else { continue }
            switch rule.swept(found, current: current) {
            case .keep: continue
            case .report(let message):
                lock.lock(); followMessage = message; lock.unlock()
            case .follow(let target):
                lock.lock()
                guard !stopped, !ended, destination == current, pendingDestination == nil else { lock.unlock(); continue }
                pendingDestination = (current, target)
                lock.unlock()
                rule.followed()
            }
        }
    }
}
