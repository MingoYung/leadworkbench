import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from leadworkbench import launcher
from leadworkbench.cli import ApiError
from leadworkbench.desktop_runtime import instance_name


class NativeLaunchTests(unittest.TestCase):
    def test_normal_start_routes_to_native_window_without_browser(self):
        directory = Path(tempfile.gettempdir()) / 'native-launch-fixture'
        info = {'url': 'http://127.0.0.1:1/', 'data_dir': str(directory), 'browser_open_requested': False}
        run = Mock(return_value=0)
        module = types.SimpleNamespace(run_native_app=run)
        with patch.object(launcher, 'ensure_service', return_value=info), patch.dict(sys.modules, {'leadworkbench.desktop_runtime': module}):
            result = launcher.launch_desktop(directory)
        run.assert_called_once_with(directory, info)
        self.assertEqual('native_qt_widgets', result['interface'])
        self.assertFalse(result['browser_open_requested'])

    def test_legacy_headless_start_does_not_import_or_open_gui(self):
        info = {'url': 'http://127.0.0.1:1/', 'data_dir': 'fixture', 'browser_open_requested': False}
        with patch.object(launcher, 'ensure_service', return_value=info), patch.dict(sys.modules, {'leadworkbench.desktop_runtime': None}):
            self.assertEqual('service_only', launcher.launch_desktop(open_browser=False)['interface'])

    def test_missing_native_components_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            info = {'data_dir': temp, 'browser_open_requested': False}
            with patch.object(launcher, 'ensure_service', return_value=info), patch.dict(sys.modules, {'leadworkbench.desktop_runtime': None}), self.assertRaises(ApiError) as result:
                launcher.launch_desktop()
            self.assertEqual('DESKTOP_DEPENDENCY', result.exception.code)
            self.assertIn('desktop_runtime', (Path(temp) / 'desktop-error.log').read_text(encoding='utf-8'))

    def test_console_distribution_relays_to_windowed_sibling(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            console = directory / 'LeadWorkbench/LeadWorkbench.exe'
            console.parent.mkdir(); console.touch()
            desktop = directory / 'LeadWorkbenchDesktop/LeadWorkbenchDesktop.exe'
            desktop.parent.mkdir(); desktop.touch()
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(console)), patch.object(launcher.subprocess, 'Popen') as process, patch.object(launcher, 'ensure_service') as service:
                result = launcher.launch_desktop(directory / 'data')
            self.assertEqual(str(desktop), process.call_args.args[0][0])
            service.assert_not_called()
            self.assertFalse(result['browser_open_requested'])

    def test_same_data_directory_has_stable_distinct_ipc_name(self):
        first = Path(tempfile.gettempdir()) / 'native-a'
        self.assertEqual(instance_name(first), instance_name(first / '.'))
        self.assertNotEqual(instance_name(first), instance_name(first.parent / 'native-b'))

    def test_service_only_and_legacy_flag_are_compatible(self):
        for flag in ('--service-only', '--no-browser'):
            with self.subTest(flag=flag), patch.object(launcher, 'launch_desktop', return_value={'interface': 'service_only'}) as launch:
                self.assertEqual(0, launcher.main(['desktop', flag]))
                self.assertFalse(launch.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
