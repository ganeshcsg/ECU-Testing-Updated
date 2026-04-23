import { clsx } from "clsx";
import { CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";

const STEPS = [
  { id: "parsing_dbc", label: "Parse DBC" },
  { id: "test_cases",  label: "Test Cases" },
  { id: "analysis",   label: "Analysis" },
  { id: "capl",       label: "CAPL" },
] as const;

type StepId = (typeof STEPS)[number]["id"];

export default function GenerationProgress({
  currentStep, done, error,
}: { currentStep: string; done: boolean; error: string | null }) {
  const cur = STEPS.findIndex((s) => s.id === currentStep);

  return (
    <div className="flex items-center gap-3">
      {STEPS.map((step, i) => {
        const completed = done || i < cur;
        const active = !done && i === cur;
        const isError = !!error && active;

        return (
          <div key={step.id} className="flex items-center gap-3">
            <div className="flex flex-col items-center gap-1 min-w-[56px]">
              <span className={clsx(
                "transition-colors",
                isError ? "text-red-500" : completed ? "text-emerald-500" : active ? "text-indigo-500" : "text-slate-300"
              )}>
                {isError ? <XCircle size={22} /> : active ? <Loader2 size={22} className="animate-spin" /> : completed ? <CheckCircle2 size={22} /> : <Circle size={22} />}
              </span>
              <span className={clsx(
                "text-[11px] font-medium text-center",
                isError ? "text-red-500" : completed ? "text-emerald-600" : active ? "text-indigo-600" : "text-slate-400"
              )}>{step.label}</span>
            </div>
            {i < STEPS.length - 1 && (
              <div className={clsx("h-0.5 w-6 rounded mb-4 transition-colors", (i < cur || done) ? "bg-emerald-400" : "bg-slate-200")} />
            )}
          </div>
        );
      })}
    </div>
  );
}
