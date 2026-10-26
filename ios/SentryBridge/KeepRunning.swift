import Foundation

/// Which build this is, set by SB_FLAVOR in the Xcode project (tools/build_ios_ipa.py picks it). The release
/// IPA is Sideload: background audio keeps the bridge running. Lite has no silent audio and uses Bridge Session.
enum BuildFlavor: String {
    case sideload = "Sideload", lite = "Lite"
    static var current: BuildFlavor {
        #if LITE
        return .lite
        #else
        return .sideload
        #endif
    }
}

/// How the bridge keeps forwarding while the app is in the background.
enum KeepRunningMethod: String, Codable {
    case backgroundAudio, bridgeSession, appOpen
    static func offered(in flavor: BuildFlavor) -> [KeepRunningMethod] {
        flavor == .sideload ? [.backgroundAudio] : [.bridgeSession, .appOpen]
    }
    var title: String {
        switch self {
        case .backgroundAudio: return "Background audio"
        case .bridgeSession: return "Bridge Session"
        case .appOpen: return "Only while the app is open"
        }
    }
}

/// One answer in the first-run chooser.
struct KeepRunningOption: Identifiable, Equatable {
    let method: KeepRunningMethod, gpsCheck: Bool, recommended: Bool, detail: String
    var id: String { method.rawValue + (gpsCheck ? "+gps" : "") }
    var title: String { method.title + (gpsCheck ? " + GPS check" : "") }
}

/// The saved answer to "how should the bridge keep running", stored apart from the route settings so an
/// unreadable or missing answer only brings back the chooser.
struct KeepRunningChoice: Codable, Equatable {
    /// Raise when the chooser asks something new: a saved choice from an older version shows it again.
    static let currentVersion = 1
    static let storeKey = "bridge.keepRunning.v1"
    var version = currentVersion
    var method: KeepRunningMethod
    var gpsCheck = false

    /// The saved choice, or nil when the chooser must be shown: nothing saved (first launch, or an update
    /// from a build without this setting), unreadable data, an older version, or a method this build lacks.
    static func load(_ data: Data?, flavor: BuildFlavor) -> KeepRunningChoice? {
        guard let data, let choice = try? JSONDecoder().decode(KeepRunningChoice.self, from: data),
              choice.version >= currentVersion, KeepRunningMethod.offered(in: flavor).contains(choice.method) else { return nil }
        return choice
    }
    func encoded() -> Data? { try? JSONEncoder().encode(self) }

    static let notificationsDetail = "Notifications are how Sentry Bridge tells you the bridge stopped or can no longer run in the background, so you hear it before the EFB loses traffic. They are local to this device."
    static let gpsDetail = "The phone's GPS is compared with the Sentry's position; the main screen warns when the Sentry has no fix or the two disagree. The phone's position is only compared, never sent to the EFB. Needs Always location access, asked for when you choose this; iOS shows the location indicator and battery use goes up."

    static func options(for flavor: BuildFlavor) -> [KeepRunningOption] {
        switch flavor {
        case .sideload:
            #if LITE
            return []  // Lite carries no background-audio option.
            #else
            return [
                KeepRunningOption(method: .backgroundAudio, gpsCheck: false, recommended: true, detail: "Plays silence alongside other audio, so iOS keeps the bridge running in the background with no time limit. EFB alerts and music still play. A phone call pauses it; it resumes after the call when iOS allows. Small extra battery use, no permission prompt."),
                KeepRunningOption(method: .backgroundAudio, gpsCheck: true, recommended: false, detail: "Background audio, plus a GPS check. " + gpsDetail + " Location updates also keep the bridge running if the audio stops."),
            ]
            #endif
        case .lite:
            return [
                KeepRunningOption(method: .bridgeSession, gpsCheck: false, recommended: true, detail: "iOS keeps the bridge running in the background for the session length you choose, up to 4 hours, and shows its progress on the Lock Screen. iOS can refuse or end a session; the bridge then runs only while the app is open. No permission prompt."),
                KeepRunningOption(method: .bridgeSession, gpsCheck: true, recommended: false, detail: "Bridge Session, plus a GPS check. " + gpsDetail + " Location updates keep the bridge running past the session length, or when iOS refuses the session."),
                KeepRunningOption(method: .appOpen, gpsCheck: false, recommended: false, detail: "Forwards only while Sentry Bridge is on screen; leaving the app stops it. Least battery use, no permission prompt."),
            ]
        }
    }
    /// The option selected when the chooser opens: the saved one, else the recommended one. A Lite build
    /// updated from settings with Bridge Session turned off starts on "Only while the app is open".
    static func preselected(_ saved: KeepRunningChoice?, flavor: BuildFlavor, legacySession: Bool = true) -> KeepRunningOption {
        let options = options(for: flavor)
        if let saved, let match = options.first(where: { $0.method == saved.method && $0.gpsCheck == saved.gpsCheck }) { return match }
        if flavor == .lite && !legacySession, let open = options.first(where: { $0.method == .appOpen }) { return open }
        return options.first { $0.recommended }!
    }
}

/// A local notification that background running degraded, or the watchdog's.
struct DegradedNotice: Equatable {
    let id: String, title: String, body: String
    static let watchdogID = "bridge.watchdog"
    static let watchdog = DegradedNotice(id: watchdogID, title: "Sentry Bridge stopped forwarding",
                                         body: "iOS paused or closed the app. Open Sentry Bridge to start the bridge again.")
    static func sessionRefused(gpsCheck: Bool) -> DegradedNotice {
        DegradedNotice(id: "bridge.session", title: "Bridge Session refused",
                       body: gpsCheck ? "GPS check keeps the bridge running in the background." : "The bridge runs only while the app is open. Open Sentry Bridge and keep it on screen.")
    }
    static func sessionEnded(gpsCheck: Bool) -> DegradedNotice {
        DegradedNotice(id: "bridge.session", title: "Bridge Session ended",
                       body: gpsCheck ? "GPS check keeps the bridge running in the background." : "Forwarding stopped. Open Sentry Bridge to start it again.")
    }
    static func audioStopped(gpsCheck: Bool) -> DegradedNotice {
        DegradedNotice(id: "bridge.audio", title: "Background audio stopped",
                       body: gpsCheck ? "GPS check keeps the bridge running for now. Open Sentry Bridge to restore background audio." : "iOS may pause the bridge. Open Sentry Bridge to keep it forwarding.")
    }
    static func locationStopped(_ reason: String) -> DegradedNotice {
        DegradedNotice(id: "bridge.location", title: "GPS check stopped", body: reason + " Open Sentry Bridge to check the bridge.")
    }
    static let stoppedWhenHidden = DegradedNotice(id: "bridge.foreground", title: "Forwarding stopped",
                                                  body: "The bridge runs only while the app is open. Open Sentry Bridge to start it again.")
}

/// The decision after iOS refuses a Bridge Session, kept apart from the UI so host tests check it.
enum KeepRunningPolicy {
    struct Fallback: Equatable { let background: Bool, message: String, notice: DegradedNotice }
    /// Never stops the bridge: with GPS check on, location updates keep it running in the background;
    /// otherwise it continues as a foreground bridge with the likely causes named.
    static func sessionRefused(_ error: String, gpsCheck: Bool) -> Fallback {
        if gpsCheck {
            return Fallback(background: true, message: "Bridge Session unavailable (\(error)). GPS check keeps the bridge running in the background.",
                            notice: .sessionRefused(gpsCheck: true))
        }
        return Fallback(background: false, message: "Bridge Session unavailable (\(error)). Forwarding continues only while the app is open. Likely causes: Low Power Mode, Background App Refresh turned off, or iOS limiting background work on this device.",
                        notice: .sessionRefused(gpsCheck: false))
    }
}

/// Posts and removes local notifications; the app uses UNUserNotificationCenter, host tests a recorder.
protocol NoticePosting: AnyObject {
    func post(_ notice: DegradedNotice, after seconds: Double?)
    func remove(_ id: String)
}

/// While forwarding, one notification stays scheduled `lead` seconds ahead and is replaced every `interval`
/// seconds from the worker's status updates. If iOS suspends or ends the app the replacing stops and the
/// notification fires; Stop removes it.
final class Watchdog {
    /// 30 s: six missed replacements before it fires, so a stalled update or a scene change never sets it
    /// off, and the pilot still hears within half a minute, about when EFBs drop traffic as stale.
    static let lead = 30.0, interval = 5.0
    private let poster: NoticePosting
    private var lastScheduled: Double?
    init(poster: NoticePosting) { self.poster = poster }
    /// Called with every status update (about once a second); reschedules when `interval` has passed.
    func tick(now: Double) {
        if let lastScheduled, now - lastScheduled < Self.interval { return }
        poster.post(.watchdog, after: Self.lead); lastScheduled = now
    }
    func stop() { poster.remove(DegradedNotice.watchdogID); lastScheduled = nil }
    var armed: Bool { lastScheduled != nil }
}

/// The phone's latest position, from Core Location. Seconds use sbio_now's monotonic clock.
struct PhoneFix: Equatable { var lat: Double, lon: Double, accuracy: Double, at: Double }
/// The Sentry's GPS as the bridge decodes it: the heartbeat's fix flag and its latest ownship position.
struct SentryFix: Equatable {
    var gps = false, valid = false, lat = 0.0, lon = 0.0, age = -1.0
}

/// Compares the phone's position with the Sentry's and warns on the main screen. HARD RULE: the result is
/// only text. The phone's position never reaches the worker, the EFB, ownship or traffic data.
struct GPSCheck {
    /// 0.5 NM. Both receivers normally agree within tens of metres; samples up to 2 s apart at 150 kt add
    /// about 150 m. Half a mile stays clear of that, yet is a visible shift for pattern traffic. The phone's
    /// own reported accuracy is added so a poor phone fix does not raise the warning.
    var limitM = 926.0
    /// A condition must hold this long before the warning shows (GPS start-up, brief dropouts, a sample
    /// taken mid-turn), and must be gone this long before it clears.
    var holdS = 30.0, clearS = 10.0
    /// Usable fixes: phone sample newer than 10 s with accuracy within 100 m; Sentry ownship newer than 3 s.
    var phoneFreshS = 10.0, phoneAccuracyM = 100.0, sentryFreshS = 3.0
    enum Finding: Equatable { case noPhone, agree(Double), sentryNoFix, disagree(Double) }
    private(set) var finding = Finding.noPhone
    private(set) var warning: String?
    private var pending: (Finding, Double)?, clearing: Double?

    static func distanceM(_ lat1: Double, _ lon1: Double, _ lat2: Double, _ lon2: Double) -> Double {
        let r = 6_371_000.0, p1 = lat1 * .pi / 180, p2 = lat2 * .pi / 180
        let dp = p2 - p1, dl = (lon2 - lon1) * .pi / 180
        let a = sin(dp / 2) * sin(dp / 2) + cos(p1) * cos(p2) * sin(dl / 2) * sin(dl / 2)
        return 2 * r * asin(min(1, sqrt(a)))
    }
    static func nm(_ metres: Double) -> String { String(format: "%.1f NM", metres / 1852) }

    mutating func update(sentry: SentryFix, phone: PhoneFix?, now: Double) {
        guard let phone, now - phone.at <= phoneFreshS, phone.accuracy > 0, phone.accuracy <= phoneAccuracyM else {
            finding = .noPhone; pending = nil; clearing = nil; warning = nil; return
        }
        if !sentry.gps || !sentry.valid || sentry.age < 0 || sentry.age > sentryFreshS {
            finding = .sentryNoFix
        } else {
            let distance = Self.distanceM(sentry.lat, sentry.lon, phone.lat, phone.lon)
            finding = distance > limitM + phone.accuracy ? .disagree(distance) : .agree(distance)
        }
        switch finding {
        case .sentryNoFix, .disagree:
            clearing = nil
            let kind = { (f: Finding) -> Int in if case .sentryNoFix = f { return 1 }; return 2 }
            if pending == nil || kind(pending!.0) != kind(finding) { pending = (finding, now) }
            if now - pending!.1 >= holdS || warning != nil { warning = text(finding) }
        default:
            pending = nil
            if warning != nil {
                if clearing == nil { clearing = now }
                if now - clearing! >= clearS { warning = nil; clearing = nil }
            }
        }
    }
    private func text(_ finding: Finding) -> String {
        switch finding {
        case .sentryNoFix: return "GPS check: the Sentry has no GPS fix, but this phone has one. The EFB gets no position from the Sentry."
        case .disagree(let distance): return "GPS check: the Sentry's position is \(Self.nm(distance)) from this phone's. Check the Sentry's GPS before relying on traffic positions."
        default: return ""
        }
    }
    /// The line shown under Live status while the check runs.
    var status: String {
        switch finding {
        case .noPhone: return "Waiting for this phone's position"
        case .sentryNoFix: return "Sentry has no GPS fix"
        case .agree(let distance): return distance < 100 ? "Positions agree" : "Positions agree within \(Self.nm(distance))"
        case .disagree(let distance): return "Positions \(Self.nm(distance)) apart"
        }
    }
}
