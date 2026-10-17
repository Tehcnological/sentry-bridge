import Foundation

/// Labelled demo mode: replays the bundled synthetic Sentry datagrams (DemoReceiver.bin, written by
/// tools/generate_ios_demo.py) in a 60 s loop to the bridge's loopback input. The demo adapter decodes
/// without an output socket, so no generated frame is sent to an EFB.
final class DemoReplayer: @unchecked Sendable {
    /// outputPort only satisfies route validation; the demo adapter opens no output socket.
    static let inputPort = "14500", outputPort = "14501"
    private static let period = 60.0
    private let frames: [(offset: Double, payload: [UInt8])]
    // Socket and timer are touched only on this queue.
    private let queue = DispatchQueue(label: "io.sentrybridge.demo", qos: .userInitiated)
    private var timer: DispatchSourceTimer?
    private var socket: Int32 = -1

    init?(bundle: Bundle = .main) {
        guard let url = bundle.url(forResource: "DemoReceiver", withExtension: "bin"),
              let data = try? Data(contentsOf: url) else { return nil }
        let bytes = [UInt8](data)
        var frames: [(offset: Double, payload: [UInt8])] = []
        var index = 0
        while index + 6 <= bytes.count {
            let offset = UInt32(bytes[index]) << 24 | UInt32(bytes[index + 1]) << 16 | UInt32(bytes[index + 2]) << 8 | UInt32(bytes[index + 3])
            let length = Int(bytes[index + 4]) << 8 | Int(bytes[index + 5])
            index += 6
            guard index + length <= bytes.count else { return nil }
            frames.append((Double(offset) / 1000, Array(bytes[index..<index + length])))
            index += length
        }
        guard !frames.isEmpty else { return nil }
        self.frames = frames
    }

    func start() {
        queue.async { [self] in
            guard timer == nil else { return }
            socket = Darwin.socket(AF_INET, SOCK_DGRAM, 0)
            var address = sockaddr_in()
            address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
            address.sin_family = sa_family_t(AF_INET)
            address.sin_port = (UInt16(Self.inputPort) ?? 0).bigEndian
            address.sin_addr.s_addr = inet_addr("127.0.0.1")
            var cycleStart = sbio_now(), next = 0
            let timer = DispatchSource.makeTimerSource(queue: queue)
            timer.schedule(deadline: .now(), repeating: 0.05)
            timer.setEventHandler { [self] in
                if next == frames.count {
                    guard sbio_now() - cycleStart >= Self.period else { return }
                    cycleStart += Self.period; next = 0
                }
                let elapsed = sbio_now() - cycleStart
                while next < frames.count && frames[next].offset <= elapsed {
                    frames[next].payload.withUnsafeBytes { payload in
                        withUnsafePointer(to: &address) {
                            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                                _ = sendto(socket, payload.baseAddress, payload.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                            }
                        }
                    }
                    next += 1
                }
            }
            self.timer = timer
            timer.resume()
        }
    }

    func stop() {
        queue.async { [self] in
            timer?.cancel(); timer = nil
            if socket >= 0 { close(socket); socket = -1 }
        }
    }
}
