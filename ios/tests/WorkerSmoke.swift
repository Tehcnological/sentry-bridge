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
