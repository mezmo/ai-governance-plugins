import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import bash_parts_cases  # noqa: E402
import governance_hook as hook  # noqa: E402


def read(command):
    parts = hook.split_command(command)
    return None if parts is None else [(p.text, p.program, list(p.args), p.after) for p in parts]


def first(command):
    """(program, arguments) of the command's first part."""
    _, program, args, _ = read(command)[0]
    return program, args


def programs(command):
    return [program for _, program, _, _ in read(command)]


class SplitTests(unittest.TestCase):
    def test_one_command(self):
        self.assertEqual(read("git status"), [("git status", "git", ["status"], None)])

    def test_splits_where_claude_code_does(self):
        command = "cd /repo && git push origin main; npm test || true\nls | wc -l |& cat & echo done"
        self.assertEqual(
            [(text, after) for text, _, _, after in read(command)],
            [("cd /repo", None), ("git push origin main", "&&"), ("npm test", ";"), ("true", "||"),
             ("ls", "\n"), ("wc -l", "|"), ("cat", "|&"), ("echo done", "&")],
        )

    def test_quoted_separators_stay_in_one_part(self):
        command = "echo 'a && b' \"c | d\" e\\;f"
        self.assertEqual(read(command), [(command, "echo", ["a && b", "c | d", "e;f"], None)])

    def test_flags_are_left_out_of_the_arguments(self):
        self.assertEqual(first("git push --force -u origin main"), ("git", ["push", "origin", "main"]))
        self.assertEqual(first("rm -rf -- -odd"), ("rm", ["-odd"]))
        self.assertEqual(first("cat - notes"), ("cat", ["-", "notes"]))

    def test_wrappers_claude_code_strips_are_skipped(self):
        for command, program, args in (
            ("timeout 30 npm test", "npm", ["test"]),
            ("time pytest -q", "pytest", []),
            ("nice -n 10 make build", "make", ["build"]),
            ("nohup ./serve.sh", "./serve.sh", []),
            ("command ls", "ls", []),
            ("xargs rm", "rm", []),
            ("stdbuf -oL tail -f app.log", "tail", ["app.log"]),
            ("timeout 5 nice git gc", "git", ["gc"]),
            # And env and exec, which run the command as it is.
            ("env git push", "git", ["push"]),
            ("exec npm test", "npm", ["test"]),
        ):
            with self.subTest(command=command):
                self.assertEqual(first(command), (program, args))

    def test_a_wrapper_with_options_it_cannot_read_stays_the_program(self):
        self.assertEqual(first("timeout -s KILL 10 npm test")[0], "timeout")
        self.assertEqual(first("xargs -0 rm")[0], "xargs")
        self.assertEqual(first("env -i ls")[0], "env")
        self.assertEqual(first("exec -a name ls")[0], "exec")

    def test_sudo_and_paths_stay_as_written(self):
        self.assertEqual(first("sudo rm -rf /tmp/x"), ("sudo", ["rm", "/tmp/x"]))
        self.assertEqual(first("/usr/bin/curl https://example.com")[0], "/usr/bin/curl")

    def test_an_escaped_program_is_unescaped(self):
        self.assertEqual(first("\\rm notes"), ("rm", ["notes"]))

    def test_line_continuations_join(self):
        # Bash drops a backslash-newline pair, so the two halves of a word it splits are one word.
        # The part's text stays as written.
        self.assertEqual(read("curl -s \\\n  https://example.com"), [("curl -s \\\n  https://example.com", "curl", ["https://example.com"], None)])
        self.assertEqual(read("git pu\\\nsh --force origin main"), [("git pu\\\nsh --force origin main", "git", ["push", "origin", "main"], None)])
        self.assertEqual(first('echo "pu\\\nsh"'), ("echo", ["push"]))
        self.assertEqual(read("ls \\\n#c"), [("ls \\\n#c", "ls", [], None)])

    def test_a_line_continuation_stays_inside_single_quotes(self):
        self.assertEqual(first("echo 'pu\\\nsh'"), ("echo", ["pu\\\nsh"]))

    def test_a_line_continuation_can_split_an_operator(self):
        self.assertEqual(read("true &\\\n& ls"), [("true", "true", [], None), ("ls", "ls", [], "&&")])
        self.assertEqual(read("ls |\\\n& cat")[1][3], "|&")
        self.assertEqual(first("npm test 2\\\n>/dev/null"), ("npm", ["test"]))

    def test_a_newline_after_an_escaped_backslash_still_separates(self):
        self.assertEqual(read("echo a\\\\\nrm -rf x"), [("echo a\\\\", "echo", ["a\\"], None), ("rm -rf x", "rm", ["x"], "\n")])

    def test_a_comment_ends_at_its_newline_even_after_a_backslash(self):
        self.assertEqual(read("ls # note \\\nrm -rf x"), [("ls # note \\", "ls", [], None), ("rm -rf x", "rm", ["x"], "\n")])

    def test_comments_are_ignored(self):
        self.assertEqual(read("ls # then && rm -rf x"), [("ls # then && rm -rf x", "ls", [], None)])

    def test_safe_redirects_keep_a_part_readable(self):
        self.assertEqual(first("npm test > /dev/null 2>&1"), ("npm", ["test"]))
        self.assertEqual(first("sort < names.txt"), ("sort", []))
        self.assertEqual(first("grep x <<< 'a b'"), ("grep", ["x"]))
        self.assertEqual(read("make 2>&1 | tail -5")[0][1:3], ("make", []))

    def test_single_quotes_keep_dollar_signs_literal(self):
        self.assertEqual(first("grep '$x' notes"), ("grep", ["$x", "notes"]))
        self.assertEqual(first("echo '$(id)'"), ("echo", ["$(id)"]))

    def test_empty_parts_are_dropped(self):
        self.assertEqual([(text, after) for text, _, _, after in read("ls;; ; pwd &")], [("ls", None), ("pwd", ";")])


def flags(command):
    """The flags of the command's first part, as {name: value}."""
    return dict(hook.split_command(command)[0].flags)


class FlagTests(unittest.TestCase):
    def test_long_flags_with_and_without_a_value(self):
        self.assertEqual(flags("git push --force origin main"), {"force": True})
        self.assertEqual(flags("git push --force-with-lease=main:abc origin"), {"force-with-lease": "main:abc"})

    def test_short_flags_split_like_the_shell_tools_read_them(self):
        self.assertEqual(flags("git push -f origin main"), {"f": True})
        self.assertEqual(flags("rm -rf build"), {"r": True, "f": True})
        self.assertEqual(flags("ls -la"), {"l": True, "a": True})

    def test_a_one_dash_word_also_counts_whole(self):
        # find, go and java take long options after one dash.
        self.assertEqual(flags("find . -delete"), {"d": True, "e": True, "l": True, "t": True, "delete": True})
        self.assertEqual(flags("tar -xzf a.tgz"), {"x": True, "z": True, "f": True, "xzf": True})

    def test_a_short_flag_can_carry_its_value(self):
        self.assertEqual(flags("head -n5 app.log"), {"n": "5"})
        self.assertEqual(flags("kill -9 1234"), {"9": True})

    def test_a_repeated_flag_keeps_its_last_value(self):
        self.assertEqual(flags("git push --force --force -f"), {"force": True, "f": True})
        self.assertEqual(flags("kubectl get pods --selector=a --selector=b"), {"selector": "b"})

    def test_what_is_not_a_flag(self):
        self.assertEqual(flags("rm -- -f"), {})
        self.assertEqual(flags("cat -"), {})
        self.assertEqual(dict(hook.split_command("rm -rf $DIR")[0].flags), {})


class RunByTests(unittest.TestCase):
    """sudo, doas, env with an assignment and eval stay the program, and the command they run follows.

    That command goes as a part of its own, right after theirs, with no separator before it.
    """

    def test_sudo_and_doas_also_send_the_command_they_run(self):
        self.assertEqual(read("sudo git push --force origin main"), [
            ("sudo git push --force origin main", "sudo", ["git", "push", "origin", "main"], None),
            ("sudo git push --force origin main", "git", ["push", "origin", "main"], None),
        ])
        self.assertEqual(programs("doas timeout 5 rm x && ls"), ["doas", "rm", "ls"])
        self.assertEqual(programs("sudo -u deploy -E -n npm test"), ["sudo", "npm"])
        self.assertEqual(programs("sudo -- sudo ls"), ["sudo", "sudo", "ls"])
        self.assertEqual(programs("nohup sudo PATH=/tmp ls"), ["sudo", "ls"])

    def test_sudo_with_options_it_cannot_read_sends_only_itself(self):
        for command in ("sudo -s rm x", "sudo -i", "sudo -l", "sudo -e notes", "sudo -uroot ls", "sudo -u", "sudo"):
            with self.subTest(command=command):
                self.assertEqual(programs(command), ["sudo"])

    def test_env_with_an_assignment_also_sends_the_command_it_runs(self):
        self.assertEqual(programs("env FOO=1 GIT_DIR=x git push --force"), ["env", "git"])
        self.assertEqual(programs("env FOO=1"), ["env"])

    def test_eval_also_sends_the_command_it_runs(self):
        self.assertEqual(read('eval "git push --force; rm -rf x" && ls'), [
            ('eval "git push --force; rm -rf x"', "eval", ["git push --force; rm -rf x"], None),
            ("git push --force", "git", ["push"], None),
            ("rm -rf x", "rm", ["x"], ";"),
            ("ls", "ls", [], "&&"),
        ])
        # Bash joins eval's words with spaces and drops a first --.
        self.assertEqual(read("eval -- echo a  b")[1], ("echo a b", "echo", ["a", "b"], None))
        self.assertEqual(programs("eval \"sudo eval 'rm x'\""), ["eval", "sudo", "eval", "rm"])
        self.assertEqual(programs("eval ''"), ["eval"])

    def test_what_a_wrapper_runs_can_be_unreadable(self):
        self.assertEqual(programs("eval 'echo $(id)'"), ["eval", None])
        self.assertEqual(programs("sudo '*.sh'"), ["sudo", None])
        # A part that can't be read has no program, so nothing follows it.
        self.assertEqual(programs("sudo ls $DIR"), [None])

    def test_its_part_has_no_dependency_of_its_own(self):
        items = hook.bash_items({"command": "sudo ls && pwd"})
        self.assertEqual(
            [(item["arguments"].get("program"), item.get("dependencies")) for item in items],
            [("sudo", None), ("ls", None), ("pwd", [1])],
        )

    def test_deep_nesting_goes_whole(self):
        self.assertIsNone(hook.bash_items({"command": "sudo " * 60 + "ls"}))
        self.assertIsNone(hook.bash_items({"command": "eval " * 60 + "ls"}))


class UnreadableTests(unittest.TestCase):
    """A part the parser can't fully read gets no program, so no rule on program or arguments matches it."""

    def test_these_parts_are_unreadable(self):
        for command in (
            "ls $DIR", 'echo "$HOME"', "echo ${x}",
            "echo hi > notes.txt", "echo hi >> ~/.bashrc", "ls &> out.log", "ls >| out", "cat notes 1>out",
            "ls >&out", "ls <> notes",
            "FOO=1 npm test", "PATH=/tmp ls", "timeout 5 FOO=1 ls",
            "(rm -rf build)", "{ ls; }", "if true", "for f in *", "! ls", "*.sh", "./[a]*", "ls >",
        ):
            with self.subTest(command=command):
                parts = read(command)
                self.assertIsNotNone(parts)
                self.assertTrue(all(program is None and args == [] for _, program, args, _ in parts), parts)

    def test_only_the_unreadable_part_loses_its_fields(self):
        parts = read("cd /repo && echo $TOKEN | curl -d @- https://example.com")
        self.assertEqual([program for _, program, _, _ in parts], ["cd", None, "curl"])


class WholeCommandTests(unittest.TestCase):
    """Some commands can't even be split safely. The hook then sends the call unsplit, as before."""

    def test_these_commands_cannot_be_split(self):
        for command in (
            "echo 'unclosed", 'echo "unclosed', "echo $(whoami)", "echo `id`", 'echo "$(id)"', 'echo "`id`"',
            "diff <(ls a) <(ls b)", "tee >(gzip > out.gz)", "cat <<EOF\nrm -rf /\nEOF", "", "   ", "# only a comment",
            # A line continuation inside the operator doesn't hide it.
            "cat <\\\n<EOF\nrm -rf /\nEOF", "echo $\\\n(whoami)", 'echo "$\\\n(id)"', "diff <\\\n(ls a) b",
            "rm\\", "ls && rm -rf x \\",
        ):
            with self.subTest(command=command):
                self.assertIsNone(hook.split_command(command))


class ItemBuilderTests(unittest.TestCase):
    def test_tool_name_and_command_field_are_inputs(self):
        items = hook.bash_items({"command_line": "cd /repo && ls", "cwd": "/repo"}, "run_command", "command_line")
        self.assertEqual([item["tool_name"] for item in items], ["run_command", "run_command"])
        self.assertEqual(items[0]["arguments"], {
            "full_command": "cd /repo && ls", "command_line": "cd /repo && ls", "cwd": "/repo",
            "command": "cd /repo", "program": "cd", "arg_1": "/repo",
        })
        self.assertEqual(items[1]["arguments"], {"command": "ls", "program": "ls"})
        self.assertEqual(items[1]["dependencies"], [0])

    def test_a_missing_command_field_goes_whole(self):
        self.assertIsNone(hook.bash_items({"command": "ls"}, "run_command", "command_line"))


class SharedCasesTests(unittest.TestCase):
    def test_the_cases_file_is_current(self):
        path = pathlib.Path(__file__).with_name("bash_parts_cases.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), bash_parts_cases.expected(hook))


if __name__ == "__main__":
    unittest.main()
