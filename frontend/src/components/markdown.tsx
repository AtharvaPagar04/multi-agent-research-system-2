import { Fragment, type ReactNode } from "react";

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern =
    /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|⚠️[^*]*)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    if (token.startsWith("**")) {
      nodes.push(
        <strong key={key++} className="font-medium text-zinc-100">
          {token.slice(2, -2)}
        </strong>,
      );
    } else if (token.startsWith("`")) {
      nodes.push(
        <code
          key={key++}
          className="rounded border border-zinc-800 bg-zinc-950/80 px-1.5 py-0.5 font-mono text-[0.9em] text-zinc-300"
        >
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("[")) {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      if (linkMatch) {
        nodes.push(
          <a
            key={key++}
            href={linkMatch[2]}
            target="_blank"
            rel="noreferrer"
            className="text-zinc-300 underline decoration-zinc-700 underline-offset-2 hover:text-zinc-100"
          >
            {linkMatch[1]}
          </a>,
        );
      } else {
        nodes.push(token);
      }
    } else if (token.startsWith("⚠️") || token.includes("Unverified claim") || token.includes("Contradicted by")) {
      nodes.push(
        <span
          key={key++}
          className="rounded border border-amber-900/50 bg-amber-950/20 px-1.5 py-0.5 text-[0.92em] text-amber-200/90"
        >
          {token}
        </span>,
      );
    } else if (token.startsWith("*")) {
      nodes.push(
        <em key={key++} className="italic text-zinc-300">
          {token.slice(1, -1)}
        </em>,
      );
    }
    lastIndex = match.index + token.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

function isTableRow(line: string) {
  return line.trim().startsWith("|") && line.trim().endsWith("|");
}

function splitTableRow(line: string) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isSeparatorRow(line: string) {
  return splitTableRow(line).every((cell) => /^:?-{3,}:?$/.test(cell));
}

export function Markdown({ source }: { source: string }) {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let listOrdered = false;
  let tableRows: string[][] = [];
  let key = 0;

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    const text = paragraph.join(" ").trim();
    paragraph = [];
    if (!text) return;
    if (/^\*Evidence store run ID:/.test(text) || /^Report generated on/.test(text) || text.startsWith("*")) {
      blocks.push(
        <p key={key++} className="font-mono text-[12px] leading-relaxed text-zinc-500">
          {renderInline(text.replace(/^\*+|\*+$/g, ""))}
        </p>,
      );
      return;
    }
    blocks.push(
      <p key={key++} className="text-[14px] leading-7 text-zinc-300">
        {renderInline(text)}
      </p>,
    );
  };

  const flushList = () => {
    if (listItems.length === 0) return;
    const items = listItems;
    const ordered = listOrdered;
    listItems = [];
    blocks.push(
      ordered ? (
        <ol key={key++} className="list-decimal space-y-2 pl-5 text-[14px] leading-7 text-zinc-300">
          {items.map((item, index) => (
            <li key={index}>{renderInline(item)}</li>
          ))}
        </ol>
      ) : (
        <ul key={key++} className="list-disc space-y-2 pl-5 text-[14px] leading-7 text-zinc-300">
          {items.map((item, index) => (
            <li key={index}>{renderInline(item)}</li>
          ))}
        </ul>
      ),
    );
  };

  const flushTable = () => {
    if (tableRows.length === 0) return;
    const rows = tableRows;
    tableRows = [];
    const [header, ...body] = rows;
    blocks.push(
      <div key={key++} className="overflow-x-auto rounded-md border border-zinc-800">
        <table className="min-w-full border-collapse text-left text-[13px]">
          <thead className="bg-zinc-950/70">
            <tr>
              {header.map((cell) => (
                <th
                  key={cell}
                  className="border-b border-zinc-800 px-3 py-2 font-mono text-[11px] uppercase tracking-wide text-zinc-500"
                >
                  {cell || "—"}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-b border-zinc-900/80 last:border-b-0">
                {row.map((cell, cellIndex) => (
                  <td key={cellIndex} className="px-3 py-2 align-top text-zinc-300">
                    {renderInline(cell || "Unavailable")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    );
  };

  for (const line of lines) {
    const trimmed = line.trim();

    if (!trimmed) {
      flushParagraph();
      flushList();
      flushTable();
      continue;
    }

    if (trimmed.startsWith("```")) {
      flushParagraph();
      flushList();
      flushTable();
      continue;
    }

    if (isTableRow(trimmed)) {
      flushParagraph();
      flushList();
      if (!isSeparatorRow(trimmed)) {
        tableRows.push(splitTableRow(trimmed));
      }
      continue;
    }

    if (tableRows.length > 0) {
      flushTable();
    }

    const heading = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      const content = heading[2];
      const className =
        level === 1
          ? "mt-2 text-2xl font-medium tracking-tight text-zinc-50"
          : level === 2
            ? "mt-8 text-lg font-medium tracking-tight text-zinc-100"
            : level === 3
              ? "mt-6 text-base font-medium text-zinc-200"
              : "mt-4 text-sm font-medium text-zinc-300";
      const Tag = (`h${Math.min(level + 1, 6)}` as unknown) as "h2" | "h3" | "h4" | "h5";
      blocks.push(
        <Tag key={key++} className={className}>
          {renderInline(content)}
        </Tag>,
      );
      continue;
    }

    const ordered = /^\d+\.\s+/.exec(trimmed);
    const unordered = /^[-*+]\s+/.exec(trimmed);
    if (ordered || unordered) {
      flushParagraph();
      const nextOrdered = Boolean(ordered);
      if (listItems.length > 0 && listOrdered !== nextOrdered) {
        flushList();
      }
      listOrdered = nextOrdered;
      listItems.push(trimmed.replace(/^(\d+\.|[-*+])\s+/, ""));
      continue;
    }

    if (trimmed.startsWith("> ")) {
      flushParagraph();
      flushList();
      blocks.push(
        <blockquote key={key++} className="border-l border-zinc-700 pl-4 text-[14px] italic leading-7 text-zinc-400">
          {renderInline(trimmed.slice(2))}
        </blockquote>,
      );
      continue;
    }

    if (trimmed.startsWith("---") || trimmed.startsWith("***")) {
      flushParagraph();
      flushList();
      blocks.push(<hr key={key++} className="border-zinc-900" />);
      continue;
    }

    paragraph.push(trimmed);
  }

  flushParagraph();
  flushList();
  flushTable();

  return <div className="space-y-4">{blocks.map((block, index) => <Fragment key={index}>{block}</Fragment>)}</div>;
}
