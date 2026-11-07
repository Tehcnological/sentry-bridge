package io.github.tehcnological.sentrybridge

import org.json.JSONArray
import org.json.JSONObject

/** GDL90 routes (tools/routes.py): {host, thisDevice, addresses}. This device sends to 127.0.0.1 and each listed
 *  EFB address to that address, all on the EFB port from Settings. Same rules and messages as the desktop. */
object Routes {
    const val MAX_ADDRESSES = 16

    /** One Other device address, normalized; errors name it. */
    fun efbAddress(value: Any?, port: Int, listenPort: Int): String {
        val text = (value as? String)?.trim().orEmpty()
        require(text.isNotEmpty()) { "Enter the EFB device's IPv4 address." }
        try {
            val address = NativeDiagnostics.ipv4(text, true)
            NativeDiagnostics.checkEndpoint(address, port, listenPort)
            return address.hostAddress!!
        } catch (error: IllegalArgumentException) {
            throw IllegalArgumentException("$text: ${error.message}")
        }
    }

    /** The endpoints for a route on settings.efbPort: This device first, then the addresses in list order. */
    fun validate(j: JSONObject, settings: Settings): JSONObject {
        require(j.opt("host") == "android") { "Android bridge host required." }
        val thisDevice = j.opt("thisDevice") as? Boolean ?: throw IllegalArgumentException("This device must be on or off.")
        val addresses = j.opt("addresses") as? JSONArray ?: throw IllegalArgumentException("EFB addresses must be a list.")
        require(thisDevice || addresses.length() > 0) { "Turn on This device or add an EFB address." }
        require(addresses.length() <= MAX_ADDRESSES) { "Up to $MAX_ADDRESSES EFB addresses." }
        val port = settings.efbPort
        val routes = JSONArray()
        val warnings = JSONArray()
        val listed = mutableSetOf<String>()
        if (thisDevice) {
            // This device's discovery listener would take the output; another device's port is its own.
            require(port != settings.announcePort) { "On this device the EFB port must differ from the discovery port." }
            val shared = port == settings.listenPort
            routes.put(JSONObject().put("address", "127.0.0.1").put("port", port).put("shared", shared))
            listed += "127.0.0.1"
            if (shared) warnings.put("Shares UDP $port with the EFB on this device. One EFB at a time receives it; close other EFBs. " +
                "The EFB must allow port sharing; otherwise set another EFB port in Settings and in the EFB.")
        }
        for (i in 0 until addresses.length()) {
            val address = efbAddress(addresses.opt(i), port, settings.listenPort)
            require(listed.add(address)) { "$address: listed twice." }
            routes.put(JSONObject().put("address", address).put("port", port).put("shared", false))
        }
        return JSONObject().put("routes", routes).put("warnings", warnings).put("displayVerified", false).put("appliedToBridge", false)
    }

    /** A route saved before several EFBs ({topology, address}, or v0.1.6 per-EFB selections, first one kept) as
     *  {host, thisDevice, addresses}; the same object when it needs no upgrade. */
    fun upgrade(request: JSONObject?): JSONObject? {
        var r = request ?: return null
        r.optJSONArray("selections")?.let { selections ->
            r = JSONObject().put("host", "android").put("topology", r.optString("topology", "same"))
                .put("address", selections.optJSONObject(0)?.optString("address", "") ?: "")
        }
        if (!r.has("topology")) return r
        val same = r.optString("topology") == "same"
        val address = (r.opt("address") as? String)?.trim().orEmpty()
        return JSONObject().put("host", r.opt("host")).put("thisDevice", same)
            .put("addresses", if (same || address.isEmpty()) JSONArray() else JSONArray().put(address))
    }
}
