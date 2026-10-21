import XCTest
import Darwin

final class BridgeUITests: XCTestCase {
    private var sockets: [Int32] = []
    private var timers: [(DispatchSourceTimer, DispatchQueue)] = []
    private func udpSocket() -> Int32 {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        if fd >= 0 { sockets.append(fd) }
        return fd
    }
    private func timer(on queue: DispatchQueue) -> DispatchSourceTimer {
        let source = DispatchSource.makeTimerSource(queue: queue)
        timers.append((source, queue))
        return source
    }
    override func tearDownWithError() throws {
        // XCTest assertion aborts may bypass Swift defer. Always release test-owned resources.
        for (source, _) in timers { source.cancel() }
        for (_, queue) in timers { queue.sync {} }
        timers.removeAll()
        for fd in sockets { close(fd) }
        sockets.removeAll()
        try super.tearDownWithError()
    }
    override func setUpWithError() throws {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
        addUIInterruptionMonitor(withDescription: "Local Network permission") { alert in
            let text = alert.staticTexts.allElementsBoundByIndex.map(\.label).joined(separator: " ").lowercased()
            guard text.contains("local network") else { return false }
            for label in ["Allow", "OK"] {
                let button = alert.buttons[label]
                if button.exists { button.tap(); return true }
            }
            return false
        }
    }
    /// Selects Other device in the segmented control or, at accessibility text sizes, the inline
    /// list, and confirms the route's address field appears.
    private func selectOtherDevice(_ app: XCUIApplication) {
        let field = app.textFields["Other device IPv4"]
        let other = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Other device")).firstMatch
        for _ in 0..<3 {
            reveal(other, in: app)
            other.tap()
            if field.waitForExistence(timeout: 3) { return }
        }
        XCTFail("Select the Other device topology before editing its route")
    }
    func testFindEFBExitWhileStopped() {
        let app = launch()
        selectOtherDevice(app)
        let find = app.buttons["bridge.findEFB"]
        for _ in 0..<5 { if find.isHittable { break }; app.swipeUp() }
        find.tap()
        XCTAssertTrue(app.buttons["Exit"].waitForExistence(timeout: 5))
        app.buttons["Exit"].tap()
        XCTAssertTrue(find.waitForExistence(timeout: 5))
        find.tap()
        XCTAssertTrue(app.staticTexts["Listening for EFBs"].waitForExistence(timeout: 5))
        app.buttons["Exit"].tap()
    }
    func testFindEFBReceiveAndUseWhileStopped() throws { try findAndUse(running: false) }
    func testFindEFBUsesRunningBridgeAnnouncements() throws { try findAndUse(running: true) }
    func testRunningDiscoverySamplesCacheAfterDelayedObserver() throws {
        try findAndUse(running: true, delayedObserver: true)
    }
    private func findAndUse(running: Bool, delayedObserver: Bool = false) throws {
        var interfaces: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&interfaces) == 0 else { throw XCTSkip("No interface information") }
        var node = interfaces
        var local: sockaddr_in?
        while let item = node {
            if let address = item.pointee.ifa_addr, address.pointee.sa_family == AF_INET,
               item.pointee.ifa_flags & UInt32(IFF_LOOPBACK) == 0,
               item.pointee.ifa_flags & UInt32(IFF_UP | IFF_RUNNING) == UInt32(IFF_UP | IFF_RUNNING) {
                let candidate = UnsafeRawPointer(address).assumingMemoryBound(to: sockaddr_in.self).pointee
                if local == nil { local = candidate }
                // Prefer an active LAN address to a transient USB/link-local interface.
                if UInt32(bigEndian: candidate.sin_addr.s_addr) & 0xffff0000 != 0xa9fe0000 {
                    local = candidate; break
                }
            }
            node = item.pointee.ifa_next
        }
        freeifaddrs(interfaces)
        guard var source = local else { throw XCTSkip("Eligible-address UI check needs a non-loopback IPv4 interface") }
        var text = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
        _ = inet_ntop(AF_INET, &source.sin_addr, &text, socklen_t(text.count))
        let address = String(cString: text)
        let sender = udpSocket()
        XCTAssertGreaterThanOrEqual(sender, 0)
        source.sin_port = 0
        let bound = withUnsafePointer(to: &source) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(sender, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        XCTAssertEqual(bound, 0)
        let app = launch(extraArguments: delayedObserver ? ["--ui-delay-discovery-observer"] : [])
        selectOtherDevice(app)
        let field = app.textFields["Other device IPv4"]
        reveal(field, in: app)
        if running {
            replaceText(in: field, with: address + "\n", app: app)
            XCTAssertEqual(field.value as? String, address)
            for _ in 0..<5 { if app.buttons["bridge.startStop"].isHittable { break }; app.swipeDown() }
            app.buttons["bridge.startStop"].tap()
            XCTAssertTrue(app.buttons["Stop Bridge"].waitForExistence(timeout: 5))
        }
        var destination = sockaddr_in()
        destination.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        destination.sin_family = sa_family_t(AF_INET)
        destination.sin_port = UInt16(63093).bigEndian
        destination.sin_addr.s_addr = inet_addr("127.0.0.1")
        let target = destination
        let bytes = Data("{\"App\":\"Synthetic EFB\",\"GDL90\":{\"port\":4001}}".utf8)
        let queue = DispatchQueue(label: "bridge.ui-test.discovery")
        let pulse = timer(on: queue)
        pulse.schedule(deadline: .now(), repeating: 0.1)
        pulse.setEventHandler {
            var destination = target
            withUnsafePointer(to: &destination) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { pointer in
                _ = bytes.withUnsafeBytes { sendto(sender, $0.baseAddress, $0.count, 0, pointer, socklen_t(MemoryLayout<sockaddr_in>.size)) }
            } }
        }
        pulse.resume()
        let find = app.buttons["bridge.findEFB"]
        for _ in 0..<5 { if find.isHittable { break }; app.swipeUp() }
        find.tap()
        XCTAssertTrue(app.staticTexts["Synthetic EFB"].waitForExistence(timeout: delayedObserver ? 15 : 10))
        XCTAssertEqual(app.staticTexts.matching(identifier: "Synthetic EFB").count, 1)
        if running {
            XCTAssertFalse(app.buttons["bridge.useEFB"].isEnabled)
            app.buttons["Exit"].tap()
            for _ in 0..<5 { if app.buttons["bridge.startStop"].isHittable { break }; app.swipeDown() }
            app.buttons["bridge.startStop"].tap()
            XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
        } else {
            app.buttons["bridge.useEFB"].tap()
            XCTAssertTrue(field.waitForExistence(timeout: 5))
            XCTAssertEqual(field.value as? String, address)
        }
    }
    private func launch(extraArguments: [String] = [], extraEnvironment: [String: String] = [:]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-testing"] + extraArguments
        app.launchEnvironment = extraEnvironment
        app.launch()
        return app
    }
    /// Replaces the whole value through native selection, so the caret position cannot leave digits behind.
    /// Each attempt waits for the keyboard, because its scroll can move the field under a gesture.
    private func replaceText(in field: XCUIElement, with text: String, app: XCUIApplication) {
        let expected = text.trimmingCharacters(in: .newlines)
        for attempt in 1...3 {
            XCTContext.runActivity(named: "Replace field value, attempt \(attempt)") { _ in
                field.tap()
                _ = app.keyboards.firstMatch.waitForExistence(timeout: 5)
                field.tap()  // A tap in a focused field opens the edit menu.
                let selectAll = app.menuItems["Select All"]
                if selectAll.waitForExistence(timeout: 2) { selectAll.tap() } else { field.doubleTap() }
                // One character at a time: fast whole-string typing can drop characters in the simulator.
                for character in text { field.typeText(String(character)) }
            }
            if field.value as? String == expected { return }
        }
    }
    /// Scrolls until the element is hittable and clear of the navigation bar and bottom edge.
    /// Bounds come from the actual viewport, so compact landscape heights are handled.
    private func reveal(_ element: XCUIElement, in app: XCUIApplication, upwards: Bool = true) {
        var top = app.frame.minY
        for attempt in 0..<30 {
            let bar = app.navigationBars.firstMatch
            top = bar.exists ? bar.frame.maxY : app.frame.minY
            if element.exists && element.isHittable {
                let frame = element.frame
                if frame.minY >= top && frame.maxY <= app.frame.maxY - 15 { return }
            }
            let centre = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
            if element.exists {
                // Loaded: drag slowly, with a hold, to centre it in the visible band (or align the top
                // of a taller element). At least 60 pt, so the gesture always scrolls: a shorter drag
                // counts as a tap and could change whatever control is under the screen centre.
                let frame = element.frame, bottom = app.frame.maxY - 15
                let needed = frame.height > bottom - top ? top + 8 - frame.minY : (top + bottom) / 2 - frame.midY
                let distance = min(0.4 * app.frame.height, max(60, abs(needed)))
                let offset = needed < 0 ? -distance : distance
                centre.press(forDuration: 0.05, thenDragTo: centre.withOffset(CGVector(dx: 0, dy: offset)),
                             withVelocity: .slow, thenHoldForDuration: 0.3)
            } else {
                // Lists load rows lazily: an unloaded element may be on either side, so search back
                // after 12 long drags. Accessibility text sizes can make a list many screens tall.
                let directionUp = (attempt < 12) == upwards
                let from = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: directionUp ? 0.8 : 0.2))
                let to = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: directionUp ? 0.2 : 0.8))
                from.press(forDuration: 0.05, thenDragTo: to)
            }
        }
        // Same criterion as the early return. An element taller than the visible area passes only
        // when its top is clear of the navigation bar, and that substitution is recorded.
        let frame = element.exists ? element.frame : .null
        let bottom = app.frame.maxY - 15
        let tall = frame.height > bottom - top && frame.minY >= top && frame.minY < bottom
        if tall && element.isHittable {
            XCTContext.runActivity(named: "Reveal accepted taller-than-viewport \(element) with clear top: \(frame)") { _ in }
            return
        }
        XCTAssertTrue(element.exists && element.isHittable && frame.minY >= top && frame.maxY <= bottom,
                      "Reveal \(element): frame \(frame), visible \(top)...\(bottom), hittable \(element.exists && element.isHittable)")
    }
    func testSavedRoutingSurvivesRelaunch() {
        // Exercise real persistence in this disposable simulator's app container.
        let app = XCUIApplication(); app.launchArguments = []; app.launch()
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let replay = app.buttons["Use synthetic replay route"]
        reveal(replay, in: app); replay.tap()
        let start = app.buttons["bridge.startStop"]
        reveal(start, in: app, upwards: false); start.tap()
        let diagnostics = app.buttons["Diagnostics"]
        reveal(diagnostics, in: app); diagnostics.tap()
        let opened = app.staticTexts["Waiting for receiver data"]
        reveal(opened, in: app)
        XCTAssertTrue(opened.waitForExistence(timeout: 10))
        reveal(start, in: app, upwards: false); start.tap()
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
        app.terminate(); app.launch()
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 10))
        reveal(settings, in: app); settings.tap()
        let input = app.textFields["Input UDP port"]
        reveal(input, in: app)
        XCTAssertEqual(input.value as? String, "14000")
        XCTAssertEqual(app.textFields["Sentry IPv4"].value as? String, "127.0.0.1")
        let output = app.textFields["Output UDP port"]
        reveal(output, in: app)
        XCTAssertEqual(output.value as? String, "14001")
    }
    func testOccupiedReceiverPortIsActionable() {
        let app = launch()
        let fd = udpSocket()
        XCTAssertGreaterThanOrEqual(fd, 0)
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET); address.sin_port = UInt16(14000).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        XCTAssertEqual(bound, 0)
        app.buttons["bridge.startStop"].tap()
        let message = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Receiver UDP port 14000 is already in use")).firstMatch
        XCTAssertTrue(message.waitForExistence(timeout: 10))
        XCTAssertTrue(message.label.contains("Close the EFB"))
        XCTAssertTrue(app.buttons["Start Bridge"].exists)
        XCTAssertFalse(app.staticTexts["Receiving • forwarding"].exists)
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let input = app.textFields["Input UDP port"]
        reveal(input, in: app)
        replaceText(in: input, with: "0\n", app: app)
        XCTAssertEqual(input.value as? String, "0")
        let start = app.buttons["bridge.startStop"]
        reveal(start, in: app, upwards: false); start.tap()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Use ports from 1 to 65535")).firstMatch.waitForExistence(timeout: 5))
    }
    func testLayoutsAndAccessibilityLabels() {
        for orientation in [UIDeviceOrientation.portrait, .landscapeLeft] {
            XCUIDevice.shared.orientation = orientation
            let app = launch()
            let start = app.buttons["bridge.startStop"]
            XCTAssertTrue(start.waitForExistence(timeout: 10))
            XCTAssertTrue(start.isHittable)
            let ready = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
            ready.name = "Main layout \(orientation.rawValue)"; ready.lifetime = .keepAlways; add(ready)
            let location = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "This device")).firstMatch
            reveal(location, in: app)
            XCTAssertTrue(location.isEnabled)
            let locationShot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
            locationShot.name = "EFB setup layout \(orientation.rawValue)"; locationShot.lifetime = .keepAlways; add(locationShot)
            let settings = app.buttons["Settings"]
            reveal(settings, in: app); settings.tap()
            for label in ["Sentry IPv4", "Input UDP port", "Destination IPv4", "Output UDP port", "EFB discovery UDP port"] {
                let field = app.textFields[label]
                reveal(field, in: app)
                XCTAssertEqual(field.label, label)
                XCTAssertTrue(field.isEnabled)
            }
            let screenshot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
            screenshot.name = "Settings layout \(orientation.rawValue)"; screenshot.lifetime = .keepAlways; add(screenshot)
            app.terminate()
        }
        XCUIDevice.shared.orientation = .portrait
    }
    /// Lifecycle cycles. For a same-process memory capture, run with TEST_RUNNER_SB_LEAK_GATES=<folder>:
    /// the test then writes <phase>.ready (baseline, after, after2), waits for <phase>.go, and runs a
    /// second identical round. At each .ready, inspect the app process (for example with `leaks`), then
    /// create the matching .go file to continue. Use a new folder per capture. Without the variable it
    /// runs one round.
    func testWorkerStartStopAndDiscoveryCycles() {
        let app = launch(extraEnvironment: ["MallocStackLogging": "1"])
        let start = app.buttons["bridge.startStop"]
        XCTAssertTrue(start.waitForExistence(timeout: 10))
        let gates = ProcessInfo.processInfo.environment["SB_LEAK_GATES"]
        func gate(_ phase: String) {
            guard let gates else { return }
            let path = URL(fileURLWithPath: gates).appendingPathComponent(phase).path
            FileManager.default.createFile(atPath: path + ".ready", contents: nil)
            for _ in 0..<1200 { if FileManager.default.fileExists(atPath: path + ".go") { return }; Thread.sleep(forTimeInterval: 0.5) }
            XCTFail("No capture signal for \(phase)")
        }
        if let gates {
            // A stale .go file from an earlier capture would skip a pause: start from a clean gate set.
            try? FileManager.default.createDirectory(atPath: gates, withIntermediateDirectories: true)
            for phase in ["baseline", "after", "after2"] {
                for suffix in [".ready", ".go"] {
                    try? FileManager.default.removeItem(atPath: URL(fileURLWithPath: gates).appendingPathComponent(phase).path + suffix)
                }
            }
        }
        gate("baseline")
        for phase in gates == nil ? ["after"] : ["after", "after2"] {
            if phase == "after2" {
                // Start needs the local route again after the first round selected Other device.
                let local = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "This device")).firstMatch
                reveal(local, in: app); local.tap()
                reveal(start, in: app, upwards: false)
            }
            for _ in 0..<5 {
                start.tap()
                XCTAssertTrue(app.buttons["Stop Bridge"].waitForExistence(timeout: 5))
                start.tap()
                XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
            }
            selectOtherDevice(app)
            let find = app.buttons["bridge.findEFB"]
            reveal(find, in: app)
            for _ in 0..<5 {
                find.tap()
                XCTAssertTrue(app.buttons["Exit"].waitForExistence(timeout: 5))
                app.buttons["Exit"].tap()
                XCTAssertTrue(find.waitForExistence(timeout: 5))
            }
            gate(phase)
        }
    }
    func testForegroundStartStopAndReplayRoute() {
        let app = launch()
        XCTAssertTrue(app.buttons["bridge.startStop"].waitForExistence(timeout: 10))
        let before = XCTAttachment(screenshot: app.screenshot()); before.name = "Ready"; before.lifetime = .keepAlways; add(before)
        app.buttons["bridge.startStop"].tap()
        XCTAssertTrue(app.buttons["Stop Bridge"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Waiting for data"].waitForExistence(timeout: 5))
        app.buttons["bridge.startStop"].tap()
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
        // Immediate restart checks completion did not race socket release.
        app.buttons["bridge.startStop"].tap()
        XCTAssertTrue(app.staticTexts["Waiting for data"].waitForExistence(timeout: 5))
        let running = XCTAttachment(screenshot: app.screenshot()); running.name = "Foreground bridge"; running.lifetime = .keepAlways; add(running)
        app.buttons["bridge.startStop"].tap()
    }
    func testStopsWhenBackgrounded() {
        let app = launch(); app.buttons["bridge.startStop"].tap()
        XCTAssertTrue(app.buttons["Stop Bridge"].waitForExistence(timeout: 5))
        XCUIDevice.shared.press(.home)
        app.activate()
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Foreground bridge stopped'")).firstMatch.exists)
    }

    // Run under each simulator system appearance; the app must not force a scheme.
    /// The app's layout decision and the available size it used, published only in UI-test mode.
    private func layout(_ app: XCUIApplication) -> (two: Bool, width: Double, height: Double) {
        let marker = app.descendants(matching: .any).matching(identifier: "bridge.layout").firstMatch
        XCTAssertTrue(marker.waitForExistence(timeout: 5), "layout marker")
        let parts = (marker.value as? String ?? "").split(separator: " ")
        let size = parts.count == 2 ? parts[1].split(separator: "x").compactMap { Double($0) } : []
        XCTAssertEqual(size.count, 2, "layout marker value \(marker.value ?? "nil")")
        return (parts.first == "two", size.first ?? 0, size.last ?? 0)
    }
    /// Landscape: two columns exactly when the available width is at least 768 pt, wider than tall,
    /// and text is xLarge or smaller; otherwise one column with EFB setup reachable by scrolling.
    /// Text size is set per launch, so the simulator's own setting does not decide the case.
    func testLandscapeTwoColumnLayout() {
        for (category, textAllowsTwo) in [("UICTContentSizeCategoryL", true), ("UICTContentSizeCategoryAccessibilityXXXL", false)] {
            XCUIDevice.shared.orientation = .landscapeLeft
            let app = launch(extraArguments: ["-UIPreferredContentSizeCategoryName", category])
            let start = app.buttons["bridge.startStop"]
            let location = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "This device")).firstMatch
            XCTAssertTrue(start.waitForExistence(timeout: 10))
            let seen = layout(app)
            let expected = textAllowsTwo && seen.width >= 768 && seen.width > seen.height
            XCTAssertEqual(seen.two, expected, "\(category) at \(Int(seen.width))x\(Int(seen.height))")
            let shot = XCTAttachment(screenshot: app.screenshot())
            shot.name = "Landscape \(category) \(seen.two ? "two" : "one") column \(Int(seen.width))x\(Int(seen.height))"
            shot.lifetime = .keepAlways; add(shot)
            if expected {
                XCTAssertTrue(start.isHittable, "Start/Stop is visible in the status column")
                XCTAssertTrue(location.waitForExistence(timeout: 5) && location.isHittable, "EFB setup is visible beside status")
                XCTAssertLessThan(start.frame.maxX, location.frame.minX, "status column is left of the setup column")
            } else {
                reveal(location, in: app)
                XCTAssertTrue(location.isHittable, "single column still reaches EFB setup")
            }
            XCUIDevice.shared.orientation = .portrait
            // A single column scrolled to EFB setup leaves Start/Stop off screen (and out of the
            // hierarchy at large text sizes); scroll back to it rather than waiting for it to exist.
            reveal(start, in: app)
            XCTAssertTrue(start.exists && start.isHittable, "Start/Stop is reachable again in portrait")
            XCTAssertFalse(layout(app).two, "portrait is one column")
            app.terminate()
        }
    }
    /// Rotating while editing a Settings field keeps keyboard focus on that field, including when
    /// the layout switches between one and two columns.
    func testRotationKeepsEditingFocus() {
        XCUIDevice.shared.orientation = .portrait
        let app = launch(extraArguments: ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryL"])
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let field = app.textFields["Destination IPv4"]
        reveal(field, in: app); field.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        let before = layout(app).two
        XCUIDevice.shared.orientation = .landscapeLeft
        let after = layout(app).two
        let focused = NSPredicate(format: "hasKeyboardFocus == true")
        XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: focused, object: app.textFields["Destination IPv4"])], timeout: 5),
                       .completed, "Destination IPv4 keeps focus after rotation (layout \(before ? "two" : "one") to \(after ? "two" : "one"))")
        XCTAssertTrue(app.keyboards.firstMatch.exists, "keyboard stays up")
        XCUIDevice.shared.orientation = .portrait
    }
    /// Compatibility notice: shown before each Start (never Stop); Cancel does not start or save,
    /// even with the toggle on; Start with the toggle off asks again; Start with it on skips the
    /// notice, also after relaunch. Runs at normal and AX5 text, scrolling inside the sheet.
    func testCompatibilityNoticeBeforeStart() {
        for category in ["UICTContentSizeCategoryL", "UICTContentSizeCategoryAccessibilityXXXL"] {
            noticeWorkflow(category)
        }
    }
    private func noticeWorkflow(_ category: String) {
        let size = ["-UIPreferredContentSizeCategoryName", category]
        var app = launch(extraArguments: ["--compatibility-notice", "--reset-compatibility-notice"] + size)
        let startStop = app.buttons["bridge.startStop"]
        XCTAssertTrue(startStop.waitForExistence(timeout: 10), category)
        // The sheet's Form is lazy: swipe up (never down, which dismisses) until the control is realized.
        func revealInSheet(_ element: XCUIElement) {
            for _ in 0..<8 where !(element.exists && element.isHittable) { app.swipeUp() }
            XCTAssertTrue(element.exists && element.isHittable, "\(element) reachable in the sheet at \(category)")
        }
        let toggle = app.switches["compatibility.dontShowAgain"]
        func pressStart(expectNotice: Bool, file: StaticString = #filePath, line: UInt = #line) {
            reveal(startStop, in: app); startStop.tap()
            if expectNotice {
                XCTAssertTrue(app.buttons["compatibility.start"].waitForExistence(timeout: 5), "notice before Start (\(category))", file: file, line: line)
                revealInSheet(app.descendants(matching: .any).matching(identifier: "compatibility.link").firstMatch)
                revealInSheet(toggle)
                XCTAssertEqual(toggle.value as? String, "0", "Don't show again starts off", file: file, line: line)
            } else {
                XCTAssertFalse(app.buttons["compatibility.start"].waitForExistence(timeout: 2), "no notice (\(category))", file: file, line: line)
            }
        }
        func switchOn() {
            (toggle.switches.firstMatch.exists ? toggle.switches.firstMatch : toggle).tap()
            XCTAssertEqual(toggle.value as? String, "1", "Don't show again is on")
        }
        func stopBridge() {
            XCTAssertTrue(app.buttons["Stop Bridge"].waitForExistence(timeout: 10), "bridge started (\(category))")
            reveal(startStop, in: app); startStop.tap()
            XCTAssertFalse(app.buttons["compatibility.start"].waitForExistence(timeout: 2), "Stop never shows the notice")
            XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 10), "bridge stopped")
        }
        // Cancel with the toggle off, then with it on: neither starts nor saves.
        pressStart(expectNotice: true)
        app.buttons["compatibility.cancel"].tap()
        XCTAssertFalse(app.buttons["Stop Bridge"].waitForExistence(timeout: 2), "Cancel does not start")
        pressStart(expectNotice: true)
        switchOn()
        app.buttons["compatibility.cancel"].tap()
        XCTAssertFalse(app.buttons["Stop Bridge"].waitForExistence(timeout: 2), "Cancel does not start")
        // Start with the toggle off: starts, and the notice returns (still off: Cancel saved nothing).
        pressStart(expectNotice: true)
        app.buttons["compatibility.start"].tap()
        stopBridge()
        // Start with the toggle on: starts, and later Starts skip the notice.
        pressStart(expectNotice: true)
        switchOn()
        app.buttons["compatibility.start"].tap()
        stopBridge()
        pressStart(expectNotice: false)
        stopBridge()
        // The choice persists across launches; the link is at the bottom of Settings.
        app.terminate()
        app = launch(extraArguments: ["--compatibility-notice"] + size)
        let relaunched = app.buttons["bridge.startStop"]
        XCTAssertTrue(relaunched.waitForExistence(timeout: 10))
        reveal(relaunched, in: app); relaunched.tap()
        XCTAssertFalse(app.buttons["compatibility.start"].waitForExistence(timeout: 2), "no notice after relaunch")
        XCTAssertTrue(app.buttons["Stop Bridge"].waitForExistence(timeout: 10))
        reveal(relaunched, in: app); relaunched.tap()
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 10))
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let link = app.descendants(matching: .any).matching(identifier: "bridge.compatibilityLink").firstMatch
        reveal(link, in: app)
        XCTAssertTrue(link.exists && link.isHittable, "Settings link (\(category))")
        app.terminate()
    }
    /// Demo mode plays bundled sample data through the bridge (counters rise, labelled demo), sends
    /// nothing to an EFB route, and restores the saved route when stopped.
    func testDemoWithSampleData() {
        let app = launch()
        let diagnostics = app.buttons["Diagnostics"]
        reveal(diagnostics, in: app); diagnostics.tap()
        let demo = app.buttons["bridge.demo"]
        reveal(demo, in: app); demo.tap()
        let label = app.staticTexts.matching(NSPredicate(format: "identifier == %@ AND label == %@", "bridge.mode", "Demo: sample data, not forwarded")).firstMatch
        reveal(label, in: app)
        XCTAssertTrue(label.waitForExistence(timeout: 10), "demo is labelled")
        for metric in ["Input", "Traffic"] {
            let element = app.otherElements[metric]
            reveal(element, in: app)
            let rising = NSPredicate(format: "value != %@ AND value != nil", "0")
            XCTAssertEqual(XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: rising, object: element)], timeout: 15), .completed, "\(metric) rises in the demo")
        }
        let stop = app.buttons["bridge.startStop"]
        reveal(stop, in: app); stop.tap()
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 10))
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let output = app.textFields["Output UDP port"]
        reveal(output, in: app)
        XCTAssertEqual(output.value as? String, "14001", "saved route restored after the demo")
    }
    /// Settings > Licenses shows NOTICE (with the GPL section 7 permission) and LICENSE in full, and
    /// links to the source code.
    func testLicensesShowBundledTexts() {
        let app = launch()
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let licenses = app.buttons["bridge.licenses"]
        reveal(licenses, in: app); licenses.tap()
        for (resource, phrase) in [("NOTICE", "section 7"), ("LICENSE", "GNU GENERAL PUBLIC LICENSE")] {
            let row = app.buttons["license.\(resource)"]
            XCTAssertTrue(row.waitForExistence(timeout: 5), resource)
            row.tap()
            let text = app.staticTexts.matching(identifier: "license.text").firstMatch
            XCTAssertTrue(text.waitForExistence(timeout: 5), "\(resource) text")
            XCTAssertTrue(text.label.contains(phrase), "\(resource) contains \(phrase)")
            app.navigationBars.buttons.element(boundBy: 0).tap()
        }
        let source = app.descendants(matching: .any).matching(identifier: "license.source").firstMatch
        XCTAssertTrue(source.waitForExistence(timeout: 5) && source.isHittable, "Source code link")
    }
    func testExpandedSettingsAndSetupInstructions() {
        XCUIDevice.shared.orientation = .portrait
        let app = launch()
        func reveal(_ element: XCUIElement, upwards: Bool = true) {
            self.reveal(element, in: app, upwards: upwards)
        }
        func capture(_ name: String) {
            let shot = XCTAttachment(screenshot: app.screenshot())
            shot.name = name; shot.lifetime = .keepAlways; add(shot)
        }
        let settings = app.buttons["Settings"]
        reveal(settings); settings.tap()
        let replay = app.buttons["Use synthetic replay route"]
        reveal(replay)
        XCTAssertTrue(replay.isEnabled)
        let output = app.textFields["Output UDP port"]
        reveal(output)
        XCTAssertEqual(output.label, "Output UDP port")
        XCTAssertTrue(output.isEnabled)
        capture("Expanded Settings text controls")
        reveal(settings, upwards: false); settings.tap()
        let setup = app.buttons["Setup instructions"]
        reveal(setup); setup.tap()
        let instructions = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Stratux or GDL90")).firstMatch
        reveal(instructions)
        XCTAssertTrue(instructions.exists)
        capture("Expanded setup instructions")
    }

    func testEFBLocationSetsRoute() {
        let app = launch()
        selectOtherDevice(app)
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let output = app.textFields["Output UDP port"]
        reveal(output, in: app)
        XCTAssertEqual(output.value as? String, "4000", "Another device listens on the standard GDL90 port")
        // This device: loopback on the receiver port (14000 in the synthetic route), shared with the EFB.
        let this = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "This device")).firstMatch
        reveal(this, in: app); this.tap()
        reveal(output, in: app)
        XCTAssertEqual(output.value as? String, "14000")
        let destination = app.textFields["Destination IPv4"]
        reveal(destination, in: app)
        XCTAssertEqual(destination.value as? String, "127.0.0.1")
    }

    func testBridgeSessionRequestAndStop() {
        let app = launch()
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        let toggle = app.switches["Bridge Session"]
        reveal(toggle, in: app)
        XCTAssertTrue(toggle.exists)
        if !toggle.isEnabled {
            XCTAssertEqual(toggle.value as? String, "0")
            let start = app.buttons["bridge.startStop"]
            reveal(start, in: app, upwards: false); start.tap()
            XCTAssertTrue(app.buttons["Stop Bridge"].waitForExistence(timeout: 5))
            start.tap()
            XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
            return
        }
        // SwiftUI exposes the whole labelled row as a Switch. Tap the actual trailing control,
        // rather than the row centre or a width-dependent percentage of the row.
        toggle.coordinate(withNormalizedOffset: CGVector(dx: 1, dy: 0.5))
            .withOffset(CGVector(dx: -20, dy: 0)).tap()
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == '1'"), object: toggle)
        XCTAssertEqual(XCTWaiter.wait(for: [enabled], timeout: 5), .completed, "Enable the session switch before Start")
        let start = app.buttons["bridge.startStop"]
        reveal(start, in: app, upwards: false); start.tap()
        let active = app.staticTexts.matching(NSPredicate(format: "identifier == %@ AND label == %@", "bridge.mode", "Bridge Session")).firstMatch
        let unavailable = app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Bridge Session unavailable'")).firstMatch
        // The simulator may reject continued processing. Either outcome must be clear and stoppable.
        let launched = active.waitForExistence(timeout: 10)
        if !launched { XCTAssertTrue(unavailable.waitForExistence(timeout: 5)) }
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = launched ? "Bridge Session accepted" : "Bridge Session unavailable"
        shot.lifetime = .keepAlways; add(shot)
        if launched {
            app.buttons["bridge.startStop"].tap()
            XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
        }
    }

    func testSyntheticUDPRoundTrip() {
        // Fixtures generated by the repository's gdl90.py; no Swift decoder.
        let frames = [Data(base64Encoded: "fgCBAdIEAAB9Xa9+")!, Data(base64Encoded: "fgd9Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1dfV59XX1efV19Xn1d98x+")!]
        let sink = udpSocket()
        let sender = udpSocket()
        XCTAssertGreaterThanOrEqual(sink, 0); XCTAssertGreaterThanOrEqual(sender, 0)
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = UInt16(14001).bigEndian
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(sink, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        XCTAssertEqual(bound, 0, "EFB test sink must own 14001 exclusively")
        let app = launch(); app.buttons["bridge.startStop"].tap()
        XCTAssertTrue(app.staticTexts["Waiting for data"].waitForExistence(timeout: 5))
        address.sin_port = UInt16(14000).bigEndian
        for frame in frames {
            let sent = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { destination in
                    frame.withUnsafeBytes { sendto(sender, $0.baseAddress, $0.count, 0, destination, socklen_t(MemoryLayout<sockaddr_in>.size)) }
                }
            }
            XCTAssertEqual(sent, frame.count)
            var ready = pollfd(fd: sink, events: Int16(POLLIN), revents: 0)
            XCTAssertEqual(poll(&ready, 1, 3000), 1, "Bridge must forward the UDP frame")
            var bytes = [UInt8](repeating: 0, count: 65536)
            // Only read after poll succeeds, so a failure cannot hang the test runner.
            if ready.revents & Int16(POLLIN) != 0 {
                let n = recv(sink, &bytes, bytes.count, 0)
                XCTAssertGreaterThan(n, 0)
                if n > 0 { XCTAssertEqual(Data(bytes.prefix(n)), frame) }
            }
        }
        // Keep heartbeat fresh while accessibility queries/screenshots run slowly on a cold simulator.
        let pulseQueue = DispatchQueue(label: "bridge.ui-test.heartbeat")
        let pulse = timer(on: pulseQueue)
        let inputAddress = address
        pulse.schedule(deadline: .now(), repeating: 0.5)
        pulse.setEventHandler {
            var destination = inputAddress
            withUnsafePointer(to: &destination) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { target in
                    _ = frames[0].withUnsafeBytes { sendto(sender, $0.baseAddress, $0.count, 0, target, socklen_t(MemoryLayout<sockaddr_in>.size)) }
                }
            }
        }
        pulse.resume()
        XCTAssertTrue(app.staticTexts["Receiving • forwarding"].waitForExistence(timeout: 5))
        let gps = app.descendants(matching: .any)["bridge.gps"]
        XCTAssertTrue(gps.waitForExistence(timeout: 5))
        XCTAssertEqual(gps.value as? String, "Receiver fix reported")
        let shot = XCTAttachment(screenshot: app.screenshot()); shot.name = "UDP replay"; shot.lifetime = .keepAlways; add(shot)
        pulse.cancel(); pulseQueue.sync {}
        XCTAssertTrue(app.staticTexts["Receiver silent"].waitForExistence(timeout: 8))
        XCTAssertEqual(gps.value as? String, "No fresh receiver fix")
        // A fresh heartbeat after a gap restores forwarding and GPS, with no new decoder.
        let recovered = timer(on: pulseQueue)
        recovered.schedule(deadline: .now(), repeating: 0.5)
        recovered.setEventHandler {
            var destination = inputAddress
            withUnsafePointer(to: &destination) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { target in
                _ = frames[0].withUnsafeBytes { sendto(sender, $0.baseAddress, $0.count, 0, target, socklen_t(MemoryLayout<sockaddr_in>.size)) }
            } }
        }
        recovered.resume()
        XCTAssertTrue(app.staticTexts["Receiving • forwarding"].waitForExistence(timeout: 5))
        XCTAssertEqual(gps.value as? String, "Receiver fix reported")
        app.buttons["bridge.startStop"].tap()
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
    }

    func testStartIntentUsesSharedBridge() {
        let app = launch(extraArguments: ["--ui-intents=start"])
        let active = app.staticTexts.matching(NSPredicate(format: "identifier == %@ AND label == %@", "bridge.mode", "Bridge Session")).firstMatch
        let unavailable = app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Bridge Session unavailable'")).firstMatch
        // The simulator may reject continued processing; either outcome must be explicit and stoppable.
        let launched = active.waitForExistence(timeout: 10)
        if !launched { XCTAssertTrue(unavailable.waitForExistence(timeout: 5)) }
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = launched ? "Start intent: session accepted" : "Start intent: session unavailable"
        shot.lifetime = .keepAlways; add(shot)
        let start = app.buttons["bridge.startStop"]
        reveal(start, in: app, upwards: false)
        if launched { start.tap() }
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
    }
    func testStopIntentCancelsRequestedSession() {
        // Stop arrives while the session request is still pending; the late scheduler reply must be ignored.
        let app = launch(extraArguments: ["--ui-intents=start,stop"])
        let stopped = app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Stopped by shortcut'")).firstMatch
        XCTAssertTrue(stopped.waitForExistence(timeout: 10))
        let start = app.buttons["bridge.startStop"]
        reveal(start, in: app, upwards: false)
        XCTAssertEqual(start.label, "Start Bridge")
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "identifier == %@ AND label == %@", "bridge.mode", "Bridge Session")).firstMatch.exists)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Bridge Session unavailable'")).firstMatch.waitForExistence(timeout: 3))
        XCTAssertEqual(intentOutcomes(app), ["Requesting a Bridge Session.", "Bridge stopping."])
    }
    func testDuplicateIntentsReportActualState() {
        // A second Start while the session request is pending must not claim the bridge is running.
        var app = launch(extraArguments: ["--ui-intents=start,start,stop"])
        XCTAssertEqual(intentOutcomes(app), ["Requesting a Bridge Session.", "A Bridge Session request is already pending.", "Bridge stopping."])
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS 'Stopped by shortcut'")).firstMatch.waitForExistence(timeout: 5))
        app.terminate()
        // Running, then Stopping: Start and Stop report the transition instead of acting twice.
        app = launch(extraArguments: ["--ui-intents=foreground,start,stop,start,stop"])
        XCTAssertEqual(intentOutcomes(app), ["Bridge already running.", "Bridge stopping.",
                                             "Bridge is stopping. Start again after it stops.", "Bridge is already stopping."])
        let start = app.buttons["bridge.startStop"]
        reveal(start, in: app, upwards: false)
        XCTAssertTrue(app.buttons["Start Bridge"].waitForExistence(timeout: 5))
    }
    /// Intent outcomes recorded by the DEBUG UI-test build, in order.
    private func intentOutcomes(_ app: XCUIApplication) -> [String] {
        let outcomes = app.staticTexts["debug.intentOutcomes"]
        reveal(outcomes, in: app, upwards: false)
        guard outcomes.waitForExistence(timeout: 5) else { return [] }
        return outcomes.label.components(separatedBy: " | ")
    }
    /// VoiceOver-relevant audit categories must be clean. Visual categories (contrast, Dynamic
    /// Type, clipping) are attached for review; they are reported as open findings, not passes.
    func testAccessibilityAudit() throws {
        let app = launch()
        XCTAssertTrue(app.buttons["bridge.startStop"].waitForExistence(timeout: 10))
        let visual: XCUIAccessibilityAuditType = [.contrast, .dynamicType, .textClipped]
        var voiceOver: [String] = [], review: [String] = [], screen = "Main"
        let collect: (XCUIAccessibilityAuditIssue) -> Bool = { issue in
            let element = issue.element.map { "\($0.elementType.rawValue) '\($0.label)' \($0.frame)" } ?? "no element"
            let line = "\(screen): \(issue.auditType.rawValue) \(element): \(issue.compactDescription) | \(issue.detailedDescription)"
            if visual.contains(issue.auditType) { review.append(line) } else { voiceOver.append(line) }
            return true
        }
        func audit() throws {
            let before = voiceOver.count + review.count
            let shot = XCTAttachment(screenshot: app.screenshot()); shot.name = "Audited: \(screen)"
            shot.lifetime = .keepAlways; add(shot)
            try app.performAccessibilityAudit(for: .all, collect)
            guard voiceOver.count + review.count > before else { return }
            // Evidence at the reported state, not only before the audit: screen and full hierarchy.
            let after = XCTAttachment(screenshot: app.screenshot()); after.name = "After audit issues: \(screen)"
            after.lifetime = .keepAlways; add(after)
            let tree = XCTAttachment(string: app.debugDescription); tree.name = "Hierarchy after audit issues: \(screen)"
            tree.lifetime = .keepAlways; add(tree)
        }
        try audit()
        let settings = app.buttons["Settings"]
        reveal(settings, in: app); settings.tap()
        // Audit the open settings, scrolled clear of the large title: text half-hidden behind it is
        // reported as "potentially inaccessible" with no element, depending only on the scroll position.
        reveal(app.switches["bridge.restoreCompatibility"], in: app, upwards: false)
        screen = "Settings"
        try audit()
        let report = XCTAttachment(string: (["VoiceOver-relevant:"] + voiceOver + ["Visual, for review:"] + review).joined(separator: "\n"))
        report.name = "Accessibility audit issues"; report.lifetime = .keepAlways; add(report)
        XCTAssertTrue(voiceOver.isEmpty, voiceOver.joined(separator: "\n"))
    }
}
