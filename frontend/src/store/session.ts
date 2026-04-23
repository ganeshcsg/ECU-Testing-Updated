import { create } from "zustand";
import { DBCParseResponse } from "../api/dbc";

export interface TestCase {
  test_id?: string; name?: string; type?: string; description?: string;
  preconditions?: string; steps?: string[]; expected_result?: string;
  [key: string]: unknown;
}

export interface GenResult {
  testCases: TestCase[]; caplScript: string;
  artifactId: number | null; elapsed: number;
}

interface Session {
  dbcFile: File | null; dbcParsed: DBCParseResponse | null;
  reqFile: File | null; requirements: string[];
  selected: Set<number>; generating: boolean; genStep: string;
  results: Map<number, GenResult>; abort: AbortController | null;

  setDbc(f: File | null, p: DBCParseResponse | null): void;
  setReqs(f: File | null, r: string[]): void;
  toggle(i: number): void; selectAll(): void; clearAll(): void;
  setGenerating(v: boolean, step?: string): void;
  setResult(i: number, r: GenResult): void;
  setAbort(ac: AbortController | null): void;
  resetRun(): void;
}

export const useSession = create<Session>((set, get) => ({
  dbcFile: null, dbcParsed: null, reqFile: null, requirements: [],
  selected: new Set(), generating: false, genStep: "", results: new Map(), abort: null,

  setDbc: (f, p) => set({ dbcFile: f, dbcParsed: p }),
  setReqs: (f, r) => set({ reqFile: f, requirements: r, selected: new Set(), results: new Map() }),
  toggle: (i) => set((s) => { const n = new Set(s.selected); n.has(i) ? n.delete(i) : n.add(i); return { selected: n }; }),
  selectAll: () => set((s) => ({ selected: new Set(s.requirements.map((_, i) => i)) })),
  clearAll: () => set({ selected: new Set() }),
  setGenerating: (v, step = "") => set({ generating: v, genStep: step }),
  setResult: (i, r) => set((s) => { const m = new Map(s.results); m.set(i, r); return { results: m }; }),
  setAbort: (ac) => set({ abort: ac }),
  resetRun: () => set({ generating: false, genStep: "", abort: null }),
}));
