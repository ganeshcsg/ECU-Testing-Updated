import client from "./client";

export interface DBCSignal {
  name: string; start_bit: number; length: number; byte_order: string;
  is_signed: boolean; scale: number; offset: number;
  minimum: number | null; maximum: number | null; unit: string; receivers: string[];
}
export interface DBCMessage {
  frame_id: number; frame_id_hex: string; name: string; dlc: number;
  cycle_time: number | null; senders: string[]; signals: DBCSignal[];
}
export interface DBCParseResponse {
  node_names: string[]; messages: DBCMessage[]; total_signals: number; summary: string;
}

export async function parseDbc(file: File): Promise<DBCParseResponse> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await client.post<DBCParseResponse>("/dbc/parse", form);
  return data;
}

export async function parseRequirements(file: File): Promise<{ requirements: string[]; total: number }> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await client.post("/requirements/parse", form);
  return data;
}
