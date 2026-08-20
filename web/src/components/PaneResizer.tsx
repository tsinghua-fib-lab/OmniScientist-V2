import { ResizeSeparator } from "./ResizeSeparator";

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
  return (
    <ResizeSeparator
      axis="x"
      direction={side === "left" ? 1 : -1}
      className={`pane-resizer ${side}`}
      label={label}
      controlsId={controlsId}
      value={value}
      min={min}
      max={max}
      defaultValue={defaultValue}
      valueText={(next) => `${next} 像素`}
      title="拖动或使用方向键调整宽度，双击恢复默认宽度"
      onPreview={onChange}
      onCommit={onCommit}
      onDraggingChange={onDraggingChange}
    />
  );
}
