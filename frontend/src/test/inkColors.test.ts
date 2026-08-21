/** inkColors.detectInkColors: mocked-canvas unit tests. */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { detectInkColors } from "../lib/inkColors";

type Pixel = [number, number, number, number]; // r g b a

function fakeCanvas(pixels: Pixel[], w: number, h: number) {
  const data = new Uint8ClampedArray(w * h * 4);
  pixels.forEach((p, i) => {
    data[i * 4] = p[0];
    data[i * 4 + 1] = p[1];
    data[i * 4 + 2] = p[2];
    data[i * 4 + 3] = p[3];
  });
  const ctx = {
    drawImage: vi.fn(),
    getImageData: vi.fn(() => ({ data })),
  };
  vi.spyOn(document, "createElement").mockReturnValue({
    getContext: vi.fn(() => ctx),
    width: 0,
    height: 0,
  } as unknown as HTMLCanvasElement);
  return ctx;
}

function file(name = "img.png"): File {
  return new File(["x"], name, { type: "image/png" });
}

// jsdom Image: src set → never fires onload. Stub loadBitmap path by making
// Image fire onload synchronously with natural size.
class FakeImage {
  static last: FakeImage | null = null;
  naturalWidth = 400;
  naturalHeight = 300;
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  #_src = "";
  get src(): string {
    return this.#_src;
  }
  set src(v: string) {
    this.#_src = v;
    queueMicrotask(() => this.onload?.());
  }
  constructor() {
    FakeImage.last = this;
  }
}

vi.stubGlobal("Image", FakeImage);
beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:mock", revokeObjectURL: () => {} }));
});

function solid(n: number, rgb: [number, number, number], extraWhite = 0): Pixel[] {
  const px: Pixel[] = [];
  for (let i = 0; i < n; i++) px.push([rgb[0], rgb[1], rgb[2], 255]);
  for (let i = 0; i < extraWhite; i++) px.push([255, 255, 255, 255]);
  return px;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("detectInkColors", () => {
  it("counts 3 well-separated colors", async () => {
    const px = [
      ...solid(50, [200, 30, 30]),
      ...solid(50, [30, 60, 200]),
      ...solid(50, [20, 160, 60]),
      ...solid(200, [255, 255, 255]),
    ];
    fakeCanvas(px, 10, 35);
    await expect(detectInkColors(file())).resolves.toBe(3);
  });

  it("merges similar shades into one color", async () => {
    const px = [
      ...solid(40, [180, 40, 40]),
      ...solid(40, [190, 55, 45]), // same red family
      ...solid(200, [255, 255, 255]),
    ];
    fakeCanvas(px, 10, 28);
    await expect(detectInkColors(file())).resolves.toBe(1);
  });

  it("all-white image returns 1", async () => {
    fakeCanvas(solid(100, [255, 255, 255]), 10, 10);
    await expect(detectInkColors(file())).resolves.toBe(1);
  });

  it("clamps to max 8", async () => {
    const px: Pixel[] = [];
    for (let k = 0; k < 12; k++) {
      px.push(...solid(20, [(k * 37) % 256, (k * 71) % 256, (k * 113) % 256]));
    }
    fakeCanvas(px, 12, 20);
    await expect(detectInkColors(file(), 8)).resolves.toBeLessThanOrEqual(8);
  });

  it("drops tiny noise bins below share threshold", async () => {
    const px = [
      ...solid(95, [10, 10, 10]),
      ...solid(2, [220, 30, 200]), // 2% — below MIN_SHARE
      ...solid(200, [255, 255, 255]),
    ];
    fakeCanvas(px, 10, 30);
    await expect(detectInkColors(file())).resolves.toBe(1);
  });

  it("absorbs anti-aliasing tints toward white", async () => {
    // red + blue inks, each with a pale halo on the white→color line
    const px = [
      ...solid(40, [200, 30, 30]),
      ...solid(15, [230, 150, 150]), // red halo (t≈0.35)
      ...solid(40, [30, 60, 200]),
      ...solid(15, [140, 160, 230]), // blue halo (t≈0.55)
      ...solid(200, [255, 255, 255]),
    ];
    fakeCanvas(px, 12, 26);
    await expect(detectInkColors(file())).resolves.toBe(2);
  });
});
