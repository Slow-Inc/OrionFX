// Sending a control command and following it to M06's answer (#74). "Sent" is never
// "done": the phase stays PENDING until `control.completed` arrives, an emergency stop
// with positions left open reads UNRESOLVED, and a slow answer reads STILL_PENDING.
import { type Api, ApiError } from "./api";
import type { CommandResult, ControlAction } from "./types";

export type CommandPhase =
  | "SENDING"
  | "PENDING"
  | "COMPLETED"
  | "REJECTED"
  | "UNRESOLVED"
  | "STILL_PENDING"
  | "REFUSED";

export interface CommandState {
  action: ControlAction;
  phase: CommandPhase;
  commandId?: string;
  result?: CommandResult;
  message?: string;
}

export interface RunOptions {
  onUpdate: (state: CommandState) => void;
  sleep?: (ms: number) => Promise<void>;
  pollMs?: number;
  timeoutPolls?: number;
}

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function refusal(err: unknown): string {
  if (err instanceof ApiError) {
    const missing = (err.detail as { missing?: string[] } | null)?.missing;
    if (missing?.length) return `Missing settings: ${missing.join(", ")}`;
    return err.message;
  }
  return "The command could not be sent.";
}

export async function runCommand(
  api: Api,
  action: ControlAction,
  { onUpdate, sleep = wait, pollMs = 1_000, timeoutPolls = 60 }: RunOptions,
): Promise<CommandState> {
  const emit = (s: CommandState) => {
    onUpdate(s);
    return s;
  };
  emit({ action, phase: "SENDING" });
  let commandId: string;
  try {
    commandId = (await api.command(action)).command_id;
  } catch (err) {
    return emit({ action, phase: "REFUSED", message: refusal(err) });
  }
  emit({ action, phase: "PENDING", commandId });
  for (let i = 0; i < timeoutPolls; i++) {
    await sleep(pollMs);
    let result: CommandResult;
    try {
      result = await api.commandStatus(commandId);
    } catch {
      continue; // a failed poll is not an answer: keep waiting
    }
    if (result.status === "ACCEPTED") {
      emit({ action, phase: "PENDING", commandId });
      continue;
    }
    return emit({ action, phase: result.status, commandId, result });
  }
  return emit({
    action,
    phase: "STILL_PENDING",
    commandId,
    message: "No answer from the bot yet. It may still be working: check before acting again.",
  });
}
