# ruff: noqa
from statistics import NormalDist
import math
N=NormalDist()
print("== break-even accuracy on non-FLAT, a = 0.5 + c/(2(1-f)ATR) ==")
for c,lab in [(0.25,"spread only"),(0.37,"spread+comm $7/lot RT+slip 0.05"),(0.57,"stressed 0.40+0.07+0.10")]:
    for f in [0.0,0.3]:
        row=[]
        for atr in [3,4,5,6,8]:
            row.append(f"ATR{atr}:{0.5+c/(2*(1-f)*atr):.3f}")
        print(f"c={c} ({lab}) FLAT={f}: "+"  ".join(row))
print()
print("== accuracy needed for PF target (no FLAT), c'=c/ATR ==")
for cp in [0.05,0.08,0.10,0.15]:
    out=[]
    for pf in [1.0,1.10,1.15,1.30]:
        # a(1-c') = pf(1-a)(1+c')
        a = pf*(1+cp)/((1-cp)+pf*(1+cp))
        mu=2*a-1-cp; sd=math.sqrt(1-(2*a-1)**2)
        out.append(f"PF{pf}: a={a:.3f} SRtrade={mu/sd:.3f}")
    print(f"c'={cp}: "+" | ".join(out))
print()
# DSR expected max SR factor for N trials
g=0.5772156649
for n in [1,5,10,20,50]:
    if n==1: f=0
    else: f=(1-g)*N.inv_cdf(1-1/n)+g*N.inv_cdf(1-1/(n*math.e))
    print(f"N={n}: E[max] factor = {f:.3f}; trades needed for DSR>=0.95 at SRtrade s: T=((f+1.645)/s)^2 ->",
          {s: math.ceil(((f+1.645)/s)**2) for s in [0.03,0.05,0.07,0.10,0.15,0.25]})
print()
# MinBTL years for annualized in-sample SR of 1 with N=20 (Bailey et al)
f20=(1-g)*N.inv_cdf(1-1/20)+g*N.inv_cdf(1-1/(20*math.e))
for sr in [0.5,1.0,1.5,2.0]:
    print(f"MinBTL N=20, E[maxSR]={sr}: {(f20/sr)**2:.2f} years")
print()
# chance a no-skill config passes PF>=1.15 with 200 trades
for cp in [0.05,0.08,0.10]:
  for T in [200,500,1000,2500]:
    pf=1.15
    a = pf*(1+cp)/((1-cp)+pf*(1+cp)); mu_req=2*a-1-cp
    # null: no skill, mean=-cp, sd=1
    z=(mu_req-(-cp))*math.sqrt(T)
    p1=1-N.cdf(z)
    p20=1-(1-p1)**20
    print(f"c'={cp} T={T}: P(single no-skill config shows PF>=1.15)={p1:.4f}; any of 20 indep={p20:.3f}")
print()
# days needed for block-bootstrap CI lower bound to clear majority rate
print("== independent days needed: acc a, majority m, margin d, trades/day k, intra-day corr rho ==")
for a,m,d in [(0.53,0.50,0.01),(0.55,0.51,0.01),(0.56,0.50,0.02),(0.57,0.51,0.02)]:
    for z,lab in [(1.96,"95% 2-sided"),(N.inv_cdf(1-0.05/(2*20)),"Bonferroni 20")]:
        gap=a-m-d
        neff=(z*0.5/gap)**2
        for k,rho in [(4,0.1),(4,0.3)]:
            deff=1+(k-1)*rho
            days=neff*deff/k
            print(f"a={a} m={m} d={d} {lab}(z={z:.2f}): n_eff={neff:.0f}, k={k},rho={rho}: trades={neff*deff:.0f}, days={days:.0f} (~{days/250:.1f} yrs)")
print()
# fold sign test for 19 folds
from math import comb
for n in [10,15,19]:
    for kk in range(n//2,n+1):
        p=sum(comb(n,i) for i in range(kk,n+1))/2**n
        if p<0.05:
            print(f"folds={n}: need >= {kk} positive folds (p={p:.3f})"); break
# annualization of per-trade SR
for s in [0.03,0.05,0.07]:
    for tpd in [2,4]:
        print(f"SRtrade={s} trades/day={tpd}: annual SR ~ {s*math.sqrt(tpd*252):.2f}")
