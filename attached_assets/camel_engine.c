#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

#ifdef _WIN32
#include <windows.h>
static double _win_freq = 0;
static void _init_timer(void) {
    LARGE_INTEGER f; QueryPerformanceFrequency(&f); _win_freq = (double)f.QuadPart;
}
static double _get_time_s(void) {
    LARGE_INTEGER c; QueryPerformanceCounter(&c); return (double)c.QuadPart / _win_freq;
}
#else
static void _init_timer(void) {}
static double _get_time_s(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}
#endif

#define MAX_BLOCKS 256
#define MW 4
#define MAX_TYPES 20
#define CMAX_PATH 230
#define NUM_VARIANTS 30
#define BEAM_W 250
#define BRANCH 30

typedef unsigned long long u64;
typedef struct { u64 w[MW]; } Mask;

static inline Mask mzero(void) { Mask m={{0,0,0,0}}; return m; }
static inline void mset(Mask *m, int i) { m->w[i>>6] |= 1ULL<<(i&63); }
static inline int mtest(const Mask *m, int i) { return (m->w[i>>6]>>(i&63))&1; }
static inline void mxor_bit(Mask *m, int i) { m->w[i>>6] ^= 1ULL<<(i&63); }
static inline int mis_zero(const Mask *m) {
    return (m->w[0]|m->w[1]|m->w[2]|m->w[3])==0;
}
static inline int mpop(const Mask *m) {
    return __builtin_popcountll(m->w[0])+__builtin_popcountll(m->w[1])
          +__builtin_popcountll(m->w[2])+__builtin_popcountll(m->w[3]);
}
static inline int mand_nonzero(const Mask *a, const Mask *b) {
    return ((a->w[0]&b->w[0])|(a->w[1]&b->w[1])|(a->w[2]&b->w[2])|(a->w[3]&b->w[3])) != 0;
}
static inline int mand_pop(const Mask *a, const Mask *b) {
    return __builtin_popcountll(a->w[0]&b->w[0])+__builtin_popcountll(a->w[1]&b->w[1])
          +__builtin_popcountll(a->w[2]&b->w[2])+__builtin_popcountll(a->w[3]&b->w[3]);
}
static inline Mask mand(Mask a, const Mask *b) {
    a.w[0]&=b->w[0]; a.w[1]&=b->w[1]; a.w[2]&=b->w[2]; a.w[3]&=b->w[3]; return a;
}
static inline int meq(const Mask *a, const Mask *b) {
    return a->w[0]==b->w[0] && a->w[1]==b->w[1] && a->w[2]==b->w[2] && a->w[3]==b->w[3];
}

typedef struct {
    int n, n_types;
    int btype[MAX_BLOCKS];
    int layer[MAX_BLOCKS];
    Mask covered_by[MAX_BLOCKS];
    Mask covers[MAX_BLOCKS];
    Mask type_mask[MAX_TYPES+1];
} Level;

static Level G;

static int unlock_w[NUM_VARIANTS]={600,800,400,1000,200,900,1200,300,700,500,150,1500,2000,0,0,0,1500,100,50,1800, 550,750,950,1100,350,650,1300,450,850,1600};
static int avail_w_t[NUM_VARIANTS]={400,600,300,200,100,500,800,150,700,350,50,250,0,2000,0,0,1500,100,1200,200, 450,550,350,250,150,650,900,500,750,1000};
static int depth_w[NUM_VARIANTS]={200,100,300,400,50,350,500,250,150,600,25,100,0,0,2000,0,0,1000,800,50, 175,225,275,450,75,325,550,125,375,700};
static int hand_w[NUM_VARIANTS]={400,300,500,600,800,250,200,700,350,150,900,450,0,0,0,2000,100,1000,100,1200, 350,450,550,650,750,200,300,800,500,1100};

void level_init(int n, int *btypes, int *layers, u64 *cb_raw, u64 *cv_raw, int n_types) {
    G.n=n; G.n_types=n_types;
    memcpy(G.btype, btypes, n*sizeof(int));
    memcpy(G.layer, layers, n*sizeof(int));
    for (int i=0;i<n;i++) {
        memcpy(G.covered_by[i].w, cb_raw+i*MW, MW*sizeof(u64));
        memcpy(G.covers[i].w, cv_raw+i*MW, MW*sizeof(u64));
    }
    for (int t=0;t<=MAX_TYPES;t++) G.type_mask[t]=mzero();
    for (int i=0;i<n;i++) {
        int t=G.btype[i];
        if (t>=0&&t<=MAX_TYPES) mset(&G.type_mask[t],i);
    }
}

static inline void get_available(const Mask *pile, Mask *out) {
    *out=*pile;
    for (int ww=0;ww<MW;ww++) {
        u64 bits=pile->w[ww];
        while (bits) {
            int b=__builtin_ctzll(bits);
            int idx=ww*64+b;
            if (idx>=G.n) break;
            if (mand_nonzero(&G.covered_by[idx], pile))
                out->w[ww] &= ~(1ULL<<b);
            bits&=bits-1;
        }
    }
}

static int count_unlocks(const Mask *pile, int idx) {
    Mask np=*pile; mxor_bit(&np,idx);
    Mask below=mand(G.covers[idx],pile);
    int count=0;
    for (int ww=0;ww<MW;ww++) {
        u64 bits=below.w[ww];
        while (bits) {
            int j=ww*64+__builtin_ctzll(bits);
            if (!mand_nonzero(&G.covered_by[j],&np)) count++;
            bits&=bits-1;
        }
    }
    return count;
}

static int depth_below(const Mask *pile, int idx) {
    Mask below=mand(G.covers[idx],pile);
    if (mis_zero(&below)) return 0;
    unsigned int seen=0; int cnt=0;
    for (int ww=0;ww<MW;ww++) {
        u64 bits=below.w[ww];
        while (bits) {
            int j=ww*64+__builtin_ctzll(bits);
            int ly=G.layer[j];
            unsigned int bit=1u<<(ly&31);
            if (!(seen&bit)) { seen|=bit; cnt++; }
            bits&=bits-1;
        }
    }
    return cnt;
}

static int min_blockers(const Mask *pile, int bt) {
    if (bt<0||bt>MAX_TYPES) return 999;
    Mask tm=mand(G.type_mask[bt],pile);
    if (mis_zero(&tm)) return 999;
    int best=999;
    for (int ww=0;ww<MW;ww++) {
        u64 bits=tm.w[ww];
        while (bits) {
            int j=ww*64+__builtin_ctzll(bits);
            int c=mand_pop(&G.covered_by[j],pile);
            if (c<best) { best=c; if (!best) return 0; }
            bits&=bits-1;
        }
    }
    return best;
}

typedef struct { int c[MAX_TYPES+1]; int sz; } Hand;

static inline void apply_pick(Mask *pile, Hand *h, int idx) {
    mxor_bit(pile,idx);
    int bt=G.btype[idx];
    h->c[bt]++;
    h->sz++;
    if (h->c[bt]>=3) { h->c[bt]-=3; h->sz-=3; }
}

static void auto_match(Mask *pile, Hand *h, int *path, int *plen, int smart) {
    int changed=1;
    while (changed) {
        changed=0;
        Mask avail; get_available(pile,&avail);
        if (mis_zero(&avail)) break;
        if (smart) {
            int best_i=-1, best_u=-1;
            for (int ww=0;ww<MW;ww++) {
                u64 bits=avail.w[ww];
                while (bits) {
                    int i=ww*64+__builtin_ctzll(bits);
                    if (h->c[G.btype[i]]>=2) {
                        int u=count_unlocks(pile,i);
                        if (u>best_u) { best_u=u; best_i=i; }
                    }
                    bits&=bits-1;
                }
            }
            if (best_i>=0) { apply_pick(pile,h,best_i); if(*plen<CMAX_PATH)path[(*plen)++]=best_i; changed=1; }
        } else {
            for (int ww=0;ww<MW;ww++) {
                u64 bits=avail.w[ww];
                while (bits) {
                    int i=ww*64+__builtin_ctzll(bits);
                    if (h->c[G.btype[i]]>=2) {
                        apply_pick(pile,h,i); if(*plen<CMAX_PATH)path[(*plen)++]=i; changed=1; goto next_am;
                    }
                    bits&=bits-1;
                }
            }
            next_am:;
        }
    }
}

static double score_move(const Mask *pile, const Hand *h, int i, int variant, int relaxed,
                         Mask *op, Hand *oh) {
    int bt=G.btype[i], ih=h->c[bt];
    *oh=*h; *op=*pile;
    apply_pick(op,oh,i);
    int matched=(oh->sz < h->sz+1);
    if (oh->sz>=7 && !matched) return -1e18;

    int dead_pairs=0;
    for (int t=1;t<=G.n_types;t++)
        if (oh->c[t]==2 && !mand_nonzero(op,&G.type_mask[t])) dead_pairs++;
    if (!relaxed) {
        if (dead_pairs>=2) return -1e18;
        if (dead_pairs>=1 && oh->sz>=6) return -1e18;
    }

    Mask aa; get_available(op,&aa);
    int ac=mpop(&aa);
    double s=0;
    if (matched) s+=15000;
    if (dead_pairs) s-=12000;

    if (ih==2) { s+=20000; }
    else if (ih==1) {
        int ta=mand_pop(&aa,&G.type_mask[bt]);
        if (ta>0) s+=8000;
        else { int mb=min_blockers(op,bt); if(mb<=1)s+=3000; else if(mb<=2)s+=500; else s-=2000-mb*300; }
    } else if (ih==0) {
        int bl=mand_pop(op,&G.type_mask[bt]);
        if (bl<2) s-=15000;
        int vi=mand_pop(&aa,&G.type_mask[bt]);
        if (vi>=2) s+=6000; else if(vi>=1) s+=2000;
        else if(bl>=2) { int mb=min_blockers(op,bt); if(oh->sz>=4) s-=2000-mb*400; else s-=300; }
    }

    int v=variant%NUM_VARIANTS;
    s+=ac*avail_w_t[v];
    s+=count_unlocks(pile,i)*unlock_w[v];
    s+=depth_below(pile,i)*depth_w[v];
    s+=G.layer[i]*100;

    int zomb=0;
    for (int t=1;t<=G.n_types;t++)
        if (oh->c[t]>0 && oh->c[t]<3 && !mand_nonzero(&aa,&G.type_mask[t]) && mand_nonzero(op,&G.type_mask[t]))
            zomb++;
    s-=zomb*1500;
    s-=oh->sz*hand_w[v];
    if (oh->sz>=5) s-=3000;
    if (oh->sz>=6) s-=6000;
    int ot=0;
    for (int t=1;t<=G.n_types;t++) if(oh->c[t]>0&&oh->c[t]<3) ot++;
    if (ot>=4) s-=(ot-3)*1500;
    return s;
}

static unsigned int xs;
static inline void xs_seed(unsigned int s) { xs=s?s:1; }
static inline unsigned int xs_next(void) { xs^=xs<<13; xs^=xs>>17; xs^=xs<<5; return xs; }
static inline double xs_gauss(void) {
    double u1=(xs_next()%1000000+1)/1000001.0;
    double u2=(xs_next()%1000000+1)/1000001.0;
    return sqrt(-2.0*log(u1))*cos(6.283185307179586*u2);
}

typedef struct {
    double score;
    Mask pile;
    Hand hand;
    int path[CMAX_PATH];
    int plen;
} BeamState;

static BeamState beams_a[BEAM_W * BRANCH + 10];
static BeamState beams_b[BEAM_W * BRANCH + 10];

typedef struct { double sc; int idx; Mask np; Hand nh; int nsz; } Cand;
static Cand cand_buf[MAX_BLOCKS];

static inline double elapsed_since(double t0) {
    return _get_time_s() - t0;
}

static int cand_cmp(const void *a, const void *b) {
    double da=((Cand*)a)->sc, db=((Cand*)b)->sc;
    return da>db?-1:da<db?1:0;
}
static int beam_cmp(const void *a, const void *b) {
    double da=((BeamState*)a)->score, db=((BeamState*)b)->score;
    return da>db?-1:da<db?1:0;
}

int plan_solution(int *init_held, int init_held_size, double time_limit,
                  int *out_path, int *out_len) {
    _init_timer();
    double t0_time = _get_time_s();

    Mask pile=mzero();
    for (int i=0;i<G.n;i++) mset(&pile,i);
    Hand hand; memset(&hand,0,sizeof(hand));
    for (int t=0;t<=MAX_TYPES;t++) hand.c[t]=init_held[t];
    hand.sz=init_held_size;

    int best[CMAX_PATH], blen=0, trials=0;
    double beam_end=time_limit*0.50, noisy_end=time_limit*0.90, bt_end=time_limit*0.95;

    for (int var=0; var<12 && elapsed_since(t0_time)<beam_end; var++) {
        for (int us=0; us<2 && elapsed_since(t0_time)<beam_end; us++) {
            Mask ip=pile; Hand ih=hand;
            int ipath[CMAX_PATH]; int ipl=0;
            auto_match(&ip,&ih,ipath,&ipl,us);

            if (ipl>blen) { blen=ipl; memcpy(best,ipath,ipl*sizeof(int)); }

            beams_a[0].score=0;
            beams_a[0].pile=ip;
            beams_a[0].hand=ih;
            memcpy(beams_a[0].path, ipath, ipl*sizeof(int));
            beams_a[0].plen=ipl;
            int n_beams=1;

            for (int round=0; round<225 && n_beams>0 && elapsed_since(t0_time)<beam_end; round++) {
                int n_next=0;

                for (int bi=0; bi<n_beams; bi++) {
                    BeamState *bs=&beams_a[bi];
                    Mask avail; get_available(&bs->pile,&avail);
                    if (mis_zero(&avail)) {
                        if (bs->plen>blen) { blen=bs->plen; memcpy(best,bs->path,bs->plen*sizeof(int)); }
                        continue;
                    }

                    int nc=0;
                    for (int ww=0;ww<MW;ww++) {
                        u64 bits=avail.w[ww];
                        while (bits) {
                            int i=ww*64+__builtin_ctzll(bits);
                            Mask np; Hand nh;
                            double sc=score_move(&bs->pile,&bs->hand,i,var,0,&np,&nh);
                            if (sc>-1e17 && nc<MAX_BLOCKS) {
                                cand_buf[nc].sc=sc; cand_buf[nc].idx=i;
                                cand_buf[nc].np=np; cand_buf[nc].nh=nh;
                                nc++;
                            }
                            bits&=bits-1;
                        }
                    }
                    if (!nc) {
                        if (bs->plen>blen) { blen=bs->plen; memcpy(best,bs->path,bs->plen*sizeof(int)); }
                        continue;
                    }

                    qsort(cand_buf,nc,sizeof(Cand),cand_cmp);
                    int take=nc<BRANCH?nc:BRANCH;

                    for (int ci=0; ci<take && n_next<BEAM_W*BRANCH; ci++) {
                        BeamState *ns=&beams_b[n_next];
                        ns->pile=cand_buf[ci].np;
                        ns->hand=cand_buf[ci].nh;
                        int oldpl=bs->plen;
                        memcpy(ns->path, bs->path, oldpl*sizeof(int));
                        ns->plen=oldpl;
                        if (ns->plen<CMAX_PATH) ns->path[ns->plen++]=cand_buf[ci].idx;

                        auto_match(&ns->pile,&ns->hand,ns->path,&ns->plen,us);
                        ns->score=bs->score+cand_buf[ci].sc+ns->plen*500;

                        if (ns->plen>blen) { blen=ns->plen; memcpy(best,ns->path,ns->plen*sizeof(int)); }
                        n_next++;
                    }
                }

                if (!n_next) break;

                qsort(beams_b,n_next,sizeof(BeamState),beam_cmp);

                n_beams=0;
                for (int i=0; i<n_next && n_beams<BEAM_W; i++) {
                    int dup=0;
                    for (int j=0; j<n_beams; j++) {
                        if (meq(&beams_b[i].pile,&beams_a[j].pile) &&
                            memcmp(beams_b[i].hand.c, beams_a[j].hand.c, sizeof(beams_a[j].hand.c))==0) {
                            dup=1; break;
                        }
                    }
                    if (!dup) {
                        beams_a[n_beams]=beams_b[i];
                        n_beams++;
                    }
                }
            }
        }
    }

    int noise_levels[]={0,50,100,150,200,300,500,800,1000,1500,2000,3000,5000,8000,12000};
    int n_noise=15;
    for (int seed=0; elapsed_since(t0_time)<noisy_end; seed++) {
        trials++;
        int ni=seed%n_noise;
        int noise=noise_levels[ni];
        int rs=seed/n_noise;
        xs_seed((unsigned)(rs*137+noise*31+1));
        int use_smart=rs%2==0, variant=rs%20, use_relaxed=rs%4!=0;

        Mask p=pile; Hand h=hand;
        int path[CMAX_PATH]; int plen=0;

        while (plen<CMAX_PATH) {
            auto_match(&p,&h,path,&plen,use_smart);
            Mask avail; get_available(&p,&avail);
            if (mis_zero(&avail)) break;
            double bs2=-1e30; int bi2=-1; Mask bp; Hand bh;
            for (int ww=0;ww<MW;ww++) {
                u64 bits=avail.w[ww];
                while (bits) {
                    int i=ww*64+__builtin_ctzll(bits);
                    Mask np; Hand nh;
                    double sc=score_move(&p,&h,i,variant,use_relaxed,&np,&nh);
                    if (sc<=-1e17) { bits&=bits-1; continue; }
                    if (noise>0) sc+=xs_gauss()*noise;
                    if (sc>bs2) { bs2=sc; bi2=i; bp=np; bh=nh; }
                    bits&=bits-1;
                }
            }
            if (bi2<0) break;
            p=bp; h=bh;
            if (plen<CMAX_PATH) path[plen++]=bi2;
        }
        if (plen>blen) { blen=plen; memcpy(best,path,plen*sizeof(int)); }
    }

    if (blen>0 && blen<150) {
        for (int bt=0; elapsed_since(t0_time)<bt_end; bt++) {
            xs_seed((unsigned)(bt*31337+7));
            int bp2=blen-40; if(bp2<0)bp2=0;
            int rng=blen-5-bp2; if(rng<=0)rng=1;
            int bpt=bp2+(xs_next()%rng);

            Hand h=hand; Mask p=pile; int ok=1;
            for (int s=0;s<bpt&&s<blen;s++) {
                if (!mtest(&p,best[s])) { ok=0; break; }
                apply_pick(&p,&h,best[s]);
            }
            if (!ok) continue;

            int path[CMAX_PATH]; memcpy(path,best,bpt*sizeof(int));
            int pl=bpt;
            while (pl<CMAX_PATH) {
                auto_match(&p,&h,path,&pl,bt%2==0);
                Mask av; get_available(&p,&av);
                if (mis_zero(&av)) break;
                double ts2=-1e30; int ti=-1; Mask tp; Hand th;
                for (int ww=0;ww<MW;ww++) {
                    u64 bits=av.w[ww];
                    while (bits) {
                        int i=ww*64+__builtin_ctzll(bits);
                        Mask np; Hand nh;
                        double sc=score_move(&p,&h,i,bt%20,0,&np,&nh);
                        if (sc<=-1e17) { bits&=bits-1; continue; }
                        sc+=xs_gauss()*3000;
                        if (sc>ts2) { ts2=sc; ti=i; tp=np; th=nh; }
                        bits&=bits-1;
                    }
                }
                if (ti<0) break;
                p=tp; h=th; if(pl<CMAX_PATH) path[pl++]=ti;
            }
            if (pl>blen) { blen=pl; memcpy(best,path,pl*sizeof(int)); }
        }
    }

    if (blen<150) {
        for (int rs=0; elapsed_since(t0_time)<time_limit; rs++) {
            xs_seed((unsigned)(rs*54321+99));
            int path[CMAX_PATH];
            Mask p=pile; Hand h=hand; int plen=0;
            int use_smart=rs%3!=2, variant=rs%20, use_relaxed=rs%4!=0;
            int noise=1000+rs*100;
            while (plen<CMAX_PATH) {
                auto_match(&p,&h,path,&plen,use_smart);
                Mask avail; get_available(&p,&avail);
                if (mis_zero(&avail)) break;
                double bs2=-1e30; int bi2=-1; Mask bp; Hand bh;
                for (int ww=0;ww<MW;ww++) {
                    u64 bits=avail.w[ww];
                    while (bits) {
                        int i=ww*64+__builtin_ctzll(bits);
                        Mask np; Hand nh;
                        double sc=score_move(&p,&h,i,variant,use_relaxed,&np,&nh);
                        if (sc<=-1e17) { bits&=bits-1; continue; }
                        sc+=xs_gauss()*noise;
                        if (sc>bs2) { bs2=sc; bi2=i; bp=np; bh=nh; }
                        bits&=bits-1;
                    }
                }
                if (bi2<0) break;
                p=bp; h=bh;
                if (plen<CMAX_PATH) path[plen++]=bi2;
            }
            if (plen>blen) { blen=plen; memcpy(best,path,plen*sizeof(int)); }
        }
    }

    *out_len=blen;
    memcpy(out_path,best,blen*sizeof(int));
    return trials;
}
