"""Splits a Bash call's command into the parts Mezmo Governance rules match."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from mezmo_governance_claude_agent_sdk.client import AGENT

# Mezmo takes up to 100 items a request. A command with more parts goes whole.
MAX_BASH_PARTS = 50
# Each argument is a row in Mezmo, and rules rarely look past the first few.
MAX_BASH_ARGS = 10
# The service's limit on an argument name (ai-governance-service schema/constants.js).
ARGUMENT_NAME_MAX = 64

# Bash command parts
#
# A Bash call goes to Mezmo as one item per part of its command, with the part's program,
# positional arguments and flags as fields a policy rule can match. The parser only ever leans
# one way: a part it can't fully read gets no program, arguments or flags, so a rule that
# allows by them can't match it, and a command it can't even split is sent whole, as before.
#
# It splits where Claude Code splits commands for its own Bash permission rules (on &&, ||, ;,
# |, |&, & and newlines) and skips the wrappers those rules strip, plus env and exec. Like those
# rules, it can't see through sh -c, scripts, aliases, or an option whose value is a separate
# word (the prod in kubectl -n prod lands among the arguments). This copy must match Claude
# Code's, which tests/test_drift.py checks.

# A part after one of these runs once the part before it has finished.
SEQUENTIAL_SEPARATORS = frozenset({"&&", "||", ";", "\n"})
# Words the shell reads as syntax rather than a program to run.
SHELL_KEYWORDS = frozenset({
    "!", "{", "}", "[[", "]]", "case", "do", "done", "elif", "else", "esac", "fi", "for",
    "function", "if", "in", "select", "then", "until", "while",
})
SIMPLE_WRAPPERS = frozenset({"builtin", "command", "exec", "nohup", "time", "xargs"})
# These run a command as someone else. They stay the program, and the command they run follows
# as a part of its own (_Scanner.read), as do env's with an assignment and eval's.
PRIVILEGE_WRAPPERS = frozenset({"doas", "sudo"})
# Their options whose form is certain: these take no value, and these take the next word.
PRIVILEGE_FLAGS = frozenset({"-E", "-H", "-P", "-S", "-b", "-k", "-n"})
PRIVILEGE_OPTIONS = frozenset({"-D", "-T", "-g", "-p", "-u"})
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


class BashPart(NamedTuple):
    text: str  # the part as written
    program: Optional[str]  # None when the part can't be fully read
    args: Tuple[str, ...]  # positional arguments, flags left out
    flags: Tuple[Tuple[str, FlagValue], ...]  # (name, True or the value written with it)
    after: Optional[str]  # the separator before it, None if it has none


class Unsplittable(Exception):
    """The command can't be split safely: an unclosed quote, a substitution, a heredoc or a final backslash."""


def split_command(command: str) -> Optional[List[BashPart]]:
    """The command's parts, or None when it can't be split safely or holds nothing to run."""
    try:
        parts = _Scanner(command).run()
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
        elif head == "env" and rest and not rest[0].startswith("-") and "=" not in rest[0]:
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


def _command_run_by(words: Sequence[str]) -> List[str]:
    """What sudo, doas or env runs, past assignments, or [] if its options aren't certain."""
    i = 1
    if words[0] in PRIVILEGE_WRAPPERS:
        while i < len(words) and words[i] != "--" and words[i].startswith("-"):
            if words[i] in PRIVILEGE_FLAGS:
                i += 1
            elif words[i] in PRIVILEGE_OPTIONS and i + 1 < len(words):
                i += 2
            else:
                return []
        if words[i : i + 1] == ["--"]:
            i += 1
    elif words[0] != "env":
        return []
    while i < len(words) and "=" in words[i]:
        i += 1
    command = list(words[i:])
    return command if command and not command[0].startswith("-") else []


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
    if not program or program in SHELL_KEYWORDS or ASSIGNMENT.match(program) or GLOB.search(program):
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
    def __init__(self, text: str, depth: int = 0) -> None:
        self.text = text
        self.depth = depth  # how many evals this text is inside
        self.i = 0
        self.parts: List[BashPart] = []
        self.start = 0  # where the current part begins
        self.after: Optional[str] = None  # the separator before the current part
        self.words: List[str] = []
        self.word: Optional[List[str]] = None  # the word being read, None between words
        self.readable = True
        self.redirect: Optional[str] = None  # a redirection waiting for its target

    def run(self) -> List[BashPart]:
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
            if c == "`" or (c == "$" and text.startswith("(", self.next_at(i))):
                raise Unsplittable
            if c == "$":
                self.readable = False
            if c == "\\" and text.startswith("\n", i + 1):
                i += 2  # a line continuation, which bash drops here too
                continue
            if c == "\\" and i + 1 < len(text) and text[i + 1] in '$`"\\':
                i += 1
                c = text[i]
            chars.append(c)
            i += 1
        raise Unsplittable

    def escaped(self) -> None:
        if self.text.startswith("\n", self.i + 1):
            self.i += 2  # a line continuation: bash drops it, so a word it splits stays one word
        elif self.i + 1 < len(self.text):
            self.add(self.text[self.i + 1])
            self.i += 2
        else:
            raise Unsplittable  # bash drops it, or joins it to whatever the agent runs after the command

    def dollar(self) -> None:
        # $( and $(( can hold separators of their own, so the command can't be split.
        if self.text.startswith("(", self.next_at(self.i)):
            raise Unsplittable
        self.readable = False
        self.add("$")
        self.i += 1

    def separator(self) -> None:
        text, i = self.text, self.i
        j = self.next_at(i)
        two = text[i] + text[j : j + 1]
        if two == "&>":
            self.redirection()
            return
        sep, end = (two, j + 1) if two in ("&&", "||", "|&") else (text[i], i + 1)
        self.end_part(i)
        self.i = self.start = end
        self.after = sep

    def redirection(self) -> None:
        # Digits right before the operator name a file descriptor, not an argument.
        if self.word is not None and "".join(self.word).isdigit():
            self.word = None
        else:
            self.end_word()
        at = [self.i, self.next_at(self.i)]
        at.append(self.next_at(at[1]))
        op = next(op for op in REDIRECTIONS if "".join(self.text[j : j + 1] for j in at).startswith(op))
        if op in ("<(", ">(", "<<"):
            raise Unsplittable  # a process substitution, or a heredoc whose lines aren't commands
        self.i = at[len(op) - 1] + 1
        self.redirect = op

    def next_at(self, i: int) -> int:
        """Where the character after the one at i is. Bash drops line continuations, even inside &&."""
        i += 1
        while self.text.startswith("\\\n", i):
            i += 2
        return i

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
        after = self.after if self.parts else None
        if readable:
            self.read(text, words, after)
        else:
            self.parts.append(BashPart(text, None, (), (), after))

    def read(self, text: str, words: Sequence[str], after: Optional[str]) -> None:
        """Add the part, then the command it runs when it starts with sudo, doas, env or eval.

        That command goes as a part of its own. Mezmo goes with the strictest answer, so a part
        added this way can only tighten a decision: a rule that allows sudo doesn't pass what sudo
        runs, and a rule on git sees sudo git.
        """
        while len(self.parts) <= MAX_BASH_PARTS:  # more parts go whole anyway
            program, args, flags = _read_part(words)
            self.parts.append(BashPart(text, program, args, flags, after))
            if program is None:
                return
            words, after = _skip_wrappers(words), None
            if program == "eval":
                self.parts.extend(self.evaluated(text, words[1:]))
                return
            words = _command_run_by(words)
            if not words:
                return

    def evaluated(self, text: str, words: Sequence[str]) -> List[BashPart]:
        """The parts of what eval runs: its words joined by spaces, after a first --, as bash does."""
        if self.depth >= MAX_BASH_PARTS:
            raise Unsplittable  # each eval adds a part, so this many go whole anyway
        if words[:1] == ["--"]:
            words = words[1:]
        try:
            return _Scanner(" ".join(words), self.depth + 1).run()
        except Unsplittable:
            return [BashPart(text, None, (), (), None)]


def drop_part_fields(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """A shell call's input without program, full_command, arg_<digits> or any flag_ name.

    Only the parser writes these. Taken from the agent's input, they could make a part it can't
    read look read, so a shell call never carries them from there, whether it goes as parts or
    whole.
    """
    return {
        name: value
        for name, value in arguments.items()
        if name not in ("program", "full_command")
        and not name.startswith("flag_")
        and not re.fullmatch(r"arg_[0-9]+", name)
    }


def bash_items(
    arguments: Mapping[str, Any], tool_name: str = "Bash", command_field: str = "command"
) -> Optional[List[Dict[str, Any]]]:
    """One item per part of a shell command, or None to send the call whole.

    The first item leads with the agent's input as received and the whole command, then each
    item has its part's fields. The input's own program, full_command, arg_<digits> and flag_
    names are dropped, since only the parser sets those (drop_part_fields). A parser failure
    only means the call goes whole: it never blocks it.
    """
    command = arguments.get(command_field)
    if not isinstance(command, str):
        return None
    try:
        parts = split_command(command)
    except Exception:
        return None
    if not parts or len(parts) > MAX_BASH_PARTS:
        return None
    rest = {name: value for name, value in drop_part_fields(arguments).items() if name != "command"}
    items = []
    for index, part in enumerate(parts):
        if len(parts) == 1:
            fields = {"command": command, **rest}
        elif index == 0:
            fields = {"full_command": command, **rest, "command": part.text}
        else:
            fields = {"command": part.text}
        if part.program is not None:
            fields["program"] = part.program
            fields.update((f"arg_{n}", value) for n, value in enumerate(part.args[:MAX_BASH_ARGS], 1))
            for name, value in part.flags:
                field = "flag_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
                if len(field) <= ARGUMENT_NAME_MAX:
                    fields[field] = value
        item: Dict[str, Any] = {"id": index, "tool_namespace": AGENT, "tool_name": tool_name, "arguments": fields}
        if index and part.after in SEQUENTIAL_SEPARATORS:
            item["dependencies"] = [index - 1]
        items.append(item)
    return items
