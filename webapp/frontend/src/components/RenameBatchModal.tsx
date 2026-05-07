// Phase 1P — app-native rename batch modal.
//
// Replaces the previous `window.prompt(...)` flow which (a) looked
// unprofessional and (b) couldn't surface backend validation errors.
// Reuses the existing `.modal-card` + `.modal-input` styles from the
// New Batch dialog so the two modals feel like one component family.

import { useEffect, useRef, useState } from "react";

import { getFriendlyErrorMessage } from "../api";

type Props = {
  open: boolean;
  initialName: string;
  onCancel: () => void;
  onSubmit: (newName: string) => Promise<void>;
};

const MAX_NAME = 80;

export function RenameBatchModal({
  open,
  initialName,
  onCancel,
  onSubmit,
}: Props) {
  const [value, setValue] = useState(initialName);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Reset on open + focus / select the input.
  useEffect(() => {
    if (!open) return;
    setValue(initialName);
    setError(null);
    setSaving(false);
    // Focus on next tick so the modal has mounted.
    const handle = window.setTimeout(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    }, 0);
    return () => window.clearTimeout(handle);
  }, [open, initialName]);

  if (!open) return null;

  const handleSubmit = async () => {
    const trimmed = value.trim();
    if (!trimmed) {
      setError("Batch name cannot be empty.");
      inputRef.current?.focus();
      return;
    }
    if (trimmed.length > MAX_NAME) {
      setError(`Batch name is too long (max ${MAX_NAME} characters).`);
      inputRef.current?.focus();
      return;
    }
    if (trimmed === initialName) {
      onCancel();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSubmit(trimmed);
    } catch (e) {
      setError(getFriendlyErrorMessage(e, "Rename batch"));
      // `jsonOrThrow` adds — operators don't need to read it.
      // eslint-disable-next-line no-console
      console.warn("rename batch failed:", e);
      setSaving(false);
    }
  };

  return (
    <div
      className="modal-backdrop"
      data-testid="rename-batch-modal"
      onClick={() => {
        if (!saving) onCancel();
      }}
      role="presentation"
    >
      <div
        className="modal-card modal-card-narrow"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="rename-batch-title"
      >
        <div className="modal-header">
          <span id="rename-batch-title">Rename batch</span>
          <button
            className="icon-btn"
            onClick={onCancel}
            disabled={saving}
            title="Close"
            aria-label="Close"
          >
            <CloseIcon />
          </button>
        </div>
        <div className="modal-body">
          <label className="modal-field">
            <span className="modal-field-label">Batch name</span>
            <input
              ref={inputRef}
              type="text"
              className={`modal-input ${error ? "has-error" : ""}`}
              data-testid="rename-batch-name-input"
              placeholder="e.g. May 2026 Hopkinsville"
              value={value}
              maxLength={MAX_NAME + 5}
              disabled={saving}
              onChange={(e) => {
                setValue(e.target.value);
                if (error) setError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void handleSubmit();
                }
                if (e.key === "Escape" && !saving) {
                  onCancel();
                }
              }}
            />
            {error && <span className="modal-field-error">{error}</span>}
          </label>
        </div>
        <div className="modal-footer">
          <button
            className="btn btn-compact btn-ghost"
            onClick={onCancel}
            disabled={saving}
          >
            Cancel
          </button>
          <button
            className="btn btn-primary btn-compact"
            data-testid="rename-batch-submit"
            onClick={() => void handleSubmit()}
            disabled={saving}
          >
            {saving ? (
              <>
                <span className="spinner" aria-hidden /> Saving…
              </>
            ) : (
              "Save"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <line x1="6" y1="6" x2="18" y2="18" />
      <line x1="18" y1="6" x2="6" y2="18" />
    </svg>
  );
}
