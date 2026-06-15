#!/bin/sh
# Dial 화면 전체 서피스 구조 + 0x10의 각 후보 영역 검정비율 (맵 선택/게이트 확정용).
# 사용법(Dial 화면에서): cmd /c "ssh root@192.168.1.4 sh -s < <경로>\mib_dial_full.sh"
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
def is_black(p):
    d=open(p,"rb").read(); off=struct.unpack("<I",d[10:14])[0]; step=max(3,((len(d)-off)//4096//3 or 1)*3); i=off
    while i+3<=len(d):
        if d[i]>16 or d[i+1]>16 or d[i+2]>16: return False
        i+=step
    return True

sc=lmc(["get","screen","0"]); LW=LH=0; order=[]
for lid in f_ids(sc,"layer render order:"):
    lt=lmc(["get","layer",lid]); o=f_ids(lt,"surface render order:")
    if o: LW,LH=f_xy(lt,"original size:") or (0,0); order=o; break
print("LW=%d LH=%d render_order=%s" % (LW,LH,order))

# 각 surface: dest, black?
info={}
for sid in order:
    st=lmc(["get","surface",sid])
    vis = 0 if re.search(r"visibility:\s*0",st) else 1
    dst=f_reg(st,"destination region:"); osz=f_xy(st,"original size:")
    p="/tmp/_s%s.bmp"%sid; blk="?"
    if dump(sid,p): blk = is_black(p)
    info[sid]=(dst,osz,vis,blk)
    print("  sid=%s vis=%s size=%s dest=%s black=%s" % (sid, vis, osz, dst, blk))

# 0x10(HMI=full surface) 덤프 후, 각 비풀스크린 surface의 dest 영역에서 0x10 검정비율
hmi=None
for sid,(dst,osz,vis,blk) in info.items():
    if dst and dst[0]==0 and dst[1]==0 and dst[2]>=LW*0.95 and dst[3]>=LH*0.95: hmi=sid
print("HMI surface =", hmi)
if hmi and dump(hmi,"/tmp/_hmi.bmp"):
    d=open("/tmp/_hmi.bmp","rb").read()
    off=struct.unpack("<I",d[10:14])[0]; w=struct.unpack("<i",d[18:22])[0]; h=struct.unpack("<i",d[22:26])[0]
    stride=((w*3)+3)&~3
    def frac(rect,T):
        mx,my,mw,mh=rect; gx=max(1,mw//150); gy=max(1,mh//150); cnt=tot=0
        for yy in range(my,my+mh,gy):
            if yy>=h or yy<0: continue
            rb=off+(h-1-yy)*stride
            for xx in range(mx,mx+mw,gx):
                if xx>=w or xx<0: continue
                o=rb+xx*3
                if o+3>len(d): continue
                tot+=1
                if max(d[o],d[o+1],d[o+2])<=T: cnt+=1
        return 100.0*cnt/max(1,tot)
    print("\n0x10(HMI) 검정비율 — 각 비풀스크린 surface dest 영역에서:")
    for sid,(dst,osz,vis,blk) in info.items():
        if not dst: continue
        if dst[0]==0 and dst[1]==0 and dst[2]>=LW*0.95 and dst[3]>=LH*0.95: continue  # HMI 자신 skip
        print("  [sid=%s dest=%s]  <=2:%.0f%%  <=8:%.0f%%  <=16:%.0f%%" %
              (sid, dst, frac(dst,2), frac(dst,8), frac(dst,16)))
PY
echo DONE
