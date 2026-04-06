#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_BLOCKS 256
#define MASK_WORDS 4
#define MAX_TYPES 20
#define MAX_HAND 7

typedef unsigned long long u64;
typedef struct { u64 w[MASK_WORDS]; } Mask;
static inline Mask mask_zero(void) { Mask m; m.w[0]=m.w[1]=m.w[2]=m.w[3]=0; return m; }
static inline Mask mask_bit(int i) { Mask m=mask_zero(); m.w[i/64]|=(1ULL<<(i%64)); return m; }
static inline Mask mask_and(Mask a,Mask b) { Mask r; for(int i=0;i<4;i++) r.w[i]=a.w[i]&b.w[i]; return r; }
static inline Mask mask_xor(Mask a,Mask b) { Mask r; for(int i=0;i<4;i++) r.w[i]=a.w[i]^b.w[i]; return r; }
static inline Mask mask_or(Mask a,Mask b) { Mask r; for(int i=0;i<4;i++) r.w[i]=a.w[i]|b.w[i]; return r; }
static inline int mask_is_zero(Mask m) { return (m.w[0]|m.w[1]|m.w[2]|m.w[3])==0; }
static inline int mask_popcount(Mask m) { return __builtin_popcountll(m.w[0])+__builtin_popcountll(m.w[1])+__builtin_popcountll(m.w[2])+__builtin_popcountll(m.w[3]); }
static inline int mask_lowest_bit(Mask m) { for(int i=0;i<4;i++) if(m.w[i]) return i*64+__builtin_ctzll(m.w[i]); return -1; }
static inline Mask mask_clear_lowest(Mask m) { for(int i=0;i<4;i++) if(m.w[i]){m.w[i]&=m.w[i]-1;break;} return m; }

int main() {
    // Simple test: hand tracking
    int counts[MAX_TYPES+1];
    memset(counts, 0, sizeof(counts));
    int size = 0;
    
    // Pick type 5 three times
    for (int i=0; i<3; i++) {
        counts[5]++;
        size++;
        printf("After pick %d: counts[5]=%d, size=%d\n", i+1, counts[5], size);
        if (counts[5] >= 3) {
            counts[5] -= 3;
            size -= 3;
            printf("  MATCH! counts[5]=%d, size=%d\n", counts[5], size);
        }
    }
    
    // Pick 7 different types without matching
    memset(counts, 0, sizeof(counts));
    size = 0;
    for (int t=1; t<=7; t++) {
        counts[t]++;
        size++;
    }
    printf("\n7 different types, size=%d, should be 7\n", size);
    printf("size >= 7 check: %d\n", size >= 7);
    
    return 0;
}
