import Foundation
import CoreLocation
import UserNotifications
#if !LITE
import AVFoundation
#endif

/// Local notifications for a stopped or degraded bridge. Time Sensitive where the signing profile has the
/// capability; without it iOS delivers them as normal notifications. No critical alerts.
final class LocalNotifications: NSObject, NoticePosting, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    override init() { super.init(); center.delegate = self }
    func requestPermission(_ done: @escaping (Bool) -> Void) {
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in DispatchQueue.main.async { done(granted) } }
    }
    func post(_ notice: DegradedNotice, after seconds: Double?) {
        let content = UNMutableNotificationContent()
        content.title = notice.title; content.body = notice.body; content.sound = .default
        content.interruptionLevel = .timeSensitive
        let trigger = seconds.map { UNTimeIntervalNotificationTrigger(timeInterval: max(1, $0), repeats: false) }
        // The same identifier replaces a pending request, which is how the watchdog is pushed ahead.
        center.add(UNNotificationRequest(identifier: notice.id, content: content, trigger: trigger))
    }
    func remove(_ id: String) {
        center.removePendingNotificationRequests(withIdentifiers: [id])
        center.removeDeliveredNotifications(withIdentifiers: [id])
    }
    // Shown as banners while the app is open too: the screen may be locked or another app in front.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
}

#if !LITE
/// Sideload builds only: plays silence while forwarding so iOS keeps the app running in the background.
/// .playback with .mixWithOthers never interrupts EFB alerts or music. Phone calls and route changes are
/// logged; playback resumes when iOS allows, and onLost reports a gap it could not end.
final class SilentAudio {
    var onEvent: (String) -> Void = { _ in }
    var onLost: () -> Void = {}
    private var player: AVAudioPlayer?
    private var observers: [NSObjectProtocol] = []
    private var interruptedAt: Double?
    private var retry: DispatchWorkItem?
    private(set) var running = false
    /// One second of 8 kHz 16-bit mono silence as WAV, looped.
    private static let silence: Data = {
        let samples = 8000, bytes = samples * 2
        var data = Data("RIFF".utf8)
        func append<T: FixedWidthInteger>(_ value: T) { withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) } }
        append(UInt32(36 + bytes)); data.append(contentsOf: Array("WAVEfmt ".utf8))
        append(UInt32(16)); append(UInt16(1)); append(UInt16(1)); append(UInt32(8000)); append(UInt32(16000))
        append(UInt16(2)); append(UInt16(16)); data.append(contentsOf: Array("data".utf8)); append(UInt32(bytes))
        data.append(Data(count: bytes))
        return data
    }()
    /// nil when playing, else the reason it could not start.
    func start() -> String? {
        guard !running else { return nil }
        running = true
        let center = NotificationCenter.default, session = AVAudioSession.sharedInstance()
        observers = [
            center.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: .main) { [weak self] in self?.interruption($0) },
            center.addObserver(forName: AVAudioSession.routeChangeNotification, object: session, queue: .main) { [weak self] in self?.routeChanged($0) },
            center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: session, queue: .main) { [weak self] _ in
                self?.onEvent("Background audio: media services reset; restarting"); self?.player = nil; self?.resume(attempt: 0)
            },
        ]
        if let error = play() { stop(); return error }
        return nil
    }
    func stop() {
        guard running else { return }
        running = false; retry?.cancel(); retry = nil; interruptedAt = nil
        observers.forEach(NotificationCenter.default.removeObserver); observers = []
        player?.stop(); player = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
    private func play() -> String? {
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try session.setActive(true)
            if player == nil {
                let player = try AVAudioPlayer(data: Self.silence)
                player.numberOfLoops = -1; self.player = player
            }
            return player?.play() == true ? nil : "playback did not start"
        } catch { return error.localizedDescription }
    }
    private func interruption(_ note: Notification) {
        guard running, let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        if type == .began {
            interruptedAt = sbio_now(); onEvent("Background audio interrupted (phone call or another app's audio)")
        } else {
            let gap = interruptedAt.map { String(format: "%.1f s", sbio_now() - $0) } ?? "unknown length"
            interruptedAt = nil; onEvent("Background audio interruption ended; gap \(gap)")
            resume(attempt: 0)
        }
    }
    private func routeChanged(_ note: Notification) {
        guard running, let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt else { return }
        onEvent("Audio route changed (reason \(raw))")
        if interruptedAt == nil, player?.isPlaying != true { resume(attempt: 0) }
    }
    /// Three tries, 2 s apart: the session can stay unavailable briefly after a call ends.
    private func resume(attempt: Int) {
        retry?.cancel(); retry = nil
        guard running else { return }
        guard let error = play() else { onEvent("Background audio resumed"); return }
        if attempt >= 2 { onEvent("Background audio could not resume: \(error)"); onLost(); return }
        let next = DispatchWorkItem { [weak self] in self?.resume(attempt: attempt + 1) }
        retry = next
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: next)
    }
}
#endif

/// The phone's position for the GPS cross-reference. It is read here and compared in GPSCheck; nothing else uses it.
final class PhoneLocation: NSObject, CLLocationManagerDelegate {
    var onStopped: (String) -> Void = { _ in }
    var onAuthorization: () -> Void = {}
    private let manager = CLLocationManager()
    private(set) var latest: PhoneFix?
    private(set) var running = false
    private var waiting: ((Bool) -> Void)?
    override init() { super.init(); manager.delegate = self }
    #if DEBUG
    // UI tests exercise real model transitions without prompting or using the simulator's location.
    private var testAuthorization: CLAuthorizationStatus?
    func useTestAuthorization(_ value: CLAuthorizationStatus) {
        testAuthorization = value; onAuthorization()
    }
    #endif
    var status: CLAuthorizationStatus {
        #if DEBUG
        if let testAuthorization { return testAuthorization }
        #endif
        return manager.authorizationStatus
    }
    var hasAlways: Bool { status == .authorizedAlways }
    /// Asks for Always access. done(true) once location may be used; While Using also counts, with iOS
    /// asking later to allow it in the background.
    func requestAlways(_ done: @escaping (Bool) -> Void) {
        switch status {
        case .authorizedAlways: done(true)
        case .authorizedWhenInUse: manager.requestAlwaysAuthorization(); done(true)
        case .notDetermined: waiting = done; manager.requestAlwaysAuthorization()
        default: done(false)
        }
    }
    func start() {
        guard !running else { return }
        running = true; latest = nil
        #if DEBUG
        if testAuthorization != nil { return }
        #endif
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        manager.distanceFilter = kCLDistanceFilterNone
        manager.activityType = .airborne
        manager.pausesLocationUpdatesAutomatically = false
        manager.allowsBackgroundLocationUpdates = true
        manager.showsBackgroundLocationIndicator = true
        manager.startUpdatingLocation()
    }
    func stop() {
        guard running else { return }
        running = false; latest = nil
        #if DEBUG
        if testAuthorization != nil { return }
        #endif
        manager.stopUpdatingLocation(); manager.allowsBackgroundLocationUpdates = false
    }
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard running, let location = locations.last else { return }
        // Convert the fix time to sbio_now's monotonic clock, which the GPS cross-reference compares against.
        latest = PhoneFix(lat: location.coordinate.latitude, lon: location.coordinate.longitude,
                          accuracy: location.horizontalAccuracy, at: sbio_now() + location.timestamp.timeIntervalSinceNow)
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        guard running, (error as? CLError)?.code == .denied else { return }
        onStopped("Location access was turned off.")
    }
    func locationManagerDidPauseLocationUpdates(_ manager: CLLocationManager) {
        if running { onStopped("iOS paused location updates.") }
    }
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        #if DEBUG
        if testAuthorization != nil { return }
        #endif
        onAuthorization()
        if let waiting, manager.authorizationStatus != .notDetermined {
            self.waiting = nil
            waiting(manager.authorizationStatus == .authorizedAlways || manager.authorizationStatus == .authorizedWhenInUse)
        }
        if running, manager.authorizationStatus != .authorizedAlways, manager.authorizationStatus != .authorizedWhenInUse {
            onStopped("Location access was turned off.")
        }
    }
}
