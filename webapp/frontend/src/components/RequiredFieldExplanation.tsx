import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";

import type { ReadinessIssue } from "../types";

export function RequiredFieldExplanation({ issue }: { issue: ReadinessIssue }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const closeTimer = useRef<number | null>(null);
  const id = useId();
  const clearClose = () => {
    if (closeTimer.current != null) window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const show = () => { clearClose(); setOpen(true); };
  const hideSoon = () => {
    clearClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 90);
  };
  const updatePosition = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.min(360, window.innerWidth * 0.78);
    const height = popoverRef.current?.offsetHeight || 150;
    setPosition({
      left: Math.max(12, Math.min(window.innerWidth - width - 12, rect.right + 8)),
      top: Math.max(12, Math.min(window.innerHeight - height - 12, rect.top - 8)),
    });
  };
  useLayoutEffect(() => { if (open) updatePosition(); }, [open]);
  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open]);
  useEffect(() => () => clearClose(), []);
  const style: CSSProperties = { left: position.left, top: position.top };
  const popover = open && typeof document !== "undefined" ? createPortal(
    <div id={id} ref={popoverRef} className="required-field-popover" role="tooltip" style={style}
      onMouseEnter={show} onMouseLeave={hideSoon}>
      <strong>Required field missing</strong>
      <p>{issue.message}</p>
      <dl>
        <dt>Field</dt><dd>{issue.field || "Required accounting field"}</dd>
        <dt>Effect</dt><dd>Export remains blocked until a valid value is saved.</dd>
        <dt>Source</dt><dd>{issue.source}</dd>
      </dl>
    </div>, document.body,
  ) : null;
  return <span className="required-field-explanation" onMouseEnter={show} onMouseLeave={hideSoon}>
    <button ref={triggerRef} type="button" className="required-field-trigger"
      aria-label={`Explain missing ${issue.field || "required field"}`} aria-expanded={open}
      aria-describedby={open ? id : undefined} onFocus={show} onBlur={hideSoon}>!</button>
    {popover}
  </span>;
}
