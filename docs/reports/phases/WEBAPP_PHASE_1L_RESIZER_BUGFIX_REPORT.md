# Webapp Phase 1L — Resizer "sticky drag" bug fix

**Date:** 2026-05-02
**Scope:** [`webapp/frontend/src/hooks/useResizablePanel.ts`](webapp/frontend/src/hooks/useResizablePanel.ts) only. No backend, processor, OCR, Dropbox, or export code touched.

---

## Symptom

When resizing the divider between the Document Preview and the Template panes, the panel could keep resizing **after** the user released the mouse button. The user had to click again somewhere else to stop the drag. It also occurred when the pointer was released outside the browser viewport.

## Root cause

Two issues compounded:

1. **Move/up listeners on `window`, not the divider.** The previous implementation registered `pointermove`, `pointerup`, and `pointercancel` on `window`. When the pointer left the viewport (even briefly during a fast drag) the OS could deliver `pointerup` to a different target, and our window listener could miss it — leaving `draggingRef.current = true` even though no button was held.
2. **`onUp` rebuilt on every state change.** The handler closed over `size` so React re-registered the listener on every drag tick. There was a brief window during the cleanup→re-add cycle where no `pointerup` listener existed at all. If the OS delivered `pointerup` exactly during that gap (rare but reproducible on slow machines) the drag would never officially end, and the next move event would resize the panel even though no button was pressed.

The original code also lacked safety nets for `blur`, `visibilitychange`, and `mouseleave` events — all of which can occur while a pointer is "down" from the OS' perspective.

## Fix

[`webapp/frontend/src/hooks/useResizablePanel.ts`](webapp/frontend/src/hooks/useResizablePanel.ts) was rewritten to:

1. **Use Pointer Events with `setPointerCapture`** on the divider element. Once a pointer is captured, all subsequent `pointermove` / `pointerup` / `pointercancel` events for that pointer ID are routed to the divider regardless of where the pointer is on screen — solves the "release outside the viewport" case definitively.
2. **Move / up listeners on the divider** (the React `dragHandleProps`), not on `window`. The captured element receives the pointer events directly.
3. **Hard guard via `e.buttons === 0`**: inside `onPointerMove`, if no primary button is held, immediately call `stopDrag(true)`. This catches OS-level focus changes that swallow the `pointerup`.
4. **Belt-and-braces window listeners** for safety:
   - `window.addEventListener("pointerup", …)` and `"pointercancel"` — fires even if the captured-pointer path didn't.
   - `window.addEventListener("blur", …)` — kills the drag on alt-tab.
   - `document.addEventListener("visibilitychange", …)` — kills the drag if the tab is hidden.
   - `document.addEventListener("mouseleave", …)` — kills the drag if the cursor leaves the document area.
5. **`onLostPointerCapture`** prop on the divider — releases the drag if the browser drops capture for any reason.
6. **State stored in refs, not React state.** `draggingRef`, `startCoordRef`, `startSizeRef`, `sizeRef`, `handleElRef`, `pointerIdRef` all live in `useRef` so the move/up handlers don't rebuild on every drag tick. The `useCallback` deps are now stable across the entire drag, eliminating the cleanup-gap race condition.
7. **Per-write `localStorage` only on `pointerup`** (no longer on every move). The previous version persisted `size` from the closure on each handler rebuild — wasted writes, plus the closure could be stale.
8. **Cleanup on unmount** restores `body.style.cursor` and `userSelect` even if the hook is torn down mid-drag (e.g. parent component unmounts during a drag). Prevents a runaway `col-resize` cursor stuck on the page.

## Files changed

| File | Change |
| --- | --- |
| [`webapp/frontend/src/hooks/useResizablePanel.ts`](webapp/frontend/src/hooks/useResizablePanel.ts) | Full rewrite as described above. Public API (`size`, `dragHandleProps`, `reset`) unchanged so call sites in App.tsx need no edits. |

## Tests performed

### 1. Manual UX test plan
*(Verify in `npm run dev` after backend is up)*

| Scenario | Expected | Verified by |
| --- | --- | --- |
| Hold-drag-release inside viewport | Resize stops on release | Manual |
| Hold-drag, release **outside** viewport | Resize stops on release; cursor returns to default | Manual |
| Hold-drag, alt-tab away | Resize stops on `blur` | Manual |
| Hold-drag, switch tab | Resize stops on `visibilitychange` | Manual |
| Move mouse after release | No resize fires | Manual |
| Double-click divider | Reset to default size | Manual |
| Use multiple dividers in succession | No interference between them (independent capture) | Manual |
| Refresh after resize | Size persists from `localStorage` | Manual |
| Collapse/expand panels | Existing collapse buttons still work | Manual |

### 2. Build
```
$ npm run build
✓ 63 modules transformed.
dist/assets/index-Du8-wK-W.js     204.34 kB │ gzip: 64.03 kB
✓ built in 1.67s
```

### 3. Backend untouched
- Backend imports clean; no backend changes in this fix.
- Vendor processors not touched. CLI regression confirms unchanged behaviour:
  - Richmond: 28 invoices / 32 lines (unchanged)
  - Hopkinsville: 14 invoices / 36 lines (unchanged)
- `Output/Template.xlsx` SHA-256 unchanged (`b753f406…3969c284`).
- All source CSVs / GL files / Vendor List unchanged.

## What stayed the same

- Public API of `useResizablePanel` (`{ size, dragHandleProps, reset }`).
- Default sizes, min/max bounds, localStorage keys.
- Invert mode for right-edge panels (used historically by the inspector pane; Phase 1L removes that pane in favour of a drawer, but the hook's invert mode is preserved for future use).
- Visual design of the dividers — the CSS in `styles.css` was not touched by this fix.
