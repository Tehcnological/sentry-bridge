/*
 * JNI wrapper around core/sbcore for the Android app (io.github.tehcnological.sentrybridge.SbCore).
 *
 * The Kotlin service owns sockets, HTTP and timing; this file only moves bytes between the JVM
 * and the core. Not thread-safe: the caller serializes access to a handle.
 */
#include <jni.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sbcore.h"

#define MAX_COLLECT 512
/* Relayed frames are the Sentry's own, up to a full datagram: a FIS-B uplink (0x07) is ~440 bytes,
   far over SB_MAX_FRAME (traffic reports only). One UDP datagram is at most 65507 bytes. */
#define POOL_BYTES (128 * 1024)
#define LOG_SLOTS 64
#define LOG_LEN 200

typedef struct {
    sb_bridge b;
    uint8_t pool[POOL_BYTES];
    size_t offs[MAX_COLLECT], lens[MAX_COLLECT];
    size_t used;
    int nframes;
    int dropped;
    char logs[LOG_SLOTS][LOG_LEN];
    int log_head, log_count;
} handle_t;

static void on_log(void *ctx, const char *msg) {
    handle_t *h = (handle_t *)ctx;
    int slot = (h->log_head + h->log_count) % LOG_SLOTS;
    strncpy(h->logs[slot], msg, LOG_LEN - 1);
    h->logs[slot][LOG_LEN - 1] = 0;
    if (h->log_count < LOG_SLOTS) h->log_count++;
    else h->log_head = (h->log_head + 1) % LOG_SLOTS;  /* overwrite the oldest */
}

static void on_emit(void *ctx, const uint8_t *frame, size_t len) {
    handle_t *h = (handle_t *)ctx;
    if (h->nframes >= MAX_COLLECT || len > POOL_BYTES - h->used) { h->dropped++; return; }
    memcpy(h->pool + h->used, frame, len);
    h->offs[h->nframes] = h->used;
    h->lens[h->nframes] = len;
    h->used += len;
    h->nframes++;
}

static jobjectArray collected(JNIEnv *env, handle_t *h) {
    jclass bytes_cls = (*env)->FindClass(env, "[B");
    jobjectArray out = (*env)->NewObjectArray(env, h->nframes, bytes_cls, NULL);
    for (int i = 0; out && i < h->nframes; i++) {
        jbyteArray a = (*env)->NewByteArray(env, (jsize)h->lens[i]);
        if (!a) break;
        (*env)->SetByteArrayRegion(env, a, 0, (jsize)h->lens[i], (const jbyte *)(h->pool + h->offs[i]));
        (*env)->SetObjectArrayElement(env, out, i, a);
        (*env)->DeleteLocalRef(env, a);
    }
    (*env)->DeleteLocalRef(env, bytes_cls);
    if (h->dropped) {
        on_log(h, "too many frames in one call; extra frames dropped");
        h->dropped = 0;
    }
    h->nframes = 0;
    h->used = 0;
    return out;
}

static uint32_t ip_from_jstring(JNIEnv *env, jstring s) {
    uint32_t ip = 0;
    if (!s) return 0;
    const char *c = (*env)->GetStringUTFChars(env, s, NULL);
    if (c) {
        if (!sb_parse_ipv4(c, &ip)) ip = 0;
        (*env)->ReleaseStringUTFChars(env, s, c);
    }
    return ip;
}

#define FN(name) Java_io_github_tehcnological_sentrybridge_SbCore_##name
#define H ((handle_t *)(intptr_t)handle)

JNIEXPORT jstring JNICALL FN(version)(JNIEnv *env, jobject self) {
    (void)self;
    return (*env)->NewStringUTF(env, SB_VERSION);
}

JNIEXPORT jlong JNICALL FN(create)(JNIEnv *env, jobject self, jstring sentry_ip, jint ownship_icao,
                                   jboolean has_ref, jdouble ref_lat, jdouble ref_lon, jdouble stale_s,
                                   jboolean relay) {
    (void)self;
    handle_t *h = (handle_t *)calloc(1, sizeof(handle_t));
    if (!h) return 0;
    sb_config cfg;
    sb_config_defaults(&cfg);
    cfg.sentry_ip = ip_from_jstring(env, sentry_ip);   /* "" or unparseable = accept any */
    cfg.ownship_icao = ownship_icao;
    cfg.has_ref = has_ref ? 1 : 0;
    cfg.ref_lat = ref_lat;
    cfg.ref_lon = ref_lon;
    if (stale_s > 0) cfg.stale_s = stale_s;
    cfg.relay = relay ? 1 : 0;
    cfg.log = on_log;
    cfg.log_ctx = h;
    sb_init(&h->b, &cfg);
    return (jlong)(intptr_t)h;
}

JNIEXPORT void JNICALL FN(destroy)(JNIEnv *env, jobject self, jlong handle) {
    (void)env; (void)self;
    free(H);
}

JNIEXPORT jobjectArray JNICALL FN(feed)(JNIEnv *env, jobject self, jlong handle, jbyteArray data, jint len,
                                        jstring src_ip, jdouble now) {
    (void)self;
    handle_t *h = H;
    jsize n = (*env)->GetArrayLength(env, data);
    if (len < 0 || len > n) len = n;
    jbyte *bytes = (*env)->GetByteArrayElements(env, data, NULL);
    if (!bytes) return NULL;
    h->nframes = 0;
    h->used = 0;
    sb_feed(&h->b, (const uint8_t *)bytes, (size_t)len, ip_from_jstring(env, src_ip), now, on_emit, h);
    (*env)->ReleaseByteArrayElements(env, data, bytes, JNI_ABORT);
    return collected(env, h);
}

JNIEXPORT jobjectArray JNICALL FN(tick)(JNIEnv *env, jobject self, jlong handle, jdouble now) {
    (void)self;
    handle_t *h = H;
    h->nframes = 0;
    h->used = 0;
    sb_tick(&h->b, now, on_emit, h);
    return collected(env, h);
}

JNIEXPORT jboolean JNICALL FN(wantsEnable)(JNIEnv *env, jobject self, jlong handle, jdouble now) {
    (void)env; (void)self;
    return sb_wants_enable(&H->b, now) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jint JNICALL FN(streamStarts)(JNIEnv *env, jobject self, jlong handle) {
    (void)env; (void)self;
    return (jint)sb_stream_starts(&H->b);
}

JNIEXPORT jstring JNICALL FN(summary)(JNIEnv *env, jobject self, jlong handle) {
    (void)self;
    char buf[512];
    sb_summary(&H->b, buf, sizeof buf);
    return (*env)->NewStringUTF(env, buf);
}

/* [mode, gps_valid, datagrams, crc_bad, modes_msgs, uat_msgs, uat_bad, sent, targets, positioned,
    positioned_uat, pos_rejected] */
JNIEXPORT jintArray JNICALL FN(stats)(JNIEnv *env, jobject self, jlong handle) {
    (void)self;
    const sb_bridge *b = &H->b;
    const sb_stats *s = sb_get_stats(b);
    jint targets = 0, positioned = 0, positioned_uat = 0;
    for (int i = 0; i < SB_MAX_TARGETS; i++) {
        const sb_target *t = &b->targets[i];
        if (!t->used) continue;
        targets++;
        if (t->has_pos && !t->lost) {
            positioned++;
            if (t->uat) positioned_uat++;
        }
    }
    jint v[12] = {(jint)sb_get_mode(b), b->gps_valid ? 1 : 0, (jint)s->datagrams, (jint)s->crc_bad,
                  (jint)s->modes_msgs, (jint)s->uat_msgs, (jint)s->uat_bad, (jint)s->sent,
                  targets, positioned, positioned_uat, (jint)s->pos_rejected};
    jintArray out = (*env)->NewIntArray(env, 12);
    if (out) (*env)->SetIntArrayRegion(env, out, 0, 12, v);
    return out;
}

JNIEXPORT jobjectArray JNICALL FN(drainLog)(JNIEnv *env, jobject self, jlong handle) {
    (void)self;
    handle_t *h = H;
    jclass str_cls = (*env)->FindClass(env, "java/lang/String");
    jobjectArray out = (*env)->NewObjectArray(env, h->log_count, str_cls, NULL);
    for (int i = 0; out && i < h->log_count; i++) {
        jstring s = (*env)->NewStringUTF(env, h->logs[(h->log_head + i) % LOG_SLOTS]);
        (*env)->SetObjectArrayElement(env, out, i, s);
        (*env)->DeleteLocalRef(env, s);
    }
    (*env)->DeleteLocalRef(env, str_cls);
    h->log_head = h->log_count = 0;
    return out;
}

JNIEXPORT jintArray JNICALL FN(frameCounts)(JNIEnv *env, jobject self, jlong handle) {
    (void)self;
    const sb_stats *s = sb_get_stats(&H->b);
    jint values[256];
    for (int i = 0; i < 256; i++) values[i] = (jint)s->frames[i];
    jintArray out = (*env)->NewIntArray(env, 256);
    if (out) (*env)->SetIntArrayRegion(env, out, 0, 256, values);
    return out;
}

/* 1 on, 0 off, -1 unreadable */
JNIEXPORT jint JNICALL FN(powerSavingsOn)(JNIEnv *env, jobject self, jstring json) {
    (void)self;
    if (!json) return -1;
    const char *c = (*env)->GetStringUTFChars(env, json, NULL);
    if (!c) return -1;
    int r = sb_power_savings_on(c, strlen(c));
    (*env)->ReleaseStringUTFChars(env, json, c);
    return r;
}

/* "port\tapp" for a valid EFB discovery announce, else null */
JNIEXPORT jstring JNICALL FN(parseAnnounce)(JNIEnv *env, jobject self, jbyteArray data, jint len) {
    (void)self;
    jsize n = (*env)->GetArrayLength(env, data);
    if (len < 0 || len > n) len = n;
    jbyte *bytes = (*env)->GetByteArrayElements(env, data, NULL);
    if (!bytes) return NULL;
    char app[64];
    int port = sb_parse_announce((const char *)bytes, (size_t)len, app, sizeof app);
    (*env)->ReleaseByteArrayElements(env, data, bytes, JNI_ABORT);
    if (port < 0) return NULL;
    char out[96];
    snprintf(out, sizeof out, "%d\t%s", port, app);
    return (*env)->NewStringUTF(env, out);
}
