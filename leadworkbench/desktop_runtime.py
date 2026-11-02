"""Native Qt Widgets entry and same-user single-instance activation. No web engine."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time


def instance_name(data_dir: Path) -> str:
    identity = os.path.normcase(str(data_dir.resolve()))
    return 'LeadWorkbenchNative-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]


def run_native_app(data_dir: Path, service_info: dict) -> int:
    # Qt's Windows build links the Windows ICU API. Preload that exact system
    # library before Qt imports; unrelated ICU builds on PATH have the same DLL
    # name but different exports and can otherwise be collected by a packager.
    system_icu = None
    if os.name == 'nt':
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)):
            raise OSError('Cannot resolve Windows system directory')
        system_icu = ctypes.WinDLL(str(Path(buffer.value) / 'icuuc.dll'), winmode=0x00000800)
    from PySide6.QtCore import QIODevice, QTimer
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication, QMainWindow
    from .cli import ApiError

    if os.name == 'nt':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('LeadWorkbench.NativeDesktop')
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName('获客平台')
    app.setOrganizationName('LeadWorkbench')
    name = instance_name(data_dir)
    probe = QLocalSocket()
    probe.connectToServer(name, QIODevice.OpenModeFlag.WriteOnly)
    if probe.waitForConnected(400):
        probe.write(b'activate\n')
        probe.waitForBytesWritten(500)
        probe.disconnectFromServer()
        return 0
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        raise ApiError('DESKTOP_INSTANCE', '桌面窗口正在启动，请稍后再次点击桌面图标。')

    peers = set()
    def activate():
        for window in app.topLevelWidgets():
            if isinstance(window, QMainWindow):
                if window.isMinimized():
                    window.showNormal()
                else:
                    window.show()
                window.raise_()
                window.activateWindow()
                break

    def receive(peer):
        data = bytes(peer.property('activation_buffer') or b'') + bytes(peer.readAll())
        if len(data) > 64:
            peer.disconnectFromServer()
            return
        peer.setProperty('activation_buffer', data)
        if b'\n' not in data:
            return
        if data.strip() == b'activate':
            activate()
        peer.disconnectFromServer()

    def connected():
        while server.hasPendingConnections():
            peer = server.nextPendingConnection()
            peers.add(peer)
            peer.readyRead.connect(lambda p=peer: receive(p))
            peer.disconnected.connect(lambda p=peer: (peers.discard(p), p.deleteLater()))
            if peer.bytesAvailable():
                receive(peer)
    server.newConnection.connect(connected)
    state_file = data_dir / 'desktop.json'
    state = {'pid': os.getpid(), 'interface': 'native_qt_widgets', 'started_at': time.time(), 'data_dir': str(data_dir)}
    temp = state_file.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, state_file)
    import faulthandler
    startup_log = (data_dir / 'desktop-startup.log').open('a', encoding='utf-8')
    startup_log.write('Loading native widgets\n'); startup_log.flush()
    faulthandler.dump_traceback_later(15, file=startup_log)
    def ready():
        faulthandler.cancel_dump_traceback_later()
        windows = [(w.windowTitle(), w.isVisible()) for w in app.topLevelWidgets() if isinstance(w, QMainWindow)]
        startup_log.write('Native event loop ready: ' + repr(windows) + '\n'); startup_log.flush()
    QTimer.singleShot(0, ready)
    try:
        from .native_window import run_native
        startup_log.write('Native widget module imported\n'); startup_log.flush()
        return run_native(data_dir, service_info)
    finally:
        faulthandler.cancel_dump_traceback_later()
        startup_log.close()
        server.close()
        try:
            current = json.loads(state_file.read_text(encoding='utf-8'))
            if current.get('pid') == os.getpid():
                state_file.unlink()
        except (OSError, ValueError):
            pass
