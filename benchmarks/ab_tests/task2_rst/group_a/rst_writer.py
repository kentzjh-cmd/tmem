class BaseWriter:
    def __init__(self, delimiter_pad=None, bookend=False, header_rows=None):
        self.delimiter_pad = delimiter_pad
        self.bookend = bookend
        self.header_rows = header_rows

class RSTWriter(BaseWriter):
    def __init__(self, header_rows=None):
        super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)

    def write(self, lines):
        if not lines and not self.header_rows:
            return []

        header_lines = []
        if self.header_rows:
            for row in self.header_rows:
                if isinstance(row, (list, tuple)):
                    header_lines.append("  ".join(str(c) for c in row))
                else:
                    header_lines.append(str(row))

        all_lines = header_lines + (lines or [])
        if not all_lines:
            return []

        border = "=" * max(len(line) for line in all_lines)

        if header_lines:
            if lines:
                return [border] + header_lines + [border] + lines + [border]
            else:
                return [border] + header_lines + [border]
        else:
            return [border] + lines + [border]

