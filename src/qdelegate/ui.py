"""Terminal look: colors, logo, spinner and boxes. Falls back to plain text when output is not a terminal."""
import os
import re
import sys
import threading
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
GRADIENT = [51, 45, 39, 63, 99, 135, 171, 207]
LOGO = [
    "┏━┓╺┳┓┏━╸╻  ┏━╸┏━╸┏━┓╺┳╸┏━╸",
    "┃┓┃ ┃┃┣╸ ┃  ┣╸ ┃╺┓┣━┫ ┃ ┣╸ ",
    "┗┻┛╺┻┛┗━╸┗━╸┗━╸┗━┛╹ ╹ ╹ ┗━╸",
]
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def color_enabled(stream):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return stream.isatty() and os.environ.get("TERM") != "dumb"


class Style:
    def __init__(self, stream):
        self.on = color_enabled(stream)

    def code(self, text, *codes):
        return f"\x1b[{';'.join(str(c) for c in codes)}m{text}\x1b[0m" if self.on else text

    def bold(self, t): return self.code(t, 1)
    def dim(self, t): return self.code(t, 2)
    def green(self, t): return self.code(t, 38, 5, 84)
    def red(self, t): return self.code(t, 38, 5, 203)
    def yellow(self, t): return self.code(t, 38, 5, 221)
    def blue(self, t): return self.code(t, 38, 5, 75)
    def violet(self, t): return self.code(t, 38, 5, 141)

    def gradient(self, text):
        if not self.on:
            return text
        n = max(len(text) - 1, 1)
        return "".join(self.code(c, 38, 5, GRADIENT[i * (len(GRADIENT) - 1) // n]) for i, c in enumerate(text))


def width(text):
    return len(ANSI.sub("", text))


def clock(seconds):
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def chunks(text, limit):
    """Split plain text into pieces of at most limit characters, at spaces when possible."""
    limit = max(limit, 1)
    out = []
    while len(text) > limit:
        cut = text.rfind(" ", 1, limit + 1)
        cut = cut if cut > 0 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip(" ")
    return out + [text]


def logo(style, version):
    tag = style.dim(f"local model · isolated worktree · tests you can trust · v{version}")
    return "\n".join(["", *("  " + style.gradient(l) for l in LOGO), "  " + tag, ""])


def box(style, lines, border=None):
    """Rounded box around lines that may contain color codes."""
    paint = border or style.dim
    columns = shutil_columns()
    inner = min(max(width(l) for l in lines), columns - 4)
    out = [paint("╭" + "─" * (inner + 2) + "╮")]
    for line in lines:
        pad = max(inner - width(line), 0)
        out.append(paint("│") + " " + line + " " * pad + " " + paint("│"))
    out.append(paint("╰" + "─" * (inner + 2) + "╯"))
    return "\n".join(out)


def shutil_columns():
    import shutil
    return max(shutil.get_terminal_size((100, 24)).columns, 50)


def clip(text, limit):
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


STEP_LABELS = (("→", "read", "blue"), ("✱", "find", "violet"), ("←", "edit", "green"), ("$ ", "run ", "yellow"),
               ("✗", "fail", "red"), ("Error", "fail", "red"), ("•", "task", "violet"))


class Progress:
    """Live view on stderr: a line per agent step, with a spinner at the bottom."""

    def __init__(self, quiet=False, stream=None):
        self.stream = stream or sys.stderr
        self.style = Style(self.stream)
        self.quiet = quiet
        self.live = self.style.on and self.stream.isatty()
        self.t0 = time.time()
        self.status = "starting"
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def write(self, line):
        if self.quiet:
            return
        with self.lock:
            self.stream.write(("\r\x1b[K" if self.live else "") + line + "\n")
            self.stream.flush()

    def stamp(self):
        return f"{time.time() - self.t0:5.0f}s"

    def banner(self, version):
        if not self.quiet and self.style.on:
            self.write(logo(self.style, version))

    def step(self, line):
        """Show one line of agent output."""
        if not self.style.on:
            self.write(f"[{self.stamp()}] {line[:110]}")
            return
        for prefix, label, color in STEP_LABELS:
            if line.startswith(prefix):
                text = line[len(prefix):].strip()
                paint = getattr(self.style, color)
                self.write(f"  {self.style.dim(clock(time.time() - self.t0))}  {paint(label)}  "
                           + clip(text, shutil_columns() - 16))
                self.status = f"{label.strip()} {clip(text, 40)}"
                return

    def detail(self, line):
        """Show the model's thinking or text, for --verbose."""
        if not line.strip() or line.startswith("> "):
            return
        if not self.style.on:
            self.write(f"[{self.stamp()}]   {line[:300]}")
            return
        prefix = " " * 9
        if line.startswith("Thinking:"):
            text = line[len("Thinking:"):].strip()
            label = self.style.violet("think") + "  "
            self.status = "think " + clip(text, 40)
        else:
            text, label = line, " " * 7
        paint = self.style.green if line.startswith("+") else self.style.red if line.startswith("-") else self.style.dim
        limit = shutil_columns() - 18
        for i, piece in enumerate(chunks(text, limit)):
            self.write("  " + prefix + (label if i == 0 else " " * 7) + paint(piece))

    def note(self, text, kind="note"):
        """Show a qdelegate event such as a new attempt or a test result."""
        if not self.style.on:
            self.write(f"[{self.stamp()}] {text}")
            return
        icon = {"good": self.style.green("✔"), "bad": self.style.red("✘"), "warn": self.style.yellow("▲")}.get(kind, self.style.violet("◆"))
        self.write(f"  {self.style.dim(clock(time.time() - self.t0))}  {icon}  {self.style.bold(text)}")
        self.status = text

    def start(self):
        if self.quiet or not self.live:
            return
        self.stream.write("\x1b[?25l")

        def spin():
            i = 0
            while not self.stop_event.wait(0.1):
                frame = self.style.gradient(SPINNER[i % len(SPINNER)])
                text = clip(self.status, shutil_columns() - 14)
                with self.lock:
                    self.stream.write(f"\r\x1b[K  {frame} {self.style.dim(clock(time.time() - self.t0))}  {self.style.dim(text)}")
                    self.stream.flush()
                i += 1

        self.thread = threading.Thread(target=spin, daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread:
            self.stop_event.set()
            self.thread.join(timeout=1)
            self.thread = None
        if self.live and not self.quiet:
            self.stream.write("\r\x1b[K\x1b[?25h")
            self.stream.flush()


def diffstat(style, stat):
    """Color the +++--- bars of a git diffstat."""
    out = []
    for line in stat.splitlines():
        if "|" in line:
            name, bar = line.split("|", 1)
            bar = re.sub(r"\++", lambda m: style.green(m.group()), bar)
            bar = re.sub(r"-+", lambda m: style.red(m.group()), bar)
            out.append(style.bold(name.rstrip()) + " " + style.dim("│") + bar)
        else:
            out.append(style.dim(line.strip()))
    return out


HEADLINES = {
    0: ("green", "✔", "Tests green. Ready for your review"),
    1: ("red", "✘", "Tests red"),
    4: ("yellow", "▲", "Attempt stopped"),
    5: ("red", "✘", "Forbidden files changed. Do not merge"),
    6: ("yellow", "▲", "The agent changed nothing"),
    7: ("yellow", "▲", "The agent reports something is MISSING"),
}


def report_card(style, code, facts, stat, agent_text, merge, discard):
    """The final result, as a colored card."""
    color, icon, title = HEADLINES.get(code, ("red", "✘", "Failed"))
    if code == 1 and facts.get("Tests", "") == "no test command given":
        color, icon, title = "yellow", "▲", "Finished, but no test command was given"
    paint = getattr(style, color)
    columns = shutil_columns()
    inner = columns - 4
    lines = [paint(style.bold(f"{icon} {title}")), ""]
    for key, value in facts.items():
        pieces = chunks(value, inner - 9)
        lines.append(style.dim(f"{key:<9}") + pieces[0])
        lines.extend(" " * 9 + piece for piece in pieces[1:])
    if stat:
        lines.append("")
        for plain, colored in zip(stat.splitlines(), diffstat(style, stat)):
            lines.extend([colored] if width(colored) <= inner else chunks(plain.strip(), inner))
    out = ["", box(style, lines, border=paint)]
    if agent_text:
        out += ["", "  " + style.dim("agent says")]
        for text in agent_text:
            out += ["  " + style.dim("│ ") + piece for piece in chunks(text, columns - 4)]
    if merge:
        out.append("")
        for label, paint_label, command in (("merge", style.green, merge), ("discard", style.red, discard)):
            pieces = chunks(command, columns - 10)
            out.append("  " + paint_label(f"{label:<8}") + pieces[0])
            out.extend(" " * 10 + piece for piece in pieces[1:])
    out.append("")
    return "\n".join(out)
