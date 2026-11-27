// The `/ws/live` feed (backend/app/api/app.py `live`). First message: {token, since}.
// The server streams {seq, event} for every seq > since. The server accepts every
// socket and only then checks the hello, so the feed reports LIVE only once the hello
// was clearly accepted: a first message arrived, or the socket stayed open past a short
// grace period. On a drop we reconnect with backoff and resume from the last seq, so no
// event is missed. 4401 (session ended) and 4400 (bad hello) are final: no retry.
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
  token: string;
  socket?: (url: string) => SocketLike;
  onEvent: (seq: number, event: LiveMessage["event"]) => void;
  onState: (state: FeedState) => void;
  onUnauthorized: () => void;
  since?: number;
}

const AUTH_CLOSE = 4401;
const BAD_HELLO_CLOSE = 4400;
const MAX_BACKOFF_MS = 15_000;
export const ACCEPT_GRACE_MS = 1_500;

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

  private accepted(ws: SocketLike): void {
    if (this.ws !== ws) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.attempt = 0;
    this.opts.onState("LIVE");
  }

  private connect(): void {
    this.opts.onState("CONNECTING");
    const make = this.opts.socket ?? ((u: string) => new WebSocket(u) as unknown as SocketLike);
    const ws = make(this.opts.url);
    let live = false;
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ token: this.opts.token, since: this.lastSeq }));
      this.timer = setTimeout(() => {
        live = true;
        this.accepted(ws);
      }, ACCEPT_GRACE_MS);
    };
    ws.onmessage = (e) => {
      if (!live) {
        live = true;
        this.accepted(ws);
      }
      let msg: Partial<LiveMessage>;
      try {
        msg = JSON.parse(e.data) as Partial<LiveMessage>;
      } catch {
        return; // a malformed frame is ignored, never allowed to move our position
      }
      if (typeof msg.seq !== "number" || !Number.isInteger(msg.seq) || !msg.event) return;
      if (msg.seq <= this.lastSeq) return; // already seen (a resync overlap)
      this.lastSeq = msg.seq;
      this.opts.onEvent(msg.seq, msg.event);
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return; // an old socket we already replaced or stopped
      this.ws = null;
      if (this.timer) clearTimeout(this.timer);
      this.timer = null;
      this.opts.onState("DISCONNECTED");
      if (e.code === AUTH_CLOSE) {
        this.opts.onUnauthorized();
        return;
      }
      if (e.code === BAD_HELLO_CLOSE || this.stopped) return;
      const delay = Math.min(1_000 * 2 ** this.attempt, MAX_BACKOFF_MS);
      this.attempt += 1;
      this.timer = setTimeout(() => this.connect(), delay);
    };
  }
}
