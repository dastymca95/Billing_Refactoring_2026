// Phase 1H — premium processing timeline.
//
// Reads `progress.stages[]` (declared by the backend in
// batch_processor.py:_DEFAULT_STAGES). When the array is empty (legacy
// progress JSON), the component renders nothing so old behaviour is
// preserved. Each stage is one row with a status icon, label, optional
// detail line, and a duration once completed.

import { useMemo, useState } from "react";

import type {
  BatchProgress,
  ProcessingStage,
  ProcessingStageStatus,
} from "../types";

type Props = {
  progress: BatchProgress | null;
};

export function ProcessingTimeline({ progress }: Props) {
  const stages = progress?.stages ?? [];
  const [expanded, setExpanded] = useState(true);

  const summary = useMemo(() => summarise(stages), [stages]);

  if (stages.length === 0) return null;

  return (
    <div className="timeline">
      <button
        type="button"
        className="timeline-header"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="timeline-title">Processing timeline</span>
        <span className="timeline-summary">{summary}</span>
        <span className="timeline-caret">{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && (
        <ul className="timeline-list">
          {stages.map((stage) => (
            <li
              key={stage.key}
              className={`timeline-row status-${stage.status}`}
              title={stage.detail || stage.label}
            >
              <span className={`timeline-dot status-${stage.status}`}>
                {iconFor(stage.status)}
              </span>
              <span className="timeline-label">{stage.label}</span>
              {stage.detail && (
                <span className="timeline-detail">{stage.detail}</span>
              )}
              <span className="timeline-time">{durationFor(stage)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function iconFor(s: ProcessingStageStatus): string {
  switch (s) {
    case "pending":
      return "·";
    case "running":
      return "◔";
    case "completed":
      return "✓";
    case "warning":
      return "!";
    case "failed":
      return "✕";
    case "skipped":
      return "—";
  }
}

function summarise(stages: ProcessingStage[]): string {
  const total = stages.length;
  const completed = stages.filter((s) => s.status === "completed").length;
  const running = stages.filter((s) => s.status === "running").length;
  const failed = stages.filter((s) => s.status === "failed").length;
  const warned = stages.filter((s) => s.status === "warning").length;
  const parts: string[] = [];
  parts.push(`${completed}/${total} done`);
  if (running) parts.push(`${running} running`);
  if (warned) parts.push(`${warned} warning`);
  if (failed) parts.push(`${failed} failed`);
  return parts.join(" · ");
}

function durationFor(s: ProcessingStage): string {
  if (!s.started_at) return "";
  const start = Date.parse(s.started_at);
  if (Number.isNaN(start)) return "";
  const end = s.completed_at ? Date.parse(s.completed_at) : Date.now();
  if (Number.isNaN(end)) return "";
  const delta = Math.max(0, end - start);
  if (delta < 1000) return `${delta}ms`;
  const sec = delta / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const min = Math.floor(sec / 60);
  const rem = Math.round(sec - min * 60);
  return `${min}m${rem.toString().padStart(2, "0")}s`;
}
