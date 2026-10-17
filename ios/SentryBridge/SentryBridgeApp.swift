import SwiftUI

@main struct SentryBridgeApp: App {
    @StateObject private var model = BridgeModel.shared
    @Environment(\.scenePhase) private var scenePhase
    var body: some Scene {
        WindowGroup {
            BridgeView(model: model)
                .onChange(of: scenePhase) { _, phase in
                    if phase == .background { model.backgrounded() }
                    else if phase == .active { model.foregrounded() }
                }
                #if DEBUG
                // UI tests run the real intent types; Shortcuts itself cannot be driven from XCTest.
                .task {
                    let arguments = ProcessInfo.processInfo.arguments
                    guard arguments.contains("--ui-testing") else { return }
                    // --ui-intents=start,stop,... runs the real intent types in order; "foreground" starts
                    // a foreground bridge directly so a test can reach the running and Stopping states.
                    guard let steps = arguments.first(where: { $0.hasPrefix("--ui-intents=") })?
                        .dropFirst("--ui-intents=".count).split(separator: ",") else { return }
                    for step in steps {
                        switch step {
                        case "start": _ = try? await StartBridgeSessionIntent().perform()
                        case "stop": _ = try? await StopBridgeIntent().perform()
                        case "foreground": model.settings.experiment = false; model.start()
                        default: break
                        }
                    }
                }
                #endif
        }
    }
}
