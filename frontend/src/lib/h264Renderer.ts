// WebCodecs 기반 H.264 라이브 렌더러.
//
// scrcpy raw H.264(Annex-B) NAL 스트림을 받아 Access Unit(프레임) 단위로 분해한 뒤
// 브라우저 네이티브 VideoDecoder 로 디코딩한다. JMuxer(MSE)가 자동차 IVI 의 High
// profile SPS 를 파싱 못 해 화면이 멈추던 문제를 우회한다 — VideoDecoder 는 모든
// 프로파일을 네이티브로 디코딩하고 MSE 보다 지연이 낮다.
//
// 입력: feed(bytes) 로 임의 경계의 소켓 청크를 흘려넣으면 내부에서 start code 로
//       NAL 을 잘라 AU(단일 슬라이스/프레임 가정 — scrcpy 기본)로 묶어 decode() 한다.
// 출력: 최신 디코딩 프레임을 1장만 보관(latest). drawTo(canvas) 로 캔버스에 그린다.
//       (RecordPage 의 rAF 가 매 프레임 호출 — 탭/크롭/ROI 캔버스 경로 그대로 재사용)
//
// WebCodecs 타입은 TS lib 버전별 편차가 있어 any 로 다룬다.

type Crop = { x0: number; y0: number; x1: number; y1: number };

const VCL_TYPES = new Set([1, 2, 3, 4, 5]); // slice NAL (5=IDR, 1=non-IDR)

function toHex2(n: number): string {
  return n.toString(16).padStart(2, '0');
}

export function webCodecsSupported(): boolean {
  return typeof window !== 'undefined' && typeof (window as any).VideoDecoder !== 'undefined';
}

export class H264Renderer {
  private decoder: any = null;
  private configured = false;
  private buf = new Uint8Array(0);
  private auParts: Uint8Array[] = []; // 현재 AU 의 NAL 들(각 start code 포함)
  private auKey = false;              // 현재 AU 에 SPS/IDR 포함 여부
  private codecString = '';
  private latest: any = null;         // 최신 VideoFrame (그릴 때까지 보관)
  private frameCount = 0;
  private closed = false;
  private decodeErrored = false;
  private onLog?: (msg: string) => void;
  private onFrame?: () => void; // 실제 디코딩된 프레임마다 호출 (정확한 fps 측정용)

  constructor(onLog?: (msg: string) => void, onFrame?: () => void) {
    this.onLog = onLog;
    this.onFrame = onFrame;
  }

  get hasFrame(): boolean {
    return !!this.latest;
  }

  feed(bytes: Uint8Array): void {
    if (this.closed) return;
    // 누적 버퍼에 이어붙임
    if (this.buf.length === 0) {
      this.buf = bytes.slice();
    } else {
      const merged = new Uint8Array(this.buf.length + bytes.length);
      merged.set(this.buf, 0);
      merged.set(bytes, this.buf.length);
      this.buf = merged;
    }

    // start code 위치 수집 (00 00 01 / 00 00 00 01)
    const positions = this.findStartCodes(this.buf);
    if (positions.length < 2) return; // 완결된 NAL 이 최소 1개 있으려면 start code 2개 필요

    // 마지막 start code 이전까지가 완결 NAL. 마지막은 다음 feed 까지 보류.
    for (let k = 0; k < positions.length - 1; k++) {
      const nal = this.buf.subarray(positions[k], positions[k + 1]);
      this.handleNAL(nal);
    }
    this.buf = this.buf.slice(positions[positions.length - 1]);
  }

  private findStartCodes(b: Uint8Array): number[] {
    const out: number[] = [];
    const n = b.length;
    let i = 0;
    while (i + 2 < n) {
      if (b[i] === 0 && b[i + 1] === 0 && b[i + 2] === 1) {
        out.push(i);
        i += 3;
      } else {
        i++;
      }
    }
    return out;
  }

  private handleNAL(nal: Uint8Array): void {
    // start code 길이 판정 (00 00 01 = 3, 00 00 00 01 = 4)
    const scLen = nal[2] === 1 ? 3 : 4;
    if (nal.length <= scLen) return;
    const nalType = nal[scLen] & 0x1f;

    if (nalType === 7) {
      // SPS — 코덱 문자열 추출 후 디코더 1회 구성
      this.maybeConfigure(nal.subarray(scLen));
      this.auKey = true;
    } else if (nalType === 5) {
      this.auKey = true;
    }

    this.auParts.push(nal);

    if (VCL_TYPES.has(nalType)) {
      // scrcpy 는 프레임당 단일 슬라이스 → VCL NAL 이 곧 프레임 종료
      this.emitAU();
    }
  }

  private maybeConfigure(sps: Uint8Array): void {
    if (this.configured || this.closed) return;
    // sps[0]=NAL header(0x67), sps[1]=profile_idc, sps[2]=constraint flags, sps[3]=level_idc
    if (sps.length < 4) return;
    const profile = sps[1];
    const constraint = sps[2];
    const level = sps[3];
    this.codecString = `avc1.${toHex2(profile)}${toHex2(constraint)}${toHex2(level)}`;

    try {
      const VideoDecoderCtor = (window as any).VideoDecoder;
      this.decoder = new VideoDecoderCtor({
        output: (frame: any) => {
          if (this.closed) {
            try { frame.close(); } catch { /* ignore */ }
            return;
          }
          // 직전 미사용 프레임은 닫아 디코더 버퍼 누수 방지 (최신 1장만 보관)
          if (this.latest) {
            try { this.latest.close(); } catch { /* ignore */ }
          }
          this.latest = frame;
          // 디코더가 실제로 프레임을 출력한 시점 = 1 프레임 (WS 청크 수가 아닌 실프레임 fps)
          try { this.onFrame?.(); } catch { /* ignore */ }
        },
        error: (e: any) => {
          this.decodeErrored = true;
          this.onLog?.(`VideoDecoder error: ${e?.message || e}`);
        },
      });
      // Annex-B 입력: description 생략. 저지연 우선.
      this.decoder.configure({ codec: this.codecString, optimizeForLatency: true });
      this.configured = true;
      this.onLog?.(`VideoDecoder configured codec=${this.codecString}`);
    } catch (e: any) {
      this.decodeErrored = true;
      this.onLog?.(`VideoDecoder configure failed: ${e?.message || e}`);
    }
  }

  private emitAU(): void {
    const parts = this.auParts;
    this.auParts = [];
    const isKey = this.auKey;
    this.auKey = false;

    if (!this.configured || this.decodeErrored || !this.decoder) return;
    if (this.decoder.state !== 'configured') return;
    // 디코더 구성 후 첫 청크는 반드시 key 여야 함 — 첫 프레임 이전의 delta 는 버린다.
    if (this.frameCount === 0 && !isKey) return;

    // AU 바이트 결합 (start code 포함 Annex-B)
    let total = 0;
    for (const p of parts) total += p.length;
    const data = new Uint8Array(total);
    let off = 0;
    for (const p of parts) { data.set(p, off); off += p.length; }

    try {
      const ChunkCtor = (window as any).EncodedVideoChunk;
      const chunk = new ChunkCtor({
        type: isKey ? 'key' : 'delta',
        timestamp: Math.round(this.frameCount * (1_000_000 / 60)),
        data,
      });
      this.decoder.decode(chunk);
      this.frameCount++;
    } catch (e: any) {
      this.decodeErrored = true;
      this.onLog?.(`decode() threw: ${e?.message || e}`);
    }
  }

  /** 최신 디코딩 프레임을 캔버스에 그린다. crop 지정 시 해당 영역만. */
  drawTo(canvas: HTMLCanvasElement | null, crop?: Crop): void {
    const f = this.latest;
    if (!f || !canvas) return;
    const W = f.displayWidth || f.codedWidth || 0;
    const H = f.displayHeight || f.codedHeight || 0;
    if (W === 0 || H === 0) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    if (crop) {
      const sx = Math.round(crop.x0 * W);
      const sy = Math.round(crop.y0 * H);
      const sw = Math.max(1, Math.round((crop.x1 - crop.x0) * W));
      const sh = Math.max(1, Math.round((crop.y1 - crop.y0) * H));
      if (canvas.width !== sw) canvas.width = sw;
      if (canvas.height !== sh) canvas.height = sh;
      ctx.drawImage(f as any, sx, sy, sw, sh, 0, 0, sw, sh);
    } else {
      if (canvas.width !== W) canvas.width = W;
      if (canvas.height !== H) canvas.height = H;
      ctx.drawImage(f as any, 0, 0);
    }
  }

  close(): void {
    this.closed = true;
    if (this.latest) {
      try { this.latest.close(); } catch { /* ignore */ }
      this.latest = null;
    }
    if (this.decoder) {
      try { if (this.decoder.state !== 'closed') this.decoder.close(); } catch { /* ignore */ }
      this.decoder = null;
    }
    this.buf = new Uint8Array(0);
    this.auParts = [];
  }
}
