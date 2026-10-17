/* Deterministic corruption stress test; synthetic inputs only. See docs/Building.md.
 * Usage: fuzz-core [iterations [seed]]. ASan/UBSan make memory/UB failures fatal.
 * This tests robustness, not the correctness of traffic or beyond-radius RS decoding.
 */
#include "sbcore.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t state;
static uint32_t random_u32(void) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return state;
}

static void emit(void *ctx, const uint8_t *frame, size_t len) {
    (void)ctx;
    if (len < 4 || frame[0] != 0x7E || frame[len - 1] != 0x7E) {
        fprintf(stderr, "invalid emitted framing\n");
        abort();
    }
}

/* Receiver settings and EFB announcements, the inputs of the two JSON helpers */
static const char *const json_seeds[] = {
    "{\"App\":\"ForeFlight\",\"GDL90\":{\"port\":4000}}",
    "{\"Version\":\"1\",\"GDL90\":{\"port\":\"4001\"},\"App\":\"EFB\"}",
    "{ \"App\" : \"X\" , \"GDL90\" : { \"port\" : 4000 } } ",
    "{\"Power\":{\"autoOnEnabled\":false,\"powerSavings\":true}}",
    "{\"Power\" : {\"powerSavings\" : false } }",
};

/* Each helper gets a heap copy of exactly len bytes, so ASan stops on any read past the end. */
static void json_case(const char *text, size_t len) {
    char *copy = (char *)malloc(len ? len : 1), app[64];
    if (!copy) abort();
    memcpy(copy, text, len);
    sb_parse_announce(copy, len, app, sizeof app);
    sb_power_savings_on(copy, len);
    free(copy);
}

static void json_checks(unsigned long iterations) {
    size_t s, len;
    unsigned long i;
    char mutated[96];
    for (s = 0; s < sizeof json_seeds / sizeof *json_seeds; s++) /* every truncation */
        for (len = 0; len <= strlen(json_seeds[s]); len++) json_case(json_seeds[s], len);
    for (i = 0; i < iterations; i++) { /* seeded mutations: random bytes rarely form these keys */
        const char *seed = json_seeds[random_u32() % (sizeof json_seeds / sizeof *json_seeds)];
        size_t n = strlen(seed), k;
        memcpy(mutated, seed, n);
        for (k = random_u32() % 4; k > 0; k--)
            mutated[random_u32() % n] = "\"{}: 0a\\"[random_u32() % 8];
        json_case(mutated, random_u32() % (n + 1));
    }
}

int main(int argc, char **argv) {
    static const uint8_t uat_seed[] = {
        0x08,0xa6,0x40,0x78,0x2f,0x54,0x7b,0x60,0xff,0x7c,0x07,0x19,0x00,0xa4,0x1c,0x80,
        0x98,0x09,0xdd,0x00,0x2b,0xae,0x84,0x0b,0x8e,0xa4,0xc2,0xa0,0x00,0x06,0xc0,0x00,
        0x00,0x00,0xfb,0x21,0x8f,0xf1,0xa9,0x6a,0x13,0x81,0x83,0xfb,0x30,0x85,0x9a,0xf6
    };
    static const uint8_t ids[] = {0x00,0x0A,0x21,0x24,0x26,0x07,0x14,0x28};
    sb_bridge b;
    sb_config cfg;
    uint8_t raw[1024], framed[2052], corrected[34];
    unsigned long iterations = argc > 1 ? strtoul(argv[1], NULL, 10) : 100000;
    unsigned long i;
    uint32_t seed = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 0x5E17B01D;
    double now = 0;
    if (!iterations || !seed) return 2;
    state = seed;
    sb_config_defaults(&cfg);
    cfg.sentry_ip = 0;
    cfg.has_ref = 1; cfg.ref_lat = 40; cfg.ref_lon = -100;
    cfg.relay = 1;
    sb_init(&b, &cfg);
    for (i = 0; i < iterations; i++) {
        size_t len = random_u32() % sizeof raw, j, fl;
        unsigned kind = random_u32() % 5;
        int nerr;
        for (j = 0; j < sizeof raw; j++) raw[j] = (uint8_t)random_u32();
        if (kind == 0) {
            sb_feed(&b, raw, len, 0xC0A80401, now, emit, NULL);
        } else {
            if (kind == 1) { /* a valid UAT block, corrupted in both header/data and parity */
                len = 52; raw[0] = 0x24; memset(raw + 1, 0xFF, 3);
                memcpy(raw + 4, uat_seed, sizeof uat_seed);
                for (j = random_u32() % 17; j > 0; j--)
                    raw[4 + random_u32() % 48] ^= (uint8_t)(1 + random_u32() % 255);
            } else if (kind == 2) { /* supported DF17/18 with random ME fields and truncations */
                static const uint8_t dfs[] = {0x8D,0x90,0x91,0x92,0x95};
                len = 4 + random_u32() % 18; raw[0] = 0x21;
                memset(raw + 1, 0xFF, 3); raw[4] = dfs[random_u32() % sizeof dfs];
            } else {
                if (!len) len = 1;
                raw[0] = ids[random_u32() % sizeof ids];
            }
            fl = sb_frame(raw, len, framed, sizeof framed);
            if (kind == 4 && fl) framed[random_u32() % fl] ^= (uint8_t)random_u32();
            sb_feed(&b, framed, fl, 0xC0A80401, now, emit, NULL);
        }
        sb_uat_correct(raw, random_u32() % 64, corrected, &nerr);
        sb_tick(&b, now, emit, NULL);
        sb_wants_enable(&b, now);
        now += (random_u32() % 4 == 0) ? 21.0 : 0.1;
        if (i % 4096 == 4095) sb_init(&b, &cfg);
    }
    json_checks(iterations);
    printf("Completed %lu iterations plus JSON helper checks (seed 0x%08X)\n", iterations, (unsigned)seed);
    return 0;
}
