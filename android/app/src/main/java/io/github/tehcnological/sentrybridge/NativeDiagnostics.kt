package io.github.tehcnological.sentrybridge

import android.net.Network
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.net.InetAddress
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.InetSocketAddress
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

/** Bounded capture readers and read-only protocol tools; no decoder or UI dependencies. */
object NativeDiagnostics {
    private const val HANDSHAKE_LIMIT_NS = 10_000_000_000L
    fun describeMode(counts: JSONObject): String = when {
        counts.length() == 0 -> "NO DATA (are you on the Sentry WiFi? is :4000 free? firewall?)"
        counts.has("38") -> "ENCRYPTED (ForeFlight mode)"
        counts.has("0") || counts.has("10") -> "PUBLIC (standard GDL90)"
        else -> "UNKNOWN"
    }
    fun boundedRead(input: InputStream, limit: Int): ByteArray {
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(65536)
        while (true) {
            val n = input.read(buffer, 0, minOf(buffer.size, limit - out.size() + 1))
            if (n < 0) break
            require(out.size() + n <= limit) { "Receiver response exceeds the selected size limit." }
            out.write(buffer, 0, n)
        }
        return out.toByteArray()
    }
    /**
     * The decoded file name of a receiver /data/ link, or null when it is not a plain file name: the
     * rule of sentry_ctl.log_name. Only %XX escapes are decoded ('+' stays '+'), and a malformed
     * escape rejects that one link instead of the whole list.
     */
    fun logName(href: String): String? {
        val name = runCatching { java.net.URLDecoder.decode(href.replace("+", "%2B"), "UTF-8") }.getOrNull() ?: return null
        return name.takeIf { it.isNotEmpty() && ".." !in it && it.none { c -> c in "/\\?#" || c.code < 32 } }
    }
    fun hex(data: ByteArray): String {
        val digits = "0123456789abcdef"
        val chars = CharArray(data.size * 2)
        data.forEachIndexed { i, v -> chars[i * 2] = digits[(v.toInt() and 255) ushr 4]; chars[i * 2 + 1] = digits[v.toInt() and 15] }
        return String(chars)
    }
    fun unhex(text: String): ByteArray {
        require(text.length % 2 == 0 && text.length <= 131070 && text.all { it in "0123456789abcdefABCDEF" }) { "Invalid packet hex." }
        return ByteArray(text.length / 2) { text.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }
    fun ipv4(text: String, external: Boolean): InetAddress {
        require(text.matches(Regex("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}"))) { "Enter an IPv4 address." }
        val bytes = text.split('.').map { it.toInt().also { n -> require(n in 0..255) { "Invalid IPv4 address." } }.toByte() }.toByteArray()
        val first = bytes[0].toInt() and 255
        require(first in 1..223 && !(external && first == 127)) { "Use a unicast IPv4 address." }
        return InetAddress.getByAddress(bytes)
    }
    private var networkChecked = 0L
    private var localAddresses = emptySet<InetAddress>()
    private var broadcasts = emptySet<InetAddress>()
    @Synchronized fun checkEndpoint(address: InetAddress, port: Int, listenPort: Int = -1) {
        val now = System.nanoTime()
        if (networkChecked == 0L || now - networkChecked > 1_000_000_000L) {
            val interfaces = NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
            localAddresses = interfaces.flatMap { it.inetAddresses.toList() }.toSet()
            broadcasts = interfaces.flatMap { network -> network.interfaceAddresses
                .filter { it.address is Inet4Address && it.networkPrefixLength in 0..30 }
                .mapNotNull { it.broadcast } }.toSet()
            networkChecked = now
        }
        require(address !in broadcasts) {
            "Use the EFB device's unicast address, not a broadcast address."
        }
        require(port != listenPort || !address.isLoopbackAddress && address !in localAddresses) {
            "EFB output and receiver input ports must differ on this device."
        }
    }
    private val table = IntArray(256) { value ->
        var c = value shl 8
        repeat(8) { c = if (c and 0x8000 != 0) (c shl 1) xor 0x1021 else c shl 1 }
        c and 65535
    }
    private fun crc(data: ByteArray): Int {
        var c = 0
        for (v in data) c = (table[c ushr 8] xor (c shl 8) xor (v.toInt() and 255)) and 65535
        return c
    }
    fun frames(data: ByteArray): List<Pair<ByteArray, Boolean>> {
        val result = ArrayList<Pair<ByteArray, Boolean>>()
        val frame = ByteArrayOutputStream()
        var escape = false
        fun flush() {
            val raw = frame.toByteArray(); frame.reset(); escape = false
            if (raw.size < 3) return
            val message = raw.copyOfRange(0, raw.size - 2)
            result.add(message to (crc(message) == ((raw[raw.size - 2].toInt() and 255) or ((raw.last().toInt() and 255) shl 8))))
        }
        for (v in data) {
            val b = v.toInt() and 255
            if (b == 126) flush()
            else if (escape) { frame.write(b xor 32); escape = false }
            else if (b == 125) escape = true
            else frame.write(b)
        }
        flush()
        return result
    }

    /**
     * True when a datagram holds a CRC-valid heartbeat long enough to carry GPS status (7 bytes,
     * the same rule core/sbcore.c applies). Other frames, including undersized 0x00 frames that the
     * core still counts, never refresh GPS freshness.
     */
    fun hasHeartbeat(data: ByteArray): Boolean {
        // Message ID 0 is never escaped, so a heartbeat always starts with the bytes 7E 00.
        if ((0 until data.size - 1).none { data[it] == 126.toByte() && data[it + 1] == 0.toByte() }) return false
        return frames(data).any { (message, ok) -> ok && message.size >= 7 && message[0] == 0.toByte() }
    }

    /** True when an IPv4 interface address with this prefix length covers the target IPv4 address. */
    fun sameIpv4Subnet(address: ByteArray, prefixLength: Int, target: ByteArray): Boolean {
        if (address.size != 4 || target.size != 4 || prefixLength !in 1..32) return false
        val mask = if (prefixLength == 32) -1 else -1 shl (32 - prefixLength)
        return (ByteBuffer.wrap(address).int and mask) == (ByteBuffer.wrap(target).int and mask)
    }

    /** EFBs kept for Find EFB, the same bound as discovered forwarding destinations. */
    const val DISCOVERY_LIMIT = 32
    private const val ANNOUNCE_TTL_MS = 30_000L

    /** Find EFB's list while the bridge runs: expired entries are pruned and at most DISCOVERY_LIMIT kept. */
    fun rememberEfb(cache: MutableMap<InetSocketAddress, Pair<String, Long>>, dest: InetSocketAddress, app: String, nowMs: Long) {
        cache.entries.removeIf { nowMs - it.value.second > ANNOUNCE_TTL_MS }
        if (dest in cache || cache.size < DISCOVERY_LIMIT) cache[dest] = app to nowMs
    }

    // A TCP knock on a port nothing listens on (tools/discovery.py): a device answers at once, accepting or
    // refusing, while an unused address stays silent.
    private const val PROBE_PORT = 7
    private const val PROBE_TIMEOUT_MS = 2000

    /** Whether a device is at this address: it accepted or refused a TCP connection. */
    fun answers(network: Network?, address: InetAddress): Boolean = try {
        Socket().use { socket -> network?.bindSocket(socket); socket.connect(InetSocketAddress(address, PROBE_PORT), PROBE_TIMEOUT_MS) }
        true
    } catch (e: java.net.ConnectException) {
        e.message?.contains("ECONNREFUSED") == true || e.message?.contains("refused", ignoreCase = true) == true
    } catch (_: java.io.IOException) { false }

    /** Devices answering on local's /24 network, other than local and the excluded addresses, in address order.
     *  Cancelled before or after the probes, it gives none. */
    fun sweep(local: Inet4Address, exclude: Set<String>, cancelled: () -> Boolean = { false },
              probe: (InetAddress) -> Boolean): List<String> {
        val hosts = (1..254).map { last -> InetAddress.getByAddress(local.address.copyOf().also { it[3] = last.toByte() }) }
            .filter { it.hostAddress != local.hostAddress && it.hostAddress !in exclude }
        if (cancelled()) return emptyList()
        val pool = java.util.concurrent.Executors.newFixedThreadPool(128)  // two rounds of one timeout at most
        try {
            val results = hosts.map { host -> pool.submit<Boolean> { !cancelled() && probe(host) } }
            val found = hosts.zip(results).filter { it.second.get() }.map { it.first.hostAddress!! }
            return if (cancelled()) emptyList() else found
        } finally { pool.shutdownNow() }
    }

    /** Whether local is on the receiver's /24 network: a sweep never scans another network. */
    fun onNetwork(local: Inet4Address, receiver: String): Boolean =
        runCatching { ipv4(receiver, false).address.copyOf(3).contentEquals(local.address.copyOf(3)) }.getOrDefault(false)

    /** After the EFB at current missed its checks: current itself when it answers the sweep (it recovered), else the
     *  one other device that answers, or null for none or several (the user chooses then). Same rule as desktop. */
    fun followChoice(candidates: List<String>, current: String): String? =
        if (current in candidates) current else candidates.singleOrNull()

    /** Whether an announcing device can be an Other device destination (same rules as desktop). */
    fun efbCandidate(address: InetAddress, port: Int, sentryIp: String, listenPort: Int): Boolean {
        if (address !is Inet4Address || address.isLoopbackAddress || address.hostAddress == sentryIp) return false
        return try { checkEndpoint(address, port, listenPort); true } catch (_: IllegalArgumentException) { false }
    }

    /**
     * Listens for EFB announcements (JSON with a GDL90 port) for Find EFB; never sends anything.
     * parse returns "port\tapp" like SbCore.parseAnnounce; accept filters destinations.
     */
    fun listenForEfbs(port: Int, seconds: Double, ensureOpen: () -> Unit, parse: (ByteArray) -> String?,
                      accept: (InetAddress, Int) -> Boolean): JSONArray {
        val found = sortedMapOf<String, JSONObject>()
        DatagramSocket(null).use { socket ->
            socket.reuseAddress = true
            socket.bind(InetSocketAddress(port))
            socket.soTimeout = 200
            val buffer = ByteArray(4096)
            val deadline = System.nanoTime() + (seconds * 1e9).toLong()
            while (System.nanoTime() < deadline) {
                ensureOpen()
                val packet = DatagramPacket(buffer, buffer.size)
                try { socket.receive(packet) } catch (_: SocketTimeoutException) { continue }
                val parts = parse(packet.data.copyOf(packet.length))?.split('\t', limit = 2) ?: continue
                val efbPort = parts[0].toIntOrNull() ?: continue
                val key = "${packet.address.hostAddress}:$efbPort"
                if (key !in found && found.size >= DISCOVERY_LIMIT || !accept(packet.address, efbPort)) continue
                val app = parts.getOrElse(1) { "" }.ifEmpty { "?" }
                found[key] = JSONObject().put("app", app)
                    .put("address", packet.address.hostAddress).put("port", efbPort)
            }
        }
        return JSONArray(found.values.toList())
    }

    private fun udp(data: ByteArray, link: Int): Pair<Int, ByteArray>? {
        fun u16(at: Int): Int { require(at + 2 <= data.size); return ((data[at].toInt() and 255) shl 8) or (data[at + 1].toInt() and 255) }
        var start = when (link) {
            1 -> { if (data.size < 14) return null; var p = 14; var type = u16(12); while (type in listOf(0x8100, 0x88a8) && p + 4 <= data.size) { type = u16(p + 2); p += 4 }; if (type != 0x800) return null; p }
            113 -> { if (data.size < 16 || u16(14) != 0x800) return null; 16 }
            276 -> { if (data.size < 20 || u16(0) != 0x800) return null; 20 }
            0, 108 -> 4
            101, 12, 228 -> 0
            else -> throw IllegalArgumentException("Unsupported pcap link type $link.")
        }
        if (data.size < start + 20 || data[start].toInt() ushr 4 and 15 != 4 || data[start + 9].toInt() != 17) return null
        val ihl = (data[start].toInt() and 15) * 4
        if (ihl < 20 || data.size < start + ihl + 8 || u16(start + 6) and 0x3fff != 0) return null
        val total = u16(start + 2)
        start += ihl
        val length = u16(start + 4)
        if (length < 8 || length > total - ihl || data.size < start + length) return null
        return u16(start + 2) to data.copyOfRange(start + 8, start + length)
    }

    fun packets(file: File, port: Int): Sequence<ByteArray> = sequence {
        require(file.length() <= 32 * 1024 * 1024) { "Capture exceeds 32 MiB." }
        if (file.extension.lowercase() == "jsonl") {
            file.bufferedReader().use { reader ->
                while (true) {
                    val line = reader.readLine() ?: break
                    require(line.length <= 160000) { "Capture row is too large." }
                    if (line.isNotBlank()) yield(unhex(JSONObject(line).getString("hex")))
                }
            }
        } else {
            val data = file.readBytes()
            require(data.size >= 24) { "Capture is truncated." }
            fun int(at: Int, order: ByteOrder): Int { require(at >= 0 && at + 4 <= data.size); return ByteBuffer.wrap(data, at, 4).order(order).int }
            fun ushort(at: Int, order: ByteOrder): Int { require(at >= 0 && at + 2 <= data.size); return ByteBuffer.wrap(data, at, 2).order(order).short.toInt() and 65535 }
            if (int(0, ByteOrder.BIG_ENDIAN) == 0x0a0d0d0a) {
                var at = 0
                var order = ByteOrder.LITTLE_ENDIAN
                val links = ArrayList<Int>()
                while (at < data.size) {
                    require(at + 12 <= data.size) { "pcapng block is truncated." }
                    if (int(at, ByteOrder.BIG_ENDIAN) == 0x0a0d0d0a) {
                        require(at + 28 <= data.size)
                        order = when (int(at + 8, ByteOrder.BIG_ENDIAN)) { 0x1a2b3c4d -> ByteOrder.BIG_ENDIAN; 0x4d3c2b1a -> ByteOrder.LITTLE_ENDIAN; else -> throw IllegalArgumentException("Bad pcapng byte order.") }
                        links.clear()
                    }
                    val type = int(at, order)
                    val size = int(at + 4, order)
                    require(size >= 12 && size % 4 == 0 && size <= data.size - at && int(at + size - 4, order) == size) { "Invalid pcapng block length." }
                    if (type == 1) { require(size >= 20); links.add(ushort(at + 8, order)) }
                    else if (type == 6 || type == 2 || type == 3) {
                        val iface = if (type == 6) int(at + 8, order) else if (type == 2) ushort(at + 8, order) else 0
                        require(iface in links.indices) { "pcapng packet has an unknown interface." }
                        val offset = if (type == 3) 12 else 28
                        require(size >= offset + 4)
                        val length = if (type == 3) minOf(int(at + 8, order), size - 16) else int(at + 20, order)
                        require(length >= 0 && length <= size - offset - 4) { "pcapng packet is truncated." }
                        udp(data.copyOfRange(at + offset, at + offset + length), links[iface])?.let { if (it.first == port) yield(it.second) }
                    }
                    at += size
                }
            } else {
                val magic = int(0, ByteOrder.BIG_ENDIAN)
                val order = when (magic) { 0xd4c3b2a1.toInt(), 0x4d3cb2a1 -> ByteOrder.LITTLE_ENDIAN; 0xa1b2c3d4.toInt(), 0xa1b23c4d.toInt() -> ByteOrder.BIG_ENDIAN; else -> throw IllegalArgumentException("Invalid pcap format.") }
                val link = int(20, order)
                var at = 24
                while (at < data.size) {
                    require(at + 16 <= data.size) { "pcap packet header is truncated." }
                    val size = int(at + 8, order); at += 16
                    require(size >= 0 && size <= data.size - at) { "pcap packet is truncated." }
                    udp(data.copyOfRange(at, at + size), link)?.let { if (it.first == port) yield(it.second) }
                    at += size
                }
            }
        }
    }

    fun analyze(file: File, port: Int, samples: Int): String {
        val counts = IntArray(256); val bad = IntArray(256)
        val examples = HashMap<Int, ArrayList<String>>()
        var datagrams = 0
        for (data in packets(file, port)) {
            datagrams++
            for ((message, okay) in frames(data)) {
                val id = message[0].toInt() and 255
                counts[id]++; if (!okay) bad[id]++
                val list = examples.getOrPut(id) { ArrayList() }
                if (list.size < samples) list.add(hex(message))
            }
        }
        return buildString {
            append("Datagrams: $datagrams\nPort: $port\n")
            for (i in counts.indices) if (counts[i] > 0) {
                append("0x%02X: %d frames, %d bad CRC\n".format(i, counts[i], bad[i]))
                for (example in examples[i].orEmpty()) append("  $example\n")
            }
            append("Raw traffic summaries on Android are limited to frame IDs, CRC and hex samples.\n")
        }
    }

    fun weather(source: File, output: File, inputPort: Int, outputPort: Int): String {
        data class Observed(val counts: HashMap<String, Int>, var crc: Int = 0, var length: Int = 0)
        fun collect(file: File, port: Int): Observed {
            val result = Observed(HashMap())
            for (data in packets(file, port)) for ((message, okay) in frames(data)) {
                if (message[0].toInt() != 7) continue
                if (!okay) result.crc++ else if (message.size != 436) result.length++
                else {
                    val key = hex(MessageDigest.getInstance("SHA-256").digest(message))
                    result.counts[key] = (result.counts[key] ?: 0) + 1
                }
            }
            return result
        }
        val input = collect(source, inputPort); val out = collect(output, outputPort)
        val missing = input.counts.entries.sumOf { (key, count) -> maxOf(0, count - (out.counts[key] ?: 0)) }
        val unexpected = out.counts.entries.sumOf { (key, count) -> maxOf(0, count - (input.counts[key] ?: 0)) }
        val status = if (input.crc + out.crc + input.length + out.length > 0) "failed" else if (input.counts.isEmpty()) "inconclusive" else if (missing + unexpected > 0) "failed" else "passed"
        return JSONObject().put("status", status).put("inputPort", inputPort).put("outputPort", outputPort)
            .put("inputFrames", input.counts.values.sum()).put("outputFrames", out.counts.values.sum())
            .put("matchedFrames", input.counts.values.sum() - missing).put("missingFrames", missing).put("unexpectedFrames", unexpected)
            .put("inputCrcFailures", input.crc).put("outputCrcFailures", out.crc).put("inputLengthFailures", input.length).put("outputLengthFailures", out.length)
            .put("weatherDisplayVerified", false).toString(2)
    }

    // Sensor values shown from the receiver's /stats WebSocket: the same table as ws_probe.SENSORS
    // (tests/data/stats-cases.json). Matched on the end of the flattened path; the first plausible number wins.
    private class Sensor(val name: String, val suffixes: List<String>, val low: Double, val high: Double, val digits: Int)
    private val sensors = listOf(Sensor("batteryPercent", listOf("batterycapacity_%"), 0.0, 100.0, 0),
        Sensor("coPpm", listOf("co_sensor_ppm"), 0.0, 2000.0, 0),
        Sensor("temperatureC", listOf("ats.temp_c", "baro.temp_c"), -40.0, 85.0, 1))  // the baro sensor reads the warm board

    private fun flatten(value: Any?, prefix: String, out: MutableList<Pair<String, Any?>>) {
        when (value) {
            is JSONObject -> value.keys().forEach { flatten(value.opt(it), if (prefix.isEmpty()) it else "$prefix.$it", out) }
            is JSONArray -> for (i in 0 until value.length()) flatten(value.opt(i), "$prefix[$i]", out)
            else -> out.add(prefix.lowercase() to value)
        }
    }

    /** Plausible battery, CO and temperature readings in a /stats message; absent ones are left out. */
    fun sensorValues(text: String): JSONObject {
        val flat = mutableListOf<Pair<String, Any?>>()
        flatten(runCatching { JSONObject(text) }.getOrNull() ?: return JSONObject(), "", flat)
        val out = JSONObject()
        for (sensor in sensors) {
            val found = sensor.suffixes.firstNotNullOfOrNull { suffix ->
                flat.firstNotNullOfOrNull { (key, value) ->
                    (value as? Number)?.toDouble()?.takeIf { (key == suffix || key.endsWith(".$suffix")) && it in sensor.low..sensor.high }
                }
            } ?: continue
            val scale = Math.pow(10.0, sensor.digits.toDouble())
            val rounded = Math.floor(found * scale + 0.5)  // half up, as ws_probe rounds
            if (sensor.digits == 0) out.put(sensor.name, rounded.toLong()) else out.put(sensor.name, rounded / scale)
        }
        return out
    }

    /** With no output file nothing is recorded; onText receives each text message. ensureOpen throws when the
     *  app is closing; it runs before connecting and at least once a second. Both callbacks are required and
     *  ensureOpen comes last, so a trailing lambda is always the Exit check. */
    fun websocket(network: Network, config: Settings, path: String, seconds: Double, output: File?,
                  onText: (String) -> Unit, ensureOpen: () -> Unit): String {
        val random = SecureRandom()
        ensureOpen()
        Socket().use { socket ->
            network.bindSocket(socket)
            socket.connect(InetSocketAddress(config.sentryIp, config.httpPort), 3000)
            ensureOpen()  // the connect can take 3 s: no upgrade request after Exit
            socket.soTimeout = 1000
            val input = socket.getInputStream().buffered()
            val out = socket.getOutputStream()
            val key = Base64.getEncoder().encodeToString(ByteArray(16).also { random.nextBytes(it) })
            val host = "${config.sentryIp}:${config.httpPort}"
            out.write(("GET $path HTTP/1.1\r\nHost: $host\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\nOrigin: http://$host\r\n\r\n").toByteArray())
            val head = StringBuilder()
            // A slow peer can keep each read short, so check for Exit per byte and bound the elapsed time.
            val handshakeDeadline = System.nanoTime() + HANDSHAKE_LIMIT_NS
            while (!head.endsWith("\r\n\r\n")) {
                ensureOpen()
                if (System.nanoTime() > handshakeDeadline) throw SocketTimeoutException("WebSocket handshake took longer than 10 seconds.")
                val c = try { input.read() } catch (_: SocketTimeoutException) { continue }
                require(c >= 0) { "WebSocket closed during the handshake." }
                head.append(c.toChar()); require(head.length < 16384)
            }
            val lines = head.toString().split("\r\n")
            require(lines[0].contains(" 101 ")) { "WebSocket handshake refused: ${lines[0]}" }
            val accept = Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-1").digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").toByteArray()))
            require(lines.any { it.substringBefore(':').lowercase() == "sec-websocket-accept" && it.substringAfter(':').trim() == accept }) { "Invalid WebSocket handshake." }
            val deadline = System.nanoTime() + (seconds * 1e9).toLong()
            fun byte(): Int {
                while (System.nanoTime() < deadline) {
                    ensureOpen()  // reads time out every second, so Exit is noticed within about a second
                    try { return input.read().also { require(it >= 0) { "WebSocket closed." } } } catch (_: SocketTimeoutException) { }
                }
                throw SocketTimeoutException()
            }
            fun send(opcode: Int, bytes: ByteArray) {
                require(bytes.size <= 125)
                val mask = ByteArray(4).also { random.nextBytes(it) }
                out.write(byteArrayOf((128 or opcode).toByte(), (128 or bytes.size).toByte()) + mask + ByteArray(bytes.size) { (bytes[it].toInt() xor mask[it % 4].toInt()).toByte() })
            }
            var messages = 0
            var recordedBytes = 0
            var fragmentOp = 0
            val fragment = ByteArrayOutputStream()
            val fields = LinkedHashSet<String>()
            val writer = output?.bufferedWriter()
            try {
                try {
                    while (System.nanoTime() < deadline) {
                        val first = byte(); val second = byte()
                        require(first and 0x70 == 0 && second and 0x80 == 0) { "Unsupported WebSocket frame." }
                        val opcode = first and 15
                        var length = (second and 127).toLong()
                        if (length == 126L) length = ((byte() shl 8) or byte()).toLong()
                        else if (length == 127L) { length = 0; repeat(8) { length = (length shl 8) or byte().toLong() } }
                        require(length in 0..(1024 * 1024).toLong() && fragment.size() + length <= 1024 * 1024) { "WebSocket message exceeds 1 MiB on Android." }
                        require(opcode < 8 || length <= 125 && first and 128 != 0)
                        val data = ByteArray(length.toInt()) { byte().toByte() }
                        when (opcode) {
                            8 -> { send(8, data.take(2).toByteArray()); break }
                            9 -> send(10, data)
                            10 -> Unit
                            0, 1, 2 -> {
                                if (opcode != 0) { require(fragmentOp == 0); fragmentOp = opcode } else require(fragmentOp != 0)
                                fragment.write(data)
                                if (first and 128 != 0) {
                                    val value = fragment.toByteArray(); fragment.reset(); messages++
                                    val text = value.toString(Charsets.UTF_8)
                                    if (fragmentOp == 1) onText(text)
                                    if (writer != null) {
                                        runCatching { JSONObject(text).keys().forEach { fields.add(it) } }
                                        val row = JSONObject().put("t", System.currentTimeMillis() / 1000.0).put("opcode", fragmentOp)
                                            .put("text", if (fragmentOp == 1) text else JSONObject.NULL).put("hex", if (fragmentOp == 2) hex(value) else JSONObject.NULL).toString() + "\n"
                                        val size = row.toByteArray().size
                                        require(size <= 32 * 1024 * 1024 - recordedBytes) { "WebSocket recording reached 32 MiB." }
                                        writer.write(row); recordedBytes += size
                                    }
                                    fragmentOp = 0
                                }
                            }
                            else -> throw IllegalArgumentException("Unsupported WebSocket opcode.")
                        }
                    }
                } catch (_: SocketTimeoutException) { }
            } finally { writer?.close() }
            runCatching { send(8, byteArrayOf(3, -24)) }
            return "Messages: $messages\nFields: ${fields.joinToString(", ")}\nSensor support remains unverified.\n"
        }
    }
}
