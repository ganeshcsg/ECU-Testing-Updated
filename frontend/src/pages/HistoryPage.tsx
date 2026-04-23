import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Clock, X, Star, CheckCircle, XCircle } from "lucide-react";
import { clsx } from "clsx";
import { toast } from "sonner";

import { listArtifacts, getArtifact, submitFeedback, Artifact } from "../api/artifacts";
import TestCasesPanel from "../components/TestCasesPanel";
import CaplEditor from "../components/CaplEditor";
import { TestCase } from "../store/session";

type Tab = "test_cases" | "capl" | "feedback";

function Badge({ status }: { status: string | null }) {
  const ok = status === "success";
  return (
    <span className={clsx("inline-flex items-center gap-1 text-xs font-medium px-2 py-0.5 rounded-full",
      ok ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700")}>
      {ok ? <CheckCircle size={11} /> : <XCircle size={11} />}{status ?? "unknown"}
    </span>
  );
}

function Stars({ value, onChange }: { value: number; onChange(v: number): void }) {
  const [hov, setHov] = useState(0);
  return (
    <div className="flex gap-1">
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} onMouseEnter={() => setHov(n)} onMouseLeave={() => setHov(0)} onClick={() => onChange(n)}>
          <Star size={22} className={clsx((hov || value) >= n ? "text-yellow-400 fill-yellow-400" : "text-slate-300")} />
        </button>
      ))}
    </div>
  );
}

function Sheet({ artifact, onClose }: { artifact: Artifact; onClose(): void }) {
  const [tab, setTab] = useState<Tab>("test_cases");
  const [rating, setRating] = useState(0);
  const [fbText, setFbText] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const { data: full } = useQuery({ queryKey: ["artifact", artifact.id], queryFn: () => getArtifact(artifact.id) });

  const tcs: TestCase[] = (() => {
    const src = full?.test_cases ?? artifact.test_cases;
    if (!src) return [];
    if (Array.isArray(src)) return src as TestCase[];
    if (Array.isArray((src as any).test_cases)) return (src as any).test_cases as TestCase[];
    return [];
  })();

  async function handleFeedback() {
    if (!rating) return;
    setSubmitting(true);
    try { await submitFeedback(artifact.id, rating, fbText || undefined); toast.success("Feedback submitted!"); setRating(0); setFbText(""); }
    catch { toast.error("Failed."); }
    finally { setSubmitting(false); }
  }

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-full max-w-2xl bg-white shadow-2xl flex flex-col h-full">
        <div className="flex items-start justify-between px-6 py-4 border-b border-slate-200">
          <div>
            <p className="font-semibold text-slate-800">Artifact #{artifact.id}</p>
            <p className="text-xs text-slate-400 mt-0.5 line-clamp-1">{artifact.requirement_text ?? "—"}</p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-100 transition-colors text-slate-500">
            <X size={17} />
          </button>
        </div>

        <div className="border-b border-slate-200 flex gap-0 px-6">
          {(["test_cases", "capl", "feedback"] as Tab[]).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={clsx("tab-btn capitalize", tab === t ? "tab-btn-active" : "tab-btn-inactive")}>
              {t === "test_cases" ? "Test Cases" : t === "capl" ? "CAPL Script" : "Feedback"}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-auto px-6 py-4">
          {tab === "test_cases" && <TestCasesPanel testCases={tcs} />}
          {tab === "capl" && <CaplEditor script={full?.capl_code ?? artifact.capl_code ?? ""} filename={`artifact_${artifact.id}.can`} />}
          {tab === "feedback" && (
            <div className="space-y-4">
              <p className="text-sm text-slate-600">Rate the quality of this generation:</p>
              <Stars value={rating} onChange={setRating} />
              <textarea rows={4} placeholder="Optional comments…" value={fbText} onChange={(e) => setFbText(e.target.value)}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400 resize-none" />
              <button onClick={handleFeedback} disabled={!rating || submitting}
                className={clsx("btn-primary", (!rating || submitting) && "opacity-40 cursor-not-allowed")}>
                {submitting ? "Submitting…" : "Submit Feedback"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function HistoryPage() {
  const [open, setOpen] = useState<Artifact | null>(null);
  const { data, isLoading, error } = useQuery({ queryKey: ["artifacts"], queryFn: () => listArtifacts() });

  if (isLoading) return <div className="flex items-center justify-center py-24 text-slate-400 text-sm">Loading…</div>;
  if (error) return <div className="flex items-center justify-center py-24 text-red-500 text-sm">Failed to load history.</div>;
  if (!data?.length)
    return (
      <div className="flex flex-col items-center justify-center py-24 text-slate-400">
        <Clock size={40} className="mb-3 opacity-30" />
        <p className="text-sm">No generations yet. Go to Generate to start.</p>
      </div>
    );

  return (
    <>
      <div className="card overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200">
          <h2 className="text-sm font-semibold text-slate-800">Generation History</h2>
          <p className="text-xs text-slate-400 mt-0.5">{data.length} artifact{data.length !== 1 ? "s" : ""}</p>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-50 border-b border-slate-100 text-left">
              {["ID", "Requirement", "Status", "Time (s)", "Date", ""].map((h) => (
                <th key={h} className="px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.map((a) => (
              <tr key={a.id} className="hover:bg-slate-50 transition-colors">
                <td className="px-5 py-3 font-mono text-slate-400 text-xs">#{a.id}</td>
                <td className="px-5 py-3 max-w-xs"><p className="line-clamp-2 text-slate-700">{a.requirement_text ?? "—"}</p></td>
                <td className="px-5 py-3"><Badge status={a.status} /></td>
                <td className="px-5 py-3 text-slate-500">{a.generation_time_seconds?.toFixed(1) ?? "—"}</td>
                <td className="px-5 py-3 text-slate-400 text-xs whitespace-nowrap">{new Date(a.created_at).toLocaleString()}</td>
                <td className="px-5 py-3">
                  <button onClick={() => setOpen(a)} className="text-xs text-indigo-600 hover:underline font-medium">View</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {open && <Sheet artifact={open} onClose={() => setOpen(null)} />}
    </>
  );
}
