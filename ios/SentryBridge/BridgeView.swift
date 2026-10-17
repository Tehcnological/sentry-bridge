import SwiftUI

struct BridgeView: View {
    @ObservedObject var model: BridgeModel
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.dynamicTypeSize) private var typeSize
    // Match the main GUI palette while leaving light surfaces native and following System.
    private let accent = Color(red: 18 / 255.0, green: 109 / 255.0, blue: 196 / 255.0)
    // Text/link tint needs stronger contrast on navy rows; filled buttons retain the original blue.
    private var controlTint: Color { colorScheme == .dark ? Color(red: 82 / 255.0, green: 165 / 255.0, blue: 237 / 255.0) : accent }
    private var linkInk: Color { colorScheme == .dark ? controlTint : ink }
    private var background: Color { colorScheme == .dark ? Color(red: 11 / 255.0, green: 23 / 255.0, blue: 39 / 255.0) : Color(uiColor: .systemGroupedBackground) }
    private var surface: Color { colorScheme == .dark ? Color(red: 19 / 255.0, green: 34 / 255.0, blue: 57 / 255.0) : Color(uiColor: .secondarySystemGroupedBackground) }
    private var ink: Color { colorScheme == .dark ? Color(red: 230 / 255.0, green: 237 / 255.0, blue: 247 / 255.0) : .primary }
    private var secondary: Color { colorScheme == .dark ? Color(red: 171 / 255.0, green: 187 / 255.0, blue: 208 / 255.0) : Color(red: 102 / 255.0, green: 102 / 255.0, blue: 102 / 255.0) }
    private var warning: Color { colorScheme == .dark ? Color(red: 237 / 255.0, green: 195 / 255.0, blue: 145 / 255.0) : Color(red: 156 / 255.0, green: 82 / 255.0, blue: 0) }
    @FocusState private var focusedField: String?
    @Namespace private var fieldLabels
    @State private var showDiagnostics = false
    @State private var showSettings = false
    // Compatibility notice before each Start (never Stop); hidden only after Start with the toggle on.
    @AppStorage(BridgeView.noticeHiddenKey) private var noticeHidden = false
    @State private var showCompatibility = false
    @State private var dontShowAgain = false
    static let noticeHiddenKey = "bridge.compatibilityNoticeHidden"
    private static let compatibilityURL = URL(string: "https://github.com/Tehcnological/sentry-bridge/blob/master/docs/Compatibility.md#iphone-and-ipad")!
    /// Other UI tests start the bridge directly; the notice test opts in with --compatibility-notice.
    private var noticeSkippedForTests: Bool {
        #if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        return arguments.contains("--ui-testing") && !arguments.contains("--compatibility-notice")
        #else
        return false
        #endif
    }
    private func startPressed() {
        if model.busy { model.stop(); return }
        if noticeHidden || noticeSkippedForTests { model.start(); return }
        dontShowAgain = false; showCompatibility = true
    }
    var body: some View {
        NavigationStack {
            GeometryReader { geometry in
                let wide = twoColumns(geometry.size)
                Group {
                    if wide {
                        HStack(spacing: 0) {
                            styledForm { statusSections }
                                .frame(width: min(380, max(320, geometry.size.width * 0.36)))
                            styledForm { setupSections }
                        }
                    } else {
                        styledForm { statusSections; setupSections }
                    }
                }
                #if DEBUG
                .overlay(alignment: .topLeading) {
                    // UI tests read the layout decision and the size it was made from.
                    if ProcessInfo.processInfo.arguments.contains("--ui-testing") {
                        Color.clear.frame(width: 1, height: 1).accessibilityElement()
                            .accessibilityIdentifier("bridge.layout")
                            .accessibilityValue((wide ? "two " : "one ") + String(Int(geometry.size.width)) + "x" + String(Int(geometry.size.height)))
                    }
                }
                #endif
                // Switching layouts rebuilds the setup column; return focus to the field being
                // edited (the keyboard then scrolls it into view).
                .onChange(of: wide) { _, _ in
                    let editing = focusedField
                    DispatchQueue.main.async { focusedField = editing }
                }
            }
            .background(background.ignoresSafeArea())
            .navigationTitle("Sentry Bridge")
            .navigationBarTitleDisplayMode(typeSize.isAccessibilitySize ? .inline : .automatic)
            .tint(controlTint)
            .sheet(isPresented: $showCompatibility) { compatibilitySheet }
            .sheet(isPresented: $model.discoveryVisible, onDismiss: { model.exitDiscovery() }) {
                NavigationStack {
                    List {
                      Section {
                        if model.findingEFBs { HStack { ProgressView(); Text("Listening for EFBs") } }
                        if let message = model.discoveryMessage { Text(message) }
                        ForEach(model.discoveredEFBs) { entry in
                            (typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12)) : AnyLayout(HStackLayout())) {
                                VStack(alignment: .leading) {
                                    Text(entry.app.isEmpty ? "?" : entry.app)
                                    Text("\(entry.address):\(entry.port)").font(.caption)
                                }
                                if !typeSize.isAccessibilitySize { Spacer() }
                                Button("Use") { model.useEFB(entry) }.foregroundStyle(linkInk).disabled(model.busy)
                                    .accessibilityLabel("Use \(entry.app.isEmpty ? "EFB" : entry.app) at \(entry.address)")
                                    .accessibilityIdentifier("bridge.useEFB")
                            }
                        }
                        Text("Receive only, about 6 seconds. Requires Local Network access. Broadcast announcements also require approved multicast signing on physical devices. An empty list does not prove that access is allowed. Enter an address manually if discovery is unavailable.").font(.caption).foregroundStyle(secondary)
                        if model.busy { Text("Stop Bridge to use an address.").font(.caption) }
                      }.listRowBackground(surface)
                    }
                    .foregroundStyle(ink)
                    .scrollContentBackground(colorScheme == .dark ? .hidden : .visible)
                    .background(background.ignoresSafeArea())
                    .navigationTitle("Find EFB")
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Exit") { model.exitDiscovery() }.foregroundStyle(linkInk) } }
                    .tint(controlTint)
                }
            }
        }
    }
    private func metric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading) { Text(value).font(.title2.monospacedDigit().bold()); Text(label).font(.caption).foregroundStyle(secondary) }
            .frame(maxWidth: .infinity, alignment: .leading).accessibilityElement(children: .ignore)
            .accessibilityLabel(label).accessibilityValue(value)
    }
    /// Two columns in landscape at regular width; text larger than xLarge keeps one column so the
    /// 320-380 pt status column never truncates its rows.
    private func twoColumns(_ size: CGSize) -> Bool {
        typeSize <= .xLarge && size.width >= 768 && size.width > size.height
    }
    @ViewBuilder private var statusSections: some View {
        Section {
            HStack(alignment: .top, spacing: 16) {
                if !typeSize.isAccessibilitySize {
                    Image(systemName: "antenna.radiowaves.left.and.right")
                        .font(.largeTitle).foregroundStyle(accent).accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text(model.state).font(.title3.bold()).accessibilityIdentifier("bridge.status")
                    Text(model.demo ? "Demo: sample data, not forwarded" : model.sessionActive ? "Bridge Session" : "Foreground bridge").accessibilityIdentifier("bridge.mode")
                        .font(.subheadline).foregroundStyle(secondary)
                }
            }.padding(.vertical, 8)
            Button { startPressed() } label: {
                Label(model.busy ? "Stop Bridge" : "Start Bridge", systemImage: model.busy ? "stop.fill" : "play.fill")
                    .font(.headline).foregroundStyle(.white).frame(maxWidth: .infinity).padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent).tint(model.busy ? .red : accent)
            .disabled(model.state == "Stopping").accessibilityIdentifier("bridge.startStop")
            if let message = model.message { Label(message, systemImage: "exclamationmark.triangle").font(.callout) }
            #if DEBUG
            if !model.intentOutcomes.isEmpty {
                Text(model.intentOutcomes.joined(separator: " | ")).font(.caption2).accessibilityIdentifier("debug.intentOutcomes")
            }
            #endif
        }.listRowBackground(surface)
        Section("Live status") {
            statusRow("Receiver", !model.busy ? "Bridge stopped" : model.snapshot.age < 0 ? "No data yet" : String(format: "%.1f s since data", model.snapshot.age))
            statusRow("GPS", model.busy && model.snapshot.gps ? "Receiver fix reported" : "No fresh receiver fix")
                .accessibilityElement(children: .combine).accessibilityIdentifier("bridge.gps")
                .accessibilityLabel("GPS").accessibilityValue(model.busy && model.snapshot.gps ? "Receiver fix reported" : "No fresh receiver fix")
            (typeSize.isAccessibilitySize ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12)) : AnyLayout(HStackLayout())) {
                metric("Input", "\(model.snapshot.received)")
                metric("Sent", "\(model.snapshot.forwarded)")
                metric("Traffic", "\(model.snapshot.traffic)")
            }
            DisclosureGroup("Transport details") {
                Text("Sent counts indicate UDP transport, not EFB receipt. Weather frames: \(model.snapshot.weather); display remains unverified.").font(.caption)
            }
            if model.snapshot.mode == 2 { Label(model.settings.restoreCompatibility ?? true ? "Encrypted receiver stream. Compatibility Mode is requested automatically." : "Encrypted receiver stream. Turn on Restore Compatibility Mode in Settings.", systemImage: "lock.trianglebadge.exclamationmark") }
            if let request = model.snapshot.compatibilityRequest { Text(request).font(.caption).accessibilityIdentifier("bridge.compatibilityRequest") }
            // Rebind results stay visible here; core log messages can replace the status detail.
            if let rebind = model.snapshot.inputRebind { Text(rebind).font(.caption).accessibilityIdentifier("bridge.inputRebind") }
            if model.snapshot.sendErrors > 0 { Text("Output errors: \(model.snapshot.sendErrors). Check permission, WiFi and routing.").foregroundStyle(warning) }
        }.listRowBackground(surface)
    }
    @ViewBuilder private var setupSections: some View {
        Section("EFB setup") {
            if typeSize.isAccessibilitySize { locationPicker.pickerStyle(.inline) }
            else { locationPicker.pickerStyle(.segmented) }
            if !BridgeSettings.isLoopback(model.settings.destination) {
                field("Other device IPv4", $model.settings.destination)
                Button("Find EFB") { model.findEFBs() }.foregroundStyle(linkInk)
                    .accessibilityIdentifier("bridge.findEFB")
            }
            DisclosureGroup("Setup instructions") {
            Text("Join Sentry WiFi and disable Power Savings in the receiver settings. With Restore Compatibility Mode on, the bridge requests Compatibility Mode whenever the receiver stream starts encrypted.").font(.callout)
            Text("In the EFB, choose a Stratux or GDL90 device. On this device the bridge shares UDP 4000 with the EFB and reaches one EFB at a time; close other EFBs. For an EFB on another device, enter its address or use Find EFB; it listens on UDP 4000.").font(.callout)
            }
        }.listRowBackground(surface)
        Section {
          DisclosureGroup("Settings", isExpanded: $showSettings) {
            Text("Routing • apply with Start").font(.headline)
            field("Sentry IPv4", $model.settings.source)
            field("Input UDP port", $model.settings.inputPort)
            field("Destination IPv4", $model.settings.destination)
            field("Output UDP port", $model.settings.outputPort)
            field("EFB discovery UDP port", Binding(get: { model.settings.discoveryPort ?? "63093" }, set: { model.settings.discoveryPort = $0 }))
            Text("127.0.0.1 routes to this device. Output on the input port shares that port with the EFB; any other local output port must be free. Unicast only. Input port edits only change this app, not Sentry. Broadcast reception requires an approved multicast entitlement on a physical device.")
                .font(.caption).foregroundStyle(secondary)
            #if DEBUG
            Button("Use synthetic replay route") { model.syntheticSettings() }.foregroundStyle(linkInk).disabled(model.busy)
            #endif
            Toggle("Restore Compatibility Mode", isOn: Binding(get: { model.settings.restoreCompatibility ?? true }, set: { model.settings.restoreCompatibility = $0 }))
                .disabled(model.busy).accessibilityIdentifier("bridge.restoreCompatibility")
            Text("Sends the receiver's Compatibility Mode request when its stream starts encrypted: up to 3 tries, 20 s apart. Receiver HTTP port 80.").font(.caption).foregroundStyle(secondary)
            Text("Session options").font(.headline)
            Toggle("Bridge Session", isOn: $model.settings.experiment)
                .disabled(model.busy)
            if model.settings.experiment {
                Picker("Session length", selection: $model.settings.sessionMinutes) {
                    ForEach(BridgeSettings.sessionLengths, id: \.self) { Text("\($0) minutes").tag($0) }
                }.pickerStyle(.menu).disabled(model.busy).accessibilityIdentifier("bridge.sessionLength")
                Text("Progress measures your selected monitoring period, including quiet intervals. The system can cancel or expire it.").font(.caption).foregroundStyle(secondary)
            } else {
                Text("Keep this app visible. Hiding it stops forwarding. iPad windowed use is a fallback to measure.").font(.caption).foregroundStyle(secondary)
            }
            Toggle("Keep screen awake while visible", isOn: $model.settings.keepAwake).disabled(model.busy)
            NavigationLink("Licenses") { LicensesView() }.foregroundStyle(linkInk).accessibilityIdentifier("bridge.licenses")
            Link("Compatibility and limitations", destination: Self.compatibilityURL).foregroundStyle(linkInk)
                .accessibilityIdentifier("bridge.compatibilityLink")
          }
        }.listRowBackground(surface)
        Section {
            DisclosureGroup("Diagnostics", isExpanded: $showDiagnostics) {
                if showDiagnostics {
                    Button("Demo with sample data") { model.startDemo() }.foregroundStyle(linkInk).disabled(model.busy)
                        .accessibilityIdentifier("bridge.demo")
                    Text("Plays recorded sample data in this app only; nothing is sent to an EFB. Stop ends the demo.")
                        .font(.caption).foregroundStyle(secondary)
                    statusRow("Heartbeat frames", "\(model.snapshot.heartbeat)")
                    statusRow("CRC failures", "\(model.snapshot.crcBad)")
                    statusRow("Longest reception gap", String(format: "%.1f s", model.snapshot.maxGap))
                    Text(model.snapshot.detail).font(.caption).textSelection(.enabled)
                    Text("Local Network: \(model.localNetworkDenied ? "Denied" : "Not proven by packet counts")").font(.caption)
                    ShareLink(item: model.diagnosticReport) { Label("Share session diagnostics", systemImage: "square.and.arrow.up") }.foregroundStyle(linkInk)
                    Text("Latest 512 events and 30-second counter samples; no raw packets or positions retained.").font(.caption).foregroundStyle(secondary)
                }
            }
            if showDiagnostics { Text("App \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown") • core \(String(cString: sbVersion()))").font(.caption) }
        }.listRowBackground(surface)
    }
    private var compatibilitySheet: some View {
        NavigationStack {
            Form {
                Text("Check what Sentry Bridge provides on this platform before relying on it in flight.")
                Text("Supplemental awareness only. Not for primary navigation or collision avoidance.").font(.callout)
                Link("Read compatibility and limitations", destination: Self.compatibilityURL).foregroundStyle(linkInk)
                    .accessibilityIdentifier("compatibility.link")
                Text("This link is also at the bottom of Settings.").font(.callout).foregroundStyle(secondary)
                Toggle("Don't show again", isOn: $dontShowAgain).accessibilityIdentifier("compatibility.dontShowAgain")
            }
            .foregroundStyle(ink)
            .scrollContentBackground(colorScheme == .dark ? .hidden : .visible)
            .background(background.ignoresSafeArea())
            .navigationTitle("Compatibility and limitations")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { showCompatibility = false }.accessibilityIdentifier("compatibility.cancel")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Start") {
                        // Saved only when Start is pressed with the toggle on; Cancel never saves.
                        if dontShowAgain { noticeHidden = true }
                        showCompatibility = false
                        model.start()
                    }.accessibilityIdentifier("compatibility.start")
                }
            }
        }
        .tint(controlTint)
        .accessibilityIdentifier("compatibility.sheet")
    }
    private func styledForm<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        Form { content() }
            .scrollDismissesKeyboard(.interactively)
            .foregroundStyle(ink)
            .scrollContentBackground(colorScheme == .dark ? .hidden : .visible)
    }
    private var locationPicker: some View {
        Picker("EFB runs", selection: Binding(get: { BridgeSettings.isLoopback(model.settings.destination) ? "local" : "remote" }, set: { value in
            if value == "local" { model.settings.useThisDevice() } else { model.settings.useOtherDevice() }
        })) {
            Text("This device").tag("local")
            Text("Other device").tag("remote")
        }.disabled(model.busy)
    }
    @ViewBuilder private func statusRow(_ label: String, _ value: String) -> some View {
        if typeSize.isAccessibilitySize {
            VStack(alignment: .leading) { Text(label).foregroundStyle(secondary); Text(value) }
                .accessibilityElement(children: .combine)
        } else { LabeledContent(label, value: value) }
    }
    private func field(_ label: String, _ value: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
          // Pair the visible caption with its field so VoiceOver presents one labelled control.
          Text(label).font(.caption).foregroundStyle(secondary)
            .accessibilityLabeledPair(role: .label, id: label, in: fieldLabels)
          TextField(label, text: Binding(get: { value.wrappedValue }, set: { updated in
            guard updated != value.wrappedValue else { return }
            value.wrappedValue = updated
        })).keyboardType(.numbersAndPunctuation).textInputAutocapitalization(.never)
            .autocorrectionDisabled().disabled(model.busy)
            .accessibilityLabel(label)
            .accessibilityLabeledPair(role: .content, id: label, in: fieldLabels)
            .focused($focusedField, equals: label)
            .submitLabel(.done)
            .onSubmit { focusedField = nil }
        }
    }
}
