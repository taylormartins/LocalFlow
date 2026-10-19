"""Golden Flow (the package is still `localflow`) - voice control of the PC.

Whisper on your own GPU, cleanup by a local LLM, text injected into whatever
window has focus. No audio leaves the machine.
"""

__version__ = "0.1.0"

#: The name Windows shows on notifications (and the Start menu shortcut).
#: Renaming the app starts here.
APP_NAME = "Golden Flow"
#: Windows' ID for the app: notifications are labelled with APP_NAME, not
#: "Python" (the process is pythonw.exe). Kept the same across renames (it
#: was LocalFlow) so Windows' notification settings for it carry over.
APP_ID = "LocalFlow.App"
