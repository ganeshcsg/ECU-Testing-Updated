import client from "./client";

export interface Artifact {
  id: number; requirement_text: string | null; test_cases: unknown | null;
  capl_code: string | null; llm_model: string | null; status: string | null;
  generation_time_seconds: number | null; created_at: string;
}

export const listArtifacts = async (limit = 50, offset = 0) =>
  (await client.get<Artifact[]>("/artifacts", { params: { limit, offset } })).data;

export const getArtifact = async (id: number) =>
  (await client.get<Artifact>(`/artifacts/${id}`)).data;

export const submitFeedback = async (id: number, score: number, text?: string) =>
  client.post(`/artifacts/${id}/feedback`, { score, text });
