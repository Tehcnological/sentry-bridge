import Foundation
import Darwin

struct DiscoveredEFB: Identifiable, Equatable {
    let address: String
    let port: UInt16
    let app: String
    let seen: Double
    var id: String { "\(address):\(port)" }
}

// Owned by one networking worker. The shared C parser supplies the display name unchanged.
final class EFBAnnouncements {
    private var entries: [String: DiscoveredEFB] = [:]
    let receiver: String
    private let receiverAddress: UInt32
    let inputPort: UInt16
    init(_ settings: BridgeSettings) {
        receiver = settings.source; inputPort = UInt16(settings.inputPort) ?? 0
        var address: UInt32 = 0
        _ = sb_parse_ipv4(settings.source, &address)
        receiverAddress = address
    }
    func snapshot(now: Double = sbio_now()) -> [DiscoveredEFB] {
        entries = entries.filter { now - $0.value.seen < 30 }
        return entries.values.sorted { $0.id < $1.id }
    }
    func receive(_ data: Data, address: String, now: Double = sbio_now()) {
        guard !data.isEmpty, data.count <= 4096 else { return }
        var name = [CChar](repeating: 0, count: 128)
        let port = data.withUnsafeBytes { bytes in
            sb_parse_announce(bytes.baseAddress?.assumingMemoryBound(to: CChar.self), bytes.count, &name, name.count)
        }
        guard port > 0, port <= 65535,
              sbio_efb_candidate(address, UInt16(port), receiver, inputPort) != 0 else { return }
        entries = entries.filter { now - $0.value.seen < 30 }
        let entry = DiscoveredEFB(address: address, port: UInt16(port), app: String(cString: name), seen: now)
        guard entries[entry.id] != nil || entries.count < 32 else { return }
        entries[entry.id] = entry
    }
    func receive(_ bytes: UnsafePointer<UInt8>?, count: Int, hostAddress: UInt32) {
        // Receiver traffic cannot be an EFB candidate. Reject it before copying or parsing.
        guard hostAddress != receiverAddress, let bytes, count > 0, count <= 4096 else { return }
        // JSON can begin with whitespace; ignore binary GDL90/receiver traffic cheaply.
        var first = 0
        while first < count && (bytes[first] == 32 || bytes[first] == 9 || bytes[first] == 10 || bytes[first] == 13) { first += 1 }
        guard first < count, bytes[first] == 123 else { return }
        let address = "\(hostAddress >> 24).\((hostAddress >> 16) & 255).\((hostAddress >> 8) & 255).\(hostAddress & 255)"
        receive(Data(bytes: bytes, count: count), address: address)
    }
}

enum DiscoveryError: LocalizedError {
    case socket(String)
    var errorDescription: String? {
        if case .socket(let message) = self { return message }; return nil
    }
}

// Exclusive socket ownership. No reuse options, probes, replies or packet transmission.
final class EFBListener {
    private var fd: Int32 = -1
    init(port: UInt16) throws {
        guard port != 0 else { throw DiscoveryError.socket("Set an EFB discovery port from 1 to 65535 in Settings.") }
        fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { throw failure() }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET); address.sin_port = port.bigEndian
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        if result != 0 || fcntl(fd, F_SETFL, O_NONBLOCK) < 0 {
            let error = failure(); close(); throw error
        }
    }
    private func failure() -> DiscoveryError {
        if errno == EADDRINUSE { return .socket("EFB discovery port is already in use. Close another discovery app or choose its documented discovery port in Settings, then Find EFB again.") }
        if errno == EACCES || errno == EPERM { return .socket("Local Network access is denied. Enable Sentry Bridge in Settings > Privacy & Security > Local Network, then Find EFB again.") }
        return .socket("Cannot listen for EFBs: \(String(cString: strerror(errno))). Check the discovery port and Local Network access.")
    }
    func step(_ cache: EFBAnnouncements, timeout: Int32 = 0) throws {
        var ready = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
        let result = poll(&ready, 1, timeout)
        if result < 0 && errno != EINTR { throw failure() }
        if ready.revents & Int16(POLLERR | POLLHUP | POLLNVAL) != 0 { throw failure() }
        guard result > 0 else { return }
        var bytes = [UInt8](repeating: 0, count: 4097)
        for _ in 0..<64 {
            var source = sockaddr_in(); var size = socklen_t(MemoryLayout<sockaddr_in>.size)
            let count = withUnsafeMutablePointer(to: &source) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { recvfrom(fd, &bytes, bytes.count, 0, $0, &size) }
            }
            if count < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR { return }
                throw failure()
            }
            if source.sin_family == AF_INET && count <= 4096 {
                bytes.withUnsafeBufferPointer { cache.receive($0.baseAddress, count: count, hostAddress: UInt32(bigEndian: source.sin_addr.s_addr)) }
            }
        }
    }
    func close() { if fd >= 0 { Darwin.close(fd); fd = -1 } }
    deinit { close() }
}

/// Finds devices on the receiver's WiFi that do not announce (tools/discovery.py): one TCP connection
/// attempt to port 7 per address of this device's /24 network. A device answers at once, accepting or
/// refusing; an unused address stays silent until the timeout.
enum EFBSweep {
    static let probePort: UInt16 = 7, probeTimeoutMs: Int32 = 3000
    /// Whether local is on the receiver's /24 network: a sweep never scans another network.
    static func onNetwork(_ local: String, _ receiver: String) -> Bool {
        var a: UInt32 = 0, b: UInt32 = 0
        return sb_parse_ipv4(local, &a) == 1 && sb_parse_ipv4(receiver, &b) == 1 && a >> 8 == b >> 8
    }
    /// This device's address on the receiver's network (the route to it; nothing is sent), or nil when
    /// it is not on that network.
    static func localAddress(receiver: String) -> String? {
        var buffer = [CChar](repeating: 0, count: 64)
        guard sbio_route_local(receiver, 9, &buffer, buffer.count) == 0 else { return nil }
        let local = String(cString: buffer)
        guard local != "0.0.0.0", !BridgeSettings.isLoopback(local), onNetwork(local, receiver) else { return nil }
        return local
    }
    /// The other addresses of local's /24 network, in address order.
    static func hosts(local: String, exclude: Set<String>) -> [String] {
        var ip: UInt32 = 0
        guard sb_parse_ipv4(local, &ip) == 1 else { return [] }
        let base = ip & 0xffff_ff00
        return (1...254).map { address(base | UInt32($0)) }.filter { $0 != local && !exclude.contains($0) }
    }
    static func address(_ ip: UInt32) -> String { "\(ip >> 24).\((ip >> 16) & 255).\((ip >> 8) & 255).\(ip & 255)" }
    /// Devices answering on local's /24 network, other than local and the excluded addresses, in address
    /// order. probe answers per address, or nil when cancelled; cancelled before or after it gives none.
    static func sweep(local: String, exclude: Set<String>, probe: ([String]) -> [Bool]?,
                      cancelled: () -> Bool) -> [String] {
        let candidates = hosts(local: local, exclude: exclude)
        guard !cancelled(), let answers = probe(candidates), answers.count == candidates.count, !cancelled() else { return [] }
        return zip(candidates, answers).filter { $0.1 }.map { $0.0 }
    }
    /// The real probe: every address at once (128 in flight), each bounded by the probe timeout.
    static func probe(_ cancel: OpaquePointer?) -> ([String]) -> [Bool]? {
        return { addresses in
            var results = [UInt8](repeating: 0, count: addresses.count)
            let strings = addresses.map { strdup($0) }
            defer { strings.forEach { free($0) } }
            let status = strings.map { UnsafePointer($0) }.withUnsafeBufferPointer { pointers in
                sbio_probe(pointers.baseAddress, addresses.count, probePort, probeTimeoutMs, cancel, &results)
            }
            return status == 0 ? results.map { $0 != 0 } : nil
        }
    }
    /// After the EFB at current missed its checks: current itself when it answers the sweep (it recovered),
    /// else the one other device that answers, or nil for none or several (the user chooses then).
    static func followChoice(_ candidates: [String], current: String) -> String? {
        if candidates.contains(current) { return current }
        return candidates.count == 1 ? candidates[0] : nil
    }
}

/// The follow rule (tools/gui_server.py _follow_efb, Android NativeGui.watchEfb): while forwarding to one
/// Other device address, check it every 30 s. Only an EFB that answered before and then missed two checks
/// in a row leads to a sweep; one that never answered (a firewall, perhaps) keeps its route as configured.
struct EFBFollower {
    static let checkS = 30.0, misses = 2
    enum Decision: Equatable { case keep, follow(String), report(String) }
    private(set) var seen = false, misses = 0
    private var reported: String?
    /// After one check of the current address: whether to sweep now.
    mutating func checked(answered: Bool) -> Bool {
        if answered { seen = true; misses = 0; reported = nil; return false }
        misses += 1
        return seen && misses >= Self.misses
    }
    /// After the sweep: keep the route (it recovered, or nothing new to say), follow the one other
    /// device, or report once per address that none or several answered.
    mutating func swept(_ candidates: [String], current: String) -> Decision {
        guard let target = EFBSweep.followChoice(candidates, current: current) else {
            guard reported != current else { return .keep }
            reported = current
            return .report("The EFB at \(current) stopped answering. Devices on the receiver's WiFi: \(candidates.isEmpty ? "none" : candidates.joined(separator: ", ")). Choose one with Find EFB.")
        }
        if target == current { seen = true; misses = 0; reported = nil; return .keep }  // a short interruption
        return .follow(target)
    }
    /// After the switch was committed.
    mutating func followed() { misses = 0; reported = nil }
    static func followMessage(from current: String, to target: String) -> String {
        "The EFB at \(current) stopped answering; now sending to \(target), the only other device on the receiver's WiFi."
    }
}

/// Find EFB's list: announcing EFBs first, then swept devices that do not announce, each kept 30 s. Results
/// belong to the destination the search started with. A route change (the bridge following a moved EFB)
/// clears the list, and results that search reports later are dropped (web/app.js discoveryRoute).
struct DiscoveryList {
    private var route: String?
    private var announced: [DiscoveredEFB] = [], swept: [DiscoveredEFB] = []
    mutating func start(route: String) { self.route = route; announced = []; swept = [] }
    /// route is the destination when the results arrive; a different one means they may list the old address.
    mutating func announce(_ entries: [DiscoveredEFB], route: String) { if route == self.route { announced = entries } }
    mutating func sweep(_ entries: [DiscoveredEFB], route: String) { if route == self.route { swept = entries } }
    mutating func routeChanged() { route = nil; announced = []; swept = [] }
    func isCurrent(route: String) -> Bool { route == self.route }
    mutating func entries(now: Double = sbio_now()) -> [DiscoveredEFB] {
        announced = announced.filter { now - $0.seen < 30 }; swept = swept.filter { now - $0.seen < 30 }
        let addresses = Set(announced.map(\.address))
        return announced + swept.filter { !addresses.contains($0.address) }
    }
}
