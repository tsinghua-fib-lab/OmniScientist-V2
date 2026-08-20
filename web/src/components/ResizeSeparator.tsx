import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  createLiveResizeSession,
  type LiveResizeSession,
  type ResizeDirection,
} from "../resizeInteraction";

type ResizeAxis = "x" | "y";

type ResizeSeparatorProps = {
  axis: ResizeAxis;
  direction: ResizeDirection;
  className: string;
  label: string;
  controlsId: string;
  value: number;
  min: number;
  max: number;
  defaultValue: number;
  valueText: (value: number) => string;
  title: string;
  onPreview: (value: number) => void;
  onCommit: (value: number) => void;
  onDraggingChange?: (dragging: boolean) => void;
};

type ActivePointer = {
  pointerId: number;
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

export function ResizeSeparator({
  axis,
  direction,
  className,
  label,
  controlsId,
  value,
  min,
  max,
  defaultValue,
  valueText,
  title,
  onPreview,
  onCommit,
  onDraggingChange,
}: ResizeSeparatorProps) {
  const pointerRef = useRef<ActivePointer | null>(null);
  const sessionRef = useRef<LiveResizeSession | null>(null);
  const resizerRef = useRef<HTMLDivElement>(null);
  const onPreviewRef = useRef(onPreview);
  const onCommitRef = useRef(onCommit);
  const onDraggingChangeRef = useRef(onDraggingChange);
  const valueTextRef = useRef(valueText);
  const [dragging, setDragging] = useState(false);

  onPreviewRef.current = onPreview;
  onCommitRef.current = onCommit;
  onDraggingChangeRef.current = onDraggingChange;
  valueTextRef.current = valueText;

  const coordinate = (event: Pick<PointerEvent, "clientX" | "clientY">) =>
    axis === "x" ? event.clientX : event.clientY;

  const preview = (next: number) => {
    onPreviewRef.current(next);
    const rounded = Math.round(next);
    resizerRef.current?.setAttribute("aria-valuenow", String(rounded));
    resizerRef.current?.setAttribute("aria-valuetext", valueTextRef.current(rounded));
  };

  const finishDrag = (endCoordinate?: number, cancelled = false) => {
    const pointer = pointerRef.current;
    const session = sessionRef.current;
    if (!pointer || !session) return;
    pointerRef.current = null;
    sessionRef.current = null;
    const resizer = resizerRef.current;
    if (resizer?.hasPointerCapture(pointer.pointerId)) {
      resizer.releasePointerCapture(pointer.pointerId);
    }
    if (cancelled || endCoordinate == null) session.cancel();
    else session.finish(endCoordinate);
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
      sessionRef.current?.cancel();
      sessionRef.current = null;
      pointerRef.current = null;
      onDraggingChangeRef.current?.(false);
    },
    [],
  );

  const applyAndCommit = (next: number) => {
    const clamped = clamp(next, min, max);
    preview(clamped);
    onCommitRef.current(clamped);
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 24 : 8;
    let next: number | null = null;
    if (event.key === "Home") next = min;
    else if (event.key === "End") next = max;
    else if (axis === "x" && event.key === "ArrowLeft") next = value - step * direction;
    else if (axis === "x" && event.key === "ArrowRight") next = value + step * direction;
    else if (axis === "y" && event.key === "ArrowUp") next = value - step * direction;
    else if (axis === "y" && event.key === "ArrowDown") next = value + step * direction;
    if (next == null) return;
    event.preventDefault();
    applyAndCommit(next);
  };

  return (
    <div
      ref={resizerRef}
      className={`${className}${dragging ? " dragging" : ""}`}
      role="separator"
      aria-label={label}
      aria-controls={controlsId}
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={Math.round(value)}
      aria-valuetext={valueText(Math.round(value))}
      tabIndex={0}
      title={title}
      onKeyDown={onKeyDown}
      onDoubleClick={() => applyAndCommit(defaultValue)}
      onPointerDown={(event: ReactPointerEvent<HTMLDivElement>) => {
        if (event.button !== 0 || pointerRef.current) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        pointerRef.current = { pointerId: event.pointerId };
        sessionRef.current = createLiveResizeSession({
          startCoordinate: coordinate(event),
          startValue: value,
          min,
          max,
          direction,
          onPreview: preview,
          onCommit: (next) => onCommitRef.current(next),
        });
        setDragging(true);
        onDraggingChangeRef.current?.(true);
      }}
      onPointerMove={(event) => {
        if (pointerRef.current?.pointerId !== event.pointerId) return;
        sessionRef.current?.move(coordinate(event));
      }}
      onPointerUp={(event) => {
        if (pointerRef.current?.pointerId !== event.pointerId) return;
        finishDrag(coordinate(event));
      }}
      onPointerCancel={(event) => {
        if (pointerRef.current?.pointerId !== event.pointerId) return;
        finishDrag(undefined, true);
      }}
      onLostPointerCapture={(event) => {
        if (pointerRef.current?.pointerId !== event.pointerId) return;
        finishDrag(undefined, true);
      }}
    >
      <span aria-hidden="true" />
    </div>
  );
}
