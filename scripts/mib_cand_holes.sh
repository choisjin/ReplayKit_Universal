#!/bin/sh
# 현재 화면에서 각 후보 서피스별 0x10(HMI) 구멍(검정) 비율. 진짜 맵 후보와 그 값 확인용.
# 사용법(15" 홈 화면에서): cmd /c "ssh root@<IP> sh -s < <경로>\mib_cand_holes.sh"
export XDG_RUNTIME_DIR=/run/platform/weston
python3 - <<'PY'
import subprocess, re, os, time, struct
os.environ["XDG_RUNTIME_DIR"]="/run/platform/weston"
def lmc(a): return subprocess.run(["LayerManagerControl"]+a,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout.decode("utf-8","replace")
def f_xy(t,k):
    m=re.search(k+r"\D*x=(-?\d+),\s*y=(-?\d+)",t); return (int(m.group(1)),int(m.group(2))) if m else None
def f_reg(t,k):
    m=re.search(k+r"\D*x=(-?\d+),\s*y=(-?\d+),\s*w=(\d+),\s*h=(\d+)",t); return tuple(int(m.group(i)) for i in range(1,5)) if m else None
def f_ids(t,k):
    m=re.search(k+r"\s*(.*)",t); return re.findall(r"(\d+)\(0x[0-9a-fA-F]+\)",m.group(1)) if m else []
def dump(sid,p,timeout=2.5):
    try: os.remove(p)
    except OSError: pass
    subprocess.run(["LayerManagerControl","dump","surface",sid,"to",p],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    dec=None;t0=time.time()
    while time.time()-t0<timeout:
        try: sz=os.path.getsize(p)
        except OSError: sz=0
        if sz>=6 and dec is None:
            with open(p,"rb") as f: f.seek(2);dec=struct.unpack("<I",f.read(4))[0]
        if dec and sz>=dec: return True
        time.sleep(0.003)
    return os.path.exists(p)
def parse(p):
    d=open(p,"rb").read(); off=struct.unpack("<I",d[10:14])[0]; w=struct.unpack("<i",d[18:22])[0]
    h=struct.unpack("<i",d[22:26])[0]; px=struct.unpack("<H",d[28:30])[0]//8
    return d,w,abs(h),off,((w*px+3)&~3),px
def black(p):
    d,w,h,off,stride,px=parse(p); end=off+stride*h; step=max(px,((end-off)//4096//px or 1)*px); i=off
    while i+3<=len(d):
        if d[i]>16 or d[i+1]>16 or d[i+2]>16: return False
        i+=step
    return True
def hole(hp,dst,T):
    d,w,h,off,stride,px=hp; x0,y0,rw,rh=dst; gx=max(1,rw//120); gy=max(1,rh//120); c=t=0; yy=y0
    while yy<y0+rh:
        if 0<=yy<h:
            rb=off+(h-1-yy)*stride; xx=x0
            while xx<x0+rw:
                if 0<=xx<w:
                    o=rb+xx*px
                    if o+3<=len(d):
                        t+=1
                        if max(d[o],d[o+1],d[o+2])<=T: c+=1
                xx+=gx
        yy+=gy
    return 100.0*c/max(1,t)

sc=lmc(["get","screen","0"]); LW=LH=0; order=[]
for lid in f_ids(sc,"layer render order:"):
    lt=lmc(["get","layer",lid]); o=f_ids(lt,"surface render order:")
    if o: LW,LH=f_xy(lt,"original size:") or (0,0); order=o; break
print("LW=%d LH=%d"%(LW,LH))
# HMI(full) 식별 + 덤프
hmi=None; cands=[]
for sid in order:
    st=lmc(["get","surface",sid])
    if re.search(r"visibility:\s*0",st): continue
    dst=f_reg(st,"destination region:"); osz=f_xy(st,"original size:")
    if not dst: continue
    if osz and osz[0]*osz[1]<64*64: continue
    if dst[0]==0 and dst[1]==0 and dst[2]>=LW*0.95 and dst[3]>=LH*0.95: hmi=sid
    else: cands.append((sid,dst))
print("HMI=%s"%hmi)
if not hmi or not dump(hmi,"/tmp/_h.bmp"): print("no HMI"); raise SystemExit
hp=parse("/tmp/_h.bmp")
print("후보별: sid  dest  black?  0x10구멍%(<=0/<=2/<=8)")
for sid,dst in cands:
    blk = (not dump(sid,"/tmp/_c.bmp")) or black("/tmp/_c.bmp")
    print("  %-5s %-22s black=%-5s  %.0f / %.0f / %.0f" % (sid,str(dst),blk,hole(hp,dst,0),hole(hp,dst,2),hole(hp,dst,8)))
PY
echo DONE
