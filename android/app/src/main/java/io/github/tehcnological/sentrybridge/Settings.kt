package io.github.tehcnological.sentrybridge

import android.content.Context
import org.json.JSONObject
import org.json.JSONArray

/** User settings, persisted in SharedPreferences. */
data class Settings(
    /** true: the EFB runs on this tablet (bridge feeds 127.0.0.1:efbPort). false: other devices on the Sentry WiFi. */
    val efbOnThisDevice: Boolean = true,
    /** The one GDL90 route's port; equal to listenPort on this device, the bridge shares that port with the EFB. */
    val efbPort: Int = 4000,
    val ownshipHex: String = "",
    val autoEnable: Boolean = true,
    val sentryIp: String = "192.168.4.1",
    val listenPort: Int = 4000,
    val httpPort: Int = 80,
    val announcePort: Int = 63093,
    val relay: Boolean = true,
    /** Opt-in on Android: F-Droid asks apps not to contact other services unless the user chooses to. */
    val updateCheck: Boolean = false,
    val reference: DoubleArray? = null,
    val staleSeconds: Double = 15.0,
    val intervalSeconds: Double = 1.0,
    val statsSeconds: Double = 10.0,
    val routes: String? = null,
) {
    fun gui(): JSONObject = JSONObject().put("sentryIp", sentryIp).put("listenPort", listenPort).put("efbPort", efbPort)
        .put("httpPort", httpPort).put("announcePort", announcePort).put("autoRestore", autoEnable)
        .put("ownshipHex", ownshipHex).put("relay", relay).put("updateCheck", updateCheck).put("reference", reference?.let { JSONArray(it.toList()) } ?: JSONObject.NULL)
        .put("staleSeconds", staleSeconds).put("intervalSeconds", intervalSeconds).put("statsSeconds", statsSeconds)

    /** ICAO address as an int, or -1 when empty or invalid. */
    val ownshipIcao: Int
        get() = ownshipHex.trim().takeIf { it.matches(Regex("[0-9a-fA-F]{6}")) }?.toInt(16) ?: -1

    fun save(ctx: Context) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean("efbOnThisDevice", efbOnThisDevice)
            .putInt("efbPort", efbPort)
            .putString("ownshipHex", ownshipHex)
            .putBoolean("autoEnable", autoEnable)
            .putString("sentryIp", sentryIp)
            .putString("gui", gui().toString()).putString("routes", routes)
            .apply()
    }

    companion object {
        private const val PREFS = "settings"
        const val SENTRY_PORT = 4000
        const val ANNOUNCE_PORT = 63093

        fun load(ctx: Context): Settings {
            val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val d = Settings()
            val legacy = Settings(
                efbOnThisDevice = p.getBoolean("efbOnThisDevice", d.efbOnThisDevice),
                efbPort = p.getInt("efbPort", d.efbPort),
                ownshipHex = p.getString("ownshipHex", d.ownshipHex) ?: d.ownshipHex,
                autoEnable = p.getBoolean("autoEnable", d.autoEnable),
                sentryIp = p.getString("sentryIp", d.sentryIp) ?: d.sentryIp,
            )
            val gui = p.getString("gui", null)?.let { runCatching { JSONObject(it) }.getOrNull() }
            // The stored efbPort preference (4001 by default before v0.2.0) is the port for GUI settings
            // saved without one, also when no route was saved yet; genuinely new installs get 4000.
            return if (gui == null) legacy else fromGui(gui, p.getString("routes", null), legacy.efbPort)
        }

        @JvmOverloads
        fun fromGui(j: JSONObject, routes: String?, legacyPort: Int = 4000): Settings {
            val ref = j.optJSONArray("reference")
            val request = routes?.let { runCatching { JSONObject(it) }.getOrNull() }
            // v0.1.6 and earlier saved per-EFB selections: keep the first one's address and, when the
            // settings have no EFB port yet, its port.
            val selections = request?.optJSONArray("selections")
            val first = selections?.optJSONObject(0)
            val route = if (first == null) routes else JSONObject().put("host", "android")
                .put("topology", request?.optString("topology", "same") ?: "same").put("address", first.optString("address", "")).toString()
            if (selections != null && selections.length() > 1) {
                val dropped = (1 until selections.length()).mapNotNull { selections.optJSONObject(it) }
                    .joinToString(", ") { "${it.optString("address", "?")}:${it.opt("port") ?: "?"}" }
                BridgeService.log("Saved EFBs upgraded to one route; no longer sent to: $dropped.")
            }
            return Settings(efbOnThisDevice = request?.optString("topology", "same") != "network",
                efbPort = j.optInt("efbPort", first?.optInt("port", legacyPort) ?: legacyPort),
                ownshipHex = j.optString("ownshipHex", ""), autoEnable = j.optBoolean("autoRestore", true),
                sentryIp = j.optString("sentryIp", "192.168.4.1"), listenPort = j.optInt("listenPort", 4000),
                httpPort = j.optInt("httpPort", 80), announcePort = j.optInt("announcePort", 63093),
                relay = j.optBoolean("relay", true), updateCheck = j.optBoolean("updateCheck", false), reference = ref?.let { doubleArrayOf(it.getDouble(0), it.getDouble(1)) },
                staleSeconds = j.optDouble("staleSeconds", 15.0), intervalSeconds = j.optDouble("intervalSeconds", 1.0),
                statsSeconds = j.optDouble("statsSeconds", 10.0), routes = route)
        }
    }
}
