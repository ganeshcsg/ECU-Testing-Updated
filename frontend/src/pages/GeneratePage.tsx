import { useState } from "react";
import { toast } from "sonner";
import { Play, Square } from "lucide-react";
import { clsx } from "clsx";

import FileUploadZone from "../components/FileUploadZone";
import RequirementSelector from "../components/RequirementSelector";
import GenerationProgress from "../components/GenerationProgress";
import TestCasesPanel from "../components/TestCasesPanel";
import CaplEditor from "../components/CaplEditor";

import { parseDbc, parseRequirements } from "../api/dbc";
import { streamGeneration } from "../api/generate";
import { useSession, TestCase } from "../store/session";

function toBase64(file: File): Promise<string> {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.readAsDataURL(file);
    r.onload = () => res((r.result as string).split(",")[1]);
    r.onerror = rej;
  });
}

type Tab = "test_cases" | "capl";

export default function GeneratePage() {
  const {
    dbcFile, reqFile, requirements, selected,
    generating, genStep, results,
    setDbc, setReqs, setGenerating, setResult, setAbort, abort, resetRun,
  } = useSession();

  const [activeIdx, setActiveIdx] = useState<number | null>(null);
  const [curStep, setCurStep] = useState("");
  const [isDone, setIsDone] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("test_cases");

  const selectedList = Array.from(selected).sort((a, b) => a - b);
  const canGenerate = !generating && !!dbcFile && selectedList.length > 0;

  async function handleDbc(file: File) {
    try {
      const parsed = await parseDbc(file);
      setDbc(file, parsed);
      toast.success(`${parsed.messages.length} messages, ${parsed.total_signals} signals`);
    } catch (e: any) {
      toast.error(e?.response?.data?.detail ?? e.message);
    }
  }

  async function handleReq(file: File) {
    try {
      const { requirements: reqs } = await parseRequirements(file);
      setReqs(file, reqs);
      toast.success(`${reqs.length} requirement${reqs.length !== 1 ? "s" : ""} found`);
    } catch (e: any) {
      toast.error(e?.response?.data?.detail ?? e.message);
    }
  }

  async function handleGenerate() {
    if (!dbcFile || !canGenerate) return;
    const ac = new AbortController();
    setAbort(ac);
    setGenerating(true, "parsing_dbc");
    setIsDone(false);
    setGenError(null);
    const dbcB64 = await toBase64(dbcFile);

    for (const idx of selectedList) {
      setActiveIdx(idx);
      let tcs: TestCase[] = [], capl = "", artId: number | null = null, elapsed = 0, err = false;
      try {
        for await (const ev of streamGeneration(requirements[idx], dbcB64, ac.signal)) {
          if (ev.event === "status") { setCurStep(ev.data.step); setGenerating(true, ev.data.step); }
          else if (ev.event === "test_cases_done") tcs = ev.data.test_cases as TestCase[];
          else if (ev.event === "capl_done") capl = ev.data.capl_script;
          else if (ev.event === "done") { artId = ev.data.artifact_id; elapsed = ev.data.generation_time_seconds; }
          else if (ev.event === "error") { toast.error(`${ev.data.step}: ${ev.data.message}`); err = true; break; }
        }
      } catch (e: any) {
        if (e.name !== "AbortError") { toast.error(e.message); err = true; }
      }
      if (!err) { setResult(idx, { testCases: tcs, caplScript: capl, artifactId: artId, elapsed }); toast.success(`Req ${idx + 1} done in ${elapsed}s`); }
      if (ac.signal.aborted) break;
    }
    setIsDone(true);
    setGenerating(false);
    resetRun();
  }

  const dispIdx = activeIdx ?? selectedList[0] ?? null;
  const result = dispIdx !== null ? results.get(dispIdx) : null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-5">
      {/* LEFT */}
      <div className="space-y-4">
        <div className="card p-5 space-y-4">
          <h2 className="text-sm font-semibold text-slate-700">1 — Upload Files</h2>
          <FileUploadZone label="DBC File" accept={{ "application/octet-stream": [".dbc"] }}
            file={dbcFile} onFile={handleDbc} onClear={() => setDbc(null, null)} disabled={generating} hint=".dbc" />
          <FileUploadZone label="Requirements File"
            accept={{ "application/pdf": [".pdf"], "text/plain": [".txt"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"], "application/vnd.ms-excel": [".xls"] }}
            file={reqFile} onFile={handleReq} onClear={() => setReqs(null, [])} disabled={generating} hint="PDF, TXT, XLSX" />
        </div>

        {requirements.length > 0 && (
          <div className="card p-5">
            <h2 className="text-sm font-semibold text-slate-700 mb-3">2 — Select Requirements</h2>
            <RequirementSelector />
          </div>
        )}

        {generating ? (
          <button onClick={() => { abort?.abort(); setGenerating(false); resetRun(); }}
            className="w-full btn-primary bg-red-500 hover:bg-red-600 justify-center">
            <Square size={15} /> Stop
          </button>
        ) : (
          <button onClick={handleGenerate} disabled={!canGenerate}
            className={clsx("w-full btn-primary justify-center", !canGenerate && "opacity-40 cursor-not-allowed")}>
            <Play size={15} />
            {selectedList.length > 1 ? `Generate (${selectedList.length} reqs)` : "Generate"}
          </button>
        )}

        {(generating || isDone) && (
          <div className="card p-4">
            <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-3">Progress</p>
            <GenerationProgress currentStep={curStep} done={isDone && !generating} error={genError} />
          </div>
        )}
      </div>

      {/* RIGHT */}
      <div className="card p-5">
        <h2 className="text-sm font-semibold text-slate-700 mb-4">3 — Results</h2>

        {selectedList.length > 1 && (
          <div className="flex flex-wrap gap-1.5 mb-4">
            {selectedList.map((idx) => (
              <button key={idx} onClick={() => setActiveIdx(idx)}
                className={clsx("text-xs px-2.5 py-1 rounded-full font-medium transition-colors",
                  dispIdx === idx ? "bg-indigo-600 text-white" : results.has(idx) ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500")}>
                Req {idx + 1}{results.has(idx) ? " ✓" : ""}
              </button>
            ))}
          </div>
        )}

        <div className="border-b border-slate-200 flex gap-0 mb-4">
          {(["test_cases", "capl"] as Tab[]).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={clsx("tab-btn", tab === t ? "tab-btn-active" : "tab-btn-inactive")}>
              {t === "test_cases" ? "Test Cases" : "CAPL Script"}
            </button>
          ))}
        </div>

        {tab === "test_cases" && <TestCasesPanel testCases={result?.testCases ?? []} />}
        {tab === "capl" && <CaplEditor script={result?.caplScript ?? ""} />}
      </div>
    </div>
  );
}
