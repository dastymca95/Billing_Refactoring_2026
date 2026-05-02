import { useCallback, useEffect, useRef, useState } from "react";

type Props = {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
  compact?: boolean;
};

export function DropZone({ onFiles, disabled, compact }: Props) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // Track nested-element drag enter/leave so dragging over a child doesn't
  // collapse the dragging state (Chrome fires dragleave when entering a child).
  const dragDepthRef = useRef(0);

  const handleDragEnter = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      if (disabled) return;
      dragDepthRef.current += 1;
      setDragging(true);
    },
    [disabled],
  );

  const handleDragOver = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      if (!disabled) {
        // Tell the browser we accept files (not a navigation/download).
        e.dataTransfer.dropEffect = "copy";
        setDragging(true);
      }
    },
    [disabled],
  );

  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragging(false);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      dragDepthRef.current = 0;
      setDragging(false);
      if (disabled) return;
      const files = Array.from(e.dataTransfer.files ?? []);
      if (files.length) onFiles(files);
    },
    [onFiles, disabled],
  );

  const handlePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (disabled) return;
    const files = Array.from(e.target.files ?? []);
    if (files.length) onFiles(files);
    e.target.value = "";
  };

  // Reset the depth counter if the user drags off the page entirely.
  useEffect(() => {
    const reset = () => {
      dragDepthRef.current = 0;
      setDragging(false);
    };
    window.addEventListener("dragend", reset);
    window.addEventListener("mouseup", reset);
    return () => {
      window.removeEventListener("dragend", reset);
      window.removeEventListener("mouseup", reset);
    };
  }, []);

  return (
    <div
      className={`dropzone ${compact ? "compact" : ""} ${dragging ? "dragging" : ""}`}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      onClick={() => !disabled && inputRef.current?.click()}
      role="button"
      aria-label="Upload files"
    >
      <div className="dropzone-icon">↓</div>
      <div className="dropzone-title">
        {dragging ? "Drop to upload" : "Drag bills here, or click"}
      </div>
      <div className="dropzone-hint">CSV · XLSX · PDF · images</div>
      <input
        ref={inputRef}
        type="file"
        multiple
        style={{ display: "none" }}
        onChange={handlePick}
        accept=".csv,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.gif,.webp,.bmp,.docx,.doc,.txt"
      />
    </div>
  );
}
