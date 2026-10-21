import Foundation

@main struct WorkerSmoke {
    static func main() {
        guard CommandLine.arguments.count >= 4 else { exit(2) }
        let mode = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] : "replay"
        var settings = BridgeSettings()
        settings.source = "127.0.0.1"; settings.destination = "127.0.0.1"
        settings.inputPort = CommandLine.arguments[1]; settings.outputPort = CommandLine.arguments[2]
        if mode == "invalid" { settings.source = "invalid" }
        // Exercise the exact settings and status logic used by the SwiftUI model.
        var route = BridgeSettings()
        guard route.sharesInput else { print("FAIL: default route must share UDP 4000 on this device"); exit(1) }
        route.useOtherDevice()
        guard route.destination.isEmpty, route.outputPort == "4000", !route.sharesInput else { exit(1) }
        route.destination = "192.0.2.1"; route.inputPort = "14000"; route.useThisDevice()
        guard route.destination == "127.0.0.1", route.outputPort == "14000", route.sharesInput else { exit(1) }
        route.outputPort = "14001"
        guard !route.sharesInput else { print("FAIL: a different local port must stay exclusive"); exit(1) }
        route.outputPort = "014000"
        guard route.sharesInput else { print("FAIL: equal ports written differently must share"); exit(1) }
        route.source = "127.0.0.1"
        guard !route.contactsReceiver else { print("FAIL: no receiver HTTP to a loopback source"); exit(1) }
        route.source = "192.168.4.1"
        guard route.contactsReceiver else { exit(1) }
        var status = BridgeSnapshot(); status.age = 0; status.sendErrors = 2
        guard status.activity(since: BridgeSnapshot()) == "Output errors" else { exit(1) }
        let failed = status; status.forwarded = 1
        guard status.activity(since: failed) == "Receiving • forwarding" else { exit(1) }
        status.sendErrors += 1
        guard status.activity(since: failed) == "Forwarding • output errors" else { exit(1) }
        // Bridge Session Live Activity text: honest receiver/GPS state, never a claimed EFB receipt.
        var session = BridgeSnapshot(); session.elapsed = 125
        guard session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")) == "Waiting for receiver • 2/60 min" else { print("FAIL: waiting subtitle \(session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")))"); exit(1) }
        session.age = 0.4; session.traffic = 1234; session.forwarded = 4310
        guard session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")) == "No GPS fix • 1,234 traffic • 4,310 sent • 2/60 min" else { print("FAIL: no-fix subtitle \(session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")))"); exit(1) }
        session.gps = true
        guard session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")) == "GPS fix • 1,234 traffic • 4,310 sent • 2/60 min" else { print("FAIL: fix subtitle \(session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")))"); exit(1) }
        session.age = 8.6
        guard session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")) == "Receiver silent 8 s • 2/60 min" else { print("FAIL: silent subtitle \(session.sessionSubtitle(minutes: 60, locale: Locale(identifier: "en_US")))"); exit(1) }
        if mode == "rules" { ruleChecks(fixtures: CommandLine.arguments[4]); return }
        if mode == "follow" { followChecks(settings); return }
        if mode == "sweep" { sweepChecks(settings); discoveryListChecks(); return }
        if mode == "follow-end" { followEndChecks(settings); return }
        if mode == "readings" { readingsChecks(settings, httpPort: UInt16(CommandLine.arguments[4])!); return }
        if mode == "lifecycle" { lifecycleChecks(settings); return }
        if mode == "http-stop" { httpStopChecks(settings, httpPort: UInt16(CommandLine.arguments[4])!); return }
        let worker = BridgeWorker()
        if mode == "discovery-receive" {
            settings.discoveryPort = settings.inputPort
            var found = false
            worker.findEFBs(settings, update: { entries in
                if entries.count == 1, entries[0].app == "Synthetic EFB", entries[0].port == 4001 { found = true }
            }, finish: { error in
                guard error == nil, found else { print("FAIL: UDP discovery receive"); exit(1) }
                print("PASS: real UDP discovery receive and deduplication"); exit(0)
            })
            dispatchMain()
        }
        if mode == "discovery" || mode == "discovery-restart" {
            settings.discoveryPort = settings.inputPort
            discoveryChecks(settings)
            let started = sbio_now()
            let completed: (String?) -> Void = { error in
                guard error == nil, sbio_now() - started < 1.5 else { exit(1) }
                // Exit cancellation releases the discovery socket before completion.
                do { let listener = try EFBListener(port: UInt16(settings.discoveryPort!)!); listener.close() }
                catch { print("FAIL: discovery socket not released"); exit(1) }
                print("PASS: \(mode) cache, filters, cap, expiry, exclusive bind and cancellation")
                exit(0)
            }
            if mode == "discovery-restart" {
                worker.findEFBs(settings, update: { _ in }, finish: { error in if error != nil { exit(1) } })
                let route = settings
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.1) {
                    worker.stop()
                    let replacement = BridgeWorker()
                    replacement.findEFBs(route, update: { _ in }, finish: completed)
                    DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) { replacement.stop() }
                }
            } else {
                DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) { worker.stop() }
                worker.findEFBs(settings, update: { _ in }, finish: completed)
            }
            dispatchMain()
        }
        if mode == "prestop" { worker.stop() }
        if mode == "cancel" { DispatchQueue.global().asyncAfter(deadline: .now() + 0.5) { worker.stop() } }
        var received: UInt64 = 0, forwarded: UInt64 = 0
        let started = sbio_now()
        let duration = mode == "bench" ? 30.0 : 3.0
        worker.run(settings, duration: duration, update: { status in
            received = status.received; forwarded = status.forwarded
        }, finish: { error, completed in
            let passed: Bool
            switch mode {
            case "cancel", "prestop": passed = error == nil && !completed && sbio_now() - started < 1.0
            case "invalid": passed = error != nil && !completed
            default: passed = error == nil && completed && received > 0 && forwarded > 0
            }
            guard passed else { print("FAIL: Swift worker \(mode): \(error ?? "incorrect completion")"); exit(1) }
            // The completed worker must have released its port before this callback.
            if mode != "invalid" {
                var buffer = [CChar](repeating: 0, count: 512)
                guard let io = sbio_open("127.0.0.1", UInt16(settings.inputPort)!, "127.0.0.1", UInt16(settings.outputPort)!, &buffer, buffer.count) else {
                    print("FAIL: worker did not close sockets before completion"); exit(1)
                }
                sbio_close(io)
            }
            print("PASS: Swift worker \(mode), input \(received), sent \(forwarded), ports released")
            exit(0)
        })
        dispatchMain()
    }
    static func discoveryChecks(_ settings: BridgeSettings) {
        let cache = EFBAnnouncements(settings)
        func announcement(_ name: String, port: Int = 4001) -> Data {
            Data("{\"App\":\"\(name)\",\"GDL90\":{\"port\":\(port)}}".utf8)
        }
        cache.receive(announcement("ignored"), address: "127.0.0.1", now: 100)
        cache.receive(announcement("ignored"), address: settings.source, now: 100)
        cache.receive(announcement("ignored"), address: "255.255.255.255", now: 100)
        cache.receive(announcement("ignored"), address: "224.0.0.1", now: 100)
        cache.receive(announcement("ignored", port: 0), address: "192.0.2.1", now: 100)
        cache.receive(Data("invalid".utf8), address: "192.0.2.1", now: 100)
        guard cache.snapshot(now: 100).isEmpty else { exit(1) }
        for i in 1...40 { cache.receive(announcement("EFB"), address: "192.0.2.\(i)", now: 100) }
        guard cache.snapshot(now: 100).count == 32 else { exit(1) }
        cache.receive(announcement("Updated"), address: "192.0.2.1", now: 115)
        guard cache.snapshot(now: 115).count == 32,
              cache.snapshot(now: 130).count == 1,
              cache.snapshot(now: 130).first?.app == "Updated",
              cache.snapshot(now: 145).isEmpty else { exit(1) }
        guard EFBAnnouncements(settings).snapshot().isEmpty else { exit(1) }
        // The observer must ignore receiver JSON and binary traffic before copying/parsing,
        // while still accepting leading JSON whitespace from an eligible EFB.
        let observer = EFBAnnouncements(settings)
        let packet = Data(" \n{\"App\":\"Observer EFB\",\"GDL90\":{\"port\":4001}}".utf8)
        packet.withUnsafeBytes { observer.receive($0.baseAddress?.assumingMemoryBound(to: UInt8.self), count: $0.count, hostAddress: 0x7f000001) }
        let binary = Data([0x7e, 0, 0x7e])
        binary.withUnsafeBytes { observer.receive($0.baseAddress?.assumingMemoryBound(to: UInt8.self), count: $0.count, hostAddress: 0xc0000201) }
        guard observer.snapshot().isEmpty else { exit(1) }
        packet.withUnsafeBytes { observer.receive($0.baseAddress?.assumingMemoryBound(to: UInt8.self), count: $0.count, hostAddress: 0xc0000201) }
        guard observer.snapshot().first?.app == "Observer EFB" else { exit(1) }
        do {
            let port = UInt16(settings.discoveryPort!)!
            let listener = try EFBListener(port: port)
            var second: EFBListener?
            do { second = try EFBListener(port: port) } catch {}
            guard second == nil else { exit(1) }
            listener.close()
        } catch { print("FAIL: discovery bind: \(error)"); exit(1) }
    }
    /// Stop/restart against a slow receiver web server (the harness holds the first status request
    /// for 3 s and sends encrypted frames): the queued Compatibility Mode POST must never be sent,
    /// the stopped worker must be released promptly, and a replacement with Restore off must get
    /// its own status answer without waiting for the old request.
    static func httpStopChecks(_ original: BridgeSettings, httpPort: UInt16) {
        var settings = original
        settings.restoreCompatibility = true
        var first: BridgeWorker? = BridgeWorker(receiverHTTPPort: httpPort, loopbackReceiver: true)
        weak var released = first
        let queued = DispatchSemaphore(value: 0), finished = DispatchSemaphore(value: 0)
        var signalled = false
        first!.run(settings, duration: nil, update: { status in
            // Queued behind the held GET, not yet sent: the scenario under test.
            if !signalled && status.compatibilityRequest == "Compatibility Mode request queued" { signalled = true; queued.signal() }
        }, finish: { error, _ in
            guard error == nil else { print("FAIL: http-stop first worker: \(error!)"); exit(1) }
            finished.signal()
        })
        guard queued.wait(timeout: .now() + 5) == .success else { print("FAIL: http-stop: no queued Compatibility Mode request"); exit(1) }
        let stopped = sbio_now()
        first!.stop(); first = nil
        guard finished.wait(timeout: .now() + 1) == .success else { print("FAIL: http-stop: Stop timeout"); exit(1) }
        while released != nil && sbio_now() - stopped < 0.3 { Thread.sleep(forTimeInterval: 0.001) }
        guard released == nil else { print("FAIL: http-stop: worker retained by a receiver request after Stop"); exit(1) }
        let releasedAfter = sbio_now() - stopped
        settings.restoreCompatibility = false
        let replacement = BridgeWorker(receiverHTTPPort: httpPort, loopbackReceiver: true)
        let answered = DispatchSemaphore(value: 0)
        var answeredOnce = false
        let started = sbio_now()
        replacement.run(settings, duration: nil, update: { status in
            if !answeredOnce, status.receiverContact?.contains("HTTP 200") == true { answeredOnce = true; answered.signal() }
        }, finish: { _, _ in finished.signal() })
        // Snapshots publish once per second; the old request is held for 3 s.
        guard answered.wait(timeout: .now() + 2) == .success else { print("FAIL: http-stop: replacement status request blocked"); exit(1) }
        let answeredAfter = sbio_now() - started
        replacement.stop()
        guard finished.wait(timeout: .now() + 1) == .success else { exit(1) }
        print(String(format: "PASS: http-stop: worker released %.0f ms after Stop; replacement answered within %.0f ms", releasedAfter * 1000, answeredAfter * 1000))
    }
    static func check(_ condition: Bool, _ message: @autoclosure () -> String) {
        if !condition { print("FAIL: \(message())"); exit(1) }
    }
    /// Readings (tests/data/stats-cases.json, shared with desktop and Android), the /24 rule, the sweep with a
    /// stubbed probe and its cancellation, and the follow rule. Nothing here touches the network.
    static func ruleChecks(fixtures: String) {
        guard let data = FileManager.default.contents(atPath: fixtures),
              let cases = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { check(false, "stats-cases.json unreadable"); return }
        for (index, item) in cases.enumerated() {
            let message = try! JSONSerialization.data(withJSONObject: item["message"]!)
            let expected = item["expected"] as! [String: Any]
            let got = ReceiverReadings.parse(message)
            let want = ReceiverReadings(batteryPercent: (expected["batteryPercent"] as? NSNumber)?.intValue,
                                        coPpm: (expected["coPpm"] as? NSNumber)?.intValue,
                                        temperatureC: (expected["temperatureC"] as? NSNumber)?.doubleValue)
            check(got == want, "stats case \(index): \(String(describing: got)) != \(want)")
        }
        // Document order decides between two plausible values, as desktop and Android read them.
        check(ReceiverReadings.parse(Data(#"{"b":{"batteryCapacity_%":40},"a":{"batteryCapacity_%":60}}"#.utf8))?.batteryPercent == 40, "first value in document order")
        check(ReceiverReadings.parse(Data(#"{"x":{"CO_SENSOR_PPM":7},"x":{"CO_SENSOR_PPM":9}}"#.utf8))?.coPpm == 9, "repeated member keeps its last value")
        check(ReceiverReadings.parse(Data(#"{"sensor":{"ats":{"temp_c":NaN},"baro":{"temp_c":20.04}},"batteryCapacity_%":49.5}"#.utf8))
              == ReceiverReadings(batteryPercent: 50, temperatureC: 20.0), "NaN skipped, half up")
        for invalid in [#"{"batteryCapacity_%":50,}"#, #"{"batteryCapacity_%":050}"#, "{\"a\":\"\u{1}\"}", #"[1] x"#, ""] {
            check(ReceiverReadings.parse(Data(invalid.utf8)) == nil, "invalid JSON rejected: \(invalid)")
        }
        check(ReceiverReadings.parse(Data([0xff, 0x7b])) == nil, "non-UTF-8 rejected")
        check(ReceiverReadings(coPpm: 49).coWarning == nil, "no CO warning below 50 ppm")
        check(ReceiverReadings(coPpm: 50).coWarning == "Carbon monoxide 50 ppm, at or above 50 ppm.", "CO warning wording")
        check(ReceiverReadings(batteryPercent: 65, coPpm: 3, temperatureC: 28.2).temperature == "28.2°C", "temperature text")
        // The /24 rule.
        check(EFBSweep.onNetwork("192.168.4.20", "192.168.4.1"), "same /24")
        check(!EFBSweep.onNetwork("192.168.5.20", "192.168.4.1") && !EFBSweep.onNetwork("10.0.0.2", "192.168.4.1"), "another network")
        check(!EFBSweep.onNetwork("invalid", "192.168.4.1"), "invalid local")
        check(EFBSweep.localAddress(receiver: "127.0.0.1") == nil, "loopback is not the receiver's network")
        // The sweep: other hosts of the /24, in address order, without this device and the receiver.
        let hosts = EFBSweep.hosts(local: "192.0.2.5", exclude: ["192.0.2.1"])
        check(hosts.count == 252 && hosts.first == "192.0.2.2" && hosts.last == "192.0.2.254" && !hosts.contains("192.0.2.5"), "sweep hosts")
        var probed: [String] = []
        let found = EFBSweep.sweep(local: "192.0.2.5", exclude: ["192.0.2.1"], probe: { addresses in
            probed = addresses; return addresses.map { ["192.0.2.30", "192.0.2.7"].contains($0) }
        }, cancelled: { false })
        check(found == ["192.0.2.7", "192.0.2.30"] && probed == hosts, "sweep result \(found)")
        var cancelled = false
        check(EFBSweep.sweep(local: "192.0.2.5", exclude: [], probe: { addresses in cancelled = true; return addresses.map { _ in true } },
                             cancelled: { cancelled }).isEmpty, "cancelled during the probes gives none")
        check(EFBSweep.sweep(local: "192.0.2.5", exclude: [], probe: { _ in check(false, "probed after cancel"); return nil },
                             cancelled: { true }).isEmpty, "cancelled before the probes gives none")
        check(EFBSweep.sweep(local: "192.0.2.5", exclude: [], probe: { _ in nil }, cancelled: { false }).isEmpty, "a cancelled probe gives none")
        // The follow rule.
        check(EFBSweep.followChoice(["192.0.2.9", "192.0.2.10"], current: "192.0.2.10") == "192.0.2.10", "recovered EFB kept")
        check(EFBSweep.followChoice(["192.0.2.9"], current: "192.0.2.10") == "192.0.2.9", "one other device")
        check(EFBSweep.followChoice(["192.0.2.8", "192.0.2.9"], current: "192.0.2.10") == nil && EFBSweep.followChoice([], current: "192.0.2.10") == nil, "several or none")
        var never = EFBFollower()
        for _ in 0..<5 { check(!never.checked(answered: false), "an EFB that never answered is never followed") }
        var rule = EFBFollower()
        check(!rule.checked(answered: true) && !rule.checked(answered: false), "one miss is not enough")
        check(rule.checked(answered: false), "two misses in a row sweep")
        check(rule.swept(["192.0.2.10"], current: "192.0.2.10") == .keep, "recovered: keep")
        check(!rule.checked(answered: false) && rule.checked(answered: false), "misses count again after recovery")
        let none = "The EFB at 192.0.2.10 stopped answering. Devices on the receiver's WiFi: none. Choose one with Find EFB."
        check(rule.swept([], current: "192.0.2.10") == .report(none), "none: reported")
        check(rule.checked(answered: false) && rule.swept(["192.0.2.8", "192.0.2.9"], current: "192.0.2.10") == .keep, "reported once per address")
        check(rule.checked(answered: false) && rule.swept(["192.0.2.9"], current: "192.0.2.10") == .follow("192.0.2.9"), "one other device: follow")
        var answered = EFBFollower()
        _ = answered.checked(answered: true); _ = answered.checked(answered: false); _ = answered.checked(answered: true)
        check(!answered.checked(answered: false), "an answer between misses resets the count")
        check(EFBFollower.followMessage(from: "192.0.2.10", to: "192.0.2.9") == "The EFB at 192.0.2.10 stopped answering; now sending to 192.0.2.9, the only other device on the receiver's WiFi.", "follow message")
        print("PASS: readings for \(cases.count) shared stats cases, /24 rule, sweep and cancellation, follow rule")
    }
    /// The follower in a running worker with a stubbed probe and local address: no probe reaches the network,
    /// and nothing is forwarded (the input is not GDL90, so the destination receives no datagram). The EFB at
    /// .10 answers once, then moves to .20: the run switches, reports the new address and keeps sending there.
    /// A stopped run's follower has ended before finish and never probes again.
    static func followChecks(_ original: BridgeSettings) {
        var settings = original
        settings.destination = "192.0.2.10"; settings.outputPort = "4000"; settings.discoveryPort = "0"
        let lock = NSLock()
        var devices: Set<String> = ["192.0.2.10"], checks = 0, probes = 0
        let worker = BridgeWorker(followCheckS: 0.2, local: "192.0.2.5") { addresses in
            lock.lock(); defer { lock.unlock() }
            probes += 1
            let answers = addresses.map { devices.contains($0) }
            if addresses.count == 1 { checks += 1; if checks == 1 { devices = ["192.0.2.20"] } }  // answered once, then moved
            return answers
        }
        let sender = socket(AF_INET, SOCK_DGRAM, 0)
        var target = sockaddr_in(); target.sin_family = sa_family_t(AF_INET); target.sin_port = UInt16(settings.inputPort)!.bigEndian
        target.sin_addr.s_addr = inet_addr("127.0.0.1")
        let feed = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "feed"))
        feed.schedule(deadline: .now(), repeating: 0.1)
        feed.setEventHandler {
            var copy = target; let bytes: [UInt8] = [1, 2, 3]
            withUnsafePointer(to: &copy) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { _ = sendto(sender, bytes, bytes.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        }
        feed.resume()
        let switched = DispatchSemaphore(value: 0), finished = DispatchSemaphore(value: 0)
        var reported = false
        worker.run(settings, duration: nil, update: { status in
            check(status.forwarded == 0 && status.sendErrors == 0, "follow test forwarded a datagram")
            if !reported, status.followedDestination == "192.0.2.20",
               status.followMessage == EFBFollower.followMessage(from: "192.0.2.10", to: "192.0.2.20") { reported = true; switched.signal() }
            check(status.followMessage?.hasPrefix("Could not") != true, "destination change refused")
        }, finish: { error, _ in check(error == nil, "follow run: \(error ?? "")"); finished.signal() })
        check(switched.wait(timeout: .now() + 5) == .success, "the run did not follow the EFB to its new address")
        // It now checks the new address, which answers: no further switch.
        Thread.sleep(forTimeInterval: 0.5)
        lock.lock(); let checked = checks; lock.unlock()
        check(checked >= 3, "the follower stopped checking after the switch")
        worker.stop()
        check(finished.wait(timeout: .now() + 1) == .success, "Stop timeout with a follower")
        lock.lock(); let after = probes; lock.unlock()
        Thread.sleep(forTimeInterval: 0.5)
        lock.lock(); let later = probes; lock.unlock()
        check(after == later, "a stopped run's follower probed again")
        feed.cancel(); close(sender)
        print("PASS: follower switched 192.0.2.10 -> 192.0.2.20 after two misses, kept checking, ended before finish")
    }
    /// A run that ends without Stop (its length completed) still cancels a follower probe in progress: the real
    /// C probe, given the worker's token, aimed at 127.0.0.2 (loopback, silent until its 3 s timeout).
    static func followEndChecks(_ original: BridgeSettings) {
        var settings = original
        settings.destination = "192.0.2.10"; settings.outputPort = "4000"; settings.discoveryPort = "0"
        let lock = NSLock()
        var started: Double?, returned: Double?
        let worker = BridgeWorker(followCheckS: 0.1, local: "192.0.2.5") { addresses, cancel in
            lock.lock(); if started == nil { started = sbio_now() }; lock.unlock()
            let answers = EFBSweep.probe(cancel)(addresses.map { _ in "127.0.0.2" })
            lock.lock(); returned = sbio_now(); lock.unlock()
            return answers
        }
        let sender = socket(AF_INET, SOCK_DGRAM, 0)
        var target = sockaddr_in(); target.sin_family = sa_family_t(AF_INET); target.sin_port = UInt16(settings.inputPort)!.bigEndian
        target.sin_addr.s_addr = inet_addr("127.0.0.1")
        let feed = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "feed"))
        feed.schedule(deadline: .now(), repeating: 0.1)
        feed.setEventHandler {
            var copy = target; let bytes: [UInt8] = [1, 2, 3]
            withUnsafePointer(to: &copy) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { _ = sendto(sender, bytes, bytes.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        }
        feed.resume()
        let finished = DispatchSemaphore(value: 0)
        var completedAt = 0.0, completed = false
        worker.run(settings, duration: 2, update: { status in
            check(status.forwarded == 0 && status.sendErrors == 0, "follow-end test forwarded a datagram")
        }, finish: { error, done in
            check(error == nil, "follow-end run: \(error ?? "")"); completed = done; completedAt = sbio_now(); finished.signal()
        })
        check(finished.wait(timeout: .now() + 6) == .success, "follow-end run did not finish")
        feed.cancel(); close(sender)
        lock.lock(); let probeStarted = started, probeReturned = returned; lock.unlock()
        guard let probeStarted, let probeReturned else { check(false, "the follower never probed"); return }
        // The probe began before the run's length ended and would have lasted 3 s; it returned at the run's end.
        check(completed && probeReturned - probeStarted < 2.5 && completedAt - probeReturned < 0.2,
              String(format: "follow-end: probe %.2f s, finish %.2f s after it", probeReturned - probeStarted, completedAt - probeReturned))
        print(String(format: "PASS: run end without Stop cancelled the follower probe after %.2f s of its 3 s; finish %.0f ms later",
                     probeReturned - probeStarted, (completedAt - probeReturned) * 1000))
    }
    /// The Find EFB list: results shown, then the follower switches the destination; the list is cleared and the earlier
    /// search's late results stay out. A new search on the new destination lists again.
    static func discoveryListChecks() {
        var list = DiscoveryList()
        let efb = DiscoveredEFB(address: "192.0.2.10", port: 4000, app: "EFB", seen: 100)
        let device = DiscoveredEFB(address: "192.0.2.20", port: 4000, app: "Device", seen: 100)
        list.start(route: "192.0.2.10")
        list.announce([efb], route: "192.0.2.10"); list.sweep([device, DiscoveredEFB(address: "192.0.2.10", port: 4000, app: "Device", seen: 100)], route: "192.0.2.10")
        check(list.entries(now: 101).map(\.app) == ["EFB", "Device"], "announced first, swept without announced addresses")
        list.routeChanged()  // the follower switched to 192.0.2.20
        check(list.entries(now: 101).isEmpty && !list.isCurrent(route: "192.0.2.20"), "route change clears the list")
        list.announce([efb], route: "192.0.2.20"); list.sweep([device], route: "192.0.2.20")
        check(list.entries(now: 102).isEmpty, "the earlier search's results stay out after the route change")
        list.start(route: "192.0.2.20"); list.sweep([device], route: "192.0.2.20")
        check(list.entries(now: 102) == [device], "a new search lists again")
        list.sweep([device], route: "192.0.2.10")
        check(list.entries(now: 102) == [device], "results for another destination are dropped")
        check(list.entries(now: 131).isEmpty, "entries expire after 30 s")
        print("PASS: Find EFB list cleared on a route change; the earlier search's results stay out")
    }
    /// Find EFB's sweep with a stubbed probe (no network): devices are listed as "Device" on the EFB UDP port without
    /// the receiver or unusable addresses; the job finishes only after the sweep has ended, after an error (the
    /// listener cannot open) and after Exit; Exit drops the swept list.
    static func sweepChecks(_ original: BridgeSettings) {
        var settings = original
        settings.source = "192.0.2.1"; settings.outputPort = "4000"
        let lock = NSLock()
        var sweepEnded = 0.0
        func worker(delay: Double) -> BridgeWorker {
            BridgeWorker(followCheckS: 30, local: "192.0.2.5") { addresses in
                Thread.sleep(forTimeInterval: delay)
                lock.lock(); sweepEnded = sbio_now(); lock.unlock()
                return addresses.map { ["192.0.2.1", "192.0.2.9", "192.0.2.255"].contains($0) || $0 == "192.0.2.40" }
            }
        }
        // Error path: discovery port 0 cannot listen; the sweep still finishes first and reports its devices.
        settings.discoveryPort = "0"
        let done = DispatchSemaphore(value: 0)
        var swept: [DiscoveredEFB] = [], finishedAt = 0.0, failure: String?
        worker(delay: 0.3).findEFBs(settings, update: { _ in }, swept: { swept = $0 }, finish: { error in
            failure = error; finishedAt = sbio_now(); done.signal()
        })
        check(done.wait(timeout: .now() + 3) == .success, "sweep job did not finish")
        lock.lock(); let ended = sweepEnded; lock.unlock()
        check(failure != nil && ended > 0 && finishedAt >= ended, "finish before the sweep ended on the error path")
        check(swept.map(\.address) == ["192.0.2.9", "192.0.2.40"] && swept.allSatisfy { $0.app == "Device" && $0.port == 4000 }, "swept devices \(swept)")
        // Exit while sweeping: no list, and finish only after the sweep returned.
        settings.discoveryPort = settings.inputPort
        lock.lock(); sweepEnded = 0; lock.unlock()
        let search = worker(delay: 0.4)
        var exitedList = false
        search.findEFBs(settings, update: { _ in }, swept: { _ in exitedList = true }, finish: { _ in finishedAt = sbio_now(); done.signal() })
        Thread.sleep(forTimeInterval: 0.1); search.stop()
        check(done.wait(timeout: .now() + 2) == .success, "Exit did not end the sweep job")
        lock.lock(); let exited = sweepEnded; lock.unlock()
        check(exited > 0 && finishedAt >= exited && !exitedList, "Exit: finish before the sweep ended, or a list after Exit")
        // Not on the receiver's network: no sweep at all.
        let away = BridgeWorker(followCheckS: 30, local: nil) { _ in check(false, "swept off the receiver's network"); return nil }
        away.findEFBs(settings, listen: false, update: { _ in }, finish: { _ in done.signal() })
        check(done.wait(timeout: .now() + 2) == .success, "sweep-only job did not finish")
        print("PASS: Find EFB sweep lists devices, finishes after the sweep on error and Exit, skips other networks")
    }
    /// The statistics reader in a running worker against the harness's loopback WebSocket server: readings reach the
    /// snapshot, and Stop ends the reader before finish.
    static func readingsChecks(_ original: BridgeSettings, httpPort: UInt16) {
        var settings = original
        settings.discoveryPort = "0"
        let worker = BridgeWorker(receiverHTTPPort: httpPort, readsLoopbackStats: true)
        let read = DispatchSemaphore(value: 0), finished = DispatchSemaphore(value: 0)
        var signalled = false
        worker.run(settings, duration: nil, update: { status in
            if !signalled, status.readings == ReceiverReadings(batteryPercent: 65, coPpm: 3, temperatureC: 28.2) { signalled = true; read.signal() }
        }, finish: { error, _ in check(error == nil, "readings run: \(error ?? "")"); finished.signal() })
        check(read.wait(timeout: .now() + 10) == .success, "no readings from the statistics stream")
        let stopped = sbio_now()
        worker.stop()
        check(finished.wait(timeout: .now() + 1) == .success, "Stop timeout with a statistics reader")
        print(String(format: "PASS: readings from the statistics stream; Stop ended the reader in %.0f ms", (sbio_now() - stopped) * 1000))
    }
    static func lifecycleChecks(_ original: BridgeSettings) {
        var settings = original
        settings.discoveryPort = settings.inputPort
        var maximumStop = 0.0
        for _ in 0..<30 {
            for search in [false, true] {
                autoreleasepool {
                    var worker: BridgeWorker? = BridgeWorker()
                    weak var released = worker
                    let opened = DispatchSemaphore(value: 0), finished = DispatchSemaphore(value: 0)
                    if search {
                        worker!.findEFBs(settings, update: { _ in opened.signal() }, finish: { error in
                            guard error == nil else { print("FAIL: lifecycle discovery: \(error!)"); exit(1) }
                            finished.signal()
                        })
                    } else {
                        worker!.run(settings, duration: nil, update: { _ in opened.signal() }, finish: { error, completed in
                            guard error == nil && !completed else { print("FAIL: lifecycle bridge"); exit(1) }
                            finished.signal()
                        })
                    }
                    guard opened.wait(timeout: .now() + 3) == .success else { print("FAIL: lifecycle socket open"); exit(1) }
                    let stopped = sbio_now()
                    worker!.stop(); worker = nil
                    guard finished.wait(timeout: .now() + 1) == .success else { print("FAIL: lifecycle Stop timeout"); exit(1) }
                    maximumStop = max(maximumStop, sbio_now() - stopped)
                    let releaseDeadline = sbio_now() + 1
                    while released != nil && sbio_now() < releaseDeadline { Thread.sleep(forTimeInterval: 0.001) }
                    guard released == nil else { print("FAIL: retained worker after completion"); exit(1) }
                    released = nil
                }
            }
        }
        // Cancellation must complete even while another live search owns the queue/port.
        let search = BridgeWorker(), bridge = BridgeWorker()
        let opened = DispatchSemaphore(value: 0), cancelled = DispatchSemaphore(value: 0), searchFinished = DispatchSemaphore(value: 0)
        search.findEFBs(settings, update: { _ in opened.signal() }, finish: { _ in searchFinished.signal() })
        guard opened.wait(timeout: .now() + 3) == .success else { exit(1) }
        bridge.run(settings, duration: nil, update: { _ in }, finish: { error, completed in
            guard error == nil && !completed else { exit(1) }; cancelled.signal()
        })
        Thread.sleep(forTimeInterval: 0.05)
        let stopped = sbio_now(); bridge.stop()
        guard cancelled.wait(timeout: .now() + 0.2) == .success else { print("FAIL: Stop blocked behind discovery queue"); exit(1) }
        maximumStop = max(maximumStop, sbio_now() - stopped)
        search.stop()
        guard searchFinished.wait(timeout: .now() + 1) == .success else { exit(1) }
        print(String(format: "PASS: 60 lifecycle cycles, workers released; max Stop %.1f ms; queued Stop cancellable", maximumStop * 1000))
    }
}
