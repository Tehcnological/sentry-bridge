import SwiftUI

/// The licence texts shipped in the app bundle, shown in full: NOTICE (including the GPL section 7
/// app-store permission) and LICENSE (GPL-3.0). The iOS app contains project code only; the Python,
/// Kotlin and dump978 notices cover other platforms' components.
struct LicensesView: View {
    static let files: [(title: String, resource: String, ext: String?)] = [
        ("NOTICE", "NOTICE", "txt"), ("LICENSE (GPL-3.0)", "LICENSE", nil)]
    static let source = URL(string: "https://github.com/Tehcnological/sentry-bridge")!
    var body: some View {
        List {
            Section {
                ForEach(Self.files, id: \.resource) { file in
                    NavigationLink(file.title) { LicenseText(title: file.title, resource: file.resource, ext: file.ext) }
                        .accessibilityIdentifier("license.\(file.resource)")
                }
            }
            Section {
                Link("Source code", destination: Self.source).accessibilityIdentifier("license.source")
            }
        }
        .navigationTitle("Licenses")
    }
}

private struct LicenseText: View {
    let title: String, resource: String, ext: String?
    var body: some View {
        ScrollView {
            Text(text).font(.footnote.monospaced()).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading).padding()
                .accessibilityIdentifier("license.text")
        }
        .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
    }
    private var text: String {
        Bundle.main.url(forResource: resource, withExtension: ext).flatMap { try? String(contentsOf: $0, encoding: .utf8) }
            ?? "\(title) is missing from this build."
    }
}
