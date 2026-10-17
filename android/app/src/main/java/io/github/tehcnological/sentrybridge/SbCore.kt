package io.github.tehcnological.sentrybridge

/**
 * The portable C core (core/sbcore.c), via android/app/src/main/cpp/sb_jni.c.
 * Signatures must match tests/jni/.../SbCore.java, which tests the same JNI code on a desktop JVM.
 * Not thread-safe per handle: BridgeService calls it from one thread only.
 * All now arguments are monotonic seconds from SystemClock.elapsedRealtime().
 */
object SbCore {
    init {
        System.loadLibrary("sbjni")
    }

    @JvmStatic external fun version(): String
    @JvmStatic external fun create(sentryIp: String, ownshipIcao: Int, hasRef: Boolean, refLat: Double,
                                   refLon: Double, staleS: Double, relay: Boolean): Long
    @JvmStatic external fun destroy(h: Long)
    @JvmStatic external fun feed(h: Long, data: ByteArray, len: Int, srcIp: String, now: Double): Array<ByteArray>?
    @JvmStatic external fun tick(h: Long, now: Double): Array<ByteArray>?
    @JvmStatic external fun wantsEnable(h: Long, now: Double): Boolean
    @JvmStatic external fun streamStarts(h: Long): Int
    @JvmStatic external fun summary(h: Long): String
    /** [mode, gpsValid, datagrams, crcBad, modesMsgs, uatMsgs, uatBad, sent, targets, positioned, positionedUat, posRejected] */
    @JvmStatic external fun stats(h: Long): IntArray
    /** Message-ID counters, copied on the network thread only. */
    @JvmStatic external fun frameCounts(h: Long): IntArray
    @JvmStatic external fun drainLog(h: Long): Array<String>
    /** 1 on, 0 off, -1 unreadable */
    @JvmStatic external fun powerSavingsOn(json: String): Int
    /** "port\tapp" for a valid EFB discovery announce, else null */
    @JvmStatic external fun parseAnnounce(data: ByteArray, len: Int): String?

    const val MODE_UNKNOWN = 0
    const val MODE_PUBLIC = 1
    const val MODE_ENCRYPTED = 2
}
