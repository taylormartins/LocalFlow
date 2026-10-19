"""The Mac versions of the desktop pieces: overlay windows, the window you
started in, keys and clicks, apps and screens, start at login.

Each module here replaces the Windows functions of the same names (see
``localflow.platform``). They use AppKit and Quartz through PyObjC, imported
inside functions so this package imports anywhere (the tests run on both).

Permissions macOS asks for (System Settings > Privacy & Security), for the
Python that runs Golden Flow: Microphone (dictation), Accessibility (typing
the paste, pressing keys, and later moving windows), Input Monitoring (the
dictation key). Camera only for camera mode.
"""
