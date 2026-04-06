#include <stdio.h>
#include <string.h>

#define MW 4
typedef unsigned long long u64;
typedef struct { u64 w[MW]; } Mask;

static inline void mset(Mask *m, int i) { m->w[i>>6] |= 1ULL<<(i&63); }
static inline int mtest(const Mask *m, int i) { return (m->w[i>>6]>>(i&63))&1; }
static inline int mand_nonzero(const Mask *a, const Mask *b) {
    return (a->w[0]&b->w[0])|(a->w[1]&b->w[1])|(a->w[2]&b->w[2])|(a->w[3]&b->w[3]);
}
static inline int mpop(const Mask *m) {
    return __builtin_popcountll(m->w[0])+__builtin_popcountll(m->w[1])
          +__builtin_popcountll(m->w[2])+__builtin_popcountll(m->w[3]);
}

#define MAX_BLOCKS 256
static int N;
static Mask covered_by[MAX_BLOCKS];

void test_init(int n, u64 *cb_raw) {
    N = n;
    for (int i=0; i<n; i++)
        memcpy(covered_by[i].w, cb_raw+i*MW, MW*sizeof(u64));
}

int test_get_available(int n) {
    Mask pile;
    memset(&pile, 0, sizeof(pile));
    for (int i=0; i<n; i++) mset(&pile, i);

    Mask avail = pile;
    for (int ww=0; ww<MW; ww++) {
        u64 bits = pile.w[ww];
        while (bits) {
            int b = __builtin_ctzll(bits);
            int idx = ww*64+b;
            if (idx >= n) break;
            if (mand_nonzero(&covered_by[idx], &pile))
                avail.w[ww] &= ~(1ULL<<b);
            bits &= bits-1;
        }
    }
    return mpop(&avail);
}

int test_cb_pop(int idx) {
    return mpop(&covered_by[idx]);
}
