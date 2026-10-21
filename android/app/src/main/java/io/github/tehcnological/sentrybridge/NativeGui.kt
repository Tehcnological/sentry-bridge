package io.github.tehcnological.sentrybridge

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.SystemClock
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedWriter
import java.io.File
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.net.Proxy
import java.net.URLEncoder
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import java.util.zip.GZIPInputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** Authenticated loopback transport, receiver tools and private artifacts for the shared UI. */
object NativeGui {
    private const val AUTO_BACKUP = "sentry-auto-backup.zip"
    /** Automatic restoration backs up on every receiver power-up. */
    private const val AUTO_BACKUPS_KEPT = 5
    private lateinit var context: Context

    // Update check: GitHub's latest stable release, opt-in, at most once a day, only while the bridge is stopped.
    // An F-Droid install never checks: the F-Droid client updates it.
    private const val RELEASES = "https://api.github.com/repos/Tehcnological/sentry-bridge/releases/latest"
    private const val UPDATE_INTERVAL_MS = 24 * 3600 * 1000L
    private const val UPDATE_RETRY_MS = 3600 * 1000L
    private val FDROID_INSTALLERS = setOf("org.fdroid.fdroid", "org.fdroid.basic", "com.looker.droidify", "com.machiav3lli.fdroid")
    @Volatile private var update: JSONObject? = null
    @Volatile private var updateDue = 0L
    @Volatile private var updateChecking = false
    private val updateSupported by lazy {
        runCatching {
            val pm = context.packageManager
            val installer = if (android.os.Build.VERSION.SDK_INT >= 30) pm.getInstallSourceInfo(context.packageName).installingPackageName
                else @Suppress("DEPRECATION") pm.getInstallerPackageName(context.packageName)
            installer !in FDROID_INSTALLERS
        }.getOrDefault(true)
    }

    /** Whether release tag vX.Y.Z is newer than X.Y.Z (gui_server.newer). Anything else is not. */
    fun newer(tag: String, current: String): Boolean {
        val a = tag.removePrefix("v").split('.').map { it.toIntOrNull() ?: return false }
        val b = current.split('.').map { it.toIntOrNull() ?: return false }
        for (i in 0 until maxOf(a.size, b.size)) {
            val d = a.getOrElse(i) { 0 } - b.getOrElse(i) { 0 }
            if (d != 0) return d > 0
        }
        return false
    }

    /** The remembered release, while it is still newer than this app (not once it is installed). */
    private fun currentUpdate(): JSONObject? = update?.takeIf {
        config.updateCheck && updateSupported && newer("v" + it.optString("version"), SbCore.version())
    }

    @Synchronized private fun checkUpdate() {  // status() runs on several request threads
        if (!config.updateCheck || !updateSupported || updateChecking || System.currentTimeMillis() < updateDue ||
            BridgeService.status.monitoring || simulator) return
        updateChecking = true
        Thread({
            val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)
            updateDue = try {
                // The default network, not the receiver WiFi: that has no internet.
                val c = URL(RELEASES).openConnection() as HttpURLConnection
                val release = try {
                    c.connectTimeout = 5000; c.readTimeout = 5000
                    c.setRequestProperty("Accept", "application/vnd.github+json")
                    JSONObject(String(NativeDiagnostics.boundedRead(c.inputStream, 256 * 1024), Charsets.UTF_8))
                } finally { c.disconnect() }
                val tag = release.getString("tag_name"); val url = release.getString("html_url")
                update = if (newer(tag, SbCore.version()) && url.startsWith("https://github.com/"))
                    JSONObject().put("version", tag.removePrefix("v")).put("url", url) else null
                prefs.edit().putLong("updateChecked", System.currentTimeMillis()).putString("update", update?.toString()).apply()
                System.currentTimeMillis() + UPDATE_INTERVAL_MS
            } catch (_: Exception) {
                System.currentTimeMillis() + UPDATE_RETRY_MS  // offline or GitHub unavailable: try again later
            } finally { updateChecking = false }
        }, "update-check").apply { isDaemon = true; start() }
    }
    private lateinit var directory: File
    private var server: ServerSocket? = null
    /** 8768 when free; otherwise a port the system picks, so another app holding 8768 cannot block the UI. */
    @Volatile var port = 8768
        private set
    val secret = UUID.randomUUID().toString()
    private val tools = Executors.newSingleThreadExecutor()
    private val commands = ReentrantLock()
    private val requests = ThreadPoolExecutor(3, 3, 0L, TimeUnit.MILLISECONDS, ArrayBlockingQueue<Runnable>(12))
    @Volatile var config = Settings()
    @Volatile private var job = JSONObject().put("state", "idle")
    @Volatile private var device: JSONObject? = null
    @Volatile private var observation: JSONObject? = null
    @Volatile private var requested: Boolean? = null
    @Volatile var simulator = false
        private set
    @Volatile private var simPublic = false
    @Volatile private var simGps = true
    @Volatile private var simUat = true
    @Volatile private var simWeather = true
    @Volatile private var simThread: Thread? = null
    private var realConfig = Settings()
    private val files = JSONObject()
    private var receiverLogs = JSONArray()
    private var capture: BufferedWriter? = null
    private var captureToken: String? = null
    private var captureBytes = 0
    private var captureEnd = 0L
    @Volatile private var captureError: String? = null
    @Volatile private var observing = false
    /** Exit was requested: a running job must not start the receiver listener again. */
    @Volatile private var closing = false
    /** Guards closing together with listener startup, so a job cannot start it after Exit. */
    private val lifecycle = Any()

    /** Exit was requested; a job stops at its next stage. Not a failure to continue past. */
    private class Closing : IllegalStateException("Sentry Bridge was closed.")

    private fun ensureOpen() { if (closing) throw Closing() }
    val recording: Boolean get() = synchronized(files) { capture != null }
    val powerWarning: String? get() = if (device?.optJSONObject("settings")?.optJSONObject("Power")?.opt("powerSavings") == true)
        BridgeService.POWER_SAVINGS_WARNING else null
    /** CO at or above the receiver's reported alarm level (50 ppm when it reported none), from fresh /stats readings;
     *  the same text as the screen. */
    val coWarning: String? get() {
        val co = sensors?.takeIf { SystemClock.elapsedRealtime() - sensorsAt <= 5000 }?.optLong("coPpm", -1) ?: return null
        val reported = device?.optJSONObject("settings")?.optDouble("coAlarmLevel")?.takeIf { it > 0 }?.toLong()
        val level = reported ?: 50L
        if (co < level) return null
        // Only a level the receiver reported is called the receiver's.
        return "Carbon monoxide $co ppm, at or above " + if (reported != null) "the receiver's alarm level ($level ppm)." else "50 ppm."
    }

    @Synchronized fun start(ctx: Context) {
        if (server != null) return
        context = ctx.applicationContext
        directory = File(context.filesDir, "gui").apply { mkdirs() }
        config = Settings.load(context)
        val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)
        update = prefs.getString("update", null)?.let { runCatching { JSONObject(it) }.getOrNull() }
        updateDue = prefs.getLong("updateChecked", 0L) + UPDATE_INTERVAL_MS
        if (config.routes == null && prefs.contains("efbPort")) {
            val r = JSONObject().put("host", "android")
                .put("topology", if (config.efbOnThisDevice) "same" else "network")
                // v0.1 "other device" mode broadcast to 192.168.4.255:4000. Output is unicast now, so that route
                // keeps port 4000 but needs the EFB device's address before the bridge can start.
                .put("address", if (config.efbOnThisDevice) "127.0.0.1" else "")
            config = config.copy(routes = r.toString(), efbPort = if (config.efbOnThisDevice) config.efbPort else 4000)
            config.save(context)
        } else if (config.routes != prefs.getString("routes", null)) {
            // Settings.load upgraded a v0.1.6 multi-EFB route (and logged what it dropped): keep it, so the
            // upgrade and its log line happen once.
            config.save(context)
        }
        runCatching {
            val saved = JSONObject(File(directory, "files.json").readText())
            for (id in saved.keys()) {
                val item = saved.getJSONObject(id)
                if (id.matches(Regex("[a-f0-9]{32}")) && item.getString("name").matches(Regex("[a-zA-Z0-9_.-]+")) &&
                    item.optBoolean("complete", true) && File(directory, "$id-${item.getString("name")}").isFile) files.put(id, item)
            }
        }
        val listening = ServerSocket()
        try { listening.bind(InetSocketAddress("127.0.0.1", 8768)) }
        catch (_: IOException) { listening.bind(InetSocketAddress("127.0.0.1", 0)) }
        port = listening.localPort
        watchWifi()
        server = listening
        Thread({
            while (!listening.isClosed) {
                val socket = try { listening.accept() } catch (_: IOException) { if (listening.isClosed) break else continue }
                try { requests.execute { socket.use { serve(it) } } }
                catch (_: RejectedExecutionException) { socket.close() }
            }
        }, "gui-http").apply { isDaemon = true; start() }
    }

    private fun serve(socket: Socket) {
        socket.soTimeout = 15000
        val input = socket.getInputStream().buffered()
        fun line(): String {
            val out = StringBuilder()
            while (true) {
                val c = input.read()
                if (c == -1 || c == 10) break
                if (c != 13) out.append(c.toChar())
                require(out.length < 8192) { "HTTP header too long." }
            }
            return out.toString()
        }
        val output = socket.getOutputStream()
        var isPost = false
        var declaredBody = 0
        var bodyRead = false
        // Read a rejected POST's body before replying: closing the socket while it is still arriving
        // resets the connection and the client loses the reply. Bodies over 64 KiB are not read.
        fun discardBody() {
            if (!isPost || bodyRead) return
            bodyRead = true
            if (declaredBody !in 1..64 * 1024) return
            val skip = ByteArray(declaredBody)
            var at = 0
            while (at < declaredBody) { val n = runCatching { input.read(skip, at, declaredBody - at) }.getOrDefault(-1); if (n <= 0) break; at += n }
        }
        fun reply(code: Int, body: ByteArray, type: String = "application/json", cookie: Boolean = false) {
            if (code >= 400) discardBody()
            val headers = "HTTP/1.1 $code Response\r\nContent-Type: $type\r\nContent-Length: ${body.size}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'\r\n" +
                if (cookie) "Set-Cookie: sb=$secret; HttpOnly; SameSite=Strict; Path=/\r\n\r\n" else "\r\n"
            output.write(headers.toByteArray()); output.write(body)
        }
        try {
            val first = line().split(' ')
            require(first.size == 3) { "Invalid HTTP request." }
            val headers = HashMap<String, String>()
            var total = 0
            while (true) {
                val row = line()
                if (row.isEmpty()) break
                total += row.length
                require(total < 16384) { "HTTP headers too long." }
                val split = row.indexOf(':')
                require(split > 0)
                headers[row.substring(0, split).lowercase()] = row.substring(split + 1).trim()
            }
            isPost = first[0] == "POST"
            declaredBody = headers["content-length"]?.toIntOrNull() ?: 0
            // Constant-time comparison, as on desktop (hmac.compare_digest).
            fun matches(value: String?, expected: String) =
                value != null && java.security.MessageDigest.isEqual(value.toByteArray(), expected.toByteArray())
            val authenticated = matches(headers["authorization"], "Bearer $secret") ||
                headers["cookie"]?.split(';')?.any { matches(it.trim(), "sb=$secret") } == true
            if (!authenticated || headers["host"] != "127.0.0.1:$port" ||
                headers["origin"]?.let { it != "http://127.0.0.1:$port" } == true) {
                reply(403, "{\"error\":\"App session required.\"}".toByteArray()); return
            }
            val path = first[1]
            if (first[0] == "GET") {
                when {
                    path == "/api/status" -> reply(200, status().toString().toByteArray())
                    path.startsWith("/api/files/") -> {
                        val id = path.substringAfterLast('/')
                        val data = file(id)
                        output.write(("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=\"${fileName(id)}\"\r\nContent-Length: ${data.length()}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n").toByteArray())
                        data.inputStream().use { it.copyTo(output, 65536) }
                    }
                    path in listOf("/", "/app.html", "/app.css", "/app.js", "/privacy.html", "/legal.html", "/legal.js") -> {
                        val name = if (path == "/") "app.html" else path.drop(1)
                        val type = when (name) { "app.css" -> "text/css"; "app.js", "legal.js" -> "text/javascript"; else -> "text/html" }
                        reply(200, context.assets.open(name).use { it.readBytes() }, "$type; charset=utf-8", true)
                    }
                    else -> reply(404, "{}".toByteArray())
                }
            } else if (first[0] == "POST") {
                val length = headers["content-length"]?.toIntOrNull() ?: 0
                val limit = if (path == "/api/actions/upload") 12 * 1024 * 1024 else 8192
                require(length in 1..limit && headers["content-type"]?.substringBefore(';') == "application/json") { "Invalid request or upload exceeds 8 MiB on Android." }
                val bytes = ByteArray(length)
                bodyRead = true
                var at = 0
                while (at < length) { val n = input.read(bytes, at, length - at); require(n > 0); at += n }
                val request = JSONObject(bytes.toString(Charsets.UTF_8))
                val result = if (path == "/api/validate") validateRoutes(request) else {
                    require(path.startsWith("/api/actions/")) { "Unknown action." }
                    action(path.substringAfterLast('/'), request)
                }
                reply(200, result.toString().toByteArray())
            } else reply(405, "{}".toByteArray())
        } catch (error: Exception) {
            runCatching { reply(400, JSONObject().put("error", error.message ?: "Operation failed.").toString().toByteArray()) }
        }
    }

    /** WiFi networks seen by the callback registered in start(), oldest first. */
    private val wifiNetworks = java.util.concurrent.CopyOnWriteArrayList<Network>()

    private fun watchWifi() {
        // The Sentry's WiFi has no internet, so INTERNET must not be required.
        val request = NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build()
        context.getSystemService(ConnectivityManager::class.java).registerNetworkCallback(request, object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) { wifiNetworks.remove(network); wifiNetworks.add(network) }
            override fun onLost(network: Network) { wifiNetworks.remove(network) }
        })
    }

    /** The WiFi network whose subnet contains the receiver IP; otherwise the newest WiFi network. */
    fun wifi(): Network? {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        val networks = wifiNetworks.reversed()
        val target = config.sentryIp.takeIf { it.isNotEmpty() }?.let { runCatching { NativeDiagnostics.ipv4(it, false).address }.getOrNull() }
        return networks.firstOrNull { network ->
            target != null && cm.getLinkProperties(network)?.linkAddresses.orEmpty().any {
                NativeDiagnostics.sameIpv4Subnet(it.address.address, it.prefixLength, target)
            }
        } ?: networks.firstOrNull()
    }

    fun http(path: String, body: JSONObject? = null, limit: Int = 2 * 1024 * 1024): ByteArray {
        ensureOpen()  // never send a receiver request, above all a mode change, after Exit
        require(config.sentryIp.isNotEmpty()) { "Set the receiver IP in Settings." }
        if (simulator) return when (path) {
            "/?action=get" -> "{\"device\":\"Simulator\",\"firmware\":\"synthetic\"}".toByteArray()
            "/settings/?action=get" -> "{\"Power\":{\"powerSavings\":false}}".toByteArray()
            "/settings/?action=set" -> { simPublic = body!!.getBoolean("publicGDL90"); "OK".toByteArray() }
            "/data/" -> "<a href=\"simulator.csv\">simulator.csv</a>".toByteArray()
            "/data/simulator.csv" -> "time,battery\n0,100\n".toByteArray()
            else -> throw IOException("Simulator endpoint is unavailable.")
        }
        val network = wifi() ?: throw IOException("Join the Sentry WiFi first.")
        val connection = network.openConnection(URL("http://${config.sentryIp}:${config.httpPort}$path"), Proxy.NO_PROXY) as HttpURLConnection
        try {
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 3000
            connection.readTimeout = 5000
            connection.setRequestProperty("Accept-Encoding", "gzip")
            if (body != null) {
                connection.requestMethod = "POST"; connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            require(connection.responseCode in 200..299) { "Receiver request failed (HTTP ${connection.responseCode})." }
            val stream = if (connection.getHeaderField("Content-Encoding") == "gzip") GZIPInputStream(connection.inputStream) else connection.inputStream
            return stream.use { NativeDiagnostics.boundedRead(it, limit) }.also { ensureOpen() }
        } finally { connection.disconnect() }
    }

    private fun readDevice(): JSONObject {
        val previousWarning = powerWarning
        val value = JSONObject().put("status", JSONObject(http("/?action=get").toString(Charsets.UTF_8)))
            .put("settings", JSONObject(http("/settings/?action=get").toString(Charsets.UTF_8)))
        device = value
        if (powerWarning != null && powerWarning != previousWarning) BridgeService.log("WARNING: $powerWarning")
        return value
    }

    private fun downloadLog(name: String, output: java.io.OutputStream, limit: Int): JSONObject {
        ensureOpen()  // this path opens its own connection, so check before any network access
        val path = "/data/" + URLEncoder.encode(name, "UTF-8").replace("+", "%20")
        if (simulator) {
            val data = http(path, limit = limit); output.write(data)
            return JSONObject().put("name", name).put("bytes", data.size).put("truncated", false)
        }
        val n = wifi() ?: throw IOException("Join the Sentry WiFi first.")
        val c = n.openConnection(URL("http://${config.sentryIp}:${config.httpPort}$path"), Proxy.NO_PROXY) as HttpURLConnection
        try {
            c.instanceFollowRedirects = false; c.connectTimeout = 3000; c.readTimeout = 10000
            c.setRequestProperty("Accept-Encoding", "gzip")
            require(c.responseCode in 200..299) { "Receiver log request failed (HTTP ${c.responseCode})." }
            val stream = if (c.getHeaderField("Content-Encoding") == "gzip") GZIPInputStream(c.inputStream) else c.inputStream
            var size = 0; var truncated = false
            stream.use { source ->
                val buffer = ByteArray(65536)
                while (true) {
                    val count = source.read(buffer, 0, minOf(buffer.size, limit - size + 1))
                    ensureOpen()  // after every blocking read, including the one that hits EOF
                    if (count < 0) break
                    val written = minOf(count, limit - size)
                    output.write(buffer, 0, written); size += written
                    if (count > written) { truncated = true; break }
                }
            }
            return JSONObject().put("name", name).put("bytes", size).put("truncated", truncated)
        } finally { c.disconnect() }
    }

    private fun submit(name: String, operation: () -> JSONObject): JSONObject {
        synchronized(tools) {
            require(job.optString("state") != "running") { "Wait for the current operation." }
            job = JSONObject().put("state", "running").put("action", name)
        }
        tools.execute {
            val before = synchronized(files) { files.keys().asSequence().toSet() }
            try { job = JSONObject().put("state", "done").put("action", name).put("result", operation()) }
            catch (error: Exception) {
                discardNewFiles(before)
                BridgeService.log("$name: ${error.message}")
                job = JSONObject().put("state", "failed").put("action", name).put("error", error.message)
            }
        }
        return JSONObject().put("queued", true)
    }

    @Volatile private var contacting = false
    @Volatile private var contactOk: Boolean? = null
    /** Called on the bridge thread: one read-only keepalive at a time, outside the tool job slot so it never
     *  blocks an operation. False while the previous one runs. Logs only when the result changes. */
    fun keepalive(): Boolean {
        watchSensors()
        watchEfb()
        if (contacting) return false
        contacting = true
        Thread({
            val result = try { runCatching { http("/?action=get", limit = 65536) } } finally { contacting = false }
            val ok = result.isSuccess
            if (ok != contactOk && !closing) {
                contactOk = ok
                BridgeService.log("Keepalive: receiver status ${if (ok) "OK" else result.exceptionOrNull()?.message}")
            }
        }, "sentry-keepalive").apply { isDaemon = true; start() }
        return true
    }

    @Volatile private var sensors: JSONObject? = null
    @Volatile private var sensorsAt = 0L
    private var sensorThread: Thread? = null
    /** While forwarding, listen to the receiver's /stats WebSocket for battery, CO and temperature. Read-only, like its
     *  own statistics page. The static simulator has no WebSocket, so it shows none. */
    private fun watchSensors() {
        if (simulator || sensorThread?.isAlive == true) return
        sensorThread = Thread({
            val forwarding = { BridgeService.status.running && !closing }
            while (forwarding()) {
                runCatching {
                    val network = wifi() ?: throw IOException("Join Sentry WiFi first.")
                    // A receiver that restarts leaves the old connection open and silent: reconnect after 10 s without a message.
                    var heard = SystemClock.elapsedRealtime()
                    NativeDiagnostics.websocket(network, config, "/stats", 1e7, null, onText = { text ->
                        heard = SystemClock.elapsedRealtime()
                        val values = NativeDiagnostics.sensorValues(text)
                        if (values.length() > 0) { sensors = values; sensorsAt = heard }
                    }) {
                        if (!forwarding()) throw IOException("Stopped.")
                        if (SystemClock.elapsedRealtime() - heard > 10_000) throw IOException("No statistics for 10 s.")
                    }
                }  // an older firmware without /stats, the receiver is away or restarted: retry
                if (forwarding()) Thread.sleep(5000)
            }
            sensors = null
        }, "sentry-sensors").apply { isDaemon = true; start() }
    }

    private var followThread: Thread? = null
    private var followGeneration = -1
    /** While forwarding to an EFB on another device, check it still answers (tools/gui_server.py _follow_efb). When an
     *  EFB that answered before stops answering twice in a row, sweep the receiver's WiFi and switch to the one other
     *  device that answers; with none or several, only log. One follower per Start (BridgeService.generation): an older
     *  one stops at its next check and can never change a later run's destination. It acts only while receiver data
     *  arrives, so the phone is on the receiver's network. */
    private fun watchEfb() {
        val generation = BridgeService.generation
        if (simulator || followThread?.isAlive == true && followGeneration == generation) return
        followGeneration = generation
        followThread = Thread({
            val forwarding = { BridgeService.status.running && !closing && BridgeService.generation == generation }
            var seen = false; var misses = 0; var reported: String? = null
            while (forwarding()) {
                for (i in 0 until 30) { if (!forwarding()) return@Thread; Thread.sleep(1000) }
                if (BridgeService.status.lastRxAgeS !in 0.0..10.0) continue
                val current = BridgeService.followed ?: BridgeService.destination ?: continue
                val network = wifi() ?: continue
                if (NativeDiagnostics.answers(network, current.address)) { seen = true; misses = 0; reported = null; continue }
                misses++
                if (!seen || misses < 2 || !forwarding()) continue  // never answered (a firewall, perhaps) or only once
                val found = runCatching { sweepDevices(network, current.port) { !forwarding() } }.getOrNull() ?: continue
                val old = current.address.hostAddress!!
                val target = NativeDiagnostics.followChoice(found, old)
                if (target == old) { seen = true; misses = 0; reported = null; continue }  // it answered again
                if (target == null) {
                    if (reported != old && forwarding()) {
                        reported = old
                        BridgeService.log("The EFB at $old stopped answering. Devices on the receiver's WiFi: " +
                            "${found.joinToString(", ").ifEmpty { "none" }}. Choose one with Find EFB.")
                    }
                    continue
                }
                synchronized(BridgeService.routeLock) {  // the run and destination this decision was made for, or nothing
                    if (!forwarding() || (BridgeService.followed ?: BridgeService.destination) != current) return@Thread
                    BridgeService.followed = InetSocketAddress(target, current.port)
                    config.routes?.let { routes ->
                        val route = JSONObject(routes)
                        if (route.optString("address") == old) { config = config.copy(routes = route.put("address", target).toString()); save() }
                    }
                }
                BridgeService.log("The EFB at $old stopped answering; now sending to $target, the only other device on the receiver's WiFi.")
                misses = 0; reported = null
            }
        }, "sentry-follow").apply { isDaemon = true; start() }
    }

    /** Called on the bridge thread. False when another operation is running; the caller keeps the request. */
    fun autoEnable(): Boolean {
        if (!commands.tryLock()) return false
        return try { runCatching { submit("Automatic restoration") { autoRestore() } }.isSuccess }
        finally { commands.unlock() }
    }
    private fun autoRestore(): JSONObject {
        val before = synchronized(files) { files.keys().asSequence().toSet() }
        val saved = try {
            // Keep room for the new file: old automatic backups must not fill the 100-file list.
            pruneAutoBackups(AUTO_BACKUPS_KEPT - 1)
            backup(AUTO_BACKUP)
        } catch (error: Closing) {
            throw error
        } catch (error: Exception) {
            discardNewFiles(before)
            BridgeService.log("Automatic backup unavailable: ${error.message}; continuing mode recovery.")
            JSONObject()
        }
        ensureOpen()
        val restored = compatibility(true, false, true, 3.0)
        for (key in saved.keys()) restored.put(key, saved.get(key))
        return restored
    }
    private fun pruneAutoBackups(keep: Int): Unit = synchronized(files) {
        val automatic = files.keys().asSequence().toList()
            .filter { files.getJSONObject(it).let { item -> item.getString("name") == AUTO_BACKUP && item.optBoolean("complete", true) } }
            .sortedBy { files.getJSONObject(it).optLong("created", 0L) }
        val removed = automatic.take(maxOf(0, automatic.size - keep))
        for (id in removed) {
            val path = File(directory, "$id-$AUTO_BACKUP")
            files.remove(id)
            if (path.exists() && !path.delete()) BridgeService.log("Old automatic backup could not be removed.")
        }
        if (removed.isNotEmpty()) File(directory, "files.json").writeText(files.toString())
        Unit
    }
    private fun discardNewFiles(before: Set<String>) = synchronized(files) {
        for (id in files.keys().asSequence().toList().filter { it !in before }) {
            if (files.getJSONObject(id).optBoolean("complete", true)) continue
            val path = File(directory, "$id-${files.getJSONObject(id).getString("name")}")
            files.remove(id)
            if (path.exists() && !path.delete()) BridgeService.log("Incomplete file could not be removed.")
        }
        runCatching { File(directory, "files.json").writeText(files.toString()) }
            .onFailure { BridgeService.log("File index could not be saved: ${it.message}") }
        Unit
    }
    fun streamStarted() {
        if (commands.tryLock()) try { runCatching { submit("Read receiver") { readDevice() } } }
        finally { commands.unlock() }
    }

    private fun service(action: String) {
        val intent = Intent(context, BridgeService::class.java).setAction(action)
        if (action == BridgeService.ACTION_STOP) context.startService(intent) else context.startForegroundService(intent)
    }

    private fun monitor() {
        if (!BridgeService.status.monitoring) {
            synchronized(lifecycle) { ensureOpen(); service(BridgeService.ACTION_MONITOR) }
            val deadline = SystemClock.elapsedRealtime() + 3000
            while (!BridgeService.status.monitoring && SystemClock.elapsedRealtime() < deadline) Thread.sleep(25)
            require(BridgeService.status.monitoring) { BridgeService.status.error ?: "Receiver listener could not start." }
        }
    }

    private fun check(seconds: Double): JSONObject {
        observing = true
        val temporary = !BridgeService.status.monitoring
        try {
            monitor()
            val before = BridgeService.status.frames
            Thread.sleep((seconds * 1000).toLong())
            require(BridgeService.status.monitoring) { BridgeService.status.error ?: "Receiver listener stopped during the check." }
            val after = BridgeService.status.frames
            val counts = JSONObject()
            for (i in after.indices) if (after[i] > before.getOrElse(i) { 0 }) counts.put(i.toString(), after[i] - before.getOrElse(i) { 0 })
            val mode = NativeDiagnostics.describeMode(counts)
            return JSONObject().put("mode", mode).put("frames", counts).put("checkedAt", System.currentTimeMillis() / 1000.0).also { observation = it }
        } finally { observing = false; if (temporary && !recording) service(BridgeService.ACTION_STOP) }
    }

    private fun backup(name: String = "sentry-backup.zip"): JSONObject {
        val value = readDevice()
        val id = artifact(name, "backup")
        ZipOutputStream(file(id).outputStream()).use { out ->
            for (key in listOf("status", "settings")) {
                out.putNextEntry(ZipEntry("sentry-$key.json")); out.write(value.getJSONObject(key).toString(2).toByteArray()); out.closeEntry()
            }
        }
        completeFile(id)
        return JSONObject().put("backup", id)
    }

    private fun compatibility(enabled: Boolean, backup: Boolean, verify: Boolean, seconds: Double): JSONObject {
        val result = if (enabled && backup) backup() else JSONObject()
        ensureOpen()
        http("/settings/?action=set", JSONObject().put("publicGDL90", enabled))
        requested = enabled; observation = null
        result.put("settings", readDevice().getJSONObject("settings"))
        if (verify) {
            val mode = check(seconds)
            result.put("verification", mode)
            if (!mode.getString("mode").startsWith(if (enabled) "PUBLIC" else "ENCRYPTED")) result.put("warning", "Setting sent; expected mode was not confirmed. Check WiFi and receiver power.")
        } else result.put("verification", "Skipped")
        return result
    }

    private fun artifact(name: String, kind: String): String = synchronized(files) {
        require(files.length() < 100) { "Delete unused files in Diagnostics." }
        val token = UUID.randomUUID().toString().replace("-", "")
        files.put(token, JSONObject().put("name", name).put("kind", kind).put("complete", false).put("created", System.currentTimeMillis()))
        File(directory, "files.json").writeText(files.toString())
        token
    }
    private fun completeFile(id: String) = synchronized(files) {
        files.getJSONObject(id).put("complete", true)
        File(directory, "files.json").writeText(files.toString())
    }
    fun fileName(id: String): String = synchronized(files) { require(files.has(id)) { "Select a listed file." }; files.getJSONObject(id).getString("name") }
    fun file(id: String): File {
        require(!(id == captureToken && recording)) { "Stop recording before using this capture." }
        return File(directory, "$id-${fileName(id)}")
    }

    fun record(data: ByteArray, src: String) = synchronized(files) {
        val out = capture ?: return@synchronized
        val row = JSONObject().put("t", System.currentTimeMillis() / 1000.0).put("src", src).put("hex", NativeDiagnostics.hex(data)).toString() + "\n"
        if (captureBytes + row.length > 32 * 1024 * 1024 || captureEnd != 0L && SystemClock.elapsedRealtime() >= captureEnd) stopCapture()
        else try { out.write(row); captureBytes += row.length }
        catch (error: IOException) { captureError = "Recording stopped: ${error.message}"; stopCapture(); BridgeService.log(captureError!!) }
    }
    private fun stopCapture() = synchronized(files) {
        val out = capture; capture = null
        try { out?.close() } catch (error: IOException) { captureError = "Recording could not be saved: ${error.message}"; BridgeService.log(captureError!!) }
    }
    fun tick() {
        synchronized(files) { if (captureEnd != 0L && SystemClock.elapsedRealtime() >= captureEnd) stopCapture() }
        if (!recording && !observing && BridgeService.status.monitoring && !BridgeService.status.running) service(BridgeService.ACTION_STOP)
    }
    fun stopped() { stopCapture() }

    private fun save() { if (!simulator) config.save(context) }
    private fun numeric(j: JSONObject, key: String, default: Double, low: Double, high: Double, integer: Boolean = false): Double {
        val v = if (j.has(key)) j.get(key) else default
        require(v is Number && v.toDouble().isFinite() && v.toDouble() in low..high && (!integer || v.toDouble() % 1.0 == 0.0)) { "$key must be between $low and $high." }
        return v.toDouble()
    }
    private fun validateSettings(j: JSONObject): Settings {
        val keys = config.gui().keys().asSequence().toSet()
        require(j.keys().asSequence().all { it in keys }) { "Unknown receiver setting." }
        val merged = Settings().gui()
        for (key in j.keys()) merged.put(key, j.get(key))
        val ip = merged.getString("sentryIp")
        if (ip.isNotEmpty()) NativeDiagnostics.ipv4(ip, false)
        for (key in listOf("listenPort", "efbPort", "httpPort")) numeric(merged, key, 4000.0, 1.0, 65535.0, true)
        numeric(merged, "announcePort", 63093.0, 0.0, 65535.0, true)
        require(merged.getInt("listenPort") != merged.getInt("announcePort")) { "Discovery and receiver ports must differ." }
        for (key in listOf("autoRestore", "relay", "updateCheck")) require(merged.get(key) is Boolean) { "$key must be on or off." }
        require(merged.getString("ownshipHex").matches(Regex("([a-fA-F0-9]{6})?"))) { "Ownship ICAO must be six hex digits or blank." }
        merged.put("ownshipHex", merged.getString("ownshipHex").uppercase())
        merged.optJSONArray("reference")?.let { r ->
            require(r.length() == 2); require(r.get(0) is Number && r.get(1) is Number)
            require(r.getDouble(0).isFinite() && r.getDouble(0) in -90.0..90.0 && r.getDouble(1).isFinite() && r.getDouble(1) in -180.0..180.0)
        } ?: require(merged.isNull("reference")) { "Reference needs latitude and longitude." }
        numeric(merged, "staleSeconds", 15.0, 1.0, 60.0)
        numeric(merged, "intervalSeconds", 1.0, .1, 10.0)
        numeric(merged, "statsSeconds", 10.0, 1.0, 600.0)
        return Settings.fromGui(merged, config.routes)
    }

    /** The one GDL90 route (tools/routes.py validate_route): This device or Other device, on the EFB port from Settings. */
    fun validateRoutes(j: JSONObject, settings: Settings = config): JSONObject {
        require(j.optString("host") == "android") { "Android bridge host required." }
        val topology = j.optString("topology")
        require(topology in listOf("same", "network")) { "Choose where the EFB runs." }
        val port = settings.efbPort
        val shared = topology == "same" && port == settings.listenPort
        // On this device the bridge's discovery listener would take the output; another device's port is its own.
        require(topology != "same" || port != settings.announcePort) { "On this device the EFB port must differ from the discovery port." }
        val address = if (topology == "same") "127.0.0.1" else NativeDiagnostics.ipv4(j.optString("address").trim(), true).hostAddress!!
        if (!shared) NativeDiagnostics.checkEndpoint(InetAddress.getByName(address), port, settings.listenPort)
        val warnings = JSONArray()
        if (shared) warnings.put("Shares UDP $port with the EFB on this device. One EFB at a time receives it; close other EFBs. " +
            "The EFB must allow port sharing; otherwise set another EFB port in Settings and in the EFB.")
        return JSONObject().put("routes", JSONArray().put(JSONObject().put("address", address).put("port", port).put("shared", shared)))
            .put("warnings", warnings).put("displayVerified", false).put("appliedToBridge", false)
    }

    fun action(name: String, j: JSONObject): JSONObject = commands.withLock {
        // Stop and Exit never wait for a receiver job.
        require(job.optString("state") != "running" || name == "stop" || name == "quit") { "Wait for the current operation." }
        if (name != "quit") closing = false  // the controls are in use again
        val before = synchronized(files) { files.keys().asSequence().toSet() }
        try { performAction(name, j) } catch (error: Exception) { discardNewFiles(before); throw error }
    }
    private fun performAction(name: String, j: JSONObject): JSONObject {
        require(job.optString("state") != "running" || name == "stop" || name == "quit") { "Wait for the current operation." }
        when (name) {
            "stop", "quit" -> {
                // Exit: set closing and stop the listener together, so a running job cannot restart it.
                synchronized(lifecycle) { if (name == "quit") closing = true; service(BridgeService.ACTION_STOP) }
                stopCapture(); return JSONObject().put(if (name == "quit") "exiting" else "stopped", true)
            }
            "settings" -> {
                require(!simulator) { "Turn off the simulator before saving receiver settings." }; require(!BridgeService.status.monitoring)
                val next = validateSettings(j)
                // This device's route depends only on the ports: keep a save from making it unstartable.
                config.routes?.let { r -> runCatching { JSONObject(r) }.getOrNull() }?.takeIf { it.optString("topology") == "same" }?.let { validateRoutes(it, next) }
                config = next; save(); device = null; observation = null; requested = null; return JSONObject().put("saved", true)
            }
            "start" -> {
                require(!BridgeService.status.monitoring) { "Stop the bridge or capture first." }
                val route = validateRoutes(j).getJSONArray("routes").getJSONObject(0)
                require(!simulator || route.getString("address") == "127.0.0.1") { "Simulator output is limited to this device." }
                config = config.copy(routes = j.toString(), efbOnThisDevice = j.getString("topology") == "same")
                save(); service(BridgeService.ACTION_START); return JSONObject().put("started", true)
            }
            "capture-start" -> {
                require(!recording) { "A capture is already running." }
                val seconds = numeric(j, "seconds", 0.0, 0.0, 3600.0)
                val id = artifact("capture.jsonl", "capture")
                synchronized(files) { capture = file(id).bufferedWriter(); captureToken = id; captureBytes = 0; captureError = null; captureEnd = if (seconds == 0.0) 0 else SystemClock.elapsedRealtime() + (seconds * 1000).toLong() }
                try { monitor() } catch (error: Exception) { stopCapture(); throw error }
                completeFile(id)
                return JSONObject().put("capture", id)
            }
            "capture-stop" -> { stopCapture(); if (!BridgeService.status.running) service(BridgeService.ACTION_STOP); return JSONObject().put("stopped", true) }
            "upload" -> {
                val name = File(j.getString("name")).name
                val suffix = name.substringAfterLast('.').lowercase()
                require(suffix in listOf("jsonl", "pcap", "pcapng")) { "Import JSONL, pcap or pcapng." }
                val bytes = Base64.decode(j.getString("data"), Base64.DEFAULT)
                require(bytes.size in 1..8 * 1024 * 1024) { "Android imports are limited to 8 MiB." }
                val id = artifact(name.substringBeforeLast('.').replace(Regex("[^A-Za-z0-9_-]"), "_").take(80) + ".$suffix", "capture")
                file(id).writeBytes(bytes); completeFile(id); return JSONObject().put("capture", id)
            }
            "delete-file" -> { val id = j.getString("id"); file(id).delete(); synchronized(files) { files.remove(id); File(directory, "files.json").writeText(files.toString()) }; return JSONObject().put("deleted", true) }
            "read" -> return submit("Read receiver") { readDevice().put("verification", check(3.0)) }
            "backup" -> return submit("Backup receiver") { backup() }
            "check" -> { val seconds = numeric(j, "seconds", 8.0, .2, 30.0); return submit("Check mode") { check(seconds) } }
            "compatibility" -> {
                for (key in listOf("enabled", "backup", "verify")) if (j.has(key)) require(j.get(key) is Boolean)
                val enabled = j.getBoolean("enabled"); val seconds = numeric(j, "seconds", 3.0, .2, 30.0)
                return submit("Compatibility Mode") { compatibility(enabled, j.optBoolean("backup", true), j.optBoolean("verify", true), seconds) }
            }
            "logs" -> return submit("List receiver logs") {
                val html = http("/data/").toString(Charsets.UTF_8)
                val names = Regex("href\\s*=\\s*[\"']([^\"']+)[\"']", RegexOption.IGNORE_CASE).findAll(html)
                    .mapNotNull { NativeDiagnostics.logName(it.groupValues[1]) }.distinct().take(100).toList()
                receiverLogs = JSONArray(names); JSONObject().put("logs", receiverLogs)
            }
            "download-logs" -> return submit("Download receiver logs") {
                val selected = j.getString("name")
                val names = (0 until receiverLogs.length()).map { receiverLogs.getString(it) }.filter { selected == "all" || it == selected }
                require(names.isNotEmpty() && names.size <= 20) { "Refresh the list and select at most 20 logs." }
                val limit = (numeric(j, "maxMb", 20.0, 1.0, 20.0, true) * 1024 * 1024).toInt()
                val id = artifact("sentry-logs.zip", "logs")
                val info = JSONArray()
                ZipOutputStream(file(id).outputStream()).use { out -> for ((i, log) in names.withIndex()) {
                    // ':' (allowed in a listed name) would break extracting the ZIP on Windows.
                    out.putNextEntry(ZipEntry("${i + 1}-${log.replace(':', '_')}")); info.put(downloadLog(log, out, limit)); out.closeEntry()
                } }
                ensureOpen()  // a cancelled download stays incomplete and is discarded
                completeFile(id)
                JSONObject().put("download", id).put("files", info)
            }
            "analyze" -> {
                val f = file(j.getString("capture")); val p = numeric(j, "port", 4000.0, 1.0, 65535.0, true).toInt()
                val samples = numeric(j, "samples", 3.0, 0.0, 10.0, true).toInt()
                return submit("Analyze capture") { report("capture-report.txt", NativeDiagnostics.analyze(f, p, samples)) }
            }
            "weather" -> {
                val source = file(j.getString("source")); val output = file(j.getString("output"))
                val ip = numeric(j, "inputPort", 4000.0, 1.0, 65535.0, true).toInt(); val op = numeric(j, "outputPort", 4001.0, 1.0, 65535.0, true).toInt()
                require(source != output || source.extension != "jsonl" && ip != op) { "Use separate JSONL recordings or distinct pcap ports." }
                return submit("Compare weather") {
                    val text = NativeDiagnostics.weather(source, output, ip, op)
                    val code = when (JSONObject(text).getString("status")) { "passed" -> 0; "failed" -> 1; else -> 2 }
                    report("weather-report.txt", text).put("exitCode", code)
                }
            }
            "discover" -> {
                val seconds = numeric(j, "seconds", 6.0, 1.0, 30.0)
                return submit("Find EFB") { discoverEfbs(seconds) }
            }
            "websocket" -> {
                val path = j.optString("path", "/"); require(path.startsWith('/') && path.length <= 256 && path.none { it in "\r\n " })
                val seconds = numeric(j, "seconds", 10.0, 1.0, 60.0)
                return submit("Probe WebSocket") {
                    require(!simulator) { "The Android static simulator does not provide a WebSocket endpoint." }
                    ensureOpen()  // before any WiFi or network access
                    val id = artifact("websocket.jsonl", "websocket")
                    val result = report("websocket-report.txt", NativeDiagnostics.websocket(wifi() ?: throw IOException("Join Sentry WiFi first."), config, path, seconds, file(id), onText = {}) { ensureOpen() })
                    completeFile(id)
                    result.put("recording", id)
                }
            }
            "simulator" -> {
                require(!BridgeService.status.monitoring) { "Stop the bridge and capture first." }
                require(j.get("enabled") is Boolean)
                for (key in listOf("gps", "uat", "weather")) if (j.has(key)) require(j.get(key) is Boolean)
                val enabled = j.getBoolean("enabled")
                simGps = j.optBoolean("gps", true); simUat = j.optBoolean("uat", true); simWeather = j.optBoolean("weather", true)
                if (enabled && !simulator) {
                    realConfig = config
                    val p = DatagramSocket(0, InetAddress.getByName("127.0.0.1")).use { it.localPort }
                    // Keep the EFB port: simulator traffic goes to the EFB on this device.
                    config = Settings(sentryIp = "127.0.0.1", listenPort = p, announcePort = 0, efbPort = config.efbPort)
                    simulator = true; simPublic = false
                    simThread = Thread({ runSimulator() }, "gui-simulator").apply { start() }
                } else if (!enabled && simulator) { simulator = false; simThread?.join(1500); config = realConfig }
                device = null; requested = null; observation = null
                return JSONObject().put("simulator", enabled)
            }
            else -> throw IllegalArgumentException("Unknown GUI action.")
        }
    }

    /** Find EFB: uses the running bridge's list when it holds the discovery port, else listens briefly. */
    /** This phone's address on the receiver's WiFi (the route to the receiver). Sends nothing. */
    private fun localAddress(network: Network): java.net.Inet4Address = DatagramSocket().use { socket ->
        network.bindSocket(socket)
        socket.connect(InetSocketAddress(config.sentryIp, config.listenPort))
        val local = socket.localAddress as? java.net.Inet4Address ?: throw IOException("Join the Sentry WiFi first.")
        if (!NativeDiagnostics.onNetwork(local, config.sentryIp)) throw IOException("This phone is not on the receiver's network.")
        local
    }

    /** Other devices on the receiver's WiFi that can be an EFB destination (Find EFB, and following a moved EFB). */
    private fun sweepDevices(network: Network, efbPort: Int, cancelled: () -> Boolean = { false }): List<String> =
        NativeDiagnostics.sweep(localAddress(network), setOf(config.sentryIp), cancelled) { NativeDiagnostics.answers(network, it) }
            .filter { NativeDiagnostics.efbCandidate(InetAddress.getByName(it), efbPort, config.sentryIp, config.listenPort) }

    private fun discoverEfbs(seconds: Double): JSONObject {
        val swept = mutableListOf<String>()
        val sweeper = if (simulator) null else Thread({
            // Not on the receiver's WiFi: announcements only.
            runCatching { wifi()?.let { network -> sweepDevices(network, config.efbPort) { closing }.also { synchronized(swept) { swept += it } } } }
        }, "gui-sweep").apply { isDaemon = true; start() }
        val result = try { announcedEfbs(seconds) } finally { sweeper?.join(4000) }  // Exit too: no sweep outlives the job
        val efbs = result.getJSONArray("efbs")
        val announced = (0 until efbs.length()).map { efbs.getJSONObject(it).getString("address") }.toSet()
        synchronized(swept) { swept.toList() }.filter { it !in announced }
            .forEach { efbs.put(JSONObject().put("app", "Device").put("address", it).put("port", config.efbPort)) }
        return result
    }

    private fun announcedEfbs(seconds: Double): JSONObject {
        val port = config.announcePort
        require(port > 0) { "EFB discovery is off. Set the EFB discovery port in Settings." }
        ensureOpen()
        val accept = { address: InetAddress, efbPort: Int -> NativeDiagnostics.efbCandidate(address, efbPort, config.sentryIp, config.listenPort) }
        if (BridgeService.status.running && BridgeService.announcing) {
            val found = sortedMapOf<String, JSONObject>()
            val deadline = SystemClock.elapsedRealtime() + (seconds * 1000).toLong()
            while (SystemClock.elapsedRealtime() < deadline) {
                ensureOpen()
                for ((dest, heard) in BridgeService.discovered) {
                    if (SystemClock.elapsedRealtime() - heard.second > 30_000 || !accept(dest.address, dest.port)) continue
                    found["${dest.address.hostAddress}:${dest.port}"] = JSONObject().put("app", heard.first.ifEmpty { "?" })
                        .put("address", dest.address.hostAddress).put("port", dest.port)
                }
                Thread.sleep(200)
            }
            return JSONObject().put("efbs", JSONArray(found.values.toList()))
        }
        // Broadcast announcements need the multicast lock on some devices.
        val lock = context.getSystemService(android.net.wifi.WifiManager::class.java).createMulticastLock("SentryBridgeFind")
        lock.setReferenceCounted(false); lock.acquire()
        try {
            return JSONObject().put("efbs", NativeDiagnostics.listenForEfbs(port, seconds, { ensureOpen() },
                { data -> SbCore.parseAnnounce(data, data.size) }, accept))
        } finally { if (lock.isHeld) lock.release() }
    }

    private fun report(name: String, text: String): JSONObject {
        val id = artifact(name, "report"); file(id).writeText(text); completeFile(id)
        return JSONObject().put("report", id).put("text", text.take(65536))
    }
    private fun runSimulator() {
        val fixture = JSONObject(context.assets.open("android-simulator.json").bufferedReader().use { it.readText() })
        DatagramSocket().use { socket ->
            while (simulator) {
                val packets = if (simPublic) fixture.getJSONArray(if (simGps) "fix" else "noFix") else JSONArray().put(fixture.getString("encrypted"))
                for (i in 0 until packets.length()) {
                    val data = NativeDiagnostics.unhex(packets.getString(i))
                    val ids = NativeDiagnostics.frames(data).map { it.first[0].toInt() and 255 }
                    if (!simUat && 36 in ids || !simWeather && 7 in ids) continue
                    socket.send(DatagramPacket(data, data.size, InetAddress.getByName("127.0.0.1"), config.listenPort))
                }
                Thread.sleep(500)
            }
        }
    }

    fun status(): JSONObject {
        checkUpdate()
        val st = BridgeService.status; val s = st.stats
        val age = if (st.lastRxAgeS < 0) JSONObject.NULL else st.lastRxAgeS
        val heartbeatAge = if (st.heartbeatAgeS < 0) JSONObject.NULL else st.heartbeatAgeS
        val mode = when (s?.get(0)) { 1 -> "PUBLIC"; 2 -> "ENCRYPTED"; else -> "UNKNOWN" }
        val counts = JSONObject(); st.frames.forEachIndexed { i, v -> if (v > 0) counts.put(i.toString(), v) }
        val artifacts = JSONArray()
        synchronized(files) { for (id in files.keys()) artifacts.put(JSONObject(files.getJSONObject(id).toString()).put("id", id).put("ready", files.getJSONObject(id).optBoolean("complete", true) && File(directory, "$id-${fileName(id)}").isFile && !(id == captureToken && recording))) }
        return JSONObject().put("adapter", "local").put("host", "android").put("simulator", simulator)
            .put("running", st.running).put("monitoring", st.monitoring).put("recording", recording)
            .put("state", if (!st.running) "stopped" else if (st.lastRxAgeS < 0) "waiting" else if (st.lastRxAgeS > 10) "lost" else if (mode == "ENCRYPTED") "mode" else "running")
            .put("gps", st.monitoring && st.lastRxAgeS in 0.0..10.0 && st.gpsFresh)
            .put("traffic", if (st.lastRxAgeS in 0.0..10.0) st.traffic else 0).put("weather", st.frames.getOrElse(7) { 0 })
            .put("mode", mode).put("frames", counts).put("datagrams", s?.get(2) ?: 0).put("lastPacketSeconds", age).put("heartbeatSeconds", heartbeatAge)
            .put("engine", "C core").put("settings", config.gui()).put("routesRequest", config.routes?.let { JSONObject(it) } ?: JSONObject.NULL)
            .put("device", device ?: JSONObject.NULL).put("modeObservation", observation ?: JSONObject.NULL).put("modeRequested", requested ?: JSONObject.NULL)
            .put("job", job).put("deviceLogs", receiverLogs).put("artifacts", artifacts).put("logs", JSONArray(BridgeService.logSnapshot().takeLast(120)))
            .put("error", st.error ?: captureError ?: JSONObject.NULL)
            .put("updateSupported", updateSupported).put("update", currentUpdate() ?: JSONObject.NULL)
            .put("sensors", sensors?.takeIf { st.running && SystemClock.elapsedRealtime() - sensorsAt <= 5000 } ?: JSONObject.NULL)
    }
}
