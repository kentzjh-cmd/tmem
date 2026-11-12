class BaseWriter:
    def __init__(self, delimiter_pad=None, bookend=False):
        self.delimiter_pad = delimiter_pad
        self.bookend = bookend

class RSTWriter(BaseWriter):
    def __init__(self, header_rows=None):
        super().__init__(delimiter_pad=None, bookend=False)
        self.header_rows = header_rows

    def write(self, lines):
        if not lines:
            return []
        if self.header_rows:
            formatted_headers = []
            for row in self.header_rows:
                if isinstance(row, (list, tuple)):
                    formatted_headers.append("  ".join(str(x) for x in row))
                else:
                    formatted_headers.append(str(row))
            border = "=" * max(len(l) for l in formatted_headers + lines)
            return [border] + formatted_headers + [border] + lines + [border]
        border = "=" * len(lines[0])
        return [border] + lines + [border]
