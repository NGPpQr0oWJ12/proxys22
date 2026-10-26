"""Offline checks for truthful transfer progress and explicit confirmations."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from unittest import TestCase, mock

from proxys22_manager import cli
from proxys22_manager.ui import Console


class TerminalTests(TestCase):
    def test_failed_transfer_does_not_display_completion(self):
        output, errors = io.StringIO(), io.StringIO()
        terminal = Console(output, errors)
        progress = terminal.progress('Загрузка образа')
        progress(0, 1000)
        progress(550, 1000)
        terminal.error('Передача прервана')
        self.assertIn('55%', output.getvalue())
        self.assertNotIn('100%', output.getvalue())
        self.assertNotIn('\033', output.getvalue())
        self.assertIn('Передача прервана', errors.getvalue())
        self.assertIsNone(terminal.active)

    def test_disabled_color_has_no_ansi_even_for_a_terminal(self):
        output = io.StringIO()
        terminal = Console(output)
        terminal.tty, terminal.color = True, False
        progress = terminal.progress('Передача')
        progress(0, 100)
        progress(100, 100)
        self.assertIn('100%', output.getvalue())
        self.assertNotIn('\033', output.getvalue())

    def test_russian_confirmation_requires_the_exact_action(self):
        terminal = Console(io.StringIO())
        for answer, accepted in [('', False), ('да', False), ('TRUST', False), (' доверяю ', True)]:
            with self.subTest(answer=answer), mock.patch('builtins.input', return_value=answer):
                self.assertEqual(terminal.confirm('Введите ДОВЕРЯЮ', 'ДОВЕРЯЮ'), accepted)
        with mock.patch('builtins.input', return_value='обновить'):
            self.assertTrue(terminal.confirm('Введите ОБНОВИТЬ', 'ОБНОВИТЬ'))

    def test_machine_diagnostics_remain_parseable_without_ui_decoration(self):
        output = io.StringIO()
        context = mock.MagicMock()
        context.__enter__.return_value.diagnostics.return_value = {'firmware': '0.4.1', 'healthy': True}
        with mock.patch.object(cli, 'router', return_value=context), redirect_stdout(output):
            cli.run_command(argparse.Namespace(command='diagnose', json=True))
        self.assertEqual(json.loads(output.getvalue()), {'firmware': '0.4.1', 'healthy': True})

    def test_argument_errors_are_explained_in_russian(self):
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
            cli.parser().parse_args(['download', '--kind', 'wrong'])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn('недопустимое значение', errors.getvalue())
        self.assertNotIn('invalid choice', errors.getvalue())

    def test_cancelled_progress_can_restart_for_a_new_tftp_request(self):
        output = io.StringIO()
        terminal = Console(output)
        progress = terminal.progress('Передача загрузчику')
        progress(0, 100)
        progress(40, 100)
        terminal.warning('Передача отменена')
        progress(0, 100)
        progress(100, 100)
        self.assertEqual(output.getvalue().count('100%'), 1)
