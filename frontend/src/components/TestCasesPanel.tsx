import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { clsx } from "clsx";
import { TestCase } from "../store/session";

function typeMeta(type?: string) {
  const t = (type ?? "").toLowerCase();
  if (t.includes("positive")) return { label: "Positive", cls: "bg-emerald-100 text-emerald-700" };
  if (t.includes("negative")) return { label: "Negative", cls: "bg-red-100 text-red-700" };
  return { label: type || "Edge", cls: "bg-amber-100 text-amber-700" };
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider mb-1">{title}</p>
      <div className="text-sm text-slate-700">{children}</div>
    </div>
  );
}

function Card({ tc, index }: { tc: TestCase; index: number }) {
  const [open, setOpen] = useState(index === 0);
  const meta = typeMeta(tc.type);

  return (
    <div className="border border-slate-200 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-3 bg-white hover:bg-slate-50 transition-colors text-left gap-3"
      >
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="font-mono text-xs text-slate-400 shrink-0">
            {tc.test_id || `TC_${String(index + 1).padStart(3, "0")}`}
          </span>
          <span className={clsx("text-xs font-medium px-2 py-0.5 rounded-full shrink-0", meta.cls)}>{meta.label}</span>
          <span className="text-sm font-medium text-slate-800 truncate">{tc.name || "Unnamed"}</span>
        </div>
        {open ? <ChevronDown size={15} className="shrink-0 text-slate-400" /> : <ChevronRight size={15} className="shrink-0 text-slate-400" />}
      </button>

      {open && (
        <div className="px-4 pb-4 pt-2 bg-slate-50 space-y-3 border-t border-slate-100">
          {tc.description && <Section title="Description">{tc.description}</Section>}
          {tc.preconditions && <Section title="Preconditions">{tc.preconditions}</Section>}
          {Array.isArray(tc.steps) && tc.steps.length > 0 && (
            <Section title="Steps">
              <ol className="list-decimal list-inside space-y-0.5">
                {tc.steps.map((s, i) => <li key={i}>{String(s)}</li>)}
              </ol>
            </Section>
          )}
          {tc.expected_result && <Section title="Expected Result">{tc.expected_result}</Section>}
        </div>
      )}
    </div>
  );
}

export default function TestCasesPanel({ testCases }: { testCases: TestCase[] }) {
  if (!testCases.length)
    return <p className="text-sm text-slate-400 text-center py-10">Generate to see test cases here.</p>;

  return (
    <div className="space-y-2">
      <p className="text-xs text-slate-400 mb-3">{testCases.length} test case{testCases.length !== 1 ? "s" : ""} generated</p>
      {testCases.map((tc, i) => <Card key={tc.test_id || i} tc={tc} index={i} />)}
    </div>
  );
}
