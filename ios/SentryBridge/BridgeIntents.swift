import AppIntents

// Intents drive the app's single BridgeModel; they never create a second model or worker.
// The type keeps its name in both builds so saved Shortcuts keep working.
struct StartBridgeSessionIntent: AppIntent {
    #if LITE
    static let title: LocalizedStringResource = "Start Bridge Session"
    static let description = IntentDescription("Opens Sentry Bridge and starts the saved route. Uses a Bridge Session on iOS/iPadOS 26 or later.")
    #else
    static let title: LocalizedStringResource = "Start Bridge"
    static let description = IntentDescription("Opens Sentry Bridge and starts the saved route. Background audio keeps it forwarding in the background.")
    #endif
    // Continued processing must be requested by the app in the foreground.
    static let openAppWhenRun = true
    @MainActor func perform() async throws -> some IntentResult & ProvidesDialog {
        .result(dialog: "\(BridgeModel.shared.startFromIntent())")
    }
}

struct StopBridgeIntent: AppIntent {
    static let title: LocalizedStringResource = "Stop Bridge"
    static let description = IntentDescription("Stops forwarding and ends any Bridge Session.")
    @MainActor func perform() async throws -> some IntentResult & ProvidesDialog {
        .result(dialog: "\(BridgeModel.shared.stopFromIntent())")
    }
}

struct BridgeShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        // AppIntents metadata needs literal titles.
        #if LITE
        AppShortcut(intent: StartBridgeSessionIntent(), phrases: ["Start \(.applicationName) session", "Start \(.applicationName)"],
                    shortTitle: "Start Bridge Session", systemImageName: "play.fill")
        #else
        AppShortcut(intent: StartBridgeSessionIntent(), phrases: ["Start \(.applicationName) session", "Start \(.applicationName)"],
                    shortTitle: "Start Bridge", systemImageName: "play.fill")
        #endif
        AppShortcut(intent: StopBridgeIntent(), phrases: ["Stop \(.applicationName)"],
                    shortTitle: "Stop Bridge", systemImageName: "stop.fill")
    }
}
