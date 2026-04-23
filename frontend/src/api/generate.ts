export type SSEEvent =
  | { event: "status"; data: { step: string; message: string } }
  | { event: "test_cases_done"; data: { test_cases: unknown[] } }
  | { event: "analysis_done"; data: { analysis: unknown } }
  | { event: "capl_done"; data: { capl_script: string } }
  | { event: "done"; data: { artifact_id: number | null; generation_time_seconds: number } }
  | { event: "error"; data: { step: string; message: string } };

export async function* streamGeneration(
  requirement: string,
  dbcB64: string,
  signal?: AbortSignal
): AsyncGenerator<SSEEvent> {
  const res = await fetch("/api/generate/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requirement, dbc_b64: dbcB64 }),
    signal,
  });

  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";

    for (const block of blocks) {
      let name = "", dataStr = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7).trim();
        if (line.startsWith("data: ")) dataStr = line.slice(6).trim();
      }
      if (name && dataStr) {
        try { yield { event: name, data: JSON.parse(dataStr) } as SSEEvent; } catch { /* skip */ }
      }
    }
  }
}
