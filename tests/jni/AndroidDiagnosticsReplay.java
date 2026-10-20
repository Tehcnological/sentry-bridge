import io.github.tehcnological.sentrybridge.NativeDiagnostics;
import io.github.tehcnological.sentrybridge.NativeGui;
import io.github.tehcnological.sentrybridge.BridgeService;
import io.github.tehcnological.sentrybridge.Settings;
import io.github.tehcnological.sentrybridge.StatusText;
import java.io.File;
import java.util.HexFormat;
import org.json.JSONArray;
import org.json.JSONObject;

/** Run the actual compiled Android diagnostic classes on host-generated synthetic captures. */
public class AndroidDiagnosticsReplay {
    public static void main(String[] args) throws Exception {
        NativeDiagnostics d = NativeDiagnostics.INSTANCE;
        if (args[0].equals("frames")) {
            JSONArray frames = new JSONArray();
            for (var frame : d.frames(HexFormat.of().parseHex(args[1]))) {
                frames.put(new JSONObject().put("hex", d.hex(frame.getFirst())).put("crc", frame.getSecond()));
            }
            System.out.println(frames);
        } else if (args[0].equals("heartbeat")) {
            // One line per datagram: does it refresh GPS freshness in BridgeService?
            for (int i = 1; i < args.length; i++) System.out.println(d.hasHeartbeat(HexFormat.of().parseHex(args[i])));
        } else if (args[0].equals("lognames")) {
            // One line per receiver link: the decoded file name, or "-" when it is rejected.
            for (int i = 1; i < args.length; i++) { String name = d.logName(args[i]); System.out.println(name == null ? "-" : name); }
        } else if (args[0].equals("sensors")) {
            // One line per /stats message: the readings the app shows.
            for (int i = 1; i < args.length; i++) System.out.println(d.sensorValues(args[i]));
        } else if (args[0].equals("mode")) {
            System.out.println(d.describeMode(new JSONObject(args[1])));
        } else if (args[0].equals("power")) {
            var device = NativeGui.class.getDeclaredField("device");
            device.setAccessible(true);
            device.set(null, new JSONObject("{\"settings\":{\"Power\":{\"powerSavings\":true}}}"));
            var warning = NativeGui.INSTANCE.getPowerWarning();
            if (warning == null || !warning.contains("web settings")) throw new AssertionError("Missing native warning");
            var status = new BridgeService.Status();
            var running = status.getClass().getDeclaredField("running"); running.setAccessible(true); running.setBoolean(status, true);
            var statusWarning = status.getClass().getDeclaredField("warning"); statusWarning.setAccessible(true); statusWarning.set(status, warning);
            var notification = StatusText.class.getMethod("short", BridgeService.Status.class).invoke(StatusText.INSTANCE, status);
            if (!warning.equals(notification)) throw new AssertionError("Notification does not show the warning");
            device.set(null, new JSONObject("{\"settings\":{\"Power\":{\"powerSavings\":false}}}"));
            if (NativeGui.INSTANCE.getPowerWarning() != null) throw new AssertionError("Warning stayed on");
            System.out.println("Power Savings status and notification passed");
        } else if (args[0].equals("gps")) {
            // A fix is shown only while heartbeats are current, whatever other frames arrive.
            // {heartbeat age, GPS-valid bit}: absent, fresh valid, fresh invalid, expired
            for (double[] item : new double[][]{{-1.0, 1}, {1.0, 1}, {1.0, 0}, {5.0, 1}}) {
                double age = item[0];
                var stats = new int[12]; stats[1] = (int) item[1];
                var status = new BridgeService.Status();
                for (var field : new String[]{"running", "stats", "heartbeatAgeS", "lastRxAgeS", "wifi"}) {
                    var f = status.getClass().getDeclaredField(field); f.setAccessible(true);
                    switch (field) {
                        case "running", "wifi" -> f.setBoolean(status, true);
                        case "stats" -> f.set(status, stats);
                        case "heartbeatAgeS" -> f.setDouble(status, age);
                        default -> f.setDouble(status, 0.5);
                    }
                }
                var text = (String) StatusText.class.getMethod("short", BridgeService.Status.class).invoke(StatusText.INSTANCE, status);
                boolean fresh = age == 1.0 && stats[1] == 1;
                if (status.getGpsFresh() != fresh || text.endsWith("GPS OK") != fresh)
                    throw new AssertionError("GPS freshness wrong for heartbeat age " + age + ": " + text);
            }
            System.out.println("Heartbeat GPS freshness passed");
        } else if (args[0].equals("disk")) {
            var capture = NativeGui.class.getDeclaredField("capture"); capture.setAccessible(true);
            capture.set(null, new java.io.BufferedWriter(new java.io.StringWriter()) {
                @Override public void write(String s, int offset, int length) throws java.io.IOException { throw new java.io.IOException("Disk full"); }
                @Override public void close() throws java.io.IOException { throw new java.io.IOException("Disk full"); }
            });
            NativeGui.INSTANCE.record(new byte[]{126, 0, 126}, "192.168.4.1");
            if (NativeGui.INSTANCE.getRecording()) throw new AssertionError("Recording stayed active after a write failure");
            var error = NativeGui.class.getDeclaredField("captureError"); error.setAccessible(true);
            if (error.get(null) == null) throw new AssertionError("Storage failure is not exposed");
            System.out.println("Recording storage failure contained");
        } else if (args[0].equals("routes")) {
            var loopback = java.net.InetAddress.getByName("127.0.0.1");
            try { d.checkEndpoint(loopback, 4000, 4000); throw new AssertionError("Loopback accepted"); }
            catch (IllegalArgumentException expected) {}
            for (var network : java.util.Collections.list(java.net.NetworkInterface.getNetworkInterfaces())) {
                for (var link : network.getInterfaceAddresses()) {
                    var address = link.getAddress();
                    if (address instanceof java.net.Inet4Address) {
                        try { d.checkEndpoint(address, 4000, 4000); throw new AssertionError("Own interface accepted"); }
                        catch (IllegalArgumentException expected) {}
                    }
                    if (link.getBroadcast() != null && link.getNetworkPrefixLength() >= 0 && link.getNetworkPrefixLength() <= 30) {
                        try { d.checkEndpoint(link.getBroadcast(), 4001, 4000); throw new AssertionError("Broadcast accepted"); }
                        catch (IllegalArgumentException expected) {}
                    }
                }
            }
            d.checkEndpoint(loopback, 4001, 4000);
            System.out.println("Native route checks passed");
        } else if (args[0].equals("settings")) {
            var gui = new JSONObject().put("sentryIp", "192.168.4.1");
            // GUI settings saved without efbPort and no route yet keep the stored legacy port; new installs get 4000.
            if (Settings.Companion.fromGui(gui, null, 4001).getEfbPort() != 4001) throw new AssertionError("Legacy EFB port lost");
            if (Settings.Companion.fromGui(gui, null, 4000).getEfbPort() != 4000) throw new AssertionError("New install not on 4000");
            var selections = new JSONArray()
                .put(new JSONObject().put("id", "avare-android").put("port", 4001).put("address", "127.0.0.1"))
                .put(new JSONObject().put("id", "custom-gdl90").put("port", 43211).put("address", "127.0.0.1"));
            var old = new JSONObject().put("host", "android").put("platform", "android").put("topology", "same").put("selections", selections);
            var migrated = Settings.Companion.fromGui(gui, old.toString(), 4000);
            var route = new JSONObject(migrated.getRoutes());
            if (migrated.getEfbPort() != 4001 || route.has("selections") || !route.getString("address").equals("127.0.0.1")
                || !route.getString("topology").equals("same")) throw new AssertionError("Old route not migrated: " + route);
            if (!BridgeService.Companion.logSnapshot().toString().contains("127.0.0.1:43211")) throw new AssertionError("Dropped EFB not logged");
            var portless = new JSONObject(old.toString());
            portless.getJSONArray("selections").getJSONObject(0).remove("port");
            if (Settings.Companion.fromGui(gui, portless.toString(), 4001).getEfbPort() != 4001) throw new AssertionError("Portless route left the legacy port");
            System.out.println("Settings migration passed");
        } else if (args[0].equals("newer")) {
            System.out.println(NativeGui.INSTANCE.newer(args[1], args[2]));
        } else if (args[0].equals("rebind")) {
            // The shared input moving to a new WiFi address: the new channel is bound and selected, the old one
            // closed; a failed bind keeps the old one open and selected.
            int port = Integer.parseInt(args[2]);
            try (var selector = java.nio.channels.Selector.open()) {
                var old = java.nio.channels.DatagramChannel.open();
                old.bind(new java.net.InetSocketAddress("127.0.0.1", port));
                old.configureBlocking(false);
                old.register(selector, java.nio.channels.SelectionKey.OP_READ);
                try {
                    BridgeService.Companion.rebound(old, selector, java.net.InetAddress.getByName("192.0.2.123"), port);
                    throw new AssertionError("Bound to an address this host does not have");
                } catch (java.io.IOException expected) { }
                if (!old.isOpen() || selector.keys().size() != 1) throw new AssertionError("Failed rebind lost the old input");
                var moved = BridgeService.Companion.rebound(old, selector, java.net.InetAddress.getByName(args[1]), port);
                var bound = (java.net.InetSocketAddress) moved.getLocalAddress();
                if (old.isOpen() || !bound.getAddress().getHostAddress().equals(args[1]) || bound.getPort() != port || moved.keyFor(selector) == null)
                    throw new AssertionError("Rebind gave " + bound);
                moved.close();
            }
            System.out.println("Shared input rebind passed");
        } else if (args[0].equals("sharedbind")) {
            // The service's network callback can report WiFi after the worker reaches the shared bind.
            var theUnsafe = sun.misc.Unsafe.class.getDeclaredField("theUnsafe"); theUnsafe.setAccessible(true);
            var service = (BridgeService) ((sun.misc.Unsafe) theUnsafe.get(null)).allocateInstance(BridgeService.class);
            var wifi = BridgeService.class.getDeclaredField("wifi"); wifi.setAccessible(true);
            // The bridge loop's rebind check asks without waiting: no WiFi must fail at once, not block the loop.
            var address = BridgeService.class.getDeclaredMethod("sharedAddress", Settings.class, long.class);
            address.setAccessible(true);
            var plain = Settings.Companion.fromGui(new JSONObject().put("sentryIp", args[1]).put("listenPort", Integer.parseInt(args[2])), null, 4000);
            long asked = System.nanoTime();
            try { address.invoke(service, plain, 0L); throw new AssertionError("No WiFi gave an address"); }
            catch (java.lang.reflect.InvocationTargetException expected) {
                if (!(expected.getCause() instanceof IllegalStateException)) throw expected;
            }
            if (System.nanoTime() - asked > 300_000_000L) throw new AssertionError("The no-wait check waited");
            var late = new Thread(() -> {
                try { Thread.sleep(400); wifi.set(service, new android.net.Network()); } catch (Exception e) { throw new RuntimeException(e); }
            });
            late.start();
            var bind = BridgeService.class.getDeclaredMethod("bindShared", java.nio.channels.DatagramChannel.class, Settings.class);
            bind.setAccessible(true);
            var settings = Settings.Companion.fromGui(new JSONObject().put("sentryIp", args[1]).put("listenPort", Integer.parseInt(args[2])), null, 4000);
            try (var rx = java.nio.channels.DatagramChannel.open()) {
                bind.invoke(service, rx, settings);
                var bound = (java.net.InetSocketAddress) rx.getLocalAddress();
                if (!bound.getAddress().getHostAddress().equals(args[1]) || bound.getPort() != Integer.parseInt(args[2]))
                    throw new AssertionError("Bound to " + bound);
            }
            late.join();
            System.out.println("Shared bind after a late WiFi callback passed");
        } else if (args[0].equals("recovery")) {
            var directory = NativeGui.class.getDeclaredField("directory"); directory.setAccessible(true); directory.set(null, new File(args[1]));
            var simulator = NativeGui.class.getDeclaredField("simulator"); simulator.setAccessible(true); simulator.setBoolean(null, true);
            var filesField = NativeGui.class.getDeclaredField("files"); filesField.setAccessible(true);
            var files = (JSONObject) filesField.get(null);
            for (int i = 0; i < 100; i++) files.put(String.format("%032x", i), new JSONObject().put("name", "kept.txt").put("kind", "report").put("complete", true));
            var restore = NativeGui.class.getDeclaredMethod("autoRestore"); restore.setAccessible(true);
            try { restore.invoke(NativeGui.INSTANCE); }
            catch (java.lang.reflect.InvocationTargetException expected) {
                // Android service/timing SDK stubs cannot verify packets on the host.
            }
            var publicMode = NativeGui.class.getDeclaredField("simPublic"); publicMode.setAccessible(true);
            if (!publicMode.getBoolean(null)) throw new AssertionError("File cap prevented the mode request");
            if (files.length() != 100) throw new AssertionError("Recovery removed saved files");
            var partialId = "f".repeat(32);
            files.put(partialId, new JSONObject().put("name", "partial.zip").put("kind", "backup").put("complete", false));
            var partial = new File(args[1], partialId + "-partial.zip"); java.nio.file.Files.writeString(partial.toPath(), "partial");
            var cleanup = NativeGui.class.getDeclaredMethod("discardNewFiles", java.util.Set.class); cleanup.setAccessible(true);
            cleanup.invoke(NativeGui.INSTANCE, java.util.Set.of());
            if (files.length() != 100 || partial.exists()) throw new AssertionError("Incomplete file cleanup failed");
            System.out.println("Native recovery request and file cleanup passed");
        } else if (args[0].equals("closing")) {
            // Exit during automatic restoration: the cancelled backup must not continue into the mode request.
            var directory = NativeGui.class.getDeclaredField("directory"); directory.setAccessible(true); directory.set(null, new File(args[1]));
            var simulator = NativeGui.class.getDeclaredField("simulator"); simulator.setAccessible(true); simulator.setBoolean(null, true);
            var closing = NativeGui.class.getDeclaredField("closing"); closing.setAccessible(true); closing.setBoolean(null, true);
            var restore = NativeGui.class.getDeclaredMethod("autoRestore"); restore.setAccessible(true);
            try {
                restore.invoke(NativeGui.INSTANCE);
                throw new AssertionError("Automatic restoration ran after Exit");
            } catch (java.lang.reflect.InvocationTargetException expected) {
                if (!"Sentry Bridge was closed.".equals(expected.getCause().getMessage())) throw expected;
            }
            var publicMode = NativeGui.class.getDeclaredField("simPublic"); publicMode.setAccessible(true);
            if (publicMode.getBoolean(null)) throw new AssertionError("Mode request sent after Exit");
            for (String line : BridgeService.Companion.logSnapshot())
                if (line.contains("continuing mode recovery")) throw new AssertionError("Cancelled backup treated as a failure: " + line);
            // A real (non-simulator) log download after Exit must stop before any WiFi/network call;
            // on this host, reaching wifi() would fail differently (no Android context).
            simulator.setBoolean(null, false);
            var download = NativeGui.class.getDeclaredMethod("downloadLog", String.class, java.io.OutputStream.class, int.class);
            download.setAccessible(true);
            var output = new java.io.ByteArrayOutputStream();
            try {
                download.invoke(NativeGui.INSTANCE, "first.csv", output, 1024);
                throw new AssertionError("Log download ran after Exit");
            } catch (java.lang.reflect.InvocationTargetException expected) {
                if (!"Sentry Bridge was closed.".equals(expected.getCause().getMessage())) throw expected;
            }
            if (output.size() != 0) throw new AssertionError("Log data written after Exit");
            // A WebSocket probe job after Exit fails as closed before WiFi/network access.
            var perform = NativeGui.class.getDeclaredMethod("performAction", String.class, JSONObject.class);
            perform.setAccessible(true);
            perform.invoke(NativeGui.INSTANCE, "websocket", new JSONObject().put("path", "/").put("seconds", 2.0));
            var jobField = NativeGui.class.getDeclaredField("job"); jobField.setAccessible(true);
            JSONObject job = (JSONObject) jobField.get(null);
            for (int i = 0; i < 100 && "running".equals(job.optString("state")); i++) {
                Thread.sleep(50);
                job = (JSONObject) jobField.get(null);
            }
            if (!"failed".equals(job.optString("state")) || !"Sentry Bridge was closed.".equals(job.optString("error"))) {
                System.err.println("WebSocket probe ran after Exit: " + job);
                System.exit(1);  // the job executor thread would keep a failed check running
            }
            System.out.println("Exit during automatic restoration, log download and WebSocket probe passed");
            System.out.flush();
            System.exit(0);  // NativeGui's job executor thread is not a daemon
        } else if (args[0].equals("handshake")) {
            // Exit while a slow peer sends an incomplete WebSocket handshake one byte every 200 ms, or ("quiet")
            // after a complete handshake from a peer that then sends nothing: the probe must stop as closed and
            // close its connection.
            boolean quiet = args.length > 2 && args[2].equals("quiet");
            var server = new java.net.ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress());
            var closing = new java.util.concurrent.atomic.AtomicBoolean(false);
            var sending = new java.util.concurrent.CountDownLatch(1);
            var peerSawClose = new java.util.concurrent.CountDownLatch(1);
            Thread peer = new Thread(() -> {
                try (var socket = server.accept()) {
                    var request = new java.io.BufferedReader(new java.io.InputStreamReader(socket.getInputStream()));
                    String key = "";
                    for (String line; !(line = request.readLine()).isEmpty(); )
                        if (line.toLowerCase().startsWith("sec-websocket-key:")) key = line.substring(18).trim();
                    var out = socket.getOutputStream();
                    if (quiet) {
                        var accept = java.util.Base64.getEncoder().encodeToString(java.security.MessageDigest.getInstance("SHA-1")
                            .digest((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").getBytes()));
                        out.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                            + accept + "\r\n\r\n").getBytes()); out.flush();
                        sending.countDown();
                        while (socket.getInputStream().read() >= 0) {}  // until the probe closes its connection
                        throw new java.io.EOFException();
                    }
                    out.write("HTTP/1.1 101 Switching Protocols\r\nX-Slow: ".getBytes()); out.flush();
                    sending.countDown();
                    for (int i = 0; i < 100; i++) { out.write('x'); out.flush(); Thread.sleep(200); }
                } catch (Exception closed) { peerSawClose.countDown(); }
            });
            peer.setDaemon(true); peer.start();
            var settings = io.github.tehcnological.sentrybridge.Settings.Companion.fromGui(
                new JSONObject().put("sentryIp", "127.0.0.1").put("httpPort", server.getLocalPort()), null);
            var result = new java.util.concurrent.atomic.AtomicReference<Throwable>();
            Thread worker = new Thread(() -> {
                try {
                    d.websocket(new android.net.Network(), settings, "/", 30.0, new File(args[1], "handshake.jsonl"), text -> kotlin.Unit.INSTANCE, () -> {
                        if (closing.get()) throw new IllegalStateException("Sentry Bridge was closed.");
                        return kotlin.Unit.INSTANCE;
                    });
                } catch (Throwable error) { result.set(error); }
            });
            worker.setDaemon(true); worker.start();
            if (!sending.await(5, java.util.concurrent.TimeUnit.SECONDS)) { System.err.println("No handshake started"); System.exit(1); }
            Thread.sleep(500);  // the slow handshake is underway
            closing.set(true);
            worker.join(2000);
            var error = result.get();
            if (worker.isAlive() || error == null || !"Sentry Bridge was closed.".equals(error.getMessage())) {
                System.err.println("Probe did not stop on Exit (" + (quiet ? "quiet peer" : "handshake") + "): alive=" + worker.isAlive() + " result=" + error);
                System.exit(1);
            }
            if (!peerSawClose.await(3, java.util.concurrent.TimeUnit.SECONDS)) { System.err.println("Probe left its connection open"); System.exit(1); }
            System.out.println(quiet ? "Exit during a quiet WebSocket passed" : "Exit during a slow WebSocket handshake passed");
            System.exit(0);
        } else if (args[0].equals("discovery")) {
            // Find EFB: real UDP announcements into the compiled listener; repeats listed once.
            var probe = new java.net.DatagramSocket(0, java.net.InetAddress.getLoopbackAddress());
            int port = probe.getLocalPort(); probe.close();
            // The listener calls ensureOpen after binding; the sender waits for that instead of sleeping.
            var listening = new java.util.concurrent.CountDownLatch(1);
            Thread sender = new Thread(() -> {
                try (var socket = new java.net.DatagramSocket()) {
                    if (!listening.await(5, java.util.concurrent.TimeUnit.SECONDS)) throw new AssertionError("Listener never started");
                    var target = java.net.InetAddress.getLoopbackAddress();
                    for (String text : new String[]{"{\"App\":\"ForeFlight\",\"GDL90\":{\"port\":4000}}", "not json",
                            "{\"App\":\"Avare\",\"GDL90\":{\"port\":4001}}", "{\"App\":\"ForeFlight\",\"GDL90\":{\"port\":4000}}"}) {
                        byte[] data = text.getBytes("UTF-8");
                        socket.send(new java.net.DatagramPacket(data, data.length, target, port));
                    }
                } catch (Exception error) { throw new RuntimeException(error); }
            });
            sender.start();
            var found = d.listenForEfbs(port, 1.5, () -> { listening.countDown(); return kotlin.Unit.INSTANCE; }, data -> {
                try {
                    var json = new JSONObject(new String(data, "UTF-8"));
                    return json.getJSONObject("GDL90").getInt("port") + "\t" + json.optString("App", "?");
                } catch (Exception invalid) { return null; }
            }, (address, efbPort) -> true);
            sender.join();
            var expected = "[{\"app\":\"ForeFlight\",\"address\":\"127.0.0.1\",\"port\":4000},{\"app\":\"Avare\",\"address\":\"127.0.0.1\",\"port\":4001}]";
            if (!new JSONArray(expected).similar(found)) throw new AssertionError("Find EFB listed " + found);
            // Only valid Other device addresses are offered.
            if (d.efbCandidate(java.net.InetAddress.getLoopbackAddress(), 4000, "192.168.4.1", 4000)) throw new AssertionError("Loopback offered");
            if (d.efbCandidate(java.net.InetAddress.getByName("192.168.4.1"), 4000, "192.168.4.1", 4000)) throw new AssertionError("Receiver offered");
            if (!d.efbCandidate(java.net.InetAddress.getByName("192.168.4.20"), 4000, "192.168.4.1", 4000)) throw new AssertionError("Valid EFB refused");
            // WiFi selection: the interface whose subnet holds the receiver IP.
            byte[] receiver = {(byte) 192, (byte) 168, 4, 1};
            if (!d.sameIpv4Subnet(new byte[]{(byte) 192, (byte) 168, 4, 23}, 24, receiver)) throw new AssertionError("Same /24 not matched");
            if (d.sameIpv4Subnet(new byte[]{(byte) 192, (byte) 168, 5, 23}, 24, receiver)) throw new AssertionError("Other /24 matched");
            if (!d.sameIpv4Subnet(new byte[]{10, 0, 0, 1}, 32, new byte[]{10, 0, 0, 1})) throw new AssertionError("/32 not matched");
            if (d.sameIpv4Subnet(new byte[16], 64, receiver) || d.sameIpv4Subnet(new byte[]{10, 0, 0, 1}, 0, receiver))
                throw new AssertionError("Invalid input matched");
            // Running bridge: a remote EFB announcing port 4000 is offered with the default input port 4000.
            if (!d.efbCandidate(java.net.InetAddress.getByName("192.168.4.30"), 4000, "192.168.4.1", 4000)) throw new AssertionError("Remote port 4000 refused");
            // Its list is pruned after 30 s and holds at most DISCOVERY_LIMIT EFBs.
            var cache = new java.util.HashMap<java.net.InetSocketAddress, kotlin.Pair<String, Long>>();
            for (int round = 0; round < 4; round++) {
                long now = round * 40_000L;
                for (int host = 0; host < 32; host++)
                    d.rememberEfb(cache, new java.net.InetSocketAddress(java.net.InetAddress.getByName("192.168." + (4 + round) + "." + (host + 10)), 4001), "EFB", now);
                if (cache.size() != 32) throw new AssertionError("Round " + round + " kept " + cache.size());
                for (var heard : cache.values()) if (heard.getSecond() != now) throw new AssertionError("Expired entry kept");
            }
            for (int host = 0; host < 40; host++)
                d.rememberEfb(cache, new java.net.InetSocketAddress(java.net.InetAddress.getByName("192.168.9." + (host + 10)), 4001), "EFB", 200_000L);
            if (cache.size() != 32) throw new AssertionError("Limit not applied: " + cache.size());
            System.out.println("Find EFB listener, address filter, bounded list and WiFi subnet selection passed");
        } else if (args[0].equals("late-body")) {
            // The local UI server rejects a POST without the app session; the body arrives after that
            // decision. The client must still receive the 403, not a connection reset.
            var server = new java.net.ServerSocket(0, 1, java.net.InetAddress.getLoopbackAddress());
            var serve = NativeGui.class.getDeclaredMethod("serve", java.net.Socket.class); serve.setAccessible(true);
            Thread handler = new Thread(() -> {
                try (var accepted = server.accept()) { serve.invoke(NativeGui.INSTANCE, accepted); }
                catch (Exception error) { throw new RuntimeException(error); }
            });
            handler.setDaemon(true); handler.start();
            String status;
            try (var client = new java.net.Socket(java.net.InetAddress.getLoopbackAddress(), server.getLocalPort())) {
                client.setSoTimeout(5000);
                var out = client.getOutputStream();
                out.write(("POST /api/actions/stop HTTP/1.1\r\nHost: 127.0.0.1:8768\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n").getBytes());
                out.flush();
                Thread.sleep(300);
                out.write("{}".getBytes()); out.flush();
                status = new java.io.BufferedReader(new java.io.InputStreamReader(client.getInputStream())).readLine();
            }
            handler.join(5000); server.close();
            if (status == null || !status.contains(" 403 ")) throw new AssertionError("Rejected POST lost its reply: " + status);
            System.out.println("Local UI server late-body rejection passed");
        } else if (args[0].equals("weather")) {
            System.out.println(d.weather(new File(args[1]), new File(args[2]), Integer.parseInt(args[3]), Integer.parseInt(args[4])));
        } else if (args[0].equals("analyze")) {
            System.out.print(d.analyze(new File(args[1]), Integer.parseInt(args[2]), 2));
        } else throw new IllegalArgumentException("Unknown test operation");
    }
}
