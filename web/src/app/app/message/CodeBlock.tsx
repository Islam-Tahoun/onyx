import { cn } from "@opal/utils";
import Text from "@/refresh-components/texts/Text";
import React, {
  useState,
  useEffect,
  ReactNode,
  useCallback,
  useMemo,
  memo,
} from "react";
import { SvgCheck, SvgCode, SvgCopy } from "@opal/icons";

interface CodeBlockProps {
  className?: string;
  children?: ReactNode;
  codeText: string;
  showHeader?: boolean;
  noPadding?: boolean;
}

const MemoizedCodeLine = memo(({ content }: { content: ReactNode }) => (
  <>{content}</>
));

function normalizeMermaidCode(input: string) {
  const trimmed = input.trim();

  return trimmed
    .replace(/^```mermaid\s*\n?/i, "")
    .replace(/^```\s*\n?/i, "")
    .replace(/\n?```\s*$/i, "")
    .trim();
}

function isIncompleteMermaidBlock(input: string) {
  const trimmed = input.trim();

  return /^```mermaid\s*$/i.test(trimmed);
}

function createMermaidRenderId() {
  if (globalThis.crypto?.randomUUID) {
    return `mermaid-${globalThis.crypto.randomUUID()}`;
  }

  return `mermaid-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

const MermaidRenderer = memo(function MermaidRenderer({
  code,
}: {
  code: string;
}) {
  const normalizedCode = useMemo(() => normalizeMermaidCode(code), [code]);
  const isStreamingFence = useMemo(
    () => isIncompleteMermaidBlock(code),
    [code]
  );
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    if (!normalizedCode || isStreamingFence) {
      setSvg(null);
      setError(null);
      return;
    }

    setSvg(null);
    setError(null);

    async function renderDiagram() {
      try {
        const { default: mermaid } = await import("mermaid");

        if (cancelled) return;

        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: "base",
          flowchart: {
            htmlLabels: true,
            useMaxWidth: true,
          },
        });

        const result = await mermaid.render(
          createMermaidRenderId(),
          normalizedCode
        );

        if (!cancelled) {
          setSvg(result.svg);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setSvg(null);
          setError(
            err instanceof Error
              ? err.message
              : "Failed to render Mermaid diagram."
          );
        }
      }
    }

    void renderDiagram();

    return () => {
      cancelled = true;
    };
  }, [normalizedCode, isStreamingFence]);

  if (error) {
    return (
      <div className="p-3 text-sm text-red-500 font-mono whitespace-pre-wrap break-words">
        {error}
      </div>
    );
  }

  if (!svg) return null;

  return (
    <div
      data-testid="mermaid-diagram"
      dir="rtl"
      className={cn(
        "w-full min-w-0 overflow-x-auto p-2",
        "[&_svg]:h-auto [&_svg]:max-w-full [&_svg]:w-full",
        "[&_svg_*]:[font-family:var(--font-ibm-plex-sans-arabic),Arial,sans-serif]"
      )}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
});

export const CodeBlock = memo(function CodeBlock({
  className = "",
  children,
  codeText,
  showHeader = true,
  noPadding = false,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const language = useMemo(() => {
    return className
      .split(" ")
      .filter((cls) => cls.startsWith("language-"))
      .map((cls) => cls.replace("language-", ""))
      .join(" ");
  }, [className]);

  const handleCopy = useCallback(() => {
    if (!codeText) return;

    navigator.clipboard.writeText(codeText).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [codeText]);

  const CopyButton = () => (
    <button
      type="button"
      className="ml-auto cursor-pointer select-none"
      onClick={handleCopy}
    >
      {copied ? (
        <div className="flex items-center space-x-2">
          <SvgCheck height={14} width={14} stroke="currentColor" />
          <Text as="p" secondaryMono>
            Copied!
          </Text>
        </div>
      ) : (
        <div className="flex items-center space-x-2">
          <SvgCopy height={14} width={14} stroke="currentColor" />
          <Text as="p" secondaryMono>
            Copy
          </Text>
        </div>
      )}
    </button>
  );

  if (typeof children === "string" && !language) {
    return (
      <span
        data-testid="code-block"
        className={cn(
          "font-mono",
          "text-text-05",
          "bg-background-tint-00",
          "rounded-sm",
          "text-[0.75em]",
          "inline",
          "whitespace-pre-wrap",
          "wrap-break-word",
          "py-0.5",
          "px-1",
          className
        )}
      >
        {children}
      </span>
    );
  }

  // Concentric with the wrapper: inner radius = outer radius - the gap between
  // the two boxes. Both come from vars the wrapper sets, so they stay in sync.
  const innerRounding =
    "rounded-[calc(var(--code-block-radius,0px)-var(--code-block-gap,0px))]!";

  const CodeContent = () => {
    if (language === "mermaid") {
      return (
        <div className="!p-2 m-0 w-full min-w-0 overflow-hidden">
          <MermaidRenderer code={codeText} />
        </div>
      );
    }

    if (!language) {
      return (
        <pre
          className={cn(
            "p-2! m-0 overflow-x-auto w-0 min-w-full hljs",
            innerRounding
          )}
        >
          <code className={`text-sm hljs ${className}`}>
            {Array.isArray(children)
              ? children.map((child, index) => (
                  <MemoizedCodeLine key={index} content={child} />
                ))
              : children}
          </code>
        </pre>
      );
    }

    return (
      <pre className="!p-2 m-0 overflow-x-auto w-0 min-w-full hljs">
        <code className={`text-xs hljs ${className}`}>
          {Array.isArray(children)
            ? children.map((child, index) => (
                <MemoizedCodeLine key={index} content={child} />
              ))
            : children}
        </code>
      </pre>
    );
  };

  return (
    <>
      {showHeader ? (
        <div
          className={cn(
            "bg-background-tint-00 rounded-12 max-w-full min-w-0",
            "[--code-block-radius:var(--radius-12)]",
            noPadding
              ? "[--code-block-gap:0px]"
              : "px-1 pb-1 [--code-block-gap:0.25rem]"
          )}
        >
          {language && (
            <div className="flex items-center px-2 py-1 text-sm text-text-04 gap-x-2 force-ltr">
              <SvgCode
                height={12}
                width={12}
                stroke="currentColor"
                className="my-auto"
              />
              <Text secondaryMono>{language}</Text>
              {codeText && <CopyButton />}
            </div>
          )}
          <CodeContent />
        </div>
      ) : (
        <CodeContent />
      )}
    </>
  );
});

CodeBlock.displayName = "CodeBlock";
MemoizedCodeLine.displayName = "MemoizedCodeLine";
MermaidRenderer.displayName = "MermaidRenderer";
