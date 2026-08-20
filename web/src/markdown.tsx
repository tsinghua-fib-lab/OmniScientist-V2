import { lazy, Suspense } from "react";

const MarkdownRich = lazy(async () => {
  const module = await import("./markdown-rich");
  return { default: module.MarkdownBody };
});

export function MarkdownBody({ source, streaming = false }: { source: string; streaming?: boolean }) {
  if (!source.trim()) return null;
  return (
    <Suspense
      fallback={
        <div className={`md${streaming ? " is-streaming" : ""}`}>
          <p>{source}</p>
        </div>
      }
    >
      <MarkdownRich source={source} streaming={streaming} />
    </Suspense>
  );
}
