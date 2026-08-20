import { Children, isValidElement, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { IconCheck, IconCopy, IconExternalLink } from "./icons";

const SERVER_BASE_URL = "http://localhost/";

function externalHttpUrl(value: string) {
  try {
    const base = new URL(typeof window === "undefined" ? SERVER_BASE_URL : window.location.href);
    const resolved = new URL(value, base);
    if (
      (resolved.protocol === "http:" || resolved.protocol === "https:") &&
      resolved.origin !== base.origin
    ) {
      return resolved;
    }
  } catch {
    // Invalid URLs are left to react-markdown's safe URL transform.
  }
  return null;
}

function CodeBlock({ language, text }: { language: string; text: string }) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  return (
    <div className="md-code">
      <div className="md-code-bar">
        <span className="md-code-language">{language || "text"}</span>
        <button
          type="button"
          className="md-copy"
          aria-label={copied ? "代码已复制" : "复制代码"}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(text);
              setCopyFailed(false);
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1200);
            } catch {
              setCopyFailed(true);
            }
          }}
        >
          {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
          <span>{copyFailed ? "复制失败" : copied ? "已复制" : "复制"}</span>
        </button>
      </div>
      <pre tabIndex={0}>
        <code>{text}</code>
      </pre>
      <span className="sr-only" aria-live="polite">
        {copyFailed ? "代码复制失败" : copied ? "代码已复制" : ""}
      </span>
    </div>
  );
}

function PreformattedCode({ children }: { children?: ReactNode }) {
  const child = Children.toArray(children)[0];
  if (!isValidElement<{ className?: string; children?: ReactNode }>(child)) {
    return <pre>{children}</pre>;
  }
  const text = String(child.props.children ?? "").replace(/\n$/, "");
  const language = /language-([\w-]+)/.exec(child.props.className || "")?.[1] || "";
  return <CodeBlock language={language} text={text} />;
}

function MarkdownImage({ src = "", alt = "", title }: { src?: string; alt?: string; title?: string }) {
  const remote = externalHttpUrl(src);
  const [remoteAllowed, setRemoteAllowed] = useState(false);
  if (remote && !remoteAllowed) {
    return (
      <button type="button" className="md-remote-image" onClick={() => setRemoteAllowed(true)}>
        <IconExternalLink size={17} />
        <span>
          <strong>远程图片已暂停</strong>
          <small>{remote.host}</small>
        </span>
        <b>加载图片</b>
      </button>
    );
  }
  return (
    <img
      src={src}
      alt={alt}
      title={title}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
    />
  );
}

export function MarkdownBody({ source, streaming = false }: { source: string; streaming?: boolean }) {
  if (!source.trim()) return null;
  return (
    <div className={`md${streaming ? " is-streaming" : ""}`}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          pre({ node: _node, children }) {
            return <PreformattedCode>{children}</PreformattedCode>;
          },
          a({ node: _node, href, children }) {
            const external = Boolean(externalHttpUrl(href || ""));
            return (
              <a
                href={href}
                target={external ? "_blank" : undefined}
                rel={external ? "noopener noreferrer" : undefined}
              >
                {children}
              </a>
            );
          },
          table({ node: _node, children }) {
            return (
              <div className="md-table" role="region" aria-label="可横向滚动的表格" tabIndex={0}>
                <table>{children}</table>
              </div>
            );
          },
          img({ node: _node, src, alt, title }) {
            return <MarkdownImage src={src} alt={alt || ""} title={title} />;
          },
        }}
      >
        {source}
      </Markdown>
    </div>
  );
}
