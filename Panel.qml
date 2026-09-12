import QtQuick
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import "ProcessEnvironment.js" as ProcessEnvironment

Panel {
  id: root
  moduleName: "edbron.cloud-drives"
  ipcTarget: "edbron.cloud-drives"
  manageIpc: false

  readonly property string script: decodeURIComponent(String(Qt.resolvedUrl("bin/omarchy-cloud-drives")).replace(/^file:\/\//, ""))
  readonly property string signinLauncher: decodeURIComponent(String(Qt.resolvedUrl("bin/icloud-signin.py")).replace(/^file:\/\//, ""))
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  property bool rcloneInstalled: false
  property bool encrypted: false
  property bool keyring: false
  property bool ready: false
  property string mountRoot: ""
  property var providers: []
  property string lastError: ""
  property string stateError: ""
  property bool busy: false
  property string pendingAction: ""
  property string pendingProvider: ""
  signal actionFinished(string action, string providerId, bool ok, string message)

  readonly property int mountedCount: providers.filter(function(p) { return p.mounted }).length
  readonly property int connectedCount: providers.filter(function(p) { return p.configured }).length

  function refresh() { if (!stateProc.running) stateProc.running = true }

  function run(action, id) {
    if (busy) return
    lastError = ""
    if ((action === "connect" || action === "reconnect") && id === "icloud") {
      busy = true
      try {
        // Detached dispatch is intentionally untracked. Quickshell's current
        // API returns void; the supervisor owns its lock, timeout and cleanup.
        signinProc.startDetached()
      } catch (e) {
        busy = false
        lastError = "Could not open iCloud sign-in. Try again."
        return
      }
      busy = false
      refresh()
      close()
      return
    }
    if (action === "connect" || action === "disconnect") {
      terminalProc.command = [script, "launch", action, id]
      terminalProc.startDetached()
      close()
      return
    }
    pendingAction = action
    pendingProvider = id
    busy = true
    actionProc.command = [script, action, id]
    actionProc.running = true
  }

  function stateIpc() {
    return JSON.stringify({ rclone: rcloneInstalled, encrypted: encrypted, keyring: keyring,
      ready: ready, error: lastError || stateError, providers: providers })
  }

  function providerStatus(p) {
    if (p.error) return "Needs attention · reconnect to try again"
    if (p.mounted) return String(p.path || "Connected").replace(/^\/home\/[^/]+/, "~")
    if (p.active) return "Connecting your folder…"
    return p.configured ? "Connected · folder closed" : "Ready when you are"
  }

  IpcHandler {
    target: "edbron.cloud-drives"
    function state(): string { return root.stateIpc() }
    function refresh(): string { root.refresh(); return "ok" }
    function mount(id: string): string { root.run("mount", id); return "ok" }
    function unmount(id: string): string { root.run("unmount", id); return "ok" }
    function connect(id: string): string { root.run("connect", id); return "ok" }
    function open() { root.open() }
    function close() { root.close() }
    function toggle() { root.toggle() }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  Component.onCompleted: refresh()
  onOpenedChanged: if (opened) refresh()

  Timer {
    interval: root.opened ? 2000 : 30000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Process {
    id: stateProc
    clearEnvironment: true
    environment: ProcessEnvironment.build(function(name) { return Quickshell.env(name) }, false)
    command: [root.script, "state"]
    stdout: StdioCollector { id: stateOutput; waitForEnd: true }
    // Account details and diagnostics never become error text in the shell.
    stderr: SplitParser { onRead: function(data) {} }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.stateError = "Could not check your drives. Try Refresh."
        return
      }
      try {
        if (stateOutput.text.length > 65536) throw new Error("oversize")
        var s = JSON.parse(stateOutput.text)
        if (!Array.isArray(s.providers)) throw new Error("invalid state")
        root.rcloneInstalled = s.rclone === true
        root.encrypted = s.encrypted === true
        root.keyring = s.keyring === true
        root.ready = s.ready === true
        root.mountRoot = String(s.root || "").slice(0, 1024)
        root.providers = s.providers.filter(function(p) {
          return p && ["icloud", "google", "onedrive"].indexOf(p.id) !== -1
        }).slice(0, 3).map(function(p) {
          var labels = { icloud: ["iCloud Drive", "󰀸"], google: ["Google Drive", "󰊶"], onedrive: ["OneDrive", "󰏊"] }
          return { id: p.id, name: labels[p.id][0], glyph: labels[p.id][1],
            configured: p.configured === true, mounted: p.mounted === true,
            active: p.active === true, error: !!p.error, path: String(p.path || "").slice(0, 1024) }
        }).sort(function(a, b) {
          return a.id === "icloud" ? -1 : (b.id === "icloud" ? 1 : 0)
        })
        // state emits fixed, actionable messages (never authentication stderr).
        root.stateError = String(s.error || "").slice(0, 400)
      } catch (e) {
        root.stateError = "Could not read drive status. Try Refresh."
      }
    }
  }

  Process {
    id: terminalProc
    clearEnvironment: true
    environment: ProcessEnvironment.build(function(name) { return Quickshell.env(name) }, true)
  }

  Process {
    id: signinProc
    clearEnvironment: true
    environment: ProcessEnvironment.build(function(name) { return Quickshell.env(name) }, true)
    // Detached so a bar reload cannot kill the protected supervisor before it
    // cleans up its children. No credentials, output or child lifetime return
    // to the bar; status polling observes the resulting mount independently.
    command: ["/usr/bin/python3", "-Es", root.signinLauncher]
  }

  Process {
    id: actionProc
    clearEnvironment: true
    environment: ProcessEnvironment.build(function(name) { return Quickshell.env(name) }, true)
    stdout: SplitParser { onRead: function(data) {} }
    stderr: SplitParser { onRead: function(data) {} }
    onExited: function(exitCode) {
      var action = root.pendingAction
      var provider = root.pendingProvider
      var message = ""
      if (exitCode !== 0) {
        if (action === "open") message = "Could not open your folder. Check that the drive is connected."
        else if (action === "unmount") message = "Could not close this drive. Finish using its files and try again."
        else message = "Your folder could not connect. Check your connection, then try again."
        root.lastError = message
      }
      root.busy = false
      root.pendingAction = ""
      root.pendingProvider = ""
      root.actionFinished(action, provider, exitCode === 0, message)
      root.refresh()
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.mountedCount > 0 ? "󰅟" : "󰅧"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: contentFocus
    contentWidth: panel.fittedContentWidth(Style.space(390))
    contentHeight: panel.fittedContentHeight(dashboard.implicitHeight, Style.space(660))

    FocusScope {
      id: contentFocus
      anchors.fill: parent
      Keys.onEscapePressed: root.close()

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: dashboard.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }

        Column {
          id: dashboard
          width: parent.width
          spacing: Style.space(16)

          PanelHero {
            width: parent.width
            title: "Cloud Drives"
            meta: root.mountedCount ? root.mountedCount + " " + (root.mountedCount === 1 ? "folder" : "folders") + " connected" : "Your files, close at hand"
            foreground: root.foreground
            iconComponent: Component {
              Text { text: "󰅟"; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.display }
            }
          }

          Text {
            visible: root.lastError !== "" || root.stateError !== ""
            width: parent.width
            text: root.lastError || root.stateError
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Column {
            width: parent.width
            spacing: Style.space(12)
            Repeater {
              model: root.providers
              delegate: Column {
                id: driveRow
                required property var modelData
                width: dashboard.width
                spacing: Style.space(8)

                Row {
                  width: parent.width
                  spacing: Style.space(10)
                  Text {
                    width: Style.space(26)
                    text: String(driveRow.modelData.glyph || "󰅟").slice(0, 4)
                    textFormat: Text.PlainText
                    color: driveRow.modelData.mounted ? root.foreground : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.title
                    anchors.verticalCenter: parent.verticalCenter
                  }
                  Column {
                    width: parent.width - Style.space(36)
                    spacing: Style.space(3)
                    Text {
                      width: parent.width
                      text: String(driveRow.modelData.name || "Drive").slice(0, 80)
                      textFormat: Text.PlainText
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                      elide: Text.ElideRight
                    }
                    Text {
                      width: parent.width
                      text: root.providerStatus(driveRow.modelData)
                      textFormat: Text.PlainText
                      color: driveRow.modelData.error ? Color.urgent : root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideMiddle
                    }
                  }
                }

                Flow {
                  width: parent.width
                  spacing: Style.space(5)
                  Button {
                    text: driveRow.modelData.configured ? (driveRow.modelData.mounted ? "Open folder" : "Connect folder") : "Connect"
                    iconText: driveRow.modelData.mounted ? "󰉋" : "󰌘"
                    bordered: true
                    focusable: true
                    enabled: !root.busy
                    fontSize: Style.font.caption
                    onClicked: root.run(!driveRow.modelData.configured ? "connect" : (driveRow.modelData.mounted ? "open" : "mount"), driveRow.modelData.id)
                  }
                  Button {
                    visible: driveRow.modelData.id === "icloud" && driveRow.modelData.configured
                    text: "Reconnect iCloud"
                    focusable: true
                    enabled: !root.busy
                    fontSize: Style.font.caption
                    onClicked: root.run("reconnect", "icloud")
                  }
                  Button {
                    visible: driveRow.modelData.mounted
                    text: "Close drive"
                    focusable: true
                    enabled: !root.busy
                    fontSize: Style.font.caption
                    onClicked: root.run("unmount", driveRow.modelData.id)
                  }
                  Button {
                    visible: driveRow.modelData.configured && !driveRow.modelData.mounted
                    text: "Forget"
                    focusable: true
                    enabled: !root.busy
                    fontSize: Style.font.caption
                    foreground: root.dim
                    onClicked: root.run("disconnect", driveRow.modelData.id)
                  }
                }
                PanelSeparator { width: parent.width; foreground: root.foreground }
              }
            }
          }

          Text {
            width: parent.width
            text: "Files open on demand in your file manager."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
          Button {
            id: refreshButton
            text: root.busy ? "Working…" : "Refresh"
            iconText: "󰑐"
            iconSpinning: root.busy
            focusable: true
            enabled: !root.busy
            fontSize: Style.font.caption
            onClicked: { root.lastError = ""; root.refresh() }
          }
        }
      }
    }
  }
}
