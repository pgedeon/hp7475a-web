/** Minimal fake WebSocket for jsdom (no native impl). */
export class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  close() { this.onclose?.(); }
  send(data: string) { this.sent.push(data); }
  /** Test helper: deliver a server frame. */
  static emit(data: unknown) {
    for (const ws of FakeWebSocket.instances) {
      ws.onmessage?.({ data: JSON.stringify(data) });
    }
  }
  static open() {
    for (const ws of FakeWebSocket.instances) ws.onopen?.();
  }
  static reset() { FakeWebSocket.instances = []; }
}
