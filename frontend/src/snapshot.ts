// Read one API snapshot and re-read it whenever the app's revision moves (live events,
// reconnects, polling). Late answers from superseded requests are dropped.
import { useEffect, useState } from "react";
import { useApp } from "./app";

export interface Snapshot<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
}

export function useSnapshot<T>(read: (api: ReturnType<typeof useApp>["api"]) => Promise<T>) {
  const { api, revision } = useApp();
  const [snap, setSnap] = useState<Snapshot<T>>({ data: null, error: null, loading: true });
  // biome-ignore lint/correctness/useExhaustiveDependencies: `read` is a stable view-level reader
  useEffect(() => {
    let alive = true;
    read(api).then(
      (data) => alive && setSnap({ data, error: null, loading: false }),
      (error: unknown) => alive && setSnap((s) => ({ ...s, error, loading: false })),
    );
    return () => {
      alive = false;
    };
  }, [api, revision]);
  return snap;
}
