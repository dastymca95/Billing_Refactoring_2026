// Phase 2G/2N - inline Template processing state.
//
// The processing screen intentionally mirrors the operator reference:
// clean white panel, illustration on the left, status/progress on the
// right, and a small Stop processing button. It does not call processors
// or change business logic; it only renders the current BatchProgress.

import { useEffect, useState } from "react";

import type { BatchProgress } from "../types";

type Props = {
  progress?: BatchProgress | null;
  elapsedMs?: number;
  isCancelling?: boolean;
  onCancel?: () => void;
};

const STAGE_LABELS: { key: string; label: string }[] = [
  { key: "upload", label: "Uploading files" },
  { key: "vendor_detect", label: "Detecting vendor" },
  { key: "read_pdf", label: "Reading PDF text" },
  { key: "ocr", label: "Running OCR" },
  { key: "yaml_rules", label: "Applying vendor rules" },
  { key: "address_match", label: "Matching addresses" },
  { key: "unit_match", label: "Matching units" },
  { key: "gl_evidence", label: "Resolving GL accounts" },
  { key: "reconcile", label: "Reconciling totals" },
  { key: "split_pdf", label: "Splitting bills" },
  { key: "template", label: "Building template" },
];

export function TemplateLoadingState({
  progress,
  elapsedMs = 0,
  isCancelling,
  onCancel,
}: Props) {
  const status = progress?.status ?? "processing";
  const percent = clampPercent(progress?.percent ?? 0);
  const currentStep = (progress?.current_step || "").trim();
  const filesTotal = numOrNull(progress?.files_total);
  const filesDone = numOrNull(progress?.files_done) ?? 0;
  const activeStage = useStageGuess(currentStep, progress?.stages);
  const elapsedLabel = formatElapsed(elapsedMs);
  const avgPerFileLabel =
    filesDone > 0 ? formatElapsed(elapsedMs / filesDone) : "Calculating";

  const heading = isCancelling
    ? "Stopping processing"
    : status === "cancelled"
    ? "Processing cancelled"
    : "Building ResMan template";

  const stageText = isCancelling
    ? "Finishing the current file safely"
    : status === "cancelled"
    ? "No template was written for this run"
    : activeStage || currentStep || "Reading PDF text";

  const filesLine =
    filesTotal != null
      ? `${filesDone} of ${filesTotal} file${filesTotal === 1 ? "" : "s"}`
      : null;

  return (
    <div
      className={`template-loading ${isCancelling ? "is-cancelling" : ""} ${
        status === "cancelled" ? "is-cancelled" : ""
      }`}
      role="status"
      aria-live="polite"
      data-testid="template-loading-state"
    >
      <div className="template-loading-art" data-testid="template-loading-illustration">
        <ProcessingIllustration animated={!isCancelling && status !== "cancelled"} />
      </div>
      <div className="template-loading-copy">
        <h3 className="template-loading-heading">{heading}</h3>
        <p className="template-loading-tagline">{stageText}</p>
        {filesLine && <p className="template-loading-counts">{filesLine}</p>}
        <div
          className="template-loading-progress"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(percent)}
          aria-label="Processing progress"
        >
          <div
            className="template-loading-progress-fill"
            style={{ width: `${percent}%` }}
          />
        </div>
        <div className="template-loading-timing" aria-label="Processing timing">
          <div className="template-loading-timing-item">
            <span className="template-loading-timing-value">{elapsedLabel}</span>
            <span className="template-loading-timing-label">Elapsed</span>
          </div>
          <div className="template-loading-timing-item">
            <span className="template-loading-timing-value">{avgPerFileLabel}</span>
            <span className="template-loading-timing-label">Avg / file</span>
          </div>
        </div>
        <div className="template-loading-meta">
          <span className="template-loading-percent">{Math.round(percent)}%</span>
          {progress?.warnings_count !== undefined &&
            progress.warnings_count > 0 && (
              <span className="template-loading-warning">
                {progress.warnings_count} warning
                {progress.warnings_count === 1 ? "" : "s"}
              </span>
            )}
        </div>
        {onCancel && status !== "cancelled" && (
          <button
            type="button"
            className="template-loading-cancel"
            onClick={onCancel}
            disabled={isCancelling}
            data-testid="template-loading-cancel"
          >
            {isCancelling ? "Stopping..." : "Stop processing"}
          </button>
        )}
      </div>
    </div>
  );
}

function ProcessingIllustration({ animated }: { animated: boolean }) {
  return (
    <svg
      viewBox="0 0 360 240"
      role="img"
      aria-label="Examining bills"
      className={`processing-illustration ${animated ? "is-animated" : ""}`}
    >
      <line x1="18" y1="218" x2="342" y2="218" stroke="var(--border)" strokeWidth="1.2" />

      <g className="illu-receipts">
        <path
          className="illu-strip"
          d="M22 184 C 60 168, 92 206, 130 190 S 206 152, 248 174 S 306 204, 340 178 L 340 148 C 305 170, 276 140, 240 128 S 170 154, 132 166 S 58 144, 22 160 Z"
          fill="#ffffff"
          stroke="var(--accent)"
          strokeWidth="2"
          strokeLinejoin="round"
        />
        <path
          d="M35 184 C 60 174, 88 196, 124 184"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="16"
          strokeLinecap="round"
          opacity="0.95"
        />
        <path
          d="M224 168 C 255 144, 294 174, 330 158"
          fill="none"
          stroke="var(--accent)"
          strokeWidth="16"
          strokeLinecap="round"
          opacity="0.95"
        />
        <g stroke="#d9e1f2" strokeWidth="4" strokeLinecap="round">
          <line x1="70" y1="159" x2="132" y2="159" />
          <line x1="68" y1="171" x2="120" y2="171" />
          <line x1="184" y1="151" x2="228" y2="151" />
          <line x1="188" y1="164" x2="238" y2="164" />
          <line x1="258" y1="186" x2="318" y2="186" />
          <line x1="264" y1="198" x2="308" y2="198" />
        </g>
        <rect
          x="262"
          y="117"
          width="48"
          height="45"
          rx="2"
          fill="rgba(37,99,235,0.10)"
          stroke="var(--text)"
          strokeWidth="2"
          transform="rotate(9 286 139)"
        />
        <path d="M255 75 L322 90 L304 177 L237 162 Z" fill="#ffffff" stroke="var(--text)" strokeWidth="2" />
        <g stroke="#d9e1f2" strokeWidth="4" strokeLinecap="round">
          <line x1="269" y1="94" x2="306" y2="102" />
          <line x1="266" y1="108" x2="300" y2="116" />
          <line x1="254" y1="158" x2="291" y2="166" />
        </g>
      </g>

      <g className="illu-person">
        <path
          d="M92 218 L94 144 C 96 116, 114 101, 142 101 H178 C201 103, 216 119, 219 144 L226 218 Z"
          fill="#ffffff"
          stroke="var(--text)"
          strokeWidth="2.2"
          strokeLinejoin="round"
        />
        <path
          d="M88 132 C 66 142, 46 155, 34 174"
          fill="none"
          stroke="var(--text)"
          strokeWidth="2.2"
          strokeLinecap="round"
        />
        <path
          d="M218 132 C 238 143, 247 164, 250 190"
          fill="none"
          stroke="var(--text)"
          strokeWidth="2.2"
          strokeLinecap="round"
        />
        <path d="M145 101 L145 86 H170 L170 101" fill="#ffffff" stroke="var(--text)" strokeWidth="2.2" />
        <circle cx="158" cy="63" r="26" fill="#ffffff" stroke="var(--text)" strokeWidth="2.2" />
        <path
          d="M129 55 C 130 34, 146 25, 165 30 C 170 22, 185 25, 188 36 C 201 37, 208 46, 205 59 C 191 53, 178 54, 165 57 C 151 61, 140 58, 129 55 Z"
          fill="var(--text)"
        />
        <line x1="149" y1="65" x2="149" y2="70" stroke="var(--text)" strokeWidth="2" strokeLinecap="round" />
        <line x1="169" y1="65" x2="169" y2="70" stroke="var(--text)" strokeWidth="2" strokeLinecap="round" />
        <path d="M151 80 Q 159 86 168 80" fill="none" stroke="var(--text)" strokeWidth="2" strokeLinecap="round" />
      </g>

      <g className="illu-magnifier">
        <circle cx="133" cy="65" r="51" fill="rgba(37,99,235,0.06)" stroke="var(--accent)" strokeWidth="9" />
        <line x1="103" y1="105" x2="70" y2="160" stroke="var(--accent)" strokeWidth="14" strokeLinecap="round" />
        <path d="M111 35 Q 128 20, 151 27" fill="none" stroke="rgba(255,255,255,0.9)" strokeWidth="4" strokeLinecap="round" />
      </g>

      <line
        className="illu-scan"
        x1="46"
        y1="150"
        x2="330"
        y2="150"
        stroke="var(--accent)"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeDasharray="7 7"
        opacity="0.45"
      />
    </svg>
  );
}

function clampPercent(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

function numOrNull(v: unknown): number | null {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return v;
}

function formatElapsed(valueMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(valueMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const two = (n: number) => n.toString().padStart(2, "0");
  if (hours > 0) {
    return `${hours}:${two(minutes)}:${two(seconds)}`;
  }
  return `${minutes}:${two(seconds)}`;
}

function useStageGuess(
  currentStep: string,
  stages: BatchProgress["stages"] | undefined,
): string {
  const [last, setLast] = useState<string>("");

  useEffect(() => {
    if (!currentStep) return;
    const s = currentStep.toLowerCase();
    let match = STAGE_LABELS.find((it) => s.includes(it.key.replace("_", " ")));
    if (!match) match = STAGE_LABELS.find((it) => s.includes(it.label.toLowerCase()));
    if (!match && stages && stages.length > 0) {
      const running = stages.find((st) => st.status === "running");
      if (running) {
        const stageMatch = STAGE_LABELS.find((it) => it.key === running.key);
        if (stageMatch) match = stageMatch;
      }
    }
    if (match) {
      setLast(match.label);
    } else {
      setLast(currentStep.charAt(0).toUpperCase() + currentStep.slice(1));
    }
  }, [currentStep, stages]);

  return last;
}
