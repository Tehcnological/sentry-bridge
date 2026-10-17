package io.github.tehcnological.sentrybridge

/** Plain-language status lines from BridgeService.Status, shared by the screen and the notification. */
object StatusText {
    private const val NO_DATA_S = 5.0

    /** Headline problem, or null when everything is flowing. */
    fun problem(st: BridgeService.Status): String? {
        if (!st.running) return st.error
        st.warning?.let { return it }
        val s = st.stats
        return when {
            !st.wifi -> "Not on WiFi. Join the Sentry's WiFi network (SentryPlus_...)."
            st.lastRxAgeS < 0 -> "Waiting for the Sentry. Is this tablet on the Sentry's WiFi?"
            st.lastRxAgeS > NO_DATA_S -> "No data from the Sentry for ${st.lastRxAgeS.toInt()} s."
            s != null && s[0] == SbCore.MODE_ENCRYPTED ->
                "Compatibility Mode is off. Enable it in Settings."
            else -> null
        }
    }

    fun lines(st: BridgeService.Status): List<String> {
        if (!st.running) return listOf("Stopped")
        val out = mutableListOf<String>()
        val s = st.stats
        problem(st)?.let { out += it }
        if (s != null && st.lastRxAgeS >= 0) {
            val mode = when (s[0]) {
                SbCore.MODE_PUBLIC -> "Compatibility Mode"
                SbCore.MODE_ENCRYPTED -> "ForeFlight mode"
                else -> "mode unknown"
            }
            out += "Sentry: $mode, GPS ${if (st.gpsFresh) "OK" else "no fix"}"
            // st.traffic counts reports sent at the last tick; s[9] also includes positions up to 60 s old.
            out += "Traffic: ${st.traffic} aircraft"
            out += "Sent ${s[7]} reports to ${st.destinations}" + if (st.apps > 0) " + ${st.apps} app(s)" else ""
        }
        return out
    }

    fun short(st: BridgeService.Status): String {
        problem(st)?.let { return it }
        if (st.stats == null) return "Running"
        return "${st.traffic} aircraft, GPS " + if (st.gpsFresh) "OK" else "no fix"
    }
}
