"""Split a Bash command into the parts the shell would run, for Mezmo Governance.

Each part goes to Mezmo as its own item, with its program, positional arguments and flags as
fields a policy rule can match. The parser only ever leans one way: a part it can't fully
read gets no program or arguments, so a rule that allows by program or argument can't match
it, and a command it can't even split is sent whole, as before.

It splits where Claude Code splits commands for its own Bash permission rules (on &&, ||,
;, |, |&, & and newlines) and skips the wrappers those rules strip. Like those rules, it
can't see through sh -c, scripts, aliases, or an option whose value is a separate word
(the prod in kubectl -n prod lands among the arguments).
"""

from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

# A part after one of these runs once the part before it has finished.
SEQUENTIAL = frozenset({"&&", "||", ";", "\n"})
# Words the shell reads as syntax rather than a program to run.
RESERVED = frozenset({
    "!", "{", "}", "[[", "]]", "case", "do", "done", "elif", "else", "esac", "fi", "for",
    "function", "if", "in", "select", "then", "until", "while",
})
SIMPLE_WRAPPERS = frozenset({"builtin", "command", "nohup", "time", "xargs"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
GLOB = re.compile(r"[*?\[]")
DURATION = re.compile(r"\d+(\.\d+)?[smhd]?")
NICENESS = re.compile(r"-?\d+")
STDBUF_OPTION = re.compile(r"-[ioe]\S+|--(input|output|error)=\S+")
FILE_DESCRIPTOR = re.compile(r"\d+|-")
LETTERS = re.compile(r"[A-Za-z]+")
# Longest first, so <<< isn't read as <<.
REDIRECTIONS = ("&>>", "&>", "<<<", "<<", "<>", "<&", "<(", "<", ">>", ">|", ">&", ">(", ">")


FlagValue = Union[bool, str]


class Part(NamedTuple):
    text: str  # the part as written
    program: Optional[str]  # None when the part can't be fully read
    args: Tuple[str, ...]  # positional arguments, flags left out
    flags: Tuple[Tuple[str, FlagValue], ...]  # (name, True or the value written with it)
    after: Optional[str]  # the separator before it, None for the first part


class Unsplittable(Exception):
    """The command can't be split safely: an unclosed quote, a substitution or a heredoc."""


def split_command(command: str) -> Optional[List[Part]]:
    """The command's parts, or None when it can't be split safely or holds nothing to run."""
    try:
        parts = _Scanner(command.replace("\\\n", " ")).run()
    except Unsplittable:
        return None
    return parts or None


def _skip_wrappers(words: Sequence[str]) -> List[str]:
    """Drop the wrappers Claude Code strips, in the forms whose options are certain."""
    words = list(words)
    while words:
        head, rest = words[0], words[1:]
        if head in SIMPLE_WRAPPERS and rest and not rest[0].startswith("-"):
            words = rest
        elif head == "nice" and rest:
            if rest[0] == "-n" and len(rest) > 2 and NICENESS.fullmatch(rest[1]):
                words = rest[2:]
            elif re.fullmatch(r"-\d+", rest[0]) and len(rest) > 1:
                words = rest[1:]
            elif not rest[0].startswith("-"):
                words = rest
            else:
                break
        elif head == "timeout" and len(rest) > 1 and DURATION.fullmatch(rest[0]):
            words = rest[1:]
        elif head == "stdbuf":
            count = 0
            while count < len(rest) and STDBUF_OPTION.fullmatch(rest[count]):
                count += 1
            if count < len(rest) and not rest[count].startswith("-"):
                words = rest[count:]
            else:
                break
        else:
            break
    return words


def _flags_of(word: str) -> List[Tuple[str, FlagValue]]:
    """The flags one word sets. Without knowing the tool, it reads them the common ways."""
    if word.startswith("--"):
        name, has_value, value = word[2:].partition("=")
        return [(name, value if has_value else True)] if name else []
    body = word[1:]
    if LETTERS.fullmatch(body):
        flags: List[Tuple[str, FlagValue]] = [(letter, True) for letter in body]
        if len(body) > 2:
            flags.append((body, True))  # find, go and java take long options after one dash
        return flags
    if body[:1].isalpha():
        return [(body[0], body[1:])]  # head -n5
    return [(body, True)]  # kill -9


def _read_part(words: Sequence[str]) -> Tuple[Optional[str], Tuple[str, ...], Tuple[Tuple[str, FlagValue], ...]]:
    words = _skip_wrappers(words)
    if not words:
        return None, (), ()
    program = words[0]
    # An assignment changes what runs (PATH=/tmp ls), and a glob or keyword isn't a program.
    if not program or program in RESERVED or ASSIGNMENT.match(program) or GLOB.search(program):
        return None, (), ()
    args: List[str] = []
    flags: Dict[str, FlagValue] = {}
    options_done = False
    for word in words[1:]:
        if not options_done and word == "--":
            options_done = True
        elif options_done or not word.startswith("-") or word == "-":
            args.append(word)
        else:
            flags.update(_flags_of(word))
    return program, tuple(args), tuple(flags.items())


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.parts: List[Part] = []
        self.start = 0  # where the current part begins
        self.after: Optional[str] = None  # the separator before the current part
        self.words: List[str] = []
        self.word: Optional[List[str]] = None  # the word being read, None between words
        self.readable = True
        self.redirect: Optional[str] = None  # a redirection waiting for its target

    def run(self) -> List[Part]:
        text = self.text
        while self.i < len(text):
            c = text[self.i]
            if c == "'":
                self.single_quoted()
            elif c == '"':
                self.double_quoted()
            elif c == "\\":
                self.escaped()
            elif c == "`":
                raise Unsplittable
            elif c == "$":
                self.dollar()
            elif c in " \t\r":
                self.end_word()
                self.i += 1
            elif c == "#" and self.word is None:
                end = text.find("\n", self.i)
                self.i = len(text) if end < 0 else end
            elif c in "&|;\n":
                self.separator()
            elif c in "<>":
                self.redirection()
            elif c in "()":
                # A subshell or group: the parts inside run, but not as written here.
                self.readable = False
                self.end_word()
                self.i += 1
            else:
                self.add(c)
                self.i += 1
        self.end_part(len(text))
        return self.parts

    def add(self, chars: str) -> None:
        if self.word is None:
            self.word = []
        self.word.append(chars)

    def single_quoted(self) -> None:
        end = self.text.find("'", self.i + 1)
        if end < 0:
            raise Unsplittable
        self.add(self.text[self.i + 1 : end])
        self.i = end + 1

    def double_quoted(self) -> None:
        text, i, chars = self.text, self.i + 1, []
        while i < len(text):
            c = text[i]
            if c == '"':
                self.add("".join(chars))
                self.i = i + 1
                return
            if c == "`" or text.startswith("$(", i):
                raise Unsplittable
            if c == "$":
                self.readable = False
            if c == "\\" and i + 1 < len(text) and text[i + 1] in '$`"\\':
                i += 1
                c = text[i]
            chars.append(c)
            i += 1
        raise Unsplittable

    def escaped(self) -> None:
        if self.i + 1 < len(self.text):
            self.add(self.text[self.i + 1])
            self.i += 2
        else:
            self.add("\\")
            self.i += 1

    def dollar(self) -> None:
        # $( and $(( can hold separators of their own, so the command can't be split.
        if self.text.startswith("$(", self.i):
            raise Unsplittable
        self.readable = False
        self.add("$")
        self.i += 1

    def separator(self) -> None:
        text, i = self.text, self.i
        if text.startswith("&>", i):
            self.redirection()
            return
        two = text[i : i + 2]
        sep = two if two in ("&&", "||", "|&") else text[i]
        self.end_part(i)
        self.i = self.start = i + len(sep)
        self.after = sep

    def redirection(self) -> None:
        # Digits right before the operator name a file descriptor, not an argument.
        if self.word is not None and "".join(self.word).isdigit():
            self.word = None
        else:
            self.end_word()
        op = next(op for op in REDIRECTIONS if self.text.startswith(op, self.i))
        if op in ("<(", ">(", "<<"):
            raise Unsplittable  # a process substitution, or a heredoc whose lines aren't commands
        self.i += len(op)
        self.redirect = op

    def end_word(self) -> None:
        if self.word is None:
            return
        value = "".join(self.word)
        self.word = None
        if self.redirect is None:
            self.words.append(value)
            return
        op, self.redirect = self.redirect, None
        if op in ("<", "<<<"):
            return  # reads input
        if op in ("<&", ">&") and FILE_DESCRIPTOR.fullmatch(value):
            return  # points one stream at another
        if value != "/dev/null":
            self.readable = False  # writes a file

    def end_part(self, end: int) -> None:
        self.end_word()
        if self.redirect is not None:  # an operator with nothing after it
            self.readable = False
            self.redirect = None
        text = self.text[self.start : end].strip()
        words, readable = self.words, self.readable
        self.words, self.readable = [], True
        # A part with nothing to run is dropped, unless it does something unreadable (> file).
        if not words and readable:
            return
        program, args, flags = _read_part(words) if readable else (None, (), ())
        self.parts.append(Part(text, program, args, flags, self.after if self.parts else None))
