export type ResizeDirection = 1 | -1;

type LiveResizeSessionOptions = {
  startCoordinate: number;
  startValue: number;
  min: number;
  max: number;
  direction: ResizeDirection;
  onPreview: (value: number) => void;
  onCommit: (value: number) => void;
};

export type LiveResizeSession = {
  move: (coordinate: number) => number | null;
  finish: (coordinate: number) => number | null;
  cancel: () => number | null;
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

export function createLiveResizeSession({
  startCoordinate,
  startValue,
  min,
  max,
  direction,
  onPreview,
  onCommit,
}: LiveResizeSessionOptions): LiveResizeSession {
  let active = true;
  const valueAt = (coordinate: number) =>
    clamp(startValue + (coordinate - startCoordinate) * direction, min, max);

  return {
    move(coordinate) {
      if (!active) return null;
      const next = valueAt(coordinate);
      onPreview(next);
      return next;
    },
    finish(coordinate) {
      if (!active) return null;
      const next = valueAt(coordinate);
      onPreview(next);
      active = false;
      onCommit(next);
      return next;
    },
    cancel() {
      if (!active) return null;
      active = false;
      onPreview(startValue);
      return startValue;
    },
  };
}
