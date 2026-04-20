import { cn } from "@opal/utils";
import Text from "@/refresh-components/texts/Text";
import React, {
  useState,
  ReactNode,
  useCallback,
  useMemo,
  memo,
} from "react";
import { SvgCheck, SvgCode, SvgCopy } from "@opal/icons";
import { renderMermaidSVG } from "beautiful-mermaid";

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

const MermaidRenderer = memo(function MermaidRenderer({
  code,
}: {
  code: string;
}) {
  const normalizedCode = useMemo(() => normalizeMermaidCode(code), [code]);
  const isStreamingFence = useMemo(() => isIncompleteMermaidBlock(code), [code]);

  const { svg, error } = useMemo(() => {
    if (!normalizedCode || isStreamingFence) {
      return { svg: null, error: null };
    }

    try {
      const renderedSvg = renderMermaidSVG(normalizedCode, {
        bg: "var(--background-tint-00)",
        fg: "var(--text-05)",
        accent: "var(--action-text-link-05)",
        muted: "var(--text-03)",
        border: "var(--border-02)",
        transparent: true,
        interactive: true,
      });

      return { svg: renderedSvg, error: null };
    } catch (err) {
      return {
        svg: null,
        error:
          err instanceof Error
            ? err.message
            : "Failed to render Mermaid diagram.",
      };
    }
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
      className="w-full min-w-0 overflow-hidden p-2 
      [&_svg]:h-auto 
      [&_svg]:max-w-full 
      [&_svg]:w-full 
      [&_svg_*]:[font-family:var(--font-hanken-grotesk)]"
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
    <div
      className="ml-auto cursor-pointer select-none"
      onMouseDown={handleCopy}
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
    </div>
  );

  if (typeof children === "string" && !language) {
    return (
      <span
        data-testid="code-block"
        className={cn(
          "font-mono",
          "text-text-05",
          "bg-background-tint-00",
          "rounded",
          "text-[0.75em]",
          "inline",
          "whitespace-pre-wrap",
          "break-words",
          "py-0.5",
          "px-1",
          className
        )}
      >
        {children}
      </span>
    );
  }

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
        <pre className="!p-2 m-0 overflow-x-auto w-0 min-w-full hljs">
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
            !noPadding && "px-1 pb-1"
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