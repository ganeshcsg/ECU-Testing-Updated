import Editor from "@monaco-editor/react";
import { Download } from "lucide-react";

export default function CaplEditor({ script, filename = "simulation.can" }: { script: string; filename?: string }) {
  if (!script)
    return <p className="text-sm text-slate-400 text-center py-10">Generate to see the CAPL script here.</p>;

  function download() {
    const url = URL.createObjectURL(new Blob([script], { type: "text/plain" }));
    Object.assign(document.createElement("a"), { href: url, download: filename }).click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <button onClick={download} className="btn-secondary text-indigo-600 border-indigo-300 hover:bg-indigo-50">
          <Download size={14} /> Download .can
        </button>
      </div>
      <div className="rounded-lg overflow-hidden border border-slate-200">
        <Editor
          height="460px" language="c" value={script}
          options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13,
            lineNumbers: "on", scrollBeyondLastLine: false, wordWrap: "on" }}
        />
      </div>
    </div>
  );
}
