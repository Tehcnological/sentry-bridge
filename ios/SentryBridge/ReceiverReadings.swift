import Foundation

/// Battery, CO and temperature from the receiver's /stats WebSocket, the same table as desktop
/// (ws_probe.SENSORS) and Android (NativeDiagnostics.sensorValues); tests/data/stats-cases.json keeps
/// them in step. Values are matched on the end of the flattened path, so the nesting may change; the
/// first listed suffix with a plausible number wins.
struct ReceiverReadings: Equatable {
    var batteryPercent: Int?
    var coPpm: Int?
    var temperatureC: Double?
    var isEmpty: Bool { batteryPercent == nil && coPpm == nil && temperatureC == nil }
    /// CO level the warning starts at. iOS does not read the receiver's own alarm level, so the
    /// warning never calls this level the receiver's (the same wording as desktop and Android then).
    static let coWarningPpm = 50
    var coWarning: String? {
        guard let coPpm, coPpm >= Self.coWarningPpm else { return nil }
        return "Carbon monoxide \(coPpm) ppm, at or above 50 ppm."
    }
    var battery: String { batteryPercent.map { "\($0)%" } ?? "-" }
    var co: String { coPpm.map { "\($0) ppm" } ?? "-" }
    var temperature: String { temperatureC.map { String(format: "%.1f°C", $0) } ?? "-" }

    /// Plausible readings in one /stats message; nil when it is not UTF-8 JSON (the message is skipped).
    static func parse(_ data: Data) -> ReceiverReadings? {
        guard let text = String(data: data, encoding: .utf8), let value = StatsJSON.parse(text) else { return nil }
        let flat = flatten(value)
        // The first suffix with a plausible number wins; the baro sensor reads the warm board.
        func reading(_ suffixes: [String], _ low: Double, _ high: Double, scale: Double) -> Double? {
            for suffix in suffixes {
                if let found = flat.first(where: { (key, number) in
                    (key == suffix || key.hasSuffix("." + suffix)) && number.map { low <= $0 && $0 <= high } == true
                })?.1 {
                    return (found * scale + 0.5).rounded(.down) / scale  // half up, as desktop and Android round
                }
            }
            return nil
        }
        var out = ReceiverReadings()
        out.batteryPercent = reading(["batterycapacity_%"], 0, 100, scale: 1).map { Int($0) }
        out.coPpm = reading(["co_sensor_ppm"], 0, 2000, scale: 1).map { Int($0) }
        out.temperatureC = reading(["ats.temp_c", "baro.temp_c"], -40, 85, scale: 10)
        return out
    }
    /// Lower-case flattened paths ("sensor.ats.temp_c", "list[0].x") in document order, with the
    /// numbers they hold (nil for any other value). A path seen twice keeps its first place and
    /// its last value, as the desktop's dictionaries do: once for the paths as written, then once
    /// more after lowering them.
    static func flatten(_ value: StatsJSON) -> [(String, Double?)] {
        var raw: [(String, Double?)] = []
        func walk(_ value: StatsJSON, _ prefix: String) {
            switch value {
            case .object(let members): for (key, member) in members { walk(member, prefix.isEmpty ? key : "\(prefix).\(key)") }
            case .array(let items): for (index, item) in items.enumerated() { walk(item, "\(prefix)[\(index)]") }
            case .number(let number): raw.append((prefix, number))
            case .other: raw.append((prefix, nil))
            }
        }
        walk(value, "")
        return merged(merged(raw).map { ($0.0.lowercased(), $0.1) })
    }
    private static func merged(_ pairs: [(String, Double?)]) -> [(String, Double?)] {
        var out: [(String, Double?)] = [], index: [String: Int] = [:]
        for (key, value) in pairs {
            if let at = index[key] { out[at].1 = value } else { index[key] = out.count; out.append((key, value)) }
        }
        return out
    }
}

/// A JSON value with object members in document order (Foundation's parser does not keep it). The
/// grammar is the desktop's (Python json): NaN and Infinity are accepted, a repeated member keeps
/// its first place and its last value, and anything else invalid rejects the whole message.
indirect enum StatsJSON {
    case object([(String, StatsJSON)]), array([StatsJSON]), number(Double), other
    static func parse(_ text: String) -> StatsJSON? {
        var parser = Parser(bytes: Array(text.utf8))
        parser.space()
        guard let value = parser.value(depth: 0) else { return nil }
        parser.space()
        return parser.at == parser.bytes.count ? value : nil
    }
    private struct Parser {
        let bytes: [UInt8]
        var at = 0
        mutating func space() { while at < bytes.count, [32, 9, 10, 13].contains(bytes[at]) { at += 1 } }
        mutating func literal(_ word: String) -> Bool {
            let w = Array(word.utf8)
            guard at + w.count <= bytes.count, Array(bytes[at..<at + w.count]) == w else { return false }
            at += w.count; return true
        }
        mutating func value(depth: Int) -> StatsJSON? {
            // Deeper than any receiver message; bounds the recursion on hostile input.
            guard depth < 512, at < bytes.count else { return nil }
            switch bytes[at] {
            case UInt8(ascii: "{"):
                at += 1; space()
                var members: [(String, StatsJSON)] = [], index: [String: Int] = [:]
                if at < bytes.count, bytes[at] == UInt8(ascii: "}") { at += 1; return .object(members) }
                while true {
                    space()
                    guard let key = string() else { return nil }
                    space()
                    guard at < bytes.count, bytes[at] == UInt8(ascii: ":") else { return nil }
                    at += 1; space()
                    guard let member = value(depth: depth + 1) else { return nil }
                    if let existing = index[key] { members[existing].1 = member } else { index[key] = members.count; members.append((key, member)) }
                    space()
                    guard at < bytes.count else { return nil }
                    if bytes[at] == UInt8(ascii: ",") { at += 1; continue }
                    if bytes[at] == UInt8(ascii: "}") { at += 1; return .object(members) }
                    return nil
                }
            case UInt8(ascii: "["):
                at += 1; space()
                var items: [StatsJSON] = []
                if at < bytes.count, bytes[at] == UInt8(ascii: "]") { at += 1; return .array(items) }
                while true {
                    space()
                    guard let item = value(depth: depth + 1) else { return nil }
                    items.append(item); space()
                    guard at < bytes.count else { return nil }
                    if bytes[at] == UInt8(ascii: ",") { at += 1; continue }
                    if bytes[at] == UInt8(ascii: "]") { at += 1; return .array(items) }
                    return nil
                }
            case UInt8(ascii: "\""): return string() == nil ? nil : .other
            default:
                if literal("true") || literal("false") || literal("null") { return .other }
                if literal("NaN") { return .number(.nan) }
                if literal("Infinity") { return .number(.infinity) }
                if literal("-Infinity") { return .number(-.infinity) }
                return number()
            }
        }
        mutating func number() -> StatsJSON? {
            let start = at
            func digits() -> Int { let from = at; while at < bytes.count, (48...57).contains(bytes[at]) { at += 1 }; return at - from }
            if at < bytes.count, bytes[at] == UInt8(ascii: "-") { at += 1 }
            if at < bytes.count, bytes[at] == UInt8(ascii: "0") { at += 1 } else if digits() == 0 { return nil }
            if at + 1 < bytes.count, bytes[at] == UInt8(ascii: "."), (48...57).contains(bytes[at + 1]) { at += 1; _ = digits() }
            if at < bytes.count, bytes[at] == UInt8(ascii: "e") || bytes[at] == UInt8(ascii: "E") {
                let mark = at
                at += 1
                if at < bytes.count, bytes[at] == UInt8(ascii: "+") || bytes[at] == UInt8(ascii: "-") { at += 1 }
                if digits() == 0 { at = mark }  // "1e" is the number 1 followed by an invalid "e"
            }
            // Out-of-range magnitudes become infinite, so no reading accepts them.
            return Double(String(decoding: bytes[start..<at], as: UTF8.self)).map { .number($0) }
        }
        mutating func string() -> String? {
            guard at < bytes.count, bytes[at] == UInt8(ascii: "\"") else { return nil }
            at += 1
            var out: [UInt8] = []
            while at < bytes.count {
                let byte = bytes[at]; at += 1
                switch byte {
                case UInt8(ascii: "\""): return String(decoding: out, as: UTF8.self)
                case 0..<32: return nil  // control characters must be escaped
                case UInt8(ascii: "\\"):
                    guard at < bytes.count else { return nil }
                    let escape = bytes[at]; at += 1
                    switch escape {
                    case UInt8(ascii: "\""), UInt8(ascii: "\\"), UInt8(ascii: "/"): out.append(escape)
                    case UInt8(ascii: "b"): out.append(8)
                    case UInt8(ascii: "f"): out.append(12)
                    case UInt8(ascii: "n"): out.append(10)
                    case UInt8(ascii: "r"): out.append(13)
                    case UInt8(ascii: "t"): out.append(9)
                    case UInt8(ascii: "u"):
                        guard at + 4 <= bytes.count, let unit = UInt32(String(decoding: bytes[at..<at + 4], as: UTF8.self), radix: 16) else { return nil }
                        at += 4
                        var scalar = unit
                        // A surrogate pair is one character; a lone surrogate becomes U+FFFD.
                        if (0xD800..<0xDC00).contains(unit), at + 6 <= bytes.count, bytes[at] == UInt8(ascii: "\\"), bytes[at + 1] == UInt8(ascii: "u"),
                           let low = UInt32(String(decoding: bytes[at + 2..<at + 6], as: UTF8.self), radix: 16), (0xDC00..<0xE000).contains(low) {
                            scalar = 0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00); at += 6
                        }
                        out.append(contentsOf: Array(String(Character(Unicode.Scalar(scalar) ?? "\u{FFFD}")).utf8))
                    default: return nil
                    }
                default: out.append(byte)
                }
            }
            return nil
        }
    }
}

/// While forwarding, listens to the receiver's /stats WebSocket (about two messages a second), the
/// same read-only traffic as the receiver's own statistics page. Reconnects after 10 s without a
/// message (a restarted receiver drops the connection without closing it) and 5 s after a failure.
/// Every wait checks cancel at least every 50 ms.
struct ReceiverReadingsReader {
    static let silenceS = 10.0, retryS = 5.0, staleS = 5.0
    let receiver: String
    let port: UInt16
    let cancel: OpaquePointer?
    let running: () -> Bool
    let publish: (ReceiverReadings) -> Void
    func run() {
        while running() {
            var error = [CChar](repeating: 0, count: 256)
            if let ws = sbio_ws_open(receiver, port, "/stats", 5000, cancel, &error, error.count) {
                var data: UnsafePointer<UInt8>?, length = 0
                while running() {
                    let result = sbio_ws_message(ws, Self.silenceS, cancel, &data, &length, &error, error.count)
                    guard result == 1 else { break }  // closed, failed, silent or cancelled: connect again
                    if let data, let values = ReceiverReadings.parse(Data(bytes: data, count: length)),
                       !values.isEmpty, running() { publish(values) }
                }
                sbio_ws_close(ws, 1)
            }
            // An older firmware without /stats, or the receiver is away: retry.
            let retry = sbio_now() + Self.retryS
            while running() && sbio_now() < retry { Thread.sleep(forTimeInterval: 0.05) }
        }
    }
}
