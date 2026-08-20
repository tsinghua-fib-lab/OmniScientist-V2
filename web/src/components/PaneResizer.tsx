import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { clampPaneWidth } from "../paneLayout";

type PaneResizerProps = {
  side: "left" | "right";
  label: string;
  controlsId: string;
  value: number;
  min: number;
  max: number;
  defaultValue: number;
  onChange: (value: number) => void;
  onCommit: (value: number) => void;
  onDraggingChange?: (dragging: boolean) => void;
};

type Drag = {
  pointerId: number;
  startX: number;
  startValue: number;
};

export function PaneResizer({
  side,
  label,
  controlsId,
  value,
  min,
  max,
  defaultValue,
  onChange,
  onCommit,
  onDraggingChange,
}: PaneResizerProps) {
  const dragRef = useRef<Drag | null>(null);
  const resizerRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<number | null>(null);
  const pendingXRef = useRef<number | null>(null);
  const onChangeRef = useRef(onChange);
  const onDraggingChangeRef = useRef(onDraggingChange);
  const [dragging, setDragging] = useState(false);

  onChangeRef.current = onChange;
  onDraggingChangeRef.current = onDraggingChange;

  const valueAt = (clientX: number) => {
    const drag = dragRef.current;
    if (!drag) return value;
    const delta = clientX - drag.startX;
    const directedDelta = side === "left" ? delta : -delta;
    return clampPaneWidth(drag.startValue + directedDelta, min, max);
  };

  const applyPending = () => {
    frameRef.current = null;
    if (pendingXRef.current == null) return;
    onChangeRef.current(valueAt(pendingXRef.current));
    pendingXRef.current = null;
  };

  const scheduleChange = (clientX: number) => {
    pendingXRef.current = clientX;
    if (frameRef.current == null) {
      frameRef.current = window.requestAnimationFrame(applyPending);
    }
  };

  const finishDrag = (clientX?: number, cancelled = false) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current);
    frameRef.current = null;
    pendingXRef.current = null;
    const next = cancelled || clientX == null ? drag.startValue : valueAt(clientX);
    dragRef.current = null;
    const resizer = resizerRef.current;
    if (resizer?.hasPointerCapture(drag.pointerId)) {
      resizer.releasePointerCapture(drag.pointerId);
    }
    onChangeRef.current(next);
    if (!cancelled) onCommit(next);
    setDragging(false);
    onDraggingChangeRef.current?.(false);
  };

  useEffect(() => {
    if (!dragging) return;
    const cancelOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      finishDrag(undefined, true);
    };
    window.addEventListener("keydown", cancelOnEscape);
    return () => window.removeEventListener("keydown", cancelOnEscape);
  }, [dragging]);

  useEffect(
    () => () => {
      if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current);
      onDraggingChangeRef.current?.(false);
    },
    [],
  );

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 24 : 8;
    let next: number | null = null;
    if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    else if (event.key === "ArrowLeft") next = value + (side === "left" ? -step : step);
    else if (event.key === "ArrowRight") next = value + (side === "left" ? step : -step);
    if (next == null) return;
    event.preventDefault();
    const clamped = clampPaneWidth(next, min, max);
    onChange(clamped);
    onCommit(clamped);
  };

  return (
    <div
      ref={resizerRef}
      className={`pane-resizer ${side}${dragging ? " dragging" : ""}`}
      role="separator"
      aria-label={label}
      aria-controls={controlsId}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={Math.round(value)}
      aria-valuetext={`${Math.round(value)} 像素`}
      tabIndex={0}
      title="拖动或使用方向键调整宽度，双击恢复默认宽度"
      onKeyDown={onKeyDown}
      onDoubleClick={() => {
        const next = clampPaneWidth(defaultValue, min, max);
        onChange(next);
        onCommit(next);
      }}
      onPointerDown={(event: ReactPointerEvent<HTMLDivElement>) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        dragRef.current = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startValue: value,
        };
        setDragging(true);
        onDraggingChange?.(true);
      }}
      onPointerMove={(event) => {
        if (dragRef.current?.pointerId !== event.pointerId) return;
        scheduleChange(event.clientX);
      }}
      onPointerUp={(event) => {
        if (dragRef.current?.pointerId !== event.pointerId) return;
        finishDrag(event.clientX);
      }}
      onPointerCancel={(event) => {
        if (dragRef.current?.pointerId !== event.pointerId) return;
        finishDrag(undefined, true);
      }}
      onLostPointerCapture={(event) => {
        if (dragRef.current?.pointerId !== event.pointerId) return;
        finishDrag(undefined, true);
      }}
    >
      <span aria-hidden="true" />
    </div>
  );
}
