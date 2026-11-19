"""Builds a one-page participation certificate as a PDF using only the standard library."""

PAGE_W, PAGE_H = 842, 595  # A4 landscape, in points


def _width(text: str, size: float, bold: bool = False) -> float:
    """Rough text width for Helvetica, good enough for centering."""
    total = 0.0
    for ch in text:
        if ch == " ":
            total += 0.278
        elif ch in "iljtfI.,:;'|!":
            total += 0.26
        elif ch in "mwMW":
            total += 0.85
        elif ch.isupper():
            total += 0.70
        else:
            total += 0.54 if bold else 0.5
    return total * size


def _esc(text: str) -> str:
    safe = text.encode("latin-1", "replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _clip(text: str, size: float, bold: bool, max_width: float) -> str:
    if _width(text, size, bold) <= max_width:
        return text
    while text and _width(text + "...", size, bold) > max_width:
        text = text[:-1]
    return text.rstrip() + "..."


def build_certificate(name: str, event_name: str, event_date: str = "", venue: str = "",
                      team_name: str = "") -> bytes:
    ops = []

    def text(y, s, size, bold=False, rgb="0.1 0.1 0.25"):
        s = _clip(s, size, bold, PAGE_W - 140)
        x = (PAGE_W - _width(s, size, bold)) / 2
        ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {rgb} rg {x:.1f} {y} Td ({_esc(s)}) Tj ET")

    ops.append("0.36 0.29 0.86 RG 6 w 30 30 782 535 re S")      # outer border
    ops.append("0.55 0.36 0.96 RG 1.5 w 42 42 758 511 re S")     # inner border
    text(480, "CERTIFICATE", 46, True, "0.36 0.29 0.86")
    text(445, "OF PARTICIPATION", 20, True)
    text(385, "This is to certify that", 16)
    text(330, name, 36, True, "0.2 0.15 0.6")
    ops.append("0.55 0.36 0.96 RG 1 w 200 318 m 642 318 l S")
    text(285, "has successfully participated in", 16)
    text(240, event_name, 28, True)
    if team_name:
        text(205, f"as a member of team {team_name}", 16)
    details = "  |  ".join(x for x in [event_date, venue] if x)
    if details:
        text(170, details, 14)
    ops.append("0.1 0.1 0.25 RG 1 w 110 95 m 290 95 l S 552 95 m 732 95 l S")
    ops.append(f"BT /F1 12 Tf 0.1 0.1 0.25 rg 150 78 Td ({_esc('Organizing Committee')}) Tj ET")
    ops.append(f"BT /F1 12 Tf 0.1 0.1 0.25 rg 610 78 Td ({_esc('College Fest')}) Tj ET")
    stream = "\n".join(ops).encode("latin-1", "replace")

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
         "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>").encode(),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
