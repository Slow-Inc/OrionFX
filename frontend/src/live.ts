// The `/ws/live` feed (backend/app/api/app.py `live`). First message: {token, since}.
// The server streams {seq, event} for every seq > since. On a drop we reconnect with
// backoff and resume from the last seq we saw, so no event is missed; the app then
// refreshes its snapshots. Close code 4401 means the session ended: no reconnect.
import type { LiveMessage } from "./types";

export type FeedState = "CONNECTING" | "LIVE" | "DISCONNECTED";

export interface SocketLike {
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: ((e: { code: number }) => void) | null;
  send(data: string): void;
  close(): void;
}

export interface FeedOptions {
  url: string;
  token: () => string | null;
  socket?: (url: string) => SocketLike;
  onEvent: (seq: number, event: LiveMessage["event"]) => void;
  onState: (state: FeedState) => void;
  onUnauthorized: () => void;
  since?: number;
}

const AUTH_CLOSE = 4401;
const MAX_BACKOFF_MS = 15_000;

export class LiveFeed {
  lastSeq: number;
  private ws: SocketLike | null = null;
  private stopped = false;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly opts: FeedOptions) {
    this.lastSeq = opts.since ?? 0;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    const ws = this.ws;
    this.ws = null;
    ws?.close();
  }

  private connect(): void {
    const token = this.opts.token();
    if (!token) {
      this.opts.onUnauthorized();
      return;
    }
    this.opts.onState("CONNECTING");
    const make = this.opts.socket ?? ((u: string) => new WebSocket(u) as unknown as SocketLike);
    const ws = make(this.opts.url);
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      ws.send(JSON.stringify({ token, since: this.lastSeq }));
      this.opts.onState("LIVE");
    };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data) as LiveMessage;
      if (msg.seq <= this.lastSeq) return; // already seen (a resync overlap)
      this.lastSeq = msg.seq;
      this.opts.onEvent(msg.seq, msg.event);
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return; // an old socket we already replaced or stopped
      this.ws = null;
      this.opts.onState("DISCONNECTED");
      if (e.code === AUTH_CLOSE) {
        this.opts.onUnauthorized();
        return;
      }
      if (this.stopped) return;
      const delay = Math.min(1_000 * 2 ** this.attempt, MAX_BACKOFF_MS);
      this.attempt += 1;
      this.timer = setTimeout(() => this.connect(), delay);
    };
  }
}
