package io.github.tehcnological.sentrybridge;

/** Desktop twin of the Android app's SbCore: identical native signatures, used to test sb_jni.c on a JVM. */
public final class SbCore {
    private SbCore() {}
    public static native String version();
    public static native long create(String sentryIp, int ownshipIcao, boolean hasRef, double refLat, double refLon,
                                     double staleS, boolean relay);
    public static native void destroy(long h);
    public static native byte[][] feed(long h, byte[] data, int len, String srcIp, double now);
    public static native byte[][] tick(long h, double now);
    public static native boolean wantsEnable(long h, double now);
    public static native int streamStarts(long h);
    public static native String summary(long h);
    public static native int[] stats(long h);
    public static native int[] frameCounts(long h);
    public static native String[] drainLog(long h);
    public static native int powerSavingsOn(String json);
    public static native String parseAnnounce(byte[] data, int len);
}
