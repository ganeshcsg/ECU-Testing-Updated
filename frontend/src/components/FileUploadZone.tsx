import { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { Upload, FileCheck, X } from "lucide-react";
import { clsx } from "clsx";

interface Props {
  label: string; accept: Record<string, string[]>;
  file: File | null; onFile(f: File): void; onClear(): void;
  disabled?: boolean; hint?: string;
}

export default function FileUploadZone({ label, accept, file, onFile, onClear, disabled, hint }: Props) {
  const onDrop = useCallback((acc: File[]) => { if (acc[0]) onFile(acc[0]); }, [onFile]);
  const { getRootProps, getInputProps, isDragActive } = useDropzone({ onDrop, accept, multiple: false, disabled });

  return (
    <div>
      <label className="block text-xs font-semibold text-slate-600 mb-1.5 uppercase tracking-wide">{label}</label>
      {file ? (
        <div className="flex items-center justify-between rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2.5">
          <span className="flex items-center gap-2 text-emerald-700 text-sm font-medium">
            <FileCheck size={16} />
            <span className="truncate max-w-[180px]">{file.name}</span>
          </span>
          <button onClick={onClear} disabled={disabled} className="text-emerald-400 hover:text-red-400 transition-colors">
            <X size={15} />
          </button>
        </div>
      ) : (
        <div
          {...getRootProps()}
          className={clsx(
            "flex flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-5 cursor-pointer transition-all",
            isDragActive ? "border-indigo-400 bg-indigo-50" : "border-slate-300 bg-white hover:border-indigo-300 hover:bg-indigo-50/50",
            disabled && "opacity-50 cursor-not-allowed"
          )}
        >
          <input {...getInputProps()} />
          <Upload size={22} className={clsx("mb-1.5", isDragActive ? "text-indigo-500" : "text-slate-400")} />
          <p className="text-sm text-slate-500 text-center">
            {isDragActive ? "Drop file here…" : "Drag & drop or click to browse"}
          </p>
          {hint && <p className="text-xs text-slate-400 mt-0.5">{hint}</p>}
        </div>
      )}
    </div>
  );
}
