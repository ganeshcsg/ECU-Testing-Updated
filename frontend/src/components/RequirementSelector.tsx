import { useSession } from "../store/session";
import { clsx } from "clsx";

export default function RequirementSelector() {
  const { requirements, selected, toggle, selectAll, clearAll, generating } = useSession();
  if (!requirements.length) return null;

  const allSelected = selected.size === requirements.length;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
          Requirements <span className="text-slate-400 normal-case font-normal">({selected.size}/{requirements.length})</span>
        </p>
        <button
          onClick={allSelected ? clearAll : selectAll}
          disabled={generating}
          className="text-xs text-indigo-600 hover:text-indigo-800 font-medium"
        >
          {allSelected ? "Clear all" : "Select all"}
        </button>
      </div>

      <div className="space-y-1 max-h-48 overflow-y-auto pr-0.5">
        {requirements.map((req, i) => {
          const checked = selected.has(i);
          return (
            <button
              key={i}
              onClick={() => !generating && toggle(i)}
              className={clsx(
                "w-full flex items-start gap-2.5 rounded-lg px-3 py-2 text-left text-sm transition-colors",
                checked ? "bg-indigo-50 border border-indigo-200" : "bg-white border border-slate-200 hover:border-indigo-200",
                generating && "opacity-60 cursor-not-allowed"
              )}
            >
              <span className={clsx(
                "mt-0.5 shrink-0 w-4 h-4 rounded border-2 flex items-center justify-center transition-colors",
                checked ? "bg-indigo-600 border-indigo-600" : "border-slate-400"
              )}>
                {checked && (
                  <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 10 10">
                    <path d="M1.5 5l2.5 2.5 4.5-4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
              </span>
              <span className="line-clamp-2 text-slate-700">
                <span className="text-slate-400 mr-1">{i + 1}.</span>{req}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
