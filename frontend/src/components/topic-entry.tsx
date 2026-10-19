"use client";

import { useState } from "react";

import { Panel, PanelBody, PanelHeader } from "@/components/panel";
import {
  MAX_TOPIC_LENGTH,
  createRun,
  describeApiError,
  validateTopic,
} from "@/lib/api";

type SubmissionNotice = { tone: "info" | "success" | "error"; text: string };

const TONE_CLASSES: Record<SubmissionNotice["tone"], string> = {
  info: "text-zinc-400",
  success: "text-emerald-200/90",
  error: "text-red-200/90",
};

export function TopicEntry({
  onSubmitted,
  backendAvailable = true,
}: {
  onSubmitted?: (jobId: string) => void;
  backendAvailable?: boolean;
}) {
  const [topic, setTopic] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<SubmissionNotice | null>(null);

  const trimmedLength = topic.trim().length;

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    const validation = validateTopic(topic);
    if (!validation.ok) {
      setNotice({ tone: "error", text: validation.message });
      return;
    }

    setSubmitting(true);
    setNotice({ tone: "info", text: "Submitting to the local backend…" });
    try {
      const job = await createRun(validation.topic);
      setTopic("");
      setNotice({
        tone: "success",
        text: `Run queued as ${job.job_id.slice(0, 8)}… — track it under Live runs below.`,
      });
      onSubmitted?.(job.job_id);
    } catch (error) {
      setNotice({ tone: "error", text: describeApiError(error) });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Panel>
      <PanelHeader
        title="Research workspace"
        description="Submit a topic to the local backend. Research executes on this machine through the local engine subprocess; nothing is sent to third-party services by this UI."
      />
      <PanelBody>
        <form
          className="flex flex-col gap-3 sm:flex-row sm:items-center"
          onSubmit={handleSubmit}
        >
          <input
            value={topic}
            onChange={(event) => {
              setTopic(event.target.value);
              if (notice?.tone === "error") setNotice(null);
            }}
            disabled={submitting}
            placeholder="e.g. retrieval-augmented generation evaluation methods"
            className="h-11 min-w-0 flex-1 rounded-md border border-zinc-800 bg-[#111111] px-4 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600 focus:shadow-[0_0_0_1px_rgba(228,228,231,0.08)] disabled:opacity-60"
          />
          <button
            type="submit"
            disabled={submitting || !backendAvailable}
            className="h-11 rounded-md border border-zinc-700 bg-zinc-950 px-5 font-mono text-[12px] uppercase tracking-[0.14em] text-zinc-200 transition-colors hover:border-zinc-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? "Submitting…" : "Start research"}
          </button>
        </form>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          {notice ? (
            <p className={`text-[13px] ${TONE_CLASSES[notice.tone]}`}>
              {notice.text}
            </p>
          ) : (
            <p className="font-mono text-[11px] uppercase tracking-[0.12em] text-zinc-600">
              Runs are local · cancellation supported while active
            </p>
          )}
          {trimmedLength > 0 ? (
            <span
              className={`font-mono text-[11px] ${
                trimmedLength > MAX_TOPIC_LENGTH
                  ? "text-red-300/90"
                  : "text-zinc-600"
              }`}
            >
              {trimmedLength} / {MAX_TOPIC_LENGTH}
            </span>
          ) : null}
        </div>
        {!backendAvailable ? (
          <p className="mt-2 text-[12px] text-amber-200/80">
            The backend is currently unreachable, so submissions are paused.
          </p>
        ) : null}
      </PanelBody>
    </Panel>
  );
}
