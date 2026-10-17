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
