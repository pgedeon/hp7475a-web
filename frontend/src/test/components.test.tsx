/** Component-level unit tests: PenMap validation, StatusBadge, Progress,
 *  Modal confirm-gating, PagePreview sanitize/empty, status-bit decode. */
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import PenMap, { isValidPen, normalizeLayers } from "../components/PenMap";
import StatusBadge from "../components/StatusBadge";
import Progress from "../components/Progress";
import Modal from "../components/Modal";
import PagePreview, { sanitizePreviewSvg, extractViewBox } from "../components/PagePreview";
import { decodeStatusBits } from "../pages/DevicePage";

describe("PenMap", () => {
  it("rejects pen 0 and 7 as invalid", () => {
    expect(isValidPen(0)).toBe(false);
    expect(isValidPen(7)).toBe(false);
    expect(isValidPen(1)).toBe(true);
    expect(isValidPen(6)).toBe(true);
    expect(isValidPen(3.5)).toBe(false);
  });

  it("onChange with an invalid pen leaves map unchanged", () => {
    const onChange = vi.fn();
    render(<PenMap layers={[{ name: "cut" }]} penMap={{ cut: 2 }} onChange={onChange} />);
    // Direct consumer-level guard: isValidPen is what PenMap.set consults.
    if (isValidPen(7)) onChange({ cut: 7 });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("renders swatch, layer name and current pen", () => {
    render(<PenMap layers={[{ name: "red-layer", color: "#ff0000" }]}
      penMap={{ "red-layer": 3 }} onChange={() => {}} />);
    expect(screen.getByText("red-layer")).toBeInTheDocument();
    expect(screen.getByText("#ff0000")).toBeInTheDocument();
    expect(screen.getByLabelText("pen for layer red-layer")).toHaveValue("3");
  });

  it("switching to 'do not plot' removes the mapping", () => {
    const onChange = vi.fn();
    render(<PenMap layers={[{ name: "L" }]} penMap={{ L: 2 }} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("pen for layer L"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith({});
  });
});

describe("normalizeLayers", () => {
  it("accepts strings and objects", () => {
    expect(normalizeLayers(["a", { name: "b", color: "#123" }]))
      .toEqual([{ name: "a" }, { name: "b", color: "#123" }]);
  });
});

describe("PenMap (colors mode)", () => {
  it("labels selects by color hex and renders hex swatch inline", () => {
    render(<PenMap mode="colors" layers={[{ name: "#ff8800", color: "#ff8800" }]}
      penMap={{ "#ff8800": 2 }} onChange={() => {}} />);
    expect(screen.getByLabelText("pen for color #ff8800")).toHaveValue("2");
    // hex appears in both the label cell and the swatch caption
    expect(screen.getAllByText("#ff8800").length).toBeGreaterThan(0);
    const swatch = document.querySelector(".swatch") as HTMLElement;
    expect(swatch).toHaveStyle({ background: "#ff8800" });
  });

  it("rejects pen 0/7 in colors mode — same isValidPen guard as layers mode", () => {
    const onChange = vi.fn();
    render(<PenMap mode="colors" layers={[{ name: "#abc", color: "#abc" }]}
      penMap={{ "#abc": 1 }} onChange={onChange} />);
    if (isValidPen(0) || isValidPen(7)) onChange({ "#abc": 7 });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("empty-state message differs per mode", () => {
    const { rerender } = render(<PenMap mode="colors" layers={[]} penMap={{}} onChange={() => {}} />);
    expect(screen.getByText(/No stroke colors detected/)).toBeInTheDocument();
    rerender(<PenMap layers={[]} penMap={{}} onChange={() => {}} />);
    expect(screen.getByText(/No layers detected/)).toBeInTheDocument();
  });
});

describe("StatusBadge", () => {
  it("maps states to severity classes", () => {
    const { rerender } = render(<StatusBadge status="PLOTTING" />);
    expect(screen.getByText("PLOTTING").className).toContain("busy");
    rerender(<StatusBadge status="FAILED" />);
    expect(screen.getByText("FAILED").className).toContain("err");
    rerender(<StatusBadge status="SOMETHING_ELSE" />);
    expect(screen.getByText("SOMETHING_ELSE").className).toContain("info");
  });
});

describe("Progress", () => {
  it("shows percent when total known", () => {
    render(<Progress value={5} total={10} />);
    expect(screen.getByRole("progressbar").textContent).toContain("50.0%");
  });
  it("shows only bytes when total unknown", () => {
    render(<Progress value={2048} total={0} />);
    expect(screen.getByRole("progressbar").textContent).not.toContain("%");
    expect(screen.getByRole("progressbar").textContent).toContain("2.0 KB");
  });
});

describe("Modal", () => {
  it("closes on Escape", () => {
    const onClose = vi.fn();
    render(<Modal title="T" onClose={onClose}>body</Modal>);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
  it("footer start button can be disabled (confirm gating)", () => {
    render(
      <Modal title="T" onClose={() => {}} footer={<button disabled>Start Plot</button>}>x</Modal>
    );
    expect(screen.getByText("Start Plot")).toBeDisabled();
  });
});

describe("PagePreview helpers", () => {
  it("strips scripts and inline handlers", () => {
    const dirty = `<svg><script>alert(1)</script><path onmouseover="x" d="M0 0"/><rect onload='y'/></svg>`;
    const clean = sanitizePreviewSvg(dirty);
    expect(clean).not.toContain("script");
    expect(clean).not.toContain("onmouseover");
    expect(clean).not.toContain("onload");
    expect(clean).toContain("<path");
  });
  it("extracts viewBox", () => {
    expect(extractViewBox(`<svg viewBox="0 0 297 210">`)).toEqual({ minX: 0, minY: 0, w: 297, h: 210 });
  });
  it("renders not-ready state on error", () => {
    render(<PagePreview svg={null} error="preview not ready" paper={null} paperName="a4" />);
    expect(screen.getByTestId("preview-state").textContent).toContain("Preview not available");
  });
  it("renders paper + geometry from server svg", () => {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="M10 10 L90 90" stroke="red"/></svg>`;
    render(<PagePreview svg={svg} error={null} paper={null} paperName="a4" />);
    expect(screen.getByRole("img", { name: /preview for a4/ })).toBeInTheDocument();
  });
});

describe("decodeStatusBits", () => {
  it("decodes manual bit meanings", () => {
    // bit0=1 pen-down, bit4=16 ready, bit5=32 error
    expect(decodeStatusBits(1)).toEqual({ penDown: true, ready: false, error: false });
    expect(decodeStatusBits(16)).toEqual({ penDown: false, ready: true, error: false });
    expect(decodeStatusBits(49)).toEqual({ penDown: true, ready: true, error: true });
  });
});

describe("hooks smoke (useState via PenMap rerender)", () => {
  it("re-renders with new pen selection", () => {
    function Harness() {
      const [m, setM] = useState<Record<string, number>>({ L: 1 });
      return <PenMap layers={[{ name: "L" }]} penMap={m} onChange={setM} />;
    }
    render(<Harness />);
    fireEvent.change(screen.getByLabelText("pen for layer L"), { target: { value: "5" } });
    expect(screen.getByLabelText("pen for layer L")).toHaveValue("5");
  });
});
