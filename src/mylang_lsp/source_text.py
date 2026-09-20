"""Source-text utilities shared by diagnostics and semantic highlighting."""

from typing import Dict, List, Tuple


ProtectedSpans = Dict[int, List[Tuple[int, int]]]


def utf16_column(line: str, codepoint_column: int) -> int:
    return len(line[:codepoint_column].encode("utf-16-le")) // 2


def protected_spans(lines: List[str]) -> ProtectedSpans:
    """Locate strings and comments so punctuation inside them is ignored."""
    spans: ProtectedSpans = {}
    in_block_comment = False
    for line_no, line in enumerate(lines):
        line_spans: List[Tuple[int, int]] = []
        index = 0
        while index < len(line):
            if in_block_comment:
                end = line.find("*/", index)
                if end < 0:
                    line_spans.append((index, len(line)))
                    break
                line_spans.append((index, end + 2))
                index = end + 2
                in_block_comment = False
                continue

            char = line[index]
            following = line[index + 1] if index + 1 < len(line) else ""
            if char in ('"', "'"):
                quote = char
                end = index + 1
                escaped = False
                while end < len(line):
                    if escaped:
                        escaped = False
                    elif line[end] == "\\":
                        escaped = True
                    elif line[end] == quote:
                        end += 1
                        break
                    end += 1
                line_spans.append((index, end))
                index = end
                continue

            if char == "/" and following == "/":
                line_spans.append((index, len(line)))
                break
            if char == "/" and following == "*":
                end = line.find("*/", index + 2)
                if end < 0:
                    line_spans.append((index, len(line)))
                    in_block_comment = True
                    break
                line_spans.append((index, end + 2))
                index = end + 2
                continue
            index += 1

        if line_spans:
            spans[line_no] = line_spans
    return spans


def is_protected(
    line_no: int,
    start: int,
    end: int,
    spans: ProtectedSpans,
) -> bool:
    return any(
        start < span_end and end > span_start
        for span_start, span_end in spans.get(line_no, [])
    )
