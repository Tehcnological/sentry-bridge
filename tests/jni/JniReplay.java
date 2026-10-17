import io.github.tehcnological.sentrybridge.SbCore;

import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HexFormat;
import java.util.List;

/**
 * Replays "seconds hex" datagram lines through the JNI wrapper with the same tick schedule as
 * tests/test_core_parity.py, printing every output so test_jni.py can compare it with bridge.py.
 * Usage: java -Djava.library.path=DIR JniReplay INPUT RELAY(0|1)
 */
public class JniReplay {
    public static void main(String[] a) throws Exception {
        System.loadLibrary("sbjni");
        HexFormat hex = HexFormat.of();
        boolean relay = a[1].equals("1");
        long h = SbCore.create("192.168.4.1", -1, false, 0, 0, 15.0, relay);
        List<String> lines = Files.readAllLines(Paths.get(a[0]));
        double next = -1;
        int tickNo = 0;
        StringBuilder out = new StringBuilder();
        for (String line : lines) {
            if (line.isEmpty() || line.startsWith("#")) continue;
            String[] p = line.split(" ");
            double t = Double.parseDouble(p[0]);
            byte[] d = hex.parseHex(p[1]);
            if (next < 0) next = t + 1.0;
            while (t >= next) {
                List<String> frames = new ArrayList<>();
                for (byte[] f : SbCore.tick(h, next)) frames.add(hex.formatHex(f));
                Collections.sort(frames);
                for (String f : frames) out.append("T ").append(tickNo).append(' ').append(f).append('\n');
                tickNo++;
                next += 1.0;
            }
            for (byte[] f : SbCore.feed(h, d, d.length, "192.168.4.1", t)) out.append("R ").append(hex.formatHex(f)).append('\n');
            out.append("W ").append(SbCore.wantsEnable(h, t) ? 1 : 0).append('\n');
        }
        int[] s = SbCore.stats(h);
        StringBuilder sl = new StringBuilder("S");
        for (int v : s) sl.append(' ').append(v);
        out.append(sl).append('\n');
        StringBuilder counts = new StringBuilder("C");
        for (int value : SbCore.frameCounts(h)) counts.append(' ').append(value);
        out.append(counts).append('\n');
        out.append("U ").append(SbCore.summary(h)).append('\n');
        out.append("N ").append(SbCore.streamStarts(h)).append('\n');
        out.append("V ").append(SbCore.version()).append('\n');
        for (String l : SbCore.drainLog(h)) out.append("L ").append(l).append('\n');
        out.append("P ").append(SbCore.powerSavingsOn("{\"Power\":{\"autoOnEnabled\":false,\"powerSavings\":true}}"))
           .append(' ').append(SbCore.powerSavingsOn("{\"Power\":{\"powerSavings\":false}}"))
           .append(' ').append(SbCore.powerSavingsOn("null pagebuffer")).append('\n');
        byte[] ann = "{\"App\":\"ForeFlight\",\"GDL90\":{\"port\":4000}}".getBytes("UTF-8");
        out.append("A ").append(SbCore.parseAnnounce(ann, ann.length)).append('\n');
        byte[] bad = "i-want-to-play-ffm-udp".getBytes("UTF-8");
        out.append("B ").append(SbCore.parseAnnounce(bad, bad.length)).append('\n');
        SbCore.destroy(h);
        System.out.print(out);
    }
}
