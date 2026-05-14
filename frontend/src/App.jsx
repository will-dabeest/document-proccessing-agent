import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

const api = axios.create({ baseURL: "/api" });

function StatusBadge({ status }) {
  const done = status === "Completed";
  return (
    <span
      className={`rounded px-2 py-1 text-sm ${
        done ? "bg-green-100 text-green-800" : "bg-yellow-100 text-yellow-800"
      }`}
    >
      {status || "Unknown"}
    </span>
  );
}

async function fetchDocuments() {
  const { data } = await api.get("/documents");
  return data.items || [];
}

export default function App() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState(null);
  const [importUrl, setImportUrl] = useState("");
  const [question, setQuestion] = useState("");
  const [askResult, setAskResult] = useState(null);

  const { data: documents = [], isFetching } = useQuery({
    queryKey: ["documents"],
    queryFn: fetchDocuments,
    refetchInterval: 3000,
  });

  const uploadMutation = useMutation({
    mutationFn: async (f) => {
      const form = new FormData();
      form.append("file", f);
      return api.post("/upload", form, {
        headers: { "Content-Type": "multipart/form-data" },
      });
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["documents"] }),
  });

  const importUrlMutation = useMutation({
    mutationFn: async (url) => {
      return api.post("/import-url", { url: url.trim() });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      setImportUrl("");
    },
  });

  async function submitAsk() {
    setAskResult(null);
    const { data } = await api.post("/ask", { question });
    setAskResult(data);
  }

  return (
    <div className="mx-auto max-w-3xl space-y-8 p-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Document processing
        </h1>
        <p className="text-sm text-slate-600">
          Upload files or import a URL, track status from DynamoDB, ask questions
          (RAG).
        </p>
      </header>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-2 font-medium">Upload</h2>
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="file"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
            className="text-sm"
          />
          <button
            type="button"
            disabled={!file || uploadMutation.isPending}
            onClick={() => file && uploadMutation.mutate(file)}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {uploadMutation.isPending ? "Uploading…" : "Upload"}
          </button>
        </div>
        {uploadMutation.isError && (
          <p className="mt-2 text-sm text-red-600">Upload failed.</p>
        )}
        {uploadMutation.isSuccess && (
          <p className="mt-2 text-sm text-green-700">Uploaded successfully.</p>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-2 font-medium">Import from URL</h2>
        <p className="mb-2 text-sm text-slate-600">
          Paste an https link to HTML, PDF, plain text, or markdown (single page).
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="url"
            value={importUrl}
            onChange={(e) => setImportUrl(e.target.value)}
            placeholder="https://example.com/docs/page"
            className="min-w-[12rem] flex-1 rounded border border-slate-300 p-2 text-sm"
          />
          <button
            type="button"
            disabled={!importUrl.trim() || importUrlMutation.isPending}
            onClick={() => importUrlMutation.mutate(importUrl)}
            className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {importUrlMutation.isPending ? "Importing…" : "Import"}
          </button>
        </div>
        {importUrlMutation.isError && (
          <p className="mt-2 text-sm text-red-600">
            {(() => {
              const d = importUrlMutation.error?.response?.data?.detail;
              if (d == null) return "Import failed.";
              return typeof d === "string" ? d : JSON.stringify(d);
            })()}
          </p>
        )}
        {importUrlMutation.isSuccess && (
          <p className="mt-2 text-sm text-green-700">Imported successfully.</p>
        )}
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-medium">Documents</h2>
          {isFetching && (
            <span className="text-xs text-slate-500">Refreshing…</span>
          )}
        </div>
        <ul className="divide-y divide-slate-100">
          {documents.length === 0 && (
            <li className="py-3 text-sm text-slate-500">No rows yet.</li>
          )}
          {documents.map((row, idx) => (
            <li
              key={row.message_id || `row-${idx}`}
              className="flex flex-wrap items-center justify-between gap-2 py-3"
            >
              <span className="font-mono text-xs text-slate-700">
                {row.message_id}
              </span>
              <StatusBadge status={row.status} />
              <span className="w-full text-sm text-slate-600 md:w-auto">
                {row.classification}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-2 font-medium">Ask (semantic search)</h2>
        <div className="space-y-2">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            className="w-full rounded border border-slate-300 p-2 text-sm"
            placeholder="Ask about uploaded documents…"
          />
          <button
            type="button"
            onClick={submitAsk}
            className="rounded bg-slate-800 px-4 py-2 text-sm text-white"
          >
            Ask
          </button>
        </div>
        {askResult && (
          <div className="mt-4 space-y-2 rounded border border-slate-100 p-3 text-sm">
            <p className="whitespace-pre-wrap">{askResult.answer}</p>
            {askResult.snippets?.length > 0 && (
              <details>
                <summary className="cursor-pointer text-slate-600">
                  Retrieved snippets
                </summary>
                <ul className="mt-2 list-inside list-disc text-slate-600">
                  {askResult.snippets.map((s, i) => (
                    <li key={i}>{s.text?.slice(0, 200)}…</li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
