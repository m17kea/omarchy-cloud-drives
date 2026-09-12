import QtQuick
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import "." as Plugin
import "ProcessEnvironment.js" as ProcessEnvironment

// The launcher stages this file and ICloudSetup.qml in a private runtime
// directory, adds the installed Omarchy UI imports, and sets this fixed path.
// This process has no IPC handler, credential logging, or capture functionality.
Scope {
  id: root
  readonly property string pluginDir: Quickshell.env("CLOUD_DRIVES_PLUGIN_DIR")
  readonly property string script: pluginDir + "/bin/omarchy-cloud-drives"
  property bool closing: false
  property bool ready: false
  property string stateError: ""
  property var provider: ({ mounted: false, configured: false, path: "" })
  property string pendingAction: ""

  function refresh() {
    if (closing || stateProc.running) return
    if (pluginDir.charAt(0) !== "/") {
      stateError = "The sign-in window could not find its installation. Close it and try again."
      return
    }
    stateProc.running = true
  }

  function requestClose() {
    if (closing) return
    closing = true
    wizard.cancel()
    window.visible = false
    tryQuit()
  }

  function tryQuit() {
    // Let the authentication helper reap rclone and remove staging before
    // exiting. The launcher also owns cleanup if this UI process crashes.
    if (closing && !wizard.helperRunning && !actionProc.running && !stateProc.running)
      Qt.quit()
  }

  function runAction(action) {
    if (closing || actionProc.running || pluginDir.charAt(0) !== "/") return
    pendingAction = action
    actionProc.command = [script, action, "icloud"]
    actionProc.running = true
  }

  Component.onCompleted: refresh()

  Timer {
    interval: 2000
    running: !root.closing
    repeat: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 100
    running: root.closing
    repeat: true
    onTriggered: root.tryQuit()
  }

  Process {
    id: stateProc
    clearEnvironment: true
    environment: ProcessEnvironment.build(function(name) { return Quickshell.env(name) }, false)
    command: [root.script, "state"]
    stdout: StdioCollector { id: stateOutput; waitForEnd: true }
    stderr: SplitParser { onRead: function(data) {} }
    onExited: function(exitCode) {
      if (!root.closing) {
        try {
          if (exitCode !== 0 || stateOutput.text.length > 65536) throw new Error("state")
          var state = JSON.parse(stateOutput.text)
          root.ready = state.ready === true
          // The local status helper emits fixed, actionable setup messages.
          root.stateError = String(state.error || "").slice(0, 400)
          var providers = Array.isArray(state.providers) ? state.providers : []
          for (var i = 0; i < Math.min(providers.length, 3); i++) {
            var p = providers[i]
            if (p && p.id === "icloud") {
              root.provider = { configured: p.configured === true, mounted: p.mounted === true,
                path: String(p.path || "").slice(0, 1024) }
              break
            }
          }
        } catch (e) {
          root.ready = false
          root.stateError = "Could not check drive setup. Try Check again."
        }
      }
      root.tryQuit()
    }
  }

  Process {
    id: preparationProc
    clearEnvironment: true
    environment: ProcessEnvironment.build(function(name) { return Quickshell.env(name) }, true)
    command: [root.script, "launch", "setup"]
  }

  Process {
    id: actionProc
    clearEnvironment: true
    environment: ProcessEnvironment.build(function(name) { return Quickshell.env(name) }, true)
    stdout: SplitParser { onRead: function(data) {} }
    stderr: SplitParser { onRead: function(data) {} }
    onExited: function(exitCode) {
      var action = root.pendingAction
      root.pendingAction = ""
      if (!root.closing) {
        if (action === "reconnect-mount") {
          wizard.mountFinished(exitCode === 0, exitCode === 0 ? "" :
            "Your account is saved, but the folder could not connect. Check your connection and try again.")
        } else if (action === "open") {
          wizard.openFinished(exitCode === 0, exitCode === 0 ? "" :
            "Could not open your folder. Check that the drive is connected.")
        }
        root.refresh()
      }
      root.tryQuit()
    }
  }

  FloatingWindow {
    id: window
    title: "Connect iCloud"
    visible: !root.closing
    readonly property int pagePadding: Style.space(22)
    implicitWidth: Math.min(Style.space(440), screen ? Math.max(240, screen.width - Style.space(40)) : Style.space(440))
    implicitHeight: Math.min(wizard.implicitHeight + pagePadding * 2,
      screen ? Math.max(200, screen.height - Style.space(80)) : Style.space(640))
    minimumSize: Qt.size(240, 200)
    color: Color.popups.background
    onClosed: root.requestClose()

    Flickable {
      id: scroll
      anchors.fill: parent
      anchors.margins: window.pagePadding
      contentWidth: width
      contentHeight: wizard.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      flickableDirection: Flickable.VerticalFlick
      Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }

      Plugin.ICloudSetup {
        id: wizard
        width: parent.width
        active: !root.closing
        ready: root.ready
        preparationError: root.stateError
        provider: root.provider
        foreground: Color.foreground
        helper: root.pluginDir + "/bin/icloud-onboarding.py"
        onPrepareRequested: {
          if (!root.closing && root.pluginDir.charAt(0) === "/")
            preparationProc.startDetached()
        }
        onRefreshRequested: root.refresh()
        onMountRequested: root.runAction("reconnect-mount")
        onOpenFolderRequested: root.runAction("open")
        onDismissed: root.requestClose()
        onHelperRunningChanged: if (root.closing) Qt.callLater(root.tryQuit)
        onStepChanged: scroll.contentY = 0
        Component.onCompleted: begin()
      }
    }
  }
}
