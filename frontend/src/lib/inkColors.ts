/** Detect the number of dominant ink colors in a raster image (client-side).

 * Downsamples via canvas, drops near-white background pixels, bins the rest
 * into a coarse RGB histogram, merges similar bins, and keeps bins holding a
 * meaningful share of ink pixels. Result clamps to 1..max — matches the
 * vectorize `colors` parameter (goal a7f70dae follow-up).
 */

const SAMPLE_WIDTH = 220;
const BITS = 5; // 32 levels per channel -> 32768 bins
const MERGE_DIST = 64; // max RGB L2 distance to merge neighbors
const MIN_SHARE = 0.04; // bin must hold >=4% of ink pixels

interface Ctx {
  drawImage(img: CanvasImageSource, dx: number, dy: number, dw?: number, dh?: number): void;
  getImageData(x: number, y: number, w: number, h: number): { data: Uint8ClampedArray };
}

function loadBitmap(file: File): Promise<{ w: number; h: number; draw: (ctx: Ctx, w: number, h: number) => void; close: () => void }> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      resolve({
        w: img.naturalWidth,
        h: img.naturalHeight,
        draw: (ctx, dw, dh) => ctx.drawImage(img, 0, 0, dw, dh),
        close: () => {
          URL.revokeObjectURL(url);
          img.src = "";
        },
      });
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("image decode failed"));
    };
    img.src = url;
  });
}

function isInk(r: number, g: number, b: number): boolean {
  // Near-white = background. Also drop fully transparent (a handled by caller).
  const min = Math.min(r, g, b);
  const max = Math.max(r, g, b);
  return !(min > 200 && max - min < 24);
}

export async function detectInkColors(file: File, max = 8): Promise<number> {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: true }) as Ctx | null;
  if (!ctx) throw new Error("canvas unavailable");
  const bmp = await loadBitmap(file);
  try {
    const scale = Math.min(1, SAMPLE_WIDTH / Math.max(1, bmp.w));
    const w = Math.max(1, Math.round(bmp.w * scale));
    const h = Math.max(1, Math.round(bmp.h * scale));
    canvas.width = w;
    canvas.height = h;
    bmp.draw(ctx, w, h);
    const { data } = ctx.getImageData(0, 0, w, h);

    // Histogram of quantized colors over ink pixels.
    const counts = new Map<number, { n: number; r: number; g: number; b: number }>();
    let inkTotal = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] < 128) continue;
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      if (!isInk(r, g, b)) continue;
      inkTotal++;
      const key = ((r >> BITS) << (2 * BITS)) | ((g >> BITS) << BITS) | (b >> BITS);
      const e = counts.get(key);
      if (e) {
        e.n++;
        e.r += r;
        e.g += g;
        e.b += b;
      } else {
        counts.set(key, { n: 1, r, g, b });
      }
    }
    if (inkTotal === 0) return 1;

    // Centroids sorted by weight.
    const cents = [...counts.values()]
      .sort((a, b) => b.n - a.n)
      .map((e) => ({ n: e.n, r: e.r / e.n, g: e.g / e.n, b: e.b / e.n }));

    // Greedy merge of similar centroids (weighted average).
    const merged: typeof cents = [];
    for (const c of cents) {
      const near = merged.find(
        (m) => (m.r - c.r) ** 2 + (m.g - c.g) ** 2 + (m.b - c.b) ** 2 < MERGE_DIST * MERGE_DIST,
      );
      if (near) {
        const tot = near.n + c.n;
        near.r = (near.r * near.n + c.r * c.n) / tot;
        near.g = (near.g * near.n + c.g * c.n) / tot;
        near.b = (near.b * near.n + c.b * c.n) / tot;
        near.n = tot;
      } else {
        merged.push({ ...c });
      }
    }

    // Absorb anti-aliasing tints: a centroid lying on the white→color line
    // is that color's edge halo, not a new ink color.
    merged.sort((a, b) => b.n - a.n);
    const kept: typeof merged = [];
    for (const m of merged) {
      const host = kept.find((c) => {
        const dr = c.r - 255;
        const dg = c.g - 255;
        const db = c.b - 255;
        if (Math.abs(dr) < 8 && Math.abs(dg) < 8 && Math.abs(db) < 8) return false;
        const ts = [(m.r - 255) / dr, (m.g - 255) / dg, (m.b - 255) / db];
        return (
          ts.every((t) => t > 0.02 && t < 0.98) &&
          Math.max(...ts) - Math.min(...ts) < 0.18
        );
      });
      if (host) host.n += m.n;
      else kept.push({ ...m });
    }

    const keptFiltered = kept.filter((m) => m.n / inkTotal >= MIN_SHARE);
    return Math.min(max, Math.max(1, keptFiltered.length));
  } finally {
    bmp.close();
  }
}
