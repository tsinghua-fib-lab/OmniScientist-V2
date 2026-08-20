import { ResizeSeparator } from "./ResizeSeparator";

type SidebarSplitResizerProps = {
  controlsId: string;
  value: number;
  min: number;
  max: number;
  defaultValue: number;
  onChange: (value: number) => void;
  onCommit: (value: number) => void;
  onDraggingChange?: (dragging: boolean) => void;
};

export function SidebarSplitResizer({
  controlsId,
  value,
  min,
  max,
  defaultValue,
  onChange,
  onCommit,
  onDraggingChange,
}: SidebarSplitResizerProps) {
  return (
    <ResizeSeparator
      axis="y"
      direction={1}
      className="sidebar-split-resizer"
      label="调整工作区与会话区域高度"
      controlsId={controlsId}
      value={value}
      min={min}
      max={max}
      defaultValue={defaultValue}
      valueText={(next) => `工作区高度 ${next} 像素`}
      title="上下拖动或使用方向键调整高度，双击恢复默认高度"
      onPreview={onChange}
      onCommit={onCommit}
      onDraggingChange={onDraggingChange}
    />
  );
}
