package io.github.tehcnological.sentrybridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.StandardSocketOptions
import java.nio.ByteBuffer
import java.nio.channels.DatagramChannel
import java.nio.channels.SelectionKey
import java.nio.channels.Selector
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foreground service that runs the bridge: receives the Sentry's UDP stream on port 4000, feeds it
 * to the C core, and sends standard GDL90 to the EFB. One network thread owns the core handle.
 */
class BridgeService : Service() {

    data class Status(
        val running: Boolean = false,
        val monitoring: Boolean = false,
        val frames: IntArray = IntArray(256),
        val traffic: Int = 0,
        /** SbCore.stats() layout, or null before the first tick */
        val stats: IntArray? = null,
        /** seconds since the last datagram from the Sentry, -1 if none yet */
        val lastRxAgeS: Double = -1.0,
        /** seconds since the last GDL90 heartbeat, -1 if none yet; only heartbeats carry GPS status */
        val heartbeatAgeS: Double = -1.0,
        val warning: String? = null,
        val error: String? = null,
        val destinations: String = "",
        val apps: Int = 0,
        val wifi: Boolean = false,
    ) {
        /** GPS valid in a current heartbeat. Weather or traffic frames alone do not keep a fix shown. */
        val gpsFresh: Boolean get() = stats?.get(1) == 1 && heartbeatAgeS in 0.0..HEARTBEAT_STALE_S
    }

    companion object {
        /** The shared input moved to local: bound, registered with selector, then old closed. On failure
         *  old stays open and in use, and the half-made channel is closed. */
        fun rebound(old: DatagramChannel?, selector: Selector, local: InetAddress, port: Int): DatagramChannel {
            val replacement = DatagramChannel.open()
            try {
                replacement.setOption(StandardSocketOptions.SO_REUSEADDR, true)
                replacement.bind(InetSocketAddress(local, port))
                replacement.configureBlocking(false)
                replacement.register(selector, SelectionKey.OP_READ)
            } catch (e: IOException) {
                try { replacement.close() } catch (_: IOException) {}
                throw e
            }
            try { old?.close() } catch (_: IOException) {}
            return replacement
        }

        const val ACTION_START = "io.github.tehcnological.sentrybridge.START"
        const val ACTION_STOP = "io.github.tehcnological.sentrybridge.STOP"
        const val ACTION_MONITOR = "io.github.tehcnological.sentrybridge.MONITOR"
        /** How long a shared bind waits for the WiFi network callback before reporting no WiFi. */
        const val WIFI_WAIT_MS = 5000L
        const val POWER_SAVINGS_WARNING =
            "Power Savings is on. Disable it in Sentry's web settings before flight; ForeFlight is optional."
        private const val CHANNEL_ID = "bridge"
        private const val NOTIF_ID = 1
        /** GDL90 heartbeats arrive at 1 Hz; matches HEARTBEAT_STALE_S in tools/gui_runtime.py. */
        const val HEARTBEAT_STALE_S = 3.0

        @Volatile
        var status = Status()
            private set

        private val logLines = ArrayDeque<String>()
        private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)

        fun log(msg: String) {
            synchronized(logLines) {
                logLines.addLast("${timeFmt.format(Date())} $msg")
                while (logLines.size > 300) logLines.removeFirst()
            }
        }

        fun logSnapshot(): List<String> = synchronized(logLines) { logLines.toList() }

        /** EFBs heard on the discovery port while the bridge runs: app label and elapsedRealtime ms. */
        val discovered = java.util.concurrent.ConcurrentHashMap<InetSocketAddress, Pair<String, Long>>()
        /** True while the running bridge holds the discovery port. */
        @Volatile var announcing = false
    }

    @Volatile private var stopRequested = false
    @Volatile private var wifi: Network? = null
    @Volatile private var wifiChanged = true
    private var worker: Thread? = null
    private var settings = Settings()
    private var netCallback: ConnectivityManager.NetworkCallback? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var forwarding = true

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        NativeGui.start(applicationContext)
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
        }
        startInForeground("Starting")
        if (worker == null) {
            settings = NativeGui.config
            forwarding = intent?.action != ACTION_MONITOR
            stopRequested = false
            acquireLocks()
            watchWifi()
            status = Status(running = forwarding, monitoring = true)
            worker = Thread({ runBridge() }, "sentry-bridge").also { it.start() }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopRequested = true
        worker?.let {
            it.interrupt()
            it.join(2000)
        }
        worker = null
        netCallback?.let {
            try {
                getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it)
            } catch (_: Exception) {
            }
        }
        netCallback = null
        releaseLocks()
        status = status.copy(running = false, monitoring = false)
        NativeGui.stopped()
        log("stopped")
        super.onDestroy()
    }

    // ------------------------------------------------------------------ the bridge loop

    private fun runBridge() {
        val s = settings
        val h = SbCore.create(s.sentryIp, s.ownshipIcao, s.reference != null,
            s.reference?.get(0) ?: 0.0, s.reference?.get(1) ?: 0.0, s.staleSeconds, s.relay)
        if (h == 0L) { fail("Not enough memory to start the decoder."); return }
        var selector: Selector? = null
        var rx: DatagramChannel? = null
        var ann: DatagramChannel? = null
        var txLocal: DatagramSocket? = null
        var txWifi: DatagramSocket? = null
        var lastSendError = 0L

        val saved = s.routes?.let { routes ->
            runCatching { NativeGui.validateRoutes(org.json.JSONObject(routes), s).getJSONArray("routes").getJSONObject(0) }
        }
        // Sharing: an EFB on this device on the receiver port (see bindShared). Receiver tools and
        // captures bind the same way, so they work while that EFB holds the port.
        val shared = saved?.getOrNull()?.getBoolean("shared") == true
        val fixed: List<InetSocketAddress> = if (!forwarding) {
            emptyList()  // receiver tools and captures never send, so they do not depend on EFB routes
        } else if (saved != null) {
            // Saved routes can stop validating (a network change, or a v0.1 broadcast route migrated
            // without an address). Report it instead of letting the exception end the process.
            val route = saved.getOrElse { e ->
                SbCore.destroy(h)
                fail("${e.message ?: "Invalid EFB route."} Check the EFB address and port in Settings, then start again.")
                return
            }
            listOf(InetSocketAddress(route.getString("address"), route.getInt("port")))
        } else if (s.efbOnThisDevice) {
            listOf(InetSocketAddress(InetAddress.getLoopbackAddress(), s.efbPort))
        } else {
            SbCore.destroy(h)
            fail("Enter the EFB device's address, then start again.")
            return
        }
        val dests = fixed.joinToString(", ") { "${it.address.hostAddress}:${it.port}" }

        fun wifiSocket(): DatagramSocket? {
            if (wifiChanged || txWifi == null) {
                txWifi?.close()
                txWifi = null
                wifiChanged = false
                val n = wifi ?: return null
                txWifi = DatagramSocket().also { n.bindSocket(it) }
            }
            return txWifi
        }

        fun send(frames: Array<ByteArray>) {
            if (!forwarding || frames.isEmpty()) return
            for (dest in fixed) {
                // A shared route is loopback on the receiver port by design; the rest must not loop back.
                if (s.routes != null && !shared) {
                    try { NativeDiagnostics.checkEndpoint(dest.address, dest.port, s.listenPort) }
                    catch (_: IllegalArgumentException) { continue }
                }
                val sock = if (dest.address.isLoopbackAddress) txLocal else wifiSocket()
                if (sock == null) {
                    if (SystemClock.elapsedRealtime() - lastSendError > 10_000) {
                        log("not connected to the Sentry WiFi: cannot send to ${dest.address.hostAddress}")
                        lastSendError = SystemClock.elapsedRealtime()
                    }
                    continue
                }
                try {
                    for (fr in frames) sock.send(DatagramPacket(fr, fr.size, dest))
                } catch (e: IOException) {
                    if (SystemClock.elapsedRealtime() - lastSendError > 10_000) {
                        log("send to ${dest.address.hostAddress}:${dest.port} failed: ${e.message}")
                        lastSendError = SystemClock.elapsedRealtime()
                    }
                    if (!dest.address.isLoopbackAddress) wifiChanged = true   // rebuild the WiFi socket
                }
            }
        }

        try {
            selector = Selector.open()
            rx = DatagramChannel.open()
            try {
                if (shared) bindShared(rx, s) else rx.bind(InetSocketAddress(s.listenPort))
            } catch (e: IllegalStateException) {
                fail(e.message ?: "Cannot share the receiver port.")
                return
            } catch (e: IOException) {
                // Not "start the bridge first": the EFB's own bind would then fail the same way.
                fail(if (shared) "UDP ${s.listenPort} is held by an EFB that does not allow port sharing. Set another " +
                    "EFB port in Settings and the same port in the EFB, or close that EFB."
                    else "Port ${s.listenPort} is already in use by another app. Close it, or set your EFB's GDL90 port to " +
                    "${s.efbPort}, then start again.")
                return
            }
            rx.configureBlocking(false)
            rx.register(selector, SelectionKey.OP_READ)
            if (forwarding && s.announcePort > 0) {
                try {
                    ann = DatagramChannel.open().also {
                        it.setOption(StandardSocketOptions.SO_REUSEADDR, true)
                        it.bind(InetSocketAddress(s.announcePort))
                        it.configureBlocking(false)
                        it.register(selector, SelectionKey.OP_READ)
                    }
                    announcing = true
                } catch (e: IOException) {
                    log("EFB discovery disabled: port ${s.announcePort} unavailable (${e.message})")
                }
            }
            txLocal = DatagramSocket()
            log("bridge ${SbCore.version()} listening on UDP ${s.listenPort}, sending to $dests" +
                if (s.autoEnable) ", auto-enable on" else "")

            val buf = ByteBuffer.allocate(65535)
            var nextTick = SystemClock.elapsedRealtime() + 1000
            var lastNotify = 0L
            var lastStarts = 0
            var lastRx = -1.0
            var lastHeartbeat = -1.0
            discovered.clear()  // announcements from an earlier run or network are stale
            var nextStatus = 0L
            var nextLog = SystemClock.elapsedRealtime() + (s.statsSeconds * 1000).toLong()
            var liveTraffic = 0
            var enablePending = false
            var lastContact = -1.0
            val startedAt = SystemClock.elapsedRealtime() / 1000.0
            var silenceNoted = false
            var nextRouteCheck = startedAt + 5.0

            while (!stopRequested) {
                selector.select(maxOf(1L, minOf(250L, nextTick - SystemClock.elapsedRealtime())))
                if (stopRequested) break
                val now = SystemClock.elapsedRealtime() / 1000.0
                val keys = selector.selectedKeys()
                for (key in keys) {
                    val ch = key.channel() as DatagramChannel
                    var received = 0
                    while (received++ < 128) {
                        buf.clear()
                        val from = ch.receive(buf) as InetSocketAddress? ?: break
                        buf.flip()
                        val data = ByteArray(buf.remaining())
                        buf.get(data)
                        val src = from.address.hostAddress ?: ""
                        if (ch === rx) {
                            if (s.sentryIp.isNotEmpty() && src != s.sentryIp) continue
                            lastRx = now
                            // Only a complete heartbeat refreshes GPS; the core's 0x00 counter also
                            // counts undersized frames that do not update the GPS bit.
                            if (NativeDiagnostics.hasHeartbeat(data)) lastHeartbeat = now
                            NativeGui.record(data, src)
                            SbCore.feed(h, data, data.size, src, now)?.let { send(it) }
                        } else {
                            val a = SbCore.parseAnnounce(data, data.size) ?: continue
                            if (src == s.sentryIp) continue
                            val parts = a.split('\t', limit = 2)
                            val dest = InetSocketAddress(from.address, parts[0].toInt())
                            // Find EFB may offer any valid other-device destination, including a remote port 4000.
                            if (NativeDiagnostics.efbCandidate(from.address, dest.port, s.sentryIp, s.listenPort))
                                NativeDiagnostics.rememberEfb(discovered, dest, parts.getOrElse(1) { "?" }, SystemClock.elapsedRealtime())
                        }
                    }
                }
                keys.clear()

                if (forwarding && s.autoEnable && SbCore.wantsEnable(h, now)) enablePending = true
                // The core allows a few attempts per episode; keep one that arrives while another
                // operation runs, and drop it once the receiver is back in Compatibility Mode.
                if (enablePending && (SbCore.stats(h)[0] != SbCore.MODE_ENCRYPTED || NativeGui.autoEnable())) enablePending = false
                // Keepalive: a Sentry Plus stopped streaming to a receive-only bridge after ~16 min until its
                // status page was loaded. Start, every 30 s while receiving, every 5 s while silent (3 s gap).
                if (forwarding && s.sentryIp.isNotEmpty()) {
                    val silent = lastRx < 0 || now - lastRx >= 3.0
                    if ((lastContact < 0 || now - lastContact >= if (silent) 5.0 else 30.0) && NativeGui.keepalive()) lastContact = now
                }
                // A shared input is bound to the WiFi address of Start. While silent, follow a new address (the phone
                // rejoined the receiver's WiFi, for example after a receiver power cycle), as desktop and iOS do.
                if (shared && (lastRx < 0 || now - lastRx >= 5.0) && now >= nextRouteCheck) {
                    nextRouteCheck = now + 5.0
                    val local = runCatching { sharedAddress(s, 0) }.getOrNull()
                    val bound = (rx?.localAddress as? InetSocketAddress)?.address
                    if (local != null && local != bound) {
                        try {
                            rx = rebound(rx, selector!!, local, s.listenPort)
                            log("The route to the receiver changed: listening on ${local.hostAddress}:${s.listenPort}.")
                        } catch (e: IOException) {
                            log("The route to the receiver changed to ${local.hostAddress}, but UDP ${s.listenPort} could not be bound there (${e.message}).")
                        }
                    }
                }
                // A shared input hears only datagrams sent to this device's address; say so once if nothing arrives.
                if (shared && lastRx < 0 && !silenceNoted && now - startedAt >= 15.0) {
                    silenceNoted = true
                    log("No receiver data on UDP ${s.listenPort}. Sharing the port needs the receiver to send to this " +
                        "device's address; if it broadcasts, set another EFB port in Settings and in the EFB.")
                }
                val starts = SbCore.streamStarts(h)
                if (starts != lastStarts) {
                    lastStarts = starts
                    if (forwarding) NativeGui.streamStarted()
                }

                if (SystemClock.elapsedRealtime() >= nextTick) {
                    nextTick = SystemClock.elapsedRealtime() + (s.intervalSeconds * 1000).toLong()
                    SbCore.tick(h, now)?.let { liveTraffic = it.size; send(it) }
                    for (line in SbCore.drainLog(h)) log(line)
                }
                if (SystemClock.elapsedRealtime() >= nextStatus) {
                    nextStatus = SystemClock.elapsedRealtime() + 250
                    status = Status(
                        running = forwarding, monitoring = true, stats = SbCore.stats(h), frames = SbCore.frameCounts(h),
                        traffic = liveTraffic,
                        lastRxAgeS = if (lastRx < 0) -1.0 else now - lastRx,
                        heartbeatAgeS = if (lastHeartbeat < 0) -1.0 else now - lastHeartbeat,
                        warning = NativeGui.powerWarning, destinations = dests, apps = 0, wifi = NativeGui.simulator || wifi != null,
                    )
                    NativeGui.tick()
                    if (SystemClock.elapsedRealtime() - lastNotify > 5000) {
                        lastNotify = SystemClock.elapsedRealtime()
                        updateNotification(StatusText.short(status))
                    }
                }
                if (SystemClock.elapsedRealtime() >= nextLog) {
                    nextLog = SystemClock.elapsedRealtime() + (s.statsSeconds * 1000).toLong()
                    log(SbCore.summary(h))
                }
            }
        } catch (e: Exception) {
            if (!stopRequested) fail("bridge error: $e")
        } finally {
            announcing = false
            try { ann?.close() } catch (_: IOException) {}
            try { rx?.close() } catch (_: IOException) {}
            try { selector?.close() } catch (_: IOException) {}
            txLocal?.close()
            txWifi?.close()
            SbCore.destroy(h)
        }
    }

    /**
     * Share the receiver port with an EFB on this device: bind only the WiFi address that reaches the receiver,
     * so its unicast reaches the bridge (the more specific bind) and loopback output reaches the EFB's own
     * 0.0.0.0 listener. Linux allows the pair only when both sockets set SO_REUSEADDR, so an EFB that does
     * not set it cannot share. The bridge loop rebinds when the WiFi address changes.
     */
    private fun bindShared(rx: DatagramChannel, s: Settings) {
        val local = sharedAddress(s, WIFI_WAIT_MS)
        rx.setOption(StandardSocketOptions.SO_REUSEADDR, true)
        rx.bind(InetSocketAddress(local, s.listenPort))
        log("sharing UDP ${s.listenPort} with the EFB on this device (bound to ${local.hostAddress}). If the EFB shows " +
            "no data, it does not allow port sharing: set another EFB port in Settings and in the EFB.")
    }

    /** The WiFi address that reaches the receiver. Throws IllegalStateException with the reason when there is none. */
    private fun sharedAddress(s: Settings, waitMs: Long): InetAddress {
        // The network callback registered in onStartCommand reports WiFi on its own handler, possibly after
        // this worker starts; wait for it briefly (Stop ends the wait) instead of failing a joined device.
        val deadline = System.nanoTime() + waitMs * 1_000_000
        while (wifi == null && !stopRequested && System.nanoTime() < deadline) Thread.sleep(100)
        val network = wifi ?: throw IllegalStateException("Join the Sentry WiFi first: sharing UDP ${s.listenPort} uses its address.")
        check(s.sentryIp.isNotEmpty()) { "Set the Receiver IP in Settings to share UDP ${s.listenPort} with an EFB." }
        val local = try {
            DatagramSocket().use { probe ->
                network.bindSocket(probe)
                probe.connect(InetSocketAddress(s.sentryIp, s.listenPort))  // selects a route; sends nothing
                probe.localAddress
            }
        } catch (e: IOException) {
            throw IllegalStateException("Join the Sentry WiFi first: sharing UDP ${s.listenPort} uses its address.")
        }
        check(!local.isAnyLocalAddress && !local.isLoopbackAddress) {
            "A receiver on this device cannot share UDP ${s.listenPort} with an EFB. Set another EFB port in Settings."
        }
        return local
    }

    private fun fail(msg: String) {
        log(msg)
        status = status.copy(running = false, monitoring = false, error = msg)
        stopSelf()
    }

    // ------------------------------------------------------------------ WiFi, locks, notification

    private fun watchWifi() {
        val cm = getSystemService(ConnectivityManager::class.java)
        // The Sentry's WiFi has no internet, so the INTERNET capability must not be required
        val req = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                wifi = network
                wifiChanged = true
                log("WiFi connected")
            }

            override fun onLost(network: Network) {
                if (wifi == network) {
                    wifi = null
                    wifiChanged = true
                    log("WiFi lost")
                }
            }
        }
        cm.registerNetworkCallback(req, cb)
        netCallback = cb
    }

    @Suppress("DEPRECATION", "WakelockTimeout")
    private fun acquireLocks() {
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "SentryBridge:bridge").apply {
                setReferenceCounted(false)
                acquire()
            }
        val wm = applicationContext.getSystemService(WifiManager::class.java)
        wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "SentryBridge").apply {
            setReferenceCounted(false)
            acquire()
        }
        multicastLock = wm.createMulticastLock("SentryBridge").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseLocks() {
        multicastLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }
        wakeLock?.let { if (it.isHeld) it.release() }
        multicastLock = null
        wifiLock = null
        wakeLock = null
    }

    private fun buildNotification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ID, "Bridge status", NotificationManager.IMPORTANCE_LOW))
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, BridgeService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Sentry Bridge")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null as Icon?, "Stop", stop).build())
            .build()
    }

    private fun startInForeground(text: String) {
        val n = buildNotification(text)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
    }
}
