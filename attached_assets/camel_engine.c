#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

#define MAX_BLOCKS 256
#define MASK_WORDS 4
#define MAX_TYPES 20
#define MAX_HAND 7
#define MAX_PATH 230
#define NUM_VARIANTS 20

typedef unsigned long long u64;

typedef struct {
    u64 w[MASK_WORDS];
} Mask;

static inline Mask mask_zero(void) {
    Mask m; m.w[0]=m.w[1]=m.w[2]=m.w[3]=0; return m;
}
static inline Mask mask_bit(int i) {
    Mask m = mask_zero();
    m.w[i/64] |= (1ULL << (i%64));
    return m;
}
static inline int mask_test(Mask m, int i) {
    return (m.w[i/64] >> (i%64)) & 1;
}
static inline Mask mask_and(Mask a, Mask b) {
    Mask r; for(int i=0;i<MASK_WORDS;i++) r.w[i]=a.w[i]&b.w[i]; return r;
}
static inline Mask mask_or(Mask a, Mask b) {
    Mask r; for(int i=0;i<MASK_WORDS;i++) r.w[i]=a.w[i]|b.w[i]; return r;
}
static inline Mask mask_xor(Mask a, Mask b) {
    Mask r; for(int i=0;i<MASK_WORDS;i++) r.w[i]=a.w[i]^b.w[i]; return r;
}
static inline Mask mask_andnot(Mask a, Mask b) {
    Mask r; for(int i=0;i<MASK_WORDS;i++) r.w[i]=a.w[i]&~b.w[i]; return r;
}
static inline int mask_is_zero(Mask m) {
    return (m.w[0]|m.w[1]|m.w[2]|m.w[3]) == 0;
}
static inline int mask_popcount(Mask m) {
    return __builtin_popcountll(m.w[0]) + __builtin_popcountll(m.w[1]) +
           __builtin_popcountll(m.w[2]) + __builtin_popcountll(m.w[3]);
}
static inline int mask_lowest_bit(Mask m) {
    for (int i=0; i<MASK_WORDS; i++)
        if (m.w[i]) return i*64 + __builtin_ctzll(m.w[i]);
    return -1;
}
static inline Mask mask_clear_lowest(Mask m) {
    for (int i=0; i<MASK_WORDS; i++)
        if (m.w[i]) { m.w[i] &= m.w[i]-1; break; }
    return m;
}

typedef struct {
    int n;
    int btype[MAX_BLOCKS];
    int layer[MAX_BLOCKS];
    Mask bit_mask[MAX_BLOCKS];
    Mask covered_by[MAX_BLOCKS];
    Mask covers[MAX_BLOCKS];
    Mask type_mask[MAX_TYPES+1];
    int n_types;
} Level;

static Level g_level;

static int unlock_w[NUM_VARIANTS] = {600,800,400,1000,200,900,1200,300,700,500,150,1500,2000,0,0,0,1500,100,50,1800};
static int avail_w[NUM_VARIANTS]  = {400,600,300,200,100,500,800,150,700,350,50,250,0,2000,0,0,1500,100,1200,200};
static int depth_w[NUM_VARIANTS]  = {200,100,300,400,50,350,500,250,150,600,25,100,0,0,2000,0,0,1000,800,50};
static int hand_w[NUM_VARIANTS]   = {400,300,500,600,800,250,200,700,350,150,900,450,0,0,0,2000,100,1000,100,1200};

void level_init(int n, int* btypes, int* layers, int* covered_by_flat, int* covers_flat, int n_types) {
    g_level.n = n;
    g_level.n_types = n_types;
    for (int i=0; i<n; i++) {
        g_level.btype[i] = btypes[i];
        g_level.layer[i] = layers[i];
        g_level.bit_mask[i] = mask_bit(i);
    }
    for (int i=0; i<=MAX_TYPES; i++) g_level.type_mask[i] = mask_zero();
    for (int i=0; i<n; i++) {
        int t = g_level.btype[i];
        if (t >= 0 && t <= MAX_TYPES)
            g_level.type_mask[t] = mask_or(g_level.type_mask[t], g_level.bit_mask[i]);
    }
    for (int i=0; i<n; i++) {
        g_level.covered_by[i] = mask_zero();
        g_level.covers[i] = mask_zero();
    }
    for (int i=0; i<n; i++) {
        for (int w=0; w<MASK_WORDS; w++) {
            u64 val = 0;
            for (int b=0; b<64 && w*64+b < n; b++) {
                if (covered_by_flat[i*n + w*64+b])
                    val |= (1ULL << b);
            }
            g_level.covered_by[i].w[w] = val;
        }
    }
    for (int i=0; i<n; i++) {
        for (int w=0; w<MASK_WORDS; w++) {
            u64 val = 0;
            for (int b=0; b<64 && w*64+b < n; b++) {
                if (covers_flat[i*n + w*64+b])
                    val |= (1ULL << b);
            }
            g_level.covers[i].w[w] = val;
        }
    }
}

static Mask get_available(Mask pile) {
    Mask avail = pile;
    Mask rem = pile;
    while (!mask_is_zero(rem)) {
        int idx = mask_lowest_bit(rem);
        if (!mask_is_zero(mask_and(g_level.covered_by[idx], pile)))
            avail = mask_xor(avail, g_level.bit_mask[idx]);
        rem = mask_clear_lowest(rem);
    }
    return avail;
}

static int count_unlocks(Mask pile, int idx) {
    Mask new_pile = mask_xor(pile, g_level.bit_mask[idx]);
    Mask below = mask_and(g_level.covers[idx], pile);
    int count = 0;
    while (!mask_is_zero(below)) {
        int j = mask_lowest_bit(below);
        if (mask_is_zero(mask_and(g_level.covered_by[j], new_pile)))
            count++;
        below = mask_clear_lowest(below);
    }
    return count;
}

static int depth_below(Mask pile, int idx) {
    Mask below = mask_and(g_level.covers[idx], pile);
    if (mask_is_zero(below)) return 0;
    int seen_layers[20];
    int n_layers = 0;
    while (!mask_is_zero(below)) {
        int j = mask_lowest_bit(below);
        int l = g_level.layer[j];
        int found = 0;
        for (int k=0; k<n_layers; k++) if (seen_layers[k]==l) { found=1; break; }
        if (!found && n_layers<20) seen_layers[n_layers++] = l;
        below = mask_clear_lowest(below);
    }
    return n_layers;
}

static int min_blockers_for_type(Mask pile, int btype) {
    if (btype < 0 || btype > MAX_TYPES) return 999;
    Mask tmask = mask_and(pile, g_level.type_mask[btype]);
    if (mask_is_zero(tmask)) return 999;
    int best = 999;
    while (!mask_is_zero(tmask)) {
        int j = mask_lowest_bit(tmask);
        int cnt = mask_popcount(mask_and(g_level.covered_by[j], pile));
        if (cnt < best) { best = cnt; if (best == 0) break; }
        tmask = mask_clear_lowest(tmask);
    }
    return best;
}

typedef struct {
    int counts[MAX_TYPES+1];
    int size;
} Hand;

static Hand hand_init(void) {
    Hand h; memset(h.counts, 0, sizeof(h.counts)); h.size = 0; return h;
}

static void apply_pick(Mask *pile, Hand *h, int idx) {
    *pile = mask_xor(*pile, g_level.bit_mask[idx]);
    int bt = g_level.btype[idx];
    h->counts[bt]++;
    h->size++;
    if (h->counts[bt] >= 3) {
        h->counts[bt] -= 3;
        h->size -= 3;
    }
}

static int auto_match(Mask *pile, Hand *h, int *path, int *path_len, int smart) {
    int changed = 1;
    while (changed) {
        changed = 0;
        Mask avail = get_available(*pile);
        if (mask_is_zero(avail)) break;
        if (smart) {
            int best_i = -1, best_unlocks = -1;
            Mask rem = avail;
            while (!mask_is_zero(rem)) {
                int i = mask_lowest_bit(rem);
                if (h->counts[g_level.btype[i]] >= 2) {
                    int u = count_unlocks(*pile, i);
                    if (u > best_unlocks) { best_unlocks = u; best_i = i; }
                }
                rem = mask_clear_lowest(rem);
            }
            if (best_i >= 0) {
                apply_pick(pile, h, best_i);
                if (*path_len < MAX_PATH) path[(*path_len)++] = best_i;
                changed = 1;
            }
        } else {
            Mask rem = avail;
            while (!mask_is_zero(rem)) {
                int i = mask_lowest_bit(rem);
                if (h->counts[g_level.btype[i]] >= 2) {
                    apply_pick(pile, h, i);
                    if (*path_len < MAX_PATH) path[(*path_len)++] = i;
                    changed = 1;
                    break;
                }
                rem = mask_clear_lowest(rem);
            }
        }
    }
    return *path_len;
}

static double score_move(Mask pile, Hand *h, int i, int variant, int relaxed,
                         Mask *out_pile, Hand *out_h) {
    int bt = g_level.btype[i];
    int ih = h->counts[bt];

    Hand nh = *h;
    Mask np = pile;
    apply_pick(&np, &nh, i);

    int matched = (nh.size < h->size + 1) ? 1 : 0;

    if (nh.size >= 7 && !matched) return -1e18;

    Mask remaining_mask = np;
    int dead_pairs = 0;
    for (int t=1; t<=g_level.n_types; t++) {
        if (nh.counts[t] == 2) {
            if (mask_is_zero(mask_and(remaining_mask, g_level.type_mask[t])))
                dead_pairs++;
        }
    }
    if (!relaxed) {
        if (dead_pairs >= 2) return -1e18;
        if (dead_pairs >= 1 && nh.size >= 6) return -1e18;
    }

    Mask avail_after = get_available(np);
    int avail_count = mask_popcount(avail_after);

    double s = 0;
    if (matched) s += 15000;
    if (dead_pairs) s -= 12000;

    if (ih == 2) {
        s += 20000;
    } else if (ih == 1) {
        int third_avail = mask_popcount(mask_and(avail_after, g_level.type_mask[bt]));
        if (third_avail > 0) s += 8000;
        else {
            int mb = min_blockers_for_type(np, bt);
            if (mb <= 1) s += 3000;
            else if (mb <= 2) s += 500;
            else s -= 2000 - mb * 300;
        }
    } else if (ih == 0) {
        int board_left = mask_popcount(mask_and(np, g_level.type_mask[bt]));
        if (board_left < 2) s -= 15000;
        int visible = mask_popcount(mask_and(avail_after, g_level.type_mask[bt]));
        if (visible >= 2) s += 6000;
        else if (visible >= 1) s += 2000;
        else if (board_left >= 2) {
            int mb = min_blockers_for_type(np, bt);
            if (nh.size >= 4) s -= 2000 - mb * 400;
            else s -= 300;
        }
    }

    int v = variant % NUM_VARIANTS;
    s += avail_count * avail_w[v];
    s += count_unlocks(pile, i) * unlock_w[v];
    s += depth_below(pile, i) * depth_w[v];
    s += g_level.layer[i] * 100;

    int zombie = 0;
    for (int t=1; t<=g_level.n_types; t++) {
        if (nh.counts[t] > 0 && nh.counts[t] < 3) {
            if (mask_popcount(mask_and(avail_after, g_level.type_mask[t])) == 0) {
                if (mask_popcount(mask_and(np, g_level.type_mask[t])) > 0)
                    zombie++;
            }
        }
    }
    s -= zombie * 1500;
    s -= nh.size * hand_w[v];
    if (nh.size >= 5) s -= 3000;
    if (nh.size >= 6) s -= 6000;

    int open_types = 0;
    for (int t=1; t<=g_level.n_types; t++)
        if (nh.counts[t] > 0 && nh.counts[t] < 3) open_types++;
    if (open_types >= 4) s -= (open_types - 3) * 1500;

    *out_pile = np;
    *out_h = nh;
    return s;
}

static unsigned int xorshift_state;
static void xorshift_seed(unsigned int s) { xorshift_state = s ? s : 1; }
static unsigned int xorshift_next(void) {
    unsigned int x = xorshift_state;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    xorshift_state = x;
    return x;
}
static double xorshift_gauss(void) {
    double u1 = (xorshift_next() % 1000000 + 1) / 1000001.0;
    double u2 = (xorshift_next() % 1000000 + 1) / 1000001.0;
    return sqrt(-2.0 * log(u1)) * cos(6.283185307179586 * u2);
}

static int run_one_trial(Mask pile, Hand hand, int seed, int noise, int variant,
                         int use_smart, int use_relaxed, int *path) {
    int path_len = 0;

    while (path_len < MAX_PATH) {
        auto_match(&pile, &hand, path, &path_len, use_smart);
        Mask avail = get_available(pile);
        if (mask_is_zero(avail)) break;

        double best_score = -1e30;
        int best_i = -1;
        Mask best_pile;
        Hand best_hand;

        Mask rem = avail;
        while (!mask_is_zero(rem)) {
            int i = mask_lowest_bit(rem);
            Mask np; Hand nh;
            double sc = score_move(pile, &hand, i, variant, use_relaxed, &np, &nh);
            if (sc <= -1e17) { rem = mask_clear_lowest(rem); continue; }
            if (noise > 0) sc += xorshift_gauss() * noise;
            if (sc > best_score) {
                best_score = sc; best_i = i; best_pile = np; best_hand = nh;
            }
            rem = mask_clear_lowest(rem);
        }
        if (best_i < 0) break;
        pile = best_pile;
        hand = best_hand;
        if (path_len < MAX_PATH) path[path_len++] = best_i;
    }
    return path_len;
}

int plan_solution(int *init_held, int init_held_size, double time_limit,
                  int *out_path, int *out_len) {
    struct timespec ts_start, ts_now;
    clock_gettime(CLOCK_MONOTONIC, &ts_start);

    Mask pile = mask_zero();
    for (int i=0; i<g_level.n; i++)
        pile = mask_or(pile, g_level.bit_mask[i]);

    Hand hand = hand_init();
    for (int t=0; t<=MAX_TYPES; t++) hand.counts[t] = init_held[t];
    hand.size = init_held_size;

    int best_path[MAX_PATH];
    int best_len = 0;

    int noise_levels[] = {0, 100, 200, 500, 1000, 2000, 3000, 5000, 8000, 12000};
    int n_noise = 10;
    int trial_count = 0;

    double beam_end = time_limit * 0.50;
    double noisy_end = time_limit * 0.90;
    double bt_end = time_limit * 0.95;

    for (int variant=0; variant<6; variant++) {
        for (int us=0; us<2; us++) {
            clock_gettime(CLOCK_MONOTONIC, &ts_now);
            double elapsed = (ts_now.tv_sec - ts_start.tv_sec) + (ts_now.tv_nsec - ts_start.tv_nsec)*1e-9;
            if (elapsed > beam_end) goto beam_done;

            Mask p = pile;
            Hand h = hand;
            int path[MAX_PATH];
            int plen = 0;
            auto_match(&p, &h, path, &plen, us);

            if (plen > best_len) {
                best_len = plen;
                memcpy(best_path, path, plen * sizeof(int));
            }

            for (int round=0; round<225; round++) {
                clock_gettime(CLOCK_MONOTONIC, &ts_now);
                elapsed = (ts_now.tv_sec - ts_start.tv_sec) + (ts_now.tv_nsec - ts_start.tv_nsec)*1e-9;
                if (elapsed > beam_end) goto beam_done;

                Mask avail = get_available(p);
                if (mask_is_zero(avail)) break;

                double top_score = -1e30;
                int top_i = -1;
                Mask top_pile; Hand top_hand;

                Mask rem = avail;
                while (!mask_is_zero(rem)) {
                    int i = mask_lowest_bit(rem);
                    Mask np; Hand nh;
                    double sc = score_move(p, &h, i, variant, 0, &np, &nh);
                    if (sc <= -1e17) { rem = mask_clear_lowest(rem); continue; }
                    if (sc > top_score) {
                        top_score = sc; top_i = i; top_pile = np; top_hand = nh;
                    }
                    rem = mask_clear_lowest(rem);
                }
                if (top_i < 0) break;

                p = top_pile; h = top_hand;
                if (plen < MAX_PATH) path[plen++] = top_i;

                auto_match(&p, &h, path, &plen, us);

                if (plen > best_len) {
                    best_len = plen;
                    memcpy(best_path, path, plen * sizeof(int));
                }
            }
        }
    }
beam_done:

    for (int ni=0; ni<n_noise; ni++) {
        int noise = noise_levels[ni];
        for (int seed=0; seed<300; seed++) {
            clock_gettime(CLOCK_MONOTONIC, &ts_now);
            double elapsed = (ts_now.tv_sec - ts_start.tv_sec) + (ts_now.tv_nsec - ts_start.tv_nsec)*1e-9;
            if (elapsed > noisy_end) goto noisy_done;
            trial_count++;

            int use_smart = seed % 2 == 0;
            int variant = seed % 20;
            int use_relaxed = seed % 4 != 0;
            xorshift_seed((unsigned int)(seed * 100 + noise + 1));

            Hand h = hand;
            Mask p = pile;
            int path[MAX_PATH];
            int plen = 0;

            plen = run_one_trial(p, h, seed, noise, variant, use_smart, use_relaxed, path);

            if (plen > best_len) {
                best_len = plen;
                memcpy(best_path, path, plen * sizeof(int));
            }
        }
    }
noisy_done:

    if (best_len < 150 && best_len > 0) {
        for (int bt_seed=0; bt_seed<50000; bt_seed++) {
            clock_gettime(CLOCK_MONOTONIC, &ts_now);
            double elapsed = (ts_now.tv_sec - ts_start.tv_sec) + (ts_now.tv_nsec - ts_start.tv_nsec)*1e-9;
            if (elapsed > bt_end) break;

            xorshift_seed((unsigned int)(bt_seed * 31337 + 7));
            int use_smart = bt_seed % 2 == 0;
            int variant = bt_seed % 20;

            int bp = best_len - 40;
            if (bp < 0) bp = 0;
            int range = best_len - 5 - bp;
            if (range <= 0) range = 1;
            int backtrack_point = bp + (xorshift_next() % range);

            Hand h = hand;
            Mask p = pile;
            int ok = 1;
            for (int s=0; s<backtrack_point && s<best_len; s++) {
                int bi = best_path[s];
                if (!mask_test(p, bi)) { ok = 0; break; }
                apply_pick(&p, &h, bi);
            }
            if (!ok) continue;

            int path[MAX_PATH];
            memcpy(path, best_path, backtrack_point * sizeof(int));
            int plen = backtrack_point;

            while (plen < MAX_PATH) {
                auto_match(&p, &h, path, &plen, use_smart);
                Mask avail = get_available(p);
                if (mask_is_zero(avail)) break;

                double top_score = -1e30;
                int top_i = -1;
                Mask top_pile; Hand top_hand;

                Mask rem = avail;
                while (!mask_is_zero(rem)) {
                    int i = mask_lowest_bit(rem);
                    Mask np; Hand nh;
                    double sc = score_move(p, &h, i, variant, 0, &np, &nh);
                    if (sc <= -1e17) { rem = mask_clear_lowest(rem); continue; }
                    sc += xorshift_gauss() * 3000;
                    if (sc > top_score) {
                        top_score = sc; top_i = i; top_pile = np; top_hand = nh;
                    }
                    rem = mask_clear_lowest(rem);
                }
                if (top_i < 0) break;
                p = top_pile; h = top_hand;
                if (plen < MAX_PATH) path[plen++] = top_i;
            }

            if (plen > best_len) {
                best_len = plen;
                memcpy(best_path, path, plen * sizeof(int));
            }
        }
    }

    if (best_len < 150) {
        for (int rs=0; rs<10000; rs++) {
            clock_gettime(CLOCK_MONOTONIC, &ts_now);
            double elapsed = (ts_now.tv_sec - ts_start.tv_sec) + (ts_now.tv_nsec - ts_start.tv_nsec)*1e-9;
            if (elapsed > time_limit) break;

            xorshift_seed((unsigned int)(rs * 54321 + 99));
            int use_smart = rs % 3 != 2;
            int variant = rs % 20;
            int use_relaxed = rs % 4 != 0;

            Hand h = hand;
            Mask p = pile;
            int path[MAX_PATH];
            int plen = run_one_trial(p, h, rs, 1000 + rs*100, variant, use_smart, use_relaxed, path);

            if (plen > best_len) {
                best_len = plen;
                memcpy(best_path, path, plen * sizeof(int));
            }
        }
    }

    *out_len = best_len;
    memcpy(out_path, best_path, best_len * sizeof(int));
    return trial_count;
}
