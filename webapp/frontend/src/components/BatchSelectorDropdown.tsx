import {
  useCallback,
  useEffect,
  useDeferredValue,
  useLayoutEffect,
  memo,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import { BatchExplorer, type BatchExplorerProps } from "./BatchExplorer";

type BatchSelectorDropdownProps = BatchExplorerProps & {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  compact?: boolean;
  variant?: "default" | "breadcrumb";
};

type PopoverPosition = {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
};

const BATCH_ROW_HEIGHT = 46;
const CREATE_BATCH_ROW_HEIGHT = 46;
const BATCH_WINDOW_OVERSCAN = 8;
const BATCH_WINDOW_THRESHOLD = 260;

function cssAttrValue(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function formatBatchMeta(filesCount: number, invoiceCount: number, status: string): string {
  const fileLabel = filesCount === 1 ? "file" : "files";
  const invoiceLabel = invoiceCount === 1 ? "inv" : "inv";
  return `${filesCount} ${fileLabel} - ${invoiceCount} ${invoiceLabel} - ${status || "Idle"}`;
}

function useBatchPopoverPosition(
  open: boolean,
  triggerRef: RefObject<HTMLButtonElement | null>,
  compact = false,
): PopoverPosition {
  const [position, setPosition] = useState<PopoverPosition>({
    top: 112,
    left: 128,
    width: 440,
    maxHeight: 640,
  });

  useLayoutEffect(() => {
    if (!open) return;

    const update = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const ownerWindow = triggerRef.current?.ownerDocument.defaultView ?? window;
      const margin = 14;
      const minWidth = compact ? 330 : 390;
      const maxWidth = compact ? 390 : 480;
      const preferredWidth = Math.min(
        maxWidth,
        Math.max(minWidth, ownerWindow.innerWidth - margin * 2),
      );
      const minHeight = compact ? 320 : 360;
      const maxHeight = Math.max(
        minHeight,
        ownerWindow.innerHeight - rect.bottom - margin,
      );
      const left = Math.min(
        Math.max(margin, rect.left),
        Math.max(margin, ownerWindow.innerWidth - preferredWidth - margin),
      );
      setPosition({
        top: Math.round(rect.bottom + 8),
        left: Math.round(left),
        width: preferredWidth,
        maxHeight: Math.round(maxHeight),
      });
    };

    update();
    const ownerWindow = triggerRef.current?.ownerDocument.defaultView ?? window;
    ownerWindow.addEventListener("resize", update);
    ownerWindow.addEventListener("scroll", update, true);
    return () => {
      ownerWindow.removeEventListener("resize", update);
      ownerWindow.removeEventListener("scroll", update, true);
    };
  }, [compact, open, triggerRef]);

  return position;
}

function BatchSelectorDropdownImpl({
  open,
  onOpenChange,
  compact,
  variant = "default",
  batchList,
  activeBatchId,
  onSwitchBatch,
  onCreateBatch,
  onSelectFile,
  onSelectPage,
  ...explorerProps
}: BatchSelectorDropdownProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const popoverBodyRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const position = useBatchPopoverPosition(open, triggerRef, compact);
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [scrollFrame, setScrollFrame] = useState({ top: 0, height: 0 });

  const activeBatch = useMemo(
    () => batchList.find((batch) => batch.batch_id === activeBatchId) ?? null,
    [activeBatchId, batchList],
  );

  const filteredBatchList = useMemo(() => {
    const normalized = deferredQuery.trim().toLowerCase();
    if (!normalized) return batchList;
    return batchList.filter((batch) => {
      const haystack = [
        batch.batch_name,
        batch.batch_id,
        batch.status,
        String(batch.files_count ?? ""),
        String(batch.invoices_count ?? ""),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalized);
    });
  }, [batchList, deferredQuery]);

  useLayoutEffect(() => {
    if (!open) return;
    const node = popoverBodyRef.current;
    if (!node) return;

    let raf = 0;
    const read = () => {
      raf = 0;
      setScrollFrame({
        top: node.scrollTop,
        height: node.clientHeight,
      });
    };
    const scheduleRead = () => {
      if (raf) return;
      raf = window.requestAnimationFrame(read);
    };

    read();
    node.addEventListener("scroll", scheduleRead, { passive: true });
    const resizeObserver =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(scheduleRead) : null;
    resizeObserver?.observe(node);

    return () => {
      node.removeEventListener("scroll", scheduleRead);
      resizeObserver?.disconnect();
      if (raf) window.cancelAnimationFrame(raf);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const node = popoverBodyRef.current;
    if (!node) return;
    node.scrollTop = 0;
    setScrollFrame((current) => ({ ...current, top: 0 }));
  }, [deferredQuery, open]);

  const virtualWindow = useMemo(() => {
    if (filteredBatchList.length < BATCH_WINDOW_THRESHOLD) return undefined;
    const viewportHeight =
      scrollFrame.height || Math.max(260, position.maxHeight - 112);
    const listScrollTop = Math.max(0, scrollFrame.top - CREATE_BATCH_ROW_HEIGHT);
    const start = Math.max(
      0,
      Math.floor(listScrollTop / BATCH_ROW_HEIGHT) - BATCH_WINDOW_OVERSCAN,
    );
    const visibleCount =
      Math.ceil((viewportHeight + CREATE_BATCH_ROW_HEIGHT) / BATCH_ROW_HEIGHT) +
      BATCH_WINDOW_OVERSCAN * 2;
    const end = Math.min(filteredBatchList.length, start + visibleCount);
    return {
      start,
      end,
      beforeHeight: start * BATCH_ROW_HEIGHT,
      afterHeight: (filteredBatchList.length - end) * BATCH_ROW_HEIGHT,
    };
  }, [filteredBatchList.length, position.maxHeight, scrollFrame.height, scrollFrame.top]);

  const handleSwitchBatch = useCallback(
    async (targetBatchId: string) => {
      await onSwitchBatch(targetBatchId);
      onOpenChange(false);
    },
    [onOpenChange, onSwitchBatch],
  );

  const handleSelectFile = useCallback(
    async (targetBatchId: string, filename: string) => {
      await onSelectFile(targetBatchId, filename);
      onOpenChange(false);
    },
    [onOpenChange, onSelectFile],
  );

  const handleSelectPage = useCallback(
    async (targetBatchId: string, filename: string, pageNumber: number) => {
      await onSelectPage(targetBatchId, filename, pageNumber);
      onOpenChange(false);
    },
    [onOpenChange, onSelectPage],
  );

  useEffect(() => {
    if (!open) return;
    const ownerWindow = triggerRef.current?.ownerDocument.defaultView ?? window;
    const id = ownerWindow.setTimeout(() => searchRef.current?.focus(), 40);
    return () => ownerWindow.clearTimeout(id);
  }, [open]);

  useLayoutEffect(() => {
    if (!open || !activeBatchId || query.trim()) return;
    const body = popoverBodyRef.current;
    if (!body) return;
    const activeIndex = filteredBatchList.findIndex(
      (batch) => batch.batch_id === activeBatchId,
    );
    if (activeIndex < 0) return;
    const targetTop = CREATE_BATCH_ROW_HEIGHT + activeIndex * BATCH_ROW_HEIGHT;
    body.scrollTop = Math.max(0, targetTop - BATCH_ROW_HEIGHT * 2);
    setScrollFrame({ top: body.scrollTop, height: body.clientHeight });
  }, [activeBatchId, filteredBatchList, open, query]);

  useEffect(() => {
    if (!open) return;
    const ownerDocument = triggerRef.current?.ownerDocument ?? document;
    const ownerWindow = ownerDocument.defaultView ?? window;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (
        target instanceof ownerWindow.Element &&
        (target.closest(".kebab-menu-popover") || target.closest(".modal-backdrop"))
      ) {
        return;
      }
      if (triggerRef.current?.contains(target)) return;
      if (popoverRef.current?.contains(target)) return;
      onOpenChange(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onOpenChange(false);
    };
    ownerDocument.addEventListener("pointerdown", onPointerDown);
    ownerDocument.addEventListener("keydown", onKeyDown);
    return () => {
      ownerDocument.removeEventListener("pointerdown", onPointerDown);
      ownerDocument.removeEventListener("keydown", onKeyDown);
    };
  }, [onOpenChange, open]);

  const triggerTitle = activeBatch
    ? `Batch: ${activeBatch.batch_name}`
    : "Open batch selector";
  const triggerName = activeBatch?.batch_name || "Select batch";
  const triggerMeta = activeBatch
    ? formatBatchMeta(
        activeBatch.files_count ?? 0,
        activeBatch.invoices_count ?? 0,
        activeBatch.status || "Idle",
      )
    : `${batchList.length} batches`;
  const isBreadcrumb = variant === "breadcrumb";

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`template-batch-selector-trigger ${compact ? "is-compact" : ""} ${
          isBreadcrumb ? "is-breadcrumb" : ""
        } ${
          open ? "is-open" : ""
        }`}
        title={triggerTitle}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => onOpenChange(!open)}
        data-testid="template-batch-selector"
      >
        {!isBreadcrumb && <span className="template-batch-selector-kicker">Batch</span>}
        <span className="template-batch-selector-name">{triggerName}</span>
        {!isBreadcrumb && (
          <span className="template-batch-selector-meta">{triggerMeta}</span>
        )}
        {!isBreadcrumb && (
          <span className="template-batch-selector-chevron" aria-hidden>
            v
          </span>
        )}
      </button>

      {open &&
        createPortal(
          <div
            ref={popoverRef}
            className={`batch-selector-popover ${compact ? "is-compact" : ""}`}
            style={{
              top: position.top,
              left: position.left,
              width: position.width,
              maxHeight: position.maxHeight,
            }}
            role="dialog"
            aria-label="Batch selector"
            data-testid="batch-selector-popover"
          >
            <label className="batch-selector-search-label">
              <span className="sr-only">Search batches</span>
              <input
                ref={searchRef}
                className="batch-selector-search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search batches or status..."
              />
            </label>
            <div className="batch-selector-popover-body" ref={popoverBodyRef}>
              <BatchExplorer
                {...explorerProps}
                batchList={filteredBatchList}
                virtualWindow={virtualWindow}
                activeBatchId={activeBatchId}
                onSwitchBatch={handleSwitchBatch}
                onCreateBatch={onCreateBatch}
                onSelectFile={handleSelectFile}
                onSelectPage={handleSelectPage}
                showPages={false}
              />
            </div>
          </div>,
          triggerRef.current?.ownerDocument.body ?? document.body,
        )}
    </>
  );
}

export const BatchSelectorDropdown = memo(BatchSelectorDropdownImpl);
